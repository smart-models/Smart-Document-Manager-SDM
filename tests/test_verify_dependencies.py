"""Tests for the dependency vulnerability/SBOM verifier."""

import json
import hashlib
from datetime import date, timedelta

import pytest

from scripts import verify_dependencies as vd


def _triage(tmp_path, lock):
    path = tmp_path / "triage.json"
    decision = {
        "lock": lock.name,
        "sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "image_id": "sha256:verified-image",
        "package": "setuptools",
        "version": "80.10.2",
        "advisories": ["GHSA-reviewed"],
        "expires": (date.today() + timedelta(days=7)).isoformat(),
        "reason": (
            "The affected sdist publishing operation is absent from this runtime."
        ),
        "source": "https://example.com/advisory",
    }
    path.write_text(json.dumps([decision]))
    (tmp_path / "inventory.json").write_text(
        json.dumps(
            {
                "test": {
                    "lock": lock.name,
                    "sha256": decision["sha256"],
                    "image_id": decision["image_id"],
                }
            }
        )
    )
    return path, decision


@pytest.mark.parametrize("new_advisory", [False, True])
def test_exact_review_keeps_raw_evidence_and_never_exempts_new_advisories(
    tmp_path, monkeypatch, new_advisory
):
    lock = _lock(tmp_path, "svc.txt", ["setuptools==80.10.2"])
    triage, _ = _triage(tmp_path, lock)
    ids = ["GHSA-reviewed"] + (["GHSA-new"] if new_advisory else [])
    monkeypatch.setattr(
        vd, "query_vulnerabilities", lambda pins: {("setuptools", "80.10.2"): ids}
    )
    monkeypatch.setattr(
        vd, "vulnerability_identity", lambda identity: (identity, "test")
    )
    report = tmp_path / "audit.json"
    code = vd.main(
        [
            "--locks-dir",
            str(tmp_path),
            "--triage",
            str(triage),
            "--json-output",
            str(report),
        ]
    )
    data = json.loads(report.read_text())
    assert code == int(new_advisory)
    assert data["unique_cves"] == int(new_advisory)
    assert data["raw_unique_cves"] == 1 + int(new_advisory)
    assert data["not_affected"][0]["advisories"] == ["GHSA-reviewed"]
    assert data["raw_findings"][0]["advisories"] == ids


@pytest.mark.parametrize(
    "change", ["expired", "hash", "version", "missing-lock", "duplicate", "image"]
)
def test_stale_or_invalid_review_fails_closed(tmp_path, monkeypatch, change):
    lock = _lock(tmp_path, "svc.txt", ["setuptools==80.10.2"])
    triage, decision = _triage(tmp_path, lock)
    if change == "expired":
        decision["expires"] = "2000-01-01"
    elif change == "hash":
        lock.write_text("setuptools==80.10.2\n# image changed\n")
    elif change == "version":
        decision["version"] = "99.0.0"
    elif change == "missing-lock":
        decision["lock"] = "absent.txt"
    elif change == "image":
        decision["image_id"] = "sha256:another-image"
    triage.write_text(
        json.dumps([decision, decision] if change == "duplicate" else [decision])
    )
    monkeypatch.setattr(vd, "query_vulnerabilities", lambda pins: {})
    assert (
        vd.main(["--locks-dir", str(tmp_path), "--triage", str(triage)])
        == vd.EXIT_ERROR
    )


def _lock(tmp_path, name, lines):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_parse_lock_skips_comments_and_blank_lines(tmp_path):
    lock = _lock(
        tmp_path,
        "svc.txt",
        ["# generated", "", "fastapi==0.138.1", "  httpx==0.28.1  ", "# tail"],
    )
    assert vd.parse_lock(lock) == [("fastapi", "0.138.1"), ("httpx", "0.28.1")]


def test_parse_lock_rejects_non_exact_entries(tmp_path):
    lock = _lock(tmp_path, "svc.txt", ["fastapi>=0.138.1"])

    with pytest.raises(ValueError, match="exact name==version pin"):
        vd.parse_lock(lock)


def test_query_vulnerabilities_maps_ids_per_pin(monkeypatch):
    def fake_post(url, payload):
        queries = payload["queries"]
        results = []
        for q in queries:
            if q["package"]["name"] == "urllib3" and q["version"] == "1.24.1":
                results.append({"vulns": [{"id": "GHSA-x"}, {"id": "GHSA-y"}]})
            else:
                results.append({})
        return {"results": results}

    monkeypatch.setattr(vd, "_post_json", fake_post)
    findings = vd.query_vulnerabilities([("urllib3", "1.24.1"), ("fastapi", "0.138.1")])

    assert findings == {("urllib3", "1.24.1"): ["GHSA-x", "GHSA-y"]}


def test_query_vulnerabilities_rejects_a_result_count_mismatch(monkeypatch):
    monkeypatch.setattr(vd, "_post_json", lambda url, payload: {"results": []})

    with pytest.raises(RuntimeError, match="result count"):
        vd.query_vulnerabilities([("fastapi", "0.138.1")])


def test_query_vulnerabilities_rejects_malformed_findings(monkeypatch):
    monkeypatch.setattr(
        vd,
        "_post_json",
        lambda url, payload: {"results": [{"vulns": [{"summary": "no id"}]}]},
    )

    with pytest.raises(RuntimeError, match="without an id"):
        vd.query_vulnerabilities([("fastapi", "0.138.1")])


def test_describe_vulnerability_never_raises(monkeypatch):
    def boom(url):
        raise OSError("offline")

    monkeypatch.setattr(vd, "_get_json", boom)
    assert vd.describe_vulnerability("GHSA-x") == ""

    monkeypatch.setattr(vd, "_get_json", lambda url: ["invalid"])
    assert vd.vulnerability_identity("GHSA-x") == ("GHSA-x", "")


@pytest.mark.parametrize("aliases", [None, 42, "CVE-invalid", {"CVE-invalid": 1}])
def test_vulnerability_identity_ignores_malformed_aliases(monkeypatch, aliases):
    monkeypatch.setattr(
        vd, "_get_json", lambda url: {"aliases": aliases, "summary": "Advisory"}
    )
    assert vd.vulnerability_identity("GHSA-x") == ("GHSA-x", "Advisory")


def test_sbom_document_is_minimal_cyclone(tmp_path):
    doc = vd.sbom_document("svc", [("BPackage", "1.0"), ("apkg", "2.0")])

    assert doc["bomFormat"] == "CycloneDX"
    assert doc["specVersion"] == "1.5"
    assert [c["name"] for c in doc["components"]] == ["apkg", "BPackage"]
    assert all(c["purl"].startswith("pkg:pypi/") for c in doc["components"])


def test_main_reports_findings_and_exits_1(tmp_path, monkeypatch, capsys):
    _lock(tmp_path, "svc.txt", ["urllib3==1.24.1"])

    def fake_post(url, payload):
        return {"results": [{"vulns": [{"id": "GHSA-x"}]}]}

    monkeypatch.setattr(vd, "_post_json", fake_post)
    monkeypatch.setattr(
        vd,
        "_get_json",
        lambda url: {"summary": "smuggling", "aliases": ["CVE-2026-1"]},
    )

    exit_code = vd.main(["--locks-dir", str(tmp_path)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "urllib3==1.24.1" in err
    assert "GHSA-x" in err
    assert "1 unique vulnerability" in err


def test_main_deduplicates_aliases_and_writes_json(tmp_path, monkeypatch):
    _lock(tmp_path, "a.txt", ["h2==4.4.0"])
    _lock(tmp_path, "b.txt", ["h2==4.4.0"])
    monkeypatch.setattr(
        vd,
        "_post_json",
        lambda url, payload: {
            "results": [
                {"vulns": [{"id": "GHSA-x"}, {"id": "PYSEC-x"}]},
                {"vulns": [{"id": "GHSA-x"}, {"id": "PYSEC-x"}]},
            ]
        },
    )
    monkeypatch.setattr(
        vd,
        "_get_json",
        lambda url: {"aliases": ["CVE-2026-1"], "summary": "duplicate host"},
    )
    report = tmp_path / "report.json"

    exit_code = vd.main(["--locks-dir", str(tmp_path), "--json-output", str(report)])

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert exit_code == vd.EXIT_VULNERABLE
    assert payload["unique_cves"] == 1
    assert payload["advisory_occurrences"] == 4
    assert payload["affected_locks"] == 2


def test_main_passes_when_nothing_is_found(tmp_path, monkeypatch, capsys):
    _lock(tmp_path, "svc.txt", ["fastapi==0.138.1"])
    monkeypatch.setattr(vd, "_post_json", lambda url, payload: {"results": [{}]})

    exit_code = vd.main(["--locks-dir", str(tmp_path)])

    assert exit_code == 0
    assert "No known vulnerabilities" in capsys.readouterr().out


def test_main_distinguishes_osv_unavailable_from_an_invalid_response(
    tmp_path, monkeypatch, capsys
):
    _lock(tmp_path, "svc.txt", ["fastapi==0.138.1"])

    def unavailable(url, payload):
        import urllib.error

        raise urllib.error.URLError("offline")

    monkeypatch.setattr(vd, "_post_json", unavailable)
    assert vd.main(["--locks-dir", str(tmp_path)]) == vd.EXIT_UNAVAILABLE
    assert "[UNAVAILABLE]" in capsys.readouterr().err

    monkeypatch.setattr(vd, "_post_json", lambda url, payload: {"results": []})
    assert vd.main(["--locks-dir", str(tmp_path)]) == vd.EXIT_ERROR
    assert "Invalid OSV response" in capsys.readouterr().err

    def invalid_json(url, payload):
        raise json.JSONDecodeError("invalid", "x", 0)

    monkeypatch.setattr(vd, "_post_json", invalid_json)
    assert vd.main(["--locks-dir", str(tmp_path)]) == vd.EXIT_ERROR
    assert "Invalid OSV response" in capsys.readouterr().err


def test_main_classifies_an_invalid_lock_as_an_internal_error(
    tmp_path, monkeypatch, capsys
):
    _lock(tmp_path, "svc.txt", ["fastapi>=0.138.1"])
    monkeypatch.setattr(
        vd,
        "_post_json",
        lambda url, payload: pytest.fail("OSV must not be queried"),
    )

    assert vd.main(["--locks-dir", str(tmp_path)]) == vd.EXIT_ERROR
    assert "Could not prepare dependency audit" in capsys.readouterr().err


def test_main_writes_sboms_when_requested(tmp_path, monkeypatch):
    _lock(tmp_path, "svc.txt", ["fastapi==0.138.1"])
    monkeypatch.setattr(vd, "_post_json", lambda url, payload: {"results": [{}]})
    sbom_dir = tmp_path / "sbom"

    exit_code = vd.main(["--locks-dir", str(tmp_path), "--sbom", str(sbom_dir)])

    assert exit_code == 0
    sbom = sbom_dir / "svc.cdx.json"
    assert sbom.is_file()
    assert json.loads(sbom.read_text(encoding="utf-8"))["bomFormat"] == "CycloneDX"
