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


def pid_file(config_path: Path) -> Path:
    """Where the running relay records its pid."""
    return config_path.parent / PID_NAME


def log_file(config_path: Path) -> Path:
    """Where a detached relay sends its stdout and stderr."""
    return config_path.parent / LOG_NAME


def publish_bind(host: str, port: int) -> None:
    """Publish the address about to be bound as $LMRELAY_BIND.

    --host and --port reach uvicorn as arguments and never touch the config, so
    without this the relay would record the configured pair in its pidfile and
    `status`, which runs in another process where those arguments are long gone,
    would name an address nothing is listening on.
    """
    os.environ[BIND_ENV_VAR] = f"{host}:{port}"


def recorded_bind(config: RelayConfig) -> str:
    """The address to record in the pidfile: what was published, else the config."""
    return os.getenv(BIND_ENV_VAR) or f"{config.host}:{config.port}"


def process_alive(pid: int) -> bool:
    """Whether a process with this pid exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        # A number too large for a pid names no process. Raising here would make
        # a corrupt pidfile crash `status` and `stop` — including the `stop`
        # whose whole job is to clear it.
        return False
    except PermissionError:
        # Signalling is refused, which means the process is there and belongs to
        # somebody else — still a running process, and still a reason not to
        # start a second relay on the same port.
        return True
    return True


def read_pid(path: Path) -> int | None:
    """Return the pid of a live relay, or None.

    The liveness check belongs here so that one call answers "is a relay
    running" and a stale file behaves exactly like an absent one everywhere.
    """
    try:
        pid = int(path.read_text(encoding="utf-8").splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None
    return pid if process_alive(pid) else None


def read_bind(path: Path) -> tuple[str, int] | None:
    """Return the address the running relay recorded, or None if it recorded none."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    # rpartition, not split: an IPv6 literal is full of colons and only the last
    # one separates the port.
    host, unused_separator, port = lines[1].rpartition(":")
    try:
        return host, int(port)
    except ValueError:
        return None


def write_pid(path: Path, pid: int, bind: str = "") -> None:
    """Write the pidfile atomically, so no reader ever sees half a number.

    The bind address goes on a second line, because it is the only record of
    where the relay actually listens once the process that chose it has exited.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f"{path.name}.{pid}.tmp"
    tmp_path.write_text(f"{pid}\n{bind}\n" if bind else f"{pid}\n", encoding="utf-8")
    os.replace(tmp_path, path)


def remove_pid(path: Path) -> None:
    """Delete the pidfile if it is there."""
    path.unlink(missing_ok=True)


def wait_for_exit(pid: int, timeout: float) -> bool:
    """Poll until the process is gone. False if it is still there at the deadline."""
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

    Waiting for this pid specifically rather than for any pid to appear is what
    keeps two racing `serve` commands from both reporting the success of the one
    relay that survived. A successful fork proves nothing on its own: the config
    is parsed and the port bound afterwards, in the child.
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
    # Pinned so the app, which loads the config itself, reads the same file the
    # CLI validated instead of repeating the search from a different process.
    os.environ[CONFIG_ENV_VAR] = str(config_path)
    exit_code = 0
    try:
        config = load_config(config_path)
        publish_bind(host or config.host, port or config.port)
        uvicorn.run(
            "lmrelay.app:app",
            host=host or config.host,
            port=port or config.port,
            log_config=None,     # uvicorn must not override our logging
            access_log=False,    # the middleware writes the request line itself
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    except Exception as exc:
        # Nothing is watching this process, so the reason has to reach the log
        # the operator will be pointed at.
        logger.error(f"{type(exc).__name__}: {str(exc)}\n{traceback.format_exc()}")
        exit_code = 1
    # os._exit, not sys.exit: unwinding would run the CLI's own error handling
    # and exit hooks a second time, in a process that only holds a copy of them.
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

    # The double fork means the pid of the process that actually serves is known
    # only to the middle process, so the grandchild reports it back through a
    # pipe. Taking it from the pidfile instead would adopt whichever relay wrote
    # there last, and report a start that in fact failed as a success.
    read_fd, write_fd = os.pipe()
    middle_pid = os.fork()
    if middle_pid > 0:
        os.close(write_fd)
        # The middle process exits immediately; reaping it here is what keeps
        # the CLI from leaving a zombie behind when it returns to the shell.
        os.waitpid(middle_pid, 0)
        log_path = log_file(config_path)
        return wait_for_relay(read_child_pid(read_fd, log_path), path, log_path)

    os.close(read_fd)
    # setsid leaves the terminal's session, and the second fork gives up session
    # leadership, so the relay can never acquire a controlling terminal again.
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.write(write_fd, f"{os.getpid()}\n".encode())
    os.close(write_fd)
    # The working directory is deliberately not changed to "/": a relative
    # --config ./lmrelay.toml must keep meaning what the operator typed.
    redirect_standard_streams(log_file(config_path))
    # The pidfile is written by the app's lifespan, not here, so that there is
    # exactly one writer whichever way the relay was started.
    run_detached_server(config_path, host, port)


def stop_daemon(config_path: Path, timeout: float = STOP_TIMEOUT) -> bool:
    """Stop the running relay. False when there was nothing to stop."""
    path = pid_file(config_path)
    pid = read_pid(path)
    if pid is None:
        # A relay that died without unlinking its own pidfile leaves one behind,
        # and stop is the natural place to clear it.
        remove_pid(path)
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # It exited between the read and the signal, which is the outcome asked
        # for.
        pass
    except PermissionError:
        # process_alive reports another user's process as alive by design, so a
        # pid we may not signal reaches this far. Removing the pidfile now would
        # only hide it.
        raise LmrelayError(f"lmrelay: pid {pid} belongs to another user; stop it as its owner")
    else:
        if not wait_for_exit(pid, timeout):
            logger.warning(
                f"lmrelay: pid {pid} ignored SIGTERM for {timeout:.0f}s; forcing it with SIGKILL"
            )
            with suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
            # SIGKILL is not instant — the kernel still has to tear the process
            # down and release its socket, and `restart` binds the same port on
            # the very next line.
            if not wait_for_exit(pid, KILL_TIMEOUT):
                logger.warning(f"lmrelay: pid {pid} is still there after SIGKILL")
    # Removed here as well as by the relay's own shutdown, because a killed
    # process never gets to clean up after itself.
    remove_pid(path)
    return True


def reload_daemon(config_path: Path) -> bool:
    """Ask the running relay to re-read its config. False when nothing is running."""
    pid = read_pid(pid_file(config_path))
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGHUP)
    except ProcessLookupError:
        # It exited between the read and the signal: there is nothing running to
        # reload, which is exactly what False already says.
        return False
    except PermissionError:
        raise LmrelayError(f"lmrelay: pid {pid} belongs to another user; reload it as its owner")
    return True


def probe_health(host: str, port: int, timeout: float = 2.0) -> bool:
    """Whether the relay answers /healthz on the address it was told to bind."""
    # A wildcard bind is not an address to connect to; loopback is the one
    # interface a 0.0.0.0 listener is certain to be reachable on.
    target = "127.0.0.1" if host == "0.0.0.0" else host
    try:
        response = httpx.get(f"http://{target}:{port}/healthz", timeout=timeout)
    except Exception:
        # A probe asks a yes/no question: a refused connection, a timeout and an
        # unusable host all answer it the same way.
        return False
    return response.status_code == 200


def daemon_status(config: RelayConfig) -> dict:
    """Everything `lmrelay status` reports, gathered in one pass."""
    pidfile = pid_file(config.config_path)
    pid = read_pid(pidfile)
    running = pid is not None
    # What the relay recorded when it bound beats what the config says now: a
    # relay started with --port would otherwise be reported at the configured
    # port, and a healthy one called "not responding" for answering elsewhere.
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
