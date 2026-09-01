#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discovery, parsing and validation of lmrelay.toml, merged with the state."""

import ipaddress
import logging
import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

# Local imports
from lmrelay.errors import ConfigError
from lmrelay.ratelimit import LIMIT_KEYS, SCOPES, ScopeLimits
from lmrelay.state import (
    DIALECTS,
    RESERVED_UPSTREAM_NAMES,
    RelayState,
    load_state,
    state_path_for,
)

logger = logging.getLogger(__name__)

# Config
CONFIG_NAME       = "lmrelay.toml"
HOME_CONFIG_PATH  = Path.home() / ".lmrelay" / CONFIG_NAME

# The one environment variable that configures anything, and it names a path
# rather than a setting: which file to read. Settings themselves are in that
# file and in state.json, and nowhere else. There was briefly an environment
# spelling for every key, and what it cost is written up in docs/ROADMAP.md.
CONFIG_ENV_VAR = "LMRELAY_CONFIG"

# Re-exported from the modules that own them so `from lmrelay.config import ...`
# keeps working: state.py and ratelimit.py cannot import config.py without a
# cycle, so the definitions live there and are surfaced here.
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

# Every [server] key, in the order the documented file lists them.
SERVER_KEYS = ("host", "port", "default_upstream", "connect_timeout", "log_level")

# The per-caller keys 0.0.4 shipped without, which now have three scopes each.
# Refused rather than ignored, for one release: a silently ignored key leaves an
# operator believing a limit is on when it is off.
RETIRED_SERVER_KEYS = {
    "rate_limit": "rate",
    "rate_burst": "burst",
    "max_concurrent": "concurrent",
}


@dataclass(frozen=True)
class Upstream:
    """One [upstream.<name>] table: where to forward and what to send with it."""

    name: str
    base_url: str
    dialect: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RelayConfig:
    """The whole of lmrelay.toml, the environment over it, and state.json merged in."""

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
        # Returned even when absent, so a wrong LMRELAY_CONFIG reports itself
        # instead of silently falling through to an unrelated file.
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

    `expand_env` is the one thing a hand-written table and a CLI-added provider
    do not share: `${VAR}` in the file is a documented way to keep a key out of
    it, while a key the CLI stored was already substituted literally, and
    expanding it again would rewrite an API key containing a $ with the value of
    an environment variable, and send that value to the provider.
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

    # A trailing slash here would double up against the forwarded path, which
    # some upstreams answer with a redirect and others with a 404.
    return Upstream(name=name, base_url=base_url.rstrip("/"), dialect=dialect, headers=headers)


def parse_upstreams(data: dict) -> dict[str, Upstream]:
    """Validate every [upstream.*] table. None at all is legal, since state may supply them."""
    section = data.get("upstream") or {}
    if not isinstance(section, dict):
        raise ConfigError("lmrelay: [upstream] must be a table of upstream tables")
    return {name: parse_upstream(name, table) for name, table in section.items()}


def state_upstreams(providers: dict[str, dict]) -> dict[str, Upstream]:
    """Validate CLI-added providers through the parser hand-written tables go through.

    A provider added by one word of CLI and one written out by hand must be able
    to fail in exactly the same ways, so the state record is shaped into a table
    and handed to parse_upstream rather than checked by a second validator. Env
    expansion is the exception: see parse_upstream.
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

    The `[auth] token` in the file is an additional valid token rather than an
    override: it is how an install that never runs the CLI gets a credential,
    and it must not invalidate the tokens an operator generated on the same
    relay. It does not turn checking on; only `lmrelay auth true` does.
    """
    auth = data.get("auth") or {}
    candidates = [record.token for record in state.tokens]
    candidates.append(str(auth.get("token") or ""))
    return tuple(dict.fromkeys(token for token in candidates if token))


def read_int(
    table: dict, section: str, name: str, default: int, minimum: int | None = None
) -> int:
    """Read one numeric key as the operator's error rather than int()'s.

    A bare int() raises ValueError, which is a sibling of ConfigError rather than
    one of its own kind, so a reload's `except LmrelayError` does not catch it:
    `port = "eleven"` left the signal handler as a traceback, from a file that
    had parsed perfectly well as TOML.

    `section` is passed rather than hardcoded because the same reader is used by
    [server] and by three [limits.*] tables, and an error naming the wrong table
    sends an operator to edit a key that is not there.

    `minimum` is passed only by keys where a number below it would be read as
    something the operator did not write: `concurrent = -1` is a mistake, and
    admitting it as another spelling of "off" would hide the mistake behind the
    behaviour they were trying to change. Ports and timeouts pass none and are
    unaffected.
    """
    value = table.get(name, default)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"lmrelay: [{section}] {name} must be a whole number, got {value!r}")
    if minimum is not None and number < minimum:
        raise ConfigError(
            f"lmrelay: [{section}] {name} cannot be less than {minimum}, got {value!r}"
        )
    return number


def read_rate(table: dict, section: str, name: str, default: float) -> float:
    """Read one rate key, refusing a negative as the operator's error.

    A float rather than an int so that a limit under one request per second can
    be written as one: `rate = 0.5` is one request every two seconds, and
    rounding it to zero would silently turn the limit off.
    """
    value = table.get(name, default)
    try:
        rate = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"lmrelay: [{section}] {name} must be a number, got {value!r}")
    if not math.isfinite(rate):
        # TOML spells `nan` and `inf`, and json.loads reads NaN and Infinity, so
        # both reach here through the file, the bundle and the environment
        # alike. Neither is a rate: nan compares false against every threshold,
        # so a limiter would be built, swept for the life of the process, and
        # refuse nobody, while the same value reads as "off" everywhere it is
        # printed. Refused in the same words a word would be.
        raise ConfigError(f"lmrelay: [{section}] {name} must be a number, got {value!r}")
    if rate < 0:
        raise ConfigError(f"lmrelay: [{section}] {name} cannot be negative, got {value!r}")
    return rate


def is_log_level(value: str) -> bool:
    """Whether logging would recognise this level.

    Named rather than inlined because the bundle importer has to make the same
    decision before it writes a config file, and a second spelling of "is this a
    level" is a second thing to keep in step.
    """
    return value.upper() in logging.getLevelNamesMapping()


def read_log_level(server: dict) -> str:
    """Read log_level, refusing a name logging would quietly read as INFO.

    getattr(logging, level) falls back on its own, so an unrecognised level was
    announced as applied by the reload and then discarded: the relay ran at INFO
    while the log said it was running at something else.
    """
    value = str(server.get("log_level", DEFAULT_LOG_LEVEL))
    if not is_log_level(value):
        raise ConfigError(
            f"lmrelay: [server] log_level '{value}' is not a logging level; "
            f"expected DEBUG, INFO, WARNING, ERROR or CRITICAL"
        )
    return value


def check_retired_keys(server: dict) -> None:
    """Refuse the per-caller limit keys the three scopes replaced."""
    for name in RETIRED_SERVER_KEYS:
        if name not in server:
            continue
        replacement = RETIRED_SERVER_KEYS[name]
        scopes = ", ".join(f"[limits.{scope}] {replacement}" for scope in SCOPES)
        raise ConfigError(
            f"lmrelay: [server] {name} was replaced by {scopes}. "
            f"Pick the scope you meant; see docs/CONFIGURATION.md."
        )


def parse_limits(data: dict) -> dict[str, ScopeLimits]:
    """Validate [limits.*]: three scopes, the same three keys, everything off by default.

    A scope or a key nobody recognises is refused rather than ignored, because a
    misspelt table is indistinguishable from a limit that is on until the moment
    it fails to refuse anybody.
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
        stray = sorted(set(table) - set(LIMIT_KEYS))
        if stray:
            raise ConfigError(
                f"lmrelay: [limits.{scope}] has no key {', '.join(stray)}; expected "
                f"{', '.join(LIMIT_KEYS)}"
            )
        label = f"limits.{scope}"
        limits[scope] = ScopeLimits(
            rate=read_rate(table, label, "rate", 0.0),
            burst=read_rate(table, label, "burst", 0.0),
            concurrent=read_int(table, label, "concurrent", 0, minimum=0),
        )
    return limits


def read_config_file(target: Path) -> dict:
    """Parse the TOML, reporting a read or syntax failure as the operator's error."""
    try:
        with target.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"lmrelay: cannot read {target}: {type(exc).__name__}: {exc}")


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

    server = data.get("server") or {}
    check_retired_keys(server)
    default_upstream = server.get("default_upstream", DEFAULT_UPSTREAM)
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
        # Legal, because turning auth on later makes it live, but said once
        # rather than doing nothing quietly.
        logger.warning(
            "lmrelay: [limits.per_token] is configured but auth is off, so nothing is "
            "keyed by a token. [limits.per_address] and [limits.total] still apply. "
            "Run 'lmrelay auth true'."
        )

    return RelayConfig(
        host=server.get("host", DEFAULT_HOST),
        port=read_int(server, "server", "port", DEFAULT_PORT),
        default_upstream=default_upstream,
        connect_timeout=read_int(server, "server", "connect_timeout", DEFAULT_CONNECT_TIMEOUT),
        log_level=read_log_level(server),
        limits=limits,
        auth_enabled=auth_enabled,
        auth_tokens=auth_tokens,
        upstreams=upstreams,
        config_path=target,
        state_path=state_file,
    )


def describe_upstreams(config: RelayConfig) -> str:
    """The upstream list as the startup log, `status` and the reload log all print it."""
    return ", ".join(sorted(config.upstreams))


def check_exposure(config: RelayConfig) -> str | None:
    """Return a warning if a non-loopback bind demands no credential.

    Not a refusal: running uncredentialed behind an authenticated nginx is
    legitimate, and refusing would break that deployment.
    """
    if config.auth_enabled:
        return None
    try:
        is_loopback = ipaddress.ip_address(config.host).is_loopback
    except ValueError:
        is_loopback = config.host in ("localhost", "")
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
