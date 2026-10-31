"""The only place that speaks HTTP to the upstream FastAPI application.

Retries transient failures -- 5xx, 429, timeouts, connection errors -- with
exponential backoff. Two hardenings that a naive retry loop misses: a 429's
``Retry-After`` header overrides the computed backoff (capped, so a broken or
hostile server cannot park a caller indefinitely), and the backoff carries
jitter so concurrent callers that failed together do not retry in lockstep.

Non-idempotent calls are not retried by default: ``request`` decides from the
HTTP method unless the caller passes ``retryable`` explicitly. A tool that
POSTs a genuinely idempotent read (a search, a count) passes
``retryable=True``; a tool that writes never does.
"""

from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from ocr_mcp.config import Settings
from ocr_mcp.errors import ChunkTransferLimit, ToolError, UpstreamError
from ocr_mcp.responses import STORAGE_RESPONSE_MODELS, validate_payload

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
SAFE_RETRY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# A 429's Retry-After is honored only up to this many seconds.
RETRY_AFTER_CAP_SECONDS = 30.0
# An error body is read only for its code; nothing larger is ever needed.
ERROR_BODY_MAX_BYTES = 65_536


def _retry_after_delay(
    response: httpx.Response, *, now: datetime | None = None
) -> float | None:
    """Return the wait a response's ``Retry-After`` header requests, if usable.

    Both RFC 9110 forms are honored: non-negative integer delta-seconds and an
    HTTP-date. Invalid or non-finite values fall back to computed backoff.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    value = raw.strip()
    if value.isascii() and value.isdecimal():
        try:
            seconds = float(value)
        except (ValueError, OverflowError):
            return None
    else:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        reference = now or datetime.now(timezone.utc)
        seconds = max(0.0, (retry_at - reference).total_seconds())
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_CAP_SECONDS)


def _backoff_delay(attempt: int, base: float) -> float:
    """Return the exponential backoff for ``attempt``, jittered to [0.5x, 1x].

    The jitter desynchronizes concurrent callers so they do not stampede the
    upstream in lockstep; the lower bound keeps the wait near the pre-jitter
    policy rather than ever dropping to zero.
    """
    return base * (2 ** (attempt - 1)) * random.uniform(0.5, 1.0)


class UpstreamClient:
    """An async client for the upstream REST API."""

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Open a pooled client bound to the upstream base URL and token.

        A blank ``UPSTREAM_CLIENT_TOKEN`` sends no Authorization header at all
        rather than an empty one: the upstream may itself serve
        unauthenticated, and ``Bearer `` is an illegal header value that h11
        refuses to serialize, failing every request before it is sent.

        ``transport`` exists for tests: an ``httpx.MockTransport`` swaps the
        upstream for a fake without touching any other wiring.
        """
        self._settings = settings
        headers = (
            {"Authorization": f"Bearer {settings.upstream_client_token}"}
            if settings.upstream_client_token
            else {}
        )
        self._client = httpx.AsyncClient(
            base_url=settings.upstream_url,
            timeout=settings.http_timeout,
            headers=headers,
            transport=transport,
        )

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._client.aclose()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: dict | None,
        params: dict | None,
        retryable: bool,
        budget: float | None = None,
    ) -> httpx.Response:
        """Retry transient failures within a shared wall-clock deadline.

        Diagnostic GETs use ``diagnostic_timeout``; storage calls pass their
        own ``budget``. Other methods keep the inference timeout per phase.
        """
        clean_params = (
            {k: v for k, v in params.items() if v is not None} if params else None
        )
        attempts = self._settings.retry_attempts if retryable else 1
        diagnostic = budget is not None or method.upper() in SAFE_RETRY_METHODS
        timeout = budget or (
            self._settings.diagnostic_timeout
            if diagnostic
            else self._settings.http_timeout
        )
        try:
            # Include backoff and Retry-After in one wall-clock budget. POSTs
            # keep their inference timeout and existing retry policy.
            async with asyncio.timeout(timeout if diagnostic else None):
                for attempt in range(1, attempts + 1):
                    delay: float | None = None
                    try:
                        response = await self._client.request(
                            method,
                            path,
                            json=json,
                            params=clean_params,
                            timeout=timeout,
                        )
                    except (httpx.TimeoutException, httpx.TransportError):
                        if attempt == attempts:
                            raise
                    else:
                        if (
                            response.status_code not in RETRY_STATUSES
                            or attempt == attempts
                        ):
                            return response
                        if response.status_code == 429:
                            delay = _retry_after_delay(response)
                    if delay is None:
                        delay = _backoff_delay(attempt, self._settings.retry_backoff)
                    await asyncio.sleep(delay)
        except (TimeoutError, httpx.TimeoutException) as exc:
            if not diagnostic:
                raise
            kind = "storage" if budget is not None else "diagnostic"
            raise ToolError(
                f"OCR {kind} request timed out. Check MCP_UPSTREAM_URL "
                "and API availability before retrying."
            ) from exc

    def _raise_for_status(self, response: httpx.Response, path: str) -> None:
        """Convert a non-success response into an UpstreamError."""
        if response.is_success:
            return
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        detail = (
            payload.get("detail", payload) if isinstance(payload, dict) else payload
        )
        raise UpstreamError(status=response.status_code, detail=detail, path=path)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        retryable: bool | None = None,
        budget: float | None = None,
    ) -> dict:
        """Call the upstream and return the decoded JSON body."""
        should_retry = (
            method.upper() in SAFE_RETRY_METHODS if retryable is None else retryable
        )
        response = await self._send(
            method,
            path,
            json=json,
            params=params,
            retryable=should_retry,
            budget=budget,
        )
        self._raise_for_status(response, path)
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ToolError(
                f"Unexpected {path} payload: expected a JSON object. "
                "Check the OCR API version and upstream configuration."
            ) from exc
        if not isinstance(payload, dict):
            raise ToolError(
                f"Unexpected {path} payload: expected a JSON object. "
                "Check the OCR API version and upstream configuration."
            )
        if (method.upper(), path) in STORAGE_RESPONSE_MODELS:
            validate_payload(payload, path, method=method)
        return payload

    async def text(self, path: str) -> str:
        """Call the upstream and return the raw response body as text."""
        response = await self._send("GET", path, json=None, params=None, retryable=True)
        self._raise_for_status(response, path)
        return response.text

    async def binary_request(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        recovery: str,
        **kwargs,
    ) -> httpx.Response:
        """Send one request (no retry) and read at most the MCP result limit.

        The body is streamed and abandoned as soon as it exceeds
        ``chunk_max_result_bytes``, so an oversized artifact never fills memory.
        An error body stops at ``ERROR_BODY_MAX_BYTES``: only its code matters.
        ``recovery`` names the REST route the caller should use instead.
        """
        limit = self._settings.chunk_max_result_bytes
        refusal = ToolError(
            f"Result exceeds the MCP result-byte limit ({limit} bytes). "
            f"Use REST directly: {recovery}"
        )
        try:
            async with asyncio.timeout(timeout):
                async with self._client.stream(
                    method, path, timeout=timeout, **kwargs
                ) as incoming:
                    if incoming.is_success:
                        advertised = incoming.headers.get("content-length", "")
                        if advertised.isdecimal() and int(advertised) > limit:
                            raise refusal
                    data = bytearray()
                    async for part in incoming.aiter_bytes():
                        data.extend(part)
                        if incoming.is_success and len(data) > limit:
                            raise refusal
                        error_cap = ERROR_BODY_MAX_BYTES
                        if not incoming.is_success and len(data) >= error_cap:
                            del data[error_cap:]
                            break
                    content_type = incoming.headers.get("content-type")
                    response = httpx.Response(
                        incoming.status_code,
                        headers={"content-type": content_type}
                        if content_type
                        else None,
                        content=bytes(data),
                        request=incoming.request,
                    )
        except TimeoutError as exc:
            raise ToolError(
                f"{path} timed out. Check API availability before a deliberate retry."
            ) from exc
        self._raise_for_status(response, path)
        return response

    async def chunk_request(self, path: str, *, json: dict) -> dict:
        """Send one optional RAG request with its own total deadline, no retry."""
        limit = self._settings.chunk_max_result_bytes
        try:
            async with asyncio.timeout(self._settings.chunk_http_timeout):
                async with self._client.stream(
                    "POST",
                    path,
                    json=json,
                    timeout=self._settings.chunk_http_timeout,
                ) as incoming:
                    advertised = incoming.headers.get("content-length", "")
                    if (
                        advertised.isascii()
                        and advertised.isdecimal()
                        and incoming.headers.get("content-encoding", "identity")
                        == "identity"
                        and int(advertised) > limit
                    ):
                        raise ChunkTransferLimit(
                            "mcp_output_limit",
                            actual_bytes=int(advertised),
                            limit_bytes=limit,
                            recovery_route=path,
                        )
                    data = bytearray()
                    async for part in incoming.aiter_bytes(
                        chunk_size=min(65536, limit + 1)
                    ):
                        if len(data) + len(part) > limit:
                            raise ChunkTransferLimit(
                                "mcp_output_limit",
                                actual_bytes=None,
                                limit_bytes=limit,
                                recovery_route=path,
                            )
                        data.extend(part)
                    response = httpx.Response(
                        incoming.status_code,
                        content=bytes(data),
                        request=incoming.request,
                    )
        except TimeoutError as exc:
            raise ToolError(
                "RAG request timed out. Parsing may have started; no automatic retry. "
                "Inspect capacity before a deliberate retry."
            ) from exc
        self._raise_for_status(response, path)
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ToolError("Unexpected RAG payload: expected valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ToolError("Unexpected RAG payload: expected an object.")
        return payload
