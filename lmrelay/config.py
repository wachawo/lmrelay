#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discovery, parsing and validation of lmrelay.toml, merged with the environment and the state."""

import copy
import ipaddress
import logging
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

# Local imports
from lmrelay.errors import ConfigError, StateError
from lmrelay.ratelimit import LIMIT_KEYS, SCOPES, ScopeLimits
from lmrelay.state import (
    DIALECTS,
    PROVIDER_PRESETS,
    RESERVED_UPSTREAM_NAMES,
    RelayState,
    add_provider,
    load_state,
    state_path_for,
)

logger = logging.getLogger(__name__)

# Config
CONFIG_NAME       = "lmrelay.toml"
HOME_CONFIG_PATH  = Path.home() / ".lmrelay" / CONFIG_NAME

# Environment. Every setting is LMRELAY_ plus the path to its key, uppercased,
# segments joined by underscores: [limits.per_token] rate is
# LMRELAY_LIMITS_PER_TOKEN_RATE. No abbreviations and no special cases, so the
# name is derivable from the file without a table.
ENV_PREFIX           = "LMRELAY_"
CONFIG_ENV_VAR       = ENV_PREFIX + "CONFIG"
TOKEN_ENV_VAR        = ENV_PREFIX + "TOKEN"
AUTH_ENABLED_ENV_VAR = ENV_PREFIX + "AUTH_ENABLED"
UPSTREAM_ENV_PREFIX  = ENV_PREFIX + "UPSTREAM_"

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

# Every file key the environment can name, as the path it sets. Upstreams are
# not here: their names are not known in advance, so they are matched by prefix.
SETTING_PATHS = (
    *(("server", key) for key in SERVER_KEYS),
    *(("limits", scope, key) for scope in SCOPES for key in LIMIT_KEYS),
    ("auth", "token"),
)
ENV_NAMES = {ENV_PREFIX + "_".join(path).upper(): path for path in SETTING_PATHS}

# The prefixes under which an unrecognised variable is a typo rather than a
# name this version has not heard of. Everything outside them, LMRELAY_CONFIG
# and LMRELAY_STATE and LMRELAY_TOKEN and the CLI's own, is left alone.
#
# LMRELAY_AUTH_ is in the list for the reason the strict value list below
# exists, and it was the one prefix missing from it: LMRELAY_AUTH_ENABLE, one
# keystroke from the real name, was read by nothing and left auth off on a relay
# whose operator had just turned it on, with the container's own credentials
# behind it. A typo silently ignored is the same outcome as a typo read as
# false, and both of them are an open relay.
CHECKED_ENV_PREFIXES = (
    ENV_PREFIX + "SERVER_", ENV_PREFIX + "LIMITS_", ENV_PREFIX + "AUTH_", UPSTREAM_ENV_PREFIX
)
# The switch is not in SETTING_PATHS, since it lives in the state rather than in
# the file, so check_env_names has to be told its name is a real one.
KNOWN_ENV_NAMES = frozenset(ENV_NAMES) | {AUTH_ENABLED_ENV_VAR}

# The closed set of upstream fields the environment can set, longest first so
# that LMRELAY_UPSTREAM_MY_LLM_BASE_URL is my_llm and BASE_URL rather than a
# name ending in _BASE with a field of URL.
UPSTREAM_ENV_FIELDS = ("BASE_URL", "DIALECT", "KEY")
UPSTREAM_ENV_ORDER  = sorted(UPSTREAM_ENV_FIELDS, key=len, reverse=True)
# An upstream named from the environment is limited to what an environment
# variable name can carry. A hyphenated name needs the file.
UPSTREAM_ENV_NAME   = re.compile(r"^[a-z0-9_]+$")

# Strict on purpose, and refusing anything else by name. This is the one place
# liberality is dangerous: a typo silently read as false is auth turned off.
TRUE_VALUES  = ("1", "true", "yes", "on")
FALSE_VALUES = ("0", "false", "no", "off")

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


def env_value(name: str) -> str:
    """One environment variable, with absent and empty meaning the same thing.

    Empty has to mean unset because `Environment="LMRELAY_SERVER_PORT="` in a
    unit file and `LMRELAY_SERVER_PORT:` in a compose file are how people write
    "I am not setting this", and reading that as port 0 would bind something
    absurd. A value, including 0, is a value.
    """
    return (os.getenv(name) or "").strip()


def env_flag(name: str, default: bool) -> bool:
    """One boolean environment variable, refusing anything ambiguous by name."""
    value = env_value(name).lower()
    if not value:
        return default
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ConfigError(
        f"lmrelay: ${name} is '{value}'; expected one of "
        f"{', '.join(TRUE_VALUES + FALSE_VALUES)}"
    )


def split_upstream_var(name: str) -> tuple[str, str] | None:
    """The upstream and the field one LMRELAY_UPSTREAM_* variable names, or None."""
    remainder = name[len(UPSTREAM_ENV_PREFIX):]
    for field_name in UPSTREAM_ENV_ORDER:
        suffix = "_" + field_name
        if remainder.endswith(suffix) and len(remainder) > len(suffix):
            return remainder[: -len(suffix)].lower(), field_name
    return None


def read_env_upstreams() -> dict[str, dict[str, str]]:
    """Every upstream the environment names, as its fields.

    An unrecognised LMRELAY_UPSTREAM_* is refused rather than ignored, for the
    same reason the old [server] limit keys are: a variable nobody reads leaves
    an operator believing a provider is configured while the relay has never
    heard of it.
    """
    found: dict[str, dict[str, str]] = {}
    for name in sorted(os.environ):
        if not name.startswith(UPSTREAM_ENV_PREFIX):
            continue
        split = split_upstream_var(name)
        if split is None:
            raise ConfigError(
                f"lmrelay: ${name} names no upstream setting; expected "
                f"{UPSTREAM_ENV_PREFIX}<NAME>_"
                f"{f', {UPSTREAM_ENV_PREFIX}<NAME>_'.join(UPSTREAM_ENV_FIELDS)}"
            )
        upstream_name, field_name = split
        if not UPSTREAM_ENV_NAME.match(upstream_name):
            raise ConfigError(
                f"lmrelay: ${name} names upstream '{upstream_name}', which is not "
                f"letters, digits and underscores; name it in the config file instead"
            )
        value = env_value(name)
        if value:
            found.setdefault(upstream_name, {})[field_name] = value
    return found


def check_env_names() -> None:
    """Refuse a variable under a structured prefix that names no setting.

    read_env_upstreams has already refused the upstream ones by the time this
    runs, so what is left is a misspelt [server], [limits] or auth key. Only
    these prefixes are checked: the names outside them are the documented
    carve-outs, LMRELAY_CONFIG and LMRELAY_STATE and LMRELAY_TOKEN and the CLI's
    own, and a blanket refusal would break the next one to be added.
    """
    for name in sorted(os.environ):
        if not name.startswith(CHECKED_ENV_PREFIXES):
            continue
        if name in KNOWN_ENV_NAMES or name.startswith(UPSTREAM_ENV_PREFIX):
            continue
        raise ConfigError(
            f"lmrelay: ${name} names no setting. Every setting is LMRELAY_ plus the "
            f"path to its key: [limits.per_token] rate is LMRELAY_LIMITS_PER_TOKEN_RATE."
        )


def read_env_settings() -> dict[tuple[str, ...], str]:
    """Every environment variable that names a file key, as its path and raw value."""
    found = {}
    for name, path in ENV_NAMES.items():
        value = env_value(name)
        if value:
            found[path] = value
    return found


def path_in_file(data: dict, path: tuple[str, ...]) -> bool:
    """Whether the parsed file already carries this key, for the shadow warning."""
    table: object = data
    for segment in path[:-1]:
        if not isinstance(table, dict):
            return False
        table = table.get(segment)
    return isinstance(table, dict) and path[-1] in table


def apply_env_settings(data: dict, settings: dict[tuple[str, ...], str]) -> dict:
    """Lay the environment over the file and return the result: the specific wins.

    The file is the shared, checked-in thing and the environment is the
    deployment, which is what every operator already expects. The alternative
    makes an environment variable a silent no-op whenever the file happens to
    mention the key.

    Values land as strings and are validated by the same readers the file goes
    through, so `LMRELAY_SERVER_PORT=eleven` is refused in the words a quoted
    port in the file is refused in.
    """
    merged = copy.deepcopy(data)
    for path, value in settings.items():
        table = merged
        for segment in path[:-1]:
            existing = table.get(segment)
            table[segment] = existing if isinstance(existing, dict) else {}
            table = table[segment]
        table[path[-1]] = value
    return merged


def warn_about_shadows(data: dict, settings: dict[tuple[str, ...], str], target: Path) -> None:
    """Name the keys the environment is overriding, and only those.

    The risk environment precedence creates is real: an operator edits the file,
    reloads, and nothing changes. Naming only genuine shadows keeps the line
    short and about the actual confusion.

    Ordered by the documented file rather than by the environment, so two relays
    with the same settings print the same line. Upstream keys are not in that
    list, since their names are not known in advance, so they follow sorted.
    """
    documented = [path for path in SETTING_PATHS if path in settings]
    rest = sorted(path for path in settings if path not in set(SETTING_PATHS))
    shadowed = [
        ".".join(path) for path in documented + rest if path_in_file(data, path)
    ]
    if shadowed:
        logger.warning(
            f"lmrelay: the environment sets {', '.join(shadowed)}, overriding {target}"
        )


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


def env_providers(state: RelayState, env_upstreams: dict[str, dict[str, str]]) -> RelayState:
    """Add the providers LMRELAY_UPSTREAM_<NAME>_KEY names, as `provider add` would.

    A header cannot be spelled in the environment at all: `x-api-key` and
    `anthropic-version` contain hyphens, which are not usable in a variable
    name, and mapping `-` to `_` is not reversible. So the credential gets the
    shortcut the CLI already has, and it routes through add_provider, which
    makes an environment-configured provider fail in exactly the ways a
    CLI-added one fails.
    """
    for name in sorted(env_upstreams):
        fields = env_upstreams[name]
        if "KEY" not in fields:
            continue
        variable = f"{UPSTREAM_ENV_PREFIX}{name.upper()}_KEY"
        if "BASE_URL" not in fields and name not in PROVIDER_PRESETS:
            raise ConfigError(
                f"lmrelay: ${variable} names no known provider; set "
                f"{UPSTREAM_ENV_PREFIX}{name.upper()}_BASE_URL as well. "
                f"Known providers: {', '.join(sorted(PROVIDER_PRESETS))}"
            )
        try:
            state = add_provider(
                state, name, fields["KEY"],
                base_url=fields.get("BASE_URL"),
                dialect=fields.get("DIALECT"),
            )
        except StateError as exc:
            raise ConfigError(f"lmrelay: ${variable}: {exc}")
    return state


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

    The TOML token and $LMRELAY_TOKEN are additional valid tokens rather than
    overrides: a container can inject one without invalidating the tokens the
    operator generated. $LMRELAY_AUTH_TOKEN is not here because it is an
    ordinary setting and has already been laid over [auth] token by the time
    this runs; $LMRELAY_TOKEN is the older spelling of the same thing and stays
    additive, so setting both and having them differ makes two credentials
    rather than a conflict.
    """
    auth = data.get("auth") or {}
    candidates = [record.token for record in state.tokens]
    candidates.append(str(auth.get("token") or ""))
    candidates.append(os.getenv(TOKEN_ENV_VAR) or "")
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
    env_upstreams = read_env_upstreams()
    check_env_names()

    target = path or find_config_path()
    if target is None:
        if not env_upstreams:
            raise ConfigError(
                f"lmrelay: no config found; looked at ./{CONFIG_NAME} and {HOME_CONFIG_PATH}, "
                f"and the environment names no upstream. Run 'lmrelay init'."
            )
        # A fileless container: the environment carries a whole relay, so the
        # config path names only where the file would have been, which is what
        # the pidfile and state.json are still located beside.
        target, data = HOME_CONFIG_PATH, {}
    else:
        data = read_config_file(target)

    # An upstream the environment names without a key is an ordinary setting and
    # goes through the same overlay, so an env base_url over a file table is
    # announced like any other shadow. The ones with a key do not: they route
    # through add_provider into the state, which shadows the file already and
    # says so in its own words.
    env_settings = read_env_settings()
    for name in sorted(env_upstreams):
        fields = env_upstreams[name]
        if "KEY" in fields:
            continue
        for field_name in sorted(fields):
            env_settings[("upstream", name, field_name.lower())] = fields[field_name]
    warn_about_shadows(data, env_settings, target)
    data = apply_env_settings(data, env_settings)

    state_file = state_path_for(target)
    state = env_providers(load_state(state_file), env_upstreams)
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
    # The switch lives in the state, but a container needs it without running
    # the CLI, so the environment can set it and wins like every other setting.
    auth_enabled = env_flag(AUTH_ENABLED_ENV_VAR, state.auth_enabled)
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
