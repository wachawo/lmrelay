#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Detached process control: pidfile, signals and status."""

import logging
import os
import signal
import time
import traceback
from contextlib import suppress
from pathlib import Path
from typing import NoReturn

import httpx
import uvicorn

# Local imports
from lmrelay.config import CONFIG_ENV_VAR, RelayConfig, describe_upstreams, load_config
from lmrelay.errors import LmrelayError

logger = logging.getLogger(__name__)

PID_NAME      = "lmrelay.pid"
LOG_NAME      = "lmrelay.log"
BIND_ENV_VAR  = "LMRELAY_BIND"
STOP_TIMEOUT  = 10.0    # seconds to wait for SIGTERM before SIGKILL
KILL_TIMEOUT  = 5.0     # seconds to wait for the kernel to finish a SIGKILL
START_TIMEOUT = 10.0    # seconds to wait for a detached child to appear
POLL_INTERVAL = 0.1
# Allowed lag between pidfile mtime and process start: /proc btime is whole seconds.
PID_CLOCK_SLACK = 2.0

# [server] keys a reload cannot apply: the socket is bound and the client open.
STARTUP_KEYS = ("host", "port", "connect_timeout")


def pid_file(config_path: Path) -> Path:
    """Return the path where the running relay records its pid."""
    return config_path.parent / PID_NAME


def log_file(config_path: Path) -> Path:
    """Return the path a detached relay sends its stdout and stderr to."""
    return config_path.parent / LOG_NAME


def publish_bind(host: str, port: int) -> None:
    """Publish the bound address as $LMRELAY_BIND: --host/--port bypass the config."""
    os.environ[BIND_ENV_VAR] = f"{host}:{port}"


def serve_app(host: str, port: int) -> None:
    """Run the relay under uvicorn: the one invocation `run` and `serve` share.

    forwarded_allow_ips stays at uvicorn's loopback-only default, so callers
    cannot spoof the address rate limits and fail2ban act on. A remote proxy is
    named in $FORWARDED_ALLOW_IPS.
    """
    publish_bind(host, port)
    uvicorn.run(
        "lmrelay.app:app",
        host=host,
        port=port,
        log_config=None,     # uvicorn must not override our logging
        access_log=False,    # the middleware writes the request line itself
        proxy_headers=True,
    )


def recorded_bind(config: RelayConfig) -> str:
    """Return the address to record in the pidfile: the published one, else the config."""
    return os.getenv(BIND_ENV_VAR) or f"{config.host}:{config.port}"


def process_alive(pid: int) -> bool:
    """Report whether a process with this pid exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        # A corrupt pidfile must not crash the `stop` meant to clear it.
        return False
    except PermissionError:
        # The process exists but belongs to another user: still alive.
        return True
    return True


def process_started_at(pid: int) -> float | None:
    """Return a process's start time as Unix time, or None if /proc cannot say."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        boot_line = next(
            line for line in Path("/proc/stat").read_text(encoding="ascii").splitlines()
            if line.startswith("btime ")
        )
        # The command name may hold spaces, so count fields after its closing
        # parenthesis: starttime is the 22nd field overall.
        start_ticks = int(stat.rpartition(")")[2].split()[19])
        return int(boot_line.split()[1]) + start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def read_pid(path: Path) -> int | None:
    """Return the pid of a live relay, or None, so a stale file acts like no file.

    Pids are reused across reboots, so a process that started after the pidfile
    was written cannot be the relay that wrote it.
    """
    try:
        pid = int(path.read_text(encoding="utf-8").splitlines()[0])
        written = path.stat().st_mtime
    except (OSError, ValueError, IndexError):
        return None
    if not process_alive(pid):
        return None
    started = process_started_at(pid)
    if started is not None and started > written + PID_CLOCK_SLACK:
        return None
    return pid


def read_bind(path: Path) -> tuple[str, int] | None:
    """Return the address the running relay recorded, or None if it recorded none."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    # rpartition: in an IPv6 literal only the last colon separates the port.
    host, unused_separator, port = lines[1].rpartition(":")
    try:
        return host, int(port)
    except ValueError:
        return None


def read_startup_settings(path: Path) -> dict:
    """Return the startup settings the pidfile recorded; absent keys are not guessed."""
    bind = read_bind(path)
    if bind is None:
        return {}
    started = {"host": bind[0], "port": bind[1]}
    with suppress(OSError, ValueError, IndexError):
        started["connect_timeout"] = int(path.read_text(encoding="utf-8").splitlines()[2])
    return started


def unapplied_settings(started: dict, config: RelayConfig) -> list[str]:
    """Return `port 11435 -> 8080` for each startup-only key the config has moved."""
    return [
        f"{name} {started[name]} -> {getattr(config, name)}"
        for name in STARTUP_KEYS
        if name in started and started[name] != getattr(config, name)
    ]


def restart_warning(unapplied: list[str], config_path: Path) -> str:
    """Return the restart warning shared by the relay's log and the CLI."""
    return (
        f"lmrelay: {', '.join(unapplied)} in {config_path} but a reload cannot apply "
        f"that: the socket is already bound and the client already open; restart to apply"
    )


def write_pid(path: Path, pid: int, bind: str = "", connect_timeout: int | None = None) -> None:
    """Write the pidfile atomically, so no reader ever sees half a number.

    Lines: pid, bind address, connect_timeout, the settings a reload cannot
    apply. connect_timeout is written only after a bind, so line numbers hold.
    """
    lines = [str(pid)]
    if bind:
        lines.append(bind)
        if connect_timeout is not None:
            lines.append(str(connect_timeout))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f"{path.name}.{pid}.tmp"
    tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def remove_pid(path: Path) -> None:
    """Delete the pidfile if it is there."""
    path.unlink(missing_ok=True)


def wait_for_exit(pid: int, timeout: float) -> bool:
    """Poll until the process is gone; False if it is still there at the deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(POLL_INTERVAL)
    return not process_alive(pid)


def read_child_pid(read_fd: int, log_path: Path) -> int:
    """Read the pid the detached grandchild reports through the startup pipe."""
    with os.fdopen(read_fd, encoding="utf-8") as pipe:
        reported = pipe.read().strip()
    if not reported.isdigit():
        raise LmrelayError(f"lmrelay: the relay did not start; see {log_path} for why")
    return int(reported)


def wait_for_relay(pid: int, path: Path, log_path: Path) -> int:
    """Wait for the process just started to record itself, or fail naming its log.

    Waiting for this exact pid keeps two racing `serve` commands from both
    claiming the one relay that survived; the fork alone proves nothing.
    """
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if read_pid(path) == pid:
            return pid
        if not process_alive(pid):
            raise LmrelayError(
                f"lmrelay: the relay exited during startup; see {log_path} for why"
            )
        time.sleep(POLL_INTERVAL)
    raise LmrelayError(
        f"lmrelay: no relay appeared within {START_TIMEOUT:.0f}s; see {log_path} for why"
    )


def redirect_standard_streams(log_path: Path) -> None:
    """Point stdin at /dev/null and stdout/stderr at the log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    devnull_fd = os.open(os.devnull, os.O_RDONLY)
    # 0600: the log carries request lines and whatever an upstream said back.
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(devnull_fd, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(devnull_fd)
    os.close(log_fd)


def run_detached_server(config_path: Path, host: str | None, port: int | None) -> NoReturn:
    """Serve under uvicorn exactly as `lmrelay run` does, then leave the process."""
    # The app loads exactly the file the CLI validated.
    os.environ[CONFIG_ENV_VAR] = str(config_path)
    exit_code = 0
    try:
        config = load_config(config_path)
        serve_app(host or config.host, port or config.port)
    except Exception as exc:
        logger.error(f"{type(exc).__name__}: {str(exc)}\n{traceback.format_exc()}")
        exit_code = 1
    # os._exit, not sys.exit: unwinding would rerun the CLI's error handling and
    # exit hooks inherited by this forked copy.
    os._exit(exit_code)


def start_detached(config_path: Path, host: str | None, port: int | None) -> int:
    """Start the relay in the background and return the pid of the process started."""
    if not hasattr(os, "fork"):
        raise LmrelayError(
            "lmrelay: this platform has no os.fork; run 'lmrelay run' in the foreground "
            "or start the relay from a service manager"
        )

    path = pid_file(config_path)
    running = read_pid(path)
    if running is not None:
        raise LmrelayError(
            f"lmrelay: already running (pid {running}); use 'lmrelay restart' or 'lmrelay stop'"
        )

    # Only the grandchild knows its pid after the double fork, so it reports it
    # through a pipe; the pidfile might name another relay.
    read_fd, write_fd = os.pipe()
    middle_pid = os.fork()
    if middle_pid > 0:
        os.close(write_fd)
        # Reap the middle process so it does not linger as a zombie.
        os.waitpid(middle_pid, 0)
        log_path = log_file(config_path)
        return wait_for_relay(read_child_pid(read_fd, log_path), path, log_path)

    os.close(read_fd)
    # setsid leaves the terminal's session; the second fork drops session
    # leadership, so no controlling terminal can be acquired again.
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.write(write_fd, f"{os.getpid()}\n".encode())
    os.close(write_fd)
    # No chdir to "/": a relative --config must keep its meaning.
    redirect_standard_streams(log_file(config_path))
    # The app's lifespan writes the pidfile, so it has one writer however started.
    run_detached_server(config_path, host, port)


def stop_daemon(config_path: Path, timeout: float = STOP_TIMEOUT) -> bool:
    """Stop the running relay; False when there was nothing to stop."""
    path = pid_file(config_path)
    pid = read_pid(path)
    if pid is None:
        remove_pid(path)
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Already gone.
        pass
    except PermissionError:
        # Another user's process: keep the pidfile rather than hide it.
        raise LmrelayError(f"lmrelay: pid {pid} belongs to another user; stop it as its owner")
    else:
        if not wait_for_exit(pid, timeout):
            logger.warning(
                f"lmrelay: pid {pid} ignored SIGTERM for {timeout:.0f}s; forcing it with SIGKILL"
            )
            with suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
            # SIGKILL is not instant, and `restart` binds the same port next.
            if not wait_for_exit(pid, KILL_TIMEOUT):
                logger.warning(f"lmrelay: pid {pid} is still there after SIGKILL")
    # Also removed here: a killed process cannot clean up after itself.
    remove_pid(path)
    return True


def reload_daemon(config_path: Path) -> bool:
    """Ask the running relay to re-read its config; False when nothing is running."""
    pid = read_pid(pid_file(config_path))
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGHUP)
    except ProcessLookupError:
        # Already gone.
        return False
    except PermissionError:
        raise LmrelayError(f"lmrelay: pid {pid} belongs to another user; reload it as its owner")
    return True


def probe_address(host: str) -> str:
    """Return the URL host part that reaches a listener bound to `host`.

    A wildcard bind is probed on loopback; an IPv6 literal is bracketed.
    """
    if host in ("", "0.0.0.0"):
        return "127.0.0.1"
    if host == "::":
        return "[::1]"
    return f"[{host}]" if ":" in host else host


def probe_health(host: str, port: int, timeout: float = 2.0) -> bool:
    """Report whether the relay answers /healthz on the address it was told to bind."""
    try:
        response = httpx.get(f"http://{probe_address(host)}:{port}/healthz", timeout=timeout)
    except Exception:
        return False
    return response.status_code == 200


def daemon_status(config: RelayConfig) -> dict:
    """Gather everything `lmrelay status` reports in one pass."""
    pidfile = pid_file(config.config_path)
    pid = read_pid(pidfile)
    running = pid is not None
    # Prefer the recorded bind over the config: a relay started with --port
    # listens elsewhere.
    host, port = (running and read_bind(pidfile)) or (config.host, config.port)
    return {
        "running":          running,
        "pid":              pid,
        "healthy":          running and probe_health(host, port),
        "host":             host,
        "port":             port,
        "config_path":      config.config_path,
        "state_path":       config.state_path,
        "upstreams":        describe_upstreams(config),
        "default_upstream": config.default_upstream,
        "auth_enabled":     config.auth_enabled,
        "token_count":      len(config.auth_tokens),
    }


def main():
    pass


if __name__ == "__main__":
    main()
