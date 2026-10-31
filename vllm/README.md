# vllm — Stage-1 GPU hardware tiering and vLLM model-server entrypoint

> **Read this when** changing how the `vllm-paddleocr` container detects the GPU, selects a VRAM tier, builds the `vllm serve` command, or writes `/app/runtime/vllm-hardware.json`.
> **Audience** — an AI agent working in this repository, and any human reading over its shoulder.
> **Source of truth** — `vllm/stage1_entrypoint.py` for the container entrypoint and runtime manifest; [`../ocrserver/autotune/tiers.py`](../ocrserver/autotune/tiers.py) for GPU detection, tier thresholds, and `build_vllm_command()`; [`../Dockerfile.vllm`](../Dockerfile.vllm) for image hardening.
> **Prerequisite** — read [`../ocrserver/README.md`](../ocrserver/README.md) and [`../scripts/README.md`](../scripts/README.md).

---

## Purpose and boundaries

**What it is:** the pre-exec Python entrypoint (`vllm/stage1_entrypoint.py`)
for the `vllm-paddleocr` service. At container start it probes the visible
NVIDIA GPU, selects memory/concurrency/dtype parameters, writes an atomic
hardware manifest to the shared `ocr-runtime-profile` volume, and `os.execvp`s
`vllm serve` for `PaddlePaddle/PaddleOCR-VL-1.6`.

**What it is not:** the OCR pipeline or HTTP coordinator (`ocrserver/` owns
layout detection, reading order, and API budgets). It does not run calibration
benchmarks itself; [`../autotune_entrypoint.py`](../autotune_entrypoint.py)
runs in the `ocr-api` container after `vllm-paddleocr` becomes healthy.

## Mental model

1. **Two-stage calibration shares one runtime volume.** Stage 1 (`vllm/stage1_entrypoint.py`
   in `vllm-paddleocr`) detects VRAM/compute capability, chooses vLLM startup
   flags (`--gpu-memory-utilization`, `--max-num-seqs`, `--max-num-batched-tokens`),
   and writes `/app/runtime/vllm-hardware.json` (`0644`). Stage 2
   (`autotune_entrypoint.py` in `ocr-api`) reads that manifest to fingerprint the
   runtime and calibrate API-side concurrency.
2. **Explicit environment overrides win over automatic tier selection.**
   Leaving `OCR_VLLM_GPU_MEM_UTIL` and `OCR_VLLM_MAX_NUM_SEQS` empty allows
   GPU-based selection. `OCR_VLLM_MAX_NUM_BATCHED_TOKENS` defaults to 16384.
   Memory uses estimated weights/runtime/KV needs capped by free VRAM; max
   sequences is 16 below 16 GiB, 32 from 16 GiB and 64 from 24 GiB.
3. **Immutable model revision pin.** `DEFAULT_MODEL_REVISION`
   (`c5630abae1d940eafe0697512a0325494b02ab42`) is passed to `--revision` so a
   cold start never pulls a moving upstream branch.
4. **Trusted per-request multimodal kwargs.** The generated command includes
   `--trust-request-mm-kwargs` because `ocrserver/vllm_client.py` sends
   `mm_processor_kwargs` (`min_pixels`/`max_pixels`) on every request; the only
   client is the `ocr-api` container on the private network. Without the flag,
   vLLM ≥0.26 rejects every request with a 400 `mm_processor_kwargs` error.
5. **Hardened derived image (`Dockerfile.vllm`).** The `vllm-paddleocr` container
   runs a thin security layer (`ocr-vllm:0.32.0-security`) over the digest-pinned
   upstream `vllm/vllm-openai:v0.32.0` image, removing unused OS/Python packages
   with known advisories and repairing NCCL metadata.

## Map of the code

| Path | Role | Change it when |
|---|---|---|
| `stage1_entrypoint.py` | Entrypoint: calls `detect_gpu()`, `select_tier()`, `build_vllm_command()`, writes `/app/runtime/vllm-hardware.json`, and `exec`s `vllm` | Runtime manifest fields or pre-exec startup behavior change |
| [`../ocrserver/autotune/tiers.py`](../ocrserver/autotune/tiers.py) | PyTorch GPU probe, memory/concurrency selection, `DEFAULT_MODEL_REVISION`, and `build_vllm_command()` | Hardware thresholds, vLLM CLI flags, or model revision pins change |
| [`../Dockerfile.vllm`](../Dockerfile.vllm) | Derived security layer over `vllm/vllm-openai` | Base vLLM image digest or security remediation pins change |
| [`../requirements/vllm-security.txt`](../requirements/vllm-security.txt) | Wheel pins applied in `Dockerfile.vllm` | Upstream vLLM wheel metadata conflicts or advisories change |
| [`../tests/test_autotune.py`](../tests/test_autotune.py), [`../tests/test_autotune_validation.py`](../tests/test_autotune_validation.py) | Offline unit tests for GPU detection, tier selection, command construction, and manifest permissions | Any change to `vllm/` or `tiers.py` |

## Interface reference

`stage1_entrypoint.py` exposes:

- `select_and_build(env, *, write_manifest=False) -> list[str]`: pure-friendly
  planner returning the `vllm serve` argument vector for `env`.
- `main() -> int`: writes `/app/runtime/vllm-hardware.json` (`schema_version: 1`,
  `gpu`, `tier`, `model`, `vllm_version`, `command`, `model_runner_v2`) and replaces the current
  process with `vllm serve`.

## Configuration

Configured via `.env` and `compose.yml` (Sections 02 and 09):

| Variable | Default | Effect |
|---|---|---|
| `OCR_GPU_DEVICE` | empty → `0` | Docker/NVIDIA index of the one GPU Compose reserves for `vllm-paddleocr` and `ocr-api`; it is device 0 inside both containers. Launcher override: `--gpu-device` / `-GpuDevice` |
| `OCR_VLLM_IMAGE` | `ocr-vllm:0.32.0-security` | Image tag run by `vllm-paddleocr` |
| `OCR_VLLM_PORT` | `8080` (internal) | Container listener port (`OCR_VLLM_DOCS_PORT=8081` on host loopback) |
| `OCR_VLLM_SERVED_MODEL_NAME` | empty → `PaddleOCR-VL-0.9B` | `--served-model-name` alias expected by `ocr-api` |
| `OCR_VLLM_MODEL_REVISION` | `c5630abae1d940eafe0697512a0325494b02ab42` | Immutable Hugging Face revision passed to `--revision` |
| `OCR_VLLM_GPU_MEM_UTIL` | empty (hardware-selected) | Override `--gpu-memory-utilization` (`(0, 1]`), checked against known free VRAM and estimated minimum footprint |
| `OCR_VLLM_MAX_NUM_SEQS` | empty (tier-selected) | Override `--max-num-seqs` |
| `OCR_VLLM_MAX_NUM_BATCHED_TOKENS` | empty → `16384` | Positive integer `--max-num-batched-tokens` |
| `OCR_VLLM_MAX_MODEL_LEN` | `16384` | `--max-model-len` context length |
| `OCR_VLLM_ENFORCE_EAGER` | empty → `false` | Pass `--enforce-eager` when true |
| `OCR_VLLM_MM_PROCESSOR_CACHE_GB` | empty → `0` | Nonnegative integer processor-cache GiB |
| `OCR_VLLM_USE_V2_MODEL_RUNNER` | `0` | Compose passes this to `VLLM_USE_V2_MODEL_RUNNER`; keep 0 for WSL compatibility |
| `OCR_VLLM_HARDWARE_FILE` | `/app/runtime/vllm-hardware.json` | Path for the shared Stage-1 hardware manifest |

## Behaviour contracts

- Temporary manifest files are fsynced and `chmod`ded to `0o644` before atomic
  `os.replace`, because `vllm-paddleocr` runs as root while `ocr-api` reads the
  shared volume as unprivileged UID `1000`.
- A manifest write failure logs a warning and does not block `vllm serve` from
  starting; `ocr-api` falls back to its own GPU probe if the file is absent.
- Stage 2 fingerprints the command digest and runner selection as well as the
  tier and effective measurement settings. Changed engine flags invalidate reuse.
- The published host port (`127.0.0.1:${OCR_VLLM_DOCS_PORT:-8081}`) exposes vLLM's
  OpenAI-compatible API and `/metrics` without bearer authentication; keep it
  bound to loopback.

## Capabilities and limits

**You can:** select initial flags from NVIDIA GPU capacity and free memory,
override individual flags via `.env`, and inspect the selected
tier in `/app/runtime/vllm-hardware.json` or `GET /health`.

**You cannot:** run `vllm-paddleocr` without an NVIDIA GPU and container runtime,
spread one stack across several GPUs (it reserves only `OCR_GPU_DEVICE`, shared
with `ocr-api`), or rely on `OCR_SERVER_API_TOKENS` to authenticate direct host requests to port
`8081`.

## Recipes

**Run offline unit tests for Stage-1 tier selection and command building:**

```powershell
python -m pytest tests/test_autotune.py tests/test_autotune_validation.py -q
```

**Inspect the generated vLLM command in a running stack:**

```powershell
pwsh .\stack.ps1 logs -Service vllm-paddleocr -Tail 50
```

## Verification

```powershell
python -m pytest tests/test_autotune.py tests/test_autotune_validation.py tests/test_dependency_inventory.py -q
docker build --check -f Dockerfile.vllm .
```

## Failure modes

| Symptom | Next check |
|---|---|
| `vllm-paddleocr` exits during startup | Inspect `stack.ps1 logs -Service vllm-paddleocr`: check free VRAM, `OCR_VLLM_GPU_MEM_UTIL` and `OCR_VLLM_ENFORCE_EAGER=true`; memory selection is an estimate, not an OOM guarantee |
| `ocr-api` cannot read `vllm-hardware.json` | Verify `ocr-runtime-profile` volume mount and `0o644` file mode in `stage1_entrypoint.py` |
| Dependency inventory test fails after changing `Dockerfile.vllm` | Refresh `requirements/locks/vllm-py312-linux.txt` and `requirements/inventory.json` |

## Invariants

- `MODEL = "PaddlePaddle/PaddleOCR-VL-1.6"` and `DEFAULT_MODEL_REVISION` remain
  pinned and synchronized across `vllm/stage1_entrypoint.py`,
  `ocrserver/autotune/tiers.py`, `compose.yml`, and `.env.example`.
- `Dockerfile.vllm` remains digest-pinned and verified against
  `requirements/inventory.json` by `tests/test_dependency_inventory.py`.

## Extending it

When adding a vLLM flag or changing tier thresholds, update
`ocrserver/autotune/tiers.py`, document any new `OCR_VLLM_*` env var in
`.env.example` and `guide/environment.html` (plus MCP copy), and add unit tests
in `tests/test_autotune.py` and `tests/test_autotune_validation.py`.

## Related documents

- [Core API and autotuner reference](../ocrserver/README.md)
- [Startup calibration fixtures](../calibration/README.md)
- [Dependency and image inventories](../requirements/README.md)
- [Launcher reference](../scripts/README.md)
