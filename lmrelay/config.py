#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discovery, parsing and validation of lmrelay.toml."""

import ipaddress
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

# Config
CONFIG_NAME       = "lmrelay.toml"
CONFIG_ENV_VAR    = "LMRELAY_CONFIG"
TOKEN_ENV_VAR     = "LMRELAY_TOKEN"
HOME_CONFIG_PATH  = Path.home() / ".lmrelay" / CONFIG_NAME
DIALECTS          = ("ollama", "openai", "anthropic")

# An upstream under either name would swallow the path root that every Ollama
# (/api/...) and OpenAI (/v1/...) client already sends to, and the breakage
# would surface as an unexplained 404 far from its cause.
RESERVED_UPSTREAM_NAMES = ("api", "v1")

# Defaults for [server]; port 11434 is the one Ollama clients already expect.
DEFAULT_HOST            = "127.0.0.1"
DEFAULT_PORT            = 11434
DEFAULT_UPSTREAM        = "ollama"
DEFAULT_DIALECT         = "openai"
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_LOG_LEVEL       = "INFO"


class ConfigError(ValueError):
    """An unusable configuration. The message is shown to the operator verbatim."""


@dataclass(frozen=True)
class Upstream:
    """One [upstream.<name>] table: where to forward and what to send with it."""

    name: str
    base_url: str
    dialect: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RelayConfig:
    """The whole of lmrelay.toml, validated."""

    host: str
    port: int
    default_upstream: str
    connect_timeout: int
    log_level: str
    auth_token: str | None
    upstreams: dict[str, Upstream]
    config_path: Path


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


def parse_upstream(name: str, table: dict) -> Upstream:
    """Validate one [upstream.<name>] table and freeze it."""
    if name in RESERVED_UPSTREAM_NAMES:
        raise ConfigError(
            f"lmrelay: upstream name '{name}' is reserved — it would shadow the "
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
        key: expand_env_value(str(value), name, key)
        for key, value in raw_headers.items()
    }

    # A trailing slash here would double up against the forwarded path, which
    # some upstreams answer with a redirect and others with a 404.
    return Upstream(name=name, base_url=base_url.rstrip("/"), dialect=dialect, headers=headers)


def parse_upstreams(data: dict) -> dict[str, Upstream]:
    """Validate every [upstream.*] table. At least one is required."""
    section = data.get("upstream") or {}
    if not isinstance(section, dict) or not section:
        raise ConfigError("lmrelay: config has no [upstream.*] sections")
    return {name: parse_upstream(name, table) for name, table in section.items()}


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

    upstreams = parse_upstreams(data)
    server = data.get("server") or {}
    default_upstream = server.get("default_upstream", DEFAULT_UPSTREAM)
    if default_upstream not in upstreams:
        raise ConfigError(
            f"lmrelay: default_upstream '{default_upstream}' is not defined; "
            f"known upstreams: {', '.join(sorted(upstreams))}"
        )

    auth = data.get("auth") or {}
    # The env var wins so the secret need not sit in a file on a shared box.
    auth_token = os.getenv(TOKEN_ENV_VAR) or auth.get("token") or None

    return RelayConfig(
        host=server.get("host", DEFAULT_HOST),
        port=int(server.get("port", DEFAULT_PORT)),
        default_upstream=default_upstream,
        connect_timeout=int(server.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT)),
        log_level=str(server.get("log_level", DEFAULT_LOG_LEVEL)),
        auth_token=auth_token,
        upstreams=upstreams,
        config_path=target,
    )


def check_exposure(config: RelayConfig) -> str | None:
    """Return a warning if a non-loopback bind has no caller credential.

    Not a refusal: running uncredentialed behind an authenticated nginx is
    legitimate, and refusing would break that deployment.
    """
    if config.auth_token:
        return None
    try:
        is_loopback = ipaddress.ip_address(config.host).is_loopback
    except ValueError:
        is_loopback = config.host in ("localhost", "")
    if is_loopback:
        return None
    return (
        f"lmrelay: listening on {config.host} with no [auth].token — every caller that can "
        f"reach this port can use the configured upstream credentials"
    )


def main():
    pass


if __name__ == "__main__":
    main()
