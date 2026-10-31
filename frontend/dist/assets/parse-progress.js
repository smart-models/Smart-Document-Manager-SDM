// Pure formatting for the live /parse/progress snapshot (PARSE section 03).
export const POLL_MS = 1000;

/** [label, value] pairs for metricTiles; missing values are null (rendered "—"). */
export function progressTiles(snapshot, elapsedSeconds) {
  const s = snapshot ?? {};
  const pages = s.pages_total != null && s.pages_done != null ? `${s.pages_done}/${s.pages_total}` : null;
  const block = s.blocks_total != null && s.block_pages != null && s.block > 0
    ? `${s.block}/${s.blocks_total} (${s.block_pages} pages)` : null;
  return [
    ["state", s.state ?? null],
    ["pages", pages],
    ["block", block],
    ["errors", s.pages_failed ?? null],
    ["unprocessed", s.pages_unprocessed ?? null],
    ["dpi", s.dpi ?? null],
    ["elapsed", `${elapsedSeconds}s`],
    ["ETA", s.eta_s != null ? `${Math.ceil(s.eta_s / 60)} min` : null],
  ];
}

const FINAL = new Set(["done", "partial", "cancelled", "failed"]);

/** Pages already processed (done, failed or skipped); the caller announces on change. */
export const processedPages = (s) => (s ? (s.pages_done | 0) + (s.pages_failed | 0) + (s.pages_unprocessed | 0) : 0);

/** Screen-reader text for COMPLETED work only: `block` is the running block while active. */
export function progressStatus(snapshot) {
  if (!snapshot || snapshot.blocks_total == null || processedPages(snapshot) <= 0) return null;
  const completed = FINAL.has(snapshot.state) ? snapshot.block : snapshot.block - 1;
  if (!(completed > 0)) return null;
  return `Parsing: block ${completed} of ${snapshot.blocks_total} done, ${processedPages(snapshot)} of ${snapshot.pages_total} pages.`;
}

export const MAX_POLL_FAILURES = 3;

/** Fold one poll reply into the displayed snapshot: transient failures (timeout,
    network, 5xx) keep the last good one until MAX_POLL_FAILURES in a row; a 404
    (entry gone) clears it at once; any success resets the counter. */
export function nextSnapshot(previous, reply, failures) {
  if (reply?.ok) return { snapshot: reply.data, failures: 0 };
  if (reply?.status === 404) return { snapshot: null, failures: 0 };
  const count = failures + 1;
  return { snapshot: count >= MAX_POLL_FAILURES ? null : previous, failures: count };
}
