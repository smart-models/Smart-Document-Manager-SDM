# scripts — Docker stack launchers and operational checks

> **Read this when** starting, verifying, inspecting, recovering or stopping the Smart Document Manager stack.
> **Audience** — operators and agents maintaining the Docker launch workflow.
> **Source of truth** — this file for CLI commands and flags; [`.env.example`](../.env.example) for settings; [`compose.yml`](../compose.yml) for containers.
> **Prerequisite** — [project requirements](../README.md) and [environment template](../.env.example).

---

## Purpose and boundaries

`stack.ps1` and `stack.sh` provide the same Docker workflow on Windows and
Linux/macOS. They perform cross-service checks before startup and order storage
initialization and OCR readiness. They do not install Docker, NVIDIA drivers,
Python or dependencies, modify `.env`, rotate credentials, or download models
outside the configured containers.

Install Docker Compose **2.24.4 or newer** (the workflow uses
`config --environment` and health waits), Python **3.11 or newer**, and the
NVIDIA container runtime described in the root README. The launcher Python
code uses only the standard library. Use PowerShell 5.1+ or PowerShell 7 on
Windows; use Bash on Linux/macOS and **Git Bash**, rather than an unconfigured
WSL stub, when testing the Bash launcher on Windows.

## Mental model

1. The shells resolve shell-versus-file precedence with Compose, send values
   over stdin to one Python planner, and execute the returned argv arrays.
2. Every normal `up` runs the token/storage preflight and Compose security
   checks before building. Builds finish before any running service is changed.
3. Startup waits for vLLM, then local storage when selected, runs `storage-init`
   synchronously, waits for API calibration/readiness, and finally starts MCP.
4. `down`, `logs`, `status`, `version` and `help` bypass Python. Recovery uses
   `.env.example` when no env file was explicitly selected, and includes the
   `storage` profile even if local `.env` is missing or malformed.

## Map of the code

| Path | Role | Change it when |
|---|---|---|
| [`../stack.ps1`](../stack.ps1), [`../stack.sh`](../stack.sh) | Arguments, prerequisites, recovery and execution | Changing shell interfaces or process handling |
| [`stack_core.py`](stack_core.py) | Pure planner and structured config redaction | Changing service selection, startup order or endpoint summaries |
| [`verify_token_config.py`](verify_token_config.py) | API/MCP relationships and storage credentials | Changing supported authentication or storage roles |
| [`verify_compose_security.py`](verify_compose_security.py) | Publication and hardening checks for both storage states | Changing the Compose security boundary |
| [`configure_storage.py`](configure_storage.py) | Explicit local credential generation | Changing storage setup |
| [`../tests/test_stack_launchers.py`](../tests/test_stack_launchers.py) | Real-shell tests against fake Docker | Changing commands, flags, recovery or error handling |

## Interface reference

Run all examples from the repository root. No command accepts a CPU/GPU
positional: the application always requires an NVIDIA GPU. The optional
`storage` profile is selected by `COMPOSE_PROFILES` in `.env`.

| Command | Effect |
|---|---|
| `help` | Print usage; also the result of a bare invocation |
| `version` | Report launcher, Engine and Compose versions; missing tools are reported without failure |
| `verify` | Check tokens/storage and both Compose profile shapes; no daemon required |
| `up` | Verify, build, start in readiness order and report configured URLs |
| `build` | Build vLLM's security layer, `ocr-api` and, when enabled, `ocr-mcp`; start nothing |
| `pull` | Refresh the pinned vLLM base and security layer (or pull a custom image), then selected storage images; failures propagate |
| `restart` | Restart selected existing services; retain their environment and images |
| `config` | Print resolved Compose JSON with credential values redacted |
| `urls` | Print configured endpoints/authentication state; no live health probe |
| `status` | Show all stack containers, including optional storage |
| `logs` | Show container logs; follow by default |
| `down` | Stop/remove containers; keep all named volumes by default |

| Bash | PowerShell | Accepted by | Effect |
|---|---|---|---|
| `--dry-run` | `-DryRun` | All except help/version | Print planned steps; skip verifiers and mutations; still resolve Compose |
| `--env-file <path>` | `-EnvFile <path>` | All except help/version | Select an existing env file; no implicit template fallback |
| `--project-name <name>` | `-ProjectName <name>` | All except help/version | Select Compose project bookkeeping; see the fixed-name limitation below |
| `--no-build` | `-NoBuild` | up | Reuse existing images |
| `--wait` | `-Wait` | up | Also wait for the final MCP healthcheck; upstream stages always wait |
| `--wait-timeout <seconds>` | `-WaitTimeout <seconds>` | up | Positive per-stage readiness budget, default 3600; implies wait |
| `--skip-verify` | `-SkipVerify` | up | Explicitly bypass the security preflight |
| `--mcp` / `--no-mcp` | `-Mcp` / `-NoMcp` | up/config/verify/build/pull/restart/urls | Override `MCP_SERVER_ENABLED`; disabled MCP is stopped/removed during up |
| `--frontend` / `--no-frontend` | `-Frontend` / `-NoFrontend` | up/config | Override the API's `EXPOSE_FRONTEND` setting |
| `--gpu-device <index>` | `-GpuDevice <index>` | up/config | Override `OCR_GPU_DEVICE`: the one Docker/NVIDIA GPU index both `vllm-paddleocr` and `ocr-api` reserve |
| `--show-secrets` | `-ShowSecrets` | config | Print unredacted config, with an explicit stderr warning |
| `--tail <lines>` | `-Tail <lines>` | logs | Positive line count; suppress implicit follow |
| `--follow` | `-Follow` | logs | Explicitly follow logs, including with tail/since |
| `--since <time>` | `-Since <time>` | logs | Pass a Compose timestamp/duration; suppress implicit follow |
| `--service <name>` | `-Service <name>` | logs | Service names; Bash repeatable/comma-separated, PowerShell comma-separated |
| `--volumes` | `-Volumes` | down | Delete named volumes, including models, tuning profiles and stored OCR archives |
| `--remove-orphans` | `-RemoveOrphans` | down | Remove obsolete service containers |
| `--yes` | `-Yes` | down | Confirm volume deletion non-interactively |

Opposing switches are rejected together. Unknown flags and flags used with
the wrong command are rejected before mutations. Volume deletion requires
typing `yes` at a terminal, or the explicit confirmation flag; redirected stdin
without confirmation is refused. A dry run does not ask for destructive confirmation.

`config` continues to show all Compose service definitions, including MCP when
disabled: disabling MCP affects launcher execution, not the Compose model.

## Configuration

CLI flag → shell environment → selected env file → built-in default. For
example, `MCP_SERVER_ENABLED=true` in `.env` and `-NoMcp` for a single run
starts without MCP; later invocations without that override use the configured
default. Copy [`.env.example`](../.env.example) only when `.env` does not exist,
then configure the tokens described in [`.env.example`](../.env.example).

The launchers never create or rewrite `.env`. Local storage activation is an
explicit operation through `python scripts/configure_storage.py`; use that
script's `--env-file` option when working with an alternate env file.

The default `OCR_VLLM_IMAGE=ocr-vllm:0.32.0-security` is built from
`Dockerfile.vllm`. When migrating from the old upstream `v0.26.0` default, replace
that setting with the new default to receive the security updates. A custom image
override is preserved: launchers skip its build and `pull` fetches it from its
registry; its dependencies require their own audit. With a custom override, use
the launchers instead of `docker compose up --build`: Compose treats `image` as a
build output tag and that explicit command would overwrite the local custom tag.

## Behaviour contracts

- vLLM and API readiness waits have a finite budget per stage. Cold starts can
  take tens of minutes. A timeout leaves containers available for diagnosis;
  it does not erase volumes or automatically undo completed provisioning.
- `restart` retains existing container configuration. Use `up` after editing
  `.env`; use `up` with build enabled after source/image changes.
- `config` redacts nonempty credential keys, including multiline and numeric
  values, through a shared JSON redactor. `urls` never prints tokens.
- A failed preflight, build, pull, health wait or storage initialization stops
  subsequent steps. No API startup is issued after storage provisioning fails.
- Disabling the storage profile excludes it from future start/restart plans;
  already running storage containers remain until explicitly stopped or `down`.
- API/MCP/storage containers have explicit non-root users, read-only root
  filesystems, dropped capabilities and no-new-privileges. The named upstream
  **vLLM exception** retains its image's user/writable filesystem for model
  runtime compatibility; all other checks still apply to it.

## Capabilities and limits

### Project names and fixed resources

`--project-name` does **not** create independent stacks or data isolation.
`container_name`, named volume `name:` entries and `smart-document-manager-private` /
`smart-document-manager-storage` network names
are fixed in Compose. A second stack can collide with containers and shares
the globally named volumes. Use one stack per Docker daemon with this layout.

MCP's healthcheck proves its TCP listener is reachable, not authenticated tool
discovery. `verify` checks configuration without contacting model/S3 services.
It checks the actual single API token and MCP list; the API does not accept an
additional service token automatically. Disabling MCP omits MCP auth checks.

## Recipes

For a visual, offline walkthrough, open the [CLI field guide](../guide/cli.html).
The [getting-started guide](../guide/getting-started.html) teaches the first run.

**First start or apply changes** — configure `.env` first:

```powershell
pwsh .\stack.ps1 verify
pwsh .\stack.ps1 up -Wait
```

```bash
bash stack.sh verify
bash stack.sh up --wait
```

**Preview a startup without MCP**, including the resolved service selection:

```powershell
pwsh .\stack.ps1 up -NoMcp -DryRun
```

```bash
bash stack.sh up --no-mcp --dry-run
```

**Run on another GPU of a multi-GPU host** — index as Docker/NVIDIA numbers it,
usually the `nvidia-smi` order; set `OCR_GPU_DEVICE` in `.env` to keep it:

```powershell
pwsh .\stack.ps1 up -GpuDevice 1 -Wait
```

```bash
bash stack.sh up --gpu-device 1 --wait
```

**Inspect readiness and recent logs:**

```powershell
pwsh .\stack.ps1 status
pwsh .\stack.ps1 logs -Service ocr-api,vllm-paddleocr -Tail 100
pwsh .\stack.ps1 urls
```

```bash
bash stack.sh status
bash stack.sh logs --service ocr-api,vllm-paddleocr --tail 100
bash stack.sh urls
```

**Enable local storage** — generate credentials explicitly, then startup runs
the synchronous bucket/identity initializer before the API:

```powershell
python scripts/configure_storage.py
pwsh .\stack.ps1 up -Wait
```

```bash
python3 scripts/configure_storage.py
bash stack.sh up --wait
```

**Stop while retaining model caches and archives:**

```powershell
pwsh .\stack.ps1 down
```

```bash
bash stack.sh down
```

## Verification

```text
python -m pytest tests/test_stack_core.py tests/test_stack_verifiers.py tests/test_stack_launchers.py tests/test_storage_setup.py -q
python -m compileall ocr_server.py autotune_entrypoint.py ocrserver vllm calibration scripts
docker compose config --quiet
```

The shell tests use fake Docker responses and isolated temporary env files,
never local credentials or a GPU. They prove planning, parity, preflight,
redaction and failure handling. They do not prove image builds, NVIDIA access,
OCR quality, IAM provisioning against live storage or cold-start timing.

## Failure modes

| Symptom | Action |
|---|---|
| Docker unavailable or daemon unreachable | Install/start Docker; `version` identifies missing tools. Read-only config/verify/urls and dry runs work without a daemon. |
| Compose too old | Upgrade to the minimum shown by `version`; use Compose v2. |
| Missing Python | Install Python 3.11+ for planned commands. Recovery commands still work. |
| Missing or malformed `.env` | Create it from the template if absent. Expanded parse errors are withheld to avoid printing secrets. Recovery uses the template unless explicitly overridden. |
| Token preflight fails | Follow the named variable diagnostic; the outbound `UPSTREAM_CLIENT_TOKEN` must appear in neither inbound list, and MCP and API inbound credentials must be distinct. |
| Missing storage credentials | Run the explicit storage configurator against the selected file, then retry. |
| Health wait times out | Inspect `status` and logs for download/tuning progress or errors; increase the finite per-stage budget when justified. |
| Volume deletion refused | Use the normal stop to keep data, or explicitly confirm destructive removal as described above. |
| Configuration edits seem ignored | `restart` retains old environment. Recreate with `up`; check shell overrides and `config`. |
| Stack runs on the wrong GPU | Pass `--gpu-device` / `-GpuDevice` to `up`, or set `OCR_GPU_DEVICE`; `docker exec vllm-paddleocr nvidia-smi -L` must list one GPU. A new GPU model recalibrates. |

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | Missing prerequisite: Docker, daemon, Compose version, selected env file |
| 2 | CLI usage/configuration/preflight error |
| Other | Docker process failure propagated unchanged |

PowerShell native parameter-binding failures, such as a missing or duplicate
parameter value, can exit 1 before the script body. Bash reports those as 2.

## Invariants

`tests/test_stack_core.py` pins startup ordering and service selection.
`tests/test_stack_verifiers.py` pins credential and publication boundaries.
`tests/test_stack_launchers.py` pins both shells' argv parity, default redaction,
recovery and destructive confirmation. Keep these synchronized with the flag
table and both help outputs whenever changing the CLI.

## Extending it

Change both shells, the planner when service execution changes, this interface
reference and the relevant tests together. Keep service-specific settings in
`.env.example` and `compose.yml`. Keep recovery out of the planner so a broken
Python/configuration environment cannot prevent shutdown.

## Related documents

| Document | Covers |
|---|---|
| [Project README](../README.md) | Requirements, API, storage and general setup |
| [Environment template](../.env.example) | Token model, file precedence and settings sections |
| [Environment field guide](../guide/environment.html) | Searchable offline parameter reference |
| [MCP README](../ocr-mcp-server/README.md) | MCP installation, native operation and protocol checks |
| [Automation guide](../.github/AGENTS.md) | CI, dependency audit and release evidence |
