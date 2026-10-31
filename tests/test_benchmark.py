"""Verify benchmark sampling, metrics, resumability and result naming."""

from __future__ import annotations

import asyncio
import hashlib
import json
from argparse import Namespace
from pathlib import Path

import httpx
import pytest
import yaml

import benchmark
from benchmark import (
    Sample,
    _real5_output_name,
    parse_vllm_metrics,
    prepare_split,
    stratified_tuning_names,
    summarize_run,
    validate_run_args,
    write_eval_config,
)


def _samples(root: Path, count: int = 20) -> list[Sample]:
    return [
        Sample(
            image_name=f"page-{index}.png",
            image_path=root / f"page-{index}.png",
            subset="base" if index < 15 else "hard",
            language="english" if index % 2 else "chinese",
            layout="single" if index % 3 else "double",
            data_source="book" if index % 4 else "paper",
        )
        for index in range(count)
    ]


def test_stratified_split_is_deterministic_and_disjoint(tmp_path: Path) -> None:
    samples = _samples(tmp_path)
    first = stratified_tuning_names(samples, 7, "seed")
    second = stratified_tuning_names(list(reversed(samples)), 7, "seed")
    holdout = {
        sample.image_name for sample in samples if sample.image_name not in first
    }
    assert first == second
    assert len(first) == 7
    assert first.isdisjoint(holdout)
    assert first | holdout == {sample.image_name for sample in samples}


def test_prepare_split_validates_and_writes_manifest(tmp_path: Path) -> None:
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    annotations = []
    for sample in _samples(image_dir, 10):
        sample.image_path.write_bytes(b"image")
        annotations.append(
            {
                "page_info": {
                    "image_path": sample.image_name,
                    "page_attribute": {
                        "subset": sample.subset,
                        "language": sample.language,
                        "layout": sample.layout,
                        "data_source": sample.data_source,
                    },
                }
            }
        )
    (tmp_path / "OmniDocBench.json").write_text(json.dumps(annotations))
    output = tmp_path / "split.json"
    manifest = prepare_split(tmp_path, output, 3, "seed", "revision")
    assert manifest["counts"] == {"full": 10, "tuning": 3, "holdout": 7}
    assert set(manifest["tuning"]).isdisjoint(manifest["holdout"])
    assert output.is_file()


def test_summarize_uses_latest_record_per_image(tmp_path: Path) -> None:
    (tmp_path / "predictions").mkdir()
    (tmp_path / "responses").mkdir()
    for name in ("a", "b"):
        (tmp_path / "predictions" / f"{name}.md").write_text("content")
        (tmp_path / "responses" / f"{name}.json").write_text(
            json.dumps({"pages": [{"markdown": "content"}]})
        )
    (tmp_path / "run.json").write_text(
        json.dumps({"selected": 2, "wall_seconds": 4.0, "gpu_run": {"samples": 1}})
    )
    records = [
        {"image": "a.png", "status": 500, "elapsed_seconds": 2, "error": "old"},
        {"image": "a.png", "status": 200, "elapsed_seconds": 1, "error": None},
        {"image": "b.png", "status": 200, "elapsed_seconds": 3, "error": None},
    ]
    (tmp_path / "requests.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records)
    )
    summary = summarize_run(tmp_path)
    assert summary["successful"] == 2
    assert summary["failed"] == 0
    assert summary["pages_per_minute"] == 30.0
    assert summary["latency_seconds"]["p95"] == 3.0


def test_eval_config_can_disable_cdm(tmp_path: Path) -> None:
    output = tmp_path / "eval.yaml"
    predictions = tmp_path / "predictions"
    predictions.mkdir()
    (predictions / "page-1.md").write_text("prediction", encoding="utf-8")
    ground_truth = tmp_path / "ground-truth.json"
    ground_truth.write_text(
        json.dumps(
            [
                {"page_info": {"image_path": "images/page-1.png"}},
                {"page_info": {"image_path": "images/page-2.png"}},
            ]
        ),
        encoding="utf-8",
    )

    write_eval_config(
        ground_truth,
        predictions,
        output,
        include_cdm=False,
    )

    config = yaml.safe_load(output.read_text(encoding="utf-8"))["end2end_eval"]
    assert config["metrics"]["display_formula"] == {"metric": ["Edit_dist"]}
    assert (
        config["dataset"]["prediction"]["data_path"] == predictions.resolve().as_posix()
    )
    subset = json.loads((tmp_path / "ground-truth-subset.json").read_text())
    assert subset == [{"page_info": {"image_path": "images/page-1.png"}}]


def test_parse_vllm_metrics_aggregates_label_series():
    metrics = """
# HELP vllm:request_success_total Completed requests.
vllm:request_success_total{finished_reason="stop"} 4
vllm:request_success_total{finished_reason="length"} 1
vllm:prompt_tokens_total{engine="0"} 123
vllm:kv_cache_usage_perc{engine="0"} 0.5
"""

    parsed = parse_vllm_metrics(metrics)

    assert parsed["vllm:request_success_total"] == 5.0
    assert parsed["vllm:prompt_tokens_total"] == 123.0
    assert "vllm:kv_cache_usage_perc" not in parsed


def test_real5_output_names_are_short_stable_and_condition_specific():
    source = "a very long unicode document 中文_page_001.png"

    first = _real5_output_name("skew", source)
    second = _real5_output_name("warping", source)

    assert first == _real5_output_name("skew", source)
    assert first.startswith("skew__")
    assert first.endswith(".png")
    assert first != second
    assert len(first) < 40


def run_args(**updates):
    return Namespace(
        **(
            {
                "concurrency": 1,
                "limit": 0,
                "warmup": 0,
                "timeout": 30.0,
                "gpu_sample_interval": 0.5,
                "dpi": 200,
                "source_dpi": 200,
            }
            | updates
        )
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("concurrency", 0),
        ("limit", -1),
        ("warmup", -1),
        ("timeout", 0),
        ("timeout", float("nan")),
        ("gpu_sample_interval", float("inf")),
        ("dpi", 0),
        ("source_dpi", -1),
    ],
)
@pytest.mark.asyncio
async def test_invalid_run_arguments_fail_before_io(key, value):
    args = run_args(**{key: value})
    with pytest.raises(ValueError, match=key):
        await benchmark.run_benchmark(args)


def test_zero_limit_means_no_limit():
    validate_run_args(run_args(limit=0))


@pytest.fixture
def benchmark_env(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset"
    images = dataset / "images"
    images.mkdir(parents=True)
    samples = _samples(images, 2)
    annotations = []
    for sample in samples:
        sample.image_path.write_bytes(b"image")
        annotations.append({"page_info": {"image_path": sample.image_name}})
    annotation = dataset / "OmniDocBench.json"
    annotation.write_text(json.dumps(annotations), encoding="utf-8")
    split = tmp_path / "split.json"
    split.write_text(
        json.dumps(
            {
                "annotation_sha256": hashlib.sha256(
                    annotation.read_bytes()
                ).hexdigest(),
                "source_revision": "fake",
            }
        ),
        encoding="utf-8",
    )
    args = run_args(
        dataset_root=str(dataset),
        split_manifest=str(split),
        split="full",
        output_dir=str(tmp_path / "run"),
        url="http://api",
        token="",
        input_format="image",
        order_seed=1,
        vllm_metrics_url="http://api/metrics",
        resume=True,
    )
    monitors = []

    class Monitor:
        def __init__(self, _interval):
            self.started = False
            self.stopped = False
            monitors.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True
            return {"samples": 0}

    monkeypatch.setattr(benchmark, "GpuMonitor", Monitor)
    monkeypatch.setattr(benchmark, "_nvidia_snapshot", lambda: {"available": False})
    return args, monitors


def mock_http(monkeypatch, post):
    factory = httpx.AsyncClient

    async def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        if request.url.path == "/metrics":
            return httpx.Response(200, text="")
        return await post(request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: factory(transport=transport, **kwargs)
    )


@pytest.mark.asyncio
async def test_empty_valid_ocr_is_completed_and_resume_skips_it(
    benchmark_env, monkeypatch
):
    args, monitors = benchmark_env
    calls = []

    async def post(request):
        calls.append(request)
        return httpx.Response(200, json={"pages": [{"markdown": ""}]})

    mock_http(monkeypatch, post)
    await benchmark.run_benchmark(args)
    await benchmark.run_benchmark(args)
    assert len(calls) == 2
    assert all(monitor.stopped for monitor in monitors)
    records = benchmark._request_records(Path(args.output_dir) / "requests.jsonl")
    assert all(record["completed"] for record in records.values())
    assert benchmark.summarize_run(Path(args.output_dir))["successful"] == 2


@pytest.mark.asyncio
async def test_failed_page_in_http_200_is_retried(benchmark_env, monkeypatch):
    args, _monitors = benchmark_env
    calls = []

    async def post(request):
        calls.append(request)
        return httpx.Response(
            200, json={"pages": [{"markdown": "", "error": "page_failed"}]}
        )

    mock_http(monkeypatch, post)
    await benchmark.run_benchmark(args)
    await benchmark.run_benchmark(args)
    assert len(calls) == 4
    records = benchmark._request_records(Path(args.output_dir) / "requests.jsonl")
    assert all(not record["completed"] for record in records.values())
    assert benchmark.summarize_run(Path(args.output_dir))["successful"] == 0


def test_interrupted_artifact_pair_cannot_mark_completion(tmp_path, monkeypatch):
    (tmp_path / "predictions").mkdir()
    (tmp_path / "responses").mkdir()
    record = {"image": "x.png", "status": 200, "error": None}
    payload = {"pages": [{"markdown": "content"}]}
    original = benchmark._atomic_write_text

    def fail_response(path, text):
        if path.suffix == ".json":
            raise OSError("disk full")
        original(path, text)

    monkeypatch.setattr(benchmark, "_atomic_write_text", fail_response)
    with pytest.raises(OSError, match="disk full"):
        benchmark._persist_sample(tmp_path, record, payload)
    assert record["completed"] is False
    assert not benchmark._sample_completed(tmp_path, record)
    assert not benchmark._sample_completed(tmp_path, {"image": "x.png", "status": 200})


@pytest.mark.asyncio
async def test_cancelled_run_drains_requests_and_stops_monitor(
    benchmark_env, monkeypatch
):
    args, monitors = benchmark_env
    entered = asyncio.Event()
    active = []

    async def post(_request):
        active.append(True)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            active.pop()

    mock_http(monkeypatch, post)
    task = asyncio.create_task(benchmark.run_benchmark(args))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not active
    assert monitors[0].stopped


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["open", "flush"])
async def test_record_io_failure_stops_monitor(benchmark_env, monkeypatch, failure):
    args, monitors = benchmark_env

    async def post(_request):
        return httpx.Response(200, json={"pages": [{"markdown": "content"}]})

    mock_http(monkeypatch, post)
    original = Path.open

    class BrokenStream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def write(self, _value):
            pass

        def flush(self):
            raise OSError("flush failed")

    def broken_open(path, mode="r", *positional, **kwargs):
        if path.name == "requests.jsonl" and mode == "a":
            if failure == "open":
                raise OSError("open failed")
            return BrokenStream()
        return original(path, mode, *positional, **kwargs)

    monkeypatch.setattr(Path, "open", broken_open)
    with pytest.raises(OSError, match=failure):
        await benchmark.run_benchmark(args)
    assert monitors[0].stopped


@pytest.mark.asyncio
async def test_error_record_preserves_input_format_and_dpi(tmp_path):
    sample = _samples(tmp_path, 1)[0]
    async with httpx.AsyncClient() as client:
        record, payload = await benchmark._request_sample(
            client,
            asyncio.Semaphore(1),
            "http://api",
            sample,
            30,
            "pdf",
            144,
            180,
        )
    assert payload is None
    assert record["input_format"] == "pdf"
    assert record["dpi"] == 144
    assert record["source_dpi"] == 180
    assert record["completed"] is False


@pytest.mark.asyncio
async def test_resume_incompatible_options_are_rejected(benchmark_env, monkeypatch):
    args, _monitors = benchmark_env

    async def post(_request):
        return httpx.Response(200, json={"pages": [{"markdown": "content"}]})

    mock_http(monkeypatch, post)
    await benchmark.run_benchmark(args)
    args.concurrency = 2
    with pytest.raises(ValueError, match="resume settings differ"):
        await benchmark.run_benchmark(args)
