// Pure model for the two operator gestures of the RAG console: a split that
// replaces one over-maximum row with measured fragments, and a glue that
// merges a below-minimum row with its neighbour. No DOM, no fetch.
import { entryKey, groupTokenCount, memberCount } from "./rag-preflight.js";
import { hasPage, pageOf, documentOf, failedPages } from "./rag-structure.js";

// FR-022: the browser measures only the encoded ``groups``, while the request
// body carries the rest of the refine payload too, so the warning threshold
// keeps headroom under the server's 2 MB and an unwarned 413 cannot happen.
const PLAN_CAP_BYTES = 1_900_000;

const GLUE_ABOVE = "above_maximum_fragment";
const GLUE_PAGE = "page_boundary_forced";
const GLUE_DOCUMENT = "document_forced";
const GLUE_GAP = "failed_gap_forced";

const NOTE_LABELS = {
  table_split: "Table split",
  code_split: "Code block split",
  grapheme_split: "Text split",
  math_atomic: "Formula kept whole",
  page_boundary_forced: "Page boundary crossed",
  document_forced: "Document boundary crossed",
  failed_gap_forced: "Failed-page gap crossed",
  above_maximum_fragment: "Fragment above maximum",
  below_minimum_fragment: "Fragment below minimum",
  token_count_unknown: "Token count unavailable",
  search_budget_exhausted: "Split search limit reached",
};

// Split responses carry diagnostics as objects; local glue notes are codes.
// Keep their codes for the save contract and render their text separately.
export function noteCode(note) {
  return typeof note === "string" ? note : typeof note?.code === "string" ? note.code : "";
}

export function noteText(note) {
  const parameters = Array.isArray(note?.parameters) ? note.parameters : [];
  const code = noteCode(note), detail = parameters.find(item => item.name === "detail")?.value;
  const label = NOTE_LABELS[code] ?? (typeof detail === "string" ? detail : code.replaceAll("_", " "));
  const tokens = parameters.find(item => item.name === "tokens")?.value;
  return Number.isSafeInteger(tokens) && tokens >= 0 ? `${label} (${tokens} tokens)` : label;
}

// Availability of the scissors and the two glues for one row. A single reason
// explains the first disabled control, so a disabled button can carry it in
// its title (FR-015).
export function gestureFor(status, position, count, { tokenizerUnavailable = false } = {}) {
  const splitAllowed = status === "above" || status === "unknown";
  const glueAllowed = status === "below" || status === "unknown";
  const scissors = splitAllowed && !tokenizerUnavailable;
  const gluePrevious = glueAllowed && position > 0;
  const glueNext = glueAllowed && position < count - 1;
  let reason = "";
  if (splitAllowed && tokenizerUnavailable) reason = "Cutting is unavailable: the tokenizer is unavailable.";
  else if (glueAllowed && position === 0) reason = "Merging with the previous group is unavailable: this is the first row.";
  else if (glueAllowed && position === count - 1) reason = "Merging with the next group is unavailable: this is the last row.";
  return { scissors, gluePrevious, glueNext, reason };
}

// Counts keyed by the stable identity of a range entry; whole chunks keep the
// saved estimate, so only range entries are stored here.
export function countsOf(state) {
  return state?.counts ?? new Map();
}

// One measured count per entry of a fragment: the fragment's token_count lands
// on its last payload entry, repeated framing entries count zero.
function recordFragmentCounts(value, fragment, counts) {
  const entries = fragment.entries ?? [];
  if (Array.isArray(fragment.member_token_counts)) {
    const bundles = [];
    for (const entry of entries) {
      const index = Array.isArray(entry) ? entry[0] : entry;
      const last = bundles[bundles.length - 1];
      if (last?.index === index) last.entries.push(entry);
      else bundles.push({ index, entries: [entry] });
    }
    if (bundles.length === fragment.member_token_counts.length) {
      bundles.forEach((bundle, index) => {
        const payload = bundle.entries.filter(entry => !Array.isArray(entry) || entry.length < 4);
        const target = payload.at(-1) ?? bundle.entries.at(-1);
        for (const entry of bundle.entries) if (Array.isArray(entry)) {
          counts.set(entryKey(value, entry), entry === target ? fragment.member_token_counts[index] : 0);
        }
      });
      return;
    }
  }
  const payload = entries.filter(entry => !Array.isArray(entry) || entry.length < 4);
  const target = payload.length ? payload[payload.length - 1] : entries[entries.length - 1];
  for (const entry of entries) {
    if (!Array.isArray(entry)) continue;
    counts.set(entryKey(value, entry), entry === target ? Number(fragment.token_count) || 0 : 0);
  }
}

// R22a: one id source for every gesture kind, so a split and a glue can never
// collide on the same number.
export function gestureId(gestures) {
  return String(gestures.reduce((max, item) => Math.max(max, Number(item.id) || 0), 0) + 1);
}

// R24b: a glue between two groups whose entries slice the same chunk is not a
// merge but a recomposition, which undo performs and which keeps the browser's
// member sum and the server's bundle measurement provably equal.
export function sameChunk(left, right) {
  const indices = new Set(left.map(entry => Array.isArray(entry) ? entry[0] : entry));
  return right.some(entry => indices.has(Array.isArray(entry) ? entry[0] : entry));
}

// R24: the per-chunk member counts the console displays, in group order; the
// refine request carries them so the server can refuse a real discordance.
export function memberCounts(value, group, counts = new Map()) {
  const members = [];
  for (const entry of group) {
    const index = Array.isArray(entry) ? entry[0] : entry;
    const count = memberCount(value, entry, counts);
    const last = members[members.length - 1];
    if (last && last.index === index) {
      last.count = last.count === null || count === null ? null : last.count + count;
    } else {
      members.push({ index, count });
    }
  }
  return members.map(member => member.count);
}

export function applySplit(value, groups, index, fragments, gestures = []) {
  const slice = [groups[index]];
  const produced = fragments.map(fragment => fragment.entries);
  const counts = new Map();
  for (const fragment of fragments) recordFragmentCounts(value, fragment, counts);
  const next = [...groups.slice(0, index), ...produced, ...groups.slice(index + 1)];
  // The record keeps its own measurements so an undo can rebuild the counts.
  const record = { id: gestureId(gestures), kind: "split", slice, produced, counts };
  return { groups: next, gestures: [record], counts };
}

// Pages and documents of the chunks referenced by a group's entries.
function pagesOfGroup(value, group) {
  const pages = [];
  for (const entry of group) {
    const index = Array.isArray(entry) ? entry[0] : entry;
    const chunk = value.chunks[index];
    if (chunk && hasPage(chunk)) pages.push(pageOf(chunk));
  }
  return pages;
}

function failedGap(value, left, right) {
  const pages = [...pagesOfGroup(value, left), ...pagesOfGroup(value, right)];
  if (!pages.length) return false;
  const low = Math.min(...pages), high = Math.max(...pages);
  return failedPages(value.metadata).some(page => page > low && page < high);
}

function note(list, code) {
  if (!list.includes(code)) list.push(code);
}

export function glueGroups(value, groups, index, direction, options, counts = new Map(), gestures = []) {
  const neighbour = direction === "previous" ? index - 1 : index + 1;
  if (neighbour < 0 || neighbour >= groups.length) {
    return { groups, notes: [], gestures, counts };
  }
  const left = groups[Math.min(index, neighbour)];
  const right = groups[Math.max(index, neighbour)];
  if (sameChunk(left, right)) {
    return { groups, notes: [], gestures, counts };
  }
  const merged = [...left, ...right];
  const at = Math.min(index, neighbour);
  const next = [...groups.slice(0, at), merged, ...groups.slice(Math.max(index, neighbour) + 1)];
  const notes = [];
  const tokens = groupTokenCount(value, merged, counts);
  if (tokens !== null && Number.isSafeInteger(options.max_tokens) && tokens > options.max_tokens) {
    note(notes, GLUE_ABOVE);
  }
  if (options.page_boundary) {
    // Exactly the server's rule: the merged group spans more than one page,
    // i.e. the union of its members' pages has more than one element.
    const pages = new Set([...pagesOfGroup(value, left), ...pagesOfGroup(value, right)]);
    if (pages.size > 1) note(notes, GLUE_PAGE);
  }
  const leftDoc = value.chunks[Array.isArray(left[0]) ? left[0][0] : left[0]];
  const rightDoc = value.chunks[Array.isArray(right[0]) ? right[0][0] : right[0]];
  if (leftDoc && rightDoc && JSON.stringify(documentOf(leftDoc, value.metadata)) !== JSON.stringify(documentOf(rightDoc, value.metadata))) {
    note(notes, GLUE_DOCUMENT);
  }
  if (failedGap(value, left, right)) note(notes, GLUE_GAP);
  const nextId = gestureId(gestures);
  const record = { id: nextId, kind: "glue", slice: [left, right], produced: [merged] };
  return { groups: next, notes, gestures: [...gestures, record], counts };
}

function dependents(state, gesture, after) {
  return after.find(later => later.slice.some(group => gesture.produced.includes(group)));
}

export function undoGesture(state, gestureId) {
  const index = state.gestures.findIndex(item => String(item.id) === String(gestureId));
  if (index < 0) return { groups: state.groups, gestures: state.gestures, counts: state.counts };
  const gesture = state.gestures[index];
  const later = dependents(state, gesture, state.gestures.slice(index + 1));
  if (later) return { blockedBy: String(later.id) };
  const groups = [...state.groups];
  const at = groups.indexOf(gesture.produced[0]);
  if (at >= 0) groups.splice(at, gesture.produced.length, ...gesture.slice);
  const gestures = state.gestures.filter(item => item !== gesture);
  // Counts are exactly what the remaining cuts measured, replayed in order:
  // undoing a merge keeps its fragments' counts, and undoing a re-cut brings
  // back the earlier cut's values for the ranges both measured.
  const counts = new Map(gestures.flatMap(item => item.counts ? [...item.counts] : []));
  return { groups, gestures, counts };
}

export function resetGestures(state) {
  let current = { groups: state.groups, gestures: state.gestures, counts: state.counts };
  for (const gesture of [...state.gestures].reverse()) {
    const next = undoGesture(current, gesture.id);
    if (next.blockedBy) continue;
    current = next;
  }
  return { groups: current.groups, gestures: [], counts: new Map() };
}

export function encodeGroups(groups) {
  return groups.map(group => group.map(entry => Array.isArray(entry) ? [...entry] : entry));
}

export function planSizeBytes(groups) {
  return new TextEncoder().encode(JSON.stringify(encodeGroups(groups))).length;
}

export function overCap(groups) {
  return planSizeBytes(groups) > PLAN_CAP_BYTES;
}

// FR-022: the cap is 100,000 groups and 2 MB of encoded plan. The console
// re-measures it after every gesture, so this companion reports both facts at
// once from the same row list the gestures mutate.
export function capState(rows) {
  const bytes = planSizeBytes(rows);
  return { tooLarge: rows.length > 100000 || bytes > PLAN_CAP_BYTES, groups: rows.length, bytes };
}
