"""Authenticated archive browsing and read-only downloads."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import urlencode

from mcp.server import MCPServer
from mcp_types import ImageContent

from ocr_mcp.context import ToolContext
from ocr_mcp.errors import error_result
from ocr_mcp.formatting import StructuredToolResult, bullet, render
from ocr_mcp.guardrails import require_archive_confirm, require_file_confirm
from ocr_mcp.inputs_storage import (
    DeleteStorageArchiveInput,
    DeleteStorageFileInput,
    DownloadStorageFileInput,
    DownloadStorageZipInput,
    GetStorageArchiveInput,
    ListStorageArchivesInput,
)

# ponytail: fixed bound keeps base64 under the Claude API's 5 MB per-image
# limit; larger images keep the base64 field instead of an image block.
IMAGE_CONTENT_MAX_BYTES = 3_750_000
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def _is_text(media_type: str) -> bool:
    return (
        media_type.startswith("text/")
        or media_type == "application/json"
        or media_type.endswith("+json")
    )


def download_payload(
    key: str, content_type: str, content: bytes
) -> tuple[dict, list[ImageContent]]:
    """Describe downloaded bytes in the form a model can read.

    UTF-8 text comes back as ``text`` and small images as an image block next
    to their metadata; every other file keeps its exact bytes as ``base64``.
    """
    payload = {
        "key": key,
        "content_type": content_type,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    media_type = content_type.split(";", 1)[0].strip().lower()
    encoded = base64.b64encode(content).decode()
    if _is_text(media_type):
        try:
            return {**payload, "text": content.decode("utf-8")}, []
        except UnicodeDecodeError:
            pass
    if media_type in IMAGE_TYPES and len(content) <= IMAGE_CONTENT_MAX_BYTES:
        return payload, [ImageContent(data=encoded, mime_type=media_type)]
    return {**payload, "base64": encoded}, []


def slim_archive(payload: dict) -> dict:
    """Drop the manifest lists that repeat ``files`` and add each file's hash.

    ``manifest.artifacts`` repeats every file with its key, and each
    ``manifest.images`` entry repeats its key; the hashes they carried move
    to ``files[].sha256`` (null when the manifest has none).
    """
    manifest = dict(payload.get("manifest", {}))
    hashes = {
        item.get("key"): item.get("sha256")
        for item in manifest.pop("artifacts", None) or []
        if isinstance(item, dict)
    }
    if isinstance(manifest.get("images"), list):
        manifest["images"] = [
            {k: v for k, v in image.items() if k != "key"}
            if isinstance(image, dict)
            else image
            for image in manifest["images"]
        ]
    files = [{**item, "sha256": hashes.get(item["key"])} for item in payload["files"]]
    return {**payload, "manifest": manifest, "files": files}


def _download_rows(payload: dict) -> list[str]:
    rows = [
        bullet("Key", payload.get("key")),
        bullet("Content type", payload.get("content_type")),
    ]
    if "sha256" in payload:
        rows.append(bullet("SHA-256", payload["sha256"]))
    if "text" in payload:
        rows.append(bullet("Text characters", len(payload["text"])))
    elif "base64" in payload:
        rows.append(bullet("Base64 bytes", len(payload["base64"])))
    else:
        rows.append(bullet("Image", "returned as an image block"))
    return rows


def _archive_rows(payload: dict) -> list[str]:
    rows = [bullet("Prefix", payload.get("prefix"))]
    files = payload.get("files", [])
    if files:
        rows.append(f"\n## Files ({len(files)})")
        for item in files:
            rows.append(f"- {item['name']} ({item['size']} bytes)")
    return rows


def _list_rows(payload: dict) -> list[str]:
    items = payload.get("items", [])
    rows = [bullet("Archives", len(items))]
    if payload.get("next_cursor"):
        rows.append(bullet("Next cursor", "present"))
    if payload.get("skipped_archives"):
        rows.append(bullet("Skipped archives", payload["skipped_archives"]))
    for item in items:
        source = f" — source {item['input_key']}" if item.get("input_key") else ""
        rows.append(
            f"- {item.get('filename')} — {item.get('kind')} — "
            f"{item.get('total_bytes')} bytes{source}"
        )
    return rows


def register(mcp: MCPServer, ctx: ToolContext) -> None:
    """Register archive browsing, downloads and exact-confirmation deletion tools."""

    @mcp.tool(
        name="ocr_list_storage_archives",
        annotations={
            "title": "List storage archives",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def list_storage_archives(
        params: ListStorageArchivesInput,
    ) -> StructuredToolResult:
        """List a filtered page of persisted OCR archives without running inference.

        Read metadata only. Reuse next_cursor unchanged with the same filters; a null
        cursor ends pagination. Storage browsing must be enabled by the operator.

        Args:
            params (ListStorageArchivesInput): kind (parse/ocr/parse-chunks or null);
                date (YYYY-MM-DD) or inclusive date_from/date_to; cursor (str|null);
                limit (int, 1-50, default 20); response_format (markdown/json).

        Returns:
            Markdown archive summary or JSON, with identical complete structured data:
            {"items":[{"prefix":str,"job_id":str,"filename":str|null,
              "kind":"parse"|"ocr"|"parse-chunks"|"unknown","created_at":str|null,
              "object_count":int,"total_bytes":int,"partial_ocr":bool,
              "unprocessed_pages":[int],"incomplete":bool,"input_key":str|null}],
             "next_cursor":str|null,"skipped_archives":int}.
            unknown/incomplete marks an abandoned archive; skipped_archives counts
            unreadable manifests, so an empty page alone does not prove no results.

        Examples:
            Use when: finding yesterday's saved invoice without parsing it again.
            Use when: inspecting candidates before a user-requested deletion.
            Don't use when: reading file contents; use ocr_download_storage_file.
            Don't use when: checking GPU activity; use ocr_get_stats.

        Error Handling:
            Invalid filters return 422; disabled/unavailable storage returns 503.
            GET retries share MCP_STORAGE_TIMEOUT. Invalid response shapes and all
            execution failures return is_error=true with structured {}.
        """
        try:
            query = params.model_dump(exclude={"response_format"}, exclude_none=True)
            payload = await ctx.client.request(
                "GET",
                "/storage/archives",
                params=query,
                budget=ctx.settings.storage_timeout,
            )
            return render(
                payload,
                params.response_format,
                title="Storage archives",
                rows=_list_rows,
            )
        except Exception as exc:
            return error_result(exc)

    @mcp.tool(
        name="ocr_get_storage_archive",
        annotations={
            "title": "Get storage archive details",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def get_storage_archive(
        params: GetStorageArchiveInput,
    ) -> StructuredToolResult:
        """Inspect one archive's manifest and file list without downloading artifacts.

        Copy an opaque prefix from ocr_list_storage_archives. This does not run OCR,
        write storage, or verify the model's health.

        Args:
            params (GetStorageArchiveInput): prefix (str, including trailing slash);
                response_format (markdown/json).

        Returns:
            Markdown file summary or JSON, and identical structured content:
            {"prefix":str,"manifest":dict,"files":[{"key":str,"name":str,
              "size":int,"content_type":str,"sha256":str|null}]}.
            manifest is the API document without its artifacts list and without
            the key of each images entry (both repeat files); sha256 comes from
            that list and serves refinement as source_sha256. name is relative to
            the archive and can include directories; use it for confirmation.

        Examples:
            Use when: selecting the precise artifact to download or delete.
            Use when: inspecting a saved archive's provenance.
            Don't use when: discovering prefixes; use ocr_list_storage_archives.
            Don't use when: reading artifact bytes; use ocr_download_storage_file.

        Error Handling:
            Missing archive returns 404; invalid prefix returns 422; storage failures
            return 503. GET retries share the storage deadline. Invalid response
            shapes and other execution errors return is_error=true with structured {}.
        """
        try:
            payload = await ctx.client.request(
                "GET",
                "/storage/archive",
                params={"prefix": params.prefix},
                budget=ctx.settings.storage_timeout,
            )
            return render(
                slim_archive(payload),
                params.response_format,
                title=f"Archive {params.prefix}",
                rows=_archive_rows,
            )
        except Exception as exc:
            return error_result(exc)

    @mcp.tool(
        name="ocr_download_storage_file",
        annotations={
            "title": "Download a stored file",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def download_storage_file(
        params: DownloadStorageFileInput,
    ) -> StructuredToolResult:
        """Download or preview one stored artifact without running OCR.

        UTF-8 text files come back as text and PNG, JPEG, GIF or WebP images as
        an image block; other files keep base64. The raw downloaded bytes must
        fit MCP_CHUNK_MAX_RESULT_BYTES. No partial file is returned; larger files
        need the REST recovery route in the error.

        Args:
            params (DownloadStorageFileInput): key (str from archive details);
                preview (bool, default false); response_format (markdown/json).

        Returns:
            Markdown metadata or JSON, with identical structured content:
            {"key":str,"content_type":str,"size":int,"sha256":str,
             "text":str} for UTF-8 text, the same without "text" plus an image
            block for images up to 3.75 MB, or with "base64":str otherwise.
            sha256 hashes the raw returned bytes. preview=true returns the whole
            file when it fits the preview limits and 413 otherwise, never a
            truncated view; false is required for source-hash checks.

        Examples:
            Use when: reading saved OCR Markdown instead of repeating inference.
            Use when: downloading json_rag.json and its hash before refinement.
            Don't use when: discovering file keys; use ocr_get_storage_archive.
            Don't use when: downloading every artifact; use ocr_download_storage_zip.

        Error Handling:
            404 means gone; 415 refuses unsupported preview types; oversized raw
            bytes produce an explicit error naming GET /storage/file for REST use.
            Downloads have one attempt under MCP_STORAGE_TIMEOUT. Failures return
            is_error=true with structured {}, never truncated base64.
        """
        try:
            response = await ctx.client.binary_request(
                "GET",
                "/storage/file",
                params={
                    "key": params.key,
                    "preview": "true" if params.preview else "false",
                },
                timeout=ctx.settings.storage_timeout,
                recovery="GET /storage/file?"
                + urlencode({"key": params.key, "preview": "false"}),
            )
            content_type = response.headers.get(
                "content-type", "application/octet-stream"
            )
            payload, images = download_payload(
                params.key, content_type, response.content
            )
            return render(
                payload,
                params.response_format,
                title=f"Downloaded {params.key}",
                rows=_download_rows,
                extra_content=images,
            )
        except Exception as exc:
            return error_result(exc)

    @mcp.tool(
        name="ocr_download_storage_zip",
        annotations={
            "title": "Download a storage archive ZIP",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def download_storage_zip(
        params: DownloadStorageZipInput,
    ) -> StructuredToolResult:
        """Download every artifact of a complete archive as one ZIP without OCR.

        The limit applies to raw ZIP bytes, before base64 encoding. Use the REST
        route for larger ZIPs; requesting the ZIP does not delete or rewrite storage.

        Args:
            params (DownloadStorageZipInput): prefix (str copied from a listing);
                response_format (markdown/json).

        Returns:
            Markdown download metadata or JSON, with identical structured data:
            {"prefix":str,"content_type":"application/zip","sha256":str,"base64":str}.
            sha256 hashes the raw ZIP. Base64 and JSON presentation add overhead to
            the raw-byte limit; successful results contain the entire ZIP.

        Examples:
            Use when: the user needs all files from a saved archive.
            Use when: exporting a complete archive without inference.
            Don't use when: only one artifact is needed; use ocr_download_storage_file.
            Don't use when: only metadata is needed; use ocr_get_storage_archive.

        Error Handling:
            404 means gone; 409 reports an incomplete archive; 413 reports API size
            limits. The MCP raw-byte limit names GET /storage/archive.zip for REST
            recovery. One attempt uses the storage deadline. Errors return
            is_error=true with structured {}, without partial ZIP bytes.
        """
        try:
            response = await ctx.client.binary_request(
                "GET",
                "/storage/archive.zip",
                params={"prefix": params.prefix},
                timeout=ctx.settings.storage_timeout,
                recovery="GET /storage/archive.zip?"
                + urlencode({"prefix": params.prefix}),
            )
            content = response.content
            payload = {
                "prefix": params.prefix,
                "content_type": "application/zip",
                "sha256": hashlib.sha256(content).hexdigest(),
                "base64": base64.b64encode(content).decode(),
            }
            return render(
                payload,
                params.response_format,
                title=f"ZIP {params.prefix}",
                rows=_download_rows,
            )
        except Exception as exc:
            return error_result(exc)

    @mcp.tool(
        name="ocr_delete_storage_archive",
        annotations={
            "title": "Delete a storage archive",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def delete_storage_archive(
        params: DeleteStorageArchiveInput,
    ) -> StructuredToolResult:
        """Irreversibly delete one selected archive and all its artifacts.

        Requires explicit user intent. The MCP guard compares confirm exactly with
        the job_id encoded by the selected prefix before any HTTP request. The API
        checks again. There is no undo or server-enforced prior-list requirement.

        Args:
            params (DeleteStorageArchiveInput): prefix (str from a listing);
                confirm (str, exact job_id from that same item; case/spacing preserved);
                response_format (markdown/json).

        Returns:
            Markdown deletion count or JSON, with identical structured data:
            {"deleted_objects":int}.
            The nonnegative count is the API's confirmed object-deletion count.

        Examples:
            Use when: the user explicitly approved deletion of this archive.
            Use when: deliberately retrying a reported partial deletion of this target.
            Don't use when: finding targets; use ocr_list_storage_archives first.
            Don't use when: removing one artifact; use ocr_delete_storage_file.

        Error Handling:
            Invalid prefixes, padded/case-changed/wrong confirmations are tool errors
            before HTTP. API 409 protects mismatches and active archives; 503 can
            report storage_delete_partial with deleted_objects.
            DELETE is never retried automatically and has one attempt under
            MCP_STORAGE_TIMEOUT; after a timeout, list the archive before any
            deliberate retry. Errors return is_error=true and structured {}; a
            partial delete must not be reported as wholly undone.
        """
        try:
            require_archive_confirm(params.prefix, params.confirm)
            payload = await ctx.client.request(
                "DELETE",
                "/storage/archive",
                json={"confirm": params.confirm},
                params={"prefix": params.prefix},
                budget=ctx.settings.storage_timeout,
            )
            return render(
                payload,
                params.response_format,
                title=f"Deleted archive {params.prefix}",
                rows=lambda p: [bullet("Deleted objects", p.get("deleted_objects"))],
            )
        except Exception as exc:
            return error_result(exc)

    @mcp.tool(
        name="ocr_delete_storage_file",
        annotations={
            "title": "Delete one stored file",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def delete_storage_file(
        params: DeleteStorageFileInput,
    ) -> StructuredToolResult:
        """Irreversibly delete one artifact and update its archive manifest.

        Requires explicit user intent. The MCP guard compares confirm exactly with
        the full relative artifact name, including nested directories, before HTTP.
        The API checks again. manifest.json cannot be deleted on its own.

        Args:
            params (DeleteStorageFileInput): key (str from archive details);
                confirm (str, exact files[].name, preserving whitespace and case);
                response_format (markdown/json).

        Returns:
            Markdown deletion count or JSON, with identical structured data:
            {"deleted_objects":int}.
            Successful deletion normally reports one; inspect the returned count.

        Examples:
            Use when: the user approved deleting exactly this stored artifact.
            Use when: a deliberate retry follows a reported partial deletion.
            Don't use when: selecting a file or name; use ocr_get_storage_archive.
            Don't use when: deleting every artifact; use ocr_delete_storage_archive.

        Error Handling:
            Invalid keys, standalone manifest deletion and nonexact confirmations
            fail before HTTP. API 404 means gone; 409 protects mismatches/concurrency;
            503 can mean read-only storage or a partial delete. DELETE has no automatic
            retry and one attempt under MCP_STORAGE_TIMEOUT; after a timeout,
            inspect the archive first. Failures return is_error=true with
            structured {}.
        """
        try:
            require_file_confirm(params.key, params.confirm)
            payload = await ctx.client.request(
                "DELETE",
                "/storage/file",
                json={"confirm": params.confirm},
                params={"key": params.key},
                budget=ctx.settings.storage_timeout,
            )
            return render(
                payload,
                params.response_format,
                title=f"Deleted {params.key}",
                rows=lambda p: [bullet("Deleted objects", p.get("deleted_objects"))],
            )
        except Exception as exc:
            return error_result(exc)
