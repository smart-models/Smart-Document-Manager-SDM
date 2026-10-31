# requirements — CI tooling and image audit inventories

> **Read this when** installing CI dependencies, refreshing an image inventory, or resolving an audit finding.
> **Audience** — maintainers updating Python dependencies and release evidence.
> **Source of truth** — `requirements/api.txt` and MCP `pyproject.toml` for runtime dependencies; this directory for CI tooling and captured audit inventories.
> **Prerequisite** — read the [automation guide](../.github/AGENTS.md).

---

## Purpose and boundaries

**What it is:** lightweight CI tooling plus exact-version audit snapshots captured
from the installed API, MCP and derived vLLM images.

**What it is not:** a claim that existing Docker builds are fully locked or a
replacement for their manifests. Snapshots are not installed on Windows or fed
back into existing Dockerfiles as constraints. vLLM has its own Python version.

## Mental model

1. Change dependencies in their owning runtime manifest, rebuild and validate.
2. Capture installed distributions from that image without starting its server.
3. Commit the inventory and its provenance together; CI catches direct-pin/hash drift.
4. OSV queries the inventories and generates one CycloneDX SBOM per file.

## Map of the code

| Path | Role | Change it when |
|---|---|---|
| `api.txt` | API direct pins | API runtime dependencies change |
| `../ocr-mcp-server/pyproject.toml` | MCP runtime/build dependencies | MCP dependencies change |
| `test.txt` | Lightweight API/MCP test dependencies | Test imports or duplicated runtime pins change |
| `benchmark.txt` | Host-only dataset downloader | Benchmark dataset tooling changes |
| `dev.txt` | Ruff and uv versions | CI tooling changes |
| `ci.txt` | Test environment entrypoint | Lightweight test setup changes |
| `locks/*.txt` | Captured image package inventories | An image's installed dependencies change |
| `inventory.json` | Image IDs, Python versions, inventory hashes | A snapshot is regenerated |
| `security-constraints.txt` | Exact advisory-driven policy pins | A transitive security policy is introduced |
| `vllm-security.txt` | Pins repairing upstream runtime dependency conflicts | The derived model-server build changes |
| `audit-triage.json` | Expiring, image-specific non-applicability decisions | A primary-source review establishes an absent vulnerable operation |

## Interface reference

From the repository root:

```text
python -m pip install -r requirements/ci.txt
python -m scripts.verify_dependency_inventory
python scripts/security_constraints.py
python scripts/verify_dependencies.py --triage requirements/audit-triage.json --sbom dependency-audit/sbom --json-output dependency-audit/audit.json
```

The audit returns 0 clean, 1 vulnerable, 2 invalid/verifier error, or 3 network
unavailable. It installs nothing. Package names/versions are queried against OSV.

## Configuration

CI avoids the large Paddle/GPU dependency set: `requirements/test.txt`
contains the imports used by fake-inference tests. The API's separate Uvicorn
pin is intentionally excluded; the MCP package's Uvicorn satisfies this test
process. Docker images retain independent dependency environments.

`inventory.json` records the captured image identity and SHA-256 of each file.
The vLLM image name must match the default in `compose.yml`, and its recorded
`base_image` must match the digest-pinned `FROM` in `Dockerfile.vllm`.
Inventory hashes prevent silent file edits; they are not cryptographic signatures.

## Behaviour contracts

- Snapshots contain exact names/versions and no editable URLs or credential-bearing paths.
- API/MCP direct Linux pins must match their snapshots.
- Default upstream image drift requires a new vLLM snapshot.
- Multiple visible system distributions are normalized to the version returned by
  `importlib.metadata.version`, matching runtime metadata resolution.
- Snapshot provenance and required release SBOMs change together.

## Capabilities and limits

**You can:** audit installed Python packages without installing Linux wheels on
Windows, generate portable SBOMs and detect direct-pin drift.

**You cannot:** reproduce artifact bytes from these inventories, audit OS packages,
prove the absence of unknown vulnerabilities, or credit a snapshot for a different
image. Snapshots were refreshed from rebuilt security-candidate images; refresh
them again when building an accepted release candidate.

OSV files PaddlePaddle advisories under `paddlepaddle`, while the API image
installs `paddlepaddle-gpu` (from the PaddlePaddle CUDA index, bound by sha256 in
the `Dockerfile`); review `paddlepaddle` advisories for the same version by hand,
because the audit queries only the installed distribution name.

The default vLLM build removes unused Ubuntu repository/Launchpad helpers
(`python3-apt`, `python3-httplib2`, `python3-oauthlib`) and unused incompatible
OpenTelemetry exporters. Its NCCL pin matches the installed Torch requirement;
the build runs `pip check`. The upstream setuptools `<81` requirement is retained.

`--triage` is explicit: without it the audit returns every raw finding. A decision
matches only its named advisories, package/version, inventory SHA-256 and recorded
image identity, and expires on its stated UTC date. Changes or expiration fail
closed. Reports retain `raw_findings`, `raw_unique_cves` and `not_affected` with
reasons/sources; `unique_cves` counts actionable findings. New advisories always
remain actionable. Current rationale and expiring decisions live in
[`audit-triage.json`](audit-triage.json).

## Recipes

**Refresh an inventory:** build and verify the target image, record its image ID
with `docker image inspect <image> --format '{{.Id}}'`, then obtain distributions
using a temporary container with `--rm --pull=never --network=none --read-only
--cap-drop=ALL --entrypoint python` (use `python3` for API/vLLM). The Python probe is:

```python
import importlib.metadata as metadata
import re
names = {re.sub(r"[-_.]+", "-", d.metadata["Name"]).lower() for d in metadata.distributions()}
for name in sorted(names):
    print(f"{name}=={metadata.version(name)}")
```

Replace that target's `locks/<target>-py<minor>-linux.txt`, retain provenance
comments and update its image, image ID, Python version and file SHA-256 in
`inventory.json`. Change release SBOM requirements if the filename changes.
Run the read-only checks and OSV audit. Do not hand-edit installed versions to
make an advisory disappear. A security-policy change requires a real rebuild.
An image with a triage decision also requires a fresh applicability review and
updated image identity; copying a prior decision is not acceptance.

**Apply a security policy:** add an exact version with advisory context to
`security-constraints.txt`, update the owning build dependencies, rebuild and
capture again. The generic `security_constraints.py --write` can prepare a pin
file, but cannot establish an image snapshot; hash drift deliberately refuses it.

## Verification

```text
python -m pytest tests/test_dependency_inventory.py tests/test_verify_dependencies.py tests/test_security_constraints.py -q
```

These checks fake OSV responses and verify parser/error behavior and provenance.
Run the actual network audit separately; local parser tests do not prove it is clean.

## Failure modes

| Symptom | Next check |
|---|---|
| Direct-pin mismatch | Rebuild using the current runtime manifest and recapture |
| Hash mismatch | Regenerate the snapshot and provenance from the actual image |
| Vulnerabilities found | Update affected dependencies/image; rebuild and retest |
| OSV unavailable | Retry deliberately; do not classify the audit as clean |
| New snapshot lacks a release SBOM | Update release workflow required evidence with the inventory |

## Invariants

`tests/test_dependency_inventory.py` checks direct pins, upstream image and hashes.
`tests/test_verify_dependencies.py` checks audit/SBOM and distinct failure states.
`tests/test_ci_workflow.py` requires one SBOM per committed inventory.

## Extending it

A new image target requires an inventory, provenance entry, direct-pin ownership,
verifier/tests and release evidence requirement. Review whether the new target
needs a separate CI environment rather than merging incompatible service pins.

## Related documents

| Document | Covers |
|---|---|
| [Automation](../.github/AGENTS.md) | Workflows and token permissions |
| [Release checklist](../.github/RELEASE_CHECKLIST.md) | Candidate identity and acceptance |
| [MCP package](../ocr-mcp-server/README.md) | Standalone MCP installation |
