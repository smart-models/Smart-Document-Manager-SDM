"""Exercise concurrent PDF requests and health responsiveness on the live API."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    """Raise RuntimeError when a live acceptance condition fails."""
    if not condition:
        raise RuntimeError(message)


async def main() -> None:
    """Verify concurrent PDF parsing and responsive health checks on the live API."""
    configuration = {**dotenv_values(ROOT / ".env"), **os.environ}
    token = configuration.get("UPSTREAM_CLIENT_TOKEN") or ""
    base = configuration.get("OCR_API_BASE_URL") or (
        "http://127.0.0.1:" + (configuration.get("OCR_SERVER_PORT") or "8020")
    )
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = {
        "pdf_base64": base64.b64encode(
            (ROOT / "calibration/multipage.pdf").read_bytes()
        ).decode(),
        "max_pages": 1,
        "dpi": 50,
        "output": "markdown",
    }
    report = {"parallel_pdf_requests": 2, "health_probe_ms": []}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=660) as client:
        health = await client.get("/health", timeout=5)
        require(
            health.status_code == 200 and health.json().get("status") == "healthy",
            "API is not ready",
        )
        if token:
            wrong = await client.get(
                "/stats", headers={"Authorization": "Bearer invalid-verification-token"}
            )
            require(wrong.status_code == 401, "Wrong API credential was accepted")
            report["wrong_token_status"] = wrong.status_code
        pending = {
            asyncio.create_task(client.post("/parse", json=body)) for _ in range(2)
        }
        completed = []
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, timeout=0.2, return_when=asyncio.FIRST_COMPLETED
                )
                completed.extend(done)
                if pending:
                    start = time.monotonic()
                    response = await client.get("/health", timeout=5)
                    require(response.status_code == 200, "Health failed during parsing")
                    report["health_probe_ms"].append(
                        round((time.monotonic() - start) * 1000, 3)
                    )
            for task in completed:
                response = task.result()
                require(response.status_code == 200, "Concurrent PDF request failed")
                payload = response.json()
                require(
                    len(payload["pages"]) == 1 and not payload["pages"][0].get("error"),
                    "Concurrent PDF returned a page error",
                )
            stats = await client.get("/stats")
            require(stats.status_code == 200, "Stats failed after parsing")
            require(
                stats.json() == {"queue_size": 0.0, "active": 0.0},
                "Request capacity was not released",
            )
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, *completed, return_exceptions=True)
    probes = report.pop("health_probe_ms")
    require(bool(probes), "No health probe overlapped PDF inference")
    report.update(
        {
            "passed": True,
            "health_probes": len(probes),
            "max_health_ms": max(probes),
            "capacity_released": True,
        }
    )
    target = ROOT / "docs/verification/api-live.json"
    target.write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print("Live API verified: concurrent PDFs, responsive health, capacity released.")


if __name__ == "__main__":
    asyncio.run(main())
