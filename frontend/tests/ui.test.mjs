// Unit tests for the console's shared UI helpers (frontend/dist/assets/ui.js).
// ui.js has no top-level DOM access, so its pure helpers import cleanly in Node.

import assert from "node:assert/strict";
import test from "node:test";

import { esc } from "../dist/assets/ui.js";

test("esc encodes every HTML-significant character, each occurrence", () => {
  // Both quote characters are encoded so an escaped value is safe inside a
  // double- or single-quoted attribute, not only in element text.
  assert.equal(
    esc(`<a href="x" title='y'>&</a> <b>`),
    "&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;&lt;/a&gt; &lt;b&gt;",
  );
});

test("esc encodes the ampersand first so entities are not double-decoded", () => {
  assert.equal(esc("&lt;"), "&amp;lt;");
});

test("esc stringifies non-string values", () => {
  assert.equal(esc(42), "42");
  assert.equal(esc(null), "null");
  assert.equal(esc(undefined), "undefined");
});
