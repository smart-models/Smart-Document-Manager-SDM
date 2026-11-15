"""Settings resolution: explicit environment > valid profile > defaults."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from urllib.parse import urlsplit

from ocrserver.autotune.tiers import DEFAULT_MODEL_REVISION


LABEL_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
# Label shown for UPSTREAM_CLIENT_TOKEN, the credential ocr-mcp presents. The
# '@' prefix cannot appear in an operator label, so the two never collide.
SERVICE_TOKEN_LABEL = "@mcp"


def _env_str(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key, "")
    return value if value.strip() else None


def parse_api_tokens(raw: str) -> dict[str, str]:
    """Parse OCR_SERVER_API_TOKENS ``token:label,token:label`` into ``{token: label}``.

    Same format as ocr_mcp's MCP_API_TOKENS parser; the services share no runtime
    code, so the rules are repeated here. Errors identify an entry by position
    and never echo its value, which may contain a credential.
    """
    tokens: dict[str, str] = {}
    for position, entry in enumerate((part.strip() for part in raw.split(",")), 1):
        if not entry:
            continue
        token, separator, label = (piece.strip() for piece in entry.partition(":"))
        if not separator or not token or not LABEL_PATTERN.fullmatch(label):
            raise ValueError(
                f"OCR_SERVER_API_TOKENS entry #{position} must be token:label with "
                f"a label matching {LABEL_PATTERN.pattern} (value withheld)"
            )
        if token in tokens or label in tokens.values():
            raise ValueError(
                f"OCR_SERVER_API_TOKENS entry #{position} repeats a token or "
                "label (value withheld)"
            )
        tokens[token] = label
    return tokens


@dataclass(frozen=True)
class Settings:
    """Store immutable, validated API settings with secrets excluded from repr."""

    port: int = 8020
    api_tokens: str = field(default="", repr=False)
    service_token: str = field(default="", repr=False)
    max_queue_size: int = 16
    element_max_slots: int = 32
    element_memory_bytes: int = 512 * 1024 * 1024
    request_timeout: float = 600.0
    page_timeout: float = 120.0
    max_file_bytes: int = 30 * 1024 * 1024
    max_embedded_image_bytes: int = 20 * 1024 * 1024
    max_pages: int = 50
    pdf_max_page_pixels: int = 25_000_000
    parse_dpi: int = 250
    rate_limit_rpm: int = 3600
    rate_limit_burst: int = 120
    rate_limit_max_clients: int = 10000
    rate_limit_idle_seconds: float = 600.0
    expose_swagger: bool = True
    expose_frontend: bool = True
    cors_origins: str = ""
    vl_lanes: int = 2
    page_parallelism: int = 1
    layout_threads: int | None = None
    vl_max_inflight: int = 16
    vl_max_num_seqs: int = 16
    # Read a table or list block again when ocrserver.quality finds a reading
    # loop in it. This is the only place the default is set: Compose and
    # .env.example pass an empty value through.
    table_reread: bool = True
    vllm_url: str = "http://vllm-paddleocr:8080/v1"
    vllm_served_model: str = "PaddleOCR-VL-0.9B"
    hf_cache_dir: str = ""
    vllm_model_revision: str = DEFAULT_MODEL_REVISION
    chunk_max_input_bytes: int = 33_554_432
    chunk_max_blocks: int = 20_000
    chunk_max_units: int = 20_000
    chunk_max_tokens: int = 32_768
    chunk_max_dp_edges: int = 320_000
    chunk_max_split_evaluations: int = 100_000
    chunk_max_token_evaluations: int = 1_100_000
    chunk_max_rendered_bytes: int = 268_435_456
    chunk_max_table_cells: int = 100_000
    chunk_max_output_chunks: int = 10_000
    chunk_max_output_bytes: int = 67_108_864
    chunk_max_workers: int = 2
    chunk_timeout_seconds: float = 120.0
    chunk_queue_wait_seconds: float = 0.5
    office_enabled: bool = True
    office_soffice_path: str = ""
    office_timeout_seconds: float = 120.0
    storage_enabled: bool = False
    storage_browser_enabled: bool = False
    # Archive-only: serve stored archives with no vLLM and no GPU. The
    # inference routes answer 503 ocr_disabled_archive_only (ocr_server.py);
    # the launchers set it through compose.archive-only.yml.
    archive_only: bool = False
    s3_browser_access_key: str = field(default="", repr=False)
    s3_browser_secret_key: str = field(default="", repr=False)
    s3_endpoint: str = "http://seaweedfs:8333"
    s3_bucket: str = "ocr-outputs"
    s3_access_key: str = field(default="", repr=False)
    s3_secret_key: str = field(default="", repr=False)
    s3_region: str = "us-east-1"
    s3_addressing_style: str = "path"
    storage_timeout_seconds: float = 10.0
    storage_max_inflight: int = 2
    storage_max_bytes: int = 134_217_728
    storage_max_objects: int = 1024
    storage_save_input: bool = False
    storage_save_parsed: bool = True

    def __post_init__(self) -> None:
        """Reject invalid ranges, URLs, credential pairs and cross-field settings."""
        positive_ints = (
            "port",
            "max_queue_size",
            "element_max_slots",
            "element_memory_bytes",
            "max_file_bytes",
            "max_embedded_image_bytes",
            "max_pages",
            "pdf_max_page_pixels",
            "parse_dpi",
            "rate_limit_burst",
            "rate_limit_max_clients",
            "vl_lanes",
            "page_parallelism",
            "vl_max_inflight",
            "vl_max_num_seqs",
            "chunk_max_input_bytes",
            "chunk_max_blocks",
            "chunk_max_units",
            "chunk_max_tokens",
            "chunk_max_dp_edges",
            "chunk_max_split_evaluations",
            "chunk_max_token_evaluations",
            "chunk_max_rendered_bytes",
            "chunk_max_table_cells",
            "chunk_max_output_chunks",
            "chunk_max_output_bytes",
            "chunk_max_workers",
            "storage_max_inflight",
            "storage_max_bytes",
            "storage_max_objects",
        )
        for name in positive_ints:
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.rate_limit_rpm) is not int or self.rate_limit_rpm < 0:
            raise ValueError("rate_limit_rpm must be a nonnegative integer")
        if self.layout_threads is not None and (
            type(self.layout_threads) is not int or self.layout_threads < 1
        ):
            raise ValueError("layout_threads must be a positive integer")
        if self.page_parallelism != 1:
            raise ValueError("page_parallelism must be 1 (serial Paddle predictor)")
        for name in (
            "request_timeout",
            "page_timeout",
            "rate_limit_idle_seconds",
            "chunk_timeout_seconds",
            "office_timeout_seconds",
            "storage_timeout_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
        if (
            isinstance(self.chunk_queue_wait_seconds, bool)
            or not isinstance(self.chunk_queue_wait_seconds, (int, float))
            or not math.isfinite(self.chunk_queue_wait_seconds)
            or self.chunk_queue_wait_seconds < 0
        ):
            raise ValueError("chunk_queue_wait_seconds must be finite and nonnegative")
        if self.chunk_max_tokens > 32768:
            raise ValueError("chunk_max_tokens cannot exceed the public limit of 32768")
        if self.port > 65535:
            raise ValueError("port must be between 1 and 65535")
        if not 50 <= self.parse_dpi <= 400:
            raise ValueError("parse_dpi must be between 50 and 400")
        for name in (
            "expose_swagger",
            "expose_frontend",
            "office_enabled",
            "storage_enabled",
            "storage_save_input",
            "storage_save_parsed",
            "storage_browser_enabled",
            "archive_only",
            "table_reread",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        for name in ("api_tokens", "service_token"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a string")
        if self.service_token in parse_api_tokens(self.api_tokens):
            raise ValueError(
                "UPSTREAM_CLIENT_TOKEN is accepted automatically; remove it from "
                "OCR_SERVER_API_TOKENS"
            )
        if not isinstance(self.cors_origins, str):
            raise TypeError("cors_origins must be a string")
        for name in ("hf_cache_dir", "vllm_model_revision", "office_soffice_path"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a string")
        if not self.vllm_model_revision.strip():
            raise ValueError("vllm_model_revision must be a nonempty string")
        if (
            not isinstance(self.vllm_served_model, str)
            or not self.vllm_served_model.strip()
        ):
            raise ValueError("vllm_served_model must be a nonempty string")
        try:
            parsed = urlsplit(self.vllm_url)
            valid = (
                parsed.scheme in ("http", "https")
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            )
            port = parsed.port  # Access validates malformed/out-of-range ports.
            valid = valid and (port is None or port > 0)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("vllm_url must be an HTTP(S) URL without credentials")
        for name in (
            "s3_access_key",
            "s3_secret_key",
            "s3_region",
            "s3_bucket",
            "s3_browser_access_key",
            "s3_browser_secret_key",
        ):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a string")
        if self.storage_enabled and not (
            self.s3_access_key.strip() and self.s3_secret_key.strip()
        ):
            raise ValueError("enabled storage requires explicit S3 credentials")
        if self.storage_browser_enabled and not (
            self.s3_browser_access_key.strip() and self.s3_browser_secret_key.strip()
        ):
            raise ValueError("enabled storage browser requires explicit credentials")
        # Without the archive writer and the browser role there is nothing an
        # archive-only server could serve; refuse to start rather than run empty.
        if self.archive_only and not (
            self.storage_enabled and self.storage_browser_enabled
        ):
            raise ValueError(
                "archive-only mode requires OCR_STORAGE_ENABLED and "
                "OCR_STORAGE_BROWSER_ENABLED"
            )
        if not self.s3_region.strip():
            raise ValueError("s3_region must be nonempty")
        if self.s3_addressing_style not in ("path", "virtual"):
            raise ValueError("s3_addressing_style must be path or virtual")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.s3_bucket):
            raise ValueError("s3_bucket must be a valid S3 bucket name")
        try:
            endpoint = urlsplit(self.s3_endpoint)
            valid = (
                endpoint.scheme in ("http", "https")
                and endpoint.hostname
                and not endpoint.username
                and not endpoint.password
                and not endpoint.query
                and not endpoint.fragment
                and endpoint.path in ("", "/")
                and (endpoint.port is None or endpoint.port > 0)
            )
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError(
                "s3_endpoint must be an HTTP(S) origin without credentials"
            )

    @property
    def auth_tokens(self) -> dict[str, str]:
        """Return accepted bearer tokens mapped to labels; empty disables auth.

        The service token joins only when OCR_SERVER_API_TOKENS enables auth.
        """
        tokens = parse_api_tokens(self.api_tokens)
        if tokens and self.service_token:
            tokens[self.service_token] = SERVICE_TOKEN_LABEL
        return tokens


_ENV_KEYS = {
    "port": "OCR_SERVER_PORT",
    "api_tokens": "OCR_SERVER_API_TOKENS",
    "service_token": "UPSTREAM_CLIENT_TOKEN",
    "max_queue_size": "OCR_MAX_QUEUE_SIZE",
    "element_max_slots": "OCR_ELEMENT_MAX_SLOTS",
    "element_memory_bytes": "OCR_ELEMENT_MEMORY_BYTES",
    "request_timeout": "OCR_REQUEST_TIMEOUT",
    "page_timeout": "OCR_PAGE_TIMEOUT",
    "max_file_bytes": "MAX_FILE_BYTES",
    "max_embedded_image_bytes": "OCR_MAX_EMBEDDED_IMAGE_BYTES",
    "max_pages": "OCR_MAX_PAGES",
    "pdf_max_page_pixels": "OCR_PDF_MAX_PAGE_PIXELS",
    "parse_dpi": "OCR_PARSE_DPI",
    "rate_limit_rpm": "OCR_RATE_LIMIT_REQUESTS_PER_MINUTE",
    "rate_limit_burst": "OCR_RATE_LIMIT_BURST_SIZE",
    "rate_limit_max_clients": "OCR_RATE_LIMIT_MAX_CLIENTS",
    "rate_limit_idle_seconds": "OCR_RATE_LIMIT_IDLE_SECONDS",
    "expose_swagger": "EXPOSE_OCR_SWAGGER",
    "expose_frontend": "EXPOSE_FRONTEND",
    "cors_origins": "OCR_CORS_ORIGINS",
    "vl_lanes": "OCR_VL_LANES",
    "page_parallelism": "OCR_PAGE_PARALLELISM",
    "layout_threads": "OCR_LAYOUT_THREADS",
    "vl_max_inflight": "OCR_VL_MAX_INFLIGHT",
    "vl_max_num_seqs": "OCR_VLLM_MAX_NUM_SEQS",
    "table_reread": "OCR_TABLE_REREAD",
    "vllm_url": "OCR_VLLM_URL",
    "vllm_served_model": "OCR_VLLM_SERVED_MODEL",
    "hf_cache_dir": "OCR_HF_CACHE_DIR",
    "vllm_model_revision": "OCR_VLLM_MODEL_REVISION",
    "chunk_max_input_bytes": "OCR_CHUNK_MAX_INPUT_BYTES",
    "chunk_max_blocks": "OCR_CHUNK_MAX_BLOCKS",
    "chunk_max_units": "OCR_CHUNK_MAX_UNITS",
    "chunk_max_tokens": "OCR_CHUNK_MAX_TOKENS",
    "chunk_max_dp_edges": "OCR_CHUNK_MAX_DP_EDGES",
    "chunk_max_split_evaluations": "OCR_CHUNK_MAX_SPLIT_EVALUATIONS",
    "chunk_max_token_evaluations": "OCR_CHUNK_MAX_TOKEN_EVALUATIONS",
    "chunk_max_rendered_bytes": "OCR_CHUNK_MAX_RENDERED_BYTES",
    "chunk_max_table_cells": "OCR_CHUNK_MAX_TABLE_CELLS",
    "chunk_max_output_chunks": "OCR_CHUNK_MAX_OUTPUT_CHUNKS",
    "chunk_max_output_bytes": "OCR_CHUNK_MAX_OUTPUT_BYTES",
    "chunk_max_workers": "OCR_CHUNK_MAX_WORKERS",
    "chunk_timeout_seconds": "OCR_CHUNK_TIMEOUT_SECONDS",
    "chunk_queue_wait_seconds": "OCR_CHUNK_QUEUE_WAIT_SECONDS",
    "office_enabled": "OCR_OFFICE_ENABLED",
    "office_soffice_path": "OCR_SOFFICE_PATH",
    "office_timeout_seconds": "OCR_OFFICE_TIMEOUT_SECONDS",
    "storage_enabled": "OCR_STORAGE_ENABLED",
    "storage_browser_enabled": "OCR_STORAGE_BROWSER_ENABLED",
    "archive_only": "OCR_ARCHIVE_ONLY",
    "s3_browser_access_key": "OCR_S3_BROWSER_ACCESS_KEY",
    "s3_browser_secret_key": "OCR_S3_BROWSER_SECRET_KEY",
    "s3_endpoint": "OCR_S3_ENDPOINT",
    "s3_bucket": "OCR_S3_BUCKET",
    "s3_access_key": "OCR_S3_ACCESS_KEY",
    "s3_secret_key": "OCR_S3_SECRET_KEY",
    "s3_region": "OCR_S3_REGION",
    "s3_addressing_style": "OCR_S3_ADDRESSING_STYLE",
    "storage_timeout_seconds": "OCR_STORAGE_TIMEOUT_SECONDS",
    "storage_max_inflight": "OCR_STORAGE_MAX_INFLIGHT",
    "storage_max_bytes": "OCR_STORAGE_MAX_BYTES",
    "storage_max_objects": "OCR_STORAGE_MAX_OBJECTS",
    "storage_save_input": "OCR_STORAGE_SAVE_INPUT",
    "storage_save_parsed": "OCR_STORAGE_SAVE_PARSED",
}
_PROFILE_FIELDS = {"vl_lanes", "page_parallelism", "layout_threads"}


def load_settings(env: Mapping[str, str], profile: dict | None) -> Settings:
    """Resolve explicit environment values, eligible profile fields and defaults.

    Only tuning fields may inherit profile decisions. Parse environment values
    according to their built-in types, then validate the complete Settings value.
    """
    # A forgotten single-token value must not silently leave the API open.
    if _env_str(env, "OCR_SERVER_API_TOKEN") is not None:
        raise ValueError(
            "OCR_SERVER_API_TOKEN was replaced by OCR_SERVER_API_TOKENS "
            "(token:label list)"
        )
    decisions = (profile or {}).get("decisions", {})
    values = {}
    for setting_field in fields(Settings):
        name, default = setting_field.name, setting_field.default
        raw = _env_str(env, _ENV_KEYS[name])
        if raw is None:
            values[name] = (
                decisions.get(name, default) if name in _PROFILE_FIELDS else default
            )
            continue
        try:
            if isinstance(default, bool):
                normalized = raw.strip().lower()
                if normalized not in (
                    "1",
                    "true",
                    "yes",
                    "on",
                    "0",
                    "false",
                    "no",
                    "off",
                ):
                    raise ValueError
                values[name] = normalized in ("1", "true", "yes", "on")
            elif isinstance(default, int) or name == "layout_threads":
                values[name] = int(raw)
            elif isinstance(default, float):
                values[name] = float(raw)
            else:
                values[name] = raw
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid configuration for {_ENV_KEYS[name]}") from exc
    return Settings(**values)
