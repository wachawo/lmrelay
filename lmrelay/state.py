#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI-owned mutable state: caller tokens, the auth switch and provider keys."""

import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

# Local imports
from lmrelay.errors import StateError

# State
STATE_NAME     = "state.json"
STATE_ENV_VAR  = "LMRELAY_STATE"
TOKEN_PREFIX   = "lmr_"
TOKEN_BYTES    = 32          # secrets.token_urlsafe(32) -> 43 chars
STATE_VERSION  = 1

DIALECTS       = ("ollama", "openai", "anthropic")

# An upstream under either name would swallow the path root that every Ollama
# (/api/...) and OpenAI (/v1/...) client already sends to, and the breakage
# would surface as an unexplained 404 far from its cause.
RESERVED_UPSTREAM_NAMES = ("api", "v1")

# Display
MASK_MIN_LENGTH = 16
MASKED_TOKEN    = "***"

# Fallbacks for a provider added under a name no preset knows.
DEFAULT_PROVIDER_DIALECT = "openai"
DEFAULT_PROVIDER_HEADERS = {"Authorization": "Bearer {token}"}

# base_url, dialect and headers for the providers worth knowing by name, so
# `lmrelay provider add openai sk-...` is four words instead of a TOML table.
# {token} is substituted literally, not through string.Template: an API key
# containing a $ must not be treated as a variable reference.
PROVIDER_PRESETS = {
    "openai":    {"base_url": "https://api.openai.com",    "dialect": "openai",
                  "headers": {"Authorization": "Bearer {token}"}},
    "anthropic": {"base_url": "https://api.anthropic.com", "dialect": "anthropic",
                  "headers": {"x-api-key": "{token}", "anthropic-version": "2023-06-01"}},
    "deepseek":  {"base_url": "https://api.deepseek.com",  "dialect": "openai",
                  "headers": {"Authorization": "Bearer {token}"}},
    "grok":      {"base_url": "https://api.x.ai",          "dialect": "openai",
                  "headers": {"Authorization": "Bearer {token}"}},
    "ollama":    {"base_url": "http://127.0.0.1:11434",    "dialect": "ollama",
                  "headers": {}},
}


@dataclass(frozen=True)
class CallerToken:
    """One credential the relay accepts, as `lmrelay token list` shows it."""

    id: int
    token: str
    label: str
    created_at: str


@dataclass(frozen=True)
class RelayState:
    """The whole of state.json, validated."""

    auth_enabled: bool
    tokens: tuple[CallerToken, ...]
    providers: dict[str, dict]
    next_token_id: int
    state_path: Path


def state_path_for(config_path: Path) -> Path:
    """Locate state.json: $LMRELAY_STATE if set, else beside the config."""
    from_env = os.getenv(STATE_ENV_VAR)
    if from_env:
        return Path(from_env).expanduser()
    return config_path.parent / STATE_NAME


def empty_state(path: Path) -> RelayState:
    """The state of a fresh install.

    Auth is off: a relay on loopback with no tokens yet is a transparent proxy,
    and starting closed would lock the operator out of their own Ollama before
    they had a credential to offer it.
    """
    return RelayState(
        auth_enabled=False,
        tokens=(),
        providers={},
        next_token_id=1,
        state_path=path,
    )


def parse_token(entry: object, path: Path) -> CallerToken:
    """Read one token record, refusing anything that would silently drop a credential."""
    if (
        not isinstance(entry, dict)
        or not isinstance(entry.get("id"), int)
        or not isinstance(entry.get("token"), str)
    ):
        raise StateError(f"lmrelay: {path} has a malformed token entry")
    return CallerToken(
        id=entry["id"],
        token=entry["token"],
        label=str(entry.get("label", "")),
        created_at=str(entry.get("created_at", "")),
    )


def load_state(path: Path) -> RelayState:
    """Read state.json. A missing file is the empty default, not an error."""
    if not path.exists():
        return empty_state(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError(f"lmrelay: cannot read {path}: {type(exc).__name__}: {exc}")
    if not isinstance(data, dict):
        raise StateError(f"lmrelay: {path} is not a JSON object")

    version = data.get("version", STATE_VERSION)
    if not isinstance(version, int) or version > STATE_VERSION:
        raise StateError(
            f"lmrelay: {path} has state version {version}; this lmrelay understands "
            f"{STATE_VERSION}. It was written by a newer lmrelay: upgrade or move the file."
        )

    providers = data.get("providers") or {}
    if not isinstance(providers, dict) or not all(
        isinstance(entry, dict) for entry in providers.values()
    ):
        # The entries are checked here and not only where they are read, so a
        # hand-edited file fails as the StateError naming it that every command
        # is written to report, rather than as an AttributeError from the parser.
        raise StateError(f"lmrelay: {path} has a malformed providers table")

    tokens = tuple(parse_token(entry, path) for entry in data.get("tokens") or ())
    highest_id = max((token.id for token in tokens), default=0)
    next_token_id = data.get("next_token_id")
    # Recomputed rather than trusted when it would collide: an id printed by
    # `token list` must never come to name a second token.
    if not isinstance(next_token_id, int) or next_token_id <= highest_id:
        next_token_id = highest_id + 1

    return RelayState(
        auth_enabled=bool(data.get("auth_enabled", False)),
        tokens=tokens,
        providers=providers,
        next_token_id=next_token_id,
        state_path=path,
    )


def save_state(state: RelayState) -> None:
    """Write state.json atomically, so a crash cannot leave a truncated token list."""
    path = state.state_path
    payload = {
        "version": STATE_VERSION,
        "auth_enabled": state.auth_enabled,
        "next_token_id": state.next_token_id,
        "tokens": [asdict(token) for token in state.tokens],
        "providers": state.providers,
    }
    temp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp rather than write_text plus chmod, for two reasons. It creates
        # the file at 0600, so the tokens are never on disk world-readable. A
        # chmod after the write leaves them so for as long as the two calls are
        # apart, and forever if the process dies in between. And it names the
        # file uniquely, so two concurrent saves cannot write into one another's
        # temp file and rename a payload neither of them reported.
        handle, name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
        temp_path = Path(name)
        with os.fdopen(handle, "w", encoding="utf-8") as target:
            target.write(json.dumps(payload, indent=2) + "\n")
        # os.replace carries the 0600 over.
        os.replace(temp_path, path)
    except OSError as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise StateError(f"lmrelay: cannot write {path}: {type(exc).__name__}: {exc}")


def generate_token() -> str:
    """Mint a caller token. The prefix makes a leaked one recognisable in a log."""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def mask_token(token: str) -> str:
    """Shorten a token for display, keeping enough of it to be recognised."""
    if len(token) < MASK_MIN_LENGTH:
        # Ellipsising a short token would print most of it.
        return MASKED_TOKEN
    return f"{token[:8]}…{token[-4:]}"


def utc_now() -> str:
    """ISO-8601 UTC to the second: state.json is read by people as well as by code."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_token(state: RelayState, token: str, label: str = "") -> tuple[RelayState, CallerToken]:
    """Record a caller token. Returns the new state and the record; the caller saves."""
    value = token.strip()
    if not value:
        raise StateError("lmrelay: token is empty")
    if any(existing.token == value for existing in state.tokens):
        raise StateError("lmrelay: that token is already configured")
    record = CallerToken(id=state.next_token_id, token=value, label=label.strip(),
                         created_at=utc_now())
    new_state = replace(
        state,
        tokens=(*state.tokens, record),
        next_token_id=state.next_token_id + 1,
    )
    return new_state, record


def delete_token(state: RelayState, token_id: int) -> RelayState:
    """Drop a token by the id `token list` prints."""
    remaining = tuple(token for token in state.tokens if token.id != token_id)
    if len(remaining) == len(state.tokens):
        known = ", ".join(str(token.id) for token in state.tokens) or "none"
        raise StateError(f"lmrelay: no token with id {token_id}; known ids: {known}")
    return replace(state, tokens=remaining)


def set_auth_enabled(state: RelayState, enabled: bool) -> RelayState:
    """Flip the auth switch. Nothing else decides whether callers need a credential."""
    return replace(state, auth_enabled=enabled)


def substitute_token(value: str, token: str) -> str:
    """Fill {token} in a preset header value."""
    return value.replace("{token}", token)


def merge_headers(preset: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    """Lay extra headers over the preset ones, matching names case-insensitively.

    A plain dict update would keep both `Authorization` and `authorization`, and
    the request would carry two of them, and an operator who typed the lowercase
    one to replace a preset's key would still be shipping the old key, and which
    one the provider honours would be the provider's choice.
    """
    merged = dict(preset)
    for name, value in extra.items():
        for existing in list(merged):
            if existing.lower() == name.lower():
                del merged[existing]
        merged[name] = value
    return merged


def add_provider(
    state: RelayState,
    name: str,
    token: str,
    base_url: str | None = None,
    dialect: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> RelayState:
    """Add a provider upstream, overwriting one of the same name.

    Overwriting rather than refusing because rotating a key is the common case,
    and a refusal would make it a delete-then-add dance.
    """
    if name in RESERVED_UPSTREAM_NAMES:
        raise StateError(
            f"lmrelay: provider name '{name}' is reserved: it would shadow the "
            f"Ollama/OpenAI path root"
        )
    preset = PROVIDER_PRESETS.get(name, {})
    if base_url is None and not preset:
        raise StateError(
            f"lmrelay: '{name}' is not a known provider; pass --base-url to add it. "
            f"Known providers: {', '.join(sorted(PROVIDER_PRESETS))}"
        )

    resolved_url = base_url if base_url is not None else str(preset["base_url"])
    if not resolved_url.startswith(("http://", "https://")):
        raise StateError(
            f"lmrelay: provider '{name}' needs a base_url starting with http:// or https://"
        )
    resolved_dialect = dialect or preset.get("dialect", DEFAULT_PROVIDER_DIALECT)
    if resolved_dialect not in DIALECTS:
        raise StateError(
            f"lmrelay: provider '{name}' has dialect '{resolved_dialect}'; "
            f"expected one of {', '.join(DIALECTS)}"
        )

    # A preset with no headers (Ollama) takes no credential; a provider no
    # preset knows still has to carry the token it was given, and a bearer is
    # what an unlisted OpenAI-compatible endpoint expects.
    template = preset.get("headers", DEFAULT_PROVIDER_HEADERS)
    headers = merge_headers(
        {key: substitute_token(value, token) for key, value in template.items()},
        extra_headers or {},
    )

    providers = dict(state.providers)
    providers[name] = {
        "base_url": resolved_url.rstrip("/"),
        "dialect": resolved_dialect,
        "headers": headers,
    }
    return replace(state, providers=providers)


def delete_provider(state: RelayState, name: str) -> RelayState:
    """Remove a CLI-added provider."""
    if name not in state.providers:
        known = ", ".join(sorted(state.providers)) or "none"
        raise StateError(f"lmrelay: no provider '{name}' was added by the CLI; known: {known}")
    providers = {key: value for key, value in state.providers.items() if key != name}
    return replace(state, providers=providers)


def main():
    pass


if __name__ == "__main__":
    main()
