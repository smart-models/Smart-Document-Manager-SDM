"""Runtime configuration, read once at process start.

Static bearer tokens only. If a deployment needs OAuth 2.1 (RFC 7662
introspection, audience and scope checks), add the fields here and a second
verifier in ``auth.py`` -- do not bolt an identity provider onto the static
path.

Inside the repository checkout the root ``.env`` is read as a fallback; an
installed package reads only the process environment, never a ``.env`` from
whatever directory its client happened to start it in.
"""

from __future__ import annotations

import ipaddress
import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOOPBACK = "127.0.0.1"
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "*"})
_SOURCE_ROOT = Path(__file__).resolve().parents[2]


def repo_env_file(source_root: Path) -> Path | None:
    """Return the checkout's root ``.env``, or None outside a checkout.

    Explicit environment variables still win over the file, which is what makes
    one Settings class work both in a container (env only) and on a developer
    machine (file). Outside the checkout there is no trusted file to read: the
    current directory belongs to whatever project the client opened.
    """
    if (source_root / "ocr_server.py").is_file():
        return source_root / ".env"
    return None


REPO_ENV_FILE = repo_env_file(_SOURCE_ROOT)
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
# JSON-RPC framing and the small non-source arguments around the largest field.
REQUEST_ENVELOPE_BYTES = 65_536


class ConfigError(RuntimeError):
    """A configuration value is missing, malformed, or unsafe."""


def parse_api_tokens(raw: str, variable: str = "MCP_API_TOKENS") -> dict[str, str]:
    """Parse ``token:label,token:label`` into ``{token: label}``.

    Labels exist so an operator can see *which* credential authenticated a
    request without any token value ever reaching a log line, and can revoke
    one without rotating the others.

    No message below ever repeats the entry or the token half of it. A
    malformed entry is, by definition, one this parser could not split — so
    echoing it to help the operator is exactly how a secret ends up in a
    startup log or a container's crash output. Entries are identified by
    position instead; the label is safe to name because it is not a
    credential.
    """
    tokens: dict[str, str] = {}
    for position, entry in enumerate(
        (part.strip() for part in raw.split(",")), start=1
    ):
        if not entry:
            continue
        token, separator, label = entry.partition(":")
        token, label = token.strip(), label.strip()
        if not separator or not token or not label:
            raise ConfigError(
                f"{variable} entry #{position} is not in 'token:label' form "
                "(value withheld: it may contain a credential)"
            )
        if not LABEL_PATTERN.match(label):
            raise ConfigError(
                f"{variable} entry #{position} has label {label!r}, which must "
                f"match {LABEL_PATTERN.pattern}"
            )
        if token in tokens:
            raise ConfigError(
                f"{variable} entry #{position} repeats a token already used by "
                f"label {tokens[token]!r}"
            )
        if label in tokens.values():
            raise ConfigError(
                f"{variable} entry #{position} repeats label {label!r}; "
                "each credential must have a unique label"
            )
        tokens[token] = label
    return tokens


def is_wildcard_bind(host: str) -> bool:
    """Return True when ``host`` publishes on every interface."""
    return host.strip().strip("[]") in WILDCARD_HOSTS


def is_loopback(host: str) -> bool:
    """Return True when ``host`` is a loopback address."""
    candidate = host.strip().strip("[]")
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return candidate == "localhost"


def host_with_port(host: str, port: int) -> str:
    """Format an HTTP authority, including the brackets required by IPv6."""
    hostname = host.strip().strip("[]")
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return f"{hostname}:{port}"


class Settings(BaseSettings):
    """Every environment variable this server reads."""

    model_config = SettingsConfigDict(
        env_file=REPO_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    api_tokens_raw: str = Field(default="", alias="MCP_API_TOKENS", repr=False)
    allow_unauthenticated_local: bool = Field(
        default=False, alias="MCP_ALLOW_UNAUTHENTICATED_LOCAL"
    )
    # The FastAPI application this server translates for.
    upstream_url: str = Field(default="http://127.0.0.1:8020", alias="MCP_UPSTREAM_URL")
    upstream_client_token: str = Field(
        default="", alias="UPSTREAM_CLIENT_TOKEN", repr=False
    )
    # Published/policed address: never bound directly, but it seeds the Host
    # allowlist and is what a compose file publishes on.
    bind_host: str = Field(default=LOOPBACK, alias="MCP_BIND_HOST")
    # The address uvicorn actually binds. A wildcard is allowed here (inside a
    # container's own namespace it is the intended shape), refused for
    # bind_host below.
    listen_host: str = Field(default=LOOPBACK, alias="MCP_LISTEN_HOST")
    port: int = Field(default=8021, ge=1, le=65535, alias="MCP_PORT")
    allowed_hosts_raw: str = Field(default="", alias="MCP_ALLOWED_HOSTS")
    allowed_origins_raw: str = Field(default="", alias="MCP_ALLOWED_ORIGINS")
    http_timeout: float = Field(
        default=660.0, gt=0, allow_inf_nan=False, alias="MCP_HTTP_TIMEOUT"
    )
    chunk_http_timeout: float = Field(
        default=780.0, gt=0, allow_inf_nan=False, alias="MCP_CHUNK_HTTP_TIMEOUT"
    )
    chunk_max_argument_bytes: int = Field(
        default=8388608, gt=0, alias="MCP_CHUNK_MAX_ARGUMENT_BYTES"
    )
    chunk_max_result_bytes: int = Field(
        default=8388608, ge=1024, alias="MCP_CHUNK_MAX_RESULT_BYTES"
    )
    diagnostic_timeout: float = Field(
        default=5.0, gt=0, allow_inf_nan=False, alias="MCP_DIAGNOSTIC_TIMEOUT"
    )
    storage_timeout: float = Field(
        default=150.0, gt=0, allow_inf_nan=False, alias="MCP_STORAGE_TIMEOUT"
    )
    retry_attempts: int = Field(default=3, ge=1, le=10, alias="MCP_RETRY_ATTEMPTS")
    retry_backoff: float = Field(
        default=0.5, gt=0, allow_inf_nan=False, alias="MCP_RETRY_BACKOFF_SECONDS"
    )
    input_roots_raw: str = Field(default="", alias="MCP_INPUT_ROOTS")
    max_file_bytes: int = Field(
        default=30 * 1024 * 1024, gt=0, alias="MCP_MAX_FILE_BYTES"
    )

    @property
    def max_request_body_bytes(self) -> int:
        """Largest HTTP body that can carry an input the tools would accept.

        The first bound is the base64 size of ``max_file_bytes``, the same one
        the OCR API uses for its own body cap. The RAG argument cap is measured
        as compact UTF-8, while a client may send non-ASCII text as JSON
        unicode escapes (up to three times larger), hence the factor of three.
        """
        return (
            max(
                4 * ((self.max_file_bytes + 2) // 3),
                3 * self.chunk_max_argument_bytes,
            )
            + REQUEST_ENVELOPE_BYTES
        )

    @property
    def input_roots(self) -> list[Path]:
        """Semicolon-separated directories visible to this MCP process."""
        return [
            Path(root.strip()).expanduser().resolve()
            for root in self.input_roots_raw.split(";")
            if root.strip()
        ]

    @field_validator("upstream_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        """Normalise the base URL so joined paths never double a slash."""
        from urllib.parse import urlsplit

        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ConfigError("MCP_UPSTREAM_URL must be a valid HTTP(S) URL") from None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigError("MCP_UPSTREAM_URL must be an HTTP(S) URL")
        if port is not None and not 1 <= port <= 65535:
            raise ConfigError("MCP_UPSTREAM_URL port must be between 1 and 65535")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigError("MCP_UPSTREAM_URL must not contain credentials or query")
        return value.rstrip("/")

    @field_validator("bind_host")
    @classmethod
    def refuse_wildcard_publish(cls, value: str) -> str:
        """Refuse a wildcard published address.

        Publishing on every interface exposes the MCP port to the whole local
        network. If that is genuinely wanted, name the one address meant to
        serve it.
        """
        if is_wildcard_bind(value):
            raise ConfigError(
                f"MCP_BIND_HOST={value!r} publishes on every interface; name the "
                "address that should serve this port instead"
            )
        return value

    @property
    def api_tokens(self) -> dict[str, str]:
        """Return the inbound token-to-label mapping."""
        return parse_api_tokens(self.api_tokens_raw)

    @property
    def allowed_hosts(self) -> list[str]:
        """Default to the published host and local aliases; honor overrides."""
        configured = [h.strip() for h in self.allowed_hosts_raw.split(",") if h.strip()]
        if configured:
            return configured
        hosts = [self.bind_host]
        if is_loopback(self.bind_host):
            for host in (LOOPBACK, "localhost"):
                if host not in hosts:
                    hosts.append(host)
        return [
            value
            for host in hosts
            for value in (
                f"[{host.strip('[]')}]" if ":" in host else host,
                host_with_port(host, self.port),
            )
        ]

    @property
    def allowed_origins(self) -> list[str]:
        """Return the browser Origin allowlist (empty = no browser clients)."""
        return [o.strip() for o in self.allowed_origins_raw.split(",") if o.strip()]

    def model_post_init(self, _context: object) -> None:
        """Refuse a configuration that would serve unauthenticated by accident.

        Both host settings are checked when there is no inbound auth, not just
        the published one. ``bind_host`` alone is the wrong question: uvicorn
        binds ``listen_host`` (see ``__main__.py``), so
        ``MCP_BIND_HOST=127.0.0.1`` with ``MCP_LISTEN_HOST=0.0.0.0`` and no
        tokens means a process listening on every interface with no
        authentication at all. Inside a container that combination is the
        intended shape — the namespace and the published-port restriction are
        what make it safe — but this process cannot tell that it is in one,
        and the Host allowlist does not restrict peer addresses. So the
        unauthenticated mode requires loopback on both, and a container that
        needs the wildcard configures tokens like any other deployment.
        """
        tokens = self.api_tokens
        if not tokens and not self.allow_unauthenticated_local:
            raise ConfigError(
                "MCP_API_TOKENS is empty: set 'token:label' entries, or set "
                "MCP_ALLOW_UNAUTHENTICATED_LOCAL=true to serve without auth"
            )
        if not tokens:
            for name, host in (
                ("MCP_BIND_HOST", self.bind_host),
                ("MCP_LISTEN_HOST", self.listen_host),
            ):
                if not is_loopback(host):
                    raise ConfigError(
                        "MCP_ALLOW_UNAUTHENTICATED_LOCAL=true requires a "
                        f"loopback {name} (got {host!r}): without tokens this "
                        "process would accept unauthenticated requests from "
                        "the network. Configure MCP_API_TOKENS instead."
                    )
        if self.upstream_client_token and self.upstream_client_token in tokens:
            raise ConfigError(
                "MCP_API_TOKENS must be distinct from UPSTREAM_CLIENT_TOKEN: one "
                "credential must not open two services"
            )


def load_settings(**overrides: object) -> Settings:
    """Build settings, surfacing every configuration problem as ConfigError."""
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        fields = ", ".join(str(e["loc"][0]) for e in exc.errors())
        raise ConfigError(f"Invalid configuration fields: {fields}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, validated once."""
    return load_settings()
