#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discovery, parsing and validation of lmrelay.toml, merged with the CLI-owned state."""

import ipaddress
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

# Local imports
from lmrelay.errors import ConfigError
from lmrelay.state import DIALECTS, RESERVED_UPSTREAM_NAMES, RelayState, load_state, state_path_for

logger = logging.getLogger(__name__)

# Config
CONFIG_NAME       = "lmrelay.toml"
CONFIG_ENV_VAR    = "LMRELAY_CONFIG"
TOKEN_ENV_VAR     = "LMRELAY_TOKEN"
HOME_CONFIG_PATH  = Path.home() / ".lmrelay" / CONFIG_NAME

# Re-exported from the modules that own them so `from lmrelay.config import ...`
# keeps working: state.py cannot import config.py without a cycle, so the
# definitions live there and are surfaced here.
__all__ = [
    "DIALECTS",
    "RESERVED_UPSTREAM_NAMES",
    "ConfigError",
    "RelayConfig",
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


@dataclass(frozen=True)
class Upstream:
    """One [upstream.<name>] table: where to forward and what to send with it."""

    name: str
    base_url: str
    dialect: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RelayConfig:
    """The whole of lmrelay.toml and state.json, validated and merged."""

    host: str
    port: int
    default_upstream: str
    connect_timeout: int
    log_level: str
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

    The TOML token and $LMRELAY_TOKEN are additional valid tokens rather than
    overrides: a container can inject one without invalidating the tokens the
    operator generated.
    """
    auth = data.get("auth") or {}
    candidates = [record.token for record in state.tokens]
    candidates.append(str(auth.get("token") or ""))
    candidates.append(os.getenv(TOKEN_ENV_VAR) or "")
    return tuple(dict.fromkeys(token for token in candidates if token))


def read_int(server: dict, name: str, default: int) -> int:
    """Read one numeric [server] key as the operator's error rather than int()'s.

    A bare int() raises ValueError, which is a sibling of ConfigError rather than
    one of its own kind, so a reload's `except LmrelayError` does not catch it:
    `port = "eleven"` left the signal handler as a traceback, from a file that
    had parsed perfectly well as TOML.
    """
    value = server.get(name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"lmrelay: [server] {name} must be a whole number, got {value!r}")


def read_log_level(server: dict) -> str:
    """Read log_level, refusing a name logging would quietly read as INFO.

    getattr(logging, level) falls back on its own, so an unrecognised level was
    announced as applied by the reload and then discarded: the relay ran at INFO
    while the log said it was running at something else.
    """
    value = str(server.get("log_level", DEFAULT_LOG_LEVEL))
    if value.upper() not in logging.getLevelNamesMapping():
        raise ConfigError(
            f"lmrelay: [server] log_level '{value}' is not a logging level; "
            f"expected DEBUG, INFO, WARNING, ERROR or CRITICAL"
        )
    return value


def load_config(path: Path | None = None) -> RelayConfig:
    """Read and validate the config. Raises ConfigError with an operator-facing message."""
    target = path or find_config_path()
    if target is None:
        raise ConfigError(
            f"lmrelay: no config found; looked at ./{CONFIG_NAME} and {HOME_CONFIG_PATH}. "
            f"Run 'lmrelay init'."
        )
    try:
        with target.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"lmrelay: cannot read {target}: {type(exc).__name__}: {exc}")

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
    default_upstream = server.get("default_upstream", DEFAULT_UPSTREAM)
    if default_upstream not in upstreams:
        raise ConfigError(
            f"lmrelay: default_upstream '{default_upstream}' is not defined; "
            f"known upstreams: {', '.join(sorted(upstreams))}"
        )

    auth_tokens = collect_auth_tokens(state, data)
    if auth_tokens and not state.auth_enabled:
        logger.warning(
            f"lmrelay: {len(auth_tokens)} caller token(s) configured but auth is off; "
            f"run 'lmrelay auth true' to require them"
        )

    return RelayConfig(
        host=server.get("host", DEFAULT_HOST),
        port=read_int(server, "port", DEFAULT_PORT),
        default_upstream=default_upstream,
        connect_timeout=read_int(server, "connect_timeout", DEFAULT_CONNECT_TIMEOUT),
        log_level=read_log_level(server),
        auth_enabled=state.auth_enabled,
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
