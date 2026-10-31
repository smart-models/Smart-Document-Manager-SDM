"""Administrative RAG guards are separate from existing OCR capacity."""

import pytest

from ocrserver.config import Settings, load_settings


def test_chunk_capacity_defaults_match_the_prd():
    settings = load_settings({}, None)
    assert settings.chunk_max_workers == 2
    assert settings.chunk_queue_wait_seconds == 0.5
    assert settings.chunk_timeout_seconds == 120
    assert settings.chunk_max_input_bytes == 33_554_432
    assert settings.chunk_max_output_bytes == 67_108_864
    assert settings.chunk_max_units == 20_000
    assert settings.chunk_max_dp_edges == 320_000
    assert settings.chunk_max_token_evaluations == 1_100_000
    assert settings.chunk_max_rendered_bytes == 268_435_456


def test_chunk_environment_overrides_do_not_change_ocr_capacity():
    settings = load_settings(
        {
            "OCR_CHUNK_MAX_WORKERS": "3",
            "OCR_CHUNK_MAX_TOKENS": "2048",
            "OCR_CHUNK_QUEUE_WAIT_SECONDS": "0.1",
            "OCR_CHUNK_TIMEOUT_SECONDS": "15.5",
        },
        {"decisions": {"chunk_max_workers": 99}},
    )
    assert settings.chunk_max_workers == 3
    assert settings.chunk_max_tokens == 2048
    assert settings.chunk_queue_wait_seconds == 0.1
    assert settings.chunk_timeout_seconds == 15.5
    assert settings.max_queue_size == Settings().max_queue_size


@pytest.mark.parametrize(
    "changes",
    [
        {"chunk_max_workers": 0},
        {"chunk_max_input_bytes": True},
        {"chunk_max_output_bytes": 0},
        {"chunk_max_units": 1.5},
        {"chunk_max_tokens": 32769},
        {"chunk_timeout_seconds": 0},
        {"chunk_timeout_seconds": float("inf")},
        {"chunk_queue_wait_seconds": float("nan")},
        {"chunk_queue_wait_seconds": -0.1},
    ],
)
def test_invalid_chunk_capacity_is_rejected_before_starting_workers(changes):
    with pytest.raises(ValueError):
        Settings(**changes)
