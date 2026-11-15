"""Configuration failures must surface before creating clients or semaphores."""

import pytest

from ocrserver.config import Settings, load_settings


def test_environment_profile_default_precedence():
    assert load_settings({}, None) == Settings()
    profile = {"decisions": {"vl_lanes": 3, "page_parallelism": 1, "layout_threads": 4}}
    result = load_settings({"OCR_VL_LANES": "5"}, profile)
    assert (result.vl_lanes, result.page_parallelism, result.layout_threads) == (
        5,
        1,
        4,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"port": 0},
        {"port": 65536},
        {"max_queue_size": 0},
        {"element_max_slots": -1},
        {"max_pages": 0},
        {"pdf_max_page_pixels": 0},
        {"max_file_bytes": 0},
        {"vl_lanes": 1.5},
        {"vl_max_inflight": 0},
        {"layout_threads": False},
        {"parse_dpi": 401},
        {"parse_dpi": 49},
        {"rate_limit_rpm": -1},
        {"request_timeout": float("nan")},
        {"page_timeout": float("inf")},
        {"request_timeout": 0},
        {"rate_limit_idle_seconds": 0},
        {"rate_limit_max_clients": 0},
        {"expose_frontend": "maybe"},
        {"vllm_model_revision": ""},
        {"vllm_url": "http://private:secret@host/v1"},
        {"vllm_url": "http://host:99999/v1"},
        {"vllm_url": "http://host:0/v1"},
        {"vllm_url": "http://[broken"},
    ],
)
def test_invalid_settings_rejected(changes):
    with pytest.raises(ValueError):
        Settings(**changes)


@pytest.mark.parametrize(
    "key,value",
    [
        ("OCR_VL_LANES", "1.5"),
        ("EXPOSE_FRONTEND", "maybe"),
        ("OCR_REQUEST_TIMEOUT", "NaN"),
        ("OCR_LAYOUT_THREADS", "zero"),
    ],
)
def test_invalid_environment_is_not_silently_defaulted(key, value):
    with pytest.raises(ValueError):
        load_settings({key: value}, None)


def test_table_reread_is_on_unless_the_environment_turns_it_off():
    assert Settings().table_reread is True
    assert load_settings({"OCR_TABLE_REREAD": ""}, None).table_reread is True
    assert load_settings({"OCR_TABLE_REREAD": "false"}, None).table_reread is False
    with pytest.raises(ValueError):
        load_settings({"OCR_TABLE_REREAD": "maybe"}, None)
    with pytest.raises(ValueError):
        Settings(table_reread="yes")


def test_fractional_profile_integer_is_not_truncated():
    with pytest.raises(ValueError, match="vl_lanes"):
        load_settings({}, {"decisions": {"vl_lanes": 1.5}})


def test_factory_does_not_load_ambient_profile(monkeypatch):
    from test_mcp_schema import FakePipeline, FakeRecognizer

    import ocr_server

    def forbidden():
        raise AssertionError("factory read the environment profile")

    monkeypatch.setattr(ocr_server, "_load_and_validate_profile", forbidden)
    ocr_server.create_app(Settings(), FakePipeline(), FakeRecognizer())


def test_launcher_uses_resolved_port_and_leaves_time_for_worker_drain(monkeypatch):
    import autotune_entrypoint

    captured = []
    monkeypatch.setenv("OCR_SERVER_PORT", "")
    monkeypatch.setattr(autotune_entrypoint, "prepare", lambda: {"action": "disabled"})
    monkeypatch.setattr(
        autotune_entrypoint.os, "execvp", lambda _, args: captured.extend(args)
    )
    assert autotune_entrypoint.main() == 0
    assert captured[captured.index("--port") + 1] == "8020"
    assert captured[captured.index("--timeout-graceful-shutdown") + 1] == "10"
