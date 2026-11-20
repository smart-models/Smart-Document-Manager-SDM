"""Build and smoke-test the MCP wheel in an isolated Python 3.11 environment.

Requires uv on PATH. Dependencies may be downloaded; --offline requires an
already populated uv cache. Only inventory, resource hashes and status are
written to the report. No OCR backend or credentials are needed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from email.parser import HeaderParser
from pathlib import Path


def require(condition: bool, message: str) -> None:
    """Keep verification active under Python -O."""
    if not condition:
        raise RuntimeError(message)


def wheel_licence(wheel: Path) -> str:
    """Check that the wheel declares AGPL-3.0-only and bundles its LICENSE."""
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_name = next(
            (name for name in names if name.endswith(".dist-info/METADATA")), None
        )
        require(metadata_name is not None, "Wheel metadata is missing")
        metadata = HeaderParser().parsestr(archive.read(metadata_name).decode())
        dist_info = metadata_name.rsplit("/", 1)[0]
        require(
            metadata["License-Expression"] == "AGPL-3.0-only",
            "Wheel does not declare AGPL-3.0-only",
        )
        require(
            metadata.get_all("License-File") == ["LICENSE"]
            and f"{dist_info}/licenses/LICENSE" in names,
            "Wheel does not bundle LICENSE",
        )
    return metadata["License-Expression"]


def clean_environment() -> dict[str, str]:
    """Remove project settings and checkout import overrides from children."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("MCP_", "OCR_", "PYTHONPATH"))
        and key.upper() != "UPSTREAM_CLIENT_TOKEN"
    }


def run_stage(
    command: list[str],
    *,
    cwd: Path,
    stage: str,
    timeout: int = 180,
) -> str:
    """Run a bounded stage without echoing installation environment or logs."""
    result = subprocess.run(
        command,
        cwd=cwd,
        env=clean_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )
    require(result.returncode == 0, f"{stage} failed (exit {result.returncode})")
    return result.stdout


async def probe(entrypoint: Path) -> dict:
    """Check installed resources and a real stdio handshake outside checkout."""
    from importlib import resources

    import ocr_mcp
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from ocr_mcp.resources.docs import DOC_RESOURCES, html_to_text
    from ocr_mcp.tool_map import RESOURCE_ONLY_ROUTES, TOOL_ROUTES

    require(sys.version_info[:2] == (3, 11), "Wheel verification requires Python 3.11")
    require(
        Path(ocr_mcp.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()),
        "Package was not imported from the isolated environment",
    )
    require(entrypoint.is_file(), "Installed ocr-mcp entrypoint is missing")
    run_stage([str(entrypoint), "--help"], cwd=Path.cwd(), stage="Entrypoint help")
    docs = {}
    package = resources.files("ocr_mcp.resources").joinpath("docs")
    for file_name, _title, _description in DOC_RESOURCES.values():
        data = package.joinpath(file_name).read_bytes()
        require(bool(data.strip()), "Packaged documentation is empty")
        docs[file_name] = {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    params = StdioServerParameters(
        command=str(entrypoint),
        args=["--transport", "stdio"],
        env={"MCP_UPSTREAM_URL": "http://127.0.0.1:1"},
        cwd=Path.cwd(),
    )
    async with asyncio.timeout(30):
        async with Client(stdio_client(params), read_timeout_seconds=10) as client:
            tools = sorted(item.name for item in (await client.list_tools()).tools)
            require(set(tools) == set(TOOL_ROUTES), "Wheel tool inventory mismatch")
            resource_uris = sorted(
                str(item.uri) for item in (await client.list_resources()).resources
            )
            require(
                set(resource_uris)
                == set(DOC_RESOURCES) | set(RESOURCE_ONLY_ROUTES.values()),
                "Wheel resource inventory mismatch",
            )
            for uri, (file_name, _title, _description) in DOC_RESOURCES.items():
                response = await client.read_resource(uri)
                require(bool(response.contents), "Packaged resource was not served")
                text = response.contents[0].text
                expected = package.joinpath(file_name).read_text("utf-8")
                if file_name.endswith(".html"):
                    # Guides are served as the text of their packaged page.
                    expected = html_to_text(expected)
                require(text == expected, "Packaged resource content mismatch")
            prompts = sorted(
                item.name for item in (await client.list_prompts()).prompts
            )
            require(
                prompts == ["ocr_diagnose", "ocr_extract_document", "ocr_prepare_rag"],
                "Wheel prompt inventory mismatch",
            )
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "package_version": ocr_mcp.__version__,
        "isolated_import": True,
        "entrypoint": True,
        "stdio_handshake": True,
        "tools": tools,
        "resources": resource_uris,
        "prompts": prompts,
        "packaged_docs": docs,
    }


def main() -> None:
    """Build, install, run the isolated probe and persist its small report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--entrypoint", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "docs/verification/mcp-wheel.json",
    )
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(asyncio.run(probe(args.entrypoint)), ensure_ascii=True))
        return
    require(sys.version_info[:2] == (3, 11), "Run this script with Python 3.11")
    uv = shutil.which("uv")
    require(uv is not None, "Install uv before running wheel verification")
    root = Path(__file__).resolve().parents[1]
    offline = ["--offline"] if args.offline else []
    with tempfile.TemporaryDirectory(prefix="ocr-wheel-check-") as temp:
        work = Path(temp).resolve()
        require(not work.is_relative_to(root), "Wheel probe must run outside checkout")
        run_stage(
            [
                uv,
                "build",
                *offline,
                "--wheel",
                "--out-dir",
                str(work / "dist"),
                str(root / "ocr-mcp-server"),
            ],
            cwd=work,
            stage="Wheel build",
        )
        wheels = list((work / "dist").glob("*.whl"))
        require(len(wheels) == 1, "Wheel build did not produce exactly one wheel")
        wheel = wheels[0]
        licence = wheel_licence(wheel)
        run_stage(
            [uv, "venv", *offline, "--python", sys.executable, str(work / "venv")],
            cwd=work,
            stage="Isolated environment creation",
        )
        binary_dir = work / "venv" / ("Scripts" if os.name == "nt" else "bin")
        python = binary_dir / ("python.exe" if os.name == "nt" else "python")
        entrypoint = binary_dir / ("ocr-mcp.exe" if os.name == "nt" else "ocr-mcp")
        run_stage(
            [uv, "pip", "install", *offline, "--python", str(python), str(wheel)],
            cwd=work,
            stage="Wheel and dependency installation",
        )
        result = run_stage(
            [
                str(python),
                "-I",
                str(Path(__file__).resolve()),
                "--probe",
                "--entrypoint",
                str(entrypoint),
            ],
            cwd=work,
            stage="Isolated package and stdio probe",
            timeout=50,
        )
        report = json.loads(result)
        report["wheel"] = wheel.name
        report["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
        report["license"] = licence
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    print(
        (
            "MCP wheel verified: licence metadata, isolated Python 3.11 install, "
            "packaged docs, entrypoint and stdio inventory."
        )
    )


if __name__ == "__main__":
    main()
