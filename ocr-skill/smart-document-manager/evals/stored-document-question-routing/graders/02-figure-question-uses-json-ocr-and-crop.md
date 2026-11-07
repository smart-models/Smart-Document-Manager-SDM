---
type: llm
focus: trace
weight: 1
---

For Q2 the plan downloads `json_ocr.json` (not `<stem>.md`) to find the Figure 2 caption and its block, uses `manifest.images[]` (image references named by block type and bbox) to find the stored crop, and notes that the chart crop (`img_in_chart_box_…`) is missing from the archive (`image_unavailable`).
