"""Exercise the deployed optional RAG routes with a caller-supplied PDF.

Run inside the API container or provide UPSTREAM_CLIENT_TOKEN in the environment.
Artifacts include OCR text: choose a private output directory deliberately.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

import httpx
import rfc8785
from tokenizers import Tokenizer


def run(pdf: Path, output: Path, base_url: str) -> dict:
    """Check live OCR, exact counts, canonical parity and strict conflicts."""
    from ocrserver.chunking.validation import validate_output
    from ocrserver.token_count import resolve_tokenizer_path

    output.mkdir(parents=True, exist_ok=True)
    revision = os.environ.get(
        "OCR_VLLM_MODEL_REVISION",
        "c5630abae1d940eafe0697512a0325494b02ab42",
    )
    tokenizer_path = resolve_tokenizer_path(
        os.environ.get("OCR_HF_CACHE_DIR", "/app/model_cache/hf"),
        revision,
    )
    if tokenizer_path is None:
        raise RuntimeError("Local Paddle tokenizer is required")
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    secret = os.environ.get("UPSTREAM_CLIENT_TOKEN", "")
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    report = {
        "pdf": pdf.name,
        "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "tokenizer_sha256": hashlib.sha256(tokenizer_path.read_bytes()).hexdigest(),
        "checks": [],
    }

    def check_file(response, label, maximum, permissive=False):
        if response.status_code != 200:
            detail = response.json().get("detail", {})
            raise AssertionError(
                f"{label}: HTTP {response.status_code}, {detail.get('code')}"
            )
        value = response.json()
        validate_output(value)
        assert response.content == rfc8785.dumps(value)
        assert list(value) == ["chunks"]
        counts = []
        for index, chunk in enumerate(value["chunks"]):
            count = len(tokenizer.encode(chunk["text"], add_special_tokens=False).ids)
            assert chunk["id"] == index
            assert chunk["token_count"] == count
            assert count <= maximum or (permissive and chunk["overflow"])
            counts.append(count)
        (output / f"{label}.rag.json").write_bytes(response.content)
        record = {
            "case": label,
            "chunks": len(counts),
            "max_actual_tokens": max(counts),
            "overflow_chunks": sum(chunk["overflow"] for chunk in value["chunks"]),
            "bytes": len(response.content),
            "sha256": hashlib.sha256(response.content).hexdigest(),
            "seconds": round(response.elapsed.total_seconds(), 3),
        }
        report["checks"].append(record)
        print(json.dumps(record), flush=True)
        return value

    with httpx.Client(base_url=base_url, headers=headers, timeout=780) as client:
        health = client.get("/health")
        assert health.status_code == 200
        print(json.dumps({"health": health.json()["status"]}), flush=True)
        with pdf.open("rb") as stream:
            response = client.post(
                "/parse",
                files={"file": (pdf.name, stream, "application/pdf")},
                data={
                    "output": "both",
                    "max_pages": "50",
                    "table_format": "html",
                },
            )
        assert response.status_code == 200, f"OCR HTTP {response.status_code}"
        parsed = response.json()
        assert "chunks" not in parsed and parsed["pages"]
        assert not any(page.get("error") for page in parsed["pages"])
        (output / "parse.json").write_bytes(response.content)
        report["parse"] = {
            "pages": len(parsed["pages"]),
            "blocks": sum(len(page["blocks"]) for page in parsed["pages"]),
            "seconds": round(response.elapsed.total_seconds(), 3),
            "json_sha256": hashlib.sha256(response.content).hexdigest(),
        }
        print(json.dumps({"parse": report["parse"]}), flush=True)
        common = {
            "parsed_document": parsed,
            "source_id": pdf.name,
            "lang": "en",
        }
        for budget in (256, 512, 1024, 2048):
            body = {
                **common,
                "options": {
                    "max_tokens": budget,
                    "strictly": True,
                    "preserve_tables": False,
                    "preserve_code_blocks": False,
                },
            }
            response = client.post("/chunk", json=body)
            check_file(response, f"strict-{budget}", budget)
            if budget == 512:
                repeated = client.post("/chunk", json=body)
                assert repeated.status_code == 200
                assert repeated.content == response.content
                report["canonical_repeat_identical"] = True
        response = client.post(
            "/chunk", json={**common, "options": {"max_tokens": 512}}
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "protected_block_exceeds_budget"
        assert "chunks" not in response.json()
        report["strict_protected_conflict"] = response.json()["detail"]
        response = client.post(
            "/chunk",
            json={
                **common,
                "options": {
                    "max_tokens": 512,
                    "strictly": False,
                },
            },
        )
        check_file(response, "protected-permissive-512", 512, permissive=True)
        with pdf.open("rb") as stream:
            response = client.post(
                "/parse/chunks",
                files={"file": (pdf.name, stream, "application/pdf")},
                data={
                    "options": json.dumps(
                        {
                            "max_tokens": 512,
                            "preserve_tables": False,
                            "preserve_code_blocks": False,
                        }
                    ),
                    "source_id": pdf.name,
                    "lang": "en",
                },
            )
        direct = check_file(response, "direct-parse-512", 512)
        scope = direct["chunks"][0]["document_context"]["scope"]
        assert scope["kind"] == "complete_document"
        assert scope["total_pages"] == len(parsed["pages"])
        report["direct_parse_scope"] = scope
    report["retrieval_qualified"] = False
    (output / "report.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8020")
    arguments = parser.parse_args()
    run(arguments.pdf, arguments.output, arguments.base_url)
