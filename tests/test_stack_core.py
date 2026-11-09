"""Launch planning against the real GPU-only OCR service topology."""

import json
from dataclasses import asdict

import pytest

from scripts.stack_core import (
    ConfigurationError,
    build_plan,
    redact_config,
    render_shell,
    sibling,
)


def plan(command="up", env=None, **cli):
    """Build a launch plan with default test paths and optional overrides."""
    return build_plan(
        env or {},
        {
            "command": command,
            "compose-file": "/repo with spaces/compose.yml",
            "env-file": "/repo with spaces/.env",
            **cli,
        },
    )


def test_up_builds_before_start_and_waits_for_model_and_api():
    result = plan()
    assert result.steps[0].argv[-4:] == [
        "build",
        "vllm-paddleocr",
        "ocr-api",
        "ocr-mcp",
    ]
    starts = [step.argv for step in result.steps if "up" in step.argv]
    assert starts[0][-1] == "vllm-paddleocr"
    assert "--wait" in starts[0]
    assert any("ocr-api" in argv and "--wait" in argv for argv in starts)
    assert starts[-1][-1] == "ocr-mcp"
    assert all(
        "cpu" not in step.argv and "gpu" not in step.argv for step in result.steps
    )


def test_storage_provisioning_finishes_before_api_start():
    result = plan(env={"COMPOSE_PROFILES": "storage"})
    descriptions = [step.description for step in result.steps]
    provision = next(i for i, step in enumerate(result.steps) if "run" in step.argv)
    api = next(
        i
        for i, step in enumerate(result.steps)
        if "up" in step.argv and "ocr-api" in step.argv
    )
    assert provision < api, descriptions
    assert result.steps[provision].argv[-3:] == ["--rm", "--no-deps", "storage-init"]
    assert any(
        "storage-gateway" in step.argv and "--wait" in step.argv
        for step in result.steps[:provision]
    )
    assert all("--profile" in step.argv for step in result.steps)


def test_no_mcp_builds_only_api_and_removes_existing_mcp():
    result = plan(**{"flag-no-mcp": "true"})
    assert result.steps[0].argv[-3:] == ["build", "vllm-paddleocr", "ocr-api"]
    assert any(
        step.argv[-4:] == ["rm", "--stop", "--force", "ocr-mcp"]
        for step in result.steps
    )
    assert not any(
        "up" in step.argv and "ocr-mcp" in step.argv for step in result.steps
    )


def test_no_build_keeps_existing_images():
    assert not any("build" in step.argv for step in plan(**{"no-build": "true"}).steps)


def test_frontend_cli_override_is_exported_not_a_secret_argument():
    result = plan(env={"EXPOSE_FRONTEND": "true"}, **{"flag-no-frontend": "true"})
    assert result.environment == {"EXPOSE_FRONTEND": "false"}


def test_gpu_device_cli_override_is_exported_over_the_env_file():
    result = plan(env={"OCR_GPU_DEVICE": "0"}, **{"gpu-device": "1"})
    assert result.environment == {"OCR_GPU_DEVICE": "1"}
    assert "NVIDIA GPU 1" in " ".join(result.messages)


def test_gpu_device_falls_back_to_the_env_file_then_gpu_zero():
    assert plan(env={"OCR_GPU_DEVICE": "2"}).resolved["gpu_device"] == "2"
    assert plan("config").resolved["gpu_device"] == "0"
    assert plan().environment == {}


def test_gpu_device_is_validated_in_env_file_and_limited_to_up_and_config():
    with pytest.raises(ConfigurationError):
        plan(env={"OCR_GPU_DEVICE": "all"})
    with pytest.raises(ConfigurationError):
        plan("build", **{"gpu-device": "1"})


def test_cli_mcp_override_has_precedence_over_invalid_environment():
    result = plan(env={"MCP_SERVER_ENABLED": "invalid"}, **{"flag-no-mcp": "true"})
    assert result.resolved["mcp_server_enabled"] == "false"


def test_urls_describe_actual_security_without_credential_values():
    result = plan(
        "urls",
        env={
            "OCR_SERVER_API_TOKENS": "secret-api-value:console",
            "MCP_API_TOKENS": "secret-mcp-value:agent",
            "OCR_SERVER_PORT": "8120",
            "COMPOSE_PROFILES": "storage",
        },
    )
    output = json.dumps(asdict(result))
    assert "secret-api-value" not in output and "secret-mcp-value" not in output
    assert "127.0.0.1:8120" in output and "8021/mcp" in output
    assert "8081" in output and "8333" in output and "23646" in output
    assert "bearer" in output.lower()
    assert "without authentication" in " ".join(plan("urls").summary)


def test_pull_targets_registry_images_and_does_not_ignore_failures():
    result = plan("pull", env={"COMPOSE_PROFILES": "storage"})
    argv = result.steps[0].argv
    assert argv[-3:] == ["build", "--pull", "vllm-paddleocr"]
    assert result.steps[1].argv[-3:] == ["pull", "seaweedfs", "storage-gateway"]
    assert "--ignore-pull-failures" not in argv


def test_custom_vllm_image_is_pulled_and_never_overwritten_by_launcher_builds():
    environment = {"OCR_VLLM_IMAGE": "registry.example/ocr:custom"}
    for command in ("build", "up"):
        result = plan(command, env=environment)
        builds = [step.argv for step in result.steps if "build" in step.argv]
        assert builds and all("vllm-paddleocr" not in argv for argv in builds)
    assert plan("pull", env=environment).steps[0].argv[-2:] == [
        "pull",
        "vllm-paddleocr",
    ]


@pytest.mark.parametrize(
    "cli",
    [
        {"flag-mcp": "true", "flag-no-mcp": "true"},
        {"flag-frontend": "true", "flag-no-frontend": "true"},
        {"hardware": "cpu"},
        {"wait-timeout": "0"},
        {"gpu-device": "-1"},
        {"gpu-device": "x"},
        {"gpu-device": "0,1"},
        {"gpu-device": "01"},
    ],
)
def test_invalid_cli_rejected(cli):
    with pytest.raises(ConfigurationError):
        plan(**cli)


def test_unknown_profiles_and_multiline_output_rejected():
    with pytest.raises(ConfigurationError):
        plan(env={"COMPOSE_PROFILES": "cpu"})
    with pytest.raises(ConfigurationError):
        render_shell(plan(**{"env-file": "bad\npath"}))


def test_restart_preserves_container_environment_and_skips_one_shot_init():
    result = plan("restart", env={"COMPOSE_PROFILES": "storage"})
    assert len(result.steps) == 1
    assert "storage-init" not in result.steps[0].argv
    assert "vllm-paddleocr" in result.steps[0].argv


def test_redaction_includes_the_aws_access_key_id_alias_used_by_seaweedfs():
    config = {
        "environment": {
            "AWS_ACCESS_KEY_ID": "admin-access-key",
            "AWS_SECRET_ACCESS_KEY": "admin-secret",
        }
    }
    assert redact_config(config) == {
        "environment": {
            "AWS_ACCESS_KEY_ID": "<redacted>",
            "AWS_SECRET_ACCESS_KEY": "<redacted>",
        }
    }


ARCHIVE_ENV = {"OCR_STORAGE_ENABLED": "true", "OCR_STORAGE_BROWSER_ENABLED": "true"}


def test_archive_only_up_never_builds_starts_or_waits_for_vllm_or_a_gpu():
    result = plan(
        env={**ARCHIVE_ENV, "COMPOSE_PROFILES": "storage"},
        **{"flag-archive-only": "true"},
    )
    assert result.resolved["archive_only"] == "true"
    assert result.resolved["gpu_device"] == ""
    assert result.environment == {"OCR_ARCHIVE_ONLY": "true"}
    assert all(
        any(arg.endswith("compose.archive-only.yml") for arg in step.argv)
        for step in result.steps
    )
    assert result.steps[0].argv[-3:] == ["build", "ocr-api", "ocr-mcp"]
    starts = [step.argv for step in result.steps if "up" in step.argv]
    assert starts and not any("vllm-paddleocr" in argv for argv in starts)
    removal = next(
        i
        for i, step in enumerate(result.steps)
        if step.argv[-4:] == ["rm", "--stop", "--force", "vllm-paddleocr"]
    )
    launches = [
        (i, step.argv[-1])
        for i, step in enumerate(result.steps)
        if "up" in step.argv or "run" in step.argv
    ]
    assert removal < launches[0][0]
    assert [target for _, target in launches] == [
        "storage-gateway",
        "storage-init",
        "ocr-api",
        "ocr-mcp",
    ]
    assert "archive-only" in " ".join(result.messages).lower()
    assert "NVIDIA GPU" not in " ".join(result.messages)
    summary = " ".join(result.summary)
    assert "archive-only" in summary and "8081" not in summary


def test_archive_only_from_the_env_file_covers_every_planned_command():
    env = {**ARCHIVE_ENV, "OCR_ARCHIVE_ONLY": "true", "COMPOSE_PROFILES": "storage"}
    assert plan("build", env=env).steps[0].argv[-3:] == ["build", "ocr-api", "ocr-mcp"]
    pull = plan("pull", env=env)
    assert [step.argv[-3:] for step in pull.steps] == [
        ["pull", "seaweedfs", "storage-gateway"]
    ]
    assert plan("pull", env={**env, "COMPOSE_PROFILES": ""}).steps == []
    restart = plan("restart", env=env)
    assert len(restart.steps) == 1 and "vllm-paddleocr" not in restart.steps[0].argv
    config = plan("config", env=env)
    assert config.steps[0].argv.count("--file") == 2
    assert config.environment == {}  # configured, not forced: nothing to export
    assert plan("urls", env=env).resolved["archive_only"] == "true"
    custom = plan("pull", env={**env, "OCR_VLLM_IMAGE": "registry.example/ocr:x"})
    assert not any("vllm-paddleocr" in step.argv for step in custom.steps)
    assert custom.warnings == []


def test_archive_only_refuses_incomplete_storage_and_gpu_selection():
    for env in (
        {},
        {"OCR_STORAGE_ENABLED": "true"},
        {"OCR_STORAGE_BROWSER_ENABLED": "true"},
    ):
        with pytest.raises(ConfigurationError, match="OCR_STORAGE_BROWSER_ENABLED"):
            plan(env=env, **{"flag-archive-only": "true"})
    with pytest.raises(ConfigurationError, match="OCR_STORAGE_BROWSER_ENABLED"):
        plan("verify", env={"OCR_ARCHIVE_ONLY": "true"})
    with pytest.raises(ConfigurationError, match="gpu-device"):
        plan(env=ARCHIVE_ENV, **{"flag-archive-only": "true", "gpu-device": "1"})


def test_standard_plan_is_unchanged_without_archive_only():
    result = plan(env=ARCHIVE_ENV)
    assert result.resolved["archive_only"] == "false"
    assert not any("compose.archive-only.yml" in step.argv for step in result.steps)
    assert not any("rm" in step.argv for step in result.steps)
    assert "8081" in " ".join(result.summary)


def test_sibling_keeps_the_callers_separator_style():
    assert (
        sibling("/repo with spaces/compose.yml", "x.yml") == "/repo with spaces/x.yml"
    )
    assert sibling(r"C:\repo\compose.yml", "x.yml") == r"C:\repo\x.yml"
    assert sibling("compose.yml", "x.yml") == "x.yml"
