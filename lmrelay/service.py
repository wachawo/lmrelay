#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Autostart registration: systemd --user on Linux, launchd on macOS."""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

# Local imports
from lmrelay.daemon import log_file
from lmrelay.errors import LmrelayError

SYSTEMD_UNIT_NAME  = "lmrelay.service"
SYSTEMD_UNIT_PATH  = Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
LAUNCHD_LABEL      = "com.lmrelay.relay"
LAUNCHD_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

# Set by the unit and the agent we install, and by nothing else. Both managers
# report the service as active from the moment they exec it, so the ExecStart,
# which is `lmrelay run`, would otherwise see its own unit running and refuse
# to start, leaving autostart permanently in a restart loop.
SERVICE_ENV_VAR = "LMRELAY_SERVICE"

NO_MANAGER_MESSAGE = (
    "lmrelay: autostart needs systemd (Linux) or launchd (macOS); everywhere else "
    "'lmrelay serve' runs the relay detached on any POSIX system"
)

SYSTEMD_UNIT_TEMPLATE = """[Unit]
Description=lmrelay
After=network-online.target

[Service]
Type=simple
Environment={service_env_var}=1
ExecStart={executable} run --config {config_path}
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""

PLIST_DOCTYPE = (
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
)

LAUNCHD_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
{doctype}
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{arguments}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>{service_env_var}</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


def detect_manager() -> str:
    """Return the service manager available here: 'systemd', 'launchd' or 'none'."""
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        return "systemd"
    if sys.platform == "darwin" and shutil.which("launchctl"):
        return "launchd"
    return "none"


def relay_executable() -> str:
    """Return the command that starts the relay, always absolute.

    A unit that names a bare 'lmrelay' resolves it against the service manager's
    PATH at boot, not the operator's shell PATH. That is the classic reason autostart
    works when it is tested and not after a reboot.
    """
    installed = shutil.which("lmrelay")
    if installed:
        return os.path.abspath(installed)
    return f"{sys.executable} -m lmrelay"


def systemd_unit_text(executable: str, config_path: Path) -> str:
    """Return the body of the systemd --user unit."""
    return SYSTEMD_UNIT_TEMPLATE.format(
        executable=executable, config_path=config_path, service_env_var=SERVICE_ENV_VAR
    )


def launchd_plist_text(executable: str, config_path: Path, log_path: Path) -> str:
    """Return the body of the launchd LaunchAgent plist."""
    # ProgramArguments is a vector and launchd execs element zero verbatim, so the
    # fallback executable ("<python> -m lmrelay") has to arrive as three elements
    # rather than one unrunnable path.
    argv = [*shlex.split(executable), "run", "--config", str(config_path)]
    arguments = "\n".join(f"        <string>{escape(item)}</string>" for item in argv)
    return LAUNCHD_PLIST_TEMPLATE.format(
        doctype=PLIST_DOCTYPE,
        label=escape(LAUNCHD_LABEL),
        arguments=arguments,
        service_env_var=escape(SERVICE_ENV_VAR),
        log_path=escape(str(log_path)),
    )


def run_service_command(argv: list[str]) -> None:
    """Run a service-manager command, raising with its own words on failure.

    A 'systemctl enable' that fails quietly means the relay does not come back
    after a reboot, and nobody finds out until it matters.
    """
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    if result.returncode == 0:
        return
    detail = (result.stderr or "").strip() or (result.stdout or "").strip()
    raise LmrelayError(
        f"lmrelay: '{' '.join(argv)}' failed with exit {result.returncode}"
        + (f": {detail}" if detail else "")
    )


def probe_service_command(argv: list[str]) -> int:
    """Return the exit code of a query command. is-enabled/is-active answer by code."""
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    return result.returncode


def enable_autostart(config_path: Path) -> str:
    """Install the unit or agent for this platform and start the relay under it."""
    manager = detect_manager()
    executable = relay_executable()
    # The manager starts the relay from a working directory the operator never
    # chose, so a relative --config would resolve somewhere else entirely.
    target = config_path.expanduser().resolve()

    if manager == "systemd":
        SYSTEMD_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SYSTEMD_UNIT_PATH.write_text(systemd_unit_text(executable, target), encoding="utf-8")
        run_service_command(["systemctl", "--user", "daemon-reload"])
        run_service_command(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME])
        return f"lmrelay: autostart enabled via systemd --user ({SYSTEMD_UNIT_PATH})"

    if manager == "launchd":
        LAUNCHD_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        plist = launchd_plist_text(executable, target, log_file(target))
        LAUNCHD_PLIST_PATH.write_text(plist, encoding="utf-8")
        run_service_command(["launchctl", "load", "-w", str(LAUNCHD_PLIST_PATH)])
        return f"lmrelay: autostart enabled via launchd ({LAUNCHD_PLIST_PATH})"

    raise LmrelayError(NO_MANAGER_MESSAGE)


def disable_autostart() -> str:
    """Stop the relay under its manager and remove the unit or agent."""
    manager = detect_manager()

    if manager == "systemd":
        if not SYSTEMD_UNIT_PATH.exists():
            return f"lmrelay: no unit at {SYSTEMD_UNIT_PATH}; nothing to disable"
        # Disabled before the file goes: systemctl refuses to disable a unit whose
        # file has already vanished, which would leave it enabled and running.
        run_service_command(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME])
        SYSTEMD_UNIT_PATH.unlink(missing_ok=True)
        run_service_command(["systemctl", "--user", "daemon-reload"])
        return f"lmrelay: autostart disabled, removed {SYSTEMD_UNIT_PATH}"

    if manager == "launchd":
        if not LAUNCHD_PLIST_PATH.exists():
            return f"lmrelay: no LaunchAgent at {LAUNCHD_PLIST_PATH}; nothing to disable"
        run_service_command(["launchctl", "unload", "-w", str(LAUNCHD_PLIST_PATH)])
        LAUNCHD_PLIST_PATH.unlink(missing_ok=True)
        return f"lmrelay: autostart disabled, removed {LAUNCHD_PLIST_PATH}"

    raise LmrelayError(NO_MANAGER_MESSAGE)


def autostart_status() -> dict:
    """Report what the service manager currently knows about the relay."""
    manager = detect_manager()
    if manager == "systemd":
        enabled = probe_service_command(["systemctl", "--user", "is-enabled", SYSTEMD_UNIT_NAME])
        active = probe_service_command(["systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME])
        return {
            "manager":   manager,
            "installed": SYSTEMD_UNIT_PATH.exists(),
            "enabled":   enabled == 0,
            "active":    active == 0,
        }
    if manager == "launchd":
        # launchctl lists only loaded agents, so one code answers both questions.
        loaded = probe_service_command(["launchctl", "list", LAUNCHD_LABEL]) == 0
        return {
            "manager":   manager,
            "installed": LAUNCHD_PLIST_PATH.exists(),
            "enabled":   loaded,
            "active":    loaded,
        }
    return {"manager": manager, "installed": False, "enabled": False, "active": False}


def started_by_service_manager() -> bool:
    """Whether this process is the one the unit or the agent execed."""
    return bool(os.getenv(SERVICE_ENV_VAR))


def service_is_active() -> bool:
    """Report whether a service manager is running the relay right now.

    False inside the managed process itself: both managers report the service
    active from the instant they exec it, so its own ExecStart asking this
    question would always be told the port is taken, by itself.
    """
    if started_by_service_manager():
        return False
    return bool(autostart_status()["active"])


def main():
    pass


if __name__ == "__main__":
    main()
