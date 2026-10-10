#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit and plist contents, and the manager calls, without a service manager."""

import configparser
import os
import plistlib
import subprocess

import pytest

# Local imports
from lmrelay import service
from lmrelay.errors import LmrelayError
from lmrelay.service import (
    LAUNCHD_LABEL,
    OLLAMA_UNIT_NAME,
    SERVICE_ENV_VAR,
    SYSTEMD_UNIT_NAME,
    autostart_status,
    detect_manager,
    disable_autostart,
    enable_autostart,
    install_system_unit,
    launchd_plist_text,
    linger_warning,
    relay_executable,
    remove_system_unit,
    service_is_active,
    system_unit_for,
    system_unit_text,
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


def systemctl_run(results: dict, main_pid: str = "1", calls: list | None = None):
    """Answer each systemctl verb with the return code the test chose, and
    `show -p MainPID` with the pid the test chose."""
    def run(argv, **unused_kwargs):
        if calls is not None:
            calls.append(argv)
        if "show" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{main_pid}\n", stderr="")
        verb = next(word for word in argv if word in results)
        return subprocess.CompletedProcess(argv, results[verb], stdout="", stderr="")

    return run


def loginctl_run(returncode: int, stdout: str):
    """Answer `loginctl show-user` the way logind would."""
    def run(argv, **unused_kwargs):
        assert argv[0] == "loginctl", argv
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    return run


@pytest.fixture
def systemd_unit(tmp_path, monkeypatch):
    """This machine has systemd, and the unit lives in tmp_path rather than in
    the developer's own ~/.config. Returns where the unit will be written."""
    monkeypatch.setattr(service, "detect_manager", lambda: "systemd")
    unit = tmp_path / SYSTEMD_UNIT_NAME
    monkeypatch.setattr(service, "SYSTEMD_UNIT_PATH", unit)
    return unit


@pytest.fixture
def system_unit(systemd_unit):
    """Where a system unit goes on this test's machine; conftest keeps it in
    tmp_path. The directory exists, as /etc/systemd/system does."""
    path = service.SYSTEM_UNIT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def launchd_agent(tmp_path, monkeypatch):
    """The same machine as a Mac, with the LaunchAgent in tmp_path."""
    monkeypatch.setattr(service, "detect_manager", lambda: "launchd")
    agent = tmp_path / f"{LAUNCHD_LABEL}.plist"
    monkeypatch.setattr(service, "LAUNCHD_PLIST_PATH", agent)
    return agent


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

    def test_a_start_that_keeps_failing_gives_up(self, tmp_path):
        """RestartSec=2 alone restarted a refused start every two seconds for
        days: one host counted 60,155 restarts."""
        unit = self.parsed(tmp_path)["Unit"]
        assert (unit["StartLimitIntervalSec"], unit["StartLimitBurst"]) == ("60", "5")

    def test_it_does_not_order_itself_after_a_target_a_user_manager_lacks(self, tmp_path):
        assert "After" not in self.parsed(tmp_path)["Unit"]


class TestTheSystemUnit:
    """`enable --system`: /etc/systemd/system, started at boot as the operator."""

    def parsed(self, tmp_path, after_ollama: bool = False):
        return parse_unit(system_unit_text(
            EXECUTABLE, tmp_path / "lmrelay.toml", "relay", "staff", after_ollama
        ))

    def test_it_is_a_unit_file_that_parses(self, tmp_path):
        assert self.parsed(tmp_path).sections() == ["Unit", "Service", "Install"]

    def test_it_starts_at_boot_rather_than_at_login(self, tmp_path):
        assert self.parsed(tmp_path)["Install"]["WantedBy"] == "multi-user.target"

    def test_it_runs_as_the_operator_rather_than_as_root(self, tmp_path):
        """The config, the state and the pidfile are the operator's files."""
        unit = self.parsed(tmp_path)["Service"]
        assert (unit["User"], unit["Group"]) == ("relay", "staff")

    def test_it_waits_for_the_network(self, tmp_path):
        unit = self.parsed(tmp_path)["Unit"]
        assert unit["Wants"] == "network-online.target"
        assert unit["After"] == "network-online.target"

    def test_and_for_a_system_ollama_when_there_is_one(self, tmp_path):
        """The one ordering a user unit cannot express."""
        unit = self.parsed(tmp_path, after_ollama=True)["Unit"]
        assert unit["After"] == f"network-online.target {OLLAMA_UNIT_NAME}"

    def test_it_runs_the_relay_with_its_config_and_says_it_is_the_unit(self, tmp_path):
        unit = self.parsed(tmp_path)["Service"]
        assert unit["ExecStart"] == f"{EXECUTABLE} run --config {tmp_path / 'lmrelay.toml'}"
        assert unit["Environment"] == f"{SERVICE_ENV_VAR}=1"

    def test_a_start_that_keeps_failing_gives_up(self, tmp_path):
        unit = self.parsed(tmp_path)["Unit"]
        assert (unit["StartLimitIntervalSec"], unit["StartLimitBurst"]) == ("60", "5")


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

    def interpreter_in(self, monkeypatch, directory):
        directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(service.sys, "executable", str(directory / "python"))

    def test_the_entry_point_beside_the_running_interpreter_is_used(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "venv" / "bin"
        self.interpreter_in(monkeypatch, bin_dir)
        entry_point = bin_dir / "lmrelay"
        entry_point.write_text("#!/bin/sh\n", encoding="utf-8")
        entry_point.chmod(0o755)
        assert relay_executable() == str(entry_point)

    def test_rather_than_whichever_install_is_first_on_path(self, tmp_path, monkeypatch):
        """With two installs on PATH, `which` named the other one, and the unit
        ran a relay the operator had not configured."""
        self.interpreter_in(monkeypatch, tmp_path / "venv" / "bin")
        monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/local/bin/lmrelay")
        assert "/usr/local/bin/lmrelay" not in relay_executable()

    def test_and_without_an_entry_point_it_is_this_interpreter_with_m(self, tmp_path, monkeypatch):
        """A unit file cannot depend on $PATH at boot."""
        self.interpreter_in(monkeypatch, tmp_path / "bin")
        assert relay_executable() == f"{tmp_path / 'bin' / 'python'} -m lmrelay"


class TestRegisteringForAutostart:
    """Writing the file is half of it; the manager has to be told."""

    def test_a_platform_with_no_manager_says_what_to_use_instead(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service, "detect_manager", lambda: "none")
        with pytest.raises(LmrelayError, match="lmrelay serve"):
            enable_autostart(tmp_path / "lmrelay.toml")

    def test_and_says_it_again_when_asked_to_disable(self, monkeypatch):
        monkeypatch.setattr(service, "detect_manager", lambda: "none")
        with pytest.raises(LmrelayError, match="lmrelay serve"):
            disable_autostart()

    def test_enabling_writes_the_unit(self, tmp_path, monkeypatch, systemd_unit):
        monkeypatch.setattr(service.subprocess, "run", recording_run([]))
        enable_autostart(tmp_path / "lmrelay.toml")
        assert systemd_unit.exists()

    def test_and_reports_one_line_to_the_operator(self, tmp_path, monkeypatch, systemd_unit):
        monkeypatch.setattr(service.subprocess, "run", recording_run([]))
        message = enable_autostart(tmp_path / "lmrelay.toml")
        assert message and "\n" not in message

    def test_the_manager_is_reloaded_before_the_unit_is_enabled(
        self, tmp_path, monkeypatch, systemd_unit
    ):
        """systemd will not see a unit file it has not been told to re-read."""
        calls: list[list[str]] = []
        monkeypatch.setattr(service.subprocess, "run", recording_run(calls))
        enable_autostart(tmp_path / "lmrelay.toml")
        assert "daemon-reload" in calls[0]

    def test_every_call_is_an_argument_list(self, tmp_path, monkeypatch, systemd_unit):
        """A string command line would go through a shell, and a home directory
        with a space in it would silently become two arguments."""
        calls: list[list[str]] = []
        monkeypatch.setattr(service.subprocess, "run", recording_run(calls))
        enable_autostart(tmp_path / "lmrelay.toml")
        assert calls and all(isinstance(argv, list) for argv in calls)

    def test_a_failing_systemctl_is_reported_with_what_it_said(
        self, tmp_path, monkeypatch, systemd_unit
    ):
        """A silent failure here means the relay does not come back after a
        reboot, and nothing ever said so."""
        monkeypatch.setattr(
            service.subprocess, "run", recording_run([], 1, "Failed to connect to bus")
        )
        with pytest.raises(LmrelayError, match="Failed to connect to bus"):
            enable_autostart(tmp_path / "lmrelay.toml")

    def test_disabling_takes_the_unit_away(self, monkeypatch, systemd_unit):
        systemd_unit.write_text("[Unit]\n", encoding="utf-8")
        monkeypatch.setattr(service.subprocess, "run", recording_run([]))
        disable_autostart()
        assert not systemd_unit.exists()

    def test_disabling_a_unit_that_was_never_written_is_not_an_error(self, systemd_unit):
        """And systemctl is not asked either: it refuses to disable a unit it
        cannot find. The autouse stub fails this test if anything is run."""
        assert "nothing to disable" in disable_autostart()

    def test_enabling_under_launchd_writes_an_agent_launchd_can_read(
        self, tmp_path, monkeypatch, launchd_agent
    ):
        """A plist that does not parse is refused at login with nothing said
        to anyone, and this is the file, not the template the text came from."""
        monkeypatch.setattr(service.subprocess, "run", recording_run([]))
        enable_autostart(tmp_path / "lmrelay.toml")
        assert plistlib.loads(launchd_agent.read_bytes())["Label"] == LAUNCHD_LABEL

    def test_and_loads_it_with_the_flag_that_clears_a_disable(
        self, tmp_path, monkeypatch, launchd_agent
    ):
        """`lmrelay disable` unloads with -w, which records the agent as disabled
        in launchd's own database. A plain `load` of a disabled agent is refused,
        so without -w here the second enable is the one that would fail."""
        calls: list[list[str]] = []
        monkeypatch.setattr(service.subprocess, "run", recording_run(calls))
        enable_autostart(tmp_path / "lmrelay.toml")
        assert calls == [["launchctl", "load", "-w", str(launchd_agent)]]

    def test_disabling_under_launchd_unloads_it_and_takes_the_agent_away(
        self, monkeypatch, launchd_agent
    ):
        """With -w, unlike `lmrelay stop`: this is the one command whose point
        is that the relay stops coming back at login."""
        launchd_agent.write_text("<plist/>\n", encoding="utf-8")
        calls: list[list[str]] = []
        monkeypatch.setattr(service.subprocess, "run", recording_run(calls))
        disable_autostart()
        assert calls == [["launchctl", "unload", "-w", str(launchd_agent)]]
        assert not launchd_agent.exists()

    def test_and_an_agent_that_was_never_written_is_nothing_to_disable(self, launchd_agent):
        assert "nothing to disable" in disable_autostart()

    def test_a_user_unit_is_refused_beside_a_system_one(self, tmp_path, system_unit):
        """Two units of one relay fight over the pidfile and the port."""
        system_unit.write_text("[Unit]\n", encoding="utf-8")
        with pytest.raises(LmrelayError, match="system unit is already installed"):
            enable_autostart(tmp_path / "lmrelay.toml")

    def test_disabling_the_user_unit_points_at_the_system_one(self, system_unit):
        system_unit.write_text("[Unit]\n", encoding="utf-8")
        assert "lmrelay disable --system" in disable_autostart()


class TestRegisteringASystemUnit:
    """`enable --system` and `disable --system`, without touching /etc."""

    def test_it_is_refused_beside_a_user_unit(self, tmp_path, systemd_unit):
        systemd_unit.write_text("[Unit]\n", encoding="utf-8")
        with pytest.raises(LmrelayError, match="lmrelay disable"):
            system_unit_for(tmp_path / "lmrelay.toml")

    def test_and_on_a_machine_without_systemd(self, tmp_path, launchd_agent):
        with pytest.raises(LmrelayError, match="needs systemd"):
            system_unit_for(tmp_path / "lmrelay.toml")

    def test_it_runs_as_whoever_asked_and_after_ollama_when_it_exists(
        self, tmp_path, monkeypatch, systemd_unit
    ):
        monkeypatch.setattr(service, "unit_owner", lambda: ("relay", "staff"))
        monkeypatch.setattr(service.subprocess, "run", systemctl_run({"cat": 0}))
        unit = parse_unit(system_unit_for(tmp_path / "lmrelay.toml"))
        assert unit["Service"]["User"] == "relay"
        assert OLLAMA_UNIT_NAME in unit["Unit"]["After"]

    def test_installing_it_talks_to_the_system_manager(self, monkeypatch, system_unit):
        calls: list[list[str]] = []
        monkeypatch.setattr(service.subprocess, "run", recording_run(calls))
        install_system_unit("[Unit]\n")
        assert system_unit.read_text(encoding="utf-8") == "[Unit]\n"
        assert calls == [
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", "--now", SYSTEMD_UNIT_NAME],
        ]

    def test_removing_it_stops_it_before_the_file_goes(self, monkeypatch, system_unit):
        system_unit.write_text("[Unit]\n", encoding="utf-8")
        calls: list[list[str]] = []
        monkeypatch.setattr(service.subprocess, "run", recording_run(calls))
        remove_system_unit()
        assert not system_unit.exists()
        assert calls == [
            ["systemctl", "disable", "--now", SYSTEMD_UNIT_NAME],
            ["systemctl", "daemon-reload"],
        ]

    def test_and_a_system_unit_that_was_never_written_is_nothing_to_remove(self, system_unit):
        assert "nothing to disable" in remove_system_unit()


class TestReportingAutostart:
    """What the last line of `lmrelay status` is made of."""

    def test_a_platform_with_no_manager_has_nothing_registered(self, monkeypatch):
        monkeypatch.setattr(service, "detect_manager", lambda: "none")
        status = autostart_status()
        assert status["manager"] == "none"
        assert not (status["installed"] or status["enabled"] or status["active"])

    def test_a_written_unit_counts_as_installed(self, monkeypatch, systemd_unit):
        systemd_unit.write_text("[Unit]\n", encoding="utf-8")
        monkeypatch.setattr(
            service.subprocess, "run", systemctl_run({"is-enabled": 0, "is-active": 0})
        )
        assert autostart_status()["installed"] is True

    def test_the_return_code_decides_it_rather_than_the_output(self, monkeypatch, systemd_unit):
        """`systemctl is-enabled` prints "disabled" and exits non-zero; reading
        its stdout would report a disabled unit as enabled."""
        monkeypatch.setattr(
            service.subprocess, "run", systemctl_run({"is-enabled": 1, "is-active": 3})
        )
        status = autostart_status()
        assert status["enabled"] is False and status["active"] is False

    def test_an_active_unit_is_what_the_cli_delegates_to(self, monkeypatch, systemd_unit):
        """`lmrelay stop` has to go through the manager when the manager owns
        the process, or the two end up disagreeing about who does."""
        monkeypatch.setattr(
            service.subprocess, "run", systemctl_run({"is-enabled": 0, "is-active": 0})
        )
        assert service_is_active() is True

    def test_but_not_from_inside_the_managed_process(self, monkeypatch, systemd_unit):
        """The unit's own ExecStart is `lmrelay run`, and by the time it runs
        the manager already calls the unit active. Answering yes here is what
        would make it refuse to start itself."""
        monkeypatch.setenv(SERVICE_ENV_VAR, "1")
        monkeypatch.setattr(
            service.subprocess, "run", systemctl_run({"is-enabled": 0, "is-active": 0})
        )
        assert service_is_active() is False

    def test_a_system_unit_is_asked_about_through_the_system_manager(
        self, monkeypatch, system_unit
    ):
        system_unit.write_text("[Unit]\n", encoding="utf-8")
        calls: list[list[str]] = []
        monkeypatch.setattr(
            service.subprocess, "run",
            systemctl_run({"is-enabled": 0, "is-active": 0}, calls=calls),
        )
        status = autostart_status()
        assert (status["scope"], status["installed"], status["active"]) == ("system", True, True)
        assert all("--user" not in argv for argv in calls) and calls

    def test_and_it_is_what_the_cli_delegates_to(self, monkeypatch, system_unit):
        """A hand-written system unit used to be invisible: `stop` signalled the
        pid behind systemd's back, and Restart=on-failure left it down."""
        system_unit.write_text("[Unit]\n", encoding="utf-8")
        monkeypatch.setattr(
            service.subprocess, "run", systemctl_run({"is-enabled": 0, "is-active": 0})
        )
        assert service_is_active() is True

    def test_but_not_from_inside_its_main_process_even_without_the_variable(
        self, monkeypatch, system_unit
    ):
        """A unit written by hand sets no LMRELAY_SERVICE, and its `lmrelay run`
        would otherwise refuse to start because its own unit is active."""
        system_unit.write_text("[Unit]\n", encoding="utf-8")
        monkeypatch.setattr(
            service.subprocess, "run",
            systemctl_run({"is-enabled": 0, "is-active": 0}, main_pid=str(os.getpid())),
        )
        assert service_is_active() is False

    def test_under_launchd_one_probe_answers_for_enabled_and_active_alike(
        self, monkeypatch, launchd_agent
    ):
        """launchd has no is-enabled and no is-active: `launchctl list <label>`
        exits zero for a loaded agent and non-zero otherwise, and that one code
        is all the status line has to go on."""
        calls: list[list[str]] = []
        monkeypatch.setattr(service.subprocess, "run", recording_run(calls))
        status = autostart_status()
        assert status["enabled"] is True and status["active"] is True
        assert calls == [["launchctl", "list", LAUNCHD_LABEL]]


class TestLinger:
    """A user unit without linger starts at login and stops after the last
    session ends, so it is not autostart on a headless server."""

    def with_loginctl(self, monkeypatch, returncode: int, stdout: str):
        monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(service.subprocess, "run", loginctl_run(returncode, stdout))

    def test_linger_off_is_warned_about_with_both_ways_out(self, monkeypatch, systemd_unit):
        self.with_loginctl(monkeypatch, 0, "no\n")
        warning = linger_warning()
        assert "does not start it at boot" in warning
        assert "enable-linger" in warning and "lmrelay enable --system" in warning

    def test_linger_on_is_not(self, monkeypatch, systemd_unit):
        self.with_loginctl(monkeypatch, 0, "yes\n")
        assert linger_warning() is None

    def test_nor_is_a_logind_that_cannot_answer(self, monkeypatch, systemd_unit):
        """A container with no bus: a warning there would be a guess."""
        self.with_loginctl(monkeypatch, 1, "")
        assert linger_warning() is None

    def test_nor_is_a_system_unit_which_needs_no_linger(self, monkeypatch, system_unit):
        system_unit.write_text("[Unit]\n", encoding="utf-8")
        self.with_loginctl(monkeypatch, 0, "no\n")
        assert linger_warning() is None


def main():
    pass


if __name__ == "__main__":
    main()
