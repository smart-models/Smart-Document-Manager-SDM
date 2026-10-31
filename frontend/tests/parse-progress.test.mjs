import test from "node:test";
import assert from "node:assert/strict";
import { POLL_MS, progressTiles, progressStatus, nextSnapshot } from "../dist/assets/parse-progress.js";

const base = { state: "ocr", pages_total: 91, pages_done: 8, pages_failed: 1, pages_unprocessed: 0,
  block: 3, blocks_total: 23, block_pages: 4, dpi: 250, elapsed_s: 70, eta_s: 700 };

test("progressTiles renders dashes before planning", () => {
  const tiles = Object.fromEntries(progressTiles({ ...base, state: "queued", pages_total: null, pages_done: 0,
    pages_failed: 0, block: 0, blocks_total: null, block_pages: null, elapsed_s: 0.4, eta_s: null }, 3));
  assert.deepEqual([tiles.pages, tiles.block, tiles.ETA], [null, null, null]);
  assert.equal(tiles.state, "queued"); assert.equal(tiles.elapsed, "3s");
});
test("progressTiles formats counts, block and ETA", () => {
  const tiles = Object.fromEntries(progressTiles(base, 71));
  assert.deepEqual([tiles.pages, tiles.block, tiles.errors, tiles.ETA], ["8/91", "3/23 (4 pages)", 1, "12 min"]);
  assert.deepEqual(progressTiles(base, 71).map(([l]) => l),
    ["state", "pages", "block", "errors", "unprocessed", "dpi", "elapsed", "ETA"]);
});
test("progressTiles shows ETA in whole minutes, rounded up", () => {
  const eta = (eta_s) => Object.fromEntries(progressTiles({ ...base, eta_s }, 1)).ETA;
  assert.deepEqual([eta(30), eta(0), eta(60), eta(61), eta(null)], ["1 min", "0 min", "1 min", "2 min", null]);
});
test("progressTiles with a failed poll keeps only elapsed", () => {
  const tiles = progressTiles(null, 5);
  assert.ok(tiles.every(([label, value]) => label === "elapsed" ? value === "5s" : value === null));
});
test("progressStatus announces only completed work", () => {
  assert.equal(progressStatus(null), null);
  // Block 1 just started: nothing completed yet.
  assert.equal(progressStatus({ ...base, block: 1, pages_done: 0, pages_failed: 0 }), null);
  // Block 3 is running: two blocks are done.
  assert.equal(progressStatus(base), "Parsing: block 2 of 23 done, 9 of 91 pages.");
  // Final state counts the last block as completed.
  assert.equal(progressStatus({ ...base, state: "done", block: 23 }), "Parsing: block 23 of 23 done, 9 of 91 pages.");
  assert.equal(POLL_MS, 1000);
});

test("nextSnapshot keeps the last good snapshot through transient failures", () => {
  const ok = { ok: true, status: 200, data: base };
  const fail = { ok: false, status: 0 };
  let st = nextSnapshot(null, ok, 0);
  assert.deepEqual(st, { snapshot: base, failures: 0 });
  st = nextSnapshot(st.snapshot, fail, st.failures);
  st = nextSnapshot(st.snapshot, fail, st.failures);
  assert.deepEqual(st, { snapshot: base, failures: 2 });
  assert.equal(nextSnapshot(st.snapshot, fail, st.failures).snapshot, null);
  assert.deepEqual(nextSnapshot(base, ok, 2), { snapshot: base, failures: 0 });
  assert.equal(nextSnapshot(base, undefined, 0).failures, 1);
});
test("nextSnapshot drops the snapshot at once on 404", () => {
  assert.deepEqual(nextSnapshot(base, { ok: false, status: 404 }, 0), { snapshot: null, failures: 0 });
});
