"""Check actual OCR Compose publications/hardening without contacting the daemon."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_ONLY_OVERLAY = "compose.archive-only.yml"
MODEL_SERVICE = "vllm-paddleocr"
BASE_SERVICES = {"ocr-api", "ocr-mcp", MODEL_SERVICE}
STORAGE_SERVICES = {"seaweedfs", "storage-gateway", "storage-init"}


def _device_reservations(service: Mapping[str, Any]) -> list:
    """Return the GPU/device reservations a resolved service declares."""
    deploy = service.get("deploy") or {}
    if not isinstance(deploy, dict):
        return [deploy]
    reservations = (deploy.get("resources") or {}).get("reservations") or {}
    return list(reservations.get("devices") or []) if reservations else []


def validate_config(
    config: Mapping[str, Any], *, archive_only: bool = False
) -> list[str]:
    """Enforce the repository boundary, with one explicit upstream vLLM exception.

    The archive-only shape (compose.yml plus compose.archive-only.yml) must
    hold the same hardening and, on top, prove its zero-VRAM promise: no model
    server in the model, no API dependency on it and no device reservation on
    any service.
    """
    services = config.get("services")
    required = BASE_SERVICES - {MODEL_SERVICE} if archive_only else BASE_SERVICES
    if not isinstance(services, dict) or not required <= services.keys():
        return [
            "Resolved Compose must contain OCR API and MCP services"
            + (" (archive-only)." if archive_only else " and vLLM.")
        ]
    errors = []
    if set(services) - BASE_SERVICES - STORAGE_SERVICES:
        errors.append(
            "Unexpected Compose services; update the security inventory "
            "before deployment."
        )
    if archive_only:
        if MODEL_SERVICE in services:
            errors.append("Archive-only Compose must not define vllm-paddleocr.")
        depends = services["ocr-api"].get("depends_on") or {}
        if MODEL_SERVICE in depends:
            errors.append("Archive-only ocr-api must not depend on vllm-paddleocr.")
        for name, service in services.items():
            if isinstance(service, dict) and _device_reservations(service):
                errors.append(f"Archive-only service {name} must not reserve a GPU.")
    if (
        STORAGE_SERVICES.intersection(services)
        and not STORAGE_SERVICES <= services.keys()
    ):
        errors.append("The storage profile must include all three storage services.")
    try:
        mcp_port = int(
            services["ocr-mcp"].get("environment", {}).get("MCP_PORT", "8021")
        )
    except (ValueError, TypeError):
        return ["MCP_PORT must be a valid port."]
    expected = {
        "ocr-api": [8020],
        "ocr-mcp": [mcp_port],
        "vllm-paddleocr": [8080],
        "storage-gateway": [8333, 23646],
    }
    for name, service in services.items():
        if not isinstance(service, dict):
            errors.append(f"Service {name} must be an object.")
            continue
        if service.get("network_mode") == "host" or service.get("privileged"):
            errors.append(
                f"Service {name} must not use host networking or privileged mode."
            )
        if service.get("cap_add") or set(service.get("cap_drop") or []) != {"ALL"}:
            errors.append(f"Service {name} must drop ALL capabilities and add none.")
        security = {
            value.replace("=", ":") for value in service.get("security_opt", [])
        }
        if "no-new-privileges:true" not in security:
            errors.append(f"Service {name} must set no-new-privileges:true.")
        # Upstream vLLM currently writes under /root and reports hardware into a
        # shared volume. Non-root/read-only operation needs separate GPU testing.
        if name != "vllm-paddleocr":
            user = str(service.get("user", "")).split(":")[0]
            if user in {"", "0", "root"} or service.get("read_only") is not True:
                errors.append(
                    f"Service {name} must use an explicit non-root user and "
                    "read-only root filesystem."
                )
        targets = []
        for port in service.get("ports", []):
            if not isinstance(port, dict):
                errors.append(f"Service {name} has an unresolved port publication.")
                continue
            try:
                if not ipaddress.ip_address(port.get("host_ip", "")).is_loopback:
                    raise ValueError
            except ValueError:
                errors.append(f"Service {name} must publish only on host loopback.")
            try:
                target, published = int(port["target"]), int(port["published"])
                if not (1 <= target <= 65535 and 1 <= published <= 65535):
                    raise ValueError
                targets.append(target)
            except (KeyError, ValueError, TypeError):
                errors.append(f"Service {name} has an invalid published port.")
        if sorted(targets) != sorted(expected.get(name, [])):
            errors.append(
                f"Service {name} has missing, duplicate or unexpected published ports."
            )
    return errors


def main() -> int:
    """Resolve both Compose profile states and validate security invariants."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--compose-file", type=Path, default=ROOT / "compose.yml")
    parser.add_argument("--project-name")
    args = parser.parse_args()
    errors = []
    # Every supported shape must preserve the boundary, even when the operator
    # is currently starting only the base application: both profile states of
    # the standard stack and both of the archive-only overlay.
    shapes = [
        (archive_only, storage)
        for archive_only in (False, True)
        for storage in (False, True)
    ]
    for archive_only, storage in shapes:
        command = ["docker", "compose", "--file", str(args.compose_file)]
        if archive_only:
            overlay = args.compose_file.with_name(ARCHIVE_ONLY_OVERLAY)
            command += ["--file", str(overlay)]
        command += ["--env-file", str(args.env_file)]
        if args.project_name:
            command += ["--project-name", args.project_name]
        if storage:
            command += ["--profile", "storage"]
        command += ["config", "--format", "json"]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env={**os.environ, "COMPOSE_PROFILES": ""},
                timeout=60,
            )
            if result.returncode:
                errors.append(
                    "Cannot resolve Compose security configuration; "
                    "expanded output withheld."
                )
                continue
            errors.extend(
                validate_config(json.loads(result.stdout), archive_only=archive_only)
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            errors.append(
                "Cannot inspect Compose security configuration; "
                "sensitive output withheld."
            )
    for error in dict.fromkeys(errors):
        print(f"[ERROR] {error}", file=sys.stderr)
    if not errors:
        print(
            "Compose security: OK (base, storage and archive-only shapes; "
            "named vLLM user/filesystem exception)."
        )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
