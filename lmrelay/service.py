#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Autostart registration: systemd (user or system unit) on Linux, launchd on macOS."""

import getpass
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
SYSTEM_UNIT_PATH   = Path("/etc/systemd/system") / SYSTEMD_UNIT_NAME
OLLAMA_UNIT_NAME   = "ollama.service"
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

# StartLimitBurst stops a start that keeps failing, a port still held by a
# stale relay say, from restarting every two seconds for days.
SYSTEMD_UNIT_TEMPLATE = """[Unit]
Description=lmrelay
StartLimitIntervalSec=60
StartLimitBurst=5

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

# The system unit runs at boot without anybody logging in, which a user unit
# does only with linger. It can also be ordered after a system Ollama, which a
# user manager cannot see.
SYSTEM_UNIT_TEMPLATE = """[Unit]
Description=lmrelay
Wants=network-online.target
After=network-online.target{after_ollama}
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User={user}
Group={group}
Environment={service_env_var}=1
ExecStart={executable} run --config {config_path}
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
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
    """Return the command that starts this install of the relay, always absolute.

    Taken from the running interpreter rather than from PATH: with two installs
    on PATH, `which lmrelay` can name the other one. A bare 'lmrelay' in a unit
    would also resolve against the manager's PATH at boot, not the shell's.
    """
    entry_point = Path(sys.executable).parent / "lmrelay"
    if entry_point.is_file() and os.access(entry_point, os.X_OK):
        return str(entry_point)
    return f"{sys.executable} -m lmrelay"


def systemd_unit_text(executable: str, config_path: Path) -> str:
    """Return the body of the systemd --user unit."""
    return SYSTEMD_UNIT_TEMPLATE.format(
        executable=executable, config_path=config_path, service_env_var=SERVICE_ENV_VAR
    )


def system_unit_text(
    executable: str, config_path: Path, user: str, group: str, after_ollama: bool
) -> str:
    """Return the body of the system unit in /etc/systemd/system."""
    return SYSTEM_UNIT_TEMPLATE.format(
        executable=executable,
        config_path=config_path,
        user=user,
        group=group,
        after_ollama=f" {OLLAMA_UNIT_NAME}" if after_ollama else "",
        service_env_var=SERVICE_ENV_VAR,
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


def query_service_command(argv: list[str]) -> str | None:
    """Return what a query command printed, or None when it failed."""
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def systemd_scope() -> str:
    """Which systemd unit runs the relay: 'system' when one is installed, else 'user'."""
    return "system" if SYSTEM_UNIT_PATH.exists() else "user"


def systemctl(scope: str) -> list[str]:
    """systemctl, pointed at the manager that owns a unit of this scope."""
    return ["systemctl"] if scope == "system" else ["systemctl", "--user"]


def running_as_root() -> bool:
    """Whether this process may write /etc/systemd/system itself."""
    return hasattr(os, "geteuid") and os.geteuid() == 0


def unit_owner() -> tuple[str, str]:
    """The user and group a system unit runs as: whoever asked, also through sudo."""
    # POSIX-only modules, and this module is imported on Windows too.
    import grp
    import pwd

    uid = int(os.getenv("SUDO_UID") or os.getuid())
    entry = pwd.getpwuid(uid)
    return entry.pw_name, grp.getgrgid(entry.pw_gid).gr_name


def linger_enabled() -> bool | None:
    """Whether systemd keeps this user's manager running with nobody logged in.

    None when logind cannot say: no loginctl, or no bus to ask it over.
    """
    if not shutil.which("loginctl"):
        return None
    answer = query_service_command(
        ["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"]
    )
    return None if answer is None else answer == "yes"


def linger_warning() -> str | None:
    """What to say about a user unit that will not start at boot, or None."""
    if detect_manager() != "systemd" or systemd_scope() != "user":
        return None
    if linger_enabled() is not False:
        return None
    user = getpass.getuser()
    return (
        f"lmrelay: linger is off for {user}, so systemd stops the relay when your last "
        f"session ends and does not start it at boot. Run 'sudo loginctl enable-linger "
        f"{user}', or use 'lmrelay enable --system' instead"
    )


def enable_autostart(config_path: Path) -> str:
    """Install the unit or agent for this platform and start the relay under it."""
    manager = detect_manager()
    executable = relay_executable()
    # The manager starts the relay from a working directory the operator never
    # chose, so a relative --config would resolve somewhere else entirely.
    target = config_path.expanduser().resolve()

    if manager == "systemd":
        if SYSTEM_UNIT_PATH.exists():
            raise LmrelayError(
                f"lmrelay: a system unit is already installed at {SYSTEM_UNIT_PATH}; a user "
                f"unit beside it would fight it for the pidfile and the port"
            )
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


def system_unit_for(config_path: Path) -> str:
    """The system unit `enable --system` installs, refusing where it cannot go."""
    if detect_manager() != "systemd":
        raise LmrelayError("lmrelay: --system needs systemd; 'lmrelay enable' covers launchd")
    if SYSTEMD_UNIT_PATH.exists():
        raise LmrelayError(
            f"lmrelay: a user unit is installed at {SYSTEMD_UNIT_PATH}; run 'lmrelay disable' "
            f"first, so the two do not fight for the pidfile and the port"
        )
    user, group = unit_owner()
    after_ollama = probe_service_command(["systemctl", "cat", OLLAMA_UNIT_NAME]) == 0
    target = config_path.expanduser().resolve()
    return system_unit_text(relay_executable(), target, user, group, after_ollama)


def system_install_commands(config_path: Path) -> list[str]:
    """What an operator without root runs to install the unit `enable --system` printed."""
    return [
        f"lmrelay enable --system --config {shlex.quote(str(config_path))} "
        f"| sudo tee {SYSTEM_UNIT_PATH} > /dev/null",
        "sudo systemctl daemon-reload",
        f"sudo systemctl enable --now {SYSTEMD_UNIT_NAME}",
    ]


def system_removal_commands() -> list[str]:
    """What an operator without root runs to undo `enable --system`."""
    return [
        f"sudo systemctl disable --now {SYSTEMD_UNIT_NAME}",
        f"sudo rm {SYSTEM_UNIT_PATH}",
        "sudo systemctl daemon-reload",
    ]


def install_system_unit(text: str) -> str:
    """Write the system unit and start the relay under it. Needs root."""
    SYSTEM_UNIT_PATH.write_text(text, encoding="utf-8")
    SYSTEM_UNIT_PATH.chmod(0o644)
    run_service_command(["systemctl", "daemon-reload"])
    run_service_command(["systemctl", "enable", "--now", SYSTEMD_UNIT_NAME])
    return f"lmrelay: autostart enabled via a systemd system unit ({SYSTEM_UNIT_PATH})"


def remove_system_unit() -> str:
    """Stop the relay under its system unit and remove the unit. Needs root."""
    if not SYSTEM_UNIT_PATH.exists():
        return f"lmrelay: no unit at {SYSTEM_UNIT_PATH}; nothing to disable"
    run_service_command(["systemctl", "disable", "--now", SYSTEMD_UNIT_NAME])
    SYSTEM_UNIT_PATH.unlink(missing_ok=True)
    run_service_command(["systemctl", "daemon-reload"])
    return f"lmrelay: autostart disabled, removed {SYSTEM_UNIT_PATH}"


def disable_autostart() -> str:
    """Stop the relay under its manager and remove the unit or agent."""
    manager = detect_manager()

    if manager == "systemd":
        if not SYSTEMD_UNIT_PATH.exists():
            if SYSTEM_UNIT_PATH.exists():
                return (
                    f"lmrelay: no user unit at {SYSTEMD_UNIT_PATH}; the relay runs from the "
                    f"system unit {SYSTEM_UNIT_PATH}: run 'lmrelay disable --system'"
                )
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
        scope = systemd_scope()
        command = systemctl(scope)
        enabled = probe_service_command([*command, "is-enabled", SYSTEMD_UNIT_NAME])
        active = probe_service_command([*command, "is-active", SYSTEMD_UNIT_NAME])
        return {
            "manager":   manager,
            "scope":     scope,
            "installed": (SYSTEM_UNIT_PATH if scope == "system" else SYSTEMD_UNIT_PATH).exists(),
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


def is_unit_main_process(scope: str) -> bool:
    """Whether this process is the unit's main process, as systemd records it.

    Covers a unit written by hand without LMRELAY_SERVICE: its `lmrelay run`
    would otherwise find its own unit active and refuse to start.
    """
    main_pid = query_service_command(
        [*systemctl(scope), "show", "-p", "MainPID", "--value", SYSTEMD_UNIT_NAME]
    )
    return main_pid == str(os.getpid())


def service_is_active() -> bool:
    """Report whether a service manager is running the relay right now.

    False inside the managed process itself: both managers report the service
    active from the instant they exec it, so its own ExecStart asking this
    question would always be told the port is taken, by itself.
    """
    if started_by_service_manager():
        return False
    status = autostart_status()
    if not status["active"]:
        return False
    return not (status["manager"] == "systemd" and is_unit_main_process(status["scope"]))


def main():
    pass


if __name__ == "__main__":
    main()
