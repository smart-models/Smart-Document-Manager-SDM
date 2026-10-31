# Release candidate gate

This is the operator acceptance gate for a release of **Smart Document Manager
(SDM)**. Keep it alongside CI: hosted checks cannot establish real GPU
inference, startup hardware calibration, LibreOffice Office-to-PDF conversion,
SeaweedFS archive lifecycle, live browser journeys, or skill-guided MCP usage.
This file defines the required evidence and decision; the linked subsystem
guides own execution details.

Passing this checklist does not itself publish, tag, push or deploy anything.
Those actions require the repository owner's explicit release instruction.

## Evidence and decision contract

Create an ignored `release-evidence/<UTC timestamp>-<full commit SHA>/` bundle.
Before execution, create `00-scope.json` with the candidate SHA, required cases
and variants, target GPU hardware, initial stack state, storage profile state,
expected evidence files and operator. Define the complete-stack scope before
testing; do not narrow it afterward merely to hide a failure.

For each check, save sanitized stdout/stderr or tool/browser traces, the command
or procedure, UTC time, exit status, expected outcome, actual outcome and image/
profile identity. Use the artifact locations below and record individual file
paths in the scope ledger. Never include tokens, a local `.env`, confidential
OCR documents or credentials embedded in command lines.

Keep outcomes distinct:

| Outcome | Release interpretation |
|---|---|
| PASS | The required assertion and its independent postconditions passed on the recorded candidate and layer. |
| FAIL | An expected assertion failed; investigate the defect or invalid test setup. A green runner with expected failures is not an all-PASS campaign. |
| BLOCKED / NOT RUN / SKIPPED | Required evidence is missing; not proof of an application defect, but insufficient for release acceptance. |
| PARTIAL | Only some required variants/layers ran; the full case is not accepted. |
| Not applicable | Written contract-based rationale established in the scope ledger; missing hardware or tools is not inapplicability. |

Expected negative responses can pass a refusal/recovery test when the exact
contract and absence of unintended effects are verified. A timed-out or refused
calibration pass proves fallback behaviour, not completion of an unmeasured
candidate.

Stop the affected campaign and withhold acceptance if candidate identity drifts,
a required assertion fails, a prerequisite or evidence file is missing, fixture
cleanup fails, unexpected mutations occur, or secrets enter an artifact.
Preserve safe diagnostics, perform authorized cleanup/restoration, and record
the blocker. A missing NVIDIA GPU, service, token or network connection leaves
the relevant check BLOCKED; never substitute a mock or an old run silently.

## Required steps

1. **Freeze identity and scope.** Require a clean candidate worktree; save the
   full SHA, timestamp, operator, tool versions, pinned model revision
   (`DEFAULT_MODEL_REVISION`) and initial runtime state in `01-candidate.txt`.
   Record `00-scope.json` before running. Associate every result with the exact
   `ocr-api`, `ocr-mcp` and `vllm-paddleocr` image IDs.

2. **Verify the private reporting channel.** Record the repository owner's
   designated private vulnerability-reporting route and its actual availability
   in `02-private-reporting.txt`, without submitting a dummy vulnerability.
   Check repository settings as needed with an authorized administrator. If a
   policy file such as `SECURITY.md` is absent, do not follow a nonexistent
   link or treat a previous policy as current.

3. **Require CI for the exact SHA.** Save workflow URLs, conclusions and SHA in
   `03-ci.txt`. Require the aggregate `CI` and all seven of its required jobs
   (`lint`, `unit`, `frontend-unit`, `guides`, `launchers`, `compose`, `wheel`)
   to succeed. Verify that the effective branch protection or ruleset for `main`
   requires the `CI` check before merging; record the setting and any applicable
   bypass. If a candidate run was cancelled, follow the
   [rerun procedure](#recovering-a-cancelled-candidate-run). Dispatch
   `Release check` (`release-check.yml`) for that same candidate when authorized
   and preserve its downloaded artifact (`release-evidence-<sha>-<run_id>-<attempt>`)
   unchanged under `03-workflow/`. A historical green run or an artifact from
   another SHA cannot be credited.

4. **Close dependency, inventory and model provenance.** Follow the
   [dependency inventory and audit procedure](../requirements/README.md).
   Require `python -m scripts.verify_dependency_inventory`,
   `python scripts/security_constraints.py`, and
   `python scripts/verify_dependencies.py --triage requirements/audit-triage.json`
   to pass; retain `04-security-constraints.log`, `04-dependency-audit.log`,
   `04-dependency-audit.json` and all three CycloneDX SBOMs
   (`app-py311-linux.cdx.json`, `mcp-py311-linux.cdx.json`,
   `vllm-py312-linux.cdx.json`) under `04-sbom/`. Verifier exits `1`
   (vulnerable), `2` (error) and `3` (OSV unavailable) all block acceptance.
   Review every active `not_affected` decision in `requirements/audit-triage.json`
   for primary-source rationale, image ID binding and expiration date.

5. **Build and identify images.** Use the supported
   [launchers](../scripts/README.md) to build `vllm-paddleocr` (from the
   digest-pinned base in `Dockerfile.vllm`), `ocr-api` and `ocr-mcp` (from the
   root multi-target `Dockerfile`). Confirm that installed distributions match
   `requirements/locks/*.txt` and `requirements/inventory.json`. Save image IDs,
   base digests and build outcomes in `05-image-digests.txt`.

6. **Require the offline, wheel and guide checks.** Use the current
   [CI workflow](workflows/ci.yml) and
   [repository verification contract](../AGENTS.md#full-verification-pass).
   Cover `compileall`, Ruff lint/format, `actionlint`, `shellcheck`,
   `PSScriptAnalyzer`, the offline pytest suites (`tests/` and `guide/tests/`),
   Node unit tests (`npm test`), mocked Playwright browser tests
   (`--project=mock`), offline field-guide Playwright tests
   (`playwright.guides.config.js`), `verify_frontend_assets.py`, and isolated
   MCP wheel verification (`verify_mcp_wheel.py`). Store results or exact-SHA
   CI references under `06-offline/`.

7. **Validate deployment and security shapes.** Verify Compose resolution for
   both the base profile and `--profile storage`, `verify_compose_security.py`,
   and `verify_token_config.py`. Exercise planner dry-runs for MCP
   enabled/disabled (`--mcp` / `--no-mcp`) and frontend enabled/disabled
   (`--frontend` / `--no-frontend`). Save sanitized plans and results in
   `07-compose/`.

8. **Verify GPU startup, calibration and API live behaviour.** Start the stack
   on representative NVIDIA hardware through `stack.ps1` / `stack.sh` with
   authenticated local configuration. Run `python scripts/verify_autotune_live.py`,
   `python scripts/verify_model_alignment.py` and `python scripts/verify_api_live.py`.
   Verify PDF, image and headless LibreOffice Office/ODF extractions (`/parse`,
   `/ocr`, `/office/pdf`), deterministic RAG chunking (`/chunk`,
   `/parse/chunks` and `python scripts/verify_rag_smoke.py`), and protected-route
   refusal (`401` without/wrong bearer token). Save sanitized outputs and
   telemetry under `08-gpu-api/`.

9. **Verify optional S3 storage (`storage` profile).** Enable the `storage`
   profile with `python scripts/configure_storage.py`, start the stack, and run
   `python scripts/verify_storage_live.py`. Confirm writer/reader/browser/admin
   credential separation, internal network isolation between
   the `smart-document-manager` and `document-storage-s3` containers, archive listing/download,
   manual RAG chunk refinement persistence, retention policy, and confirmed
   deletion using disposable fixtures. Save evidence under `09-storage/`.

10. **Execute live browser console acceptance.** With the stack running, execute
    the live Playwright project (`npm run test:live` in `frontend/`) against the
    authenticated console across Parse, OCR, RAG, Storage and Monitor tabs.
    Save results and screenshots under `10-playwright-live/`.

11. **Execute live MCP and skill-guided acceptance.** Run
    `python scripts/verify_mcp_live.py` against the running `smart-document-mcp`
    container to verify bearer authentication, Host/Origin protections, tool/
    resource/prompt discovery, localconfirmation guardrails and calibration OCR.
    Exercise agent workflows using the `smart-document-manager` and
    `smart-document-reader` skills under `ocr-skill/`, and save sanitized traces
    under `11-mcp-agent/`.

12. **Restore environment and record final decision.** Remove disposable test
    archives, restore initial service configuration (or stop the stack cleanly
    with `down`), and record before/after state in `12-restoration/`.

## Automation boundary and final decision

The [Release check workflow](workflows/release-check.yml) reuses `ci.yml` on the
candidate ref and produces its own artifact (`release-evidence-<sha>-<run_id>-<attempt>`,
retained for 90 days) containing `metadata.txt`, all CI logs/XMLs (`lint.log`,
`actionlint.log`, `shellcheck.log`, `psscriptanalyzer.log`, `unit.log`,
`unit.xml`, `launchers.log`, `launchers.xml`, `frontend-unit.log`,
`frontend-browser.log`, `guides.log`, `guides.xml`, `compose.log`, `wheel.log`,
`mcp-wheel.json`), `security-constraints.log`, `dependency-audit.log`,
`dependency-audit.json`, the three `sbom/*.cdx.json` files and `manifest.json`.
Preserve that downloaded bundle under `03-workflow/` and reference it from the
operator scope ledger.

Create `13-decision.json` with the candidate SHA, required-case/layer outcomes,
blockers, evidence references, completion time and the operator's decision. Then
run [`scripts/release_evidence.py`](../scripts/release_evidence.py) over the
operator bundle, supplying `--commit <full candidate SHA>` and `--require
<relative file>` for every expected file in the scope ledger.

`status: complete` in `manifest.json` means only that the listed files exist and
were hashed; it does not inspect exit statuses or turn a failed or skipped live
check into PASS. Require both manifest completeness and an affirmative review of
every required outcome before accepting a release candidate.

## Recovering a cancelled candidate run

`ci.yml` cancels superseded runs on the same ref, while `release-check.yml`
queues its own runs (`cancel-in-progress: false`). If a reused `CI` run for the
candidate SHA is cancelled, recover by either dispatching `Release check` again
or rerunning **all jobs** of the candidate run via GitHub CLI (artifacts are
scoped to `github.run_attempt`, so rerunning only failed jobs cannot silently
reuse an earlier attempt's artifacts):

```bash
gh run list --repo OWNER/REPO --workflow ci.yml --commit CANDIDATE_SHA --json databaseId,headSha,status,conclusion,url
gh run view RUN_ID --repo OWNER/REPO --json workflowName,headSha,event,status,conclusion,jobs,url
gh run rerun RUN_ID --repo OWNER/REPO
gh run watch RUN_ID --repo OWNER/REPO --exit-status
gh run view RUN_ID --repo OWNER/REPO --json workflowName,headSha,event,status,conclusion,jobs,url
```

Before rerunning, verify that `headSha` matches the exact candidate commit.
Never credit a green run or artifact from a newer commit on the same branch.

## Maintenance

Update workflow producers, the `--require` list in `workflows/release-check.yml`,
`tests/test_ci_workflow.py` and this checklist in the same commit. Actions are
updated by monthly Dependabot PRs; review SHA provenance before merging.
Inventory regeneration and expiring OSV triage rules are documented in
[`requirements/README.md`](../requirements/README.md).
