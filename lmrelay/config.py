#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discovery, parsing and validation of lmrelay.toml, merged with the state."""

import ipaddress
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

# Local imports
from lmrelay.errors import ConfigError
from lmrelay.ratelimit import (
    LIMIT_KEYS,
    NO_RATE,
    SCOPES,
    ScopeLimits,
    describe_scope,
    parse_rate,
)
from lmrelay.state import (
    DIALECTS,
    RESERVED_UPSTREAM_NAMES,
    RelayState,
    load_state,
    state_path_for,
    write_private,
)

logger = logging.getLogger(__name__)

# Config
CONFIG_NAME       = "lmrelay.toml"
HOME_CONFIG_PATH  = Path.home() / ".lmrelay" / CONFIG_NAME

# Names which file to read, not a setting: settings come only from that file and
# state.json, never from the environment.
CONFIG_ENV_VAR = "LMRELAY_CONFIG"

# Some names are defined in state.py and ratelimit.py (which cannot import this
# module without a cycle) and re-exported here.
__all__ = [
    "DIALECTS",
    "RESERVED_UPSTREAM_NAMES",
    "SCOPES",
    "ConfigError",
    "RelayConfig",
    "ScopeLimits",
    "Upstream",
    "check_exposure",
    "describe_upstreams",
    "find_config_path",
    "load_config",
    "parse_upstream",
    "parse_upstreams",
]

# Defaults for [server]; port 11435 leaves 11434 to the Ollama already installed.
DEFAULT_HOST            = "127.0.0.1"
DEFAULT_PORT            = 11435
DEFAULT_UPSTREAM        = "ollama"
DEFAULT_DIALECT         = "openai"
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_LOG_LEVEL       = "INFO"

SERVER_KEYS = ("host", "port", "default_upstream", "connect_timeout", "log_level")

# Old limit keys, refused with an explanation: a scope written only in keys
# nobody reads is off while its author believes it is on.
RETIRED_LIMIT_KEYS = ("burst", "requests", "period")

# For `lmrelay limits set`: a scope's body runs from its header to the next table.
LIMIT_TABLE_HEADER = re.compile(r"^\s*\[\s*limits\s*\.\s*([A-Za-z0-9_]+)\s*\]\s*$")
ANY_TABLE_HEADER   = re.compile(r"^\s*\[")
# The `=` column of the example config, so inserted keys line up.
LIMIT_KEY_WIDTH    = max(len(key) for key in LIMIT_KEYS)


@dataclass(frozen=True)
class Upstream:
    """One [upstream.<name>] table: where to forward and what to send with it."""

    name: str
    base_url: str
    dialect: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RelayConfig:
    """The validated lmrelay.toml with state.json merged in."""

    host: str
    port: int
    default_upstream: str
    connect_timeout: int
    log_level: str
    limits: dict[str, ScopeLimits]
    auth_enabled: bool
    auth_tokens: tuple[str, ...]
    upstreams: dict[str, Upstream]
    config_path: Path
    state_path: Path


def find_config_path() -> Path | None:
    """Return the first config location that applies: env var, CWD, then home."""
    from_env = os.getenv(CONFIG_ENV_VAR)
    if from_env:
        # Returned even when missing, so a wrong path is reported, not skipped.
        return Path(from_env).expanduser()
    local_candidate = Path.cwd() / CONFIG_NAME
    if local_candidate.exists():
        return local_candidate
    if HOME_CONFIG_PATH.exists():
        return HOME_CONFIG_PATH
    return None


def expand_env_value(value: str, upstream_name: str, header_name: str) -> str:
    """Expand ${VAR} from the process environment so API keys can stay out of the file."""
    try:
        return Template(value).substitute(os.environ)
    except KeyError as exc:
        variable = exc.args[0]
        raise ConfigError(
            f"lmrelay: upstream '{upstream_name}' header '{header_name}' references "
            f"${{{variable}}}, which is not set"
        )
    except ValueError as exc:
        raise ConfigError(
            f"lmrelay: upstream '{upstream_name}' header '{header_name}' has a malformed "
            f"${{...}} reference: {exc}"
        )


def parse_upstream(name: str, table: dict, expand_env: bool = True) -> Upstream:
    """Validate one [upstream.<name>] table and freeze it.

    `expand_env` is off for CLI-added providers and bundles: their values are
    already final, and expanding an API key that contains `$` would replace part
    of it with an environment variable and send that to the provider.
    """
    if name in RESERVED_UPSTREAM_NAMES:
        raise ConfigError(
            f"lmrelay: upstream name '{name}' is reserved: it would shadow the "
            f"Ollama/OpenAI path root"
        )
    if not isinstance(table, dict):
        raise ConfigError(f"lmrelay: [upstream.{name}] must be a table")

    base_url = table.get("base_url")
    if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"lmrelay: upstream '{name}' needs a base_url starting with http:// or https://"
        )

    dialect = table.get("dialect", DEFAULT_DIALECT)
    if dialect not in DIALECTS:
        raise ConfigError(
            f"lmrelay: upstream '{name}' has dialect '{dialect}'; "
            f"expected one of {', '.join(DIALECTS)}"
        )

    raw_headers = table.get("headers") or {}
    if not isinstance(raw_headers, dict):
        raise ConfigError(f"lmrelay: upstream '{name}' headers must be a table of strings")
    headers = {
        key: expand_env_value(str(value), name, key) if expand_env else str(value)
        for key, value in raw_headers.items()
    }

    # A trailing slash doubles up with the forwarded path (redirect or 404).
    return Upstream(name=name, base_url=base_url.rstrip("/"), dialect=dialect, headers=headers)


def parse_upstreams(data: dict) -> dict[str, Upstream]:
    """Validate every [upstream.*] table. None at all is legal, since state may supply them."""
    section = data.get("upstream") or {}
    if not isinstance(section, dict):
        raise ConfigError("lmrelay: [upstream] must be a table of upstream tables")
    return {name: parse_upstream(name, table) for name, table in section.items()}


def state_upstreams(providers: dict[str, dict]) -> dict[str, Upstream]:
    """Validate CLI-added providers with parse_upstream, without env expansion.

    One validator for both sources, so they fail in the same ways.
    """
    return {
        name: parse_upstream(
            name,
            {
                "base_url": provider.get("base_url"),
                "dialect": provider.get("dialect", DEFAULT_DIALECT),
                "headers": provider.get("headers") or {},
            },
            expand_env=False,
        )
        for name, provider in providers.items()
    }


def merge_upstreams(
    from_file: dict[str, Upstream],
    from_state: dict[str, Upstream],
    config_path: Path,
) -> dict[str, Upstream]:
    """Merge both sources with state winning, and say so when it shadows the file."""
    shadowed = sorted(set(from_file) & set(from_state))
    if shadowed:
        logger.warning(
            f"lmrelay: provider(s) {', '.join(shadowed)} from state.json shadow the "
            f"[upstream.*] of the same name in {config_path}"
        )
    return from_file | from_state


def collect_auth_tokens(state: RelayState, data: dict) -> tuple[str, ...]:
    """Every credential a caller may present, in the order they were configured.

    `[auth] token` from the file is added to the stored tokens, not a
    replacement for them. It does not turn checking on; `lmrelay auth true` does.
    """
    auth = data.get("auth") or {}
    candidates = [record.token for record in state.tokens]
    candidates.append(str(auth.get("token") or ""))
    return tuple(dict.fromkeys(token for token in candidates if token))


def read_int(
    table: dict, section: str, name: str, default: int, minimum: int | None = None
) -> int:
    """Read one whole-number key, raising ConfigError rather than ValueError.

    ValueError is not an LmrelayError, so a reload would not catch it. `section`
    names the table in the error; `minimum` rejects values such as
    `concurrent = -1` instead of treating them as "off".
    """
    value = table.get(name, default)
    # Strings come from the CLI. Floats and bools are refused, not coerced.
    whole = isinstance(value, (int, str)) and not isinstance(value, bool)
    try:
        number = int(value) if whole else None
    except ValueError:
        number = None
    if number is None:
        raise ConfigError(f"lmrelay: [{section}] {name} must be a whole number, got {value!r}")
    if minimum is not None and number < minimum:
        raise ConfigError(
            f"lmrelay: [{section}] {name} cannot be less than {minimum}, got {value!r}"
        )
    return number


def read_rate(table: dict, section: str, name: str) -> str:
    """Read a rate as `count/period`; empty (the default) means no rate limit.

    A period without a unit (`30/30`) is ambiguous and refused. Non-strings,
    including TOML's `inf` and `nan`, are refused too: a nan limiter would
    compare false against every threshold and refuse nobody.
    """
    value = table.get(name, NO_RATE)
    if not isinstance(value, str) or (value and parse_rate(value) is None):
        raise ConfigError(
            f"lmrelay: [{section}] {name} must be a count, a slash and a period, the period "
            f"a whole number and one of s, m, h: \"10/30m\", \"2/1h\", \"1/60s\", or \"\" for "
            f"no limit on how often. Got {value!r}"
        )
    # The spelling, not the numbers: see ScopeLimits.
    return value


def read_log_level(server: dict) -> str:
    """Read log_level, refusing a name logging would silently treat as INFO."""
    value = str(server.get("log_level", DEFAULT_LOG_LEVEL))
    if value.upper() not in logging.getLevelNamesMapping():
        raise ConfigError(
            f"lmrelay: [server] log_level '{value}' is not a logging level; "
            f"expected DEBUG, INFO, WARNING, ERROR or CRITICAL"
        )
    return value


def parse_server(data: dict) -> dict:
    """Validate [server] and fill in its defaults.

    Unknown keys are refused, not ignored: a misspelt `prot = 8080` would
    otherwise silently do nothing.
    """
    server = data.get("server") or {}
    if not isinstance(server, dict):
        raise ConfigError("lmrelay: [server] must be a table")
    unknown = sorted(set(server) - set(SERVER_KEYS))
    if unknown:
        raise ConfigError(
            f"lmrelay: [server] has no key {', '.join(unknown)}; expected "
            f"{', '.join(SERVER_KEYS)}. Limits live in [limits.*]; see docs/CONFIGURATION.md."
        )
    host = server.get("host", DEFAULT_HOST)
    if not isinstance(host, str):
        raise ConfigError(f"lmrelay: [server] host must be a string, got {host!r}")
    return {
        "host": host,
        "port": read_int(server, "server", "port", DEFAULT_PORT),
        "default_upstream": server.get("default_upstream", DEFAULT_UPSTREAM),
        "connect_timeout": read_int(server, "server", "connect_timeout", DEFAULT_CONNECT_TIMEOUT),
        "log_level": read_log_level(server),
    }


def check_retired_limit_keys(table: dict, scope: str) -> None:
    """Refuse the retired limit keys that `concurrent` and `rate` replaced."""
    found = [name for name in RETIRED_LIMIT_KEYS if name in table]
    if found:
        raise ConfigError(
            f"lmrelay: [limits.{scope}] {', '.join(found)} "
            f"{'were' if len(found) > 1 else 'was'} replaced by concurrent and rate. "
            f"'concurrent' is how many at once and 'rate' is how often, and they are two "
            f"numbers because they are two questions: concurrent = 2, rate = \"10/30m\". "
            f"See docs/CONFIGURATION.md."
        )


def parse_limits(data: dict) -> dict[str, ScopeLimits]:
    """Validate [limits.*]: three scopes, the same two keys, everything off by default.

    Unknown scopes and keys are refused, not ignored: a misspelt table looks
    like a limit that is on but never refuses anybody.
    """
    section = data.get("limits") or {}
    if not isinstance(section, dict):
        raise ConfigError("lmrelay: [limits] must be a table of scope tables")
    unknown = sorted(set(section) - set(SCOPES))
    if unknown:
        raise ConfigError(
            f"lmrelay: [limits] has no scope {', '.join(unknown)}; expected "
            f"{', '.join(f'[limits.{scope}]' for scope in SCOPES)}"
        )

    limits = {}
    for scope in SCOPES:
        table = section.get(scope) or {}
        if not isinstance(table, dict):
            raise ConfigError(f"lmrelay: [limits.{scope}] must be a table")
        check_retired_limit_keys(table, scope)
        stray = sorted(set(table) - set(LIMIT_KEYS))
        if stray:
            raise ConfigError(
                f"lmrelay: [limits.{scope}] has no key {', '.join(stray)}; expected "
                f"{', '.join(LIMIT_KEYS)}"
            )
        label = f"limits.{scope}"
        limits[scope] = ScopeLimits(
            concurrent=read_int(table, label, "concurrent", 0, minimum=0),
            rate=read_rate(table, label, "rate"),
        )
    return limits


def read_config_file(target: Path) -> dict:
    """Parse the TOML, reporting a read or syntax failure as the operator's error."""
    try:
        with target.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"lmrelay: cannot read {target}: {type(exc).__name__}: {exc}")


def read_config_text(target: Path) -> str:
    """Return the config as text, for `lmrelay limits set`."""
    try:
        return target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"lmrelay: cannot read {target}: {type(exc).__name__}: {exc}")


def parse_config_text(text: str, target: Path) -> dict:
    """Parse config text, naming the file it came from in any error."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"lmrelay: cannot read {target}: {type(exc).__name__}: {exc}")


def render_limit_value(value: int | str) -> str:
    """Render one limit value as TOML: a bare count or a quoted rate."""
    return f'"{value}"' if isinstance(value, str) else str(value)


def is_key_line(line: str, name: str) -> bool:
    """Return whether this line assigns exactly `name`, not a longer key."""
    remainder = line.lstrip()
    if not remainder.startswith(name):
        return False
    return remainder[len(name):].lstrip().startswith("=")


def rewrite_key_line(line: str, rendered: str) -> str:
    """Replace the value on an assignment, keeping the indent and any trailing comment."""
    head, unused_separator, remainder = line.partition("=")
    ending = "\n" if line.endswith("\n") else ""
    body = remainder.rstrip("\n")
    comment = ""
    if "#" in body:
        at = body.index("#")
        value, comment = body[:at], body[at:]
        # Keep the original gap before the comment.
        comment = value[len(value.rstrip()):] + comment
    return f"{head}= {rendered}{comment}{ending}"


def new_limit_section(scope: str, values: dict[str, int | str]) -> str:
    """Render a new `[limits.<scope>]` table to append to the file."""
    written = [
        f"{key:<{LIMIT_KEY_WIDTH}} = {render_limit_value(values[key])}\n"
        for key in LIMIT_KEYS if key in values
    ]
    return f"\n[limits.{scope}]\n" + "".join(written)


def edit_limit_section(text: str, scope: str, values: dict[str, int | str]) -> str:
    """Return the config with one scope's values changed and nothing else touched.

    Edited line by line rather than re-rendered, so the operator's comments
    survive. Missing keys are appended to the scope; a missing scope to the file.
    """
    lines = text.splitlines(keepends=True)
    opens = [
        index for index, line in enumerate(lines)
        if (match := LIMIT_TABLE_HEADER.match(line)) and match.group(1) == scope
    ]
    if not opens:
        return text + new_limit_section(scope, values)

    start = opens[0]
    end = start + 1
    while end < len(lines) and not ANY_TABLE_HEADER.match(lines[end]):
        end += 1
    body = lines[start + 1:end]

    for name in LIMIT_KEYS:
        if name not in values:
            continue
        rendered = render_limit_value(values[name])
        for offset, line in enumerate(body):
            if is_key_line(line, name):
                body[offset] = rewrite_key_line(line, rendered)
                break
        else:
            # Before trailing blank lines, which separate the next table.
            place = len(body)
            while place > 0 and not body[place - 1].strip():
                place -= 1
            body.insert(place, f"{name:<{LIMIT_KEY_WIDTH}} = {rendered}\n")

    return "".join(lines[:start + 1] + body + lines[end:])


def set_scope_limits(
    target: Path, scope: str, wanted: ScopeLimits
) -> tuple[ScopeLimits, ScopeLimits]:
    """Write one whole scope into the config file. Returns (before, after).

    Both keys are always written, so `limits set total 1` also clears an old
    rate. The edited text is parsed and checked before writing, so a layout this
    editor cannot handle (e.g. inline tables) is refused with the file untouched.
    """
    text = read_config_text(target)
    if text and not text.endswith("\n"):
        text += "\n"
    before = parse_limits(parse_config_text(text, target))[scope]

    values: dict[str, int | str] = {"concurrent": wanted.concurrent, "rate": wanted.rate}
    edited = edit_limit_section(text, scope, values)
    try:
        after = parse_limits(parse_config_text(edited, target))[scope]
    except ConfigError as exc:
        raise ConfigError(
            f"lmrelay: editing [limits.{scope}] in {target} would not have left a file this "
            f"relay can read ({exc}). Edit it by hand; nothing has been written."
        )
    if after != wanted:
        raise ConfigError(
            f"lmrelay: [limits.{scope}] in {target} would still read "
            f"{describe_scope(after)} rather than {describe_scope(wanted)}. Edit it by "
            f"hand; nothing has been written."
        )

    # 0600: the file may hold provider keys.
    write_private(target, edited)
    return before, after


def load_config(path: Path | None = None) -> RelayConfig:
    """Read and validate the config. Raises ConfigError with an operator-facing message."""
    target = path or find_config_path()
    if target is None:
        raise ConfigError(
            f"lmrelay: no config found; looked at ./{CONFIG_NAME} and {HOME_CONFIG_PATH}. "
            f"Run 'lmrelay init'."
        )
    data = read_config_file(target)

    state_file = state_path_for(target)
    state = load_state(state_file)
    upstreams = merge_upstreams(
        parse_upstreams(data), state_upstreams(state.providers), target
    )
    if not upstreams:
        raise ConfigError(
            f"lmrelay: config has no [upstream.*] sections in {target} and no providers in "
            f"{state_file}. Run 'lmrelay provider add' or add an [upstream.*] table."
        )

    server = parse_server(data)
    default_upstream = server["default_upstream"]
    if default_upstream not in upstreams:
        raise ConfigError(
            f"lmrelay: default_upstream '{default_upstream}' is not defined; "
            f"known upstreams: {', '.join(sorted(upstreams))}"
        )

    limits = parse_limits(data)
    auth_enabled = state.auth_enabled
    auth_tokens = collect_auth_tokens(state, data)
    if auth_tokens and not auth_enabled:
        logger.warning(
            f"lmrelay: {len(auth_tokens)} caller token(s) configured but auth is off; "
            f"run 'lmrelay auth true' to require them"
        )
    if limits["per_token"].configured() and not auth_enabled:
        # Legal (it applies once auth is on), but worth a warning.
        logger.warning(
            "lmrelay: [limits.per_token] is configured but auth is off, so nothing is "
            "keyed by a token. [limits.per_address] and [limits.total] still apply. "
            "Run 'lmrelay auth true'."
        )

    return RelayConfig(
        **server,
        limits=limits,
        auth_enabled=auth_enabled,
        auth_tokens=auth_tokens,
        upstreams=upstreams,
        config_path=target,
        state_path=state_file,
    )


def describe_upstreams(config: RelayConfig) -> str:
    """Return the upstream names as one line for logs and `status`."""
    return ", ".join(sorted(config.upstreams))


def check_exposure(config: RelayConfig) -> str | None:
    """Return a warning if a non-loopback bind demands no credential.

    A warning, not a refusal: running without auth behind an authenticating
    proxy is legitimate.
    """
    if config.auth_enabled:
        return None
    try:
        is_loopback = ipaddress.ip_address(config.host).is_loopback
    except ValueError:
        # Not "": an empty host binds every interface.
        is_loopback = config.host == "localhost"
    if is_loopback:
        return None
    return (
        f"lmrelay: listening on {config.host} with auth off. Every caller that can reach "
        f"this port can use the configured upstream credentials. Run 'lmrelay auth true'."
    )


def main():
    pass


if __name__ == "__main__":
    main()
