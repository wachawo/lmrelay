#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit and plist contents, and the manager calls, without a service manager."""

import configparser
import plistlib
import subprocess
import sys

import pytest

# Local imports
from lmrelay import service
from lmrelay.errors import LmrelayError
from lmrelay.service import (
    LAUNCHD_LABEL,
    SERVICE_ENV_VAR,
    SYSTEMD_UNIT_NAME,
    autostart_status,
    detect_manager,
    disable_autostart,
    enable_autostart,
    launchd_plist_text,
    relay_executable,
    service_is_active,
    systemd_unit_text,
)

EXECUTABLE = "/opt/venv/bin/lmrelay"


@pytest.fixture(autouse=True)
def no_real_service_manager(monkeypatch):
    """A test that reached a real systemctl or launchctl would enable, start or
    stop the relay in the developer's own session, so the call itself fails."""
    def refuse(argv, **unused_kwargs):
        raise AssertionError(f"a test tried to run {argv}")

    monkeypatch.setattr(service.subprocess, "run", refuse)


def parse_unit(text: str) -> configparser.RawConfigParser:
    """Read a unit file. systemd keys are case-sensitive; configparser lowercases
    them unless told otherwise, and Raw keeps $MAINPID out of interpolation."""
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser.read_string(text)
    return parser


def recording_run(calls, returncode: int = 0, stderr: str = ""):
    """Stand in for subprocess.run, recording the argv it was handed."""
    def run(argv, **unused_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout="", stderr=stderr)

    return run


def systemctl_run(results: dict):
    """Answer each systemctl verb with the return code the test chose."""
    def run(argv, **unused_kwargs):
        verb = next(word for word in argv if word in results)
        return subprocess.CompletedProcess(argv, results[verb], stdout="", stderr="")

    return run


class TestTheSystemdUnit:
    """Written by hand into ~/.config/systemd/user, read at every login."""

    def parsed(self, tmp_path):
        return parse_unit(systemd_unit_text(EXECUTABLE, tmp_path / "lmrelay.toml"))

    def test_it_is_a_unit_file_that_parses(self, tmp_path):
        assert self.parsed(tmp_path).sections() == ["Unit", "Service", "Install"]

    def test_it_runs_the_relay_in_the_foreground(self, tmp_path):
        """systemd supervises the process itself; a detached `serve` would exit
        immediately and be restarted forever."""
        assert self.parsed(tmp_path)["Service"]["ExecStart"].startswith(f"{EXECUTABLE} run")

    def test_with_the_config_it_was_given(self, tmp_path):
        target = tmp_path / "elsewhere" / "lmrelay.toml"
        assert f"--config {target}" in systemd_unit_text(EXECUTABLE, target)

    def test_a_reload_is_a_signal_rather_than_a_restart(self, tmp_path):
        """`systemctl --user reload` must not drop the streams in flight."""
        assert "kill -HUP $MAINPID" in self.parsed(tmp_path)["Service"]["ExecReload"]

    def test_it_comes_back_when_it_fails(self, tmp_path):
        assert self.parsed(tmp_path)["Service"]["Restart"] == "on-failure"

    def test_it_starts_with_the_user_session(self, tmp_path):
        assert self.parsed(tmp_path)["Install"]["WantedBy"] == "default.target"

    def test_it_tells_the_process_it_is_the_unit(self, tmp_path):
        """Type=simple marks the unit active the moment systemd forks it, so
        the ExecStart, which is `lmrelay run`, would find its own unit running
        and refuse the port to itself. Without this the unit never serves a
        request: it fails, is restarted, and reaches the start limit."""
        assert self.parsed(tmp_path)["Service"]["Environment"] == f"{SERVICE_ENV_VAR}=1"


class TestTheLaunchdPlist:
    """The same arrangement on macOS, in XML."""

    def loaded(self, tmp_path):
        text = launchd_plist_text(
            EXECUTABLE, tmp_path / "lmrelay.toml", tmp_path / "lmrelay.log"
        )
        return plistlib.loads(text.encode("utf-8"))

    def test_it_is_a_plist_that_parses(self, tmp_path):
        assert self.loaded(tmp_path)["Label"] == LAUNCHD_LABEL

    def test_it_runs_the_relay_with_its_config(self, tmp_path):
        argv = self.loaded(tmp_path)["ProgramArguments"]
        assert argv == [EXECUTABLE, "run", "--config", str(tmp_path / "lmrelay.toml")]

    def test_it_starts_when_it_is_loaded(self, tmp_path):
        assert self.loaded(tmp_path)["RunAtLoad"] is True

    def test_it_tells_the_process_it_is_the_agent(self, tmp_path):
        """`launchctl list <label>` succeeds for a loaded agent, and RunAtLoad
        starts the process while it is loaded, so the same self-refusal applies
        here as under systemd."""
        assert self.loaded(tmp_path)["EnvironmentVariables"] == {SERVICE_ENV_VAR: "1"}

    def test_but_is_restarted_only_after_a_failure(self, tmp_path):
        """An unconditional KeepAlive would fight `lmrelay stop`."""
        assert self.loaded(tmp_path)["KeepAlive"] == {"SuccessfulExit": False}

    def test_its_output_goes_where_the_relays_log_goes(self, tmp_path):
        loaded = self.loaded(tmp_path)
        assert loaded["StandardOutPath"] == str(tmp_path / "lmrelay.log")
        assert loaded["StandardErrorPath"] == str(tmp_path / "lmrelay.log")

    def test_a_path_with_an_ampersand_survives_the_xml(self, tmp_path):
        """An unescaped & in a home directory name makes the plist unparseable,
        and launchd refuses it at boot with nothing said to anyone."""
        config = tmp_path / "a&b" / "lmrelay.toml"
        text = launchd_plist_text(EXECUTABLE, config, tmp_path / "lmrelay.log")
        assert plistlib.loads(text.encode("utf-8"))["ProgramArguments"][-1] == str(config)


class TestDetectingTheManager:
    """Which of the two, if either, this machine has."""

    def pretend(self, monkeypatch, platform: str, tool: str | None):
        monkeypatch.setattr(service.sys, "platform", platform)
        monkeypatch.setattr(
            service.shutil, "which", lambda name: f"/usr/bin/{name}" if name == tool else None
        )

    def test_linux_with_systemctl_is_systemd(self, monkeypatch):
        self.pretend(monkeypatch, "linux", "systemctl")
        assert detect_manager() == "systemd"

    def test_linux_without_it_has_no_manager(self, monkeypatch):
        """A container or a minimal box: autostart is refused with a message
        rather than a unit file nothing will ever read."""
        self.pretend(monkeypatch, "linux", None)
        assert detect_manager() == "none"

    def test_macos_with_launchctl_is_launchd(self, monkeypatch):
        self.pretend(monkeypatch, "darwin", "launchctl")
        assert detect_manager() == "launchd"

    def test_windows_has_neither(self, monkeypatch):
        self.pretend(monkeypatch, "win32", "systemctl")
        assert detect_manager() == "none"


class TestFindingTheExecutable:
    """What the unit file will call, hours later, with a different $PATH."""

    def test_the_installed_entry_point_is_used(self, monkeypatch):
        monkeypatch.setattr(service.shutil, "which", lambda name: EXECUTABLE)
        assert relay_executable() == EXECUTABLE

    def test_and_the_fallback_is_still_an_absolute_path(self, monkeypatch):
        """A unit file cannot depend on $PATH at boot."""
        monkeypatch.setattr(service.shutil, "which", lambda name: None)
        assert relay_executable().startswith(sys.executable)


class TestRegisteringForAutostart:
    """Writing the file is half of it; the manager has to be told."""

    def as_systemd(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service, "detect_manager", lambda: "systemd")
        monkeypatch.setattr(service, "SYSTEMD_UNIT_PATH", tmp_path / SYSTEMD_UNIT_NAME)
        return tmp_path / SYSTEMD_UNIT_NAME

    def test_a_platform_with_no_manager_says_what_to_use_instead(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service, "detect_manager", lambda: "none")
        with pytest.raises(LmrelayError, match="lmrelay serve"):
            enable_autostart(tmp_path / "lmrelay.toml")

    def test_enabling_writes_the_unit(self, tmp_path, monkeypatch):
        unit = self.as_systemd(tmp_path, monkeypatch)
        monkeypatch.setattr(service.subprocess, "run", recording_run([]))
        enable_autostart(tmp_path / "lmrelay.toml")
        assert unit.exists()

    def test_and_reports_one_line_to_the_operator(self, tmp_path, monkeypatch):
        self.as_systemd(tmp_path, monkeypatch)
        monkeypatch.setattr(service.subprocess, "run", recording_run([]))
        message = enable_autostart(tmp_path / "lmrelay.toml")
        assert message and "\n" not in message

    def test_the_manager_is_reloaded_before_the_unit_is_enabled(self, tmp_path, monkeypatch):
        """systemd will not see a unit file it has not been told to re-read."""
        self.as_systemd(tmp_path, monkeypatch)
        calls: list[list[str]] = []
        monkeypatch.setattr(service.subprocess, "run", recording_run(calls))
        enable_autostart(tmp_path / "lmrelay.toml")
        assert "daemon-reload" in calls[0]

    def test_every_call_is_an_argument_list(self, tmp_path, monkeypatch):
        """A string command line would go through a shell, and a home directory
        with a space in it would silently become two arguments."""
        self.as_systemd(tmp_path, monkeypatch)
        calls: list[list[str]] = []
        monkeypatch.setattr(service.subprocess, "run", recording_run(calls))
        enable_autostart(tmp_path / "lmrelay.toml")
        assert calls and all(isinstance(argv, list) for argv in calls)

    def test_a_failing_systemctl_is_reported_with_what_it_said(self, tmp_path, monkeypatch):
        """A silent failure here means the relay does not come back after a
        reboot, and nothing ever said so."""
        self.as_systemd(tmp_path, monkeypatch)
        monkeypatch.setattr(
            service.subprocess, "run", recording_run([], 1, "Failed to connect to bus")
        )
        with pytest.raises(LmrelayError, match="Failed to connect to bus"):
            enable_autostart(tmp_path / "lmrelay.toml")

    def test_disabling_takes_the_unit_away(self, tmp_path, monkeypatch):
        unit = self.as_systemd(tmp_path, monkeypatch)
        unit.write_text("[Unit]\n", encoding="utf-8")
        monkeypatch.setattr(service.subprocess, "run", recording_run([]))
        disable_autostart()
        assert not unit.exists()


class TestReportingAutostart:
    """What the last line of `lmrelay status` is made of."""

    def test_a_platform_with_no_manager_has_nothing_registered(self, monkeypatch):
        monkeypatch.setattr(service, "detect_manager", lambda: "none")
        status = autostart_status()
        assert status["manager"] == "none"
        assert not (status["installed"] or status["enabled"] or status["active"])

    def test_a_written_unit_counts_as_installed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service, "detect_manager", lambda: "systemd")
        unit = tmp_path / SYSTEMD_UNIT_NAME
        unit.write_text("[Unit]\n", encoding="utf-8")
        monkeypatch.setattr(service, "SYSTEMD_UNIT_PATH", unit)
        monkeypatch.setattr(
            service.subprocess, "run", systemctl_run({"is-enabled": 0, "is-active": 0})
        )
        assert autostart_status()["installed"] is True

    def test_the_return_code_decides_it_rather_than_the_output(self, monkeypatch, tmp_path):
        """`systemctl is-enabled` prints "disabled" and exits non-zero; reading
        its stdout would report a disabled unit as enabled."""
        monkeypatch.setattr(service, "detect_manager", lambda: "systemd")
        monkeypatch.setattr(service, "SYSTEMD_UNIT_PATH", tmp_path / SYSTEMD_UNIT_NAME)
        monkeypatch.setattr(
            service.subprocess, "run", systemctl_run({"is-enabled": 1, "is-active": 3})
        )
        status = autostart_status()
        assert status["enabled"] is False and status["active"] is False

    def test_an_active_unit_is_what_the_cli_delegates_to(self, monkeypatch, tmp_path):
        """`lmrelay stop` has to go through the manager when the manager owns
        the process, or the two end up disagreeing about who does."""
        monkeypatch.setattr(service, "detect_manager", lambda: "systemd")
        monkeypatch.setattr(service, "SYSTEMD_UNIT_PATH", tmp_path / SYSTEMD_UNIT_NAME)
        monkeypatch.setattr(
            service.subprocess, "run", systemctl_run({"is-enabled": 0, "is-active": 0})
        )
        assert service_is_active() is True

    def test_but_not_from_inside_the_managed_process(self, monkeypatch, tmp_path):
        """The unit's own ExecStart is `lmrelay run`, and by the time it runs
        the manager already calls the unit active. Answering yes here is what
        would make it refuse to start itself."""
        monkeypatch.setenv(SERVICE_ENV_VAR, "1")
        monkeypatch.setattr(service, "detect_manager", lambda: "systemd")
        monkeypatch.setattr(service, "SYSTEMD_UNIT_PATH", tmp_path / SYSTEMD_UNIT_NAME)
        monkeypatch.setattr(
            service.subprocess, "run", systemctl_run({"is-enabled": 0, "is-active": 0})
        )
        assert service_is_active() is False


def main():
    pass


if __name__ == "__main__":
    main()
