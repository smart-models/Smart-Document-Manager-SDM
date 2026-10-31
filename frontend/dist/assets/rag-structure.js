// Keep this scanner aligned with ocrserver/rag_structure.py; shared fixtures
// verify that preflight text joins are identical to saved text joins.
export const hasTokens = chunk => Number.isSafeInteger(chunk.token_count) && chunk.token_count >= 0;
export const validPage = page => Number.isSafeInteger(page) && page > 0;
// Refined files store merged fields as lists; only the browser reviews them.
// A single-entry page list is that page.
export const pageOf = chunk => Array.isArray(chunk.page) && chunk.page.length === 1 ? chunk.page[0] : chunk.page;
export const hasPage = chunk => validPage(pageOf(chunk));
export const documentOf = (chunk, metadata) => chunk.document ?? metadata?.document ?? null;
// Equality key shared with canonical() in rag_structure.py: object key order is ignored.
// Integers beyond 2^53 are already rounded by JSON.parse and cannot match exactly.
export const canonical = value => JSON.stringify(value, (key, item) =>
  item && typeof item === "object" && !Array.isArray(item)
    ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)) : item);
export const sameDocument = (a, b, metadata) => canonical(documentOf(a, metadata)) === canonical(documentOf(b, metadata));
// Any structural continuation, protected or not, keeps its text compact.
export const ragTextSeparator = (previous, next, continued) => !continued ? "\n\n"
  : /[\r\n]$/.test(previous) || /^[\r\n]/.test(next) ? "" : "\n";
// The export skips failed pages; anything but a list of page numbers is ignored.
export const failedPages = metadata => Array.isArray(metadata?.failed_pages)
  ? metadata.failed_pages.filter(validPage) : [];

// OCR block labels able to hold each structure. A typed block (type is a string,
// or a list in refined files) joins a structure only when one label allows it;
// untyped blocks are scanned by their text alone.
const TABLE_TYPES = new Set(["table"]), CODE_TYPES = new Set(["algorithm", "code"]);
const allows = (chunk, labels) => {
  const type = chunk.type;
  return typeof type === "string" ? labels.has(type)
    : !Array.isArray(type) || type.some(item => labels.has(item));
};

const fenceLine = /^ {0,3}(`{3,}|~{3,})([^\n]*)$/;
const trimCell = text => text.replace(/^[ \t]+|[ \t]+$/g, "");
function tableCells(text) {
  const cells = [];
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    if (text[i] === "\\") { i++; continue; }
    if (text[i] === "|") { cells.push(trimCell(text.slice(start, i))); start = i + 1; }
  }
  if (!cells.length) return null;
  cells.push(trimCell(text.slice(start)));
  if (cells[0] === "") cells.shift();
  if (cells.at(-1) === "") cells.pop();
  return cells.length ? cells : null;
}
const isRule = cells => cells !== null && cells.every(cell => /^:?-+:?$/.test(cell));

const header = (cells, next) => {
  if (cells === null || isRule(cells) || next === undefined) return false;
  const rule = tableCells(next);
  return isRule(rule) && rule.length === cells.length;
};

// Structural continuations are found whatever the options; joins are the
// continuations whose protection is enabled and that cross no hard boundary.
export function ragStructureBoundaries(chunks, metadata, options) {
  const continuations = new Set(), joins = new Set(), hard = new Set(), gaps = new Set(), crossed = new Set();
  const failed = failedPages(metadata), found = { tables: false, code: false };
  const tables = options.keep_tables_together, code = options.keep_code_blocks_together;
  let fence = null, depth = 0, table = false, last = null;
  let incomplete = 0, hidden = 0;
  const link = (index, enabled) => {
    continuations.add(index);
    if (enabled) (hard.has(index) ? crossed : joins).add(index);
  };
  // An unclosed structure is incomplete when protected. At its document end,
  // unprotected, its literal content hid structures from the other protection.
  const close = end => {
    if (fence !== null) { if (code) incomplete++; else if (end && tables) hidden++; }
    if (depth > 0) { if (tables) incomplete++; else if (end && code) hidden++; }
    fence = null; depth = 0; table = false; last = null;
  };
  for (let index = 0; index < chunks.length; index++) {
    const chunk = chunks[index], previous = chunks[index - 1];
    const tableOk = allows(chunk, TABLE_TYPES), codeOk = allows(chunk, CODE_TYPES);
    if (previous) {
      const newDocument = !sameDocument(previous, chunk, metadata);
      // A failed page strictly between two known pages is never bridged.
      const low = Math.min(pageOf(previous), pageOf(chunk)), high = Math.max(pageOf(previous), pageOf(chunk));
      if (hasPage(previous) && hasPage(chunk) && high - low > 1
        && failed.some(page => page > low && page < high)) gaps.add(index - 1);
      if (newDocument || gaps.has(index - 1) || (options.page_boundary
        && (!hasPage(previous) || !hasPage(chunk) || pageOf(previous) !== pageOf(chunk)))) hard.add(index - 1);
      // A typed block of another kind cannot continue an open structure.
      if (newDocument) close(true);
      else if ((fence !== null && !codeOk) || (depth > 0 && !tableOk)) close(false);
    }
    if (fence !== null) link(index - 1, code);
    else if (depth > 0) link(index - 1, tables);
    const lines = chunk.text.replace(/\r\n?/g, "\n").split("\n");
    // The separator supplies a newline at a chunk boundary, not an empty row.
    if (lines.length > 1 && lines.at(-1) === "") lines.pop();
    for (const [lineIndex, line] of lines.entries()) {
      const marker = codeOk && fenceLine.exec(line);
      if (fence) {
        if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length
          && /^[ \t]*$/.test(marker[2])) fence = null;
        last = null; table = false;
        continue;
      }
      if (depth === 0 && marker && (marker[1][0] !== "`" || !marker[2].includes("`"))) {
        fence = marker[1]; found.code = true; last = null; table = false;
        continue;
      }
      // Markdown fences inside HTML are literal. Track HTML even when its
      // protection is off, so enabling code protection cannot absorb it.
      let html = depth > 0;
      if (tableOk) for (const tag of line.matchAll(/<\/?table(?=[\t\f />])[^<>]*>/gi)) {
        html = found.tables = true;
        if (/^<\//.test(tag[0])) depth = Math.max(0, depth - 1);
        else if (!/\/[ \t]*>$/.test(tag[0])) depth++;
      }
      if (html || !tableOk) { table = false; last = null; continue; }
      const cells = tableCells(line);
      // A header and rule opening the chunk start a new table, not a continuation.
      if (lineIndex === 0 && header(cells, lines[1])) table = false;
      if (table && cells) {
        if (lineIndex === 0 && last?.index === index - 1) link(index - 1, tables);
      } else table = false;
      if (isRule(cells) && last?.cells && !isRule(last.cells) && cells.length === last.cells.length) {
        table = found.tables = true;
        if (last.index === index - 1) link(index - 1, tables);
      }
      last = { cells, index };
    }
  }
  close(true);
  // found feeds the selection badges only; the server scanner does not need it.
  return { continuations, joins, hard, gaps, incomplete, hidden, conflicts: crossed.size, found };
}

// Whether the saved text contains tables or fenced code, for the selection
// badges: the same scanner, so badges agree with what protection recognizes.
export const ragStructurePresence = (chunks, metadata) =>
  ragStructureBoundaries(chunks, metadata, {}).found;
