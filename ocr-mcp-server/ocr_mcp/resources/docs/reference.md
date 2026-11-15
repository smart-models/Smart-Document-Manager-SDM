# Personal Smart OCR tool reference

## Contents

- Wire shape and endpoint coverage
- Sources and limits
- Parsing options and results
- Partial results
- Optional structural RAG
- Recognition tasks and results
- Diagnostics and readiness
- Stored archives
- Stored RAG refinement and split
- Office preview
- Errors and retry discipline
- Authentication and unsupported controls

Terms used throughout: an *archive* is one stored job; a *file* is one object
inside an archive; the *budget* is `options.max_tokens`.

## Wire shape and endpoint coverage

Every call has the wire shape `{"params": {...}}`. All fourteen tools accept
`response_format="markdown"|"json"`, default Markdown, with identical complete
structured content. Clients that read structured content, such as Claude Code,
show it instead of the text; the text rendering serves other clients. Tool execution errors carry `is_error=true` and `{}`; the
text names the API `code` when one exists, plus the next action.

| API operation | MCP | Console |
|---|---|---|
| GET /health | ocr_check_health | Connection status and Monitor |
| GET /stats | ocr_get_stats | Monitor |
| GET /metrics | ocr://metrics resource | Monitor |
| POST /parse | ocr_parse_document | Parse |
| GET /parse/progress/{progress_id} | ocr_parse_document (relayed as MCP progress) | Parse progress |
| POST /ocr | ocr_recognize_image | OCR |
| POST /chunk | ocr_create_rag_chunks, parsed_document | Optional RAG from OCR JSON |
| POST /parse/chunks | ocr_create_rag_chunks, document source | Optional direct RAG |
| GET /storage/archives | ocr_list_storage_archives | Storage list |
| GET /storage/archive | ocr_get_storage_archive | Storage detail |
| GET /storage/file | ocr_download_storage_file | Storage preview/download |
| GET /storage/archive.zip | ocr_download_storage_zip | Storage ZIP |
| DELETE /storage/archive | ocr_delete_storage_archive | Storage delete |
| DELETE /storage/file | ocr_delete_storage_file | Storage delete file |
| POST /storage/rag/refine | ocr_refine_rag_storage | RAG console save |
| POST /storage/rag/split | ocr_split_rag_group | RAG console split preview |
| POST /office/pdf | ocr_convert_office_to_pdf | Office preview |

The hidden `/` and `/assets/config.js` routes are browser bootstrap surfaces.
`/assets` serves local files. `/docs`, `/docs/oauth2-redirect`, `/redoc` and
`/openapi.json` are optional framework documentation, not tools.

## Sources and limits

Exactly one of `file_path`, `image_base64`, `pdf_base64`, `document_base64` is
required for parsing; recognition accepts only the first two. Recognition
requires a single-frame image; multi-frame images return 422
`multiframe_image_not_supported` and belong to parsing.

`file_path` is absolute on the machine running the MCP server and must resolve
inside `MCP_INPUT_ROOTS` (semicolon-separated roots, empty by default). A local
stdio server sees host paths; a container sees only its mounts. Symlink targets
must be inside an allowed root at the containment check. Checking and opening
are not atomic: roots must be trusted directories that untrusted writers cannot
change. Paths preserve leading/trailing spaces; on a refusal, check for
accidental padding. The refusal does not list the roots.

Base64 may be plain or a data URI. Whitespace inside the encoded data, invalid
encoding, empty content and oversized input are refused locally, and
`pdf_base64` must contain PDF bytes (also refused locally otherwise). Hand-copied
base64 is unreliable: hand over bytes produced by a tool, or use `file_path`.

`document_base64` takes PDF, image or Office bytes (.docx .pptx .xlsx .doc .ppt
.xls .odt .odp .ods); the format is detected from content, never from a name.

For archive naming, `filename` names base64 input; with `file_path` the basename
is forwarded, and an explicit `filename` wins over it. Without either, the API
derives a safe name from the detected format.

`MCP_MAX_FILE_BYTES` defaults to 31457280 (30 MiB); the API separately enforces
`MAX_FILE_BYTES` with the same default, so a lower API limit can still return
413. The RAG tool measures its 8 MiB argument cap after base64 encoding, so its
document mode takes about 6 MiB of raw file. Over HTTP the MCP request body is
capped at max(4/3 × `MCP_MAX_FILE_BYTES`, 3 × the RAG argument cap) + 64 KiB
(about 40 MiB); `ocr_check_health` reports it as `mcp.max_request_body_bytes`.

Recognition shares an estimated image memory budget, `OCR_ELEMENT_MEMORY_BYTES`
(default 512 MiB): exhausted capacity, or one image over it, returns 503
`image_memory_full`. Images over the pixel cap return 400 `image_too_large`.

## Parsing options and results

| Field | Values | Default and meaning |
|---|---|---|
| output | markdown, json, both | both; page Markdown and/or blocks |
| max_pages | integer >= 1 | omitted: server `OCR_MAX_PAGES`, 50 unless the operator raised it; the 422 names the limit, as does any archive's `manifest.effective.max_pages` |
| dpi | integer 50–400 | omitted: server `OCR_PARSE_DPI`, normally 250; PDF and Office input |
| table_format | html, markdown | html keeps spans; markdown expands merged cells into repeated pipe-table values |
| include_images | boolean | false; true returns figure crops |
| json_rag | boolean | false; true adds a Qdrant bulk-ingest export `{chunks, metadata}` |
| filename | string ≤ 1024 | archive name for base64 input |
| response_format | markdown, json | markdown; MCP presentation only |

These are the tool's own defaults. The skill proposes the console's instead, which
differ in `table_format="markdown"` and `json_rag=true` (SKILL.md, "Propose the
parameters, then wait").

`max_pages` reads from the first page of a PDF or a multi-frame image; there is
no offset. A longer source returns `pages_truncated` with
`details.{max_pages,total_pages}`. A `dpi` on an image returns `dpi_ignored`.
`table_format=markdown` reports expanded spans as the info warning
`tables_spans_expanded`; block HTML keeps the spans. Crops over
`OCR_MAX_EMBEDDED_IMAGE_BYTES` (normally 20971520 bytes) are dropped with
`images_truncated`. Office input returns the info warning `office_converted`.

Two warnings report how well a page was read, each with
`target={"field":"page","index":<0-based>}`; neither sets a page `error`:

- `layout_low_confidence`: the page's layout boxes scored low
  (`details.layout_score` under `details.threshold`, 0.5). When the layout fails
  this way about half of the page's text is left out; a dense page of many small
  blocks can also score low and be complete. A reason to check the page.
- `table_suspect`: a table or list block (`details.block_id`, `block_type`) was
  read in a loop. `details.reason`: `low_density` (far too little text for its
  size: `megapixels`, `chars`), `cut_repetition` (the text ends where a runaway
  dot leader was cut) or `repeated_rows` (rows repeated to the end).
  `details.reread` says what became of it:
  - `kept`: a second reading replaced the first and the warning is `info`. It
    passed the same checks, so it is usually whole, not surely: tell the user
    the table was read twice.
  - `rejected`: a second reading was made and was no better; the first stays.
  - `skipped`: no second reading was applied (the parse was short of time, or
    the reading failed); the first stays.
  - `off`: the server runs with `OCR_TABLE_REREAD=false`; the first stays.

  With `rejected`, `skipped` or `off` treat the block's text as incomplete.

The result has `pages`, `model_name`, `pipeline_version`, `processing_time_ms`,
`warnings`, `usage` and `json_rag`. `usage` (`tokenizer`, `content_tokens`,
`blocks`) counts parsed block content, not model inference, and is null when
counting is unavailable. `json_rag` is null unless requested; a failed export
stays null with `json_rag_failed`; its chunk `id`s start at 1, the chunk tool's
at 0. It is a different format from
`ocr_create_rag_chunks` output. `json_rag.metadata` repeats the warning codes
(`warnings`) and names pages: `failed_pages`, `unprocessed_pages`,
`low_layout_confidence_pages` and `suspect_table_pages` (1-based; every page
with one of the two warnings above, also when its block was re-read and
`kept`; both keys are missing from an export made before these checks
existed). Its chunks carry no mark: read the two lists before loading chunks
into a vector store.

Each page has `page` (1-based) and nullable `width`, `height` (raster size, the
coordinate space of `bbox`), `markdown`, `blocks`, `images`, `image_names` and
`error`. Each block has `type`, `bbox`, `content`, `content_tokens`, and
nullable `order`, `block_id`, `group_id`, `global_block_id`, `global_group_id`,
`polygon`. Unknown block types pass through and must not be discarded. `images`
are base64 PNG crops paired with `image_names`, not full-page renders. With
archiving on, read single crops from the archive's `images/` files with
`ocr_download_storage_file`, which returns them as images, instead of setting
`include_images`. Every
warning has `code`, `severity`, `message`, nullable `target` and `details`.

PDF form fields are initialised before rasterisation. Pages are processed in
blocks, but the HTTP response is sent only when the document finishes.
Orientation correction, unwarping, chart and seal branches keep the pipeline's
disabled defaults.

## Partial results

HTTP 200 does not mean every page succeeded. Inspect every page and warning:

- A page that failed with budget left has an `error` and a `page_failed`
  warning (`target.index`); later pages continue.
- When `OCR_REQUEST_TIMEOUT` runs out, the request still returns 200: every
  page never reached has `error` `page_unprocessed: …`, and one
  `pages_unprocessed` warning lists them in `details.pages`.
- Error pages have null `width`, `height`, `markdown` and `blocks`.
- A page without an `error` can still miss text: `layout_low_confidence` and
  `table_suspect` (see Parsing options and results) name it.
- A client disconnect during the parse cancels it and skips archiving; a response
  the client refuses after completion changes nothing: the archive exists.
- When the client sends a progress token, `ocr_parse_document` relays page
  counts as MCP progress notifications and ends with a completion report.

## Optional structural RAG

`ocr_create_rag_chunks` accepts exactly one `source_key` (a stored `json_ocr.json`
or `parsed.json`, fetched by the MCP itself), `parsed_document`, `file_path`,
`image_base64`, `pdf_base64` or `document_base64`. JSON mode posts to `/chunk`
and skips model health; document mode posts once to `/parse/chunks` after a
health preflight. Supply the structured `/parse` object, not an export envelope
or Markdown.

`options.max_tokens` is required (1–32768). Defaults: `strictly=true`,
`preserve_tables=true`, `preserve_code_blocks=true`, `overlap_tokens=0`,
`marginal_policy="metadata"`, `table_header_policy="declared"`.
`metadata_fields` selects the metadata written to each chunk; `id`, `text` and
`token_count` are always present, and omitting it keeps all metadata. Overlap
is nonnegative and below the budget. Margins may instead use `text`. Table
headers may use `first_row` (an explicit user assumption) or `none`. `source_id`
and BCP-47 `lang` are metadata, never recognition settings.

Truncation and partial input:

- Document mode without `max_pages`: a document longer than `OCR_MAX_PAGES` is
  refused with 422 `incomplete_ocr` (`details.reason="pages_truncated"`,
  `max_pages`, `total_pages`) before any inference. Pass `max_pages` to accept
  the prefix.
- Existing JSON with `pages_truncated` needs `accept_truncated=true`.
- Any failed or unprocessed page returns 422 `incomplete_ocr`, with no override.

The result is exactly `{"chunks":[...]}` with contiguous numeric IDs from 0, final
text, exact PaddleOCR token counts and flat chunk metadata; `outputSchema`
advertises the full contract (sections, source/context spans, pages, margins,
assets, relations, diagnostics, versions, document scope). Offsets count
Unicode code points. Repeated titles, HTML, headers and overlap are counted. No
embedding is run. A protected oversized table or code block fails strict mode
atomically; permissive mode permits only isolated protected overflow. Budget
errors keep the measured counts and the conflicting option: explain the
choices, never silently change strictness or drop content.

Chunks carry no mark for a page the parse warned about
(`layout_low_confidence`, `table_suspect`). With `source_key` or
`parsed_document` the text summary names those pages
(`low_layout_confidence_pages`, `suspect_table_pages`); the structured result
never does. Document mode cannot name them: `/parse/chunks` returns no parse
warnings. With storage enabled read `warnings` in the archive manifest
(`ocr_get_storage_archive`); without storage run `ocr_parse_document` first
and chunk its result as `parsed_document`.

This tool's argument object is capped by `MCP_CHUNK_MAX_ARGUMENT_BYTES`
(8388608, compact UTF-8, checked again after file/base64 preparation).
`MCP_CHUNK_MAX_RESULT_BYTES` (8388608) caps this tool's whole MCP result and
also every binary download (storage files, ZIPs, Office PDFs). Limit errors
have `code=mcp_input_limit|mcp_output_limit`, `limit_bytes`, `actual_bytes`
when known, and `recovery_route=/chunk|/parse/chunks`; use REST from a client
able to save the full response. Both presentations carry the complete
structured result and a summary under 4096 bytes. `MCP_CHUNK_HTTP_TIMEOUT=780`
is a total upstream deadline; keep it at least `OCR_REQUEST_TIMEOUT +
OCR_CHUNK_TIMEOUT_SECONDS + 30`. Capacity 503 is retryable only by choice.

## Recognition tasks and results

| task | Intended input/output |
|---|---|
| ocr | Plain text from an image; default task |
| table | Table crop; raw model markup: OTSL (`<fcel>` opens a cell, `<nl>` ends a row, no header mark), HTML or Markdown |
| formula | Formula crop; raw formula/LaTeX text |
| chart | Chart crop; raw chart interpretation text |
| spotting | Text regions with parsed boxes |
| seal | Stamp/seal crop |

Recognition returns `result`, `task`, `boxes`, `model_name`,
`processing_time_ms`, `warnings`. `boxes` is null except for spotting; an empty
list is valid. LOC quadrilaterals become axis-aligned envelopes in normalised
0–1000 units, not pixels; legacy numeric rectangles keep their own scale. The
spotting `result` keeps the raw `<|LOC_n|>` tokens after each line; `boxes` is
their parsed form. Small
spotting inputs can be upscaled 2x without converting LOC units. Malformed or
partial coordinates produce `spotting_invalid_coordinates` or
`spotting_incomplete_coordinates`.

`generation_truncated` means vLLM stopped at its token limit: the partial
result is kept with HTTP 200, `target.field="result"` and
`details.finish_reason="length"`. Report it as incomplete and do not retry
automatically. The runtime context limit is 16,384 tokens; no per-request
token control exists. The server offers no cropping, confidence scores,
per-request language/model selection or translation. Recognition is archived like a parse (kind `ocr`, `<stem>.md` holds the
result, `<stem>` being the archive folder's filename without its extension) but its response carries no archive reference: find it in the listing.

## Diagnostics and readiness

`ocr_check_health` forwards `status` (healthy/degraded), `mode`
(ocr/archive_only), `vllm` (reachable/unreachable, or disabled in archive-only
mode), `ocr_enabled`, `model`, `pipeline_version`, `version`, `storage`
(status, last operation, timestamp) and `autotune` (`action`, plus when present
`decisions`, `mode`, `objective`, `reason`, `warmup_seconds`,
`measured_candidates`, `effective`), plus `mcp`: this MCP server's
`input_roots` (where `file_path` may point; empty means base64 only),
`max_file_bytes` and `max_request_body_bytes`. In OCR mode status follows the
periodic vLLM poll; in archive-only mode it follows the archive bucket probe
(degraded until the first probe succeeds, or whenever it fails) and `autotune`
reports `action="disabled"` with an archive-only reason.
Both inference tools check it first and refuse the POST unless healthy, before
the transfer and preprocessing; local source validation happens earlier. An
archive-only API (`mode="archive_only"`, `ocr_enabled=false`) is refused at the
same preflight with a message naming the mode: no POST, nothing to wait for.
The stack runs without vLLM and without a GPU because an operator started it
with `--archive-only` / `-ArchiveOnly` or with `OCR_ARCHIVE_ONLY=true` in
`.env`; OCR comes back only when it is started in OCR mode again (no flag, and
that variable unset or false). Storage, RAG and Office tools are unaffected.

`ocr_get_stats` returns floating-point gauges `queue_size` (acquired parse
slots) and `active`; neither is a job list. `ocr://metrics` returns raw
Prometheus text; a failed read is a protocol error. `ocr_requests_total` counts
`/parse`, `/ocr`, `/chunk`, `/parse/chunks` and `/office/pdf` only, never the
storage routes.

## Stored archives

Archiving needs `OCR_STORAGE_ENABLED`; browsing needs
`OCR_STORAGE_BROWSER_ENABLED` (503 `storage_browser_disabled` otherwise). With
browsing on and archiving off, reads, ZIPs, split and whole-archive deletion
work; single-file deletion and refinement return 503 `storage_write_disabled`,
because they rewrite the manifest. Storage tools never call model health.
Prefixes and keys are opaque: copy them exactly. Archives expire after
`OCR_S3_RETENTION_DAYS` (default 30).

| Tool | Fields | Result |
|---|---|---|
| ocr_list_storage_archives | kind, date or date_from/date_to, cursor, limit | items, next_cursor, skipped_archives |
| ocr_get_storage_archive | prefix | prefix, manifest, files |
| ocr_download_storage_file | key, preview (false) | key, content_type, size, sha256, and `text` or `base64`; images come as an image block |
| ocr_download_storage_zip | prefix | prefix, content_type, sha256, base64 |
| ocr_delete_storage_archive | prefix, confirm = job_id | deleted_objects |
| ocr_delete_storage_file | key, confirm = file name | deleted_objects |

Listing: `kind` is `parse`, `ocr` or `parse-chunks`; dates are UTC
`YYYY-MM-DD`; `date` cannot be combined with a range; `limit` is 1–50 (default
20). Each item has `prefix`, `job_id`, `filename`, `kind` (`unknown` only for
incomplete archives), `created_at`, `object_count`, `total_bytes`,
`partial_ocr`, `unprocessed_pages`, `incomplete` and `input_key` (the archived
original upload `input.<ext>`, or null). Each page reads at most 100
manifests, so a page can hold fewer than `limit` items, or none, while
`next_cursor` is still set: follow it until null. `skipped_archives` counts
archives younger than 10 minutes without a complete manifest (probably still
being written); older ones are listed with `incomplete=true`.

Files: each entry has `key`, `name`, `size`, `content_type` and `sha256` (from
the manifest, null when it has none). The returned manifest drops its
`artifacts` list and the `key` of each `images` entry, which repeat `files`.
Downloads return UTF-8 text files as `text`, PNG, JPEG, GIF and WebP up to
3.75 MB as an image block, and other files as `base64`. `preview=true`
serves text/JSON/Markdown up to 1 MiB and PNG up to 10 MiB; larger files return
413 `storage_file_too_large` (retry with `preview=false`) and other types 415
`storage_preview_unsupported`. `preview=false` downloads up to the storage
limit. The original upload is archived only with `OCR_STORAGE_SAVE_INPUT=true`;
one that would exceed the archive budget is left out with `input_not_saved` in
the manifest's `archive_warnings`.

Incomplete archives: detail and download calls on an `incomplete=true` item
return 404 `storage_not_found`. The ZIP route returns 404 when the manifest is
missing, 503 `storage_invalid_manifest` when it is unreadable, 409
`storage_archive_incomplete` when listed files are missing, and 413
`storage_archive_too_large` over its limits.

Downloads stream and stop at the 8 MiB result cap;
larger files return an error naming the REST route, never partial data. List
and detail GETs retry transient failures inside `MCP_STORAGE_TIMEOUT` (default
150 s); downloads and deletes are a single attempt within it.

Deletion: MCP compares `confirm` exactly with the archive `job_id` or the
file's relative `name` (directories included, such as `images/p0001-0001.png`)
before any HTTP call, preserving case and whitespace; the API repeats the check
and returns 409 `storage_confirmation_required` on a mismatch. MCP refuses
`manifest.json` locally; the API would return 422 `storage_invalid_key`.
Deleting a file also removes its manifest entry. A recent archive without a
complete manifest returns 409 `storage_archive_in_progress`. A partial archive
delete returns 503 `storage_delete_partial` with `deleted_objects`; repeating
the same request is safe. DELETEs are never retried automatically.

## Stored RAG refinement and split

Both tools read a stored `json_rag.json` listed in the archive manifest whose
name matches `(^|[/.])json_rag\.json$`, so a refinement output
(`json_rag_<job>_<n>.json`) cannot be refined again (422 `rag_invalid_source`).
Refinement numbers are never reissued: the manifest's `rag_seq` keeps the highest
number handed out even after that file is deleted.
They verify `source_sha256` against its bytes (409 `rag_source_changed`); get
the hash from `files[].sha256` in `ocr_get_storage_archive`, and read the chunks
with `ocr_download_storage_file`, which returns the JSON as `text`. A file over the
8 MiB result cap cannot be downloaded through MCP, so it cannot be refined
there.

| Field | Rule |
|---|---|
| source_key | stored file key ending in `json_rag.json`, ≤ 1024 chars |
| source_sha256 | 64 lowercase hex characters |
| options.min_tokens, options.max_tokens | required integers ≥ 1, min ≤ max |
| options.page_boundary | false; the console and the skill propose true |
| options.keep_tables_together, keep_code_blocks_together | true |
| group (split) / groups (refine) | 1–100000 entries / groups |
| execution_id (refine) | UUID; omitted → a fresh one, echoed in the result |
| created_at (refine) | ISO 8601 with timezone; omitted → current UTC time, echoed |
| overrides, counts (refine) | optional; one list per group |

A plan entry is a chunk index `n`, a slice `[n, start, end]` or repeated framing
`[n, start, end, "repeat"]`, with nonnegative integers and `start < end`.
Booleans are not integers. The refine plan contract, otherwise 422
`rag_invalid_plan`:

- the groups reference every source chunk, contiguously and in order;
- the non-repeat slices of a chunk partition its text exactly, with no gap or
  overlap;
- `repeat` slices are real framing spans of their chunk;
- override codes match `^[a-z][a-z0-9_]*$` and declare only facts the server
  can derive.

`ocr_split_rag_group` returns `fragments` (`entries`, `token_count`, `status`,
`notes`, `member_token_counts`) and `warnings`; it writes nothing.
`ocr_refine_rag_storage` writes `json_rag_<job>_<n>.json` into the same archive
and returns `prefix`, `file`, `chunk_count`. Repeating an identical request
returns the same file; reusing an `execution_id` for different content returns
409 `rag_execution_conflict`. Plans over 2 MiB return 413 `rag_plan_too_large`.
Neither POST is retried automatically.

Preflight, cut and merge follow one rule set on both sides
(`ocrserver/rag_structure.py`, mirrored by the console). Hard boundaries, which
a group crosses only with a recorded warning: a change of `document`; a failed
page strictly between two chunks' pages; and, with `page_boundary=true`, any
page change or a chunk without a page. Protected joins, which keep two chunks
together: a pipe or HTML table continuing into the next chunk when
`keep_tables_together=true`, and an open code fence when
`keep_code_blocks_together=true`; a page boundary wins over a join. A group's
`status` is `within`, `below`, `above` or `unknown` (a member without
`token_count`) against `min_tokens` and `max_tokens`. `ocr_split_rag_group` cuts
one group at word, sentence, paragraph, list or structural boundaries, repeats
table headers and reopens code fences as `repeat` entries, keeps formulas
atomic, and glues fragments back together while they fit the maximum. Its
`notes` and the saved chunks' `diagnostics` use these codes:

| Code | Severity | Meaning and extra parameter |
|---|---|---|
| `table_split` | warning | a table was cut, or a protected table join broken; `rows` |
| `code_split` | warning | a code block was cut, or a protected fence join broken; `lines` |
| `grapheme_split` | warning | a cut at a non-lexical offset; `tokens` |
| `math_atomic` | warning | a formula kept whole leaves the fragment above the maximum; `tokens` |
| `page_boundary_forced` | warning | the group spans pages with `page_boundary=true`; `pages` |
| `document_forced` | warning | the group spans documents; `documents` |
| `failed_gap_forced` | warning | the group bridges a failed page; `pages` |
| `above_maximum_fragment` | info | measured above `max_tokens`; `tokens` |
| `below_minimum_fragment` | info | measured below `min_tokens`; `tokens` |
| `token_count_unknown` | warning | the fragment could not be measured |
| `search_budget_exhausted` | info | the split search stopped early; `evaluations` |

`counts` and `overrides` are declarations for a client that displays its own
measurements: `counts` must equal the server's per-member measurement, and
`overrides` may declare only `above_maximum_fragment`, `below_minimum_fragment`
or a fact the server derives itself, otherwise 422 `rag_invalid_plan`. A plan
without them is complete. A plan with slice entries needs the tokenizer (503
`rag_tokenizer_unavailable`); a whole-chunk plan sums the saved estimates
(`metadata.refinement.token_count_method`). The saved file carries
`metadata.refinement` with `options`, `range_counts` and any `overrides`. In it,
ids restart at 1 and every field other than `id`, `text`, `token_count` and
`document` becomes a list of the merged sources' distinct values (`page: [2]`,
`type: ["text", "table"]`).

## Office preview

`ocr_convert_office_to_pdf` accepts `file_path` or `document_base64` and an
optional `filename`. The API accepts only Office bytes (PDF and images return
400 `not_office_document`) and enforces `MAX_FILE_BYTES`, the parse queue and
`OCR_REQUEST_TIMEOUT`. LibreOffice runs one conversion at a time, each bounded
by `OCR_OFFICE_TIMEOUT_SECONDS` (default 120); a converted PDF over
`MAX_FILE_BYTES` returns 413 `file_too_large`. Rendering follows LibreOffice,
and spreadsheets paginate by their print setup. The result has `filename`,
`content_type`, `sha256`, `base64`, bounded like storage downloads. No model
health is needed and no archive is written. `ocr_parse_document` already
converts Office input, so use
this tool only to show or keep the rendered PDF.

## Errors and retry discipline

| Failure | Action |
|---|---|
| Local source refusal | Correct source count, encoding, size or allowed file location |
| Invalid MCP argument | Fix field names, enum values and shape; no HTTP was sent |
| 400 invalid_input_count / invalid_base64 | Supply exactly one valid source |
| 400 corrupt_image / corrupt_pdf | Supply readable bytes; suspect hand-copied base64 first |
| 400 image_too_large | Downscale the image |
| 400 office_disabled | Operator disabled Office input; convert to PDF first |
| 422 office_converter_unavailable | Operator installs LibreOffice or fixes OCR_SOFFICE_PATH |
| 422 office_conversion_failed/timeout | Repair or re-save the file, or export it to PDF |
| 401/403 | Operator fixes UPSTREAM_CLIENT_TOKEN; never expose its value |
| 404 | Check MCP_UPSTREAM_URL and API version |
| 413 file_too_large / request_body_too_large | Split or compress; identical input fails again |
| 413 pdf_pixel_limit | Lower `dpi` or omit it, or send fewer pages |
| 400 image_too_large | Downscale the image |
| 422 | Correct options and server limits |
| 422 incomplete_ocr | Pass `max_pages`, accept truncation explicitly, or reparse failed pages |
| 422 tokenizer_mismatch / missing_structured_blocks | Use the structured `/parse` JSON from this server |
| 413 chunk_input_limit / chunk_output_limit / chunk_work_limit | Use a smaller document or REST |
| 504 chunk_timeout | Retry deliberately with a smaller document |
| 429 | Wait before another call |
| 503 queue_full/slots_full/image_memory_full | Inspect ocr_get_stats and reduce concurrency |
| 503 backend_unavailable/shutting_down, backend_error | Inspect ocr_check_health and wait |
| 503 ocr_disabled_archive_only | Archive-only mode: read the stored archives; only an operator starting the stack in OCR mode (no --archive-only / -ArchiveOnly, OCR_ARCHIVE_ONLY unset or false) brings OCR back |
| 500 internal_error | Report it; do not retry in a loop |
| 500 chunk_worker_failed | Retry once deliberately, then ask the operator to check API logs |
| 500 chunk_validation_failed | Ask the operator to check API logs; do not retry in a loop |
| Diagnostic timeout | Check API availability; a failed preflight submitted nothing |
| Inference 504 / timeout | Work may still be running; inspect activity before a deliberate retry |
| Page error in a 200 response | Report the partial result and name the failed pages |
| generation_truncated | Keep the partial text, report incompleteness; no automatic retry |
| 404 storage_not_found | Gone, or incomplete; list again instead of retrying |
| 409 storage_confirmation_required | Reread job_id/name; confirm must match exactly |
| 409 storage_archive_in_progress / storage_archive_busy | Wait; a writer or another change is active |
| 409 rag_source_changed | Download the file again and send its current sha256 |
| 409 rag_execution_conflict | Use a fresh UUID for a different plan |
| 413 storage_file_too_large | Retry with preview=false, or download via REST |
| 413 storage_archive_too_large / storage_listing_too_large / storage_too_many_objects | Download files individually or narrow the filters |
| 415 storage_preview_unsupported | Download with preview=false |
| 422 storage_invalid_* / rag_invalid_* | Correct the named field; copy prefixes/keys verbatim |
| 503 storage_write_disabled | Archiving is off: no single-file deletion or refinement |
| 503 storage_delete_partial | Some objects removed (`deleted_objects`); repeat the same delete |
| 503 storage_browser_busy / rag_tokenizer_unavailable | Wait before a deliberate retry |
| 503 storage_invalid_manifest | The archive manifest is unreadable; report it |
| 503 storage_browser_disabled / storage_unavailable | Operator must enable or repair storage |
| 504 rag_request_timeout / storage_confirmation_timeout | State is unknown; list or download again before retrying |
| MCP result limit on a download | Report the REST route named in the error; a human or REST client fetches it |

POSTs and DELETEs (inference, refinement, split, Office, deletion) are never
retried automatically. Diagnostic GETs retry transient 429/5xx/network failures
up to `MCP_RETRY_ATTEMPTS` (3) with jittered backoff and bounded Retry-After
(30 seconds), within `MCP_DIAGNOSTIC_TIMEOUT` (default 5 seconds) including all
attempts. A 200 degraded response is not retried. `MCP_HTTP_TIMEOUT` (default
660 seconds) bounds inference HTTP phases, DELETE, refine and split calls; keep
it above `OCR_REQUEST_TIMEOUT` (600). For Office previews it is a total
deadline. There are no job IDs, cancellation, cached result lookup or
completion polling. Errors do not echo upstream detail, paths, document bytes
or secrets.

## Authentication and unsupported controls

MCP HTTP requires `MCP_API_TOKENS=token:label,...` by default. The upstream uses
`UPSTREAM_CLIENT_TOKEN`, a plain service token the API accepts automatically
(never listed in its `OCR_SERVER_API_TOKENS`), and must differ from every inbound
token. Wrong inbound tokens fail the MCP session.
Unauthenticated local mode requires loopback bind and listen addresses;
container HTTP mode always uses inbound tokens.

OCR health is public for process checks. With an API token configured, data
operations (stats, metrics, parsing, recognition, storage, Office) require it;
the HTML, configuration flags and assets stay public so users can enter the
token, which the bootstrap never reveals. Browser tokens live in
sessionStorage. `EXPOSE_FRONTEND=false` disables the browser shell and assets.

The only destructive operations are the two storage deletions, guarded by
`confirm`. There are no dry runs, undo, restart, retuning, page offsets,
arbitrary prompts or output-path fields.
