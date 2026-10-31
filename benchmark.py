"""Reproducible quality and performance harness for Personal Smart OCR.

The harness deliberately separates a small, deterministic tuning split from the
holdout used to validate a chosen configuration. It records raw API responses,
official-evaluator Markdown, latency, API timing, and sampled NVIDIA GPU usage.
Runs are resumable and contain enough environment metadata to compare profiles.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import io
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

DEFAULT_DATASET_ROOT = Path("benchmarks/datasets/omnidocbench")
DEFAULT_SPLIT_MANIFEST = Path("benchmarks/manifests/omnidocbench_split.json")
DEFAULT_SOURCE_REVISION = "aa1ee96d106dbe53d0ae59474d75c6e6d9b53fec"
REAL5_REPOSITORY = "PaddlePaddle/Real5-OmniDocBench"
REAL5_REVISION = "c6428e836c22cc0c10b9dd34d47709f78190478b"
REAL5_CONDITIONS = {
    "illumination": "Real5-OmniDocBench-Illumination",
    "scanning": "Real5-OmniDocBench-Scanning",
    "screen": "Real5-OmniDocBench-Screen-Photography",
    "skew": "Real5-OmniDocBench-Skew",
    "warping": "Real5-OmniDocBench-Warping",
}
DEFAULT_TUNING_SIZE = 160
SCHEMA_VERSION = 1
VLLM_METRICS = {
    "vllm:num_preemptions_total",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
    "vllm:request_success_total",
    "vllm:time_to_first_token_seconds_count",
    "vllm:time_to_first_token_seconds_sum",
    "vllm:e2e_request_latency_seconds_count",
    "vllm:e2e_request_latency_seconds_sum",
    "vllm:request_queue_time_seconds_sum",
    "vllm:request_inference_time_seconds_sum",
}


@dataclass(frozen=True)
class Sample:
    """Identify one dataset page and its sampling attributes."""

    image_name: str
    image_path: Path
    subset: str
    language: str
    layout: str
    data_source: str

    @property
    def stratum(self) -> tuple[str, str, str, str]:
        """Return the subset/language/layout/source key used for stratification."""
        return self.subset, self.language, self.layout, self.data_source


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_key(seed: str, value: str) -> str:
    return hashlib.sha256(f"{seed}:{value}".encode()).hexdigest()


def _safe_print(value: str) -> None:
    """Print arbitrary dataset names on legacy Windows console encodings."""
    encoding = sys.stdout.encoding or "utf-8"
    printable = value.encode(encoding, errors="backslashreplace").decode(encoding)
    print(printable, flush=True)


def load_samples(dataset_root: Path) -> list[Sample]:
    """Read OmniDocBench annotations into ordered image paths and sample metadata."""
    annotation_path = dataset_root / "OmniDocBench.json"
    annotations = json.loads(annotation_path.read_text(encoding="utf-8"))
    samples: list[Sample] = []
    for item in annotations:
        page = item["page_info"]
        attrs = page.get("page_attribute", {})
        image_name = page["image_path"]
        samples.append(
            Sample(
                image_name=image_name,
                image_path=dataset_root / "images" / image_name,
                subset=str(attrs.get("subset", "unknown")),
                language=str(attrs.get("language", "unknown")),
                layout=str(attrs.get("layout", "unknown")),
                data_source=str(attrs.get("data_source", "unknown")),
            )
        )
    return samples


def verify_dataset(dataset_root: Path) -> dict[str, Any]:
    """Check dataset files and return annotation hashes and distribution counts."""
    annotation_path = dataset_root / "OmniDocBench.json"
    if not annotation_path.is_file():
        raise FileNotFoundError(f"missing annotation file: {annotation_path}")
    samples = load_samples(dataset_root)
    missing = [
        sample.image_name for sample in samples if not sample.image_path.is_file()
    ]
    image_dir = dataset_root / "images"
    actual_images = [path for path in image_dir.iterdir() if path.is_file()]
    annotated = {sample.image_name for sample in samples}
    extras = sorted(path.name for path in actual_images if path.name not in annotated)
    report = {
        "schema_version": SCHEMA_VERSION,
        "dataset_root": str(dataset_root.resolve()),
        "annotation_sha256": _sha256(annotation_path),
        "annotations": len(samples),
        "images": len(actual_images),
        "image_bytes": sum(path.stat().st_size for path in actual_images),
        "missing_images": missing,
        "extra_images": extras,
        "subsets": dict(sorted(Counter(s.subset for s in samples).items())),
        "languages": dict(sorted(Counter(s.language for s in samples).items())),
        "layouts": dict(sorted(Counter(s.layout for s in samples).items())),
    }
    if missing or extras or len(samples) != len(actual_images):
        raise ValueError(json.dumps(report, indent=2))
    return report


def stratified_tuning_names(
    samples: list[Sample], tuning_size: int, seed: str
) -> set[str]:
    """Select a stable proportional sample across benchmark attributes."""
    if not 0 < tuning_size < len(samples):
        raise ValueError("tuning_size must be between 1 and dataset_size - 1")
    buckets: dict[tuple[str, str, str, str], list[Sample]] = defaultdict(list)
    for sample in samples:
        buckets[sample.stratum].append(sample)
    for bucket in buckets.values():
        bucket.sort(key=lambda s: _stable_key(seed, s.image_name))

    allocations: dict[tuple[str, str, str, str], int] = {}
    remainders: list[tuple[float, str, tuple[str, str, str, str]]] = []
    total = len(samples)
    for stratum, bucket in buckets.items():
        exact = len(bucket) * tuning_size / total
        allocations[stratum] = math.floor(exact)
        remainders.append(
            (exact - math.floor(exact), _stable_key(seed, repr(stratum)), stratum)
        )

    remaining = tuning_size - sum(allocations.values())
    for _, _, stratum in sorted(remainders, reverse=True):
        if remaining <= 0:
            break
        if allocations[stratum] < len(buckets[stratum]):
            allocations[stratum] += 1
            remaining -= 1

    if remaining:
        candidates = [
            sample
            for stratum, bucket in buckets.items()
            for sample in bucket[allocations[stratum] :]
        ]
        candidates.sort(key=lambda s: _stable_key(seed, s.image_name))
        for sample in candidates[:remaining]:
            allocations[sample.stratum] += 1

    return {
        sample.image_name
        for stratum, count in allocations.items()
        for sample in buckets[stratum][:count]
    }


def prepare_split(
    dataset_root: Path,
    output_path: Path,
    tuning_size: int,
    seed: str,
    source_revision: str,
) -> dict[str, Any]:
    """Verify the dataset and write a deterministic tuning/holdout split manifest."""
    verification = verify_dataset(dataset_root)
    samples = load_samples(dataset_root)
    tuning = stratified_tuning_names(samples, tuning_size, seed)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "source": "opendatalab/OmniDocBench",
        "source_revision": source_revision,
        "annotation_sha256": verification["annotation_sha256"],
        "seed": seed,
        "tuning": sorted(tuning),
        "holdout": sorted(s.image_name for s in samples if s.image_name not in tuning),
        "counts": {
            "full": len(samples),
            "tuning": len(tuning),
            "holdout": len(samples) - len(tuning),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _real5_output_name(condition: str, source_name: str) -> str:
    digest = hashlib.sha256(source_name.encode("utf-8")).hexdigest()[:16]
    suffix = Path(source_name).suffix.lower()
    return f"{condition}__{digest}{suffix}"


def prepare_real5_subset(
    base_dataset_root: Path,
    output_root: Path,
    split_output: Path,
    repo_cache: Path,
    base_count: int,
    seed: str,
    revision: str,
) -> dict[str, Any]:
    """Download a deterministic cross-condition Real5 robustness subset."""
    try:
        from huggingface_hub import HfApi, snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "prepare-real5 requires huggingface-hub in the host environment"
        ) from exc

    if base_count < 1:
        raise ValueError("base_count must be positive")
    base_samples = load_samples(base_dataset_root)
    base_annotations = json.loads(
        (base_dataset_root / "OmniDocBench.json").read_text(encoding="utf-8")
    )
    annotations_by_name = {
        item["page_info"]["image_path"]: item for item in base_annotations
    }

    repo_files = HfApi().list_repo_files(
        REAL5_REPOSITORY,
        repo_type="dataset",
        revision=revision,
    )
    condition_names = {
        code: {
            path.split("/", 1)[1]
            for path in repo_files
            if path.startswith(f"{directory}/") and "/" in path
        }
        for code, directory in REAL5_CONDITIONS.items()
    }
    available = [
        sample
        for sample in base_samples
        if all(sample.image_name in names for names in condition_names.values())
    ]
    if base_count > len(available):
        raise ValueError(
            f"requested {base_count} base pages, only {len(available)} are common"
        )
    if base_count == len(available):
        selected_names = {sample.image_name for sample in available}
    else:
        selected_names = stratified_tuning_names(available, base_count, seed)
    selected = sorted(
        (sample for sample in available if sample.image_name in selected_names),
        key=lambda sample: _stable_key(seed, sample.image_name),
    )

    remote_paths = [
        f"{directory}/{sample.image_name}"
        for sample in selected
        for directory in REAL5_CONDITIONS.values()
    ]
    upstream_root = Path(
        snapshot_download(
            repo_id=REAL5_REPOSITORY,
            repo_type="dataset",
            revision=revision,
            local_dir=repo_cache,
            allow_patterns=remote_paths + ["README.md", "LICENSE", "eval.yaml"],
            max_workers=4,
        )
    )

    annotation_path = output_root / "OmniDocBench.json"
    if annotation_path.exists():
        raise FileExistsError(
            f"{annotation_path} already exists; choose a new output directory"
        )
    images_dir = output_root / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    generated_annotations: list[dict[str, Any]] = []
    mappings: list[dict[str, str]] = []
    for sample in selected:
        for condition, directory in REAL5_CONDITIONS.items():
            output_name = _real5_output_name(condition, sample.image_name)
            source_path = upstream_root / directory / sample.image_name
            destination = images_dir / output_name
            shutil.copy2(source_path, destination)

            annotation = copy.deepcopy(annotations_by_name[sample.image_name])
            annotation["page_info"]["image_path"] = output_name
            attributes = annotation["page_info"].setdefault("page_attribute", {})
            attributes["real5_condition"] = condition
            generated_annotations.append(annotation)
            mappings.append(
                {
                    "condition": condition,
                    "source_name": sample.image_name,
                    "output_name": output_name,
                    "remote_path": f"{directory}/{sample.image_name}",
                }
            )

    annotation_path.write_text(
        json.dumps(generated_annotations, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for source_name, destination_name in (
        ("README.md", "UPSTREAM_README.md"),
        ("LICENSE", "LICENSE"),
        ("eval.yaml", "UPSTREAM_EVAL.yaml"),
    ):
        source = upstream_root / source_name
        if source.is_file():
            shutil.copy2(source, output_root / destination_name)

    verification = verify_dataset(output_root)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "source": f"https://huggingface.co/datasets/{REAL5_REPOSITORY}",
        "source_revision": revision,
        "annotation_sha256": verification["annotation_sha256"],
        "seed": seed,
        "base_pages": [sample.image_name for sample in selected],
        "conditions": REAL5_CONDITIONS,
        "mappings": mappings,
        "tuning": [],
        "holdout": [],
        "counts": {
            "base_pages": len(selected),
            "conditions": len(REAL5_CONDITIONS),
            "full": len(generated_annotations),
        },
    }
    output_root.joinpath("download-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    split_output.parent.mkdir(parents=True, exist_ok=True)
    split_output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "verification": verification,
        "counts": manifest["counts"],
        "manifest": str(split_output.resolve()),
    }


def select_samples(
    samples: list[Sample], manifest: dict[str, Any], split: str
) -> list[Sample]:
    """Keep dataset order while selecting a split, rejecting missing image names."""
    if split == "full":
        return list(samples)
    names = set(manifest[split])
    selected = [sample for sample in samples if sample.image_name in names]
    if len(selected) != len(names):
        raise ValueError(f"split {split!r} references missing dataset images")
    return selected


def _nvidia_snapshot() -> dict[str, Any]:
    command = [
        "nvidia-smi",
        "--query-gpu=index,name,uuid,memory.total,driver_version,compute_cap",
        "--format=csv,noheader,nounits",
    ]
    try:
        value = subprocess.run(
            command, capture_output=True, text=True, timeout=10, check=True
        )
        return {"available": True, "csv": value.stdout.strip()}
    except (OSError, subprocess.SubprocessError):
        return {"available": False}


class GpuMonitor:
    """Sample NVIDIA utilization in a daemon thread for benchmark summaries."""

    def __init__(self, interval: float = 0.5) -> None:
        """Configure the sampling interval without starting the polling thread."""
        self.interval = interval
        self.samples: list[dict[str, float]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start background GPU polling without blocking benchmark requests."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, float]:
        """Stop polling within a bounded wait and summarize available samples."""
        self._stop.set()
        if self._thread:
            # nvidia-smi has a five-second timeout; leave time for its cleanup.
            self._thread.join(timeout=6.0)
        if not self.samples:
            return {"samples": 0, "peak_memory_mib": 0.0, "mean_utilization_pct": 0.0}
        return {
            "samples": len(self.samples),
            "peak_memory_mib": max(s["memory_mib"] for s in self.samples),
            "mean_utilization_pct": statistics.fmean(
                s["utilization_pct"] for s in self.samples
            ),
        }

    def _run(self) -> None:
        command = [
            "nvidia-smi",
            "--query-gpu=memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ]
        while not self._stop.is_set():
            try:
                value = subprocess.run(
                    command, capture_output=True, text=True, timeout=5, check=True
                ).stdout.splitlines()[0]
                memory, utilization = (float(part.strip()) for part in value.split(","))
                self.samples.append(
                    {"memory_mib": memory, "utilization_pct": utilization}
                )
            except (OSError, subprocess.SubprocessError, ValueError, IndexError):
                pass
            self._stop.wait(self.interval)


def parse_vllm_metrics(text: str) -> dict[str, float]:
    """Aggregate selected Prometheus samples across labels."""
    values: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or " " not in line:
            continue
        sample, raw_value = line.rsplit(None, 1)
        name = sample.split("{", 1)[0]
        if name not in VLLM_METRICS:
            continue
        try:
            values[name] = values.get(name, 0.0) + float(raw_value)
        except ValueError:
            continue
    return values


async def _fetch_vllm_snapshot(
    client: httpx.AsyncClient, metrics_url: str
) -> dict[str, float]:
    try:
        response = await client.get(metrics_url, timeout=10)
        response.raise_for_status()
        return parse_vllm_metrics(response.text)
    except httpx.HTTPError:
        return {}


def _counter_delta(
    before: dict[str, float], after: dict[str, float]
) -> dict[str, float]:
    return {
        name: after[name] - before.get(name, 0.0)
        for name in sorted(after)
        if name in VLLM_METRICS
    }


def _add_metric_sets(*sets: dict[str, float]) -> dict[str, float]:
    names = {name for values in sets for name in values}
    return {
        name: sum(float(values.get(name, 0.0)) for values in sets) for name in names
    }


def _combine_gpu_runs(*runs: dict[str, float]) -> dict[str, float]:
    valid = [run for run in runs if int(run.get("samples", 0)) > 0]
    total_samples = sum(int(run["samples"]) for run in valid)
    if not total_samples:
        return {"samples": 0, "peak_memory_mib": 0.0, "mean_utilization_pct": 0.0}
    return {
        "samples": total_samples,
        "peak_memory_mib": max(float(run["peak_memory_mib"]) for run in valid),
        "mean_utilization_pct": sum(
            float(run["mean_utilization_pct"]) * int(run["samples"]) for run in valid
        )
        / total_samples,
    }


async def _request_sample(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    base_url: str,
    sample: Sample,
    timeout: float,
    input_format: str = "image",
    dpi: int = 200,
    source_dpi: int = 200,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Submit one admitted benchmark page and retain a timed outcome record.

    Optionally wrap the image as a single-page PDF using explicit source/render
    DPI. Record HTTP/payload failures and return them with available metadata so
    one failed sample cannot abort the benchmark's remaining requests.
    """
    async with semaphore:
        started = time.perf_counter()
        record = {
            "image": sample.image_name,
            "status": 0,
            "completed": False,
            "api_processing_ms": None,
            "subset": sample.subset,
            "language": sample.language,
            "layout": sample.layout,
            "data_source": sample.data_source,
            "input_format": input_format,
            "dpi": dpi if input_format == "pdf" else None,
            "source_dpi": source_dpi if input_format == "pdf" else None,
        }
        try:
            if input_format == "pdf":
                with Image.open(sample.image_path) as source:
                    page = source.convert("RGB")
                    buffer = io.BytesIO()
                    page.save(buffer, format="PDF", resolution=float(source_dpi))
                    page.close()
                    file_name = f"{Path(sample.image_name).stem}.pdf"
                    file_bytes = buffer.getvalue()
                    media_type = "application/pdf"
            else:
                file_name = sample.image_name
                file_bytes = sample.image_path.read_bytes()
                media_type = (
                    "image/png"
                    if sample.image_path.suffix.lower() == ".png"
                    else "image/jpeg"
                )
            form_data = {
                "output": "both",
                "table_format": "html",
                "include_images": "false",
            }
            if input_format == "pdf":
                form_data["dpi"] = str(dpi)
            response = await client.post(
                f"{base_url}/parse",
                files={"file": (file_name, file_bytes, media_type)},
                data=form_data,
                timeout=timeout,
            )
            record["status"] = response.status_code
            payload = response.json() if response.content else None
            record.update(
                {
                    "elapsed_seconds": time.perf_counter() - started,
                    "api_processing_ms": payload.get("processing_time_ms")
                    if isinstance(payload, dict)
                    else None,
                    "error": _payload_error(payload)
                    if response.status_code == 200
                    else str(payload),
                }
            )
            return record, payload
        except Exception as exc:  # noqa: BLE001 - benchmark must record and continue
            record.update(
                {
                    "elapsed_seconds": time.perf_counter() - started,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return record, None


def _payload_error(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return "Response must be an object"
    pages = payload.get("pages")
    if not isinstance(pages, list) or not pages:
        return "Response must contain a nonempty pages list"
    if any(not isinstance(page, dict) for page in pages):
        return "Response contains invalid pages"
    if any(page.get("error") for page in pages):
        return "Response contains failed pages"
    if any(not isinstance(page.get("markdown"), str) for page in pages):
        return "Response contains invalid page Markdown"
    return None


def _markdown_from_payload(payload: dict[str, Any]) -> str:
    error = _payload_error(payload)
    if error:
        raise ValueError(error)
    return "\n\n".join(page["markdown"] for page in payload["pages"])


def validate_run_args(args: argparse.Namespace) -> None:
    """Reject invalid benchmark limits before filesystem or network activity."""
    for key, minimum in (
        ("concurrency", 1),
        ("limit", 0),
        ("warmup", 0),
        ("dpi", 1),
        ("source_dpi", 1),
    ):
        value = getattr(args, key)
        if type(value) is not int or value < minimum:
            raise ValueError(f"{key} must be an integer >= {minimum}")
    for key in ("timeout", "gpu_sample_interval"):
        value = getattr(args, key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{key} must be finite and positive")


def _atomic_write_text(path: Path, text: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Keep the original write/replace error if cleanup also fails.
                pass


def _request_records(path: Path) -> dict[str, dict[str, Any]]:
    records = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                _safe_print("Ignoring an incomplete benchmark request record")
                continue
            if isinstance(record, dict) and isinstance(record.get("image"), str):
                records[record["image"]] = record
    return records


def _sample_completed(run_dir: Path, record: dict[str, Any]) -> bool:
    """Verify recorded completion and matching artifacts, including empty OCR."""
    if record.get("status") != 200 or record.get("error"):
        return False
    if "completed" in record and record["completed"] is not True:
        return False
    stem = Path(record["image"]).stem
    prediction = run_dir / "predictions" / f"{stem}.md"
    response = run_dir / "responses" / f"{stem}.json"
    try:
        payload = json.loads(response.read_text(encoding="utf-8"))
        if _payload_error(payload):
            return False
        if prediction.read_text(encoding="utf-8") != _markdown_from_payload(payload):
            return False
        for key, path in (
            ("prediction_sha256", prediction),
            ("response_sha256", response),
        ):
            if "completed" in record and record.get(key) != _sha256(path):
                return False
        return True
    except (OSError, ValueError):
        return False


def _persist_sample(run_dir: Path, record: dict, payload: object) -> None:
    record["completed"] = False
    if record["status"] != 200 or record.get("error") or _payload_error(payload):
        return
    stem = Path(record["image"]).stem
    prediction = run_dir / "predictions" / f"{stem}.md"
    response = run_dir / "responses" / f"{stem}.json"
    _atomic_write_text(prediction, _markdown_from_payload(payload))
    _atomic_write_text(response, json.dumps(payload, ensure_ascii=False, indent=2))
    record["prediction_sha256"] = _sha256(prediction)
    record["response_sha256"] = _sha256(response)
    record["completed"] = True


async def run_benchmark(args: argparse.Namespace) -> int:
    """Run or resume verified dataset requests and persist benchmark evidence.

    Validate arguments and annotation identity before sending requests. Retain
    raw responses, prediction Markdown, timings and measured runtime metadata.
    """
    validate_run_args(args)
    dataset_root = Path(args.dataset_root)
    split_manifest = json.loads(Path(args.split_manifest).read_text(encoding="utf-8"))
    if (
        _sha256(dataset_root / "OmniDocBench.json")
        != split_manifest["annotation_sha256"]
    ):
        raise ValueError("dataset annotation hash does not match split manifest")
    samples = select_samples(load_samples(dataset_root), split_manifest, args.split)
    samples.sort(
        key=lambda sample: _stable_key(str(args.order_seed), sample.image_name)
    )
    if args.limit:
        samples = samples[: args.limit]

    run_dir = Path(args.output_dir)
    predictions_dir = run_dir / "predictions"
    responses_dir = run_dir / "responses"
    predictions_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = run_dir / "requests.jsonl"
    completed = (
        {
            name
            for name, record in _request_records(metrics_path).items()
            if _sample_completed(run_dir, record)
        }
        if args.resume
        else set()
    )
    pending = [sample for sample in samples if sample.image_name not in completed]
    previous_run: dict[str, Any] = {}
    run_path = run_dir / "run.json"
    if args.resume and run_path.exists():
        previous_run = json.loads(run_path.read_text(encoding="utf-8"))
        comparable = {
            "split": args.split,
            "concurrency": args.concurrency,
            "input_format": args.input_format,
            "dpi": args.dpi if args.input_format == "pdf" else None,
            "source_dpi": args.source_dpi if args.input_format == "pdf" else None,
            "annotation_sha256": split_manifest["annotation_sha256"],
        }
        mismatched = {
            key: (previous_run.get(key), value)
            for key, value in comparable.items()
            if previous_run.get(key) != value
        }
        if mismatched:
            raise ValueError(f"resume settings differ from prior run: {mismatched}")

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    base_url = args.url.rstrip("/")
    async with httpx.AsyncClient(headers=headers) as client:
        health = (await client.get(f"{base_url}/health", timeout=30)).json()
        if health.get("status") != "healthy":
            raise RuntimeError(f"OCR API is not healthy: {health}")
        warmup_samples = samples[: min(args.warmup, len(samples))]
        warmup_sem = asyncio.Semaphore(1)
        for sample in warmup_samples:
            await _request_sample(
                client,
                warmup_sem,
                base_url,
                sample,
                args.timeout,
                args.input_format,
                args.dpi,
                args.source_dpi,
            )
        vllm_before = await _fetch_vllm_snapshot(client, args.vllm_metrics_url)

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "started_at": previous_run.get("started_at")
            or datetime.now(UTC).isoformat(),
            "dataset_revision": split_manifest["source_revision"],
            "annotation_sha256": split_manifest["annotation_sha256"],
            "split": args.split,
            "selected": len(samples),
            "pending_at_start": len(pending),
            "concurrency": args.concurrency,
            "warmup": len(warmup_samples),
            "input_format": args.input_format,
            "dpi": args.dpi if args.input_format == "pdf" else None,
            "source_dpi": args.source_dpi if args.input_format == "pdf" else None,
            "url": base_url,
            "health": health,
            "host": {
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
            "gpu": _nvidia_snapshot(),
            "vllm_metrics_before": vllm_before,
            "segments": list(previous_run.get("segments", [])),
        }
        if not manifest["segments"] and previous_run.get("wall_seconds"):
            manifest["segments"].append(
                {
                    "wall_seconds": previous_run["wall_seconds"],
                    "gpu_run": previous_run.get("gpu_run", {}),
                    "vllm_metrics_delta": previous_run.get("vllm_metrics_delta", {}),
                    "legacy": True,
                }
            )
        _atomic_write_text(run_path, json.dumps(manifest, indent=2))

        monitor = GpuMonitor(args.gpu_sample_interval)
        wall_start = time.perf_counter()
        semaphore = asyncio.Semaphore(args.concurrency)
        tasks = []
        try:
            monitor.start()
            tasks = [
                asyncio.create_task(
                    _request_sample(
                        client,
                        semaphore,
                        base_url,
                        sample,
                        args.timeout,
                        args.input_format,
                        args.dpi,
                        args.source_dpi,
                    )
                )
                for sample in pending
            ]
            with metrics_path.open("a", encoding="utf-8") as metrics:
                # Separate a possibly truncated last record before appending.
                metrics.write("\n")
                for task in asyncio.as_completed(tasks):
                    record, payload = await task
                    _persist_sample(run_dir, record, payload)
                    metrics.write(json.dumps(record, ensure_ascii=False) + "\n")
                    metrics.flush()
                    _safe_print(
                        f"[{record['status']}] {record['image']} "
                        f"{record['elapsed_seconds']:.2f}s"
                    )
            vllm_after = await _fetch_vllm_snapshot(client, args.vllm_metrics_url)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                segment_gpu = await asyncio.to_thread(monitor.stop)
        wall_seconds = time.perf_counter() - wall_start
        segment_delta = _counter_delta(vllm_before, vllm_after)
        manifest["segments"].append(
            {
                "finished_at": datetime.now(UTC).isoformat(),
                "pending_at_start": len(pending),
                "wall_seconds": wall_seconds,
                "gpu_run": segment_gpu,
                "vllm_metrics_delta": segment_delta,
            }
        )
        total_wall = sum(float(item["wall_seconds"]) for item in manifest["segments"])
        total_gpu = _combine_gpu_runs(
            *(item.get("gpu_run", {}) for item in manifest["segments"])
        )
        total_vllm = _add_metric_sets(
            *(item.get("vllm_metrics_delta", {}) for item in manifest["segments"])
        )
        manifest.update(
            {
                "finished_at": datetime.now(UTC).isoformat(),
                "wall_seconds": total_wall,
                "gpu_run": total_gpu,
                "vllm_metrics_after": vllm_after,
                "vllm_metrics_delta": total_vllm,
            }
        )
        _atomic_write_text(run_path, json.dumps(manifest, indent=2))

    print(json.dumps(summarize_run(run_dir), indent=2))
    return 0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def summarize_run(run_dir: Path) -> dict[str, Any]:
    """Write and return summary metrics from verified completed request records."""
    records = _request_records(run_dir / "requests.jsonl")
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    successful = [
        record for record in records.values() if _sample_completed(run_dir, record)
    ]
    latencies = [float(record["elapsed_seconds"]) for record in successful]
    wall_seconds = float(run.get("wall_seconds") or sum(latencies))
    vllm_delta = run.get("vllm_metrics_delta", {})
    request_count = float(vllm_delta.get("vllm:request_success_total", 0.0))
    ttft_count = float(vllm_delta.get("vllm:time_to_first_token_seconds_count", 0.0))
    e2e_count = float(vllm_delta.get("vllm:e2e_request_latency_seconds_count", 0.0))
    summary = {
        "selected": run.get("selected", 0),
        "recorded": len(records),
        "successful": len(successful),
        "failed": len(records) - len(successful),
        "success_rate": len(successful) / len(records) if records else 0.0,
        "wall_seconds": wall_seconds,
        "pages_per_minute": len(successful) * 60 / wall_seconds
        if wall_seconds
        else 0.0,
        "latency_seconds": {
            "mean": statistics.fmean(latencies) if latencies else 0.0,
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
            "max": max(latencies, default=0.0),
        },
        "gpu_run": run.get("gpu_run", {}),
        "vllm": {
            "block_requests": request_count,
            "prompt_tokens": vllm_delta.get("vllm:prompt_tokens_total", 0.0),
            "generation_tokens": vllm_delta.get("vllm:generation_tokens_total", 0.0),
            "generation_tokens_per_second": (
                float(vllm_delta.get("vllm:generation_tokens_total", 0.0))
                / wall_seconds
                if wall_seconds
                else 0.0
            ),
            "mean_ttft_seconds": (
                float(vllm_delta.get("vllm:time_to_first_token_seconds_sum", 0.0))
                / ttft_count
                if ttft_count
                else 0.0
            ),
            "mean_e2e_seconds": (
                float(vllm_delta.get("vllm:e2e_request_latency_seconds_sum", 0.0))
                / e2e_count
                if e2e_count
                else 0.0
            ),
            "preemptions": vllm_delta.get("vllm:num_preemptions_total", 0.0),
        },
        "failures": [
            {
                "image": record["image"],
                "status": record["status"],
                "error": record["error"],
            }
            for record in records.values()
            if not _sample_completed(run_dir, record)
        ],
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return summary


def write_eval_config(
    ground_truth: Path,
    predictions: Path,
    output: Path,
    *,
    include_cdm: bool = True,
    subset_ground_truth: bool = True,
) -> None:
    """Write evaluator configuration and optionally a prediction-matched dataset.

    Reject predictions without matching annotations before writing the subset.
    The include_cdm flag controls formula evaluation in the generated config.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    evaluation_ground_truth = ground_truth
    if subset_ground_truth:
        prediction_names = {path.stem for path in predictions.glob("*.md")}
        annotations = json.loads(ground_truth.read_text(encoding="utf-8"))
        selected_annotations = [
            item
            for item in annotations
            if Path(item.get("page_info", {}).get("image_path", "")).stem
            in prediction_names
        ]
        if len(selected_annotations) != len(prediction_names):
            matched = {
                Path(item.get("page_info", {}).get("image_path", "")).stem
                for item in selected_annotations
            }
            missing = sorted(prediction_names - matched)
            raise ValueError(f"predictions missing from ground truth: {missing[:5]}")
        evaluation_ground_truth = output.parent / "ground-truth-subset.json"
        evaluation_ground_truth.write_text(
            json.dumps(selected_annotations, ensure_ascii=False),
            encoding="utf-8",
        )
    formula_metrics = "[Edit_dist, CDM]" if include_cdm else "[Edit_dist]"
    cdm_workers = "\n      cdm_workers: 4" if include_cdm else ""
    text = f"""end2end_eval:
  metrics:
    text_block:
      metric: [Edit_dist]
    display_formula:
      metric: {formula_metrics}{cdm_workers}
    table:
      metric: [TEDS, Edit_dist]
      teds_workers: 4
    reading_order:
      metric: [Edit_dist]
  dataset:
    dataset_name: end2end_dataset
    ground_truth:
      data_path: {evaluation_ground_truth.resolve().as_posix()}
    prediction:
      data_path: {predictions.resolve().as_posix()}
    match_method: quick_match
    match_workers: 4
    quick_match_truncated_timeout_sec: 300
    match_timeout_sec: 420
    timeout_fallback_max_chunk_span: 10
    timeout_fallback_order_penalty: 0.10
"""
    output.write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    """Define dataset verification, split preparation and benchmark CLI commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser("verify", help="Validate an OmniDocBench download")
    verify.add_argument("--dataset-root", default=str(DEFAULT_DATASET_ROOT))

    prepare = subparsers.add_parser(
        "prepare", help="Create deterministic tuning/holdout splits"
    )
    prepare.add_argument("--dataset-root", default=str(DEFAULT_DATASET_ROOT))
    prepare.add_argument("--output", default=str(DEFAULT_SPLIT_MANIFEST))
    prepare.add_argument("--tuning-size", type=int, default=DEFAULT_TUNING_SIZE)
    prepare.add_argument("--seed", default="personal-smart-ocr-v1")
    prepare.add_argument("--source-revision", default=DEFAULT_SOURCE_REVISION)

    real5 = subparsers.add_parser(
        "prepare-real5",
        help="Download a deterministic five-condition Real5 robustness subset",
    )
    real5.add_argument("--base-dataset-root", default=str(DEFAULT_DATASET_ROOT))
    real5.add_argument(
        "--output-root",
        default="benchmarks/datasets/real5-subset",
    )
    real5.add_argument(
        "--split-output",
        default="benchmarks/manifests/real5_subset.json",
    )
    real5.add_argument(
        "--repo-cache",
        default="benchmarks/cache/real5-repository",
    )
    real5.add_argument("--base-count", type=int, default=10)
    real5.add_argument("--seed", default="personal-smart-ocr-real5-v1")
    real5.add_argument("--revision", default=REAL5_REVISION)

    run = subparsers.add_parser("run", help="Run a resumable API benchmark")
    run.add_argument("--dataset-root", default=str(DEFAULT_DATASET_ROOT))
    run.add_argument("--split-manifest", default=str(DEFAULT_SPLIT_MANIFEST))
    run.add_argument("--split", choices=("tuning", "holdout", "full"), default="tuning")
    run.add_argument("--output-dir", required=True)
    run.add_argument("--url", default="http://127.0.0.1:8020")
    run.add_argument("--token", default=os.environ.get("UPSTREAM_CLIENT_TOKEN", ""))
    run.add_argument("--concurrency", type=int, default=1)
    run.add_argument("--warmup", type=int, default=2)
    run.add_argument("--limit", type=int, default=0)
    run.add_argument("--order-seed", type=int, default=20260809)
    run.add_argument("--timeout", type=float, default=900.0)
    run.add_argument("--input-format", choices=("image", "pdf"), default="image")
    run.add_argument("--dpi", type=int, default=200)
    run.add_argument(
        "--source-dpi",
        type=int,
        default=200,
        help="Physical DPI assigned when wrapping benchmark images as PDFs.",
    )
    run.add_argument("--gpu-sample-interval", type=float, default=0.5)
    run.add_argument(
        "--vllm-metrics-url",
        default="http://127.0.0.1:8081/metrics",
    )
    run.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)

    summarize = subparsers.add_parser(
        "summarize", help="Summarize a completed or partial run"
    )
    summarize.add_argument("--run-dir", required=True)

    config = subparsers.add_parser(
        "eval-config", help="Write an OmniDocBench evaluator config"
    )
    config.add_argument(
        "--ground-truth", default=str(DEFAULT_DATASET_ROOT / "OmniDocBench.json")
    )
    config.add_argument("--predictions", required=True)
    config.add_argument("--output", required=True)
    config.add_argument(
        "--cdm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include the system-dependent CDM formula metric.",
    )
    config.add_argument(
        "--subset-ground-truth",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Evaluate only pages that have prediction Markdown files.",
    )
    return parser


def main() -> int:
    """Dispatch the selected benchmark command and return its process exit code."""
    args = build_parser().parse_args()
    if args.command == "verify":
        print(json.dumps(verify_dataset(Path(args.dataset_root)), indent=2))
        return 0
    if args.command == "prepare":
        manifest = prepare_split(
            Path(args.dataset_root),
            Path(args.output),
            args.tuning_size,
            args.seed,
            args.source_revision,
        )
        print(json.dumps(manifest["counts"], indent=2))
        return 0
    if args.command == "prepare-real5":
        report = prepare_real5_subset(
            Path(args.base_dataset_root),
            Path(args.output_root),
            Path(args.split_output),
            Path(args.repo_cache),
            args.base_count,
            args.seed,
            args.revision,
        )
        print(json.dumps(report, indent=2))
        return 0
    if args.command == "run":
        return asyncio.run(run_benchmark(args))
    if args.command == "summarize":
        print(json.dumps(summarize_run(Path(args.run_dir)), indent=2))
        return 0
    if args.command == "eval-config":
        write_eval_config(
            Path(args.ground_truth),
            Path(args.predictions),
            Path(args.output),
            include_cdm=args.cdm,
            subset_ground_truth=args.subset_ground_truth,
        )
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
