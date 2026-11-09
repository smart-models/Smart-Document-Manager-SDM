"""Pure Docker launch planner shared by PowerShell and Bash; no file or Docker IO."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Mapping

HARDWARE_PROFILES: tuple[str, ...] = ()  # This application requires NVIDIA GPU.
DEFAULT_WAIT_TIMEOUT = "3600"
COMMANDS = {"up", "verify", "config", "build", "pull", "restart", "urls"}
# Merged after compose.yml in archive-only mode: parks vllm-paddleocr under an
# unused profile and strips the API's vLLM dependency and GPU reservation.
ARCHIVE_ONLY_OVERLAY = "compose.archive-only.yml"


class ConfigurationError(ValueError):
    """Invalid launcher configuration; messages must never include credentials."""


@dataclass
class Step:
    """One external command and its human-readable dry-run description."""

    argv: list[str]
    description: str


@dataclass
class Plan:
    """Ordered execution plan and user-facing messages produced for a CLI call."""

    steps: list[Step] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    resolved: dict[str, str] = field(default_factory=dict)


def parse_input(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Read resolved environment and --cli-* intent from stdin, never argv."""
    environment, cli = {}, {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            if key.startswith("--cli-"):
                cli[key[6:]] = value
            else:
                environment[key] = value
    return environment, cli


def boolean(value: str, default: bool, name: str) -> bool:
    """Match the boolean spellings accepted by the API without echoing input."""
    normalized = value.strip().lower()
    if not normalized:
        return default
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


def sibling(path: str, name: str) -> str:
    """Return `name` beside `path`, keeping the caller's own separator style."""
    cut = max(path.rfind("/"), path.rfind("\\")) + 1
    return path[:cut] + name


def switch(cli: Mapping[str, str], name: str, fallback: bool) -> bool:
    """Resolve an opposed --<name> / --no-<name> CLI flag pair."""
    positive = boolean(cli.get(f"flag-{name}", ""), False, name)
    negative = boolean(cli.get(f"flag-no-{name}", ""), False, name)
    if positive and negative:
        raise ConfigurationError(f"--{name} and --no-{name} cannot be combined.")
    return positive or (fallback and not negative)


def build_plan(environment: Mapping[str, str], cli: Mapping[str, str]) -> Plan:
    """Resolve intent into ordered steps, preserving Compose's resolved precedence."""
    command = cli.get("command", "").lower()
    if command not in COMMANDS:
        raise ConfigurationError("Unknown planner command.")
    if cli.get("hardware"):
        raise ConfigurationError(
            "This stack has no CPU/GPU profile positional; NVIDIA GPU is required."
        )
    profiles = {
        value.strip()
        for value in environment.get("COMPOSE_PROFILES", "").split(",")
        if value.strip()
    }
    if profiles - {"storage"}:
        raise ConfigurationError(
            "COMPOSE_PROFILES only supports storage for this application."
        )
    storage = "storage" in profiles
    mcp_override = any(
        boolean(cli.get(key, ""), False, key) for key in ("flag-mcp", "flag-no-mcp")
    )
    mcp = (
        switch(cli, "mcp", True)
        if mcp_override
        else boolean(
            environment.get("MCP_SERVER_ENABLED", ""), True, "MCP_SERVER_ENABLED"
        )
    )
    # The CLI flag only switches archive-only on; OCR_ARCHIVE_ONLY in the env
    # file makes it the configured default. Archives live in S3, so the mode
    # is pointless without the writer and the browser role; refuse before any
    # container changes instead of enabling storage behind the operator's back.
    archive_only = boolean(
        cli.get("flag-archive-only", ""), False, "archive-only"
    ) or boolean(environment.get("OCR_ARCHIVE_ONLY", ""), False, "OCR_ARCHIVE_ONLY")
    if archive_only and not all(
        boolean(environment.get(key, ""), False, key)
        for key in ("OCR_STORAGE_ENABLED", "OCR_STORAGE_BROWSER_ENABLED")
    ):
        raise ConfigurationError(
            "Archive-only mode needs OCR_STORAGE_ENABLED=true and "
            "OCR_STORAGE_BROWSER_ENABLED=true: it only serves stored archives. "
            "Run python scripts/configure_storage.py first."
        )
    wait_timeout = cli.get("wait-timeout", "") or DEFAULT_WAIT_TIMEOUT
    if (
        not wait_timeout.isascii()
        or not wait_timeout.isdigit()
        or int(wait_timeout) < 1
    ):
        raise ConfigurationError("--wait-timeout must be a positive integer.")
    wait = boolean(cli.get("wait", ""), False, "wait") or bool(cli.get("wait-timeout"))
    no_build = boolean(cli.get("no-build", ""), False, "no-build")
    result = Plan(
        resolved={
            "hardware": "",
            "archive_only": str(archive_only).lower(),
            "mcp_server_enabled": str(mcp).lower(),
            "storage_enabled": str(storage).lower(),
            "wait_timeout": wait_timeout,
        }
    )
    if cli.get("flag-archive-only") == "true":
        result.environment["OCR_ARCHIVE_ONLY"] = "true"
    if cli.get("flag-frontend") == "true" or cli.get("flag-no-frontend") == "true":
        if command not in {"up", "config"}:
            raise ConfigurationError("Frontend overrides support up and config only.")
        frontend = switch(cli, "frontend", True)
        result.environment["EXPOSE_FRONTEND"] = str(frontend).lower()
        result.resolved["expose_frontend"] = str(frontend).lower()
    if archive_only and cli.get("gpu-device"):
        raise ConfigurationError(
            "--gpu-device has no effect in archive-only mode: no service reserves "
            "a GPU."
        )
    if command in {"up", "config"} and archive_only:
        result.resolved["gpu_device"] = ""
        if command == "up":
            result.messages.append(
                "Archive-only mode: vllm-paddleocr stays stopped, no GPU is "
                "reserved and OCR inference is disabled; stored archives stay "
                "readable."
            )
    elif command in {"up", "config"}:
        # One index from Docker/NVIDIA numbering; both GPU services reserve it.
        gpu = cli.get("gpu-device", "") or environment.get("OCR_GPU_DEVICE", "") or "0"
        if not re.fullmatch(r"0|[1-9][0-9]*", gpu):
            raise ConfigurationError(
                "--gpu-device and OCR_GPU_DEVICE must be one GPU index, such as 1."
            )
        if cli.get("gpu-device"):
            result.environment["OCR_GPU_DEVICE"] = gpu
        result.resolved["gpu_device"] = gpu
        if command == "up":
            result.messages.append(f"NVIDIA GPU {gpu} runs vllm-paddleocr and ocr-api.")
    elif cli.get("gpu-device"):
        raise ConfigurationError("--gpu-device supports up and config only.")

    base = ["docker", "compose"]
    if cli.get("project-name"):
        base += ["--project-name", cli["project-name"]]
        result.warnings.append(
            "Project names do not isolate the fixed container, volume and "
            "network names; concurrent stacks are unsupported."
        )
    compose_file = cli.get("compose-file", "compose.yml")
    base += ["--file", compose_file]
    if archive_only:
        base += ["--file", sibling(compose_file, ARCHIVE_ONLY_OVERLAY)]
    base += ["--env-file", cli.get("env-file", ".env")]
    if storage:
        base += ["--profile", "storage"]
    built = ["ocr-api"] + (["ocr-mcp"] if mcp else [])
    model_image = environment.get("OCR_VLLM_IMAGE", "").strip()
    # Archive-only never builds, pulls or starts the model server.
    build_model = not archive_only and (
        not model_image or model_image == "ocr-vllm:0.32.0-security"
    )
    if not archive_only and not build_model:
        result.warnings.append(
            "Custom vLLM image selected; the default security image and its "
            "audit do not cover this override."
        )
    infrastructure = ["seaweedfs", "storage-gateway"] if storage else []
    model = [] if archive_only else ["vllm-paddleocr"]
    running = [*model, *infrastructure, *built]

    def step(description: str, *arguments: str) -> None:
        result.steps.append(Step([*base, *arguments], description))

    def start(description: str, services: list[str], healthy: bool) -> None:
        arguments = ["up", "--detach", "--no-build", "--no-deps"]
        if healthy:
            arguments += ["--wait", "--wait-timeout", wait_timeout]
        step(description, *arguments, *services)

    if command in {"up", "build"} and not (command == "up" and no_build):
        step(
            "build application images before changing running containers",
            "build",
            *(["vllm-paddleocr"] if build_model else []),
            *built,
        )
    if command == "up":
        if not mcp:
            step(
                "stop and remove any previously enabled MCP service",
                "rm",
                "--stop",
                "--force",
                "ocr-mcp",
            )
        if archive_only:
            # Entering archive-only must free the GPU: a model server left over
            # from a normal run is stopped and removed; its cache volume stays.
            step(
                "stop and remove a running vLLM container; keep its model cache",
                "rm",
                "--stop",
                "--force",
                "vllm-paddleocr",
            )
        else:
            start("start vLLM and wait for model readiness", ["vllm-paddleocr"], True)
        if storage:
            # Provisioning must finish before the first API write to the archive.
            # A failed run aborts execution; it is never hidden by detached up.
            step(
                "start storage and wait for readiness",
                "up",
                "--detach",
                "--no-build",
                "--wait",
                "--wait-timeout",
                wait_timeout,
                *infrastructure,
            )
            step(
                "provision storage identities, bucket and retention",
                "run",
                "--rm",
                "--no-deps",
                "storage-init",
            )
        start("start OCR API and wait for calibration/readiness", ["ocr-api"], True)
        if mcp:
            start("start MCP after the OCR API is healthy", ["ocr-mcp"], wait)
        result.messages.append(
            f"Readiness has a finite {wait_timeout}s budget per stage; "
            + (
                "archive-only startup skips model loading and calibration."
                if archive_only
                else "a cold GPU/model calibration can take tens of minutes."
            )
        )
    elif command == "pull":
        if archive_only:
            result.messages.append(
                "Archive-only mode: the vLLM image is neither built nor pulled."
            )
        if build_model:
            step(
                "refresh the pinned vLLM base and build its security layer",
                "build",
                "--pull",
                "vllm-paddleocr",
            )
        registry = ([] if build_model else model) + infrastructure
        if registry:
            step("pull registry images; propagate download failures", "pull", *registry)
    elif command == "restart":
        step(
            "restart existing services without applying .env changes",
            "restart",
            *running,
        )
        result.messages.append(
            "restart retains container configuration; use up to apply .env changes."
        )
    elif command == "config":
        step(
            "render resolved Compose configuration (redacted by default)",
            "config",
            "--format",
            "json",
        )

    if command in {"up", "urls"}:
        describe_endpoints(environment, result, mcp, storage, archive_only)
    return result


def describe_endpoints(
    environment: Mapping[str, str],
    plan: Plan,
    mcp: bool,
    storage: bool,
    archive_only: bool = False,
) -> None:
    """Describe actual fixed loopback publications without disclosing credentials."""

    def endpoint(
        label: str, variable: str, default: int, suffix: str, auth: str
    ) -> None:
        value = environment.get(variable, "").strip() or str(default)
        if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 65535:
            raise ConfigurationError(f"{variable} must be a valid port.")
        plan.summary.append(f"  {label}: http://127.0.0.1:{value}{suffix} -- {auth}")

    plan.summary.append(
        "Endpoints configured on host loopback (not a live health check):"
    )
    if archive_only:
        plan.summary.append(
            "  Mode: archive-only -- OCR inference disabled; stored archives only."
        )
    api_auth = (
        "bearer required for data endpoints"
        if environment.get("OCR_SERVER_API_TOKENS")
        else "data endpoints without authentication"
    )
    endpoint("OCR API / console", "OCR_SERVER_PORT", 8020, "", api_auth)
    if (
        boolean(
            plan.environment.get(
                "EXPOSE_FRONTEND", environment.get("EXPOSE_FRONTEND", "")
            ),
            True,
            "EXPOSE_FRONTEND",
        )
        is False
    ):
        plan.summary.append("  Frontend console: disabled.")
    if boolean(environment.get("EXPOSE_OCR_SWAGGER", ""), True, "EXPOSE_OCR_SWAGGER"):
        endpoint("OCR Swagger", "OCR_SERVER_PORT", 8020, "/docs", "public")
    if archive_only:
        plan.summary.append("  vLLM API / Swagger: not started (archive-only).")
    else:
        endpoint(
            "vLLM API / Swagger",
            "OCR_VLLM_DOCS_PORT",
            8081,
            "/docs",
            "without authentication",
        )
    if mcp:
        state = (
            "bearer required"
            if environment.get("MCP_API_TOKENS")
            else "missing MCP tokens; startup will be refused"
        )
        endpoint("MCP", "MCP_PORT", 8021, "/mcp", state)
    else:
        plan.summary.append("  MCP: disabled (MCP_SERVER_ENABLED=false).")
    if storage:
        endpoint("S3", "OCR_S3_PORT", 8333, "", "S3 credentials required")
        endpoint(
            "Storage console",
            "OCR_STORAGE_CONSOLE_PORT",
            23646,
            "",
            "console credentials required",
        )


def render_shell(plan: Plan) -> str:
    """Render the same plan as JSON into Bash read records without eval or jq."""
    lines = []

    def record(tag: str, value: str) -> None:
        if "\n" in value or "\r" in value:
            raise ConfigurationError("A resolved plan value contains a line break.")
        lines.append(f"{tag} {value}")

    for tag, mapping in (("env", plan.environment), ("resolved", plan.resolved)):
        for name, value in mapping.items():
            record(tag, f"{name}={value}")
    for tag, values in (
        ("message", plan.messages),
        ("warning", plan.warnings),
        ("summary", plan.summary),
    ):
        for value in values:
            record(tag, value)
    for step in plan.steps:
        record("step", step.description)
        for argument in step.argv:
            record("arg", argument)
    return "\n".join(lines) + "\n"


def main() -> int:
    """Run the CLI planner or JSON secret redactor from stdin."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(newline="\n")
    raw = sys.stdin.read()
    if "--redact-config" in sys.argv:
        try:
            print(json.dumps(redact_config(json.loads(raw)), indent=2))
        except (ValueError, TypeError):
            print(
                "[ERROR] Cannot render Compose configuration; "
                "sensitive output withheld.",
                file=sys.stderr,
            )
            return 2
        return 0
    environment, cli = parse_input(raw)
    try:
        plan = build_plan(environment, cli)
        # Validate the common representation even when PowerShell requests JSON.
        shell = render_shell(plan)
    except ConfigurationError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 2
    print(shell, end="") if "--format=sh" in sys.argv else print(
        json.dumps(asdict(plan))
    )
    return 0


def redact_config(value: object) -> object:
    """Redact structured JSON, including multiline scalars and environment lists."""
    secret = re.compile(
        r"(?:^|_)(TOKEN|TOKENS|SECRET|PASSWORD|KEY|ACCESS_KEY_ID)$", re.IGNORECASE
    )
    if isinstance(value, dict):
        return {
            key: "<redacted>"
            if secret.search(key) and item not in (None, "")
            else redact_config(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str) and "=" in item:
                key, _, content = item.partition("=")
                result.append(
                    f"{key}=<redacted>" if secret.search(key) and content else item
                )
            else:
                result.append(redact_config(item))
        return result
    return value


if __name__ == "__main__":
    raise SystemExit(main())
