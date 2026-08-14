#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Upstream selection, dialect checks and request construction."""

import hmac
from collections.abc import Mapping

import httpx
from starlette.requests import Request

# Local imports
from lmrelay.config import RelayConfig, Upstream

# Hop-by-hop headers are meaningful only to the single connection they arrived
# on and must not be relayed (RFC 9110 7.6.1).
HOP_BY_HOP = {
    "connection", "keep-alive", "transfer-encoding", "te",
    "trailer", "upgrade", "proxy-authenticate", "proxy-authorization",
}

# `host` is recomputed by httpx from the target URL. `authorization` and
# `x-api-key` carry the CALLER's credential for lmrelay itself and must never
# reach a provider — an upstream with no configured headers (Ollama) therefore
# receives no credential at all.
DROPPED_REQUEST_HEADERS = HOP_BY_HOP | {"host", "authorization", "x-api-key"}

# Starlette re-frames the relayed body as chunked, so a forwarded content-length
# would no longer match: clients either hang or truncate.
DROPPED_RESPONSE_HEADERS = HOP_BY_HOP | {"content-length"}

# Paths that exist in exactly one dialect. Used to refuse, never to allow.
OLLAMA_PREFIX   = "/api/"
OPENAI_PATHS    = {"/v1/chat/completions", "/v1/completions", "/v1/embeddings"}
ANTHROPIC_PATHS = {"/v1/messages", "/v1/complete"}

DIALECT_LABELS = {"ollama": "Ollama", "openai": "OpenAI", "anthropic": "Anthropic"}


def check_caller_token(headers: Mapping[str, str], expected_token: str) -> bool:
    """Constant-time check of the caller's credential against the configured token.

    Both carriers are accepted because an Anthropic SDK pointed at the relay
    sends `x-api-key` and cannot easily be made to send a bearer; accepting only
    one would leave half the callers unable to authenticate at all.
    """
    authorization = headers.get("authorization", "")
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return hmac.compare_digest(parts[1].strip(), expected_token)
        return False
    api_key = headers.get("x-api-key", "")
    if api_key:
        return hmac.compare_digest(api_key.strip(), expected_token)
    return False


def select_upstream(path: str, config: RelayConfig) -> tuple[Upstream, str]:
    """Return the upstream for a request path and the path to forward.

    The leading segment selects an upstream only when it exactly names one;
    anything else falls through to the default with the path untouched. That is
    what lets an Ollama client that has never heard of lmrelay keep sending
    /api/chat to port 11434.
    """
    segments = path.lstrip("/").split("/", 1)
    if segments[0] in config.upstreams:
        remainder = "/" + segments[1] if len(segments) > 1 else "/"
        return config.upstreams[segments[0]], remainder
    return config.upstreams[config.default_upstream], path


def check_dialect(upstream: Upstream, path: str) -> str | None:
    """Return a refusal message for a path that certainly does not exist upstream.

    A denylist of known-wrong shapes rather than an allowlist of known-good
    ones: an allowlist would start refusing legitimate traffic the day a
    provider ships a new endpoint, making the relay something you have to keep
    updated.
    """
    if upstream.dialect == "ollama":
        # Ollama serves both its native /api/* and an OpenAI-compatible /v1/*,
        # so nothing is certainly wrong.
        return None
    if path.startswith(OLLAMA_PREFIX):
        return describe_dialect_mismatch(upstream, path, "ollama")
    if upstream.dialect == "openai" and path in ANTHROPIC_PATHS:
        return describe_dialect_mismatch(upstream, path, "anthropic")
    if upstream.dialect == "anthropic" and path in OPENAI_PATHS:
        return describe_dialect_mismatch(upstream, path, "openai")
    return None


def describe_dialect_mismatch(upstream: Upstream, path: str, path_dialect: str) -> str:
    """Phrase the refusal so the caller knows it came from lmrelay, not the provider."""
    # "an" is fixed rather than derived: all three dialect labels begin with a vowel.
    return (
        f"lmrelay: upstream '{upstream.name}' speaks the {DIALECT_LABELS[upstream.dialect]} API; "
        f"'{path}' is an {DIALECT_LABELS[path_dialect]}-dialect path. lmrelay forwards requests "
        f"unchanged and does not translate between dialects."
    )


def build_upstream_headers(headers: Mapping[str, str], upstream: Upstream) -> dict[str, str]:
    """Forward the caller's headers minus the dropped ones, with the upstream's on top."""
    configured = {name.lower() for name in upstream.headers}
    forwarded = {
        name: value
        for name, value in headers.items()
        if name.lower() not in DROPPED_REQUEST_HEADERS and name.lower() not in configured
    }
    return forwarded | upstream.headers


def filter_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return the upstream's response headers, minus the ones we must not relay."""
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in DROPPED_RESPONSE_HEADERS
    }


def build_upstream_url(upstream: Upstream, forward_path: str, query: str) -> str:
    """Concatenate base_url with the forwarded path; the query string rides along verbatim."""
    url = upstream.base_url + forward_path
    return f"{url}?{query}" if query else url


def has_request_body(headers: Mapping[str, str]) -> bool:
    """Report whether the caller sent a body at all.

    A GET with no body must not be re-framed as chunked — some upstreams reject
    that — so an empty request is forwarded with no body rather than an empty
    stream.
    """
    if headers.get("content-length"):
        return headers["content-length"] != "0"
    return "chunked" in headers.get("transfer-encoding", "").lower()


def build_upstream_request(
    client: httpx.AsyncClient,
    request: Request,
    upstream: Upstream,
    forward_path: str,
) -> httpx.Request:
    """Build the outbound request: same method, path, query and body bytes."""
    # Built directly rather than through client.build_request so httpx's default
    # Accept / Accept-Encoding / User-Agent do not join headers the caller never
    # sent — an Accept-Encoding we invented would come back as a compressed body
    # the caller never asked for. The client's timeout still has to be attached
    # by hand as a result.
    body = request.stream() if has_request_body(request.headers) else b""
    return httpx.Request(
        method=request.method,
        url=build_upstream_url(upstream, forward_path, request.url.query),
        headers=build_upstream_headers(request.headers, upstream),
        content=body,
        extensions={"timeout": client.timeout.as_dict()},
    )


def main():
    pass


if __name__ == "__main__":
    main()
