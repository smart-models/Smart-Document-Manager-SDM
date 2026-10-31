"""Verify frozen PARSE artifacts against the live chunk/split/refine routes.

No OCR requests are made. Original storage objects are never overwritten.
Run with the project's Python environment from the repository root.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values
from tokenizers import Tokenizer

from ocrserver.rag_refinement import materialize
from ocrserver.rag_split import SplitBudget, render_fragment, split_group
from ocrserver.schemas import RagRefinementOptions, StorageRagRefineRequest

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "benchmarks/results/rag-online-2026-10-03"
OUTPUT = ROOT / "benchmarks/results/rag-tests-2026-10-03"
TOKENIZER = ROOT / "benchmarks/cache/rag-tests/tokenizer.json"


class RealCounter:
    """Count exact tokens using the frozen corpus tokenizer without special tokens."""

    def __init__(self):
        """Load the local tokenizer and initialize the physical encode-call counter."""
        self.tokenizer = Tokenizer.from_file(str(TOKENIZER))
        self.calls = 0

    def count(self, text):
        """Count one exact encoding attempt and return its token count."""
        self.calls += 1
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)


def digest(data):
    """Return the SHA-256 hex digest of the supplied bytes."""
    return hashlib.sha256(data).hexdigest()


def expected_header(case):
    """Extract the expected leading HTML table rows from a frozen test case."""
    rows = list(
        re.finditer(r"<tr\b[^>]*>.*?</tr\s*>", case["text"], re.IGNORECASE | re.DOTALL)
    )
    count = case.get("expected_header_rows", 1)
    assert len(rows) >= count
    return case["text"][rows[0].start() : rows[count - 1].end()]


def coverage(chunks, groups):
    """Assert ordered, gap-free primary character coverage for every source chunk."""
    spans = {index: [] for index in range(len(chunks))}
    for group in groups:
        for entry in group:
            if isinstance(entry, int):
                spans[entry].append((0, len(chunks[entry]["text"])))
            elif len(entry) == 3:
                spans[entry[0]].append(tuple(entry[1:3]))
    for index, owned in spans.items():
        cursor = 0
        for start, end in owned:
            assert start == cursor, (index, start, cursor)
            cursor = end
        assert cursor == len(chunks[index]["text"]), (index, cursor)


def main():
    """Verify frozen live chunk/split/refine cases and write qualification evidence."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    headers = {}
    if env.get("UPSTREAM_CLIENT_TOKEN"):
        headers["Authorization"] = "Bearer " + env["UPSTREAM_CLIENT_TOKEN"]
    base = "http://127.0.0.1:" + env.get("OCR_SERVER_PORT", "8020")
    counter = RealCounter()
    records = json.loads((INPUT / "report.json").read_text(encoding="utf-8"))
    corpus = []
    cases = []
    # Freeze only verified parts available at invocation, capped to avoid
    # turning functional qualification into another whole-corpus ingest.
    selected = [r for r in records if r.get("verified")][:9]
    with httpx.Client(base_url=base, headers=headers, timeout=130) as client:
        for record in selected:
            stem = Path(record["path"]).stem
            raw = (INPUT / (stem + ".response.json")).read_bytes()
            parsed = json.loads(raw)
            downloaded = client.get("/storage/file", params={"key": record["rag_key"]})
            downloaded.raise_for_status()
            assert downloaded.json() == parsed["json_rag"]
            item = {
                "document": record["document"],
                "first_page": record["first_page"],
                "last_page": record["last_page"],
                "response_sha256": digest(raw),
                "source_key": record["rag_key"],
                "source_sha256": digest(downloaded.content),
                "tokenizer": parsed["usage"]["tokenizer"],
                "response_file": stem + ".response.json",
            }
            (OUTPUT / item["response_file"]).write_bytes(raw)
            corpus.append(item)
            tables = [
                (i, c)
                for i, c in enumerate(parsed["json_rag"]["chunks"])
                if c.get("type") == "table"
            ]
            for index, chunk in tables[:2]:
                if sum(c["kind"] == "table" for c in cases) >= 8:
                    break
                cases.append(
                    {
                        "name": stem + f"-table-{chunk['id']}",
                        "kind": "table",
                        "text": chunk["text"],
                        "source": item,
                        "index": index,
                        "expected_header_rows": 2
                        if stem == "istat-rapporto-2025-cap1-pages001-020"
                        and chunk["id"] == 24
                        else 1,
                    }
                )
        for item in corpus:
            parsed = json.loads(
                (OUTPUT / item["response_file"]).read_text(encoding="utf-8")
            )
            prose = sorted(
                (
                    (i, c)
                    for i, c in enumerate(parsed["json_rag"]["chunks"])
                    if c.get("type") == "text"
                ),
                key=lambda x: len(x[1]["text"]),
                reverse=True,
            )
            for index, chunk in prose[:2]:
                if sum(c["kind"] == "prose" for c in cases) >= 6:
                    break
                cases.append(
                    {
                        "name": item["response_file"] + f"-text-{chunk['id']}",
                        "kind": "prose",
                        "text": chunk["text"],
                        "source": item,
                        "index": index,
                    }
                )
        for kind, texts in {
            "code": [
                "```python\n" + "result = value + 1\n" * 120 + "```\n",
                "```\n" + "👨‍👩‍👧‍👦e\u0301" * 150 + "\n```",
            ],
            "formula": [
                "$$\\sum_{i=0}^{n}x_i^2$$\n",
                "$$" + "\\frac{x_i}{y_i}" * 200 + "$$",
            ],
            "unicode": ["👨‍👩‍👧‍👦 e\u0301 città. " * 100, "界😀e\u0301 " * 200],
        }.items():
            for index, text in enumerate(texts):
                cases.append(
                    {
                        "name": f"synthetic-{kind}-{index}",
                        "kind": kind,
                        "text": text,
                        "source": None,
                    }
                )
        (OUTPUT / "corpus.json").write_text(
            json.dumps(
                {
                    "frozen_at": datetime.now(timezone.utc).isoformat(),
                    "tokenizer_sha256": digest(TOKENIZER.read_bytes()),
                    "documents": corpus,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        (OUTPUT / "cases.json").write_text(
            json.dumps(cases, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        results = []

        def check(name, work):
            start = time.monotonic()
            try:
                details = work() or {}
                result = {"case": name, "status": "PASS", **details}
            except Exception as exc:  # noqa: BLE001 - record failed cases and continue.
                result = {
                    "case": name,
                    "status": "FAIL",
                    "error": type(exc).__name__ + ": " + str(exc)[:400],
                }
            result["seconds"] = round(time.monotonic() - start, 4)
            results.append(result)
            (OUTPUT / "api-cases.json").write_text(
                json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(json.dumps(result, ensure_ascii=False), flush=True)

        for case in cases:
            ranges = [(128, 512), (256, 1024)]
            if case.get("expected_header_rows", 1) > 1:
                ranges.append((128, 256))
            for minimum, maximum in ranges:

                def local(case=case, minimum=minimum, maximum=maximum):
                    """Verify local split/save parity, exact counts and source coverage.

                    Require repeated table headers and balanced table markup for
                    each rendered fragment across the selected token window.
                    """
                    chunks = [
                        {
                            "id": 1,
                            "text": case["text"],
                            "token_count": counter.count(case["text"]),
                            "page": 1,
                            "document": case["name"],
                        }
                    ]
                    options = RagRefinementOptions(
                        min_tokens=minimum, max_tokens=maximum
                    )
                    fragments = split_group(
                        chunks,
                        [0],
                        options,
                        counter,
                        SplitBudget(min_tokens=minimum, max_tokens=maximum),
                    )
                    groups = [f.entries for f in fragments]
                    coverage(chunks, groups)
                    request = StorageRagRefineRequest(
                        source_key="test/json_rag.json",
                        source_sha256="a" * 64,
                        execution_id=uuid.uuid4(),
                        created_at=datetime.now(timezone.utc),
                        options=options,
                        groups=groups,
                        counts=[f.member_token_counts for f in fragments],
                    )
                    saved = materialize(
                        {"chunks": chunks, "metadata": {"document": case["name"]}},
                        request,
                        counter=counter,
                    )
                    for fragment, chunk in zip(fragments, saved["chunks"]):
                        rendered = render_fragment(case["text"], fragment.entries)
                        assert chunk["text"] == rendered
                        assert (
                            chunk["token_count"]
                            == fragment.token_count
                            == counter.count(rendered)
                        )
                        expected = (
                            "below"
                            if fragment.token_count < minimum
                            else "above"
                            if fragment.token_count > maximum
                            else "within"
                        )
                        assert fragment.status == expected
                        if expected == "within":
                            assert "below_minimum_fragment" not in fragment.notes
                        if case["kind"] == "table":
                            assert expected_header(case) in rendered
                            assert rendered.lower().count(
                                "<table"
                            ) == rendered.lower().count("</table>")
                    return {
                        "fragments": len(fragments),
                        "above": sum(f.status == "above" for f in fragments),
                        "below": sum(f.status == "below" for f in fragments),
                    }

                check(f"local:{case['name']}:{minimum}-{maximum}", local)

        for case in [c for c in cases if c["source"]]:

            def live(case=case):
                """Verify repeatable live splits and idempotent refinement saves.

                Retain untouched source chunks, verify full primary coverage and
                compare downloaded text/counts against the proposed fragments.
                """
                source = case["source"]
                payload = {
                    "source_key": source["source_key"],
                    "source_sha256": source["source_sha256"],
                    "options": {
                        "min_tokens": 128,
                        "max_tokens": 256
                        if case.get("expected_header_rows", 1) > 1
                        else 512,
                    },
                    "group": [case["index"]],
                }
                response = client.post("/storage/rag/split", json=payload)
                response.raise_for_status()
                value = response.json()
                repeated = client.post("/storage/rag/split", json=payload)
                repeated.raise_for_status()
                assert repeated.json() == value
                original = json.loads(
                    (OUTPUT / source["response_file"]).read_text(encoding="utf-8")
                )["json_rag"]
                fragments = value["fragments"]
                # Split the one chosen source, retaining every other source unchanged.
                groups = (
                    [[i] for i in range(case["index"])]
                    + [f["entries"] for f in fragments]
                    + [[i] for i in range(case["index"] + 1, len(original["chunks"]))]
                )
                coverage(original["chunks"], groups)
                request = {
                    "source_key": source["source_key"],
                    "source_sha256": source["source_sha256"],
                    "options": payload["options"],
                    "groups": groups,
                    "execution_id": str(uuid.uuid4()),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                saved = client.post("/storage/rag/refine", json=request)
                saved.raise_for_status()
                key = saved.json()["file"]["key"]
                downloaded = client.get("/storage/file", params={"key": key})
                downloaded.raise_for_status()
                rag = downloaded.json()
                offset = case["index"]
                for f, c in zip(
                    fragments, rag["chunks"][offset : offset + len(fragments)]
                ):
                    assert c["text"] == render_fragment(case["text"], f["entries"])
                    assert c["token_count"] == f["token_count"]
                    if case["kind"] == "table":
                        assert expected_header(case) in c["text"]
                assert "[object Object]" not in downloaded.text
                retry = client.post("/storage/rag/refine", json=request)
                retry.raise_for_status()
                assert retry.json() == saved.json()
                return {
                    "fragments": len(fragments),
                    "saved_key": key,
                    "idempotent_save": True,
                }

            check("live-split-save:" + case["name"], live)

        for source in corpus[:5]:
            parsed = json.loads(
                (OUTPUT / source["response_file"]).read_text(encoding="utf-8")
            )
            for maximum, overlap in [(256, 0), (512, 0), (1024, 0), (512, 32)]:

                def chunk(
                    parsed=parsed, maximum=maximum, overlap=overlap, source=source
                ):
                    payload = {
                        "parsed_document": parsed,
                        "options": {
                            "max_tokens": maximum,
                            "overlap_tokens": overlap,
                            "preserve_tables": False,
                            "preserve_code_blocks": False,
                            "table_header_policy": "first_row",
                            "strictly": True,
                        },
                    }
                    response = client.post("/chunk", json=payload)
                    if response.status_code == 422:
                        detail = response.json().get("detail", {})
                        assert detail.get("code") in {
                            "protected_block_exceeds_budget",
                            "table_row_exceeds_budget",
                            "table_header_exceeds_budget",
                            "atomic_unit_exceeds_budget",
                        }, detail
                        return {"structural_conflict": detail}
                    response.raise_for_status()
                    value = response.json()
                    for i, c in enumerate(value["chunks"]):
                        assert c["id"] == i
                        assert c["token_count"] == counter.count(c["text"])
                        assert c["token_count"] <= maximum
                    filename = source["response_file"].replace(
                        ".response.json", f"-{maximum}-{overlap}.chunks.json"
                    )
                    (OUTPUT / filename).write_bytes(response.content)
                    return {"chunks": len(value["chunks"]), "artifact": filename}

                check(f"chunk:{source['response_file']}:{maximum}:{overlap}", chunk)
        print(
            json.dumps(
                {
                    "passed": sum(r["status"] == "PASS" for r in results),
                    "failed": sum(r["status"] == "FAIL" for r in results),
                }
            ),
            flush=True,
        )
        if any(result["status"] == "FAIL" for result in results):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
