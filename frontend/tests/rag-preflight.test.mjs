import assert from "node:assert/strict";
import test from "node:test";
import { planRagAggregation, ragGroupPreview, ragAggregationPreview,
  entryKey, groupText, groupTokenCount, tokenRangeStatus } from "../dist/assets/rag-preflight.js";
import { ragStructureBoundaries, ragTextSeparator } from "../dist/assets/rag-structure.js";

const options = { min_tokens: 250, max_tokens: 400, page_boundary: false };

test("token status keeps inclusive range boundaries and unknown counts", () => {
  for (const [tokens, status] of [
    [null, "unknown"], [249, "below"], [250, "within"],
    [400, "within"], [401, "above"],
  ]) assert.equal(tokenRangeStatus(tokens, options), status);
});
const raw = counts => ({ chunks: counts.map((token_count, index) => ({
  id: index + 1, text: `Block ${index + 1}`, token_count, document: "report.pdf", page: 1,
})) });

test("View renders repeated table headers and body ranges after a cut", () => {
  const opening = "<table>\n", header = "<tr><th>Name</th></tr>\n";
  const first = "<tr><td>first</td></tr>\n";
  const second = "<tr><td>second</td></tr>\n", closing = "</table>";
  const text = opening + header + first + second + closing;
  const value = { chunks: [{ id: 1, text, page: 2, document: "report.pdf" }] };
  const bodyStart = opening.length + header.length + first.length;
  const entries = [[0, 0, opening.length, "repeat"],
    [0, opening.length, opening.length + header.length, "repeat"],
    [0, bodyStart, text.length]];
  const preview = ragGroupPreview(value, { source_indices: entries });
  assert.equal(preview.text, opening + header + second + closing);
  assert.ok(preview.source_payloads.every(payload => payload.id === 1 && payload.page === 2));
});

test("server code-point ranges preserve emoji in previews", () => {
  const value = { chunks: [{ id: 1, text: "A😀éZ", page: 1 }] };
  assert.equal(groupText(value, [[0, 1, 4]]), "😀é");
  assert.equal(ragGroupPreview(value, { source_indices: [[0, 4, 5]] }).text, "Z");
  value.chunks[0].text = "😀😀ab";
  assert.equal(groupText(value, [[0, 2, 4]]), "ab");
});

test("preflight rebalances a short tail without splitting, losing or reordering raw blocks", () => {
  const value = raw([280, 120, 100, 100]);
  const before = JSON.stringify(value);
  const plan = planRagAggregation(value, options);
  assert.deepEqual(plan.groups.map(group => group.source_indices), [[0], [1, 2, 3]]);
  assert.deepEqual(plan.groups.map(group => group.token_count), [280, 320]);
  assert.deepEqual(plan.warnings, []);
  assert.equal(JSON.stringify(value), before);
});

test("equally many short groups favor the smallest total deficit", () => {
  const plan = planRagAggregation(raw([400, 50, 400, 150, 150, 200]), {
    min_tokens: 250, max_tokens: 450,
  });
  assert.deepEqual(plan.groups.map(group => group.token_count), [400, 450, 300, 200]);
});

test("the optional page boundary changes the best-effort plan", () => {
  const value = raw([200, 200, 200, 100]);
  value.chunks[3].page = 2;
  const document = planRagAggregation(value, options);
  const page = planRagAggregation(value, { ...options, page_boundary: true });
  assert.deepEqual(document.groups.map(group => group.token_count), [400, 300]);
  assert.deepEqual(page.groups.map(group => group.token_count), [200, 400, 100]);
  assert.equal(page.groups.every(group => group.pages.length === 1), true);
  assert.equal(page.counts.below, 2);
});

test("oversized raw blocks are kept whole and reported instead of rejected", () => {
  const value = raw([100, 900, 150]);
  const plan = planRagAggregation(value, options);
  assert.deepEqual(plan.groups.map(group => group.token_count), [100, 900, 150]);
  assert.equal(plan.groups[1].status, "above");
  assert.match(plan.groups[1].reason, /exceeds the target maximum/);
});

test("missing and invalid counts are unknown, never inferred as zero or certified within range", () => {
  const value = raw([300, undefined, null, -1, true, 2.5, 300]);
  const plan = planRagAggregation(value, options);
  assert.equal(plan.counts.unknown, 5);
  assert.equal(plan.counts.within, 2);
  assert.match(plan.warnings.join(" "), /no valid token count/);
  const pageBoundary = planRagAggregation(value, { ...options, page_boundary: true });
  assert.equal(pageBoundary.counts.unknown, 5);
  assert.ok(pageBoundary.groups.filter(group => group.status === "unknown")
    .every(group => group.source_indices.length === 1));
});

test("missing pages cannot be certified for page aggregation; documents remain separate", () => {
  const value = raw([300, 300, 300]);
  delete value.chunks[1].page;
  value.chunks[2].document = "other.pdf";
  const plan = planRagAggregation(value, { ...options, page_boundary: true });
  assert.equal(plan.groups.length, 3);
  assert.equal(plan.groups[1].missing_pages, 1);
  assert.match(plan.warnings.join(" "), /page number/);
  const mixed = raw([120, 180]);
  mixed.chunks[1].document = "other.pdf";
  assert.equal(planRagAggregation(mixed, options).groups.length, 2);
});

test("only the last group of a run names the boundary that ended it", () => {
  const value = raw([100, 240, 100, 100, 240, 100]);
  value.chunks.slice(3).forEach(chunk => { chunk.page = 2; });
  const reasons = options => planRagAggregation(value, { min_tokens: 150, max_tokens: 250, ...options })
    .groups.map(group => group.reason);
  const below = "Below target after grouping whole blocks";
  const within = "Within the estimated token range.";
  assert.deepEqual(reasons({}), [`${below}.`, within, within, within, `${below} (end of document).`]);
  assert.deepEqual(reasons({ page_boundary: true }), [`${below}.`, within,
    `${below} (page change).`, `${below}.`, within, `${below} (end of document).`]);
  value.chunks.slice(3).forEach(chunk => { chunk.document = "other.pdf"; });
  assert.equal(reasons({})[2], `${below} (document change).`);
  const gap = { ...raw([100, 100]), metadata: { failed_pages: [2] } };
  gap.chunks[1].page = 3;
  const plan = planRagAggregation(gap, options);
  assert.deepEqual(plan.groups.map(group => group.source_indices), [[0], [1]]);
  assert.match(plan.groups[0].reason, /\(failed-page gap\)/);
  assert.match(plan.warnings.join(" "), /Failed pages are hard boundaries/);
});

test("a refined single-page list is that page under the page boundary", () => {
  const value = raw([100, 100, 100]);
  value.chunks.forEach((chunk, index) => { chunk.page = [index < 2 ? 1 : 2]; });
  const plan = planRagAggregation(value, { min_tokens: 150, max_tokens: 400, page_boundary: true });
  assert.deepEqual(plan.groups.map(group => [group.source_indices, group.pages]), [[[0, 1], [1]], [[2], [2]]]);
  assert.doesNotMatch(plan.warnings.join(" "), /span several pages|without a page number/);
});

test("refined chunks spanning pages are kept separate without claiming a missing page", () => {
  const value = raw([100, 100]);
  value.chunks[0].page = [1, 2];
  value.chunks[1].page = 2;
  const plan = planRagAggregation(value, { min_tokens: 150, max_tokens: 400, page_boundary: true });
  assert.deepEqual(plan.groups.map(group => group.source_indices), [[0], [1]]);
  assert.deepEqual(plan.groups.map(group => [group.pages, group.missing_pages]), [[[1, 2], 0], [[2], 0]]);
  assert.match(plan.groups[0].reason, /spans several pages/);
  assert.doesNotMatch(plan.groups[0].reason, /page number missing/);
  const warnings = plan.warnings.join(" ");
  assert.match(warnings, /already span several pages/);
  assert.doesNotMatch(warnings, /without a page number/);
  assert.deepEqual(planRagAggregation(value, { min_tokens: 150, max_tokens: 400 }).groups
    .map(group => group.source_indices), [[0, 1]]);
});

test("group preview concatenates text and retains all source payload values and associations", () => {
  const value = raw([120, 180]);
  value.chunks[0].page_header = value.chunks[1].page_header = "Header";
  value.chunks[0].footnote = "First note";
  value.chunks[1].footnote = "Second note";
  value.chunks[1].custom = { tags: ["a", "b"] };
  const before = JSON.stringify(value);
  const plan = planRagAggregation(value, options);
  const preview = ragGroupPreview(value, plan.groups[0]);
  assert.equal(preview.text, "Block 1\n\nBlock 2");
  assert.deepEqual(preview.source_payloads.map(item => item.footnote), ["First note", "Second note"]);
  assert.deepEqual(preview.source_payloads[1].custom, { tags: ["a", "b"] });
  assert.equal(JSON.stringify(value), before);
});

test("aggregation preview merges source fields without per-source copies", () => {
  const value = { metadata: { document: "report.pdf" }, chunks: [
    { id: 1, text: "A", token_count: 100, page: 1, page_header: "H" },
    { id: 2, text: "B", token_count: 100, page: 1, page_header: "H", footnote: "N" },
    { id: 3, text: "C", token_count: 100, page: 2, page_header: "H2" },
  ] };
  const plan = planRagAggregation(value, { min_tokens: 150, max_tokens: 400, page_boundary: false });
  assert.deepEqual(plan.groups.map(group => group.source_indices), [[0, 1, 2]]);
  assert.deepEqual(ragAggregationPreview(value, plan).chunks, [{
    id: 1, text: "A\n\nB\n\nC", token_count: 300, document: "report.pdf",
    page: [1, 2], page_header: ["H", "H2"], footnote: ["N"],
  }]);
});

test("a printed folio equal to the page index is redundant and dropped when merging", () => {
  const value = { metadata: { document: "report.pdf" }, chunks: [
    { id: 1, text: "A", token_count: 100, page: 2, page_number: "2" },
    { id: 2, text: "B", token_count: 100, page: 2, page_number: "ii" },
  ] };
  const separate = planRagAggregation(value, { min_tokens: 50, max_tokens: 150, page_boundary: false });
  const chunks = ragAggregationPreview(value, separate).chunks;
  assert.equal(chunks.length, 2);
  assert.equal("page_number" in chunks[0], false);
  assert.deepEqual(chunks[1].page_number, ["ii"]);
  const together = planRagAggregation(value, { min_tokens: 150, max_tokens: 400, page_boundary: false });
  assert.deepEqual(ragAggregationPreview(value, together).chunks[0].page_number, ["ii"]);
});

test("empty input, zero counts, source warnings and invalid ranges have explicit results", () => {
  assert.deepEqual(planRagAggregation(raw([]), options).groups, []);
  assert.deepEqual(planRagAggregation(raw([0, 0]), options).counts, { within: 0, below: 1, above: 0, unknown: 0 });
  const value = { ...raw([300]), metadata: { failed_pages: [2], unattached_blocks: 1 } };
  assert.equal(planRagAggregation(value, options).warnings.length, 2);
  for (const limits of [{ min_tokens: 600 }, { min_tokens: 0 }, { max_tokens: NaN }, { min_tokens: 1.5 }]) {
    assert.throws(() => planRagAggregation(value, { ...options, ...limits }), /positive whole token counts/);
  }
});

const source = { metadata: { document: "default" }, chunks: [
  { id: 1, text: "alfa beta gamma", token_count: 3 },
  { id: 2, text: "delta epsilon", token_count: 2 }] };

test("entries slice the source text with the mirrored separator", () => {
  assert.equal(groupText(source, [[0, 0, 4], 1]), "alfa" + "\n\n" + "delta epsilon");
});

test("two entries of the same chunk join compactly, never with a blank line", () => {
  assert.equal(groupText(source, [[0, 5, 9, "repeat"], [0, 0, 4]]), "beta" + "\n" + "alfa");
});

test("a fragment count comes from the measured map, not from the source estimate", () => {
  const counts = new Map([[entryKey(source, [0, 0, 4]), 7]]);
  assert.equal(groupTokenCount(source, [[0, 0, 4], 1], counts), 9);
  assert.equal(groupTokenCount(source, [[0, 0, 4], 1]), null);
});

test("a plan without gestures keeps today's text and counts", () => {
  assert.equal(groupText(source, [0, 1]), "alfa beta gamma" + "\n\n" + "delta epsilon");
  assert.equal(groupTokenCount(source, [0, 1]), 5);
});

const continuationSource = { metadata: { document: "default" }, chunks: [
  { id: 1, text: "<table>\n<tr><td>a</td></tr>", token_count: 10, page: 1, document: "default" },
  { id: 2, text: "<tr><td>b</td></tr>\n</table>\n", token_count: 10, page: 1, document: "default" }] };

test("a bare gesture spanning a continuation boundary joins compactly like the server", () => {
  const structureOptions = { min_tokens: 10, max_tokens: 1000, page_boundary: false,
    keep_tables_together: true, keep_code_blocks_together: true };
  const { continuations } = ragStructureBoundaries(continuationSource.chunks, continuationSource.metadata,
    structureOptions);
  assert.equal(continuations.has(0), true);
  const [first, second] = continuationSource.chunks.map(chunk => chunk.text);
  const separator = ragTextSeparator(first, second, true);
  // The separator really is the compact one, never the plain blank line.
  assert.notEqual(separator, "\n\n");
  assert.equal(groupText(continuationSource, [0, 1], { continuations }), first + separator + second);
});

test("both page-boundary settings preserve every block once in input order", () => {
  let seed = 42;
  const next = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed; };
  for (let sample = 0; sample < 50; sample++) {
    const value = raw(Array.from({ length: 50 }, () => next() % 750));
    value.chunks.forEach((chunk, index) => { chunk.page = Math.floor(index / 7) + 1; });
    for (const page_boundary of [false, true]) {
      const plan = planRagAggregation(value, { ...options, page_boundary });
      assert.deepEqual(plan.groups.flatMap(group => group.source_indices), value.chunks.map((_, index) => index));
      assert.equal(plan.groups.reduce((sum, group) => sum + group.token_count, 0), value.chunks.reduce((sum, chunk) => sum + chunk.token_count, 0));
      if (page_boundary) assert.ok(plan.groups.every(group => group.pages.length === 1));
    }
  }
});
