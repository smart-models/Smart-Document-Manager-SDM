---
type: llm
focus: trace
weight: 1
---

For Q3 the plan does not download the 648 KB `json_ocr.json` whole and does not start from `document.md`: it chooses `json_rag.json` (303 KB) as the reading source because of `files[].size`, says that pages can still be cited while block ids, boxes and caption links are gone, and if `document.md` is mentioned at all it says that it has no page boundaries, headers, footers or footnotes.
