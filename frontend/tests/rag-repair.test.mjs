import test from "node:test";
import assert from "node:assert/strict";
import { gestureFor, applySplit, glueGroups, undoGesture, resetGestures, planSizeBytes, overCap, encodeGroups, capState, sameChunk, memberCounts, noteCode, noteText } from "../dist/assets/rag-repair.js";

test("structured split notes display readable text and retain save codes", () => {
  const note = { code: "below_minimum_fragment", severity: "info", targets: [],
    parameters: [{ name: "detail", value: "group below the minimum" }, { name: "tokens", value: 200 }] };
  assert.equal(noteCode(note), "below_minimum_fragment");
  assert.equal(noteText(note), "Fragment below minimum (200 tokens)");
  assert.equal(noteText("table_split"), "Table split");
  assert.equal(noteCode("above_maximum_fragment"), "above_maximum_fragment");
  assert.equal(noteText({ code: "new_note", parameters: [{ name: "detail", value: "Readable detail" }] }), "Readable detail");
});

const GROUPS = [[0], [1], [2]];
const VALUE = { metadata: { document: "default" }, chunks: [
  { id: 1, text: "alfa beta gamma", token_count: 3, page: 1, document: "default" },
  { id: 2, text: "delta epsilon", token_count: 4, page: 2, document: "default" },
  { id: 3, text: "zeta eta", token_count: 2, page: 2, document: "default" }] };
const FRAGMENTS = [{ entries: [[0, 0, 10, "repeat"], [0, 10, 40]], token_count: 15, status: "within", notes: ["table_split"] },
                   { entries: [[0, 0, 10, "repeat"], [0, 40, 80]], token_count: 15, status: "within", notes: ["table_split"] }];
const OPTIONS = { min_tokens: 10, max_tokens: 20, page_boundary: false };

test("a multisource split keeps measured counts for each save member", () => {
  const fragments = [{ entries: [[0, 0, 4], [1, 0, 5]], token_count: 16,
    member_token_counts: [7, 9], status: "within", notes: [] }];
  const model = applySplit(VALUE, [[0, 1]], 0, fragments);
  assert.deepEqual(memberCounts(VALUE, model.groups[0], model.counts), [7, 9]);
});

test("gestures follow the row status", () => {
  assert.deepEqual(gestureFor("above", 1, 3), { scissors: true, gluePrevious: false, glueNext: false, reason: "" });
  assert.equal(gestureFor("below", 0, 3).gluePrevious, false);
  assert.match(gestureFor("below", 0, 3).reason, /first row/);
  assert.equal(gestureFor("within", 1, 3).scissors, false);
  assert.equal(gestureFor("above", 1, 3, { tokenizerUnavailable: true }).scissors, false);
  assert.match(gestureFor("above", 1, 3, { tokenizerUnavailable: true }).reason, /tokenizer/);
});

test("a split replaces one row, records its slice and its measured counts", () => {
  const result = applySplit(VALUE, GROUPS, 0, FRAGMENTS, []);
  assert.deepEqual(result.groups[0], FRAGMENTS[0].entries);
  assert.equal(result.groups.length, 4);
  assert.deepEqual(result.gestures[0].slice, [[0]]);
  assert.equal(result.gestures[0].id, "1");
  assert.equal(result.counts.get("0:10-40"), 15);
});

test("glue merges in source order and notes the overflow from real counts", () => {
  const result = glueGroups(VALUE, GROUPS, 0, "next", { min_tokens: 3, max_tokens: 5 }, new Map());
  assert.deepEqual(result.groups[0], [0, 1]);
  assert.equal(result.groups.length, 2);
  assert.deepEqual(result.notes, ["above_maximum_fragment"]);   // 3 + 4 = 7 > 5
});

test("glue across pages is noted when the page boundary is on", () => {
  // Groups 0 and 1 sit on pages 1 and 2, so the merged group spans a page.
  const result = glueGroups(VALUE, GROUPS, 0, "next",
                            { min_tokens: 1, max_tokens: 100, page_boundary: true }, new Map());
  assert.deepEqual(result.groups[0], [0, 1]);
  assert.deepEqual(result.notes, ["page_boundary_forced"]);
});

test("glue within one page stays silent when the page boundary is on", () => {
  // Groups 1 and 2 are both on page 2, so the merged group spans one page.
  const result = glueGroups(VALUE, GROUPS, 1, "next",
                            { min_tokens: 1, max_tokens: 100, page_boundary: true }, new Map());
  assert.deepEqual(result.groups[1], [1, 2]);
  assert.deepEqual(result.notes, []);
});

test("undo of a consumed gesture is blocked with the dependent id", () => {
  const split = applySplit(VALUE, GROUPS, 0, FRAGMENTS, []);
  // The glue takes a fragment of chunk 0 and the whole chunk 1: different
  // chunks, so R24b allows it, and it consumes the split's second fragment.
  const glued = glueGroups(VALUE, split.groups, 1, "next", OPTIONS, split.counts, split.gestures);
  const undo = undoGesture({ groups: glued.groups, gestures: glued.gestures, counts: glued.counts }, 1);
  assert.equal(undo.blockedBy, "2");
});

test("a glue then a cut get distinct ids and each undo targets its own gesture", () => {
  const glued = glueGroups(VALUE, GROUPS, 0, "next", OPTIONS, new Map(), []);
  const cut = applySplit(VALUE, glued.groups, 0, FRAGMENTS, glued.gestures);
  assert.equal(glued.gestures[0].id, "1");
  assert.equal(cut.gestures[0].id, "2");
  assert.notEqual(cut.gestures[0].id, glued.gestures[0].id);
  const state = { groups: cut.groups, gestures: [...glued.gestures, ...cut.gestures], counts: cut.counts };
  // The cut undoes itself exactly, and the consumed glue is blocked naming the cut.
  const undone = undoGesture(state, 2);
  assert.deepEqual(undone.groups, glued.groups);
  assert.deepEqual(undone.gestures, glued.gestures);
  assert.equal(undoGesture(state, 1).blockedBy, "2");
});

test("glue between two groups of the same chunk is disabled", () => {
  const fragments = [[[0, 0, 5]], [[0, 5, 10]]];
  assert.equal(sameChunk(fragments[0], fragments[1]), true);
  assert.equal(sameChunk([[0]], [[1]]), false);
  const result = glueGroups(VALUE, fragments, 0, "next", OPTIONS, new Map(), []);
  assert.deepEqual(result.groups, fragments);
  assert.deepEqual(result.gestures, []);
});

test("member counts follow the server's per-chunk bundle rule", () => {
  // One chunk split in two is a single summed member, exactly what the browser
  // displays and what the server compares with its own bundle measurement.
  const counts = new Map([["0:10-40", 15], ["0:40-80", 12]]);
  assert.deepEqual(memberCounts(VALUE, [[0, 10, 40], [0, 40, 80]], counts), [27]);
  assert.deepEqual(memberCounts(VALUE, [[0, 10, 40], 1], counts), [15, 4]);
});

test("undo restores exactly the slice of its own gesture", () => {
  const split = applySplit(VALUE, GROUPS, 1, FRAGMENTS, []);
  const undone = undoGesture({ groups: split.groups, gestures: split.gestures, counts: split.counts }, 1);
  assert.deepEqual(undone.groups, GROUPS);
  assert.deepEqual(undone.gestures, []);
  assert.equal(undone.counts.size, 0);
});

test("undoing a merge keeps the counts of the fragments it contained", () => {
  // Regression: the undo deleted the cut's counts, so merging the same
  // fragment again showed "Unknown size".
  const split = applySplit(VALUE, GROUPS, 0, FRAGMENTS, []);
  const glued = glueGroups(VALUE, split.groups, 1, "next", OPTIONS, split.counts, split.gestures);
  const undone = undoGesture({ groups: glued.groups, gestures: glued.gestures, counts: glued.counts }, 2);
  assert.deepEqual(undone.counts, split.counts);
  const again = glueGroups(VALUE, undone.groups, 1, "next", OPTIONS, undone.counts, undone.gestures);
  assert.deepEqual(memberCounts(VALUE, again.groups[1], again.counts), [15, 4]);
});

test("undoing a re-cut restores the counts the earlier cut measured", () => {
  const first = applySplit(VALUE, GROUPS, 0, [{ entries: [[0, 0, 7], [0, 7, 15]], token_count: 3,
    member_token_counts: [3], status: "within", notes: [] }], []);
  const second = applySplit(VALUE, first.groups, 0, [
    { entries: [[0, 0, 7]], token_count: 1, status: "below", notes: [] },
    { entries: [[0, 7, 15]], token_count: 2, status: "below", notes: [] }], first.gestures);
  const counts = new Map([...first.counts, ...second.counts]);
  const undone = undoGesture({ groups: second.groups, gestures: [...first.gestures, ...second.gestures], counts }, 2);
  assert.deepEqual(undone.counts, first.counts);
});

test("resetting the plan discards every gesture and every count", () => {
  const split = applySplit(VALUE, GROUPS, 1, FRAGMENTS, []);
  const reset = resetGestures({ groups: split.groups, gestures: split.gestures, counts: split.counts });
  assert.deepEqual(reset.gestures, []);
  assert.equal(reset.counts.size, 0);
  assert.equal(reset.groups.length, GROUPS.length);
});

test("the cap is re-measured from the gesture-modified row list", () => {
  const small = [[0], [1, 2]];
  assert.deepEqual(capState(small), { tooLarge: false, groups: 2, bytes: planSizeBytes(small) });
  const many = Array.from({ length: 100_001 }, (_, index) => [index]);
  assert.equal(capState(many).tooLarge, true);          // one group over 100,000
  assert.equal(capState(many).groups, 100_001);
  const fragments = Array.from({ length: 100_000 }, (_, index) =>
    [[0, index, index + 1, "repeat"], [0, index, index + 1]]);
  const capped = capState(fragments);
  assert.equal(capped.groups, 100_000);                  // group count is at the cap
  assert.equal(capped.bytes, planSizeBytes(fragments));  // measured on the encoded plan
  assert.ok(capped.bytes > 2 * 1024 * 1024);
  assert.equal(capped.tooLarge, true);
});

test("integer plans stay under the cap and fragment plans are measured against it", () => {
  const integers = Array.from({ length: 100_000 }, (_, index) => [index]);
  assert.ok(planSizeBytes(integers) < 2 * 1024 * 1024);
  assert.equal(overCap(integers), false);
  const fragments = Array.from({ length: 100_000 }, (_, index) =>
    [[0, index, index + 1, "repeat"], [0, index, index + 1]]);
  assert.equal(overCap(fragments), true);
  assert.equal(encodeGroups(integers).every(Array.isArray), true);
});
