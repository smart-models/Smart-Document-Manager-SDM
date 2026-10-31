"""Run the MCP server with uvicorn: ``python -m ocr_mcp``."""

from __future__ import annotations

import argparse

import uvicorn

from ocr_mcp.config import get_settings, load_settings


def main() -> None:
    """Serve the streamable HTTP app on the configured port.

    ``settings.listen_host`` (``MCP_LISTEN_HOST``), not ``settings.bind_host``
    (``MCP_BIND_HOST``): the two answer different questions. ``bind_host`` is
    the published/policed address -- what compose publishes on, what seeds the
    Host allowlist -- and never reaches a socket. ``listen_host`` is what this
    process actually binds inside its own network namespace, defaulting to
    loopback so running outside Docker is safe by default; a container sets it
    to ``0.0.0.0`` because inside its own namespace that is exactly what the
    published-port restriction assumes.
    """
    parser = argparse.ArgumentParser(description="Personal Smart OCR MCP server")
    parser.add_argument("--transport", choices=["http", "stdio"], default="http")
    args = parser.parse_args()
    if args.transport == "stdio":
        from ocr_mcp.server import build_server, configure_package_logging

        settings = load_settings(
            allow_unauthenticated_local=True,
            bind_host="127.0.0.1",
            listen_host="127.0.0.1",
        )
        configure_package_logging()
        server, _ctx = build_server(settings)
        server.run(transport="stdio")
        return
    settings = get_settings()
    uvicorn.run("ocr_mcp.server:app", host=settings.listen_host, port=settings.port)


if __name__ == "__main__":
    main()
