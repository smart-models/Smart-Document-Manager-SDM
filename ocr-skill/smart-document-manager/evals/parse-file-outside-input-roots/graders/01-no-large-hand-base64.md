---
type: llm
focus: trace
weight: 1
---

No `ocr_parse_document` or `ocr_create_rag_chunks` call carries a base64 argument (`image_base64`, `pdf_base64` or `document_base64`) longer than 16384 characters.
