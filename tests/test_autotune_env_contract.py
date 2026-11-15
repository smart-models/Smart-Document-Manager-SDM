"""Regression coverage for the autotune environment contract."""

import json

import pytest

import autotune_entrypoint
from ocrserver.autotune.fingerprint import build_fingerprint
from ocrserver.autotune.profile import AutotuneConfig
from ocrserver.autotune.runner import run
from ocrserver.config import Settings


@pytest.mark.parametrize(
    "key,value",
    [
        ("OCR_PARSE_DPI", "400"),
        ("OCR_LAYOUT_THREADS", "1"),
        ("OCR_VL_LANES", "3"),
        ("OCR_PAGE_TIMEOUT", "20"),
    ],
)
def test_measurement_settings_invalidate_profile(monkeypatch, key, value):
    monkeypatch.setattr("ocrserver.autotune.fingerprint._source_digest", lambda: "same")
    assert build_fingerprint(None, {}) != build_fingerprint(None, {key: value})


def test_resolved_defaults_and_secrets_do_not_invalidate_profile(monkeypatch):
    monkeypatch.setattr("ocrserver.autotune.fingerprint._source_digest", lambda: "same")
    assert build_fingerprint(None, {}) == build_fingerprint(
        None,
        {
            "OCR_PARSE_DPI": "250",
            "OCR_SERVER_API_TOKENS": "not-a-real-token:test",
        },
    )


def test_engine_command_invalidation(monkeypatch, tmp_path):
    path = tmp_path / "hardware.json"
    manifest = {
        "schema_version": 1,
        "gpu": None,
        "tier": {},
        "vllm_version": "test",
        "command": ["vllm", "--max-model-len", "16384"],
    }
    path.write_text(json.dumps(manifest))
    env = {"OCR_VLLM_HARDWARE_FILE": str(path)}
    before = build_fingerprint(None, env)
    manifest["command"][-1] = "8192"
    path.write_text(json.dumps(manifest))
    assert before != build_fingerprint(None, env)


def test_manual_axes_are_measured_and_not_swept():
    seen = []

    def measure(candidate, items):
        seen.append(dict(candidate))
        return {
            "pages_per_min": 10.0,
            "p95_seconds": 1.0,
            "outputs": ["stable"] * len(items),
            "cers": [0.1] * len(items),
            "durations_seconds": [1.0] * len(items),
            "kv_cache_pct": 0.1,
            "metric_samples": 1,
        }

    result = run(
        AutotuneConfig(),
        {},
        {
            "OCR_VL_LANES": "3",
            "OCR_LAYOUT_THREADS": "1",
        },
        measure_fn=measure,
    )
    assert result["action"] == "calibrated"
    assert seen == [{"vl_lanes": 3, "page_parallelism": 1, "layout_threads": 1}] * 2
    assert result["decisions"] == seen[0]


def test_fallback_does_not_load_previous_profile(monkeypatch):
    import ocr_server

    monkeypatch.setenv("OCR_AUTOTUNE_PROFILE", "previous-profile.json")
    monkeypatch.setenv("OCR_AUTOTUNE_ACTION", "fallback")

    def forbidden(*_args):
        raise AssertionError("fallback attempted to load old profile")

    monkeypatch.setattr("ocrserver.autotune.profile.load_profile", forbidden)
    assert ocr_server._load_and_validate_profile() is None


def test_nonserial_pages_are_rejected():
    with pytest.raises(ValueError, match="page_parallelism"):
        Settings(page_parallelism=2)


@pytest.mark.asyncio
async def test_measurement_uses_effective_pdf_and_timeout_settings(monkeypatch):
    from PIL import Image

    calls = []
    image = Image.new("RGB", (1, 1))

    def rasterize(raw, **kwargs):
        calls.append(kwargs)
        return [image]

    async def fetch(_url):
        return {"kv_cache_pct": 0.1, "preemption_total": 0}

    async def monitor(_url, stop):
        await stop.wait()
        return {"peak_kv_cache_pct": 0.1, "preemption_total": 0, "samples": 1}

    class Pipeline:
        async def parse_document(self, pages, lanes, page_timeout, timeout, **kwargs):
            calls.append((lanes, page_timeout, timeout))
            for page in pages:
                page.close()
            return [{"markdown": "expected"}]

    monkeypatch.setattr("ocrserver.pdf_utils.rasterize_pdf", rasterize)
    monkeypatch.setattr(autotune_entrypoint, "_fetch_vllm_metrics", fetch)
    monkeypatch.setattr(autotune_entrypoint, "_monitor_vllm_metrics", monitor)
    settings = Settings(
        parse_dpi=300,
        max_pages=10,
        pdf_max_page_pixels=50_000_000,
        page_timeout=25,
        request_timeout=70,
    )
    await autotune_entrypoint._measure_workload(
        Pipeline(),
        {"vl_lanes": 3},
        [("pdf", b"pdf", "expected")],
        settings.vllm_url,
        settings,
    )
    assert calls == [
        {
            "dpi": 300,
            "max_pages": 10,
            "max_page_pixels": 50_000_000,
        },
        (3, 25, 70),
    ]


@pytest.mark.asyncio
async def test_undrained_predictor_invalidates_measurement(monkeypatch):
    import httpx
    from types import SimpleNamespace

    class Pipeline:
        def __init__(self, *args, **kwargs):
            pass

        async def aclose(self, timeout):
            return False

    async def measure(*_args):
        return {"measurement": "completed"}

    factory = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    monkeypatch.setattr(httpx, "AsyncClient", lambda: factory(transport=transport))
    monkeypatch.setattr("ocrserver.pipeline_backend.PipelineBackend", Pipeline)
    monkeypatch.setattr(autotune_entrypoint, "_measure_workload", measure)
    item = SimpleNamespace(kind="text", path="plain_text.png", ground_truth="expected")
    with pytest.raises(RuntimeError, match="native work"):
        await autotune_entrypoint._async_measure(
            {"vl_lanes": 2}, [item], "http://test/v1", "model"
        )


def test_sweep_stops_after_loss_of_measurement_isolation():
    from ocrserver.autotune.runner import MeasurementIsolationError

    seen = []

    def measure(candidate, items):
        seen.append(candidate["vl_lanes"])
        if candidate["vl_lanes"] != 2:
            raise MeasurementIsolationError("native work remains")
        return {
            "pages_per_min": 10.0,
            "p95_seconds": 1.0,
            "outputs": ["stable"] * len(items),
            "cers": [0.1] * len(items),
            "durations_seconds": [1.0] * len(items),
            "kv_cache_pct": 0.1,
            "metric_samples": 1,
        }

    result = run(AutotuneConfig(mode="full"), {}, {}, measure_fn=measure)
    assert result["action"] == "fallback"
    assert "native work" in result["reason"]
    assert seen == [2, 2, 4]


def test_consumed_autotune_environment_has_a_documented_contract():
    import ast
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    files = [root / "autotune_entrypoint.py", root / "vllm/stage1_entrypoint.py"]
    files.extend((root / "ocrserver/autotune").glob("*.py"))
    consumed = set()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            name = getattr(call.func, "attr", getattr(call.func, "id", ""))
            if name not in {"get", "_env_str", "_env_int", "_env_bool", "_env_float"}:
                continue
            for arg in call.args[:2]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.startswith(("OCR_", "VLLM_")):
                        consumed.add(arg.value)
    public = set(
        re.findall(r"^\s*#?\s*(OCR_\w+)\s*=", (root / ".env.example").read_text(), re.M)
    )
    internal = {
        "OCR_AUTOTUNE_PROFILE",
        "OCR_VLLM_HARDWARE_FILE",
        "OCR_VLLM_PORT",
        "VLLM_USE_V2_MODEL_RUNNER",
    }
    assert consumed - public - internal == set()
    compose = (root / "compose.yml").read_text()
    assert all(key in compose for key in internal)


def test_all_api_settings_are_declared_in_environment_template():
    import re
    from pathlib import Path

    from ocrserver.config import _ENV_KEYS

    template = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    declared = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=", template, re.M))
    assert set(_ENV_KEYS.values()) <= declared


def _ocr_api_service() -> str:
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[1] / "compose.yml").read_text()
    start = compose.index("\n  ocr-api:\n")
    end = compose.index("\n  ocr-mcp:\n", start)
    return compose[start:end]


def test_table_reread_default_is_set_in_settings_alone():
    import re
    from pathlib import Path

    from ocrserver.config import Settings

    # Compose and the template pass an empty value, so flipping the default is
    # one line in ocrserver/config.py.
    assert "OCR_TABLE_REREAD: ${OCR_TABLE_REREAD:-}" in _ocr_api_service()
    template = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    assert re.search(r"^OCR_TABLE_REREAD=$", template, re.M)
    assert Settings().table_reread is True


def test_ocr_api_runs_layout_on_the_gpu_with_a_memory_cap():
    service = _ocr_api_service()
    assert "driver: nvidia" in service and "capabilities: [gpu]" in service
    assert "FLAGS_gpu_memory_limit_mb: ${OCR_LAYOUT_GPU_MEMORY_MB:-3584}" in service


def test_vllm_and_layout_reserve_the_same_selected_gpu():
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[1] / "compose.yml").read_text()
    assert "count: all" not in compose
    assert compose.count('device_ids: ["${OCR_GPU_DEVICE:-0}"]') == 2
