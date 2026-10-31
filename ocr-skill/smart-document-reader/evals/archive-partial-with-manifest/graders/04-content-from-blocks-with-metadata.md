---
type: llm
focus: trace
weight: 1
---

The content summary comes from the blocks in `json_ocr.json` (facts cited with page and block ids, such as `p1:b4`) and includes the page metadata that `document.md` lacks: the header "Northwind Traders — Quarterly Operations Report", the footer "Internal — do not distribute" and footnote 1 linked to the marker in the summary sentence. `document.md` is not treated as the source of the document.
