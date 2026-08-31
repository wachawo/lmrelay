#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The transfer bundle: one JSON file that reproduces this relay on another machine."""

import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Local imports
from lmrelay import __version__
from lmrelay.config import (
    DEFAULT_UPSTREAM,
    SERVER_KEYS,
    TOKEN_ENV_VAR,
    RelayConfig,
    is_log_level,
    parse_upstream,
)
from lmrelay.errors import BundleError
from lmrelay.ratelimit import LIMIT_KEYS, SCOPES
from lmrelay.state import MASKED_TOKEN, CallerToken, RelayState, utc_now, write_private

BUNDLE_VERSION = 1

# The path that means the terminal rather than a file, for both verbs.
STDIO_PATH = "-"

# A setting is a path, and the bundle spells it the third way: [limits.total]
# concurrent in the file is LMRELAY_LIMITS_TOTAL_CONCURRENT in the environment
# and limits.total.concurrent here. The key names are taken from the config and
# the limiter rather than restated, so a scope or a [server] key added there
# arrives here without an edit and cannot come to mean something else.
#
# The type is what an import checks a value against, and is the one thing this
# module has to say for itself: a config written with port = "eleven" parses as
# TOML and then refuses to start, which is a bundle that half applied. The two
# tables are held to the config's own key lists by a test, so a key added there
# and forgotten here fails at the suite rather than at an operator's import.
SERVER_TYPES: dict[str, type] = {
    "host":             str,
    "port":             int,
    "default_upstream": str,
    "connect_timeout":  int,
    "log_level":        str,
}
LIMIT_TYPES: dict[str, type] = {
    "rate":       float,
    "burst":      float,
    "concurrent": int,
}

# What a refused value is told it should have been, in the words the config's
# own reader uses: it says a port and a concurrency "must be a whole number",
# and a bundle that called both of them "a number" sent an operator looking for
# a different mistake than the one they had made.
TYPE_NAMES: dict[type, str] = {str: "a string", int: "a whole number", float: "a number"}

TOP_LEVEL_KEYS = (
    "bundle_version", "written_by", "exported_at", "server", "limits", "auth", "upstreams",
)
AUTH_KEYS      = ("enabled", "tokens")
TOKEN_KEYS     = ("id", "token", "label", "created_at")
UPSTREAM_KEYS  = ("base_url", "dialect", "headers")

# What a credential that reached the relay through the file or the environment
# is labelled once the bundle turns it into a stored token.
FILE_TOKEN_LABEL = "from [auth] token"
ENV_TOKEN_LABEL  = f"from ${TOKEN_ENV_VAR}"

CONFIG_HEADER = """\
# Written by 'lmrelay config import' from {source}.
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
# Three scopes, the same three keys in each, every one 0 (off). A request must
# pass every scope you set. If you set one number, set [limits.total]
# concurrent: that is the one that protects the upstream.
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
    # What --no-secrets left out, named so the import can say what is missing
    # instead of restoring a masked value and letting the upstream 401 about it.
    missing: tuple[str, ...]


def describe_source(path: str) -> str:
    """Name the place a bundle came from, for a message about it."""
    return "standard input" if path == STDIO_PATH else path


def token_source_label(token: str, environment_token: str | None) -> str:
    """Say where a credential that has no token record came from."""
    return ENV_TOKEN_LABEL if token == environment_token else FILE_TOKEN_LABEL


def bundle_tokens(
    config: RelayConfig, state: RelayState, keep_secrets: bool, environment_token: str | None
) -> list[dict]:
    """Every credential a caller may present, as records that keep their ids.

    The state's tokens keep the ids `token list` prints, which is what makes an
    imported relay the same relay. A credential that arrived through
    [auth] token or $LMRELAY_TOKEN has no record at all, and leaving it out
    would export a relay that refuses a caller the exported one served, so it
    is given the next free id and a label saying where it was.
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
            label=token_source_label(token, environment_token),
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

    Expanded, because a bundle reproduces the relay that was exported rather
    than the machine it ran on: ${OPENAI_API_KEY} means nothing on a host that
    does not export it, and importing an unexpanded one would produce a relay
    that refuses to start for a reason belonging to somewhere else.
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


def build_bundle(
    config: RelayConfig,
    state: RelayState,
    keep_secrets: bool = True,
    environment_token: str | None = None,
) -> dict:
    """Assemble the bundle for this relay, from the effective configuration."""
    return {
        "bundle_version": BUNDLE_VERSION,
        # Two version fields doing two jobs: bundle_version is load bearing,
        # written_by is for the person reading a bundle six months later.
        "written_by": f"lmrelay {__version__}",
        "exported_at": utc_now(),
        "server": {name: getattr(config, name) for name in SERVER_KEYS},
        "limits": {
            scope: {key: getattr(config.limits[scope], key) for key in LIMIT_KEYS}
            for scope in SCOPES
        },
        "auth": {
            "enabled": config.auth_enabled,
            "tokens": bundle_tokens(config, state, keep_secrets, environment_token),
        },
        "upstreams": bundle_upstreams(config, keep_secrets),
    }


def count_secrets(bundle: dict) -> tuple[int, int]:
    """How many caller tokens and provider credentials the bundle carries."""
    tokens = len(bundle["auth"]["tokens"])
    keyed = sum(1 for upstream in bundle["upstreams"].values() if upstream["headers"])
    return tokens, keyed


def write_bundle(bundle: dict, destination: str) -> None:
    """Write the bundle to a file at 0600, or to stdout when asked for by name."""
    text = json.dumps(bundle, indent=2) + "\n"
    if destination == STDIO_PATH:
        sys.stdout.write(text)
        return
    # Through the same writer state.json uses: the bundle holds every caller
    # token and every provider key, so it must be 0600 from creation rather
    # than from a chmod after the write.
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
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise BundleError(f"lmrelay: {describe_source(source)} is not JSON: {exc}")
    if not isinstance(data, dict):
        raise BundleError(f"lmrelay: {describe_source(source)} is not a JSON object")
    return data


def check_keys(present, allowed, what: str, source: str) -> None:
    """Refuse a key this bundle version does not carry, and name it.

    Ignoring it would be forward compatibility bought at the price of importing
    a relay that looks configured and is not. At one version both ends agree on
    the key set, so an unknown key is a hand edit or a wrong bundle_version, and
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
            f"'lmrelay config export' writes one."
        )
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise BundleError(
            f"lmrelay: {source} has bundle_version {version!r}, which is not a version number"
        )
    if version > BUNDLE_VERSION:
        # Refused rather than partially read: a newer bundle may carry a setting
        # this version does not enforce, and importing it would produce a relay
        # that looks configured and is not.
        raise BundleError(
            f"lmrelay: {source} has bundle version {version}; this lmrelay understands "
            f"{BUNDLE_VERSION}. It was written by a newer lmrelay: upgrade, or export again "
            f"from that machine with a matching version."
        )
    return version


def check_types(table: dict, types: dict[str, type], where: str, source: str) -> None:
    """Refuse a value whose type the config parser would reject at the next start."""
    for name, value in table.items():
        wanted = types[name]
        accepted = (int, float) if wanted is float else (wanted,)
        # bool is a subclass of int, and `port = true` is not a port. nan and
        # inf are floats, and json.loads reads both, and neither is a limit: the
        # config refuses one, so a bundle carrying one writes a file that parses
        # as TOML and then refuses to start, which is an import that half
        # applied.
        unusable = isinstance(value, float) and not math.isfinite(value)
        if isinstance(value, bool) or unusable or not isinstance(value, accepted):
            raise BundleError(
                f"lmrelay: {source} has {where} {name} = {value!r}, "
                f"which is not {TYPE_NAMES[wanted]}"
            )


def check_server_values(server: dict, source: str) -> None:
    """Refuse a [server] value that is the right type and still unusable.

    The type table cannot reach this one: log_level is a string, and so is
    "VERBOSE", and the pair of files an import writes with it in them is a relay
    that refuses to start on every command afterwards, on a machine whose
    working config the import has already moved aside. It is the one [server]
    key the config has a value validator for, so this is the whole of the list.
    The predicate is the config's own, so there is one definition of a level.
    """
    level = server.get("log_level")
    if level is not None and not is_log_level(str(level)):
        raise BundleError(
            f"lmrelay: {source} has server log_level = {level!r}, which is not a logging "
            f"level; expected DEBUG, INFO, WARNING, ERROR or CRITICAL"
        )


def parse_server(data: dict, source: str) -> dict:
    """Validate the [server] half, refusing a value the config could not load."""
    server = data.get("server") or {}
    if not isinstance(server, dict):
        raise BundleError(f"lmrelay: {source} has a server section that is not an object")
    check_keys(server, SERVER_KEYS, "server key(s)", source)
    check_types(server, SERVER_TYPES, "server", source)
    check_server_values(server, source)
    return server


def parse_limits(data: dict, source: str) -> dict:
    """Validate the [limits.*] half: the three scopes, and the same three keys in each."""
    limits = data.get("limits") or {}
    if not isinstance(limits, dict):
        raise BundleError(f"lmrelay: {source} has a limits section that is not an object")
    check_keys(limits, SCOPES, "limit scope(s)", source)
    for scope, table in limits.items():
        if not isinstance(table, dict):
            raise BundleError(f"lmrelay: {source} has a limits {scope} that is not an object")
        check_keys(table, LIMIT_KEYS, f"key(s) in limits {scope}", source)
        check_types(table, LIMIT_TYPES, f"limits {scope}", source)
        for name, value in table.items():
            # 0 is off, so a negative is a mistake rather than another spelling
            # of it, and config.py refuses one for the same reason.
            if value < 0:
                raise BundleError(
                    f"lmrelay: {source} has limits {scope} {name} = {value!r}, "
                    f"and a limit cannot be negative"
                )
    return limits


def parse_bundle_token(entry: object, next_id: int, source: str) -> tuple[CallerToken, bool]:
    """Read one token record. Returns the record and whether its value was masked.

    id, label and created_at may be left out, because a bundle written by hand
    to provision a machine is a legitimate way to use this format and an id is
    ours to mint. The token itself may not: a record without one names nothing.
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
            # An id printed by `token list` must never come to name a second token.
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
    """Validate every upstream through the parser a hand-written table goes through."""
    section = data.get("upstreams") or {}
    if not isinstance(section, dict):
        raise BundleError(f"lmrelay: {source} has an upstreams section that is not an object")

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
                # Dropped rather than carried: a header holding *** is sent to
                # the provider, refused, and reads as a wrong key rather than as
                # a bundle that was exported without its secrets.
                missing.append(f"upstream {name} header {key}")
                continue
            headers[key] = value
        # expand_env=False for the same reason state_upstreams passes it: the
        # value in a bundle is already the finished one, and running a provider
        # key containing a $ through Template would rewrite it with the value of
        # an environment variable and send that to the provider.
        upstream = parse_upstream(
            name,
            {"base_url": table.get("base_url"), "dialect": table.get("dialect", "openai"),
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
    server = parse_server(data, source)
    limits = parse_limits(data, source)
    auth_enabled, tokens, missing_tokens = parse_auth(data, source)
    providers, missing_headers = parse_bundle_upstreams(data, source)

    if not providers:
        raise BundleError(
            f"lmrelay: {source} defines no upstreams, and a relay with none answers "
            f"every request with a 404"
        )
    # Checked against the default too, and not only against what the bundle
    # spells out: a hand-written bundle that leaves default_upstream out is a
    # documented way to provision a machine, and one carrying a single upstream
    # under any other name defaults to 'ollama' and cannot be loaded. Refused
    # here rather than at the next start, so the import does not write a pair of
    # files the relay refuses.
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

    JSON's string escaping is a subset of TOML's basic string escaping, so
    json.dumps is the encoder rather than a hand-rolled one that would be the
    thing to get a backslash wrong in.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value)


def render_table(title: str, table: dict, order) -> list[str]:
    """One TOML table, its values aligned on the widest key it carries."""
    names = [name for name in order if name in table]
    if not names:
        return []
    width = max(len(name) for name in names)
    return [f"[{title}]", *(f"{name.ljust(width)} = {toml_value(table[name])}" for name in names)]


def render_config(bundle: Bundle, source: str, state_path: Path) -> str:
    """Render the settings half as the lmrelay.toml an import writes.

    Every scope is written even when it is off, because this file is the
    operator's new config and a limit they cannot see in it is one they will not
    know they have. The upstreams and the tokens are not here: they go to the
    state, which shadows the file, so there is no second place for either.
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
