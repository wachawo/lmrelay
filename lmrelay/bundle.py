#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The transfer bundle: one TOML file that reproduces this relay on another machine."""

import json
import re
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

# Local imports
from lmrelay import __version__
from lmrelay.config import (
    DEFAULT_DIALECT,
    DEFAULT_UPSTREAM,
    SERVER_KEYS,
    RelayConfig,
    parse_limits,
    parse_server,
    parse_upstream,
)
from lmrelay.errors import BundleError, ConfigError
from lmrelay.ratelimit import LIMIT_KEYS, SCOPES
from lmrelay.state import MASKED_TOKEN, CallerToken, RelayState, utc_now, write_private

BUNDLE_VERSION = 1

# What TOML accepts as a key without quotes.
BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")

# Stdin for import, stdout for export.
STDIO_PATH = "-"

# Written before the first table: TOML assigns a bare key to the last table seen.
META_KEYS = ("bundle_version", "written_by", "exported_at")
TOP_LEVEL_KEYS = (*META_KEYS, "server", "limits", "auth", "upstream")
AUTH_KEYS      = ("enabled", "tokens")
TOKEN_KEYS     = ("id", "token", "label", "created_at")
UPSTREAM_KEYS  = ("base_url", "dialect", "headers")

# Label for an `[auth] token` credential exported as a stored token.
FILE_TOKEN_LABEL = "from [auth] token"

CONFIG_HEADER = """\
# Written by 'lmrelay import' from {source}.
#
# A bundle carries settings, not comments: the notes in the lmrelay.toml this
# came from belong to the operator who wrote them and are not part of the
# transfer. Add your own here; nothing rewrites this file except another
# import, which backs it up first.
#
# The upstreams, the caller tokens and the auth switch are in {state} beside
# this file, where the CLI keeps them.
"""

LIMITS_HEADER = """\
# Three scopes, two numbers each, off at 0 and "". 'concurrent' is how many a
# caller may have in flight and 'rate' is how often it may start one, written
# count/period. A request must pass every scope you set. If you set one, set
# [limits.total]: that is the one that protects the upstream.
"""

BUNDLE_HEADER = """\
# lmrelay bundle, written by {written_by} at {exported_at}.
#
# This is an lmrelay.toml with the machine-owned half beside it. [auth] and
# [upstream.*] headers live in state.json on a running relay; they are here so
# that one file reproduces one relay. Read it, edit it, hand it to
# 'lmrelay import'.
#
# It carries settings, not comments: the notes in the lmrelay.toml this came
# from belong to the operator who wrote them and are not part of the transfer.
"""


@dataclass(frozen=True)
class Bundle:
    """A validated bundle, ready to become an lmrelay.toml and a state.json."""

    version: int
    written_by: str
    exported_at: str
    server: dict
    limits: dict
    auth_enabled: bool
    tokens: tuple[CallerToken, ...]
    providers: dict[str, dict]
    # Secrets masked by --no-secrets, so the import can name what is missing.
    missing: tuple[str, ...]


def describe_source(path: str) -> str:
    """Name the place a bundle came from, for a message about it."""
    return "standard input" if path == STDIO_PATH else path


def bundle_tokens(config: RelayConfig, state: RelayState, keep_secrets: bool) -> list[dict]:
    """Every credential a caller may present, as token records with stable ids.

    An `[auth] token` from the file has no record, so it gets the next free id
    and FILE_TOKEN_LABEL rather than being dropped.
    """
    records = list(state.tokens)
    known = {record.token for record in records}
    next_id = state.next_token_id
    for token in config.auth_tokens:
        if token in known:
            continue
        records.append(CallerToken(
            id=next_id,
            token=token,
            label=FILE_TOKEN_LABEL,
            created_at=utc_now(),
        ))
        known.add(token)
        next_id += 1
    return [
        {**asdict(record), "token": record.token if keep_secrets else MASKED_TOKEN}
        for record in records
    ]


def bundle_upstreams(config: RelayConfig, keep_secrets: bool) -> dict:
    """Every upstream in effect, hand-written and CLI-added alike, headers expanded.

    Expanded because `${VAR}` may not be set on the importing host.
    """
    return {
        name: {
            "base_url": upstream.base_url,
            "dialect": upstream.dialect,
            "headers": {
                key: value if keep_secrets else MASKED_TOKEN
                for key, value in upstream.headers.items()
            },
        }
        for name, upstream in sorted(config.upstreams.items())
    }


def build_bundle(config: RelayConfig, state: RelayState, keep_secrets: bool = True) -> dict:
    """Assemble the bundle for this relay, from the configuration in effect."""
    return {
        "bundle_version": BUNDLE_VERSION,
        # Informational only; bundle_version is the one that is checked.
        "written_by": f"lmrelay {__version__}",
        "exported_at": utc_now(),
        "server": {name: getattr(config, name) for name in SERVER_KEYS},
        "limits": {
            scope: {key: getattr(config.limits[scope], key) for key in LIMIT_KEYS}
            for scope in SCOPES
        },
        "auth": {
            "enabled": config.auth_enabled,
            "tokens": bundle_tokens(config, state, keep_secrets),
        },
        "upstream": bundle_upstreams(config, keep_secrets),
    }


def count_secrets(bundle: dict) -> tuple[int, int]:
    """Count the caller tokens and the upstreams with headers in the bundle."""
    tokens = len(bundle["auth"]["tokens"])
    keyed = sum(1 for upstream in bundle["upstream"].values() if upstream["headers"])
    return tokens, keyed


def write_bundle(bundle: dict, destination: str) -> None:
    """Write the bundle to a file at 0600, or to stdout when no path was given."""
    text = render_bundle(bundle)
    if destination == STDIO_PATH:
        sys.stdout.write(text)
        return
    # The bundle holds every token and provider key: 0600 from creation.
    write_private(Path(destination).expanduser(), text)


def read_bundle(source: str) -> dict:
    """Read a bundle from a file or from stdin, refusing anything that is not one."""
    try:
        if source == STDIO_PATH:
            text = sys.stdin.read()
        else:
            text = Path(source).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise BundleError(
            f"lmrelay: cannot read {describe_source(source)}: {type(exc).__name__}: {exc}"
        )
    if text.lstrip().startswith("{"):
        # Named explicitly: a TOML syntax error at line 1 reads as corruption.
        raise BundleError(
            f"lmrelay: {describe_source(source)} is JSON, and a bundle is TOML. It was "
            f"written by a build from before the format changed: export again from that "
            f"machine with a matching lmrelay."
        )
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise BundleError(f"lmrelay: {describe_source(source)} is not TOML: {exc}")


def check_keys(present, allowed, what: str, source: str) -> None:
    """Refuse a key this bundle version does not carry, and name it.

    Ignoring it could import a relay that looks configured and is not;
    forward compatibility is what bundle_version is for.
    """
    unknown = sorted(set(present) - set(allowed))
    if not unknown:
        return
    raise BundleError(
        f"lmrelay: {source} has {what} this lmrelay does not know: {', '.join(unknown)}. "
        f"At bundle version {BUNDLE_VERSION} both ends agree on the keys, so this is a "
        f"hand edit or a bundle_version that is not the one it was written at."
    )


def check_version(data: dict, source: str) -> int:
    """Refuse a bundle from a newer lmrelay rather than reading half of it."""
    version = data.get("bundle_version")
    if version is None:
        raise BundleError(
            f"lmrelay: {source} has no bundle_version, so it is not an lmrelay export. "
            f"'lmrelay export' writes one."
        )
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise BundleError(
            f"lmrelay: {source} has bundle_version {version!r}, which is not a version number"
        )
    if version > BUNDLE_VERSION:
        # A newer bundle may carry settings this version would not enforce.
        raise BundleError(
            f"lmrelay: {source} has bundle version {version}; this lmrelay understands "
            f"{BUNDLE_VERSION}. It was written by a newer lmrelay: upgrade, or export again "
            f"from that machine with a matching version."
        )
    return version


def check_settings(data: dict, source: str) -> tuple[dict, dict]:
    """Validate [server] and [limits.*] with the config's own parsers.

    The same parsers read the written config at the next start, so an import
    never writes a file the relay refuses. Returns the tables as written.
    """
    server = data.get("server") or {}
    limits = data.get("limits") or {}
    try:
        parse_server({"server": server})
        parse_limits({"limits": limits})
    except ConfigError as exc:
        reason = str(exc).removeprefix("lmrelay: ")
        raise BundleError(f"lmrelay: {source} would not load as a config: {reason}")
    return server, limits


def parse_bundle_token(entry: object, next_id: int, source: str) -> tuple[CallerToken, bool]:
    """Read one token record. Returns the record and whether its value was masked.

    Only `token` is required, so a bundle can be written by hand.
    """
    if not isinstance(entry, dict):
        raise BundleError(f"lmrelay: {source} has a token entry that is not an object")
    check_keys(entry, TOKEN_KEYS, "token key(s)", source)
    value = entry.get("token")
    if not isinstance(value, str) or not value.strip():
        raise BundleError(f"lmrelay: {source} has a token entry with no token in it")
    token_id = entry.get("id", next_id)
    if not isinstance(token_id, int) or isinstance(token_id, bool):
        raise BundleError(f"lmrelay: {source} has a token with id {token_id!r}, which is not an id")
    masked = value == MASKED_TOKEN
    return CallerToken(
        id=token_id,
        token=value.strip(),
        label=str(entry.get("label", "")),
        created_at=str(entry.get("created_at", "")) or utc_now(),
    ), masked


def parse_auth(data: dict, source: str) -> tuple[bool, tuple[CallerToken, ...], list[str]]:
    """Validate the auth half: the switch, the tokens, and what was masked out."""
    auth = data.get("auth") or {}
    if not isinstance(auth, dict):
        raise BundleError(f"lmrelay: {source} has an auth section that is not an object")
    check_keys(auth, AUTH_KEYS, "auth key(s)", source)
    enabled = auth.get("enabled", False)
    if not isinstance(enabled, bool):
        raise BundleError(
            f"lmrelay: {source} has auth enabled = {enabled!r}, which is not true or false"
        )
    entries = auth.get("tokens") or []
    if not isinstance(entries, list):
        raise BundleError(f"lmrelay: {source} has an auth tokens list that is not a list")

    tokens: list[CallerToken] = []
    missing: list[str] = []
    used_ids: set[int] = set()
    for entry in entries:
        record, masked = parse_bundle_token(entry, max(used_ids, default=0) + 1, source)
        if record.id in used_ids:
            # An id must name exactly one token.
            raise BundleError(f"lmrelay: {source} has two tokens with id {record.id}")
        used_ids.add(record.id)
        if masked:
            label = f" ({record.label})" if record.label else ""
            missing.append(f"caller token {record.id}{label}")
            continue
        if any(existing.token == record.token for existing in tokens):
            raise BundleError(f"lmrelay: {source} has the same token twice")
        tokens.append(record)
    return enabled, tuple(tokens), missing


def parse_bundle_upstreams(data: dict, source: str) -> tuple[dict[str, dict], list[str]]:
    """Validate every upstream with parse_upstream, dropping masked headers."""
    section = data.get("upstream") or {}
    if not isinstance(section, dict):
        raise BundleError(f"lmrelay: {source} has an upstream section that is not a table")

    providers: dict[str, dict] = {}
    missing: list[str] = []
    for name, table in sorted(section.items()):
        if not isinstance(table, dict):
            raise BundleError(f"lmrelay: {source} has an upstream '{name}' that is not an object")
        check_keys(table, UPSTREAM_KEYS, f"key(s) in upstream '{name}'", source)
        raw_headers = table.get("headers") or {}
        if not isinstance(raw_headers, dict):
            raise BundleError(f"lmrelay: {source} has upstream '{name}' headers that are not an object")
        headers = {}
        for key, value in raw_headers.items():
            if value == MASKED_TOKEN:
                # Dropped and reported: sent as-is it would look like a wrong key.
                missing.append(f"upstream {name} header {key}")
                continue
            headers[key] = value
        # Values are already expanded; see parse_upstream for why not again.
        upstream = parse_upstream(
            name,
            {"base_url": table.get("base_url"), "dialect": table.get("dialect", DEFAULT_DIALECT),
             "headers": headers},
            expand_env=False,
        )
        providers[name] = {
            "base_url": upstream.base_url,
            "dialect": upstream.dialect,
            "headers": upstream.headers,
        }
    return providers, missing


def parse_bundle(data: dict, source: str) -> Bundle:
    """Validate the whole bundle. Nothing is written until this has returned."""
    version = check_version(data, source)
    check_keys(data, TOP_LEVEL_KEYS, "top-level key(s)", source)
    server, limits = check_settings(data, source)
    auth_enabled, tokens, missing_tokens = parse_auth(data, source)
    providers, missing_headers = parse_bundle_upstreams(data, source)

    if not providers:
        raise BundleError(
            f"lmrelay: {source} defines no upstreams, and a relay with none answers "
            f"every request with a 404"
        )
    # An omitted default_upstream falls back to 'ollama', which must exist too.
    # Checked here so the import does not write files the relay refuses.
    named = server.get("default_upstream")
    default_upstream = DEFAULT_UPSTREAM if named is None else named
    if default_upstream not in providers:
        known = ", ".join(sorted(providers))
        if named is None:
            raise BundleError(
                f"lmrelay: {source} sets no default_upstream, so it would fall back to "
                f"'{DEFAULT_UPSTREAM}', which it does not define; it has: {known}. "
                f"Name one of them as default_upstream."
            )
        raise BundleError(
            f"lmrelay: {source} names default_upstream '{default_upstream}', which it does "
            f"not define; it has: {known}"
        )

    return Bundle(
        version=version,
        written_by=str(data.get("written_by", "an unknown lmrelay")),
        exported_at=str(data.get("exported_at", "an unrecorded time")),
        server=server,
        limits=limits,
        auth_enabled=auth_enabled,
        tokens=tokens,
        providers=providers,
        missing=tuple(missing_tokens + missing_headers),
    )


def toml_value(value) -> str:
    """Render one scalar as TOML.

    JSON string escaping is a subset of TOML basic-string escaping, so
    json.dumps encodes strings.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value)


def render_assignments(table: dict, order) -> list[str]:
    """Render the `key = value` lines of one table, aligned on the widest key."""
    names = [name for name in order if name in table]
    if not names:
        return []
    width = max(len(name) for name in names)
    return [f"{name.ljust(width)} = {toml_value(table[name])}" for name in names]


def render_table(title: str, table: dict, order) -> list[str]:
    """Render one TOML table, or nothing if it has no keys."""
    lines = render_assignments(table, order)
    return [f"[{title}]", *lines] if lines else []


def render_inline_table(table: dict) -> str:
    """Render an upstream's headers as a one-line TOML table, every key quoted."""
    pairs = ", ".join(f"{json.dumps(key)} = {toml_value(value)}" for key, value in table.items())
    return f"{{ {pairs} }}" if pairs else "{}"


def toml_key(name: str) -> str:
    """Render a table key bare when TOML allows it, quoted otherwise.

    An upstream name is a TOML key the operator chose, and `my.relay` written
    bare would be read back as a table `my` holding a table `relay`.
    """
    return name if BARE_KEY.fullmatch(name) else json.dumps(name)


def render_upstream(name: str, upstream: dict) -> list[str]:
    """Render one [upstream.<name>] table as lmrelay.toml spells it."""
    return [
        f"[upstream.{toml_key(name)}]",
        f"base_url = {toml_value(upstream['base_url'])}",
        f"dialect  = {toml_value(upstream['dialect'])}",
        f"headers  = {render_inline_table(upstream.get('headers') or {})}",
    ]


def render_bundle(bundle: dict) -> str:
    """Render the bundle as TOML: an lmrelay.toml plus the state.json half.

    Every scope is written even when off, and so is an empty `headers`, so the
    file shows the whole exported configuration without relying on defaults.
    """
    blocks = [
        BUNDLE_HEADER.format(
            written_by=bundle["written_by"], exported_at=bundle["exported_at"]
        ).rstrip("\n"),
        "\n".join(render_assignments(bundle, META_KEYS)),
        "\n".join(render_table("server", bundle["server"], SERVER_KEYS)),
        LIMITS_HEADER.rstrip("\n"),
    ]
    for scope in SCOPES:
        blocks.append("\n".join(render_table(f"limits.{scope}", bundle["limits"][scope], LIMIT_KEYS)))

    auth = bundle["auth"]
    blocks.append(f"[auth]\nenabled = {toml_value(auth['enabled'])}")
    for token in auth["tokens"]:
        # An array of tables keeps ids and labels readable and editable.
        blocks.append("\n".join(["[[auth.tokens]]", *render_assignments(token, TOKEN_KEYS)]))

    for name in sorted(bundle["upstream"]):
        blocks.append("\n".join(render_upstream(name, bundle["upstream"][name])))
    return "\n\n".join(block for block in blocks if block) + "\n"


def render_config(bundle: Bundle, source: str, state_path: Path) -> str:
    """Render the settings half as the lmrelay.toml an import writes.

    Only scopes the bundle sets are written. Upstreams and tokens go to
    state.json instead, so each lives in one place.
    """
    blocks = [CONFIG_HEADER.format(source=source, state=state_path).rstrip("\n")]
    blocks.append("\n".join(render_table("server", bundle.server, SERVER_KEYS)))
    if bundle.limits:
        blocks.append(LIMITS_HEADER.rstrip("\n"))
        for scope in SCOPES:
            table = bundle.limits.get(scope)
            if table:
                blocks.append("\n".join(render_table(f"limits.{scope}", table, LIMIT_KEYS)))
    return "\n\n".join(block for block in blocks if block) + "\n"


def bundle_state(bundle: Bundle, state_path: Path) -> RelayState:
    """Turn a validated bundle into the state.json half."""
    return RelayState(
        auth_enabled=bundle.auth_enabled,
        tokens=bundle.tokens,
        providers=bundle.providers,
        next_token_id=max((token.id for token in bundle.tokens), default=0) + 1,
        state_path=state_path,
    )


def main():
    pass


if __name__ == "__main__":
    main()
