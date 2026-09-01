#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pidfiles, liveness and one real detached relay, started and stopped."""

import os
import socket
import subprocess
import sys
import threading
import time

import pytest

# Local imports
from lmrelay.config import CONFIG_ENV_VAR, load_config
from lmrelay.daemon import (
    BIND_ENV_VAR,
    LOG_NAME,
    PID_NAME,
    daemon_status,
    log_file,
    pid_file,
    probe_health,
    process_alive,
    publish_bind,
    read_bind,
    read_pid,
    read_startup_settings,
    recorded_bind,
    reload_daemon,
    remove_pid,
    restart_warning,
    start_detached,
    stop_daemon,
    unapplied_settings,
    wait_for_relay,
    write_pid,
)
from lmrelay.errors import LmrelayError
from tests.conftest import free_port

# An upstream pointed at a host nothing resolves: nothing in this file asks the
# relay to forward anything, and a test that accidentally did would fail loudly
# instead of reaching a real Ollama.
CONFIG_TEMPLATE = """
[server]
host = "127.0.0.1"
port = {port}

[upstream.ollama]
base_url = "http://ollama.invalid:11434"
dialect  = "ollama"
"""

# pid 1 is init: alive, and not ours to signal.
FOREIGN_PID = 1

# Binds the port it is given, ignores SIGTERM, and waits to be killed.
HOLDS_A_PORT = """
import signal, socket, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
sock = socket.socket()
sock.bind(("127.0.0.1", int(sys.argv[1])))
sock.listen(1)
print("bound", flush=True)
time.sleep(60)
"""


def write_config_on_port(tmp_path, port: int):
    """Write a config on the given port and return its path."""
    target = tmp_path / "lmrelay.toml"
    target.write_text(CONFIG_TEMPLATE.format(port=port), encoding="utf-8")
    return target


def dead_pid() -> int:
    """A pid that names no process: a child that has already been reaped."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def holds_within(predicate, timeout: float = 20.0) -> bool:
    """Poll until the predicate holds, because a fork is not instant. False
    rather than a failure when it never does, so the caller can say why with
    the relay's own log."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def read_log(config_path) -> str:
    """The relay's own log is the only account of a start that never answered."""
    target = log_file(config_path)
    return target.read_text(encoding="utf-8") if target.exists() else "no log was written"


def start_relay_through_the_cli(tmp_path, attempts: int = 3):
    """Start a detached relay the way an operator does, by running the command.

    Not by calling start_detached() in this process: by the time this test runs,
    pytest is multi-threaded, and macOS kills a forked child that touches the
    system frameworks without exec'ing, silently, leaving the empty log that
    sent the first diagnosis of this failure after a port race instead. A real
    `lmrelay serve` forks from a single-threaded shell, which is the path worth
    covering anyway.

    free_port() closes its probe socket before returning the number, so the port
    can still be taken in between; that specific loss is retried rather than
    failed, and everything else is reported with both sides' output, because the
    relay's log lives on the runner where nobody reading CI can open it.
    """
    last = ""
    for remaining in range(attempts - 1, -1, -1):
        port = free_port()
        config = write_config_on_port(tmp_path, port)
        done = subprocess.run(
            [sys.executable, "-m", "lmrelay", "serve",
             "--config", str(config), "--port", str(port)],
            capture_output=True, text=True, timeout=60,
        )
        pid = read_pid(pid_file(config))
        if done.returncode == 0 and pid is not None:
            return pid, config, port
        last = f"exit {done.returncode}\n{done.stdout}{done.stderr}\n{read_log(config)}"
        if remaining and "address already in use" in last.lower():
            remove_pid(pid_file(config))
            continue
        raise AssertionError(f"`lmrelay serve` did not leave a relay running:\n{last}")
    raise AssertionError(f"the port was taken {attempts} times over. Last attempt:\n{last}")


class TestWhereTheProcessFilesGo:
    """Beside the config, so every command looks in one place."""

    def test_the_pidfile_sits_beside_the_config(self, tmp_path):
        assert pid_file(tmp_path / "lmrelay.toml") == tmp_path / PID_NAME

    def test_and_so_does_the_log(self, tmp_path):
        assert log_file(tmp_path / "lmrelay.toml") == tmp_path / LOG_NAME


class TestReadingThePidfile:
    """One call answers "is a relay running", so every unusable file reads as no."""

    def test_a_missing_file_means_nothing_is_running(self, tmp_path):
        assert read_pid(tmp_path / PID_NAME) is None

    def test_so_does_an_empty_one(self, tmp_path):
        target = tmp_path / PID_NAME
        target.write_text("", encoding="utf-8")
        assert read_pid(target) is None

    def test_so_does_a_file_of_garbage(self, tmp_path):
        target = tmp_path / PID_NAME
        target.write_text("not-a-pid\n", encoding="utf-8")
        assert read_pid(target) is None

    def test_and_so_does_one_naming_a_process_that_has_exited(self, tmp_path):
        """A stale pidfile has to behave like no pidfile, or a relay killed by a
        reboot could never be started again."""
        target = tmp_path / PID_NAME
        write_pid(target, dead_pid())
        assert read_pid(target) is None

    def test_so_does_one_holding_a_number_too_large_to_be_a_pid(self, tmp_path):
        """A partial write parses as an int and then overflows os.kill. Reading
        it as a live relay is not the risk; raising is, because it would crash `status`
        and `stop`, and `stop` is the one command that could clear the file."""
        target = tmp_path / PID_NAME
        target.write_text("999999999999999999999\n", encoding="utf-8")
        assert read_pid(target) is None

    def test_a_live_pid_is_returned(self, tmp_path):
        target = tmp_path / PID_NAME
        write_pid(target, os.getpid())
        assert read_pid(target) == os.getpid()

    def test_the_directory_is_made_if_it_is_not_there(self, tmp_path):
        target = tmp_path / "new" / PID_NAME
        write_pid(target, os.getpid())
        assert read_pid(target) == os.getpid()

    def test_removing_it_makes_it_read_as_nothing(self, tmp_path):
        target = tmp_path / PID_NAME
        write_pid(target, os.getpid())
        remove_pid(target)
        assert read_pid(target) is None

    def test_removing_one_that_is_not_there_is_not_an_error(self, tmp_path):
        """Shutdown removes it unconditionally, and a clean exit must not turn
        into a traceback over a file that was already gone."""
        remove_pid(tmp_path / PID_NAME)


class TestWhetherAProcessIsThere:
    """The check behind every stale-pidfile decision."""

    def test_this_one_is(self):
        assert process_alive(os.getpid())

    def test_a_reaped_child_is_not(self):
        assert not process_alive(dead_pid())

    def test_a_number_too_large_for_a_pid_is_not(self):
        assert not process_alive(999999999999999999999)


# Under root the refusal these tests rely on never comes: os.kill(1, SIGTERM) is
# permitted, and stop_daemon would go on to wait out its timeout and SIGKILL init.
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root may signal pid 1"
)
class TestAPidWeMayNotSignal:
    """process_alive reports another user's process as alive on purpose, so a
    pid we cannot signal reaches the commands that signal it."""

    def test_stopping_it_says_whose_it_is_rather_than_raising_permission_error(self, tmp_path):
        config = write_config_on_port(tmp_path, 11435)
        write_pid(pid_file(config), FOREIGN_PID)
        with pytest.raises(LmrelayError, match="another user"):
            stop_daemon(config)
        # And the pidfile is left alone: removing it would only hide the state
        # the operator has to act on.
        assert pid_file(config).exists()

    def test_and_so_does_reloading_it(self, tmp_path):
        config = write_config_on_port(tmp_path, 11435)
        write_pid(pid_file(config), FOREIGN_PID)
        with pytest.raises(LmrelayError, match="another user"):
            reload_daemon(config)


class TestStoppingSomethingThatWillNotGo:
    """SIGKILL is asynchronous, and `restart` binds the same port next."""

    def test_the_port_is_free_again_before_stop_returns(self, tmp_path):
        """uvicorn's graceful shutdown waits for in-flight responses and this
        relay has no read timeout, so a stop during a long stream reliably takes
        the SIGKILL branch. Returning before the kernel has torn the process
        down hands `restart` an address still in use."""
        port = free_port()
        config = write_config_on_port(tmp_path, port)
        # The context manager closes the stdout pipe on the way out, which a bare
        # Popen left open for the garbage collector to complain about.
        with subprocess.Popen(
            [sys.executable, "-c", HOLDS_A_PORT, str(port)], stdout=subprocess.PIPE
        ) as child:
            try:
                assert child.stdout.readline().strip() == b"bound"
                write_pid(pid_file(config), child.pid)
                # Reap as init would for a real detached relay. Unreaped, the
                # killed child is a zombie that os.kill(pid, 0) still finds, and
                # stop_daemon sits out its whole SIGKILL timeout on a process
                # that is gone.
                threading.Thread(target=child.wait, daemon=True).start()
                assert stop_daemon(config, timeout=0.2) is True
                with socket.socket() as rebind:
                    rebind.bind(("127.0.0.1", port))
            finally:
                child.kill()


class TestAskingTheRelayIfItIsWell:
    """probe_health, which only ever decides what to print."""

    def test_a_port_nothing_listens_on_is_not_healthy(self):
        """Any exception is a no: a refused connection and a timeout mean the
        same thing to the operator reading `status`."""
        assert not probe_health("127.0.0.1", free_port(), timeout=1.0)


class TestNothingToActOn:
    """The commands say so rather than failing."""

    def test_stopping_reports_that_nothing_was_running(self, tmp_path):
        assert stop_daemon(write_config_on_port(tmp_path, 11435)) is False

    def test_and_so_does_reloading(self, tmp_path):
        assert reload_daemon(write_config_on_port(tmp_path, 11435)) is False


class TestTheStatusBlock:
    """What `lmrelay status` is assembled from."""

    def status(self, tmp_path):
        return daemon_status(load_config(write_config_on_port(tmp_path, 11435)))

    def test_a_stopped_relay_has_no_pid_and_is_not_probed(self, tmp_path):
        status = self.status(tmp_path)
        assert status["running"] is False
        assert status["pid"] is None
        assert status["healthy"] is False

    def test_it_still_says_what_the_relay_would_do_if_it_were_started(self, tmp_path):
        """That is the question a stopped relay raises, so the rest of the block
        is filled in whether or not anything is running."""
        status = self.status(tmp_path)
        assert status["port"] == 11435
        assert status["upstreams"] == "ollama"
        assert status["default_upstream"] == "ollama"

    def test_and_whether_a_caller_would_need_a_credential(self, tmp_path):
        status = self.status(tmp_path)
        assert status["auth_enabled"] is False
        assert status["token_count"] == 0


class TestTheAddressTheRelayRecorded:
    """`status` runs in another process, where a --port is long gone."""

    def test_a_pidfile_carries_the_address_alongside_the_pid(self, tmp_path):
        target = tmp_path / PID_NAME
        write_pid(target, os.getpid(), "127.0.0.1:9999")
        assert read_pid(target) == os.getpid()
        assert read_bind(target) == ("127.0.0.1", 9999)

    def test_one_written_without_an_address_reads_as_none(self, tmp_path):
        target = tmp_path / PID_NAME
        write_pid(target, os.getpid())
        assert read_pid(target) == os.getpid()
        assert read_bind(target) is None

    def test_only_the_last_colon_separates_the_port(self, tmp_path):
        """An IPv6 literal is full of colons and would otherwise parse as garbage."""
        target = tmp_path / PID_NAME
        write_pid(target, os.getpid(), "::1:11435")
        assert read_bind(target) == ("::1", 11435)

    def test_a_second_line_of_garbage_reads_as_none(self, tmp_path):
        target = tmp_path / PID_NAME
        target.write_text(f"{os.getpid()}\nnot-an-address\n", encoding="utf-8")
        assert read_pid(target) == os.getpid()
        assert read_bind(target) is None

    def test_the_published_address_is_what_gets_recorded(self, tmp_path, monkeypatch):
        config = load_config(write_config_on_port(tmp_path, 11435))
        # publish_bind writes os.environ directly; recording the variable with
        # monkeypatch first is what takes the published address back out at
        # teardown. recorded_bind reads the empty string as unset.
        monkeypatch.setenv(BIND_ENV_VAR, "")
        assert recorded_bind(config) == "127.0.0.1:11435"
        publish_bind("0.0.0.0", 8080)
        assert recorded_bind(config) == "0.0.0.0:8080"

    def test_status_reports_where_the_relay_went_not_where_the_config_points(
        self, tmp_path, monkeypatch
    ):
        """`serve --port 9999` used to be reported at the configured port, and a
        healthy relay called not responding for answering elsewhere."""
        config_path = write_config_on_port(tmp_path, 11435)
        write_pid(pid_file(config_path), os.getpid(), "127.0.0.1:9999")
        monkeypatch.setattr("lmrelay.daemon.probe_health", lambda host, port: port == 9999)

        status = daemon_status(load_config(config_path))
        assert (status["host"], status["port"]) == ("127.0.0.1", 9999)
        assert status["healthy"] is True


class TestWhatTheRunningRelayStartedWith:
    """The pidfile is the only record that survives the process that chose it."""

    def test_the_three_settings_a_reload_cannot_apply_are_recorded(self, tmp_path):
        target = tmp_path / PID_NAME
        write_pid(target, os.getpid(), "127.0.0.1:11435", 10)
        assert read_startup_settings(target) == {
            "host": "127.0.0.1", "port": 11435, "connect_timeout": 10
        }

    def test_a_pidfile_from_a_build_that_recorded_no_timeout_omits_it(self, tmp_path):
        """Absent rather than guessed: whoever names what a reload cannot apply
        then says less rather than something wrong."""
        target = tmp_path / PID_NAME
        write_pid(target, os.getpid(), "127.0.0.1:11435")
        assert read_startup_settings(target) == {"host": "127.0.0.1", "port": 11435}

    def test_and_a_third_line_never_floats_without_a_second(self, tmp_path):
        """A reader counting lines must not find a timeout where an address
        should be."""
        target = tmp_path / PID_NAME
        write_pid(target, os.getpid(), connect_timeout=10)
        assert target.read_text(encoding="utf-8") == f"{os.getpid()}\n"

    def test_a_pidfile_with_no_address_at_all_says_nothing(self, tmp_path):
        target = tmp_path / PID_NAME
        write_pid(target, os.getpid())
        assert read_startup_settings(target) == {}

    def test_an_ipv6_literal_survives_the_round_trip(self, tmp_path):
        """Full of colons, and only the last one separates the port."""
        target = tmp_path / PID_NAME
        write_pid(target, os.getpid(), "::1:11435", 10)
        assert read_startup_settings(target)["host"] == "::1"


class TestNamingWhatAReloadCannotApply:
    """One comparison and one sentence, for the relay's log and the terminal."""

    def started(self, **overrides):
        return {"host": "127.0.0.1", "port": 11435, "connect_timeout": 10, **overrides}

    def test_nothing_moved_is_nothing_to_say(self, tmp_path):
        config = load_config(write_config_on_port(tmp_path, 11435))
        assert unapplied_settings(self.started(), config) == []

    def test_a_moved_port_is_named_with_both_values(self, tmp_path):
        """Naming only the key sends the operator to the file to read the half
        of the answer the file already has."""
        config = load_config(write_config_on_port(tmp_path, 8080))
        assert unapplied_settings(self.started(), config) == ["port 11435 -> 8080"]

    def test_each_one_separately_so_a_port_does_not_hide_a_timeout(self, tmp_path):
        config = load_config(write_config_on_port(tmp_path, 8080))
        assert unapplied_settings(self.started(connect_timeout=30), config) == [
            "port 11435 -> 8080", "connect_timeout 30 -> 10"
        ]

    def test_a_key_the_pidfile_did_not_record_is_not_named(self, tmp_path):
        """It cannot be compared, and inventing a "from" value would put a
        number in front of the operator that nothing measured."""
        config = load_config(write_config_on_port(tmp_path, 8080))
        started = {"host": "127.0.0.1", "port": 11435}
        assert unapplied_settings(started, config) == ["port 11435 -> 8080"]

    def test_the_sentence_names_the_file_and_says_what_to_do(self, tmp_path):
        message = restart_warning(["port 11435 -> 8080"], tmp_path / "lmrelay.toml")
        assert "port 11435 -> 8080" in message
        assert str(tmp_path / "lmrelay.toml") in message
        assert "restart to apply" in message


class TestWaitingForTheRelayWeStarted:
    """Which pid `serve` reports, and what it does when the child does not live."""

    def test_a_pidfile_naming_another_relay_is_not_adopted(self, tmp_path, monkeypatch):
        """Two `serve` commands raced against each other both find a pidfile,
        and waiting for any pid rather than for this one lets the loser report
        the winner's relay as the one it started."""
        monkeypatch.setattr("lmrelay.daemon.START_TIMEOUT", 0.3)
        target = tmp_path / PID_NAME
        write_pid(target, os.getpid())
        with pytest.raises(LmrelayError) as raised:
            wait_for_relay(FOREIGN_PID, target, tmp_path / LOG_NAME)
        # The timeout branch by name: a dead pid would also raise, from the
        # "exited during startup" branch, and prove nothing about the wait.
        assert "no relay appeared" in str(raised.value)
        assert str(tmp_path / LOG_NAME) in str(raised.value)

    def test_a_child_that_died_is_reported_rather_than_waited_out(self, tmp_path):
        """The config is parsed and the port bound in the child, so a fork that
        worked proves nothing. Sitting out the full start timeout to then say
        nothing appeared would bury the reason."""
        with pytest.raises(LmrelayError, match="exited during startup"):
            wait_for_relay(dead_pid(), tmp_path / PID_NAME, tmp_path / LOG_NAME)

    def test_and_the_pid_that_recorded_itself_is_the_one_returned(self, tmp_path):
        target = tmp_path / PID_NAME
        write_pid(target, os.getpid())
        assert wait_for_relay(os.getpid(), target, tmp_path / LOG_NAME) == os.getpid()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="start_detached forks; Windows cannot")
class TestARealDetachedRelay:
    """The only test in the suite that starts a process. It also stops it."""

    def test_it_starts_answers_and_stops(self, tmp_path, monkeypatch):
        # The detached child inherits the environment, and this is how the CLI
        # passes --config on to a relay that loads the config itself. Set before
        # the start, since the helper may write the config more than once.
        monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "lmrelay.toml"))

        pid, config, port = start_relay_through_the_cli(tmp_path)
        try:
            assert process_alive(pid)
            healthy = holds_within(lambda: probe_health("127.0.0.1", port))
            # Recorded by the process that bound the socket, which is the only
            # thing that knows what it started with. Asserted here rather than
            # against write_pid alone, because the call site is what a `reload`
            # in another process reads to say what it cannot apply, and dropping
            # an argument there failed nothing.
            started = read_startup_settings(pid_file(config))
        finally:
            stopped = stop_daemon(config)
        assert healthy, read_log(config)
        assert started == {"host": "127.0.0.1", "port": port, "connect_timeout": 10}
        assert stopped
        assert read_pid(pid_file(config)) is None

    def test_starting_a_second_one_is_refused(self, tmp_path):
        """A second bind would fail later with a message about a port rather
        than about the relay already running."""
        config = write_config_on_port(tmp_path, free_port())
        write_pid(pid_file(config), os.getpid())
        with pytest.raises(LmrelayError):
            start_detached(config, "127.0.0.1", 11435)
        assert read_pid(pid_file(config)) == os.getpid()


def main():
    pass


if __name__ == "__main__":
    main()
