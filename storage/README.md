# storage — Optional SeaweedFS S3 archive gateway and least-privilege bootstrap

> **Read this when** configuring or changing the optional `storage` Compose profile, S3 bucket/IAM provisioning (`storage/bootstrap.py`), the nginx S3 gateway (`storage/nginx.conf`), or S3 credential separation.
> **Audience** — an AI agent working in this repository, and any human reading over its shoulder.
> **Source of truth** — `storage/bootstrap.py`, `storage/nginx.conf`, and `storage/start.sh` for container bootstrap; [`../ocrserver/storage.py`](../ocrserver/storage.py) and [`../ocrserver/storage_browser.py`](../ocrserver/storage_browser.py) for API-side S3 access; [`../scripts/configure_storage.py`](../scripts/configure_storage.py) for local credential generation.
> **Prerequisite** — read [`../ocrserver/README.md`](../ocrserver/README.md) and [`../scripts/README.md`](../scripts/README.md).

---

## Purpose and boundaries

**What it is:** the container-side bootstrap and reverse-proxy configuration for
Smart Document Manager's optional S3 archive profile (`COMPOSE_PROFILES=storage`).
It comprises three Compose services (`seaweedfs` / `document-storage-s3`,
`storage-gateway` / `document-storage-gateway`, and one-shot `storage-init` /
`document-storage-init`) that provide an S3-compatible object store with
four separated privilege roles.

**What it is not:** the API's archive writer or browser router (those live in
`ocrserver/storage.py` and `ocrserver/storage_browser.py`), nor an automatic
credential generator on `stack.* up`. Credentials must be generated explicitly
by the operator via `python scripts/configure_storage.py` before starting the
`storage` profile.

## Mental model

1. **Four S3/IAM identities enforce least privilege:**
   - **Administrator (`OCR_S3_ADMIN_*`)**: used *only* by `storage-init`
     (`storage/bootstrap.py`) to create the bucket, lifecycle rule, and three
     service users. Never passed into the `ocr-api` or `ocr-mcp` containers.
   - **Writer (`OCR_S3_ACCESS_KEY` / `OCR_S3_SECRET_KEY`, user `ocr-writer`)**:
     used by `ocr-api` (`ArtifactStore`) to `ListBucket` and `PutObject`
     archived outputs. Cannot delete objects.
   - **Browser (`OCR_S3_BROWSER_*`, user `ocr-browser`)**: used by `ocr-api`
     (`StorageBrowser`) when `OCR_STORAGE_BROWSER_ENABLED=true` to list,
     download (`GetObject`), and explicitly delete (`DeleteObject`) archives.
   - **Reader (`OCR_S3_READER_*`, user `ocr-reader`)**: external read-only
     identity (`ListBucket`, `GetObject`) for operators or downstream pipelines.
2. **Synchronous one-shot initialization.** During `stack.* up`, the launcher
   starts `seaweedfs` and `storage-gateway`, waits for health, and runs
   `storage-init` (`python -m storage.bootstrap`) synchronously before starting
   `ocr-api`. If `storage-init` fails, `ocr-api` is not started.
3. **Unbuffered nginx gateway (`storage/nginx.conf`).** `storage-gateway`
   proxies S3 (`:8333`) and admin console (`:23646`) traffic to `seaweedfs` over
   the internal `smart-document-manager-storage` network while publishing
   loopback ports on the host.
4. **Index reconcile on bootstrap.** `storage/bootstrap.py` calls
   `ocrserver.storage_index.rebuild_index` after reconciling IAM policies and
   lifecycle rules, so each `up` brings the archive index back in line with the
   bucket.

## Map of the code

| Path | Role | Change it when |
|---|---|---|
| `bootstrap.py` | `storage-init` entrypoint: waits for SeaweedFS, creates bucket, reconciles `ocr-writer`, `ocr-reader`, `ocr-browser` IAM users/policies, configures retention lifecycle, and rebuilds the archive index | IAM policies, bucket lifecycle rules, or bootstrap verification change |
| `nginx.conf` | Unprivileged nginx reverse proxy for S3 (`8333`), admin console (`23646`), and local `/healthz` (`127.0.0.1:8334`) | Gateway timeouts, `client_max_body_size` (`144m`), or proxy headers change |
| `start.sh` | Pre-entrypoint shell guard for `seaweedfs` verifying required admin/console environment variables | SeaweedFS container startup prerequisites change |
| [`../scripts/configure_storage.py`](../scripts/configure_storage.py) | Operator CLI helper that populates missing `OCR_S3_*` / `OCR_SEAWEED_*` secrets in `.env` and adds `storage` to `COMPOSE_PROFILES` | Required storage secret names or defaults change |
| [`../ocrserver/storage.py`](../ocrserver/storage.py), [`../ocrserver/storage_browser.py`](../ocrserver/storage_browser.py), [`../ocrserver/storage_index.py`](../ocrserver/storage_index.py) | API-side writer, browser/delete routes, and S3 archive index | Archive object layout or `/storage/*` API behavior changes |
| [`../tests/test_storage_setup.py`](../tests/test_storage_setup.py), [`../tests/test_storage.py`](../tests/test_storage.py), [`../tests/test_api_storage.py`](../tests/test_api_storage.py), [`../tests/test_storage_browser.py`](../tests/test_storage_browser.py), [`../tests/test_storage_guards.py`](../tests/test_storage_guards.py) | Offline unit tests for `bootstrap.py`, `configure_storage.py`, `ArtifactStore`, `/storage/*` routes, and the archive index reconcile | Any storage bootstrap, API, or index change |

## Interface reference

`python -m storage.bootstrap` (run inside `storage-init`) reads its configuration
from environment variables and prints a single secret-free JSON line on success:

```json
{"storage_initialized": true, "bucket": "ocr-outputs", "retention_days": 30, "identities": 3, "indexed_archives": 0}
```

On failure it exits non-zero with a sanitized error message (`Storage initialization
failed: <ErrorCode_or_Reason>`) that never echoes access keys or secrets.

## Configuration

Defined in `.env.example` Sections 12 and 13:

| Variable | Default | Purpose |
|---|---|---|
| `COMPOSE_PROFILES` | empty | Add `storage` to enable `seaweedfs`, `storage-gateway`, and `storage-init` |
| `OCR_STORAGE_ENABLED` | `false` | Enable writing OCR/RAG outputs to S3 from `ocr-api` |
| `OCR_STORAGE_BROWSER_ENABLED` | `false` | Enable `/storage/*` list, download, whole-archive delete, and RAG split routes; single-file deletion and RAG refinement also need `OCR_STORAGE_ENABLED` |
| `OCR_S3_ENDPOINT` | `http://storage-gateway:8333` | Internal S3 endpoint used by `ocr-api` (`http://seaweedfs:8333` inside `storage-init`) |
| `OCR_S3_BUCKET` | `ocr-outputs` | Dedicated S3 bucket for OCR archives |
| `OCR_S3_RETENTION_DAYS` | `30` | Bucket expiration rule in days (`0` deletes the lifecycle rule) |
| `OCR_STORAGE_SAVE_INPUT` | `false` | Opt in to archiving original uploaded input files alongside parsed outputs; an input that would exceed `OCR_STORAGE_MAX_BYTES`/`OCR_STORAGE_MAX_OBJECTS` is left out (manifest `archive_warnings` code `input_not_saved`) instead of failing the archive |
| `OCR_STORAGE_SAVE_PARSED` | `true` | Save the pre-chunking `/parse` result as `parsed.json` in `parse-chunks` archives; `parse` archives always keep `json_ocr.json` and `document.md` |
| `OCR_S3_ADMIN_ACCESS_KEY`, `OCR_S3_ADMIN_SECRET_KEY` | empty | Provisioning admin credentials (`storage-init` and `seaweedfs` only) |
| `OCR_S3_ACCESS_KEY`, `OCR_S3_SECRET_KEY` | empty | `ocr-writer` credentials for `ocr-api` |
| `OCR_S3_BROWSER_ACCESS_KEY`, `OCR_S3_BROWSER_SECRET_KEY` | empty | `ocr-browser` credentials for `ocr-api` |
| `OCR_S3_READER_ACCESS_KEY`, `OCR_S3_READER_SECRET_KEY` | empty | `ocr-reader` external read-only credentials |
| `OCR_SEAWEED_ADMIN_PASSWORD`, `OCR_SEAWEED_READER_PASSWORD` | empty | SeaweedFS admin console (`:23646`) passwords |

## Behaviour contracts

- **Key rotation is atomic and verified.** `ensure_user()` registers a new
  configured access key via SeaweedFS's IAM extension, verifies it with a real
  signed `list_buckets()` probe, and only then deletes obsolete keys for that
  managed user.
- **Credentials are never logged.** `bootstrap.py`, `configure_storage.py`, and
  `verify_token_config.py` never print secret values on stdout or stderr.
- **Preflight validation (`scripts/verify_token_config.py`)** refuses startup if
  the `storage` profile or `OCR_STORAGE_ENABLED=true` is active while required
  S3 credentials are missing or overlapping across roles.
- **`rebuild_index` reconciles and never renews.** It writes only missing index
  entries, so retention expires an existing entry with its archive. It deletes
  noncanonical entries always, and entries whose archive is gone unless their
  address stamp is under ten minutes old, since a writer may still be reserving
  that archive. Pruning is best effort: a failure there leaves the orphans for
  the next `up` and never blocks startup. A malformed listing stops the run before
  anything is pruned, and readiness is published only after the full scan.

## Capabilities and limits

**You can:** provision idempotent bucket/IAM/retention state on every `up`,
reconcile the archive index on every `up`, rotate managed S3 keys by updating `.env` and re-running `up`, and isolate S3
traffic on the internal `smart-document-manager-storage` bridge network.

**You cannot:** pass `OCR_S3_ADMIN_*` to `ocr-api` (blocked by
`scripts/verify_compose_security.py`), delete objects using `ocr-writer`
credentials, or upload single S3 objects larger than nginx's `144m`
`client_max_body_size`.

## Recipes

**Generate local storage credentials and start the stack with S3 enabled:**

```powershell
python scripts/configure_storage.py
pwsh .\stack.ps1 verify
pwsh .\stack.ps1 up -Wait
```

```bash
python3 scripts/configure_storage.py
bash stack.sh verify
bash stack.sh up --wait
```

## Verification

Run the offline storage bootstrap, verifier, and API storage suites from the
repository root:

```powershell
python -m pytest tests/test_storage_setup.py tests/test_storage.py tests/test_api_storage.py tests/test_storage_browser.py tests/test_storage_guards.py tests/test_mcp_storage.py -q
python scripts/verify_compose_security.py --env-file .env.example
```

For live end-to-end verification against running containers, use
`python scripts/verify_storage_live.py`.

## Failure modes

| Symptom | Next check |
|---|---|
| `Run scripts/configure_storage.py first` or preflight error | Run `python scripts/configure_storage.py` to populate missing `OCR_S3_*` and `OCR_SEAWEED_*` keys in `.env` |
| `Configured identity credentials are invalid` | Access key and secret key are out of sync; rotate both together in `.env` and re-run `up` |
| `SeaweedFS was not ready within 60 seconds` | Inspect `stack.ps1 logs -Service seaweedfs` and verify volume permissions on `ocr-output-storage` |
| `storage_invalid_listing` on every archive listing after an upgrade | A noncanonical index entry fails the indexed listing. Run `stack.* up` so `storage-init` prunes it; rebuilding only `ocr-api` does not run `storage-init` |
| Short or empty listing pages that still carry a cursor | Index entries of archives already gone cost the page its manifest reads; the next `stack.* up` prunes them |

## Invariants

- `ocr-api` never receives `OCR_S3_ADMIN_ACCESS_KEY` or `OCR_S3_ADMIN_SECRET_KEY`:
  enforced by `scripts/verify_compose_security.py` and `tests/test_stack_verifiers.py`.
- All four S3/SeaweedFS credential pairs must be distinct when storage is active:
  enforced by `scripts/verify_token_config.py`.
- `storage-gateway` and `storage-init` run as non-root with `read_only: true`,
  `cap_drop: [ALL]`, and `no-new-privileges:true`.

## Extending it

When changing IAM actions or archive object keys, update `storage/bootstrap.py`
and `ocrserver/storage*.py` together, verify policy compatibility in
`tests/test_storage_setup.py`, and keep `scripts/verify_token_config.py` aligned
with any new credential variables.

## Related documents

- [Core API and `/storage/*` routes](../ocrserver/README.md)
- [Launcher and storage helper reference](../scripts/README.md)
- [MCP storage tools](../ocr-mcp-server/README.md)
- [Environment template](../.env.example)
