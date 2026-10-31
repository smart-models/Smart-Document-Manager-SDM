"""Check image audit inventories against their provenance and direct runtime pins."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
import tomllib

from scripts.security_constraints import canonical_name, parse_exact_pins

ROOT = Path(__file__).resolve().parents[1]
EXACT = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^;\s]+)$")


def direct_pins(lines: list[str]) -> dict[str, str]:
    """Read exact Linux runtime pins; reject unknown unpinned requirements."""
    pins = {}
    for raw in lines:
        line = raw.partition("#")[0].strip()
        if not line or line.startswith("-e "):
            continue
        # pywin32 is an explicit Windows-only dependency of the MCP package.
        if line.startswith("pywin32>=") and "sys_platform == 'win32'" in line:
            continue
        match = EXACT.fullmatch(line)
        if not match:
            raise ValueError("Runtime dependencies must use exact, supported pins")
        pins[canonical_name(match[1])] = match[2]
    return pins


def inventory_errors(root: Path) -> list[str]:
    """Return stale, missing or mismatched dependency inventory diagnostics."""
    errors = []
    manifest = json.loads((root / "requirements/inventory.json").read_text("utf-8"))
    if set(manifest) != {"app", "mcp", "vllm"}:
        return ["Dependency inventories must cover app, mcp and upstream vllm"]
    project = tomllib.loads((root / "ocr-mcp-server/pyproject.toml").read_text("utf-8"))
    direct = {
        "app": direct_pins(
            (root / "requirements/api.txt").read_text("utf-8").splitlines()
        ),
        "mcp": direct_pins(project["project"]["dependencies"]),
        "vllm": direct_pins(
            (root / "requirements/vllm-security.txt").read_text("utf-8").splitlines()
        ),
    }
    expected_locks = set()
    for target, metadata in manifest.items():
        name = metadata["lock"]
        if Path(name).name != name or not re.fullmatch(
            r"[a-z]+-py\d+-linux\.txt", name
        ):
            errors.append(f"Invalid inventory filename for {target}")
            continue
        path = root / "requirements/locks" / name
        expected_locks.add(name)
        pins = parse_exact_pins(path)
        if not pins:
            errors.append(f"Empty inventory: {name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != metadata["sha256"]:
            errors.append(f"Inventory hash mismatch: {name}; regenerate from the image")
        for package, version in direct.get(target, {}).items():
            if pins.get(package, (None, None))[1] != version:
                errors.append(f"{name}: direct pin differs for {package}=={version}")
        if target == "vllm":
            compose = (root / "compose.yml").read_text("utf-8")
            if f"${{OCR_VLLM_IMAGE:-{metadata['image']}}}" not in compose:
                errors.append("vLLM inventory differs from the default Compose image")
            dockerfile = (root / "Dockerfile.vllm").read_text("utf-8")
            if f"FROM {metadata['base_image']}\n" not in dockerfile:
                errors.append("vLLM base image differs from the captured inventory")
    actual = {path.name for path in (root / "requirements/locks").glob("*.txt")}
    if actual != expected_locks:
        errors.append("Inventory manifest and lock file list differ")
    return errors


def main() -> int:
    """Validate committed dependency inventories and return a CLI exit code."""
    try:
        errors = inventory_errors(ROOT)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"[ERROR] Cannot validate dependency inventories: {exc}", file=sys.stderr)
        return 2
    for error in errors:
        print(f"[ERROR] {error}", file=sys.stderr)
    if not errors:
        print("Dependency inventories: OK (direct pins, hashes and vLLM base image)")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
