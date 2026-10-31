"""The dependency bundle every tool module receives at registration time."""

from __future__ import annotations

from dataclasses import dataclass

from ocr_mcp.client import UpstreamClient
from ocr_mcp.config import Settings


@dataclass(frozen=True)
class ToolContext:
    """Shared dependencies for tool implementations."""

    client: UpstreamClient
    settings: Settings
