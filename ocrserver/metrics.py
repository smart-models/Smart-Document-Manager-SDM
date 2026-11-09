"""Prometheus metric objects — names are spec-fixed."""

from prometheus_client import Counter, Gauge, Histogram, Info

OCR_DURATION_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300, 600, 900)

REQUESTS = Counter(
    "ocr_requests_total",
    "Total HTTP requests by endpoint and status.",
    ["endpoint", "status"],
)
REQUESTS_REJECTED = Counter(
    "ocr_requests_rejected_total",
    "Requests rejected before processing.",
    ["reason"],
)
PAGES_PROCESSED = Counter(
    "ocr_pages_processed_total",
    "Total document pages processed.",
)
REQUEST_DURATION = Histogram(
    "ocr_request_duration_seconds",
    "HTTP request duration by endpoint.",
    ["endpoint"],
    buckets=OCR_DURATION_BUCKETS,
)
PAGE_DURATION = Histogram(
    "ocr_page_duration_seconds",
    "Per-page processing duration.",
    buckets=OCR_DURATION_BUCKETS,
)
LAYOUT_DURATION = Histogram(
    "ocr_layout_duration_seconds",
    "Layout detection (CPU) phase duration.",
)
VL_DURATION = Histogram(
    "ocr_vl_duration_seconds",
    "VLM recognition (GPU via vLLM) phase duration.",
)
QUEUE_SIZE = Gauge(
    "ocr_queue_size",
    "Current parse queue depth.",
)
ACTIVE_REQUESTS = Gauge(
    "ocr_active_requests",
    "Requests currently in-flight.",
)
VLLM_UP = Gauge(
    "ocr_vllm_up",
    "1 if vLLM backend is reachable, 0 otherwise.",
)
ARCHIVE_ONLY_MODE = Gauge(
    "ocr_archive_only_mode",
    "1 in archive-only mode: vLLM is disabled by design, so ocr_vllm_up=0 is "
    "not an outage.",
)
SERVER_INFO = Info(
    "ocr_server",
    "Server build and configuration info.",
)
AUTOTUNE_INFO = Info(
    "ocr_autotune",
    "Active autotune profile and decisions.",
)

STORAGE_ATTEMPTS = Counter(
    "ocr_storage_attempts_total",
    "Archive attempts by outcome.",
    ["status"],
)
STORAGE_DURATION = Histogram(
    "ocr_storage_duration_seconds",
    "Time spent waiting for archival.",
)
STORAGE_BYTES = Counter(
    "ocr_storage_bytes_total",
    "Bytes in confirmed S3 PutObject operations.",
)
STORAGE_INFLIGHT = Gauge(
    "ocr_storage_inflight",
    "Storage workers still holding capacity.",
)
