"""Check exact security policy pins against captured image inventories.

The existing Dockerfiles consume their own dependency manifests, not these audit
snapshots. Rebuild and capture an image after changing dependency versions.
The generic --write utility changes pins only; the inventory hash check refuses
such edits until a real image snapshot and its provenance are regenerated.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONSTRAINTS = REPOSITORY_ROOT / "requirements" / "security-constraints.txt"
DEFAULT_LOCKS_DIR = REPOSITORY_ROOT / "requirements" / "locks"
NAME_NORMALIZER = re.compile(r"[-_.]+")


def canonical_name(value: str) -> str:
    """Return a PEP 503-style normalized package name."""
    return NAME_NORMALIZER.sub("-", value).lower()


def parse_exact_pins(path: Path) -> dict[str, tuple[str, str]]:
    """Return normalized name to original name/version for exact pins."""
    pins: dict[str, tuple[str, str]] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.partition("#")[0].strip()
        if not line:
            continue
        name, separator, version = line.partition("==")
        if not separator or not name.strip() or not version.strip():
            raise ValueError(f"{path}:{line_number} is not an exact name==version pin")
        normalized = canonical_name(name.strip())
        if normalized in pins:
            raise ValueError(f"{path}:{line_number} duplicates package {name.strip()}")
        pins[normalized] = (name.strip(), version.strip())
    return pins


def synchronized_lock_text(lock: Path, overrides: dict[str, tuple[str, str]]) -> str:
    """Return ``lock`` with every applicable security version substituted."""
    output: list[str] = []
    for raw_line in lock.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        name, separator, _version = line.partition("==")
        normalized = canonical_name(name.strip()) if separator else ""
        if normalized in overrides:
            original_name, secure_version = overrides[normalized]
            output.append(f"{original_name}=={secure_version}")
        else:
            output.append(raw_line)
    return "\n".join(output) + "\n"


def security_constraint_errors(
    constraints: Path, locks: list[Path]
) -> tuple[list[str], dict[Path, str]]:
    """Return validation errors and synchronized text for changed locks."""
    overrides = parse_exact_pins(constraints)
    seen = {name: set() for name in overrides}
    errors: list[str] = []
    changed: dict[Path, str] = {}
    for lock in locks:
        pins = parse_exact_pins(lock)
        for name, (_display_name, required_version) in overrides.items():
            if name not in pins:
                continue
            seen[name].add(lock.name)
            actual_version = pins[name][1]
            if actual_version != required_version:
                errors.append(
                    f"{lock.name}: {name}=={actual_version} does not satisfy "
                    f"security constraint =={required_version}"
                )
        candidate = synchronized_lock_text(lock, overrides)
        if candidate != lock.read_text(encoding="utf-8"):
            changed[lock] = candidate
    for name, lock_names in seen.items():
        if not lock_names:
            errors.append(
                f"security constraint {name} is stale: no target lock contains it"
            )
    return errors, changed


def main(argv: list[str] | None = None) -> int:
    """Check security pins, or synchronize them before rebuilding images."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, default=DEFAULT_CONSTRAINTS)
    parser.add_argument("--locks-dir", type=Path, default=DEFAULT_LOCKS_DIR)
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite applicable lock pins; default is a read-only check",
    )
    args = parser.parse_args(argv)
    try:
        locks = sorted(args.locks_dir.glob("*.txt"))
        if not locks:
            raise ValueError(f"no lock files found in {args.locks_dir}")
        errors, changed = security_constraint_errors(args.constraints, locks)
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    if args.write:
        for lock, text in changed.items():
            lock.write_text(text, encoding="utf-8")
            print(f"[UPDATED] {lock.name}")
        # Re-evaluate after the write rather than assuming it succeeded.
        errors, changed = security_constraint_errors(args.constraints, locks)
    if errors:
        for error in errors:
            print(f"[ERROR] {error}", file=sys.stderr)
        if not args.write and changed:
            print(
                "Update the image dependencies, rebuild, "
                "then regenerate its inventory.",
                file=sys.stderr,
            )
        return 1
    print(f"Security constraints verified across {len(locks)} audit inventory file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
