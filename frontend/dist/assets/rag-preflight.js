import { partitionRagUnits } from "./rag-partition.js";
import { hasTokens, hasPage, pageOf, validPage, documentOf, canonical, sameDocument, failedPages,
  ragStructureBoundaries, ragTextSeparator } from "./rag-structure.js";

export function tokenRangeStatus(tokens, { min_tokens, max_tokens }) {
  if (tokens === null) return "unknown";
  if (tokens < min_tokens) return "below";
  if (tokens > max_tokens) return "above";
  return "within";
}

export function planRagAggregation(value, { min_tokens, max_tokens, page_boundary = false,
  keep_tables_together = true, keep_code_blocks_together = true }) {
  if (!Number.isSafeInteger(min_tokens) || min_tokens < 1
    || !Number.isSafeInteger(max_tokens) || max_tokens < min_tokens) {
    throw new Error("Enter positive whole token counts, with minimum no greater than maximum.");
  }
  if (typeof page_boundary !== "boolean") throw new Error("Page boundary setting must be selected.");
  if (!Array.isArray(value?.chunks)) throw new Error("Select a JSON RAG file first.");
  if (typeof keep_tables_together !== "boolean" || typeof keep_code_blocks_together !== "boolean") {
    throw new Error("Structure protection settings must be selected.");
  }
  const options = { min_tokens, max_tokens, page_boundary, keep_tables_together, keep_code_blocks_together };
  const chunks = value.chunks, groups = [];
  chunks.forEach(chunk => {
    if (!chunk || typeof chunk.text !== "string") throw new Error("Invalid raw chunk text.");
  });
  const structure = ragStructureBoundaries(chunks, value.metadata, options);
  const tightJoins = structure.joins;
  // A refined file stores a list when a merged chunk already spans pages.
  const pagesOf = chunk => hasPage(chunk) ? [pageOf(chunk)] : Array.isArray(chunk.page) ? chunk.page.filter(validPage) : [];
  const spans = chunk => !hasPage(chunk) && pagesOf(chunk).length > 0;

  function emit(indices, boundary) {
    const missing = indices.filter(index => !hasTokens(chunks[index])).length;
    const known = indices.reduce((total, index) => total + (hasTokens(chunks[index]) ? chunks[index].token_count : 0), 0);
    const tokens = missing || !Number.isSafeInteger(known) ? null : known;
    const pages = new Set();
    let missingPages = 0;
    for (const index of indices) {
      const found = pagesOf(chunks[index]);
      if (!found.length) missingPages++;
      for (const page of found) pages.add(page);
    }
    const status = tokenRangeStatus(tokens, options);
    let reason = status === "unknown" ? "Token estimate unavailable or exceeds the safe numeric range."
      : status === "below" ? `Below target after grouping whole blocks${boundary ? ` (${boundary})` : ""}.`
      : status === "above" ? "An indivisible block or protected structure exceeds the target maximum."
          : "Within the estimated token range.";
    if (page_boundary && missingPages) reason += " Page grouping cannot be verified: page number missing.";
    if (page_boundary && indices.some(index => spans(chunks[index]))) reason += " Already spans several pages, so it stays separate.";
    groups.push({ source_indices: indices, pages: [...pages], missing_pages: missingPages,
      // Any structural continuation stays compact, protected or not.
      text_separators: indices.slice(1).map((index, offset) => ragTextSeparator(chunks[indices[offset]].text,
        chunks[index].text, structure.continuations.has(indices[offset]))),
      token_count: tokens, known_tokens: Number.isSafeInteger(known) ? known : null, missing_counts: missing, status, reason });
  }

  let run = [];
  // Only the run's last group ends at the boundary; earlier ones were cut by the range.
  function flush(boundary) {
    const parts = partitionRagUnits(run.map(unit => unit.tokens), min_tokens, max_tokens);
    parts.forEach(([start, end], part) =>
      emit(run.slice(start, end).flatMap(unit => unit.indices), part === parts.length - 1 ? boundary : null));
    run = [];
  }
  // Form protected atoms before isolating unknown counts. A missing estimate
  // inside a table must not split that table between preflight and execution.
  for (let index = 0; index < chunks.length;) {
    if (index > 0 && structure.hard.has(index - 1)) flush(
      !sameDocument(chunks[index - 1], chunks[index], value.metadata) ? "document change"
        : structure.gaps.has(index - 1) ? "failed-page gap" : "page change");
    const indices = [index++];
    while (index < chunks.length && tightJoins.has(index - 1)) indices.push(index++);
    const known = indices.every(i => hasTokens(chunks[i]));
    const tokens = known ? indices.reduce((sum, i) => sum + chunks[i].token_count, 0) : null;
    if (tokens === null || !Number.isSafeInteger(tokens)
      || (page_boundary && indices.some(i => !hasPage(chunks[i])))) {
      // A block already spanning pages says so in its own reason.
      const boundary = tokens !== null && Number.isSafeInteger(tokens)
        && indices.every(i => hasPage(chunks[i]) || spans(chunks[i])) ? null : "missing source information";
      flush(boundary);
      emit(indices, boundary);
    } else run.push({ indices, tokens });
  }
  flush("end of document");

  const counts = { within: 0, below: 0, above: 0, unknown: 0 };
  for (const group of groups) counts[group.status]++;
  const known = groups.filter(group => group.token_count !== null).map(group => group.token_count);
  const warnings = [];
  if (structure.conflicts) warnings.push("A protected structure crosses a forced page boundary; the page boundary takes priority.");
  if (structure.incomplete) warnings.push("An unclosed table or code fence is kept together until its document ends or a block of another type follows.");
  if (structure.hidden) warnings.push("An unclosed table or code fence whose protection is off hides later structures from the enabled protection.");
  if (structure.gaps.size) warnings.push("Failed pages are hard boundaries: blocks on either side of one are never merged.");
  if (groups.some(group => group.known_tokens === null)) warnings.push("A token sum exceeds the safe numeric range; its size is unknown.");
  if (chunks.some(chunk => !hasTokens(chunk))) warnings.push("Some raw blocks have no valid token count; their size cannot be estimated.");
  if (page_boundary && chunks.some(chunk => !pagesOf(chunk).length)) warnings.push("Blocks without a page number are kept separate; page grouping is unverified for those blocks.");
  if (page_boundary && chunks.some(spans)) warnings.push("Blocks that already span several pages are kept separate.");
  const failed = failedPages(value.metadata);
  if (failed.length) warnings.push(`The source reports failed pages: ${failed.join(", ")}.`);
  if (value.metadata?.unattached_blocks > 0) warnings.push(`The source reports ${value.metadata.unattached_blocks} unattached blocks.`);
  return { options, groups, counts, warnings,
    input_count: chunks.length, min_tokens: known.length ? known.reduce((a, b) => Math.min(a, b)) : null,
    max_tokens: known.length ? known.reduce((a, b) => Math.max(a, b)) : null,
    mean_tokens: known.length ? Math.round(known.reduce((a, b) => a + b / known.length, 0)) : null };
}

// Plan entries are a whole chunk (an integer index) or a range
// ``[index, start, end]``, optionally a ``:repeat`` framing span.
const entryIndex = entry => Array.isArray(entry) ? entry[0] : entry;
const sourceOffsets = new WeakMap();

// Server ranges use Unicode code points; JavaScript slice uses UTF-16 units.
// Build the conversion once per source chunk containing astral characters.
export function entryText(value, entry) {
  const chunk = value.chunks[entryIndex(entry)], text = chunk.text;
  if (!Array.isArray(entry)) return text;
  let cached = sourceOffsets.get(chunk);
  if (!cached || cached.text !== text) {
    const offsets = /[\uD800-\uDBFF][\uDC00-\uDFFF]/.test(text) ? [0] : null;
    if (offsets) for (const character of text) offsets.push(offsets[offsets.length - 1] + character.length);
    cached = { text, offsets };
    sourceOffsets.set(chunk, cached);
  }
  if (!cached.offsets) return text.slice(entry[1], entry[2]);
  return text.slice(cached.offsets[entry[1]], cached.offsets[entry[2]]);
}
// A preflight group is an object with source_indices and text_separators; a
// gesture group is the bare entry list.
const groupEntries = group => Array.isArray(group) ? group : group.source_indices;
const groupSeparators = group => Array.isArray(group) ? undefined : group.text_separators;

// The stable identity of a range, the key the gesture's measured counts use.
// A whole chunk is its index; ranges append the optional framing kind.
export function entryKey(value, entry) {
  if (!Array.isArray(entry)) return String(entry);
  return `${entry[0]}:${entry[1]}-${entry[2]}${entry.length > 3 ? `:${entry[3]}` : ""}`;
}

// A whole chunk keeps its saved estimate; a range has none until the split
// endpoint measures it, so its count lives in the gesture's map.
export function memberCount(value, entry, counts = new Map()) {
  if (!Array.isArray(entry)) {
    return hasTokens(value.chunks[entry]) ? value.chunks[entry].token_count : null;
  }
  return counts.get(entryKey(value, entry)) ?? null;
}

// Entries of one chunk join compactly; a chunk change uses the separator the
// preflight stored for that boundary or, for a bare gesture group, the real
// structural continuation flag, the same rule the server applies while saving
// (NFR-003). Contract for callers (Task 7 gestures and the parity test):
// compute the continuation set once with ragStructureBoundaries(value.chunks,
// value.metadata, options) and pass { continuations } to every groupText call;
// an explicit separators option wins, otherwise a preflight group's stored
// text_separators are used, and a bare group falls back to continuations.
export function groupText(value, group, { separators = null, continuations = new Set() } = {}) {
  const entries = groupEntries(group), stored = separators ?? groupSeparators(group);
  let text = "", previous = null, previousIndex = -1, transitions = 0;
  for (const entry of entries) {
    const index = entryIndex(entry), piece = entryText(value, entry);
    if (previous !== null) {
      if (index === previousIndex) text += ragTextSeparator(previous, piece, true);
      else if (stored !== undefined && stored !== null) text += stored[transitions++];
      else text += ragTextSeparator(previous, piece, continuations.has(previousIndex));
    }
    text += piece;
    previous = piece;
    previousIndex = index;
  }
  return text;
}

export function groupTokenCount(value, group, counts = new Map()) {
  const entries = groupEntries(group);
  let total = 0;
  for (const entry of entries) {
    const count = memberCount(value, entry, counts);
    if (count === null) return null;
    total += count;
  }
  return entries.length && Number.isSafeInteger(total) ? total : null;
}

export function ragGroupPreview(value, group) {
  const sources = group.source_indices.map(entry => {
    const index = Array.isArray(entry) ? entry[0] : entry;
    return value.chunks[index];
  }).filter(Boolean);
  return {
    text: groupText(value, group),
    // Keep per-source payloads intact, including repeated values and custom fields.
    // Field flattening for the saved output is independent from range preflight.
    source_payloads: sources.map(({ text, ...payload }) => payload),
  };
}

// Chunk merge for the section 04 preview: the same fields that
// ocrserver/rag_refinement.py materialize() writes — id, text, token_count,
// document, plus each source payload field as a list of its distinct values
// (always a list; no per-source copies) — without the
// execution metadata the server adds when it saves the file.
// tests/test_rag_refinement.py compares this output with the saved file for
// every structure fixture and protection combination.
const MERGED = new Set(["id", "text", "token_count", "document"]);
// ASCII whitespace only: String.trim and Python's str.strip disagree on the rest.
const trimAscii = text => text.replace(/^[ \t\n\r\f\v]+|[ \t\n\r\f\v]+$/g, "");

export function ragAggregationPreview(value, plan, counts = new Map()) {
  const merged = plan.groups.map((group, index) => {
    const sources = group.source_indices.map(source => value.chunks[entryIndex(source)]);
    const entries = [["id", index + 1], ["text", groupText(value, group)]];
    // Empty fields are absent, not null, as in the raw ingest file.
    const document = documentOf(sources[0], value.metadata);
    const tokens = groupTokenCount(value, group, counts);
    if (tokens !== null) entries.push(["token_count", tokens]);
    if (document !== null) entries.push(["document", document]);
    const fields = new Map();
    for (const source of sources) {
      for (const [key, field] of Object.entries(source)) {
        if (MERGED.has(key) || field === null) continue;
        // A printed folio repeating the physical index is redundant.
        if (key === "page_number" && hasPage(source)
          && typeof field === "string" && trimAscii(field) === String(pageOf(source))) continue;
        if (!fields.has(key)) fields.set(key, new Map());
        const values = fields.get(key), id = canonical(field);
        if (!values.has(id)) values.set(id, field);
      }
    }
    for (const [key, map] of fields) entries.push([key, [...map.values()]]);
    // fromEntries keeps a "__proto__" payload key as data, as Python saves it.
    return Object.fromEntries(entries);
  });
  const tokens = merged.map(chunk => chunk.token_count);
  const total = tokens.reduce((sum, token) => sum + token, 0);
  // Beyond this bound doubles lose exactness; materialize() reports unknown too.
  const complete = tokens.length > 0 && tokens.every(token => Number.isSafeInteger(token))
    && Number.isSafeInteger(200 * total);
  return {
    ...value,
    chunks: merged,
    metadata: {
      ...(value.metadata ?? {}),
      n_chunks: merged.length,
      total_tokens: complete ? total : null,
      // Exact half-up hundredths, as materialize() computes them.
      avg_tokens: complete ? Math.floor((200 * total + tokens.length) / (2 * tokens.length)) / 100 : null,
      min_tokens: complete ? tokens.reduce((a, b) => Math.min(a, b)) : null,
      max_tokens: complete ? tokens.reduce((a, b) => Math.max(a, b)) : null,
    },
  };
}
