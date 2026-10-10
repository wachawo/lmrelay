#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Command line interface: process control, tokens and providers."""

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from importlib import resources
from pathlib import Path

# Local imports
from lmrelay import __version__
from lmrelay.bundle import (
    STDIO_PATH,
    build_bundle,
    bundle_state,
    count_secrets,
    describe_source,
    parse_bundle,
    read_bundle,
    render_config,
    write_bundle,
)
from lmrelay.config import (
    CONFIG_ENV_VAR,
    DEFAULT_LOG_LEVEL,
    HOME_CONFIG_PATH,
    SCOPES,
    ConfigError,
    RelayConfig,
    check_exposure,
    find_config_path,
    load_config,
    read_int,
    read_rate,
    set_scope_limits,
)
from lmrelay.daemon import (
    daemon_status,
    log_file,
    pid_file,
    read_startup_settings,
    reload_daemon,
    restart_warning,
    serve_app,
    start_detached,
    stop_daemon,
    unapplied_settings,
)
from lmrelay.errors import BundleError, LmrelayError
from lmrelay.logging_setup import setup_logging
from lmrelay.ratelimit import ScopeLimits, describe_limits, describe_scope
from lmrelay.service import (
    LAUNCHD_PLIST_PATH,
    SYSTEMD_UNIT_NAME,
    autostart_status,
    detect_manager,
    disable_autostart,
    enable_autostart,
    install_system_unit,
    linger_warning,
    remove_system_unit,
    run_service_command,
    running_as_root,
    service_is_active,
    system_install_commands,
    system_removal_commands,
    system_unit_for,
    systemctl,
    systemd_scope,
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
    write_private,
)

logger = logging.getLogger(__name__)

EXAMPLE_CONFIG_NAME = "lmrelay.toml.example"
STATUS_LABEL_WIDTH  = 12
BACKUP_SUFFIX       = ".bak"


def apply_config_env(args: argparse.Namespace) -> None:
    """Publish --config as $LMRELAY_CONFIG.

    uvicorn re-imports app.py, which loads the config from scratch, so the
    choice has to travel by environment.
    """
    if getattr(args, "config", None):
        os.environ[CONFIG_ENV_VAR] = str(Path(args.config).expanduser())


def config_path_from(args: argparse.Namespace) -> Path:
    """Locate the config this command applies to, whether or not it exists yet.

    The home-path fallback lets `token gen` run before `init`.
    """
    apply_config_env(args)
    return find_config_path() or HOME_CONFIG_PATH


def service_control(action: str, config_path: Path) -> str:
    """Hand stop/restart/reload to the service manager that owns the process.

    Signalling the pid behind the manager's back would be undone by the next
    Restart=on-failure.
    """
    if detect_manager() == "systemd":
        scope = systemd_scope()
        if action == "reload" and scope == "system":
            # The process runs as this user, so signal it directly; `systemctl
            # reload` would need root for the same SIGHUP.
            reload_daemon(config_path)
            return "lmrelay: signalled the systemd-managed process to re-read its config (SIGHUP)."
        run_service_command([*systemctl(scope), action, SYSTEMD_UNIT_NAME])
        return f"lmrelay: {action} via systemd ({scope} unit)."
    if action == "reload":
        # launchd has no reload verb.
        reload_daemon(config_path)
        return "lmrelay: signalled the launchd-managed process to re-read its config (SIGHUP)."
    # No -w: it marks the job disabled across reboots. A stop is temporary;
    # only `lmrelay disable` turns autostart off.
    run_service_command(["launchctl", "unload", str(LAUNCHD_PLIST_PATH)])
    if action == "restart":
        run_service_command(["launchctl", "load", str(LAUNCHD_PLIST_PATH)])
    return f"lmrelay: {action} via launchd."


def reload_running_relay(config_path: Path) -> None:
    """Ask a running relay to pick a saved change up, or say when it will."""
    if reload_daemon(config_path):
        # "Signalled", not "live": SIGHUP is not acknowledged, and a relay that
        # cannot parse the new config keeps the old one and says why in its log.
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


def log_table(header: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> None:
    """Log a table whose columns are as wide as their widest cell."""
    widths = [max(len(cell) for cell in column) for column in zip(header, *rows, strict=True)]
    for row in (header, *rows):
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=True)]
        logger.info("  ".join(cells).rstrip())


def describe_autostart(status: dict) -> str:
    """Render autostart_status() as the one line the status block shows."""
    if status["manager"] == "none":
        return "unavailable on this platform"
    manager = status["manager"]
    if "scope" in status:
        manager = f"{manager} {status['scope']} unit"
    if not status["installed"]:
        return f"{manager}: not installed"
    enabled = "enabled" if status["enabled"] else "disabled"
    active = "active" if status["active"] else "inactive"
    return f"{manager}: {enabled}, {active}"


def save_new_token(state: RelayState, record: CallerToken) -> None:
    """Persist a freshly added token, and say so if tokens are not yet required.

    Minting a token and requiring tokens are separate decisions: adding one must
    not start refusing traffic already flowing, so the operator gets a reminder.
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
    # 0600 from creation: the file is meant to hold provider keys.
    write_private(HOME_CONFIG_PATH, example)
    logger.info(f"Wrote {HOME_CONFIG_PATH}. Edit it, then run 'lmrelay run'.")


def run_relay(args: argparse.Namespace) -> None:
    """Run the relay in the foreground under uvicorn."""
    apply_config_env(args)
    if service_is_active():
        raise LmrelayError(
            f"lmrelay: the {detect_manager()} unit is active and already owns the port; "
            f"run 'lmrelay stop' first, or 'lmrelay restart'"
        )

    # Loaded here too so a broken config fails readably, not in a uvicorn worker.
    config = load_config()
    setup_logging(config.log_level)

    if args.host:
        # The app checks only the configured host for exposure.
        exposure_warning = check_exposure(replace(config, host=args.host))
        if exposure_warning:
            logger.warning(exposure_warning)

    serve_app(args.host or config.host, args.port or config.port)


def serve_relay(args: argparse.Namespace) -> None:
    """Start the relay detached and report where it went."""
    apply_config_env(args)
    # Loaded before the fork so a broken config fails at the terminal.
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


def warn_unapplied_settings(config_path: Path) -> None:
    """Warn in the terminal about changed settings that only a restart applies.

    The relay warns only into its log. Computed here because SIGHUP is not
    acknowledged: the pidfile holds the startup settings, the config on disk
    holds the new ones.
    """
    try:
        config = load_config()
    except LmrelayError:
        # The relay reports a config it refuses, in its own log.
        return
    started = read_startup_settings(pid_file(config_path))
    unapplied = unapplied_settings(started, config)
    if unapplied:
        logger.warning(restart_warning(unapplied, config.config_path))


def reload_relay(args: argparse.Namespace) -> None:
    """Re-read the config in the running relay without dropping connections."""
    config_path = config_path_from(args)
    if service_is_active():
        logger.info(service_control("reload", config_path))
        warn_unapplied_settings(config_path)
        return
    if reload_daemon(config_path):
        # "Signalled", not "reloaded": see reload_running_relay.
        logger.info("lmrelay: signalled the running relay to re-read its config (SIGHUP).")
        warn_unapplied_settings(config_path)
    else:
        logger.info("lmrelay: not running; nothing to reload.")


def show_status(args: argparse.Namespace) -> None:
    """Report what the relay is doing, or would do if it were started."""
    apply_config_env(args)
    setup_logging(plain=True)
    config = load_config()
    info = daemon_status(config)
    autostart = autostart_status()

    if info["running"]:
        health = "healthy" if info["healthy"] else "not responding"
        process = f"running (pid {info['pid']}), {health}"
    else:
        # The rest still shows what a start would do.
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
        ("limits", describe_limits(config.limits)),
        ("autostart", describe_autostart(autostart)),
    ]
    for label, value in rows:
        logger.info(f"{label:<{STATUS_LABEL_WIDTH}} {value}")
    warning = linger_warning() if autostart["installed"] else None
    if warning:
        logger.warning(warning)


def show_version(unused_args: argparse.Namespace) -> None:
    """Print the line `--version` prints, as a command."""
    setup_logging(plain=True)
    logger.info(f"lmrelay {__version__}")


def enable_service(args: argparse.Namespace) -> None:
    """Register the relay with the platform's service manager and start it."""
    apply_config_env(args)
    config = load_config()
    # A unit runs from an unknown working directory, so the path must be absolute.
    config_path = config.config_path.resolve()
    if args.system:
        enable_system_service(config_path)
        return
    logger.info(enable_autostart(config_path))
    warning = linger_warning()
    if warning:
        logger.warning(warning)


def enable_system_service(config_path: Path) -> None:
    """Install the system unit as root, or print it and the commands that install it.

    The unit goes to stdout and messages to stderr, so the printed command can
    pipe this same invocation into `sudo tee`.
    """
    text = system_unit_for(config_path)
    if running_as_root():
        logger.info(install_system_unit(text))
        return
    sys.stdout.write(text)
    logger.info("Installing a system unit needs root. Run:")
    for command in system_install_commands(config_path):
        logger.info(f"  {command}")


def disable_service(args: argparse.Namespace) -> None:
    """Unregister the relay from the platform's service manager and stop it."""
    if not args.system:
        logger.info(disable_autostart())
        return
    if running_as_root() or systemd_scope() != "system":
        logger.info(remove_system_unit())
        return
    logger.info("Removing a system unit needs root. Run:")
    for command in system_removal_commands():
        logger.info(f"  {command}")


def acceptable_token_count(config_path: Path, state: RelayState) -> int:
    """Count the credentials a caller could actually present.

    Includes an `[auth] token` from the config file, not only CLI-stored tokens,
    so `auth true` works on an install that never ran `token gen`.
    """
    try:
        return len(load_config(config_path).auth_tokens)
    except LmrelayError:
        # A config that will not load contributes no token; the state still can.
        return len(state.tokens)


def set_auth(args: argparse.Namespace) -> None:
    """Turn caller authentication on or off."""
    config_path = config_path_from(args)
    state = load_state(state_path_for(config_path))
    enabled = args.value == "true"
    if enabled and not acceptable_token_count(config_path, state):
        # Auth on with no tokens would 401 every request.
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
    # A preset may have chosen base_url and dialect, so show what was set.
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
        # The name may come from lmrelay.toml; a silent no-op would read as success.
        raise LmrelayError(
            f"lmrelay: provider '{args.name}' was not added by the CLI; if it is defined "
            f"in {config_path}, remove its [upstream.{args.name}] section by hand"
        )
    save_state(delete_provider(state, args.name))
    logger.info(f"Provider '{args.name}' deleted.")
    reload_running_relay(config_path)


def scope_limits_from(args: argparse.Namespace) -> ScopeLimits:
    """Build the requested scope limits, validated by the config's own readers.

        limits set total 1              one at a time
        limits set total 1/60s          one a minute, and one at a time
        limits set per_address 2 10/30m ten every half hour, two at a time

    A rate alone also sets the at-once cap to its count. The config readers,
    not argparse `type=`, keep CLI errors worded like TOML errors.
    """
    section = f"limits.{args.scope}"
    if args.rate is not None and "/" in args.concurrent:
        raise LmrelayError(
            f"lmrelay: '{args.concurrent} {args.rate}' is the wrong way round; the count at "
            f"once comes first: 'lmrelay limits set {args.scope} 2 10/30m'"
        )

    if args.rate is None and "/" in args.concurrent:
        spelled = read_rate({"rate": args.concurrent}, section, "rate")
        # read_rate has refused anything that is not `count/period`.
        return ScopeLimits(concurrent=int(spelled.partition("/")[0]), rate=spelled)

    return ScopeLimits(
        concurrent=read_int({"concurrent": args.concurrent}, section, "concurrent", 0, minimum=0),
        rate=read_rate({"rate": args.rate or ""}, section, "rate"),
    )


def warn_unreachable_cap(scope: str, limits: ScopeLimits) -> None:
    """Warn when the at-once cap exceeds what the rate can ever start together.

    The bucket holds the rate's count, so a higher cap never refuses anybody.
    Legal, but almost always a slip.
    """
    allowance = limits.allowance()
    if allowance is None or limits.concurrent <= allowance[0]:
        return
    logger.warning(
        f"lmrelay: [limits.{scope}] allows {limits.concurrent} at once but {limits.rate} "
        f"lets only {allowance[0]} start together, so the cap will never refuse anybody."
    )


def warn_unkeyed_scope(scope: str, config_path: Path, limits: ScopeLimits) -> None:
    """Warn when per_token limits are set while auth is off.

    load_config warns too, but only into the log at the next start.
    """
    if scope != "per_token" or not limits.configured():
        return
    if load_state(state_path_for(config_path)).auth_enabled:
        return
    logger.warning(
        "Auth is off, so nothing is keyed by a token and this scope refuses nobody. "
        "[limits.per_address] and [limits.total] still apply. Run 'lmrelay auth true'."
    )


def limits_set(args: argparse.Namespace) -> None:
    """Write one scope's numbers into the config file."""
    config_path = config_path_from(args)
    wanted = scope_limits_from(args)
    if not config_path.exists():
        # A new file with limits and no upstream would not start.
        raise ConfigError(
            f"lmrelay: no config at {config_path} to edit; limits live in lmrelay.toml. "
            f"Run 'lmrelay init' first."
        )

    before, after = set_scope_limits(config_path, args.scope, wanted)
    # Before and after, worded as `status` words them.
    logger.info(
        f"[limits.{args.scope}] {describe_scope(before)} -> {describe_scope(after)} "
        f"in {config_path}."
    )
    warn_unkeyed_scope(args.scope, config_path, after)
    warn_unreachable_cap(args.scope, after)
    reload_running_relay(config_path)


def plural(count: int, noun: str) -> str:
    """Return 'N noun', with an s unless N is 1."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def check_export_destination(destination: str, config: RelayConfig, force: bool) -> None:
    """Refuse a destination an export would quietly ruin.

    The relay's own config and state are always refused: a bundle written over
    state.json still loads, with no tokens and auth silently off. Any other
    existing file needs --force, as with `init` and `import`.
    """
    if destination == STDIO_PATH:
        return
    target = Path(destination).expanduser()
    for path, what in ((config.config_path, "config"), (config.state_path, "state")):
        if target.resolve() == path.resolve():
            raise BundleError(
                f"lmrelay: {target} is this relay's own {what} file, and a bundle is not "
                f"one. Export to another path; nothing has been written."
            )
    if target.exists() and not force:
        raise BundleError(
            f"lmrelay: {target} is already there, and an export would overwrite it. "
            f"Pass --force to replace it, or choose another path."
        )


def config_export(args: argparse.Namespace) -> None:
    """Write everything needed to reproduce this relay somewhere else."""
    apply_config_env(args)
    config = load_config()
    state = load_state(config.state_path)
    # No path means stdout, so `lmrelay export | ssh ...` moves a relay; messages
    # go to stderr so the pipe carries only the bundle.
    destination = args.path or STDIO_PATH
    check_export_destination(destination, config, args.force)
    keep_secrets = not args.no_secrets
    # The effective config, not the files verbatim: ${VAR} headers are exported
    # expanded, since a bundle reproduces the relay, not the machine.
    bundle = build_bundle(config, state, keep_secrets=keep_secrets)
    write_bundle(bundle, destination)

    tokens, keyed = count_secrets(bundle)
    to_terminal = destination == STDIO_PATH
    where = "standard output" if to_terminal else f"{destination} (0600)"
    logger.info(f"Wrote {where}.")
    if keep_secrets:
        # Bundles get attached to issues; on a terminal the secrets are also in
        # the scrollback, so that case is a warning.
        count = f"{plural(tokens, 'caller token')} and {plural(keyed, 'provider key')}"
        if to_terminal and (tokens or keyed):
            logger.warning(f"That was {count} in clear, on your terminal. --no-secrets masks them.")
        else:
            logger.info(f"It contains {count} in clear.")
    else:
        logger.info(
            "Token values and header values are masked, so it carries no secrets and the "
            "relay it imports will need 'lmrelay token gen' and 'lmrelay provider add'."
        )
    logger.info("It carries settings, not comments: the notes in your lmrelay.toml stay here.")


def backup_path(path: Path) -> Path:
    """Where an import moves a file it is about to replace."""
    return path.with_name(path.name + BACKUP_SUFFIX)


def plan_replacement(paths: tuple[Path, ...], force: bool) -> list[tuple[Path, Path]]:
    """Refuse to overwrite anything, or return where the existing files will move.

    An existing .bak is never overwritten: it is the file the operator would
    reach for, and timestamped copies would leave secrets lying around.
    """
    existing = [path for path in paths if path.exists()]
    if not existing:
        return []
    names = " and ".join(str(path) for path in existing)
    if not force:
        verb = "are" if len(existing) > 1 else "is"
        raise BundleError(
            f"lmrelay: {names} {verb} already there, and an import replaces the whole "
            f"configuration rather than merging into it. Pass --force to move it aside first."
        )
    moves = []
    for path in existing:
        target = backup_path(path)
        if target.exists():
            raise BundleError(
                f"lmrelay: {target} is already there, and this import would overwrite it. "
                f"Move or delete it first; nothing has been changed."
            )
        moves.append((path, target))
    return moves


def report_missing_secrets(missing: tuple[str, ...]) -> None:
    """Warn about the secrets a masked bundle did not carry.

    A mask restored as a header would be refused upstream and look like a wrong key.
    """
    if not missing:
        return
    logger.warning(f"Secrets were masked in it, so nothing was restored for: {', '.join(missing)}.")
    logger.warning("Run 'lmrelay token gen' and 'lmrelay provider add <name> <key>' to replace them.")


def config_import(args: argparse.Namespace) -> None:
    """Replace the config and the state with a bundle, backing up what was there."""
    config_path = config_path_from(args)
    state_path = state_path_for(config_path)
    origin = args.path or STDIO_PATH
    if origin == STDIO_PATH and sys.stdin.isatty():
        # Refused rather than hanging silently on an unpiped terminal.
        raise BundleError(
            "lmrelay: no bundle to read. Give a path, or pipe one in: "
            "'lmrelay import relay.toml', or 'cat relay.toml | lmrelay import'."
        )
    source = describe_source(origin)

    # Validate the bundle and plan the backups before touching anything, so a
    # rejected bundle leaves the existing files intact. The file operations
    # below cannot be atomic, so each move is announced as it happens.
    bundle = parse_bundle(read_bundle(origin), source)
    moves = plan_replacement((config_path, state_path), args.force)

    for existing, target in moves:
        existing.rename(target)
        logger.info(f"Moved {existing} to {target}.")

    state = bundle_state(bundle, state_path)
    write_private(config_path, render_config(bundle, source, state_path))
    save_state(state)

    logger.info(f"Imported {source}, written by {bundle.written_by} at {bundle.exported_at}.")
    logger.info(
        f"Wrote {config_path} and {state_path} (0600): "
        f"{plural(len(state.providers), 'upstream')}, "
        f"{plural(len(state.tokens), 'caller token')}, "
        f"auth {'on' if state.auth_enabled else 'off'}."
    )
    report_missing_secrets(bundle.missing)
    if state.auth_enabled and not state.tokens:
        logger.warning(
            "Auth is on and the bundle carried no usable token, so every request will now be "
            "refused. Run 'lmrelay token gen', or 'lmrelay auth false'."
        )
    reload_running_relay(config_path)
    # Said even when nothing is running: that relay gets started next.
    logger.info(
        "host, port and connect_timeout are read at startup only; run 'lmrelay restart' if "
        "the bundle moved any of them."
    )


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


def add_limits_commands(subparsers: argparse._SubParsersAction) -> None:
    """Attach `lmrelay limits <verb>`."""
    limits_parser = subparsers.add_parser("limits", help="set how much a caller may ask for")
    limits_subparsers = limits_parser.add_subparsers(dest="limits_command", required=True)

    set_parser = limits_subparsers.add_parser(
        "set", help="write one scope's numbers into the config file"
    )
    set_parser.add_argument("scope", choices=SCOPES, help="whose requests the number counts")
    # Strings, so the handler's config readers word errors as the TOML would.
    set_parser.add_argument(
        "concurrent", metavar="N|N/PERIOD",
        help="how many at once, or a rate on its own, which also caps at once; 0 turns it off",
    )
    set_parser.add_argument(
        "rate", nargs="?", default=None, metavar="N/PERIOD",
        help="how often: 10/30m, 2/1h, 1/60s. Left off, only the at-once cap applies",
    )
    add_config_option(set_parser)
    set_parser.set_defaults(handler=limits_set)


def add_transfer_commands(subparsers: argparse._SubParsersAction) -> None:
    """Attach `lmrelay export` and `lmrelay import`."""
    export_parser = subparsers.add_parser(
        "export", help="write everything needed to reproduce this relay"
    )
    # No path means stdout, so moving a relay is one pipe; '-' is also accepted.
    export_parser.add_argument(
        "path", nargs="?", default=None,
        help=f"file to write. Left off, or '{STDIO_PATH}', it goes to standard output",
    )
    export_parser.add_argument(
        "--no-secrets", action="store_true", help="mask token and header values"
    )
    export_parser.add_argument(
        "--force", action="store_true", help="overwrite an existing file at that path"
    )
    add_config_option(export_parser)
    export_parser.set_defaults(handler=config_export)

    import_parser = subparsers.add_parser(
        "import", help="replace the config and the state with a bundle"
    )
    import_parser.add_argument(
        "path", nargs="?", default=None,
        help=f"bundle to read. Left off, or '{STDIO_PATH}', it is read from standard input",
    )
    import_parser.add_argument(
        "--force", action="store_true", help="replace an existing config, backing it up first"
    )
    add_config_option(import_parser)
    import_parser.set_defaults(handler=config_import)


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

    version_parser = subparsers.add_parser("version", help="print the version")
    version_parser.set_defaults(handler=show_version)

    enable_parser = subparsers.add_parser("enable", help="start the relay automatically")
    enable_parser.add_argument(
        "--system", action="store_true",
        help="a system unit that starts at boot, rather than a systemd --user unit",
    )
    add_config_option(enable_parser)
    enable_parser.set_defaults(handler=enable_service)

    disable_parser = subparsers.add_parser("disable", help="undo 'lmrelay enable'")
    disable_parser.add_argument(
        "--system", action="store_true", help="remove the system unit 'enable --system' installed"
    )
    disable_parser.set_defaults(handler=disable_service)

    auth_parser = subparsers.add_parser("auth", help="turn caller authentication on or off")
    auth_parser.add_argument("value", choices=["true", "false"])
    add_config_option(auth_parser)
    auth_parser.set_defaults(handler=set_auth)

    add_token_commands(subparsers)
    add_provider_commands(subparsers)
    add_limits_commands(subparsers)
    add_transfer_commands(subparsers)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    setup_logging(DEFAULT_LOG_LEVEL)
    try:
        args.handler(args)
    except LmrelayError as exc:
        # The message is written for the operator; a traceback would bury it.
        logger.error(str(exc))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
