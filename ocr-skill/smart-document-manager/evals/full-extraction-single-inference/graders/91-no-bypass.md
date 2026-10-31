---
type: llm
focus: trace
weight: 1
---

The run never read `.env` and never called the OCR REST API directly (no `curl`, `Invoke-WebRequest`, `httpx` or `requests` call to port 8020).
