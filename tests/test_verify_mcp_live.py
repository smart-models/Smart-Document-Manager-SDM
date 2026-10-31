"""Live checks are strict under optimization and don't require a dotenv file."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.verify_mcp_live import live_configuration, require, summary_line


def test_summary_line_reports_actual_counts():
    report = {"tools": ["t"] * 14, "resources": ["r"] * 11, "prompts": ["p"] * 3}
    assert "14 tools, 11 resources, 3 prompts" in summary_line(report)


def test_failed_live_check_is_not_an_assert():
    with pytest.raises(RuntimeError, match="inventory mismatch"):
        require(False, "inventory mismatch")


@pytest.mark.parametrize("optimized", [False, True])
def test_failed_check_exits_nonzero_in_subprocess(optimized):
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(root), str(root / "ocr-mcp-server")]),
    }
    command = [
        sys.executable,
        *(["-O"] if optimized else []),
        "-c",
        "from scripts.verify_mcp_live import require; "
        "require(False, 'inventory mismatch')",
    ]
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode != 0
    assert "inventory mismatch" in result.stderr


def test_environment_overrides_optional_dotenv(tmp_path):
    path = tmp_path / ".env"
    path.write_text("MCP_API_TOKENS=file-token:file\nMCP_PORT=8010\n", encoding="utf-8")
    assert live_configuration(
        {"MCP_API_TOKENS": "env-token:env", "MCP_PORT": "8022"}, path
    ) == ("env-token", "http://127.0.0.1:8022/mcp")
    assert live_configuration(
        {"MCP_API_TOKENS": "env-token:env"}, tmp_path / "absent"
    ) == ("env-token", "http://127.0.0.1:8021/mcp")


def test_missing_credentials_fail_with_actionable_message(tmp_path):
    with pytest.raises(RuntimeError, match="Set MCP_API_TOKENS"):
        live_configuration({}, tmp_path / "absent")
