# .github — Repository automation and release evidence

> **Read this when** changing a workflow, investigating CI, or preparing a release.
> **Audience** — repository maintainers and coding agents editing automation.
> **Source of truth** — this file for automation rules; `workflows/*.yml` for executable jobs; `../requirements/README.md` for dependency inventories.
> **Prerequisite** — read [repository instructions](../AGENTS.md) and the [launcher guide](../scripts/README.md).

---

## Purpose and boundaries

This directory defines GitHub Actions verification and the operator release
checklist for **Smart Document Manager (SDM)**. It does not implement
application behaviour, run GPU inference, or deploy to a production target. An
agent changing it should first identify whether the change belongs to regular
CI, scheduled dependency auditing, or release acceptance: those have different
triggers, permissions and evidence requirements.

## Mental model

1. Pull requests and pushes to `main` run the read-only `CI` workflow. Its
   aggregate `CI` job depends on all seven required jobs (`lint`, `unit`,
   `frontend-unit`, `guides`, `launchers`, `compose`, `wheel`) and fails for
   any failure, cancellation or skip. Require that single `CI` check in the
   effective branch ruleset for `main`; YAML alone cannot enable branch
   protection.
2. Dependency auditing (`dependency-audit.yml`) runs separately on schedule or
   manual dispatch because it queries the external OSV database. It alone holds
   `issues: write` to create, update or close the one deduplicated
   `dependency-audit` issue.
3. Release checking (`release-check.yml`) reuses `ci.yml` for the exact
   candidate SHA, runs the OSV audit with the committed expiring triage file,
   and hashes all collected artifacts and SBOMs into a SHA-labelled evidence
   bundle. A complete manifest proves artifact presence and integrity, not live
   GPU acceptance — follow [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) too.

## Map of the code

| Path | Responsibility | Read/change it when |
|---|---|---|
| [workflows/ci.yml](workflows/ci.yml) | Read-only verification across 7 jobs (`lint`, `unit`, `frontend-unit`, `guides`, `launchers`, `compose`, `wheel`) plus the aggregate `CI` gate and per-job evidence artifacts. | Adding a required suite or changing supported test environments. |
| [workflows/dependency-audit.yml](workflows/dependency-audit.yml) | Scheduled/manual OSV audit, CycloneDX SBOM generation and deduplicated vulnerability issue lifecycle. | Changing audit scheduling, triage wiring or issue reporting. |
| [workflows/release-check.yml](workflows/release-check.yml) | Reused `ci.yml`, inventory/security-constraint check, OSV audit and SHA-256 evidence bundle (`manifest.json`). | Changing the automated subset of release acceptance. |
| [dependabot.yml](dependabot.yml) | Monthly grouped update PRs for GitHub Actions pinned to commit SHAs. | Changing how action updates are proposed. |
| [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) | Operator release steps, outcome definitions, numbered evidence bundle and rerun/abort rules. | Assessing whether a candidate has sufficient evidence to release. |
| [../scripts/verify_dependencies.py](../scripts/verify_dependencies.py) | OSV queries, expiring triage evaluation and CycloneDX SBOM output. | Changing audit parsing or report format. |
| [../scripts/release_evidence.py](../scripts/release_evidence.py) | Required-artifact verification and SHA-256 manifest builder. | Changing release evidence format. |
| [../tests/test_ci_workflow.py](../tests/test_ci_workflow.py) | Workflow contract tests (permissions, SHA pins, aggregate gate, npm lock, SBOM parity). | Changing workflow wiring. |

The workflows orchestrate existing repository owners rather than reimplementing
them: [`scripts/`](../scripts/README.md) owns launcher, security, audit and
evidence logic; [`tests/`](../AGENTS.md#offline-test-suites) and
[`frontend/`](../AGENTS.md#frontend-and-browser-suites) own the offline test
suites. The required `guides` job runs both `guide/tests` and the standalone
Playwright configuration (`frontend/playwright.guides.config.js`) over local
`file://` pages with no server or network; see
[`guide/README.md`](../guide/README.md#verification).

Dependabot checks the `github-actions` ecosystem monthly so immutable action
pins still receive update proposals. Python requirements and captured image
inventories are intentionally excluded: they follow the verified-image capture
procedure in [`requirements/README.md`](../requirements/README.md).

## Interface reference

| Workflow | Trigger | Permissions | Result |
|---|---|---|---|
| `CI` (`ci.yml`) | Push to `main`, every PR, `workflow_call`, `workflow_dispatch` | `contents: read` | Required aggregate `CI` check and `ci-<job>-<attempt>` artifacts |
| `Dependency audit` (`dependency-audit.yml`) | Monday `04:17 UTC` or `workflow_dispatch` | `contents: read`, `issues: write` | Audit JSON, SBOMs and one maintained `dependency-audit` issue |
| `Release check` (`release-check.yml`) | `workflow_dispatch` | `contents: read` | Full reused `CI` plus OSV audit and `release-evidence-<sha>-<run_id>-<attempt>` |

| Required CI job | What it verifies |
|---|---|
| `lint` | `compileall`, critical Ruff rules across the repo, full Ruff lint/format (`E`, `W`, `F`, `D`) on CI/launcher/guide modules, `verify_dependency_inventory`, `security_constraints`, pinned `actionlint`, `shellcheck stack.sh`, and `PSScriptAnalyzer` on `stack.ps1` |
| `unit` | Offline API, chunking, autotune, calibration, storage, MCP, skill and automation pytest suites on Python 3.11 with Node 24 planner parity |
| `frontend-unit` | Node unit tests (`npm test`) and mocked Playwright browser suite (`--project=mock`) on Node 24 |
| `guides` | Offline HTML field-guide link/anchor and `.env.example` parity tests (`pytest guide/tests`) plus isolated `file://` Chromium checks (`playwright.guides.config.js`) |
| `launchers` | `test_stack_core.py`, `test_stack_launchers.py` and `test_stack_verifiers.py` exercising real `bash` and `pwsh` parity against a Docker stub |
| `compose` | `docker build --check` for `--target ocr-api`, `--target ocr-mcp` and `Dockerfile.vllm`; `docker compose config` for base and `storage` profiles; `verify_compose_security.py`; `stack.sh verify` with `--no-mcp` and with synthetic credentials |
| `wheel` | Isolated Python 3.11 build and install of the `ocr-mcp` wheel (`verify_mcp_wheel.py`), packaged `docs://` resource parity and real stdio MCP handshake |

## Configuration

Actions are pinned to full 40-character commit SHAs with version comments.
`actionlint` `1.7.12` is downloaded in `lint` only after its archive SHA-256 is
verified. Python 3.11 and Node 24 match the supported local test environment.
The upstream vLLM image inventory uses its own Python 3.12 environment; the
model server is not started in CI.

Workflows use [`.env.example`](../.env.example) only — never a local `.env` or
production secret. Because `.env.example` ships with `MCP_API_TOKENS` empty,
the `compose` job verifies both the `--no-mcp` path and the MCP-enabled path
using synthetic test tokens (`ci-synthetic-mcp:ci` and `ci-synthetic-api`) that
are never used to start a container.

## Behaviour contracts

- Every required job in `ci.yml` must appear in `jobs.ci.needs`, and any
  `failure`, `cancelled` or `skipped` result fails the aggregate `CI` gate.
- Every `actions/checkout` step sets `persist-credentials: false`; only
  `dependency-audit.yml` grants `issues: write`.
- Node jobs install `frontend/package-lock.json` via `npm ci --ignore-scripts`
  and cache npm against that lockfile.
- `release-check.yml` reuses `ci.yml` on the same commit and downloads only the
  current attempt's artifacts (`ci-*-${{ github.run_attempt }}`), so a partial
  rerun cannot mix stale evidence from an earlier attempt.
- Release evidence requires `metadata.txt`, all CI job logs/XMLs (including
  `guides.log` and `guides.xml`), `mcp-wheel.json`, `security-constraints.log`,
  `dependency-audit.log`, `dependency-audit.json` and one CycloneDX SBOM per
  committed lock (`sbom/app-py311-linux.cdx.json`,
  `sbom/mcp-py311-linux.cdx.json`, `sbom/vllm-py312-linux.cdx.json`).
- Both audit workflows pass `--triage requirements/audit-triage.json`. An
  expired decision, changed inventory hash, or changed image identity fails
  closed; new advisories are never covered by an existing triage entry.

## Capabilities and limits

**You can:** verify API/MCP/chunking/frontend/launcher/guide contracts offline,
retain JUnit and browser summaries, audit captured Python distributions against
OSV, and produce a hashed release-evidence bundle for an exact commit SHA.

**You cannot:** infer OCR quality or GPU memory bounds from fake-inference
tests, approve a skipped hardware calibration pass, prove an image is
vulnerability-free from a clean OSV scan, or deploy from these workflows.
`docker build --check` validates Dockerfile syntax and instructions without
building the multi-gigabyte OCR/vLLM images.

## Recipes

**Validate automation locally, from the repository root:**

```powershell
python -m pip install -r requirements/ci.txt
python -m pytest tests/test_ci_workflow.py tests/test_branding_contract.py tests/test_release_evidence.py tests/test_verify_dependencies.py tests/test_security_constraints.py tests/test_render_dependency_audit_issue.py tests/test_dependency_inventory.py -q
actionlint
```

```bash
python3 -m pip install -r requirements/ci.txt
python3 -m pytest tests/test_ci_workflow.py tests/test_branding_contract.py tests/test_release_evidence.py tests/test_verify_dependencies.py tests/test_security_constraints.py tests/test_render_dependency_audit_issue.py tests/test_dependency_inventory.py -q
actionlint
```

**Run the dependency audit on GitHub:** Actions → `Dependency audit` → Run
workflow. Exit `0` = clean (closes any open `dependency-audit` issue); exit `1`
= actionable vulnerabilities (creates/updates the issue); exit `2` = verifier
error; exit `3` = OSV unreachable.

**Prepare a release candidate:** follow
[RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md), dispatch `Release check` on the
candidate ref, download `release-evidence-<sha>-<run_id>-<attempt>`, and verify
its manifest hashes alongside the live GPU and storage acceptance checks.

## Verification

`tests/test_ci_workflow.py` checks aggregate job coverage, permissions,
full-SHA action pins, `persist-credentials: false`, single-line `#` comment
hazards in `run: >-` scalars, `npm ci --ignore-scripts` lockfile alignment,
`actionlint` wiring, `release-check.yml` attempt-scoped artifact reuse, triage
flags, and 1:1 SBOM requirements against `requirements/locks/*.txt`.

Local checks do not prove that GitHub Actions executed a workflow or that
branch protection is active on `main`. For cancelled candidate runs, follow the
[release rerun procedure](RELEASE_CHECKLIST.md#recovering-a-cancelled-candidate-run).

## Failure modes

| Symptom | Inspect first |
|---|---|
| Individual jobs pass but aggregate `CI` fails | A required job was skipped or cancelled; inspect job conditions and `jobs.ci.needs`. |
| Inventory hash or direct-pin mismatch | Rebuild the affected image and regenerate its `requirements/locks/*.txt` snapshot and `requirements/inventory.json` entry. |
| Audit fails with exit 1 | Actionable OSV vulnerability or expired/mismatched triage entry in `requirements/audit-triage.json`. |
| Audit fails with exit 3 | OSV API was unreachable; retry deliberately — never treat an outage as a clean scan. |
| `npm ci` fails in `unit`, `frontend-unit` or `guides` | `frontend/package-lock.json` must match `frontend/package.json`. |
| Candidate CI run was cancelled by a newer push | Follow the exact-SHA rerun procedure in [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md#recovering-a-cancelled-candidate-run). |
| Release manifest step fails | A required log, XML, JSON or SBOM artifact was not produced by an upstream job. |

## Invariants

- `tests/test_ci_workflow.py` enforces permissions, SHA pins, checkout
  credentials, aggregate coverage, Node lockfile installation, `actionlint`,
  guide evidence, and SBOM parity.
- `tests/test_release_evidence.py` enforces SHA-256 manifest generation and
  missing-file refusal.
- `tests/test_dependency_inventory.py` and `tests/test_security_constraints.py`
  enforce runtime pin alignment, inventory provenance, and audit-only lock
  boundaries.

## Extending it

Add any new required suite to `workflows/ci.yml`, `jobs.ci.needs`, the evidence
requirements in `workflows/release-check.yml`, `tests/test_ci_workflow.py`, and
[RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) in the same commit. Keep reusable
checks in `scripts/` rather than embedding complex logic in YAML, and never
grant write permissions or secrets to pull-request workflows.

## Related documents

- [Repository agent guide](../AGENTS.md)
- [Release candidate gate](RELEASE_CHECKLIST.md)
- [Dependency inventories and OSV triage](../requirements/README.md)
- [Launcher and verifier reference](../scripts/README.md)
- [Offline HTML field guides](../guide/README.md)
- [MCP server reference](../ocr-mcp-server/README.md)
