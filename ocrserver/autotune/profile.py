"""Autotune profile persistence and validation."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ocrserver.autotune.fingerprint import fingerprint_digest, fingerprints_match
from ocrserver.autotune.gates import finite_number
from ocrserver.autotune.tiers import _env_bool, _env_int

PROFILE_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class AutotuneConfig:
    """Store validated calibration mode, objective, deadline and profile policy."""

    mode: str = "quick"
    objective: str = "balanced"
    profile_path: str = ""
    max_seconds: int = 600
    force: bool = False

    def __post_init__(self) -> None:
        """Reject unsupported modes, objectives and nonpositive integer deadlines."""
        if self.mode not in {"off", "quick", "full"}:
            raise ValueError("OCR_AUTOTUNE must be off, quick or full")
        if self.objective not in {"throughput", "latency", "balanced"}:
            raise ValueError(
                "OCR_TUNING_OBJECTIVE must be throughput, latency or balanced"
            )
        if type(self.max_seconds) is not int or self.max_seconds <= 0:
            raise ValueError("OCR_AUTOTUNE_MAX_SECONDS must be a positive integer")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AutotuneConfig:
        """Parse explicit environment settings and mode-dependent time budgets."""
        mode = env.get("OCR_AUTOTUNE", "quick").strip().lower() or "quick"
        objective = (
            env.get("OCR_TUNING_OBJECTIVE", "balanced").strip().lower() or "balanced"
        )
        profile_path = env.get("OCR_AUTOTUNE_PROFILE", "").strip()
        force = _env_bool(env, "OCR_AUTOTUNE_FORCE") or False
        max_seconds = _env_int(env, "OCR_AUTOTUNE_MAX_SECONDS")
        if max_seconds is None:
            max_seconds = 1200 if mode == "full" else 600
        return cls(
            mode=mode,
            objective=objective,
            profile_path=profile_path,
            max_seconds=max_seconds,
            force=force,
        )


def make_profile(
    fingerprint: dict,
    decisions: dict,
    measurements: list,
    warmup_seconds: float,
    mode: str,
    objective: str,
) -> dict:
    """Build a timestamped profile containing measurements and fingerprint hash."""
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "fingerprint": fingerprint,
        "fingerprint_sha256": fingerprint_digest(fingerprint),
        "mode": mode,
        "objective": objective,
        "decisions": decisions,
        "measurements": measurements,
        "warmup_seconds": warmup_seconds,
    }


def load_profile(path: Path) -> dict | None:
    """Return a validated profile or None for absent, unreadable or invalid data."""
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
        return profile if _valid_profile(profile) else None
    except (OSError, ValueError):
        return None


def save_profile(path: Path, profile: dict) -> None:
    """Validate and atomically replace a profile through a flushed temporary file.

    Create the parent directory when necessary. Remove temporary files on
    failure; invalid profiles raise ValueError before any filesystem write.
    """
    if not _valid_profile(profile):
        raise ValueError("Cannot persist an invalid autotune profile")
    payload = json.dumps(profile, sort_keys=True, allow_nan=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _finite_tree(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _finite_tree(item) for key, item in value.items()
        )
    if isinstance(value, list):
        return all(_finite_tree(item) for item in value)
    return value is None or isinstance(value, (str, int, bool))


def _valid_decisions(value: object) -> bool:
    return (
        isinstance(value, dict)
        and type(value.get("vl_lanes")) is int
        and value["vl_lanes"] > 0
        and type(value.get("page_parallelism")) is int
        and value["page_parallelism"] == 1
        and (
            value.get("layout_threads") is None
            or (type(value["layout_threads"]) is int and value["layout_threads"] > 0)
        )
    )


def _valid_profile(profile: object) -> bool:
    """Validate cached evidence before any decisions may influence API settings.

    Require finite values, a matching fingerprint digest, valid candidates and
    exactly one accepted promoted winner matching the saved decisions. Accepted
    measurements must carry repeated timing, memory, quality and output hashes.
    """
    if not isinstance(profile, dict) or not _finite_tree(profile):
        return False
    if profile.get("schema_version") != PROFILE_SCHEMA_VERSION:
        return False
    if profile.get("mode") not in ("quick", "full"):
        return False
    if profile.get("objective") not in ("latency", "throughput", "balanced"):
        return False
    fingerprint = profile.get("fingerprint")
    if not isinstance(fingerprint, dict):
        return False
    if profile.get("fingerprint_sha256") != fingerprint_digest(fingerprint):
        return False
    if not _valid_decisions(profile.get("decisions")):
        return False
    if not finite_number(profile.get("warmup_seconds")):
        return False
    measurements = profile.get("measurements")
    if not isinstance(measurements, list) or not measurements:
        return False
    for measurement in measurements:
        if not isinstance(measurement, dict):
            return False
        if not _valid_decisions(measurement.get("candidate")):
            return False
        if (
            type(measurement.get("accepted")) is not bool
            or type(measurement.get("promoted")) is not bool
        ):
            return False
        if measurement["accepted"]:
            for key in ("pages_per_min", "p95_seconds"):
                if not finite_number(measurement.get(key)) or measurement[key] <= 0:
                    return False
            if (
                type(measurement.get("repetitions")) is not int
                or measurement["repetitions"] < 2
            ):
                return False
            if (
                not finite_number(measurement.get("kv_cache_pct"))
                or measurement["kv_cache_pct"] >= 0.95
            ):
                return False
            if (
                type(measurement.get("metric_samples")) is not int
                or measurement["metric_samples"] < measurement["repetitions"]
            ):
                return False
            if (
                not finite_number(measurement.get("output_variation_cer"))
                or measurement["output_variation_cer"] > 0.002
            ):
                return False
            hashes = measurement.get("output_sha256")
            cers = measurement.get("cers")
            durations = measurement.get("durations_seconds")
            if (
                not isinstance(hashes, list)
                or not hashes
                or any(
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(char not in "0123456789abcdef" for char in value)
                    for value in hashes
                )
            ):
                return False
            if (
                not isinstance(cers, list)
                or len(cers) != len(hashes)
                or any(not finite_number(value) or value > 1 for value in cers)
            ):
                return False
            if (
                not isinstance(durations, list)
                or len(durations) != len(hashes) * measurement["repetitions"]
                or any(not finite_number(value) or value <= 0 for value in durations)
            ):
                return False
        elif measurement["promoted"]:
            return False
    winners = [measurement for measurement in measurements if measurement["promoted"]]
    return (
        measurements[0]["accepted"]
        and len(winners) == 1
        and winners[0]["candidate"] == profile["decisions"]
    )


def profile_matches(
    profile: dict, fingerprint: dict, mode: str, objective: str
) -> bool:
    """Require a valid profile with matching mode, objective and fingerprint."""
    if not _valid_profile(profile):
        return False
    if profile.get("mode") != mode:
        return False
    if profile.get("objective") != objective:
        return False
    return fingerprints_match(profile.get("fingerprint", {}), fingerprint)
