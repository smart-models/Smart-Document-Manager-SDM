---
type: llm
focus: trace
weight: 1
---

Every `[index,start,end]` slice entry in the saved plan appears verbatim in the `entries` of an earlier `ocr_split_rag_group` result in the same run, and no saved group of whole chunks has a token sum above 512 unless it is a single chunk.
