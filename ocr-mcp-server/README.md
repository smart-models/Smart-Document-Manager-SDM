# ocr-mcp-server — Agent tools over Smart Document Manager

> **Read this when** connecting an MCP client, configuring the server, or changing a tool contract.
> **Audience** — agents maintaining this repository and operators connecting OCR tools.
> **Source of truth** — this file for transports and MCP behavior; `ocr_mcp/tool_map.py` for coverage; `.env.example` for shipped settings.
> **Prerequisite** — read the [project guide](../README.md) and [environment template](../.env.example).

---

## Purpose and boundaries

**What it is:** fourteen task tools, live metrics, packaged teaching resources
and workflow prompts over the Smart Document Manager API.

**What it is not:** a model host or an in-process import of the API. All service
calls cross the HTTP boundary; the package runs independently of `ocrserver`.

## Mental model

1. An MCP inbound credential opens this server; an independent outbound credential
   authenticates its HTTP requests to the OCR API.
2. Every tool takes one `params` object. Structured content is preserved across
   Markdown and JSON presentations, with bounded text summaries for RAG creation.
3. Inference checks health first. Storage and Office preview do not need a model.
4. Destructive tools check exact confirmation locally before HTTP; the API checks
   it independently. An HTTP timeout never establishes that work stopped.

## Map of the code

| Path | Role | Change it when |
|---|---|---|
| `ocr_mcp/tool_map.py` | Tool-to-endpoint inventory | API routes change |
| `ocr_mcp/inputs*.py`, `rag_contract.py` | Discoverable argument schemas | Arguments or chunk contracts change |
| `ocr_mcp/client.py`, `responses.py` | HTTP requests and response validation | Timeouts or upstream result shapes change |
| `ocr_mcp/guardrails.py` | Local destructive guards | Archive layouts or confirmation semantics change |
| `ocr_mcp/tools/` | Tool descriptions and presentation | Task behavior changes |
| `ocr_mcp/resources/`, `prompts.py` | Teaching resources and workflows | Agent guidance changes |
| `ocr_mcp/server.py`, `auth.py`, `config.py` | Assembly, authentication and settings | Transport or deployment behavior changes |

## Interface reference

`ocr_mcp/tool_map.py` owns the exact endpoint inventory. MCP `tools/list`
advertises argument schemas and result contracts for all fourteen tools.
`resources/list` exposes `ocr://metrics`, the four `docs://ocr/` documents and
six field guides under `docs://guide/` (getting-started, cli, environment,
architecture, document-pipeline, rag-chunking), listed in
`ocr_mcp/resources/docs.py`. A guide read returns the visible text of its
offline HTML page as Markdown; styles, scripts and embedded fonts stay out.
Their canonical pages live in [`guide/`](../guide/README.md); byte-for-byte tests
keep the packaged HTML copies aligned. Prompt discovery advertises
`ocr_extract_document`, `ocr_diagnose` and `ocr_prepare_rag`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| MCP_API_TOKENS | empty, HTTP refuses startup | Inbound token:label entries |
| UPSTREAM_CLIENT_TOKEN | empty | OCR API bearer credential |
| MCP_UPSTREAM_URL | http://127.0.0.1:8020 | Existing OCR service |
| MCP_BIND_HOST | 127.0.0.1 | Published address / default Host allowlist |
| MCP_LISTEN_HOST | 127.0.0.1 | Actual listener; Compose sets 0.0.0.0 |
| MCP_PORT | 8021 | Listener and default published port |
| MCP_ALLOWED_HOSTS | bind host plus local aliases | Host allowlist, with/without port; explicit entries replace defaults |
| MCP_ALLOWED_ORIGINS | empty | No browser clients by default |
| MCP_ALLOW_UNAUTHENTICATED_LOCAL | false | Explicit HTTP local-only development |
| MCP_HTTP_TIMEOUT | 660 | Inference HTTP phase timeout in seconds, above API timeout (600) |
| MCP_CHUNK_HTTP_TIMEOUT | 780 | Total RAG upstream HTTP deadline; allow API OCR timeout + chunk timeout + at least 30 seconds |
| MCP_CHUNK_MAX_ARGUMENT_BYTES | 8388608 | UTF-8 RAG arguments cap, checked again after source/base64 preparation |
| MCP_CHUNK_MAX_RESULT_BYTES | 8388608 | Entire serialized RAG tool-result cap, including summary and metadata; also the raw-byte cap for storage downloads and Office PDFs; minimum 1024 |
| MCP_DIAGNOSTIC_TIMEOUT | 5 | Total seconds for a diagnostic GET, including retries and backoff |
| MCP_STORAGE_TIMEOUT | 150 | Total seconds for a storage list/detail (with retries) or a single download attempt |
| MCP_RETRY_ATTEMPTS | 3 | Maximum diagnostic GET attempts within its deadline; inference POSTs never retry |
| MCP_RETRY_BACKOFF_SECONDS | 0.5 | Initial backoff, with jitter |
| MCP_INPUT_ROOTS | empty | Semicolon-separated allowed server input directories |
| MCP_MAX_FILE_BYTES | 31457280 | Decoded input cap; align with API MAX_FILE_BYTES |

All settings are documented in the root `compose.yml`. Do not place secrets
in URLs or labels. Local unauthenticated mode requires loopback on both bind
and listen hosts. For loopback publishing, the default Host allowlist includes
`127.0.0.1` and `localhost`, with and without `MCP_PORT`. Explicit Host and
Origin allowlists remain authoritative.
IPv6 published hosts use bracketed authorities, such as `[::1]:8021`, in the
default Host allowlist. Upstream URL ports must
be valid; URLs cannot contain credentials, queries or fragments. Timeout and
backoff settings must be finite positive numbers.

Streamable HTTP request bodies are capped at
`max(4/3 × MCP_MAX_FILE_BYTES, 3 × MCP_CHUNK_MAX_ARGUMENT_BYTES) + 64 KiB`
(40 MiB + 64 KiB with the defaults), replacing the SDK's 4 MiB default so every
input the tools accept can arrive as base64. The factor of three covers clients
that send non-ASCII parsed JSON as unicode escapes. A larger body gets HTTP 413
before any tool runs; stdio has no transport cap. `ocr_check_health` reports
both limits and the input roots under `mcp`.

Static tokens publish no OAuth metadata: there is no
`/.well-known/oauth-protected-resource` and no `resource_metadata` in
`WWW-Authenticate`, because no authorization server exists. Clients send the
configured `Authorization` header.

The preflight avoids transferring input to a degraded OCR API and avoids its
PDF rasterization and other preprocessing. Source validation and base64
preparation in MCP happen before the preflight. Health, stats and metrics use
`MCP_DIAGNOSTIC_TIMEOUT`; retries and Retry-After cannot extend that budget.
A timed-out preflight prevents inference from being submitted. The inference
HTTP timeout remains separate and is not a total deadline for the whole tool.
The RAG path has its own total upstream HTTP deadline. Keep its configured
value above the API OCR timeout plus chunk timeout plus a 30-second margin
(750 seconds with API defaults). Other tools retain their existing timeouts.
An HTTP timeout does not establish that upstream work stopped, and does not
return an archive prefix. Do not assume a timed-out call created a retrievable
archive; inspect storage explicitly if it is enabled.

The RAG tool checks both input and result sizes without silent truncation.
Oversized input returns `mcp_input_limit`; oversized HTTP or tool output returns
`mcp_output_limit`, with the byte limit and a REST recovery route. Its text
summary is at most 4096 UTF-8 bytes; the complete file appears only in
`structured_content`. Use `POST /chunk` with the original structured OCR JSON
to recover without inference. Recovering a source-mode call through
`POST /parse/chunks` may run OCR again; the MCP server never retries it or
promises a stored file.

A successful HTTP status still requires a valid JSON object with the documented
field types. Invalid JSON and malformed payloads return a sanitized tool error
with `is_error=true` and empty structured content in both presentation modes;
the rejected body is never copied into the error. Invalid health data prevents
the inference POST. Valid empty OCR text, per-page failures and extra fields are
preserved. PDF detection accepts ASCII whitespace and an optional UTF-8 BOM
before `%PDF-`, matching the API.

## Behaviour contracts

`tool_map.py` maps the OCR, storage, RAG and Office API surface to fourteen tools and
`/metrics` to `ocr://metrics`. Hidden browser routes and framework documentation
are explicitly acknowledged. `client.py` owns every upstream HTTP request;
`inputs.py` validates sources before requests; `responses.py` validates upstream
objects and nested pages, boxes, warnings and diagnostics. `tools/` adds
presentation and readiness preflights. `resources/docs/` ships documents inside
the package.

`ocr_parse_document` supports every parse option including `json_rag`.
`ocr_recognize_image` supports ocr, table, formula, chart, spotting and seal.
Both return full structured content alongside Markdown or JSON text. Page errors
and warnings survive. Clients that read structured content (Claude Code) show
that instead of the text, so a JSON presentation is compact. When the client
sends a progress token, `ocr_parse_document` tags the parse with a
`progress_id`, polls `/parse/progress/{progress_id}` every two seconds and
relays page counts as MCP progress notifications, ending with a completion
report; a failed poll never changes the result.

`ocr_create_rag_chunks` is explicit and optional. Existing `parsed_document`
JSON uses `/chunk` without another OCR or model health check; PDF/image/Office input
uses `/parse/chunks` once. Its complete structured result follows the bundled
`rag.schema.json`; both text presentations contain only a bounded summary.
Sections, exact PaddleOCR counts, table/code protections and strict overflow
follow the API. On strict conflict, choose a larger budget, allowed splitting,
or explicitly permissive overflow; the tool never changes options by itself.
MCP byte limits direct the caller to REST, not a fictional result file.

Storage tools (`ocr_list_storage_archives`, `ocr_get_storage_archive`,
`ocr_download_storage_file`, `ocr_download_storage_zip`) are read-only;
`ocr_delete_storage_archive` and `ocr_delete_storage_file` require an exact
`confirm` value matching the archive job ID or relative artifact name including
directories. MCP refuses mismatches before HTTP, preserving whitespace and case;
the API checks the confirmation independently. Listings carry `input_key` when
the original upload was archived (`OCR_STORAGE_SAVE_INPUT=true`). RAG refinement
and split tools (`ocr_refine_rag_storage`, `ocr_split_rag_group`) mirror the
stored-source API schemas; refinement fills `execution_id` (a fresh UUID) and
`created_at` (current UTC time) when omitted and echoes both. Downloads return
UTF-8 text artifacts as `text`, PNG, JPEG, GIF and WebP up to 3.75 MB as an
image block beside their metadata, and other files as `base64`. Archive details
drop the manifest's `artifacts` list and each image entry's `key`, which repeat
`files`, and add `files[].sha256` for refinement. `ocr_convert_office_to_pdf`
previews Office documents as base64 PDF when the result fits the configured
byte limit. `ocr_check_health` adds an `mcp` object: this server's
`input_roots`, `max_file_bytes` and `max_request_body_bytes`.

Logs carry labels and outcomes, never values: the accepted token labels at
startup, `token verified: <label>` per authenticated HTTP request, and one
`tool '<name>' ok|error <seconds>s` line per tool call, never its arguments.
Over stdio these go to the client's MCP server log, not to `docker logs`.

## Capabilities and limits

**You can:** parse documents, recognize focused elements, create chunks on request,
browse and download archives, explicitly delete artifacts, refine saved RAG
plans, preview splits and convert Office files to preview PDFs.

**You cannot:** use an unmounted client-local path, cancel a submitted inference,
assume a timeout stopped it, or receive partial binary bytes above a size cap.
Storage/Office caps count raw bytes; the RAG creation cap counts the whole
serialized tool result. JSON response validation preserves valid extra fields.

## Recipes

### Installation

Use Python 3.11 and an environment separate from the GPU API dependencies.
From the repository root on Windows:

```powershell
py -3.11 -m venv .venv-mcp
.\.venv-mcp\Scripts\python.exe -m pip install ./ocr-mcp-server
```

On POSIX systems:

```sh
python3.11 -m venv .venv-mcp
.venv-mcp/bin/python -m pip install ./ocr-mcp-server
```

The runtime pins MCP SDK 2.3.0 and mcp-types 2.3.0, validated by the package's API and protocol tests. See the [official SDK package](https://pypi.org/project/mcp/2.3.0/).
Do not install the API's older Uvicorn pin into this environment.

Register the server in the agent host under the name `smart-document-mcp`, for example
`claude mcp add smart-document-mcp -- .venv-mcp/bin/python -m ocr_mcp --transport stdio`. The
skills in `ocr-skill/` refer to its tools as `smart-document-mcp:<tool>`; a host registered
under another name shows them with that name instead.

### Usage

Start the OCR API first. In the repository `.env`, configure
`MCP_API_TOKENS` as comma-separated `token:label` entries with unique labels. Choose fresh secret
values and keep them distinct from `OCR_SERVER_API_TOKENS`.
Set `UPSTREAM_CLIENT_TOKEN` to the API's service token (plain, no label) for
local runs; ocr-api accepts it automatically.
Environment variables override `.env`; unrelated API settings are ignored.
Source checkout runs use the repository root `.env`; an installed wheel reads
only its process environment, never a `.env` from its working directory.

```powershell
.\.venv-mcp\Scripts\python.exe -m ocr_mcp
```

Connect a Streamable HTTP MCP client to `http://127.0.0.1:8021/mcp` with the
header `Authorization: Bearer <configured MCP token>`. A wrong token fails
the session. Tokens never belong in tool arguments.
With the default local Host allowlist, `http://localhost:8021/mcp` also works.

For a process-managed client, use `.venv-mcp/Scripts/python.exe` (Windows) or
`.venv-mcp/bin/python` with arguments `-m ocr_mcp --transport stdio` and the
repository as its working directory. Stdio trusts the process boundary and
does not require an inbound bearer token; upstream authentication still applies.

After reading `docs://ocr/reference`, call:

```json
{"name":"ocr_check_health","arguments":{"params":{"response_format":"json"}}}
```

For extraction, supply real image/PDF bytes in `image_base64` / `pdf_base64`,
or an absolute server path in `file_path` under an allowed input root.
`document_base64` accepts PDF, image or Office bytes (.docx .pptx .xlsx .doc
.ppt .xls .odt .odp .ods); the API detects the format from content and renders
Office files to PDF with LibreOffice before parsing. Office files passed as
`file_path` follow the same input-root rules and are sent as `document_base64`.
Recognition refuses PDF and Office bytes.
For storage archive names, base64 requests may include `filename`; requests
using `file_path` forward only that file's basename automatically.
A local stdio server reads the directories `ocr_check_health` lists in
`mcp.input_roots`. In a container, the client uses the mounted path instead.
Example after mounting a document as `/inputs/invoice.pdf`:

```json
{"name":"ocr_parse_document","arguments":{"params":{"file_path":"/inputs/invoice.pdf","max_pages":2,"output":"both","include_images":false,"json_rag":true}}}
```

Storage archive listing and download:

```json
{"name":"ocr_list_storage_archives","arguments":{"params":{"kind":"parse","limit":10}}}
```

```json
{"name":"ocr_download_storage_file","arguments":{"params":{"key":"invoice.pdf__20261018T120000000000Z__job123AB/page-1.md"}}}
```

Office-to-PDF preview:

```json
{"name":"ocr_convert_office_to_pdf","arguments":{"params":{"document_base64":"<base64-docx>","filename":"report.docx"}}}
```

The operating skill is in [ocr-skill](../ocr-skill/README.md); the server's
`instructions` ask the agent to load it before its first tool call, so install it
wherever the tools are registered.

### Standalone Docker

To run the image outside Compose while using the API on the existing
`smart-document-manager-private` network, first stop the Compose MCP service to free port 8021.
Set `MCP_API_TOKENS` and `UPSTREAM_CLIENT_TOKEN` in the launching shell; a
standalone `docker run` does not read the repository `.env` automatically.
Use separate inbound and upstream credentials. Then run:

```bash
docker run --rm --init --name ocr-mcp-standalone \
  --network smart-document-manager-private -p 127.0.0.1:8021:8021 \
  --read-only --tmpfs /tmp:size=64m,mode=1777 \
  --env MCP_API_TOKENS --env UPSTREAM_CLIENT_TOKEN \
  --env MCP_BIND_HOST=127.0.0.1 --env MCP_LISTEN_HOST=0.0.0.0 \
  --env MCP_UPSTREAM_URL=http://ocr-api:8020 \
  personal-smart-ocr-mcp:latest
```

The listener is reachable inside the container while the published host port
remains restricted to loopback. `--init` forwards termination signals and
reaps child processes. For a different API location, set `MCP_UPSTREAM_URL` to
an address reachable from this container. Verify authenticated MCP discovery;
a listening TCP port alone does not establish protocol or upstream readiness.

### Docker Compose

On first setup, copy the root `.env.example` to `.env` if no local file exists.
Set `MCP_API_TOKENS` in `.env`, then run:

```powershell
pwsh .\stack.ps1 up -Wait
```

On Linux/macOS use `bash stack.sh up --wait`; the
[launcher reference](../scripts/README.md) owns the complete CLI surface.

Compose calls `http://ocr-api:8020` and passes the same `UPSTREAM_CLIENT_TOKEN`
to both services: ocr-api accepts it automatically under the label `@mcp`
while `OCR_SERVER_API_TOKENS` enables authentication, so it must not also
appear in that list. It is required when API authentication is enabled. See
[`.env.example`](../.env.example). The MCP port is published on loopback, the filesystem
is read-only and the process is unprivileged. The `ocr-mcp` service (container
`smart-document-mcp`) starts with the other services by default, after the OCR
API becomes healthy.
The launcher verifies credentials and waits for OCR readiness before starting MCP.
For low-level service maintenance, Compose remains available directly.

For local file input, add a Compose override such as:

```yaml
services:
  ocr-mcp:
    volumes:
      - ./calibration:/inputs:ro
    environment:
      MCP_INPUT_ROOTS: /inputs
```

The client must use `/inputs/...`, not the host's path. Base64 works without
mounts. Paths are literal: leading/trailing spaces are preserved, and a refusal
explains how to remove accidental padding. Input roots must contain only
intended OCR documents in trusted directories. Symlinks are resolved before
checking containment, but checking and opening are not atomic. Use read-only
mounts and prevent untrusted host processes from replacing files/directories
during reads; a read-only container mount alone cannot prevent host changes.

The TCP healthcheck verifies listener availability only; use
`ocr_check_health` to check the upstream inference service.

## Verification

From the repository root, in an environment with its lightweight API test
dependencies (`requirements/test.txt`):

```sh
python -m pytest tests/test_mcp_coverage.py tests/test_mcp_tools.py tests/test_mcp_schema.py tests/test_mcp_server.py tests/test_mcp_client.py
docker compose config --quiet
```

Tests drive real MCP protocol calls with fake HTTP or the real FastAPI handlers
backed by fake inference. Coverage checks compare runtime OpenAPI, hidden
routes, registered tools/resources and upload field constraints. Documentation
checks cover reference names and packaged byte parity, not every prose claim.
Live OCR quality must be assessed using representative documents.
## Failure modes

| Symptom | Next check |
|---|---|
| HTTP startup refuses empty tokens | Configure inbound tokens or explicit local-only unauthenticated mode |
| Client receives 401 or a Host/Origin refusal | Check its bearer token and configured transport allowlists |
| File path is refused | Check mounts, trusted roots and literal whitespace; use base64 remotely |
| Tool reports an invalid upstream payload | Check the API version; rejected content is not echoed |
| Confirmation is refused | Copy the exact job ID or relative artifact name from storage details |
| Binary download exceeds the cap | Use the named REST route; do not raise limits blindly |

## Invariants

- Every API operation is accounted for: `tests/test_mcp_coverage.py`.
- Guards refuse mismatched confirmations before HTTP, and preserve exact text:
  `tests/test_mcp_contract_fixes.py`.
- Malformed storage payloads are tool errors with empty structured content;
  valid extra fields survive: the same regression suite.
- Published documents match the owning skill files byte-for-byte:
  `test_skill_yaml_length_references_and_packaged_documents`.
- Hatch build artifacts explicitly retain the published Markdown and HTML files
  despite parent VCS ignore rules, in both sdist and wheel builds.
  `scripts/verify_mcp_wheel.py` checks resource reads in an isolated installation.

## Extending it

When changing a route, update `tool_map.py`, inputs, HTTP response validation,
the relevant tool module and its tests. Update the operating skill and affected
reference/playbook in the same change, then re-copy its four published documents.
Keep the MCP package independent of the API package; compare mirrored contracts
in tests instead of importing API implementation code at runtime.

## Related documents

| Document | Covers |
|---|---|
| [Skill installation](../ocr-skill/README.md) | Operating and JSON-reading skills |
| [Launcher reference](../scripts/README.md) | Starting and recovering the stack |
| [Environment template](../.env.example) | Credentials and configuration precedence |
| [Project guide](../README.md) | API routes, RAG chunking and S3 storage |
