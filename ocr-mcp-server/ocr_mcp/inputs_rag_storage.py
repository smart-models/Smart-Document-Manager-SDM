"""Input models for stored RAG refinement and split tools."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, BeforeValidator

from ocr_mcp.inputs import ToolInput


def _normalize_entry(value):
    """Validate one plan entry: int, [i,s,e] or [i,s,e,'repeat']."""
    if isinstance(value, bool) or not isinstance(value, (int, list, tuple)):
        raise ValueError("plan entry must be an integer or an entry list")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("plan entry index must not be negative")
        return value
    if len(value) == 3:
        index, start, end = value
        repeat = None
    elif len(value) == 4:
        index, start, end, repeat = value
        if repeat != "repeat":
            raise ValueError("plan entry repeat flag must be 'repeat'")
    else:
        raise ValueError("plan entry must have three or four elements")
    for name, item in (("index", index), ("start", start), ("end", end)):
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"plan entry {name} must be an integer")
        if item < 0:
            raise ValueError(f"plan entry {name} must not be negative")
    if start >= end:
        raise ValueError("plan entry range must not be empty")
    if repeat is None:
        return index, start, end
    return index, start, end, repeat


RagPlanEntry = int | tuple[int, int, int] | tuple[int, int, int, str]
RagPlanEntryField = Annotated[RagPlanEntry, BeforeValidator(_normalize_entry)]


class RagRefinementOptions(BaseModel):
    """Set stored-RAG token budgets and page, table and code protection policy."""

    model_config = ConfigDict(extra="forbid")
    min_tokens: int = Field(
        ge=1,
        description=(
            "Minimum desired fragment tokens; small unsplittable fragments "
            "may be reported (e.g. 128)"
        ),
    )
    max_tokens: int = Field(
        ge=1,
        description=(
            "Maximum desired fragment tokens; must be at least min_tokens (e.g. 512)"
        ),
    )
    page_boundary: bool = Field(
        default=False,
        description="Prevent groups from crossing source page boundaries (e.g. false)",
    )
    keep_tables_together: bool = Field(
        default=True,
        description=(
            "Protect tables unless the user explicitly permits splitting "
            "them (e.g. true)"
        ),
    )
    keep_code_blocks_together: bool = Field(
        default=True,
        description=(
            "Protect fenced code blocks unless splitting is explicitly "
            "allowed (e.g. true)"
        ),
    )


class RefineRagStorageInput(ToolInput):
    """Identify a stored source and an explicit, reproducible refinement plan."""

    source_key: str = Field(
        min_length=1,
        max_length=1024,
        description=(
            "Stored json_rag source key copied from archive details (e.g. "
            "'invoice.pdf__20261018T120000000000Z__job123AB/json_rag.json')"
        ),
    )
    source_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$",
        description=(
            "SHA-256 from a full download of that source, checked against "
            "concurrent changes (e.g. "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')"
        ),
    )
    execution_id: str | None = Field(
        default=None,
        description=(
            "Omit for a fresh UUID; reuse only for the identical plan "
            "(e.g. '550e8400-e29b-41d4-a716-446655440000')"
        ),
    )
    created_at: str | None = Field(
        default=None,
        description=(
            "Omit for the current UTC time; ISO 8601 with an explicit timezone "
            "(e.g. '2026-10-18T12:00:00Z')"
        ),
    )
    options: RagRefinementOptions = Field(
        description=(
            "Explicit token budget and table/code/page protection policy "
            "(e.g. {'min_tokens':128,'max_tokens':512})"
        )
    )
    groups: list[list[RagPlanEntryField]] = Field(
        min_length=1,
        max_length=100000,
        description=(
            "Ordered plan: integer chunk indexes or half-open codepoint "
            "slices [index,start,end], optionally with 'repeat' framing; "
            "preserve coverage (e.g. [[0,1],[2,[3,0,40]]])"
        ),
    )
    overrides: list[list[str]] | None = Field(
        default=None,
        description=(
            "Acknowledged diagnostic codes, one list per group; null leaves"
            " defaults (e.g. [[],['above_maximum_fragment']])"
        ),
    )
    counts: list[list[int | None]] | None = Field(
        default=None,
        description=(
            "Measured per-source-chunk contributions in each group's entry "
            "order; one list per group, null means unknown (e.g. "
            "[[100,120],[null]])"
        ),
    )


class SplitRagGroupInput(ToolInput):
    """Identify one stored-RAG plan group for a read-only split preview."""

    source_key: str = Field(
        min_length=1,
        max_length=1024,
        description=(
            "Stored json_rag source key to preview without writing (e.g. "
            "'invoice.pdf__20261018T120000000000Z__job123AB/json_rag.json')"
        ),
    )
    source_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$",
        description=(
            "SHA-256 returned by a full source download; stale hashes are "
            "refused (e.g. "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')"
        ),
    )
    options: RagRefinementOptions = Field(
        description=(
            "Token budget and table/code/page protection for the preview "
            "(e.g. {'min_tokens':128,'max_tokens':512})"
        )
    )
    group: list[RagPlanEntryField] = Field(
        min_length=1,
        max_length=100000,
        description=(
            "One group of chunk indexes or half-open slices to split; "
            "optional 'repeat' framing (e.g. [0,[1,0,40]])"
        ),
    )
