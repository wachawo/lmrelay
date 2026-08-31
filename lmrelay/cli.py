#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Command line interface: process control, tokens and providers."""

import argparse
import logging
import os
import subprocess
from dataclasses import replace
from importlib import resources
from pathlib import Path

import uvicorn

# Local imports
from lmrelay import __version__
from lmrelay.config import (
    CONFIG_ENV_VAR,
    DEFAULT_LOG_LEVEL,
    HOME_CONFIG_PATH,
    ConfigError,
    check_exposure,
    find_config_path,
    load_config,
)
from lmrelay.daemon import (
    daemon_status,
    log_file,
    publish_bind,
    reload_daemon,
    start_detached,
    stop_daemon,
)
from lmrelay.errors import LmrelayError
from lmrelay.logging_setup import setup_logging
from lmrelay.service import (
    LAUNCHD_PLIST_PATH,
    SYSTEMD_UNIT_NAME,
    autostart_status,
    detect_manager,
    disable_autostart,
    enable_autostart,
    service_is_active,
)
from lmrelay.state import (
    DIALECTS,
    CallerToken,
    RelayState,
    add_provider,
    add_token,
    delete_provider,
    delete_token,
    generate_token,
    load_state,
    mask_token,
    save_state,
    set_auth_enabled,
    state_path_for,
)

logger = logging.getLogger(__name__)

EXAMPLE_CONFIG_NAME = "lmrelay.toml.example"
STATUS_LABEL_WIDTH  = 12


def apply_config_env(args: argparse.Namespace) -> None:
    """Publish --config as $LMRELAY_CONFIG.

    uvicorn imports app.py itself, and app.py loads the config again from
    scratch, so the choice has to travel by environment, not by argument.
    """
    if getattr(args, "config", None):
        os.environ[CONFIG_ENV_VAR] = str(Path(args.config).expanduser())


def config_path_from(args: argparse.Namespace) -> Path:
    """Locate the config this command applies to, whether or not it exists yet.

    Falling back to the home path rather than refusing lets `token gen` run
    before `init`: the state lands where the config will.
    """
    apply_config_env(args)
    return find_config_path() or HOME_CONFIG_PATH


def run_service_command(argv: list[str]) -> None:
    """Run a service-manager command, turning a non-zero exit into an operator message."""
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise LmrelayError(f"lmrelay: {' '.join(argv)} failed: {detail}")


def service_control(action: str, config_path: Path) -> str:
    """Hand stop/restart/reload to the service manager that owns the process.

    Signalling the pid behind the manager's back would leave the two disagreeing
    about who owns the relay, and the next Restart=on-failure would undo it.
    """
    if detect_manager() == "systemd":
        run_service_command(["systemctl", "--user", action, SYSTEMD_UNIT_NAME])
        return f"lmrelay: {action} via systemd."
    if action == "reload":
        # launchd has no reload verb, and it is the same process either way.
        reload_daemon(config_path)
        return "lmrelay: reloaded the launchd-managed process with SIGHUP."
    # No -w: that flag writes the job into launchd's disabled database, where it
    # survives a reboot. A stop is meant to be temporary, and only `lmrelay
    # disable` may decide the relay stops coming back at login.
    run_service_command(["launchctl", "unload", str(LAUNCHD_PLIST_PATH)])
    if action == "restart":
        run_service_command(["launchctl", "load", str(LAUNCHD_PLIST_PATH)])
    return f"lmrelay: {action} via launchd."


def reload_running_relay(config_path: Path) -> None:
    """Ask a running relay to pick a saved change up, or say when it will."""
    if reload_daemon(config_path):
        # Not "the change is live": SIGHUP is delivered, not acknowledged, and a
        # relay that cannot parse what it re-reads keeps the config it had. The
        # reason is in its log, so the claim made here stops at what was done.
        logger.info("Signalled the running relay to re-read it (SIGHUP).")
    else:
        logger.info("No relay is running; the change applies at the next start.")


def parse_headers(pairs: list[str] | None) -> dict[str, str]:
    """Turn repeated --header NAME=VALUE into a dict, splitting on the first = only."""
    headers = {}
    for pair in pairs or []:
        name, separator, value = pair.partition("=")
        if not separator or not name.strip():
            raise LmrelayError(f"lmrelay: --header expects NAME=VALUE, got '{pair}'")
        headers[name.strip()] = value
    return headers


def log_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    """Log a table whose columns are as wide as their widest cell."""
    widths = [max(len(cell) for cell in column) for column in zip(header, *rows, strict=True)]
    for row in (header, *rows):
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=True)]
        logger.info("  ".join(cells).rstrip())


def describe_autostart(status: dict) -> str:
    """Render autostart_status() as the one line the status block shows."""
    if status["manager"] == "none":
        return "unavailable on this platform"
    if not status["installed"]:
        return f"{status['manager']}: not installed"
    enabled = "enabled" if status["enabled"] else "disabled"
    active = "active" if status["active"] else "inactive"
    return f"{status['manager']}: {enabled}, {active}"


def save_new_token(state: RelayState, record: CallerToken) -> None:
    """Persist a freshly added token, and say so if it is not yet being required.

    Minting a credential and requiring one are two decisions, and this command
    only makes the first: an operator who adds a token to a relay that is still
    serving other traffic has not asked for that traffic to start failing. The
    reminder is here because the alternative — saying nothing — leaves them
    believing the relay is closed when it is not.
    """
    save_state(state)
    label = f" ({record.label})" if record.label else ""
    logger.info(f"Token {record.id}{label} added to {state.state_path}.")
    if not state.auth_enabled:
        logger.info("Auth is off, so this token is not required yet. Run 'lmrelay auth true'.")


def init_config(unused_args: argparse.Namespace) -> None:
    """Write the bundled example config to ~/.lmrelay/lmrelay.toml."""
    if HOME_CONFIG_PATH.exists():
        raise ConfigError(f"lmrelay: {HOME_CONFIG_PATH} already exists; not overwriting")
    example = resources.files("lmrelay").joinpath(EXAMPLE_CONFIG_NAME).read_text(encoding="utf-8")
    HOME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    HOME_CONFIG_PATH.write_text(example, encoding="utf-8")
    # The file is meant to hold provider keys.
    HOME_CONFIG_PATH.chmod(0o600)
    logger.info(f"Wrote {HOME_CONFIG_PATH}. Edit it, then run 'lmrelay run'.")


def run_relay(args: argparse.Namespace) -> None:
    """Run the relay in the foreground under uvicorn."""
    apply_config_env(args)
    if service_is_active():
        raise LmrelayError(
            f"lmrelay: the {detect_manager()} unit is active and already owns the port; "
            f"run 'lmrelay stop' first, or 'lmrelay restart'"
        )

    # Loaded here as well so a broken config fails at the command line with a
    # readable message instead of inside a uvicorn worker.
    config = load_config()
    setup_logging(config.log_level)

    if args.host:
        # The app checks the configured host at startup, so an overridden one
        # would otherwise be exposed without the warning.
        exposure_warning = check_exposure(replace(config, host=args.host))
        if exposure_warning:
            logger.warning(exposure_warning)

    publish_bind(args.host or config.host, args.port or config.port)
    uvicorn.run(
        "lmrelay.app:app",
        host=args.host or config.host,
        port=args.port or config.port,
        log_config=None,     # uvicorn must not override our logging
        access_log=False,    # the middleware writes the request line itself
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


def serve_relay(args: argparse.Namespace) -> None:
    """Start the relay detached and report where it went."""
    apply_config_env(args)
    # Loaded before the fork so a broken config fails at the terminal rather
    # than inside a detached process nobody is watching.
    config = load_config()
    setup_logging(config.log_level)

    pid = start_detached(config.config_path, args.host, args.port)
    host = args.host or config.host
    port = args.port or config.port
    logger.info(f"lmrelay started (pid {pid}) on {host}:{port}.")
    logger.info(f"Log: {log_file(config.config_path)}")


def stop_relay(args: argparse.Namespace) -> None:
    """Stop the relay, through the service manager when one owns it."""
    config_path = config_path_from(args)
    if service_is_active():
        logger.info(service_control("stop", config_path))
        return
    if stop_daemon(config_path):
        logger.info("lmrelay: stopped.")
    else:
        logger.info("lmrelay: not running.")


def restart_relay(args: argparse.Namespace) -> None:
    """Stop whatever is running and start it again, detached."""
    config_path = config_path_from(args)
    if service_is_active():
        logger.info(service_control("restart", config_path))
        return
    stop_daemon(config_path)
    serve_relay(args)


def reload_relay(args: argparse.Namespace) -> None:
    """Re-read the config in the running relay without dropping connections."""
    config_path = config_path_from(args)
    if service_is_active():
        logger.info(service_control("reload", config_path))
        return
    if reload_daemon(config_path):
        logger.info("lmrelay: reloaded (SIGHUP).")
    else:
        logger.info("lmrelay: not running; nothing to reload.")


def show_status(args: argparse.Namespace) -> None:
    """Report what the relay is doing, or would do if it were started."""
    apply_config_env(args)
    setup_logging(plain=True)
    config = load_config()
    info = daemon_status(config)

    if info["running"]:
        health = "healthy" if info["healthy"] else "not responding"
        process = f"running (pid {info['pid']}), {health}"
    else:
        # The rest is still shown: "what would it do if I started it" is the
        # question a stopped relay raises.
        process = "stopped"
    count = info["token_count"]
    tokens = "1 token" if count == 1 else f"{count} tokens"
    rows = [
        ("lmrelay", process),
        ("listening", f"{info['host']}:{info['port']}"),
        ("config", str(info["config_path"])),
        ("state", str(info["state_path"])),
        ("upstreams", f"{info['upstreams']} (default: {info['default_upstream']})"),
        ("auth", f"{'on' if info['auth_enabled'] else 'off'}, {tokens}"),
        ("autostart", describe_autostart(autostart_status())),
    ]
    for label, value in rows:
        logger.info(f"{label:<{STATUS_LABEL_WIDTH}} {value}")


def enable_service(args: argparse.Namespace) -> None:
    """Register the relay with the platform's service manager and start it."""
    apply_config_env(args)
    config = load_config()
    # A unit runs from an unknown working directory, so the path it names has to
    # be absolute even when the operator typed a relative one.
    logger.info(enable_autostart(config.config_path.resolve()))


def disable_service(unused_args: argparse.Namespace) -> None:
    """Unregister the relay from the platform's service manager and stop it."""
    logger.info(disable_autostart())


def acceptable_token_count(config_path: Path, state: RelayState) -> int:
    """How many credentials a caller could actually present.

    Not just the ones the CLI stored: `[auth] token` and $LMRELAY_TOKEN are
    equally valid, and they are how a container install gets one. Counting only
    the state would refuse to turn auth on in exactly the setup load_config
    warns about, telling the operator to run the command that just refused.
    """
    try:
        return len(load_config(config_path).auth_tokens)
    except LmrelayError:
        # A config that will not load cannot contribute a token; the state can.
        return len(state.tokens)


def set_auth(args: argparse.Namespace) -> None:
    """Turn caller authentication on or off."""
    config_path = config_path_from(args)
    state = load_state(state_path_for(config_path))
    enabled = args.value == "true"
    if enabled and not acceptable_token_count(config_path, state):
        # Auth on with an empty token set 401s every request, the operator's own
        # included.
        raise LmrelayError("lmrelay: no tokens configured; run 'lmrelay token gen' first")
    save_state(set_auth_enabled(state, enabled))
    logger.info(f"Auth is now {'on' if enabled else 'off'}.")
    reload_running_relay(config_path)


def token_gen(args: argparse.Namespace) -> None:
    """Generate a caller token, save it and show it once."""
    config_path = config_path_from(args)
    state = load_state(state_path_for(config_path))
    state, record = add_token(state, generate_token(), args.label or "")
    save_new_token(state, record)
    logger.info(f"Token: {record.token}")
    logger.info("Copy it now: it will not be shown again.")
    reload_running_relay(config_path)


def token_add(args: argparse.Namespace) -> None:
    """Store a caller token the operator brought along."""
    config_path = config_path_from(args)
    state = load_state(state_path_for(config_path))
    state, record = add_token(state, args.token, args.label or "")
    save_new_token(state, record)
    reload_running_relay(config_path)


def token_list(args: argparse.Namespace) -> None:
    """List the stored caller tokens, masked unless --show."""
    config_path = config_path_from(args)
    state = load_state(state_path_for(config_path))
    setup_logging(plain=True)
    if not state.tokens:
        logger.info("no tokens; run 'lmrelay token gen'")
        return
    rows = [
        (
            str(token.id),
            token.token if args.show else mask_token(token.token),
            token.label or "-",
            token.created_at,
        )
        for token in state.tokens
    ]
    log_table(("ID", "TOKEN", "LABEL", "CREATED"), rows)


def token_delete(args: argparse.Namespace) -> None:
    """Delete a caller token by the id that `token list` prints."""
    config_path = config_path_from(args)
    state = load_state(state_path_for(config_path))
    remaining = delete_token(state, args.id)
    removed = next(token for token in state.tokens if token.id == args.id)
    save_state(remaining)
    label = f" ({removed.label})" if removed.label else ""
    logger.info(f"Deleted token {removed.id}{label} {mask_token(removed.token)}.")
    if remaining.auth_enabled and not remaining.tokens:
        logger.warning(
            "That was the last token and auth is on, so every request will now be "
            "refused. Add a token, or run 'lmrelay auth false'."
        )
    reload_running_relay(config_path)


def provider_add(args: argparse.Namespace) -> None:
    """Add or replace an upstream provider and its key."""
    config_path = config_path_from(args)
    state = load_state(state_path_for(config_path))
    state = add_provider(
        state,
        args.name,
        args.token,
        base_url=args.base_url,
        dialect=args.dialect,
        extra_headers=parse_headers(args.header),
    )
    save_state(state)
    provider = state.providers[args.name]
    # Reported because a preset chose the base_url and the dialect, and an
    # operator who never typed them deserves to see what they got.
    logger.info(
        f"Provider '{args.name}' -> {provider['base_url']} ({provider['dialect']} dialect)."
    )
    reload_running_relay(config_path)


def provider_list(args: argparse.Namespace) -> None:
    """List every upstream in effect, hand-written and CLI-added alike."""
    apply_config_env(args)
    setup_logging(plain=True)
    config = load_config()
    state = load_state(config.state_path)
    rows = []
    for name in sorted(config.upstreams):
        upstream = config.upstreams[name]
        headers = ", ".join(
            f"{key}={value if args.show else mask_token(value)}"
            for key, value in upstream.headers.items()
        )
        source = "state" if name in state.providers else "config"
        rows.append((name, upstream.base_url, upstream.dialect, source, headers or "-"))
    log_table(("NAME", "BASE_URL", "DIALECT", "SOURCE", "HEADERS"), rows)


def provider_delete(args: argparse.Namespace) -> None:
    """Remove a CLI-added provider."""
    config_path = config_path_from(args)
    state = load_state(state_path_for(config_path))
    if args.name not in state.providers:
        # `provider list` may well show the name, because lmrelay.toml defines
        # it; deleting nothing and saying so would read as a delete that worked.
        raise LmrelayError(
            f"lmrelay: provider '{args.name}' was not added by the CLI; if it is defined "
            f"in {config_path}, remove its [upstream.{args.name}] section by hand"
        )
    save_state(delete_provider(state, args.name))
    logger.info(f"Provider '{args.name}' deleted.")
    reload_running_relay(config_path)


def add_config_option(parser: argparse.ArgumentParser) -> None:
    """Attach --config, which every command that reads config or state accepts."""
    parser.add_argument("--config", default=None, help="path to lmrelay.toml")


def add_bind_options(parser: argparse.ArgumentParser) -> None:
    """Attach the two [server] overrides that only apply at start."""
    parser.add_argument("--host", default=None, help="override [server].host")
    parser.add_argument("--port", type=int, default=None, help="override [server].port")


def add_token_commands(subparsers: argparse._SubParsersAction) -> None:
    """Attach `lmrelay token <verb>`."""
    token_parser = subparsers.add_parser("token", help="manage caller tokens")
    # dest + required so a bare `lmrelay token` prints help and exits non-zero.
    token_subparsers = token_parser.add_subparsers(dest="token_command", required=True)

    gen_parser = token_subparsers.add_parser("gen", help="generate and store a caller token")
    gen_parser.add_argument("--label", default=None, help="what the token is for")
    add_config_option(gen_parser)
    gen_parser.set_defaults(handler=token_gen)

    add_parser = token_subparsers.add_parser("add", help="store an existing caller token")
    add_parser.add_argument("token", help="the token to accept")
    add_parser.add_argument("--label", default=None, help="what the token is for")
    add_config_option(add_parser)
    add_parser.set_defaults(handler=token_add)

    list_parser = token_subparsers.add_parser("list", help="list the stored caller tokens")
    list_parser.add_argument("--show", action="store_true", help="print tokens unmasked")
    add_config_option(list_parser)
    list_parser.set_defaults(handler=token_list)

    delete_parser = token_subparsers.add_parser("delete", help="delete a caller token by id")
    delete_parser.add_argument("id", type=int, help="id from 'lmrelay token list'")
    add_config_option(delete_parser)
    delete_parser.set_defaults(handler=token_delete)


def add_provider_commands(subparsers: argparse._SubParsersAction) -> None:
    """Attach `lmrelay provider <verb>`."""
    provider_parser = subparsers.add_parser("provider", help="manage upstream providers")
    provider_subparsers = provider_parser.add_subparsers(dest="provider_command", required=True)

    add_parser = provider_subparsers.add_parser("add", help="add or replace a provider")
    add_parser.add_argument("name", help="upstream name, e.g. openai")
    add_parser.add_argument("token", help="the provider API key")
    add_parser.add_argument("--base-url", default=None, help="required when there is no preset")
    add_parser.add_argument("--dialect", default=None, choices=DIALECTS, help="request dialect")
    add_parser.add_argument(
        "--header", action="append", default=None, metavar="NAME=VALUE",
        help="extra header, repeatable",
    )
    add_config_option(add_parser)
    add_parser.set_defaults(handler=provider_add)

    list_parser = provider_subparsers.add_parser("list", help="list every upstream in effect")
    list_parser.add_argument("--show", action="store_true", help="print header values unmasked")
    add_config_option(list_parser)
    list_parser.set_defaults(handler=provider_list)

    delete_parser = provider_subparsers.add_parser("delete", help="remove a CLI-added provider")
    delete_parser.add_argument("name", help="upstream name")
    add_config_option(delete_parser)
    delete_parser.set_defaults(handler=provider_delete)


def build_parser() -> argparse.ArgumentParser:
    """Assemble the whole command surface."""
    parser = argparse.ArgumentParser(
        prog="lmrelay",
        description="Credentialed relay beside a local Ollama, retargetable at a hosted provider.",
    )
    parser.add_argument("--version", action="version", version=f"lmrelay {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help=f"write {HOME_CONFIG_PATH}")
    init_parser.set_defaults(handler=init_config)

    run_parser = subparsers.add_parser("run", help="run the relay in the foreground")
    add_bind_options(run_parser)
    add_config_option(run_parser)
    run_parser.set_defaults(handler=run_relay)

    serve_parser = subparsers.add_parser("serve", help="run the relay detached")
    add_bind_options(serve_parser)
    add_config_option(serve_parser)
    serve_parser.set_defaults(handler=serve_relay)

    stop_parser = subparsers.add_parser("stop", help="stop the running relay")
    add_config_option(stop_parser)
    stop_parser.set_defaults(handler=stop_relay)

    restart_parser = subparsers.add_parser("restart", help="stop and start again, detached")
    add_bind_options(restart_parser)
    add_config_option(restart_parser)
    restart_parser.set_defaults(handler=restart_relay)

    reload_parser = subparsers.add_parser("reload", help="re-read the config in place")
    add_config_option(reload_parser)
    reload_parser.set_defaults(handler=reload_relay)

    status_parser = subparsers.add_parser("status", help="report the relay's state")
    add_config_option(status_parser)
    status_parser.set_defaults(handler=show_status)

    enable_parser = subparsers.add_parser("enable", help="start the relay at login")
    add_config_option(enable_parser)
    enable_parser.set_defaults(handler=enable_service)

    disable_parser = subparsers.add_parser("disable", help="stop starting the relay at login")
    disable_parser.set_defaults(handler=disable_service)

    auth_parser = subparsers.add_parser("auth", help="turn caller authentication on or off")
    auth_parser.add_argument("value", choices=["true", "false"])
    add_config_option(auth_parser)
    auth_parser.set_defaults(handler=set_auth)

    add_token_commands(subparsers)
    add_provider_commands(subparsers)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    setup_logging(DEFAULT_LOG_LEVEL)
    try:
        args.handler(args)
    except LmrelayError as exc:
        # The message is already written for the operator; a traceback would
        # only bury it.
        logger.error(str(exc))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
