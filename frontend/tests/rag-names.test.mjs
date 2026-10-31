import test from "node:test";
import assert from "node:assert/strict";
import { isRagSource, isRagRefined, isRagListed } from "../dist/assets/rag-names.js";

test("source names match json_rag.json at any depth", () => {
  for (const name of ["json_rag.json", "a/json_rag.json", "doc.json_rag.json"]) {
    assert.equal(isRagSource(name), true, name);
    assert.equal(isRagRefined(name), false, name);
    assert.equal(isRagListed(name), true, name);
  }
});

test("refined names are listed but never a source", () => {
  for (const name of [
    "json_rag_sFkpOeyg_001.json",
    "json_rag_Abcd123__023.json",
    "json_rag_refined__doc__256-512t__X__Y.json",
    "doc.json_rag_refined__x.json",
    "a/b/json_rag_refined__c.json",
  ]) {
    assert.equal(isRagRefined(name), true, name);
    assert.equal(isRagSource(name), false, name);
    assert.equal(isRagListed(name), true, name);
  }
});

test("other JSON files are not listed", () => {
  for (const name of ["json_ocr.json", "xjson_rag.json", "json_rag.json.bak",
    "json_chunks.json", "json_rag_sFkpOeyg.json", "json_rag__001.json",
    "json_rag_sFkpOeyg_1.json", "doc.json_rag_sFkpOeyg_023.json",
    "a/b/json_rag_sFkpOeyg_023.json"]) {
    assert.equal(isRagListed(name), false, name);
  }
});
