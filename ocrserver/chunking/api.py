"""Opt-in HTTP adapters with bounded bodies and canonical successful files."""

import asyncio
import json
import time
from typing import NoReturn

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from ..schemas import ChunkRequest, ParseChunksJsonRequest, RagResponse
from .canonical import canonical_bytes
from .errors import ChunkError

_ERROR_RESPONSES = {
    413: {"description": "Input, output or deterministic work limit exceeded."},
    415: {"description": "Unsupported request media type."},
    422: {"description": "Invalid input or no feasible structural partition."},
    500: {"description": "Chunk worker failed; no partial RAG file is returned."},
    503: {"description": "Temporary capacity or tokenizer availability failure."},
    504: {"description": "Chunk deadline exceeded; worker has been stopped."},
}


async def _run_connected(request, operation):
    """Cancel CPU work and await its cleanup when the client disconnects."""

    async def disconnected():
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    work = asyncio.create_task(operation)
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait(
            (work, watcher),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if work in done:
            return await work
        raise asyncio.CancelledError
    finally:
        watcher.cancel()
        if not work.done():
            work.cancel()
        await asyncio.gather(work, watcher, return_exceptions=True)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _loads(raw):
    return json.loads(raw, object_pairs_hook=_unique_object)


async def _body(request, maximum):
    declared = request.headers.get("content-length")
    if declared and (not declared.isdecimal() or int(declared) > maximum):
        raise ChunkError(
            "chunk_input_limit",
            "Request body exceeds input limit",
            status_code=413,
            details={"max_bytes": maximum},
        )
    parts, length = [], 0
    async for part in request.stream():
        length += len(part)
        if length > maximum:
            raise ChunkError(
                "chunk_input_limit",
                "Request body exceeds input limit",
                status_code=413,
                details={"max_bytes": maximum},
            )
        parts.append(part)
    return b"".join(parts)


def _schema(model):
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    definitions = schema.pop("$defs", {})
    definitions[model.__name__] = schema
    return definitions


def _parse_openapi(max_pages):
    """Describe JSON and multipart parse/chunk inputs with the runtime page cap."""
    schemas = _schema(ParseChunksJsonRequest)
    schema = schemas["ParseChunksJsonRequest"]
    for branch in schema["properties"]["max_pages"].get("anyOf", []):
        if branch.get("type") == "integer":
            branch["maximum"] = max_pages
    schema["oneOf"] = [
        {
            "required": [field],
            "properties": {field: {"type": "string", "pattern": r"\S"}},
        }
        for field in ("image_base64", "pdf_base64", "document_base64")
    ]
    multipart = {
        "type": "object",
        "additionalProperties": False,
        "required": ["file", "options"],
        "properties": {
            "file": {"type": "string", "format": "binary"},
            "options": {
                "type": "string",
                "contentMediaType": "application/json",
                "description": "JSON object matching ChunkOptions.",
            },
            **{
                key: value
                for key, value in schema["properties"].items()
                if key in {"source_id", "lang", "max_pages", "dpi", "filename"}
            },
        },
    }
    return schemas, {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ParseChunksJsonRequest"}
                },
                "multipart/form-data": {"schema": multipart},
            },
        }
    }


async def _parse_upload(request, settings):
    """Read bounded JSON or multipart input into a validated parse/chunk model.

    Require one source and reject duplicate/unknown form fields. Limit file reads
    before returning bytes, closing the replayed multipart form on every path.
    """
    raw = await _body(request, 4 * ((settings.max_file_bytes + 2) // 3) + 65_536)
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip() == "application/json":
        model = ParseChunksJsonRequest.model_validate(_loads(raw))
        useful = [
            value
            for value in (model.image_base64, model.pdf_base64, model.document_base64)
            if value and value.strip()
        ]
        if len(useful) != 1:
            raise ValueError("exactly one source is required")
        return model, None
    if not content_type.startswith("multipart/form-data;"):
        raise HTTPException(415, detail={"code": "unsupported_media_type"})

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    replay = Request(request.scope, receive)
    async with replay.form(max_files=1, max_fields=6) as form:
        if len(form.multi_items()) != len(form):
            raise ValueError("duplicate form field")
        if set(form) - {
            "file",
            "options",
            "source_id",
            "lang",
            "max_pages",
            "dpi",
            "filename",
        }:
            raise ValueError("unknown form field")
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ValueError("exactly one file is required")
        values = {key: value for key, value in form.items() if key != "file"}
        values["filename"] = upload.filename or values.get("filename")
        values["options"] = _loads(values.get("options", ""))
        for key in ("max_pages", "dpi"):
            if key in values:
                values[key] = int(values[key])
        model = ParseChunksJsonRequest.model_validate(values)
        data = await upload.read(settings.max_file_bytes + 1)
        if len(data) > settings.max_file_bytes:
            raise ChunkError(
                "file_too_large",
                "Uploaded file exceeds input limit",
                status_code=413,
            )
        return model, data


def _refuse_default_truncation(max_pages: int, total: int) -> NoReturn:
    """Reject a document longer than the default cap before OCR spends anything."""
    raise ChunkError(
        "incomplete_ocr",
        "Default page truncation was not accepted",
        details={
            "reason": "pages_truncated",
            "max_pages": max_pages,
            "total_pages": total,
        },
    )


def register_chunk_routes(
    app,
    settings,
    parse_service,
    runtime,
    *,
    capture_factory,
    archive,
):
    """Register both source paths against one parser and one CPU runtime."""
    schemas, parse_extra = _parse_openapi(settings.max_pages)
    schemas.update(_schema(ChunkRequest))

    @app.exception_handler(ChunkError)
    async def chunk_error(request, error):
        headers = {"Retry-After": "1"} if error.status_code == 503 else None
        return JSONResponse(
            {"detail": error.to_detail()},
            status_code=error.status_code,
            headers=headers,
        )

    @app.post(
        "/chunk",
        tags=["CHUNK"],
        response_model=RagResponse,
        responses=_ERROR_RESPONSES,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ChunkRequest"}
                    }
                },
            },
        },
    )
    async def chunk(request: Request):
        if (
            request.headers.get("content-type", "").split(";", 1)[0]
            != "application/json"
        ):
            raise HTTPException(415, detail={"code": "unsupported_media_type"})
        raw = await _body(request, settings.chunk_max_input_bytes)
        try:
            model = ChunkRequest.model_validate(_loads(raw))
        except ValidationError as error:
            raise RequestValidationError(
                error.errors(include_input=False, include_context=False)
            ) from None
        except (ValueError, TypeError):
            raise HTTPException(422, detail={"code": "invalid_json_body"}) from None
        result = await _run_connected(request, runtime.run(model.model_dump()))
        return Response(canonical_bytes(result), media_type="application/json")

    @app.post(
        "/parse/chunks",
        tags=["PARSE"],
        response_model=RagResponse,
        responses=_ERROR_RESPONSES,
        openapi_extra=parse_extra,
    )
    async def parse_chunks(request: Request):
        """Parse once, chunk the validated result and optionally archive the output.

        Enforce runtime page/token limits before inference and reject implicit
        truncation. Preserve explicit scope and exact canonical chunk bytes when
        passing the successful result to best-effort archival.
        """
        try:
            model, file_bytes = await _parse_upload(request, settings)
        except ValidationError as error:
            raise RequestValidationError(
                error.errors(include_input=False, include_context=False)
            ) from None
        except (ValueError, TypeError):
            raise HTTPException(422, detail={"code": "invalid_json_body"}) from None
        if model.max_pages is not None and model.max_pages > settings.max_pages:
            raise HTTPException(
                422,
                detail={
                    "code": "max_pages_out_of_range",
                    "max": settings.max_pages,
                },
            )
        if model.options.max_tokens > settings.chunk_max_tokens:
            raise ChunkError(
                "invalid_chunk_options",
                "Token budget exceeds configured maximum",
                details={"max_tokens": settings.chunk_max_tokens},
            )
        capture = capture_factory(
            "parse-chunks",
            model.model_dump(
                exclude={
                    "image_base64",
                    "pdf_base64",
                    "document_base64",
                }
            ),
            original_filename=model.filename,
        )
        if capture is not None:
            capture.effective.update(
                {
                    "options": model.options.model_dump(),
                    "source_id": model.source_id,
                    "lang": model.lang,
                }
            )
        parsed = await parse_service(
            file_bytes,
            (model.image_base64 or "").strip() or None,
            (model.pdf_base64 or "").strip() or None,
            "json",
            model.max_pages or settings.max_pages,
            model.dpi or settings.parse_dpi,
            str(model.dpi) if model.dpi is not None else None,
            "html",
            False,
            request.state.start_time,
            document_b64=(model.document_base64 or "").strip() or None,
            capture=capture,
            on_truncation=(
                None if model.max_pages is not None else _refuse_default_truncation
            ),
        )
        request.state.pages = len(parsed.pages)
        structured = parsed.model_dump()
        truncation = next(
            (
                warning
                for warning in structured["warnings"]
                if warning["code"] == "pages_truncated"
            ),
            None,
        )
        scope = {
            "kind": "page_prefix" if truncation else "complete_document",
            "origin": "explicit_max_pages" if truncation else "full_parse",
            "selected_pages": [page["page"] for page in structured["pages"]],
            "requested_max_pages": model.max_pages,
            "total_pages": (truncation.get("details") or {}).get("total_pages")
            if truncation
            else len(structured["pages"]),
        }
        body = {
            "parsed_document": structured,
            "options": model.options.model_dump(),
            "source_id": model.source_id,
            "lang": model.lang,
        }
        chunk_started = time.monotonic()
        result = await _run_connected(request, runtime.run(body, scope=scope))
        if capture is not None:
            capture.timings.update(
                {
                    "chunk_time_ms": (time.monotonic() - chunk_started) * 1000,
                    "request_time_ms": (time.monotonic() - request.state.start_time)
                    * 1000,
                }
            )
        result_bytes = canonical_bytes(result)
        response = Response(result_bytes, media_type="application/json")
        await archive(
            capture,
            result,
            response,
            parsed=parsed,
            result_bytes=result_bytes,
        )
        return response

    return schemas
