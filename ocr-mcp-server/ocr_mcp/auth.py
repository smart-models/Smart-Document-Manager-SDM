"""Inbound bearer-token verification.

Static tokens only. The OAuth path (RFC 7662 introspection, audience and scope
checks) is a second ``TokenVerifier`` subclass plus the matching settings --
add it when a deployment actually has an identity provider, not before.
"""

from __future__ import annotations

import hmac
import logging

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

from ocr_mcp.config import Settings, host_with_port

logger = logging.getLogger(__name__)


def _constant_time_equals(presented: str, known: str) -> bool:
    """Compare two strings in constant time, whatever bytes they carry.

    ``hmac.compare_digest`` refuses non-ASCII ``str`` arguments with
    ``TypeError`` rather than returning False. The presented value arrives
    from an unauthenticated HTTP header (ASGI decodes headers latin-1, so any
    byte >= 0x80 lands here as a non-ASCII character), so that exception is
    reachable by anyone who can reach the port and would turn a plain
    authentication failure into a 500. Encoding both sides to UTF-8 keeps the
    comparison constant-time while making every input a verdict.
    """
    return hmac.compare_digest(presented.encode("utf-8"), known.encode("utf-8"))


class StaticTokenVerifier(TokenVerifier):
    """Verify a bearer token against a fixed token-to-label mapping."""

    def __init__(self, tokens: dict[str, str]) -> None:
        """Store an immutable copy of the token-to-label mapping."""
        self._tokens = dict(tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return an access token for a known bearer value, otherwise None.

        Every known token is compared, and the loop does not stop at the first
        match: the total time depends only on how many tokens are configured,
        never on which one (if any) matched. A plain ``dict`` lookup would
        leak that through timing, since neither hashing nor ``str.__eq__`` is
        constant-time.

        Only the label is ever logged -- never the ``AccessToken`` (its repr
        includes the plaintext token) and never the presented value on
        failure.
        """
        matched_label: str | None = None
        for known_token, label in self._tokens.items():
            if _constant_time_equals(token, known_token):
                matched_label = label
        if matched_label is None:
            logger.info("token verification failed")
            return None
        logger.info("token verified: %s", matched_label)
        return AccessToken(token=token, client_id=matched_label, scopes=[])


def is_unauthenticated(settings: Settings) -> bool:
    """Return True when this deployment serves without inbound auth."""
    return not settings.api_tokens and settings.allow_unauthenticated_local


def build_token_verifier(settings: Settings) -> TokenVerifier | None:
    """Return the verifier for this deployment, or None when auth is off."""
    if is_unauthenticated(settings):
        return None
    return StaticTokenVerifier(settings.api_tokens)


def build_auth_settings(settings: Settings) -> AuthSettings | None:
    """Return the SDK auth settings, or None when auth is off.

    No ``resource_server_url``: with it the SDK would publish OAuth
    protected-resource metadata naming this server as an authorization server
    it is not, and clients without a configured header would start an OAuth
    flow that can only fail. ``issuer_url`` is required by the SDK and unused.
    """
    if is_unauthenticated(settings):
        return None
    base = f"http://{host_with_port(settings.bind_host, settings.port)}"
    return AuthSettings(issuer_url=base, resource_server_url=None)
