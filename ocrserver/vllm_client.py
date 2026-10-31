"""AsyncOpenAI wrapper for element-level vLLM calls with retry and circuit breaker."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from ocrserver.config import Settings
from ocrserver.element_ocr import (
    DEFAULT_MIN_PIXELS,
    TASK_PROMPTS,
    max_pixels_for_task,
)
from ocrserver.metrics import VLLM_UP

logger = logging.getLogger(__name__)


class BackendUnavailable(Exception):
    """Controlled upstream failure, without backend response details."""

    def __init__(self, message: str = "Backend unavailable", status_code: int = 503):
        """Store a safe explanation and the transport status for API translation."""
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CompletionResult:
    """Completion text and termination metadata for a single recognition call."""

    text: str
    finish_reason: str | None = None


class CircuitBreaker:
    """Global circuit breaker for the single vLLM backend.

    Only connection errors trip the breaker; timeouts do not.
    """

    def __init__(
        self,
        threshold: int = 5,
        open_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure connection-failure threshold, cooldown and monotonic clock."""
        self._threshold = threshold
        self._open_seconds = open_seconds
        self._clock = clock
        self._consecutive_errors = 0
        self._opened_at: float = 0.0
        self.state: str = "closed"
        self._probe_in_flight: bool = False
        self._probe_id: object | None = None

    def before_request(self) -> object | None:
        """Admit a request or reserve the sole half-open recovery probe."""
        if self._probe_in_flight:
            raise BackendUnavailable("circuit breaker half-open probe in flight")
        if self.state == "open":
            if self._clock() - self._opened_at >= self._open_seconds:
                self.state = "half_open"
            else:
                raise BackendUnavailable("circuit breaker open")
        if self.state == "half_open":
            self._probe_id = object()
            self._probe_in_flight = True
            return self._probe_id
        return None

    def release_probe(self, probe_id: object | None) -> None:
        """Release only the matching probe owner's in-flight reservation."""
        if probe_id is not None and probe_id is self._probe_id:
            self._probe_id = None
            self._probe_in_flight = False

    def record_success(self, probe_id: object | None = None) -> None:
        """Close the breaker unless a different half-open probe owns recovery."""
        if self._probe_id is not None and probe_id is not self._probe_id:
            return
        self._consecutive_errors = 0
        self.state = "closed"

    def record_connection_error(self, probe_id: object | None = None) -> None:
        """Count eligible connection failures and reopen failed recovery probes."""
        if self._probe_id is not None and probe_id is not self._probe_id:
            return
        self._consecutive_errors += 1
        if self.state == "half_open":
            self._open()
            return
        if self._consecutive_errors >= self._threshold:
            self._open()

    def record_timeout(self) -> None:
        """Leave breaker state unchanged because timeouts do not prove an outage."""
        pass  # timeouts do not trip the breaker

    def _open(self) -> None:
        self.state = "open"
        self._opened_at = self._clock()
        logger.warning(
            "vLLM circuit breaker opened after %d errors", self._consecutive_errors
        )


class VllmClient:
    """Wraps AsyncOpenAI for element-level recognition via vLLM."""

    def __init__(
        self,
        settings: Settings,
        semaphore: asyncio.Semaphore,
        breaker: CircuitBreaker,
        on_up: Callable[[], None] | None = None,
        on_down: Callable[[], None] | None = None,
        _client: AsyncOpenAI | None = None,
    ) -> None:
        """Wire admission, circuit breaking and backend availability callbacks.

        Use an injected SDK client when supplied. The default HTTP client has
        SDK retries disabled so recognition owns its connection-only retry.
        """
        self._settings = settings
        self._sem = semaphore
        self._breaker = breaker
        self._on_up = on_up or (lambda: VLLM_UP.set(1))
        self._on_down = on_down or (lambda: VLLM_UP.set(0))
        self._client = _client or AsyncOpenAI(
            base_url=settings.vllm_url,
            api_key="EMPTY",
            max_retries=0,
        )

    async def aclose(self) -> None:
        """Close the SDK's pooled HTTP client when the API shuts down."""
        await self._client.close()

    async def _create_completion(
        self, image_b64: str, prompt: str, max_pixels: int, timeout: float
    ) -> CompletionResult:
        """Send one deterministic multimodal completion and validate its first choice.

        Forward bounded image-processing parameters and retain the termination
        reason. Reject malformed response text/metadata as a controlled backend
        failure without exposing upstream response bodies.
        """
        resp = await self._client.chat.completions.create(
            model=self._settings.vllm_served_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            temperature=0.0,
            timeout=timeout,
            extra_body={
                "mm_processor_kwargs": {
                    "min_pixels": DEFAULT_MIN_PIXELS,
                    "max_pixels": max_pixels,
                }
            },
        )
        choices = getattr(resp, "choices", None)
        if not isinstance(choices, list) or not choices:
            raise BackendUnavailable("vLLM returned an invalid completion")
        choice = choices[0]
        content = getattr(getattr(choice, "message", None), "content", None)
        finish_reason = getattr(choice, "finish_reason", None)
        if not isinstance(content, str):
            raise BackendUnavailable("vLLM returned an invalid completion")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise BackendUnavailable("vLLM returned an invalid completion")
        return CompletionResult(text=content, finish_reason=finish_reason)

    async def recognize(
        self, image_b64: str, task: str, timeout: float
    ) -> CompletionResult:
        """Recognize one task with at most one connection retry and no timeout retry.

        Hold shared inference admission only while requesting a completion.
        Always release half-open probe ownership on success, failure or cancel.
        """
        prompt = TASK_PROMPTS[task]
        max_pixels = max_pixels_for_task(task)
        owner = self._breaker.before_request()
        try:
            for attempt in range(2):
                try:
                    async with self._sem:
                        completion = await self._create_completion(
                            image_b64, prompt, max_pixels, timeout
                        )
                    self._breaker.record_success(owner)
                    self._on_up()
                    return completion
                except APITimeoutError:
                    self._breaker.record_timeout()
                    raise
                except APIStatusError as exc:
                    status = (
                        503 if exc.status_code in (429, 500, 502, 503, 504) else 502
                    )
                    self._on_down()
                    raise BackendUnavailable("vLLM HTTP failure", status) from exc
                except APIConnectionError as exc:
                    self._breaker.record_connection_error(owner)
                    self._on_down()
                    if attempt:
                        raise BackendUnavailable(
                            "vLLM connection failed after retry"
                        ) from exc
                    # Keep probe ownership across the logical request, but
                    # leave the shared inference slot available during backoff.
                    await asyncio.sleep(0.5)
            raise AssertionError("unreachable retry state")
        finally:
            self._breaker.release_probe(owner)
