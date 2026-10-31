"""In-memory progress of /parse requests, polled by the console.

Entries carry counters only, never document names or content, and are dropped
shortly after the request concludes.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

RETENTION_SECONDS = 60.0
ACTIVE_STATES = ("queued", "rasterizing", "ocr", "finalizing")
FINAL_STATES = ("done", "partial", "cancelled", "failed")

_UUID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def parse_progress_id(raw: str | None) -> str | None:
    """Return the lower-case canonical UUID, None when absent, else ValueError."""
    if not raw:
        return None
    if not _UUID.fullmatch(raw):
        raise ValueError("progress_id must be a canonical UUID")
    return raw.lower()


@dataclass
class ProgressEntry:
    """Mutable progress counters of one /parse request."""

    dpi: int | None
    started: float
    clock: Callable[[], float] = time.monotonic
    state: str = "queued"
    pages_total: int | None = None
    pages_done: int = 0
    pages_failed: int = 0
    pages_unprocessed: int = 0
    block: int | None = None
    blocks_total: int | None = None
    block_pages: int | None = None
    finished_at: float | None = None
    _ocr_started: float | None = None

    def plan(self, pages_total: int, blocks_total: int) -> None:
        """Record the page and block totals once the document is known."""
        self.pages_total = pages_total
        self.blocks_total = blocks_total

    def begin_block(self, block: int, block_pages: int) -> None:
        """Start block `block` (1-based) of `block_pages` pages."""
        self.block = block
        self.block_pages = block_pages
        self.state = "rasterizing"
        if self._ocr_started is None:
            self._ocr_started = self.clock()

    def set_state(self, state: str) -> None:
        """Move to another state unless the entry already concluded."""
        if self.finished_at is None:
            self.state = state

    def page_done(self) -> None:
        """Count one page delivered while its block is still running."""
        if self.finished_at is None:
            self.pages_done += 1

    def end_block(self, done: int, failed: int, unprocessed: int) -> None:
        """Fold one block's remaining page outcomes into the totals."""
        self.pages_done += done
        self.pages_failed += failed
        self.pages_unprocessed += unprocessed

    def snapshot(self, now: float) -> dict:
        """Counters as served by the progress route."""
        end = self.finished_at if self.finished_at is not None else now
        # Pages stream back one by one, so the rate is measured per page from
        # the first block start: available after the first page, any block size.
        eta = None
        processed = self.pages_done + self.pages_failed
        if (
            self.state in ACTIVE_STATES
            and processed
            and self.pages_total
            and self._ocr_started is not None
        ):
            left = self.pages_total - processed - self.pages_unprocessed
            eta = round((now - self._ocr_started) / processed * max(left, 0))
        return {
            "state": self.state,
            "pages_total": self.pages_total,
            "pages_done": self.pages_done,
            "pages_failed": self.pages_failed,
            "pages_unprocessed": self.pages_unprocessed,
            "block": self.block,
            "blocks_total": self.blocks_total,
            "block_pages": self.block_pages,
            "dpi": self.dpi,
            "elapsed_s": round(end - self.started, 1),
            "eta_s": eta,
        }


class ProgressRegistry:
    """Bounded id -> entry map with lazy expiry of concluded entries."""

    def __init__(
        self,
        capacity: int,
        *,
        retention: float = RETENTION_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._retention = retention
        self._clock = clock
        self._entries: dict[str, ProgressEntry] = {}

    def _purge(self) -> float:
        now = self._clock()
        for key in [
            k
            for k, e in self._entries.items()
            if e.finished_at is not None and now - e.finished_at > self._retention
        ]:
            del self._entries[key]
        return now

    def register(self, progress_id: str, dpi: int | None) -> ProgressEntry | None:
        """Create an entry; None when the id exists or no slot can be freed."""
        now = self._purge()
        if progress_id in self._entries:
            return None
        if len(self._entries) >= self._capacity:
            finished = [k for k, e in self._entries.items() if e.finished_at]
            if not finished:
                return None
            del self._entries[min(finished, key=lambda k: self._entries[k].finished_at)]
        entry = ProgressEntry(dpi=dpi, started=now, clock=self._clock)
        self._entries[progress_id] = entry
        return entry

    def get(self, progress_id: str) -> ProgressEntry | None:
        """The live or recently concluded entry, if any."""
        self._purge()
        return self._entries.get(progress_id)

    def finish(self, entry: ProgressEntry, state: str) -> None:
        """Conclude the entry in a final state (first call wins)."""
        if entry.finished_at is None:
            entry.state = state
            entry.finished_at = self._clock()
