"""Security preflight follows the existing OCR authentication and Compose model."""

from copy import deepcopy

import pytest

from scripts.verify_token_config import validate_config as validate_tokens
from scripts.verify_compose_security import validate_config as validate_compose


def configured(**overrides):
    """Return a valid baseline token configuration with optional test overrides."""
    return {
        "MCP_API_TOKENS": "mcp-secret:agent",
        "OCR_SERVER_API_TOKENS": "api-secret:console",
        "UPSTREAM_CLIENT_TOKEN": "service-secret",
        **overrides,
    }


def test_service_token_and_optional_api_auth():
    assert validate_tokens(configured()) == []
    assert validate_tokens({"MCP_API_TOKENS": "mcp:agent"}) == []
    assert validate_tokens({"MCP_SERVER_ENABLED": "false"}) == []
    assert (
        validate_tokens(
            {"MCP_SERVER_ENABLED": "false", "OCR_SERVER_API_TOKENS": "a:console"}
        )
        == []
    )


@pytest.mark.parametrize(
    "raw", ["", "secret", "secret:@label", "one:a,two:a", "one:a,one:b"]
)
def test_mcp_requires_valid_distinct_tokens_and_labels(raw):
    assert validate_tokens(configured(MCP_API_TOKENS=raw))


@pytest.mark.parametrize(
    "raw", ["api-secret", "api-secret:@mcp", "api-secret:a,api-secret:b"]
)
def test_api_requires_valid_distinct_tokens_and_labels(raw):
    errors = validate_tokens(configured(OCR_SERVER_API_TOKENS=raw))
    assert any("OCR_SERVER_API_TOKENS" in error for error in errors)
    assert "api-secret" not in " ".join(errors)


@pytest.mark.parametrize(
    "overrides",
    [
        {"UPSTREAM_CLIENT_TOKEN": ""},
        {"UPSTREAM_CLIENT_TOKEN": "api-secret"},
        {"MCP_API_TOKENS": "api-secret:agent"},
        {"MCP_API_TOKENS": "service-secret:agent"},
        {"OCR_SERVER_API_TOKEN": "api-secret"},
    ],
)
def test_token_relationship_failures_never_echo_secrets(overrides):
    errors = validate_tokens(configured(**overrides))
    assert errors
    assert not any(
        secret in " ".join(errors)
        for secret in ("api-secret", "mcp-secret", "service-secret")
    )


def test_storage_profile_requires_all_credentials_and_nonnegative_retention():
    values = {"MCP_SERVER_ENABLED": "false", "COMPOSE_PROFILES": "storage"}
    assert validate_tokens(values)
    for role in ("ADMIN_", "", "READER_", "BROWSER_"):
        for kind in ("ACCESS_KEY", "SECRET_KEY"):
            values[f"OCR_S3_{role}{kind}"] = role + kind
    values.update(
        OCR_SEAWEED_ADMIN_PASSWORD="admin", OCR_SEAWEED_READER_PASSWORD="reader"
    )
    assert validate_tokens(values) == []
    values["OCR_S3_RETENTION_DAYS"] = "0"
    assert validate_tokens(values) == []
    values["OCR_S3_RETENTION_DAYS"] = "-1"
    assert validate_tokens(values)
    values["OCR_S3_RETENTION_DAYS"] = "30"
    values["OCR_S3_READER_ACCESS_KEY"] = values["OCR_S3_ACCESS_KEY"]
    assert validate_tokens(values)


def test_external_storage_needs_writer_but_not_local_admin():
    values = {"MCP_SERVER_ENABLED": "false", "OCR_STORAGE_ENABLED": "true"}
    assert validate_tokens(values)
    values.update(OCR_S3_ACCESS_KEY="writer", OCR_S3_SECRET_KEY="secret")
    assert validate_tokens(values) == []


def test_storage_profile_whitespace_cannot_bypass_credential_checks():
    assert validate_tokens(
        {"MCP_SERVER_ENABLED": "false", "COMPOSE_PROFILES": " storage "}
    )


def compose(storage=False, mcp_port=8121):
    """Return a baseline hardened Compose configuration dictionary."""
    services = {}
    targets = {"ocr-api": [8020], "ocr-mcp": [mcp_port], "vllm-paddleocr": [8080]}
    if storage:
        targets.update(
            {"seaweedfs": [], "storage-gateway": [8333, 23646], "storage-init": []}
        )
    for name, ports in targets.items():
        services[name] = {
            "user": "101:101" if name == "storage-gateway" else "1000:1000",
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "ports": [
                {"host_ip": "127.0.0.1", "target": target, "published": str(target)}
                for target in ports
            ],
        }
    services["ocr-mcp"]["environment"] = {"MCP_PORT": str(mcp_port)}
    # Explicit named exception for the upstream GPU runtime.
    services["vllm-paddleocr"].pop("user")
    services["vllm-paddleocr"].pop("read_only")
    return {"services": services}


@pytest.mark.parametrize("storage", [False, True])
def test_expected_ocr_topology_and_dynamic_mcp_port(storage):
    assert validate_compose(compose(storage)) == []


@pytest.mark.parametrize(
    "change",
    [
        {"privileged": True},
        {"network_mode": "host"},
        {"cap_add": ["SYS_ADMIN"]},
        {"cap_drop": []},
        {"security_opt": []},
        {"read_only": False},
        {"user": "0:0"},
    ],
)
def test_api_hardening_regression_rejected(change):
    config = compose()
    config["services"]["ocr-api"].update(change)
    assert validate_compose(config)


@pytest.mark.parametrize("bind", [None, "0.0.0.0", "::", "8.8.8.8", "192.168.1.2"])
def test_current_loopback_boundary_cannot_silently_expand(bind):
    config = compose()
    config["services"]["vllm-paddleocr"]["ports"][0]["host_ip"] = bind
    assert validate_compose(config)


def test_unexpected_missing_or_duplicate_publications_rejected():
    for mutation in ("missing", "extra", "duplicate"):
        config = compose(True)
        ports = config["services"]["storage-gateway"]["ports"]
        if mutation == "missing":
            ports.pop()
        elif mutation == "extra":
            ports.append({"host_ip": "127.0.0.1", "target": 9333, "published": "9333"})
        else:
            ports.append(deepcopy(ports[0]))
        assert validate_compose(config)


def archive_only_compose(storage=False):
    """Return the hardened archive-only shape: no model server anywhere."""
    config = compose(storage)
    del config["services"]["vllm-paddleocr"]
    return config


@pytest.mark.parametrize("storage", [False, True])
def test_archive_only_shape_is_accepted_only_without_vllm_or_gpu(storage):
    assert validate_compose(archive_only_compose(storage), archive_only=True) == []
    # The standard shape is not an archive-only shape, and vice versa.
    assert validate_compose(compose(storage), archive_only=True)
    assert validate_compose(archive_only_compose(storage))


@pytest.mark.parametrize(
    "change",
    [
        {"depends_on": {"vllm-paddleocr": {"condition": "service_healthy"}}},
        {
            "deploy": {
                "resources": {
                    "reservations": {
                        "devices": [{"driver": "nvidia", "capabilities": ["gpu"]}]
                    }
                }
            }
        },
    ],
)
def test_archive_only_rejects_a_leaked_vllm_dependency_or_gpu_reservation(change):
    config = archive_only_compose()
    config["services"]["ocr-api"].update(change)
    assert validate_compose(config, archive_only=True)


def test_archive_only_rejects_a_gpu_reservation_on_any_service():
    config = archive_only_compose(True)
    config["services"]["ocr-mcp"]["deploy"] = {
        "resources": {"reservations": {"devices": [{"driver": "nvidia"}]}}
    }
    assert validate_compose(config, archive_only=True)
    # Hardening is not relaxed in archive-only mode.
    config = archive_only_compose()
    config["services"]["ocr-api"]["read_only"] = False
    assert validate_compose(config, archive_only=True)
