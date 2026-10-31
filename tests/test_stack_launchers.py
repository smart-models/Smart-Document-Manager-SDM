"""Real launchers tested against isolated Docker fakes; no GPU or local .env."""

import json
from pathlib import Path
import re

import pytest

from tests import stack_support as support


@pytest.fixture(params=["bash", "powershell"])
def launcher(request):
    if request.param == "bash":
        if support.bash_executable() is None:
            pytest.skip("Bash unavailable")
        return support.run_bash_launcher
    if support.powershell_executable() is None:
        pytest.skip("PowerShell unavailable")
    return support.run_powershell_launcher


@pytest.fixture
def stub(tmp_path):
    return {
        "stub_dir": support.write_docker_stub(tmp_path / "bin"),
        "log_path": tmp_path / "docker.log",
        "env": support.write_stub_answers(tmp_path / "answers with spaces"),
    }


def test_launcher_files_exist():
    assert support.STACK_PS1.is_file() and support.STACK_SH.is_file()


def test_verify_does_not_need_daemon_and_never_sends_tokens_on_argv(launcher, stub):
    stub["env"]["STACK_STUB_INFO_EXIT"] = "1"
    result = launcher(["verify"], **stub)
    assert result.returncode == 0, result.stderr
    assert not any(invocation == "info" for invocation in result.invocations)
    assert not any(
        "fake-api-secret" in invocation or "fake-mcp-secret" in invocation
        for invocation in result.invocations
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["up", "cpu"],
        ["up", "--mcp", "--no-mcp"],
        ["up", "--frontend", "--no-frontend"],
        ["build", "--wait"],
        ["status", "--no-build"],
        ["up", "--wait-timeout", "0"],
        ["up", "--gpu-device", "x"],
        ["build", "--gpu-device", "1"],
        ["bogus"],
    ],
)
def test_invalid_options_fail_before_mutation(launcher, stub, arguments):
    result = launcher(arguments, **stub)
    assert result.returncode == 2, result.stderr
    assert not any(
        " up " in f" {line} " or " build " in f" {line} " for line in result.invocations
    )


def test_failed_preflight_prevents_build_and_up(launcher, stub):
    Path(stub["env"]["STACK_STUB_ENVIRONMENT_FILE"]).write_text(
        "MCP_API_TOKENS=\n", encoding="utf-8"
    )
    result = launcher(["up"], **stub)
    assert result.returncode == 2, result.stderr
    assert "MCP_API_TOKENS" in result.stderr
    assert not any(
        " build " in f" {line} " or " up " in f" {line} " for line in result.invocations
    )


def test_build_failure_stops_start_and_preserves_docker_exit_code(launcher, stub):
    stub["env"].update(STACK_STUB_FAIL_OPERATION="build", STACK_STUB_FAIL_CODE="19")
    result = launcher(["up"], **stub)
    assert result.returncode == 19, result.stderr
    assert any(" build " in f" {line} " for line in result.invocations)
    assert not any(" up " in f" {line} " for line in result.invocations)


def test_storage_failure_prevents_api_start(launcher, stub):
    env_file = Path(stub["env"]["STACK_STUB_ENVIRONMENT_FILE"])
    values = env_file.read_text(encoding="utf-8") + "COMPOSE_PROFILES=storage\n"
    for role in ("ADMIN_", "", "READER_", "BROWSER_"):
        for kind in ("ACCESS_KEY", "SECRET_KEY"):
            values += f"OCR_S3_{role}{kind}={role}{kind}\n"
    values += "OCR_SEAWEED_ADMIN_PASSWORD=admin\nOCR_SEAWEED_READER_PASSWORD=reader\n"
    env_file.write_text(values, encoding="utf-8")
    stub["env"].update(STACK_STUB_FAIL_OPERATION="run", STACK_STUB_FAIL_CODE="19")
    result = launcher(["up", "--no-build"], **stub)
    assert result.returncode == 19, result.stderr
    assert any("storage-init" in line for line in result.invocations)
    assert not any(
        " up " in f" {line} " and "ocr-api" in line for line in result.invocations
    )


def test_readme_commands_and_flags_match_both_help_outputs(launcher, stub):
    result = launcher(["help"], **stub)
    assert result.returncode == 0
    reference = (support.REPOSITORY_ROOT / "scripts/README.md").read_text(
        encoding="utf-8"
    )
    for command in (
        "help",
        "version",
        "verify",
        "up",
        "build",
        "pull",
        "restart",
        "config",
        "urls",
        "status",
        "logs",
        "down",
    ):
        assert f"| `{command}` |" in reference
        assert re.search(rf"^  {command}\s", result.stdout, re.MULTILINE)
    pattern = (
        r"--[a-z]+(?:-[a-z]+)*"
        if launcher == support.run_bash_launcher
        else r"(?<!\w)-[A-Z][A-Za-z]+"
    )
    flag_table = reference.split("| Bash | PowerShell |", 1)[1].split("\n\n", 1)[0]
    documented = set(re.findall(pattern, flag_table))
    actual = set(re.findall(pattern, result.stdout))
    assert documented == actual


def test_config_redacts_multiline_and_numeric_credentials(launcher, stub):
    path = Path(stub["env"]["STACK_STUB_CONFIG_FILE"])
    config = json.loads(path.read_text(encoding="utf-8"))
    config["services"]["ocr-api"]["environment"] = {
        "OCR_SERVER_API_TOKENS": "secret-line-one\nsecret-line-two",
        "OCR_S3_SECRET_KEY": 123456789,
        "OCR_REQUEST_TIMEOUT": "600",
        "UNSET_TOKEN": "",
    }
    path.write_text(json.dumps(config), encoding="utf-8")
    result = launcher(["config"], **stub)
    assert result.returncode == 0, result.stderr
    assert "secret-line" not in result.stdout and "123456789" not in result.stdout
    assert "<redacted>" in result.stdout and "600" in result.stdout
    assert (
        json.loads(result.stdout)["services"]["ocr-api"]["environment"]["UNSET_TOKEN"]
        == ""
    )


def test_config_docker_failure_is_not_masked_by_redactor(launcher, stub):
    stub["env"]["STACK_STUB_CONFIG_EXIT"] = "37"
    result = launcher(["config"], **stub)
    assert result.returncode == 37, result.stderr
    assert result.stdout == ""


def test_volume_removal_requires_confirmation_even_without_python(launcher, stub):
    result = launcher(["down", "--volumes"], **stub)
    assert result.returncode == 2, result.stderr
    assert not any(" down " in f" {line} " for line in result.invocations)


def test_recovery_ignores_broken_local_dotenv_and_python(
    launcher, stub, tmp_path, monkeypatch
):
    repo = tmp_path / "recovery repo"
    repo.mkdir()
    for name in ("stack.ps1", "stack.sh", "compose.yml", ".env.example"):
        (repo / name).write_bytes((support.REPOSITORY_ROOT / name).read_bytes())
    (repo / ".env").write_text("BROKEN='unterminated\n", encoding="utf-8")
    monkeypatch.setattr(support, "STACK_SH", repo / "stack.sh")
    monkeypatch.setattr(support, "STACK_PS1", repo / "stack.ps1")
    stub["env"]["PATH"] = support.python_free_path(tmp_path / "path without python")
    for command in (
        ["status"],
        ["logs", "--tail", "5"],
        ["down"],
        ["help"],
        ["version"],
    ):
        result = launcher(command, **stub)
        assert result.returncode == 0, result.stderr
        if command[0] not in {"help", "version"}:
            assert any("--profile storage" in line for line in result.invocations)


@pytest.mark.parametrize(
    "arguments",
    [
        ["up", "--dry-run"],
        ["up", "--wait", "--no-build", "--dry-run"],
        ["up", "--no-mcp", "--no-frontend", "--dry-run"],
        ["up", "--gpu-device", "1", "--dry-run"],
        ["build", "--dry-run"],
        ["pull", "--dry-run"],
        ["restart", "--dry-run"],
        ["config", "--dry-run"],
        ["urls"],
        ["status", "--dry-run"],
        ["logs", "--tail", "10", "--service", "ocr-api,ocr-mcp", "--dry-run"],
        ["down", "--volumes", "--yes", "--dry-run"],
    ],
)
def test_shell_parity_and_no_cpu_gpu_profile(arguments, stub):
    if support.bash_executable() is None or support.powershell_executable() is None:
        pytest.skip("Parity requires Bash and PowerShell")
    bash = support.run_bash_launcher(arguments, **stub)
    powershell = support.run_powershell_launcher(arguments, **stub)
    assert bash.returncode == powershell.returncode == 0, (
        bash.stderr,
        powershell.stderr,
    )

    def normalized(lines):
        return [
            line.replace("\\", "/").replace('"', "").replace("/c/", "C:/").strip()
            for line in lines
        ]

    assert normalized(bash.invocations) == normalized(powershell.invocations)
    assert "--profile cpu" not in bash.stdout and "--profile gpu" not in bash.stdout


@pytest.mark.parametrize("version,expected", [("2.24.3", 1), ("2.100.1", 0)])
def test_compose_version_gate_handles_numeric_versions(
    launcher, stub, version, expected
):
    stub["env"]["STACK_STUB_COMPOSE_VERSION"] = version
    result = launcher(["status"], **stub)
    assert result.returncode == expected


def test_unreachable_daemon_fails_status_with_human_message(launcher, stub):
    stub["env"]["STACK_STUB_INFO_EXIT"] = "1"
    result = launcher(["status"], **stub)
    assert result.returncode == 1
    assert "Docker daemon not reachable" in result.stderr
