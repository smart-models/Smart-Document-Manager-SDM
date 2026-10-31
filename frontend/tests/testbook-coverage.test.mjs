import { readFileSync, readdirSync, existsSync, globSync } from "node:fs";
import { createHash } from "node:crypto";
import { test } from "node:test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const bookPath = path.join(root, "tests", "testbook", "test-book-e2e.md");
const contractPath = path.join(root, "tests", "testbook", "coverage-contract.json");
const book = readFileSync(bookPath, "utf8");
const contract = JSON.parse(readFileSync(contractPath, "utf8"));

const CASE_ID = /SDM-[A-Z]{3}-\d{3}/g;
const caseIds = [...book.matchAll(CASE_ID)].map(match => match[0]);
const uniqueIds = [...new Set(caseIds)];
const allOwned = Object.values(contract.owners).flat();
const excludedIds = new Set(contract.exclusions.map(entry => entry.id));

const executableColumn = row =>
  row.split("|").map(cell => cell.trim()).filter(Boolean)[2];

test("every book case id is declared exactly once in coverage-contract owners", () => {
  const seen = new Map();
  for (const id of allOwned) seen.set(id, (seen.get(id) || 0) + 1);
  const duplicates = [...seen].filter(([, count]) => count > 1).map(([id]) => id);
  if (duplicates.length) throw new Error(`Duplicate owners: ${duplicates.join(", ")}`);
  const missing = uniqueIds.filter(id => !seen.has(id) && !excludedIds.has(id));
  if (missing.length) throw new Error(`Unassigned cases: ${missing.join(", ")}`);
});

test("every owner entry and exclusion is a real book case id", () => {
  const bookIds = new Set(uniqueIds);
  for (const id of allOwned)
    if (!bookIds.has(id)) throw new Error(`Owner ${id} has no book case`);
  for (const entry of contract.exclusions)
    if (!bookIds.has(entry.id)) throw new Error(`Exclusion ${entry.id} has no book case`);
});

test("exclusions carry a written reason of at least 40 characters", () => {
  for (const entry of contract.exclusions)
    if (typeof entry.reason !== "string" || entry.reason.trim().length < 40)
      throw new Error(`Exclusion ${entry.id} needs a >=40 char reason`);
});

test("excluded cases are not simultaneously owned as executable", () => {
  const owned = new Set(allOwned);
  for (const id of excludedIds)
    if (owned.has(id)) throw new Error(`Case ${id} is both owned and excluded`);
});

test("ratchet cannot exceed the declared maximum unassigned count", () => {
  const unassigned = uniqueIds.filter(id => !allOwned.includes(id) && !excludedIds.has(id));
  if (unassigned.length > contract.ratchet.maxUnassigned)
    throw new Error(
      `${unassigned.length} unassigned cases exceed ratchet.maxUnassigned=${contract.ratchet.maxUnassigned}: ${unassigned.join(", ")}`,
    );
});

test("every mapped executable file exists", () => {
  const rows = book.split("\n").filter(line => line.includes("|"));
  const candidates = new Set();
  for (const row of rows) {
    const cells = row.split("|").map(cell => cell.trim());
    if (!CASE_ID.test(cells[1])) continue;
    CASE_ID.lastIndex = 0;
    const exe = executableColumn(row);
    if (!exe || exe === "—" || exe.includes("coverage-contract")) continue;
    for (const candidate of exe.split(/[,+]/).map(part => part.trim())) {
      if (!candidate || candidate === "—") continue;
      candidates.add(candidate.replace(/`/g, ""));
    }
  }
  const repoRoot = path.join(root, "..");
  const missing = [...candidates].filter(name => {
    if (name.includes("*"))
      return globSync(name, { cwd: repoRoot }).length === 0
        && globSync(name, { cwd: root }).length === 0;
    if (name.endsWith(".py"))
      return !existsSync(path.join(repoRoot, name.startsWith("tests/") ? name : `tests/${name}`));
    if (name.endsWith(".mjs") || name.endsWith(".js"))
      return !existsSync(path.join(root, name))
        && !existsSync(path.join(root, "tests", name))
        && !existsSync(path.join(root, "guide-tests", name));
    return !existsSync(path.join(repoRoot, name)) && !existsSync(path.join(root, name));
  });
  if (missing.length) throw new Error(`Mapped executable files missing: ${missing.join(", ")}`);
});

test("every corpus document matches its manifest hash and is used by a book case", () => {
  const docsDir = path.join(root, "tests", "testbook", "documents");
  const corpus = JSON.parse(readFileSync(path.join(docsDir, "manifest.json"), "utf8"));
  const tooling = new Set(["manifest.json", "generate.py", corpus.source.license_file]);
  const listed = new Set(corpus.files.map(file => file.path));
  const unlisted = readdirSync(docsDir).filter(name => !tooling.has(name) && !listed.has(name));
  if (unlisted.length) throw new Error(`Corpus files missing from manifest.json: ${unlisted.join(", ")}`);
  for (const file of corpus.files) {
    const hash = createHash("sha256").update(readFileSync(path.join(docsDir, file.path))).digest("hex");
    if (hash !== file.sha256) throw new Error(`${file.path} does not match its manifest sha256`);
    if (!book.includes(`documents/${file.path}`)) throw new Error(`${file.path} is not used by any book case`);
  }
});
