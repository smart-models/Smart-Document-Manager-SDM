"""Coverage contract derived from OCR's runtime OpenAPI inventory."""

TOOL_ROUTES = {
    "ocr_check_health": {("GET", "/health")},
    "ocr_get_stats": {("GET", "/stats")},
    # The progress route is polled while a parse runs and relayed as MCP
    # progress notifications when the client sends a progress token.
    "ocr_parse_document": {
        ("POST", "/parse"),
        ("GET", "/parse/progress/{progress_id}"),
    },
    "ocr_recognize_image": {("POST", "/ocr")},
    "ocr_create_rag_chunks": {("POST", "/chunk"), ("POST", "/parse/chunks")},
    "ocr_list_storage_archives": {("GET", "/storage/archives")},
    "ocr_get_storage_archive": {("GET", "/storage/archive")},
    "ocr_download_storage_file": {("GET", "/storage/file")},
    "ocr_download_storage_zip": {("GET", "/storage/archive.zip")},
    "ocr_delete_storage_archive": {("DELETE", "/storage/archive")},
    "ocr_delete_storage_file": {("DELETE", "/storage/file")},
    "ocr_refine_rag_storage": {("POST", "/storage/rag/refine")},
    "ocr_split_rag_group": {("POST", "/storage/rag/split")},
    "ocr_convert_office_to_pdf": {("POST", "/office/pdf")},
}
RESOURCE_ONLY_ROUTES = {("GET", "/metrics"): "ocr://metrics"}
# Documented API routes that serve browser UIs only; no tool is needed.
BROWSER_ONLY_ROUTES: dict[tuple[str, str], str] = {}
HIDDEN_ROUTES = {
    "/": "Browser console; no model operation.",
    "/assets/config.js": "Public bootstrap flags and limits; never credentials.",
}
FRAMEWORK_PATHS = {
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}
STATIC_MOUNTS = {"/assets"}
