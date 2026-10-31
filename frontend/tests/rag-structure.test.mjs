import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { ragStructureBoundaries, ragStructurePresence } from "../dist/assets/rag-structure.js";
import { planRagAggregation, ragGroupPreview } from "../dist/assets/rag-preflight.js";

const fixtures = JSON.parse(readFileSync(new URL("../../tests/fixtures/rag-structures.json", import.meta.url)));
export function fixtureSource(fixture) {
  const metadata = { document: "default",
    ...("failed_pages" in fixture ? { failed_pages: fixture.failed_pages } : {}) };
  return { metadata, chunks: fixture.texts.map((text, index) => ({
    id: index + 1, text, token_count: fixture.tokens ? fixture.tokens[index] : 100,
    page: fixture.pages ? fixture.pages[index] : 1,
    document: fixture.documents ? fixture.documents[index] : "default",
    // A null type is an absent field: untyped blocks keep the text-only scan.
    ...(fixture.types?.[index] == null ? {} : { type: fixture.types[index] }),
    custom: { original: index },
  })) };
}
export const fixtureOptions = fixture => ({ min_tokens: 150, max_tokens: 250,
  page_boundary: false, keep_tables_together: true, keep_code_blocks_together: true, ...fixture.options });

for (const fixture of fixtures) test(`structure: ${fixture.name}`, () => {
  const source = fixtureSource(fixture), options = fixtureOptions(fixture);
  const boundaries = ragStructureBoundaries(source.chunks, source.metadata, options);
  assert.deepEqual([...boundaries.joins], fixture.joins);
  assert.deepEqual([...boundaries.continuations], fixture.continuations ?? fixture.joins);
  assert.deepEqual([...boundaries.hard], fixture.hard ?? []);
  assert.deepEqual([...boundaries.gaps], fixture.gaps ?? []);
  assert.equal(boundaries.incomplete, fixture.incomplete ?? 0);
  assert.equal(boundaries.hidden, fixture.hidden ?? 0);
  assert.equal(boundaries.conflicts, fixture.conflicts ?? 0);
  const plan = planRagAggregation(source, options);
  assert.equal("matches" in plan, false);
  const warnings = plan.warnings.join(" ");
  assert.equal(/page boundary takes priority/.test(warnings), Boolean(fixture.conflicts));
  assert.equal(/protection is off hides later structures/.test(warnings), Boolean(fixture.hidden));
  assert.equal(/Failed pages are hard boundaries/.test(warnings), Boolean(fixture.gaps));
  if (fixture.first_group_text) assert.equal(ragGroupPreview(source, plan.groups[0]).text, fixture.first_group_text);
  assert.deepEqual(plan.groups.flatMap(g => g.source_indices), source.chunks.map((_, index) => index));
  for (const join of fixture.joins) assert.ok(plan.groups.some(g => g.source_indices.includes(join)
    && g.source_indices.includes(join + 1)));
  for (const join of fixture.hard ?? []) assert.ok(plan.groups.every(g => !(g.source_indices.includes(join)
    && g.source_indices.includes(join + 1))));
  for (const group of plan.groups) {
    const preview = ragGroupPreview(source, group);
    assert.deepEqual(preview.source_payloads.map(p => p.custom.original), group.source_indices);
    if (group.source_indices.some(index => source.chunks[index].token_count === null)) assert.equal(group.status, "unknown");
  }
});

test("presence: tables and code blocks in the saved text", () => {
  const presence = texts => ragStructurePresence(
    texts.map((text, index) => ({ id: index + 1, text })), { document: "default" });
  assert.deepEqual(presence(["First", "Second"]), { tables: false, code: false });
  assert.deepEqual(presence(["A | B", "--- | ---", "1 | 2"]), { tables: true, code: false });
  assert.deepEqual(presence(["hello | there", "foo | bar"]), { tables: false, code: false });
  assert.deepEqual(presence(["A \\| B", "--- | ---"]), { tables: false, code: false });
  assert.deepEqual(presence(["<table>", "<tr><td>A</td></tr>", "</table>"]), { tables: true, code: false });
  assert.deepEqual(presence(["```py", "a = 1", "```"]), { tables: false, code: true });
  assert.deepEqual(presence(["~~~lang", "~~~", "after"]), { tables: false, code: true });
  assert.deepEqual(presence(["```html", "<table>", "```"]), { tables: false, code: true });
  assert.deepEqual(presence(["<table>", "<tr><td>code", "```", "</td></tr></table>"]), { tables: true, code: false });
  // Badges follow the same type gate as protection; untyped blocks keep the text scan.
  const typed = (texts, types) => ragStructurePresence(
    texts.map((text, index) => ({ id: index + 1, text, type: types[index] })), { document: "default" });
  assert.deepEqual(typed(["Use the `<table>` element.", "~~~~~~"], ["text", "text"]), { tables: false, code: false });
  assert.deepEqual(typed(["A | B\n--- | ---", "~~~\nx\n~~~"], ["table", "algorithm"]), { tables: true, code: true });
  assert.deepEqual(typed(["A | B\n--- | ---"], [["text", "table"]]), { tables: true, code: false });
  assert.deepEqual(presence(["Use the `<table>` element.", "~~~~~~"]), { tables: true, code: false });
});
