"""Exercise deployed OCR alignment with public fixtures and no inference retries.

Formula responses require a separate semantic review; HTTP success does not
establish mathematical correctness. Credentials and request bytes are omitted
from the report and all failure messages.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
MODEL = "PaddlePaddle/PaddleOCR-VL-1.6"
TEXT_LINES = (
    "The quick brown fox",
    "jumps over the lazy dog.",
    "Lorem ipsum dolor sit amet.",
)


class VerificationError(RuntimeError):
    """A diagnostic consisting solely of verifier-owned, public text."""


def require(condition: bool, message: str) -> None:
    """Keep verification active even under Python -O."""
    if not condition:
        raise VerificationError(message)


def recognize(client: httpx.Client, path: Path, task: str) -> dict:
    """Submit one calibration image and require a valid recognition response."""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    response = client.post("/ocr", json={"task": task, "image_base64": encoded})
    require(response.status_code == 200, f"HTTP {response.status_code}")
    payload = response.json()
    require(isinstance(payload, dict), "Invalid response object")
    return payload


def parse_form(client: httpx.Client, path: Path) -> dict:
    """Parse the calibration PDF and require a valid response object."""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    response = client.post("/parse", json={"pdf_base64": encoded, "output": "both"})
    require(response.status_code == 200, f"HTTP {response.status_code}")
    payload = response.json()
    require(isinstance(payload, dict), "Invalid response object")
    return payload


def check_result(payload: dict, task: str) -> None:
    """Check the short corpus without declaring formula equivalence."""
    require(payload.get("model_name") == MODEL, "Unexpected model")
    warnings = payload.get("warnings")
    require(isinstance(warnings, list), "Invalid warnings")
    require(not warnings, "Unexpected warnings on a short clean fixture")
    if task == "parse":
        pages = payload.get("pages")
        require(isinstance(pages, list) and len(pages) == 1, "Expected one PDF page")
        require(isinstance(pages[0], dict), "Invalid PDF page")
        require(not pages[0].get("error"), "PDF page error")
        text = pages[0].get("markdown")
        require(isinstance(text, str), "Missing PDF text")
        require("FORM VALUE" in " ".join(text.split()).upper(), "Form value missing")
        return

    require(payload.get("task") == task, "Unexpected task")
    result = payload.get("result")
    require(isinstance(result, str) and bool(result.strip()), "Missing OCR text")
    text = " ".join(re.sub(r"<[^>]*>", " ", result).split())
    if task in {"ocr", "spotting"}:
        require(all(line in text for line in TEXT_LINES), "Expected text missing")
    if task == "spotting":
        boxes = payload.get("boxes")
        require(isinstance(boxes, list) and len(boxes) == 3, "Expected three boxes")
        for box in boxes:
            require(isinstance(box, list) and len(box) == 4, "Incomplete box")
            require(
                all(
                    type(value) in (int, float) and 0 <= value <= 1000 for value in box
                ),
                "Invalid LOC coordinates",
            )
            require(box[0] < box[2] and box[1] < box[3], "Invalid box envelope")
    if task == "table":
        expected = (
            "Name",
            "Value",
            "Unit",
            "Alpha",
            "100",
            "kg",
            "Beta",
            "250",
            "m",
            "Gamma",
            "3.14",
            "rad",
        )
        cells = text.replace("|", " ").split()
        require(all(value in cells for value in expected), "Table content missing")


def run(base_url: str, target: Path) -> dict:
    """Run serially, persisting completed cases even if a later case fails."""
    configuration = {**dotenv_values(ROOT / ".env"), **os.environ}
    token = configuration.get("UPSTREAM_CLIENT_TOKEN") or ""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    formula_dir = ROOT / "docs/verification/formula-smoke"
    fixtures = json.loads((formula_dir / "fixtures.json").read_text(encoding="utf-8"))
    cases = [
        ("text", "ocr", ROOT / "calibration/plain_text.png", None),
        ("spotting", "spotting", ROOT / "calibration/plain_text.png", None),
        ("table", "table", ROOT / "calibration/simple_table.png", None),
    ]
    cases.extend(
        (
            f"formula_{item['name']}",
            "formula",
            formula_dir / f"{item['name']}.png",
            item["latex"],
        )
        for item in fixtures["cases"]
    )
    cases.append(("pdf_form", "parse", ROOT / "tests/fixtures/acroform.pdf", None))
    report = {
        "runtime_modified": False,
        "cases": [],
        "automated_checks_passed": False,
        "formula_semantic_review": "required",
    }
    target.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        target.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )

    try:
        with httpx.Client(base_url=base_url, headers=headers, timeout=180) as client:
            health = client.get("/health", timeout=5)
            require(health.status_code == 200, "Health request failed")
            require(health.json().get("status") == "healthy", "API is not ready")
            for name, task, path, expected in cases:
                record = {"name": name, "task": task, "transport_passed": False}
                report["cases"].append(record)
                started = time.monotonic()
                try:
                    record["fixture"] = path.relative_to(ROOT).as_posix()
                    record["fixture_sha256"] = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
                    payload = (
                        parse_form(client, path)
                        if task == "parse"
                        else recognize(
                            client,
                            path,
                            task,
                        )
                    )
                    record.update(transport_passed=True, response=payload)
                    check_result(payload, task)
                    record["content_checks_passed"] = True
                    if expected is not None:
                        record.update(
                            expected_latex=expected, semantic_review="required"
                        )
                    print(f"{name}: HTTP and automatic checks passed", flush=True)
                except Exception as exc:
                    record["error_type"] = type(exc).__name__
                    if isinstance(exc, VerificationError):
                        record["check_failure"] = str(exc)
                    raise VerificationError(f"Verification failed: {name}") from None
                finally:
                    record["wall_seconds"] = round(time.monotonic() - started, 3)
                    save()
            health = client.get("/health", timeout=5)
            require(health.status_code == 200, "Final health request failed")
            report["final_health"] = health.json().get("status")
            require(report["final_health"] == "healthy", "API is not healthy")
            report["automated_checks_passed"] = True
    finally:
        save()
    return report


def main() -> None:
    """Run model-alignment checks and persist a credential-free evidence report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8020")
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "docs/verification/model-alignment-fixes/live.json",
    )
    args = parser.parse_args()
    try:
        run(args.base_url, args.report)
    except Exception as exc:
        detail = str(exc) if isinstance(exc, VerificationError) else type(exc).__name__
        raise SystemExit(f"Alignment verification failed: {detail}") from None
    print(
        (
            "Automatic checks passed. Review the four formula outputs for "
            "mathematical correctness."
        )
    )


if __name__ == "__main__":
    main()
