#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Startup and SIGHUP reloads of a running relay."""

import logging
import os

import pytest
from starlette.testclient import TestClient

# Local imports
from lmrelay.config import CONFIG_ENV_VAR, ConfigError
from lmrelay.daemon import PID_NAME, read_pid, write_pid
from lmrelay.state import STATE_NAME
from tests.conftest import (
    CONFIG_TEMPLATE,
    TESTCLIENT_KEY,
    TOKEN,
    answer_in_flight,
    build_relay,
    config_limits,
    wait_until,
    write_config,
    write_state,
)

EXTRA_UPSTREAM = """
[upstream.second]
base_url = "http://second.invalid:11434"
dialect  = "ollama"
"""


def with_setting(body: str, name: str, value: str) -> str:
    """Set one [server] key in a config body, replacing its line if it has one.

    Takes the body rather than building it so that a test needing two keys, as
    the rate limit does, can apply this twice instead of hard-coding a config.
    """
    setting = f"{name} = {value}"
    for line in body.splitlines():
        if line.startswith(f"{name} "):
            return body.replace(line, setting)
    return body.replace("[server]", f"[server]\n{setting}")


def config_where(name: str, value: str) -> str:
    """The standard config with one [server] key set to something a test needs.

    Spelled once here because the template's columns are aligned: a test that
    hard-codes that spacing breaks on a change to a config it does not care
    about. A key the template omits, log_level, is added rather than replaced.
    """
    return with_setting(CONFIG_TEMPLATE.format(token=TOKEN), name, value)


class TestStartup:
    """What the process does before it serves anything."""

    def test_a_broken_config_stops_it_from_starting(self, tmp_path, monkeypatch, recorder):
        """Better than a process that binds a port and answers every request
        with the same 500."""
        bad = tmp_path / "lmrelay.toml"
        bad.write_text('[server]\nhost = "127.0.0.1"\n', encoding="utf-8")
        monkeypatch.setenv(CONFIG_ENV_VAR, str(bad))
        from lmrelay.app import app

        with pytest.raises(ConfigError, match=r"\[upstream"), TestClient(app):
            pass

    def test_so_does_a_relay_already_running_on_the_same_config(self, tmp_path, monkeypatch):
        """Refused before the bind. Left to the bind, the second relay would die
        of an "address already in use" that names neither the process holding
        the port nor the command that would have stopped or restarted it."""
        from lmrelay.app import app

        monkeypatch.setenv(CONFIG_ENV_VAR, str(write_config(tmp_path, CONFIG_TEMPLATE.format(
            token=TOKEN
        ))))
        # This process, so the pid in the file is one that is certainly alive.
        write_pid(tmp_path / PID_NAME, os.getpid())
        with pytest.raises(ConfigError, match="already running"), TestClient(app):
            pass

    def test_a_port_anyone_can_reach_is_warned_about_as_it_starts(
        self, tmp_path, monkeypatch, caplog
    ):
        """No state file, so auth is off, on a wildcard bind: the moment the
        operator who wrote that config is still at the terminal to read it. The
        reload path says the same thing, and is tested with it; this is the
        first start, where a relay that stayed quiet would look deliberate."""
        from lmrelay.app import app

        monkeypatch.setenv(
            CONFIG_ENV_VAR, str(write_config(tmp_path, config_where("host", '"0.0.0.0"')))
        )
        with caplog.at_level(logging.WARNING), TestClient(app):
            pass
        assert "caller that can reach this port" in caplog.text

    def test_it_records_its_pid_and_clears_it_again(self, tmp_path, monkeypatch):
        from lmrelay.app import app

        monkeypatch.setenv(CONFIG_ENV_VAR, str(write_config(tmp_path, CONFIG_TEMPLATE.format(
            token=TOKEN
        ))))
        pidfile = tmp_path / PID_NAME
        with TestClient(app):
            assert read_pid(pidfile) == os.getpid()
        assert read_pid(pidfile) is None

    def test_but_it_does_not_clear_one_another_relay_has_claimed(self, tmp_path, monkeypatch):
        """uvicorn runs the shutdown half of the lifespan when the bind fails
        too, so a relay that never served would otherwise delete the pidfile of
        the live relay that beat it to the port, leaving a running relay that
        `status` calls stopped and `stop` cannot find."""
        from lmrelay.app import app

        monkeypatch.setenv(CONFIG_ENV_VAR, str(write_config(tmp_path, CONFIG_TEMPLATE.format(
            token=TOKEN
        ))))
        pidfile = tmp_path / PID_NAME
        with TestClient(app):
            # Alive, and not this process: exactly what a relay that lost the
            # race to the port finds in the file it is about to unlink.
            write_pid(pidfile, os.getppid())
        assert read_pid(pidfile) == os.getppid()


class TestReloadingInPlace:
    """What SIGHUP does, called directly: the config changes under a live relay."""

    def test_an_upstream_added_to_the_file_is_in_effect_without_a_restart(
        self, authed, recorder, tmp_path
    ):
        from lmrelay.app import app, reload_config

        write_config(tmp_path, CONFIG_TEMPLATE.format(token=TOKEN) + EXTRA_UPSTREAM)
        reload_config(app)
        assert authed.post("/second/api/chat", json={}).status_code == 200
        assert str(recorder.last.url) == "http://second.invalid:11434/api/chat"

    def test_the_client_is_kept_so_answers_in_flight_survive(self, authed, tmp_path):
        """Closing it would abort every stream being relayed, and nothing a
        reload can change is baked into it: URLs and headers are read per
        request from the config that just moved."""
        from lmrelay.app import app, reload_config

        before = app.state.http
        write_config(tmp_path, CONFIG_TEMPLATE.format(token=TOKEN) + EXTRA_UPSTREAM)
        reload_config(app)
        assert app.state.http is before

    def test_a_broken_file_leaves_the_running_config_in_place(self, authed, tmp_path):
        """A half-saved edit must not take the relay down: the operator is at a
        text editor, not at the terminal watching for a crash."""
        from lmrelay.app import app, reload_config

        before = app.state.config
        write_config(tmp_path, "this is not = = toml")
        reload_config(app)
        assert app.state.config is before
        assert authed.post("/api/chat", json={}).status_code == 200

    def test_a_broken_state_file_is_reported_the_same_way_and_not_raised(
        self, authed, tmp_path, caplog
    ):
        """load_config reads state.json too, and its StateError is a sibling of
        ConfigError rather than a subclass. Catching only ConfigError would let
        it escape the asyncio signal callback as a traceback while `token gen`
        had already told the operator the change had been picked up."""
        from lmrelay.app import app, reload_config

        before = app.state.config
        (tmp_path / STATE_NAME).write_text('{"version": 1, "auth_enabled": tru', encoding="utf-8")
        with caplog.at_level(logging.ERROR):
            reload_config(app)
        assert app.state.config is before
        assert "keeping the running config" in caplog.text

    def test_a_wrong_typed_value_is_discarded_like_a_syntax_error(self, authed, tmp_path, caplog):
        """`port = "eleven"` parses as TOML and fails at int(). A bare ValueError
        is not the LmrelayError this catches, so it left the signal handler as a
        traceback while the CLI had already reported the signal sent."""
        from lmrelay.app import app, reload_config

        before = app.state.config
        write_config(tmp_path, config_where("port", '"eleven"'))
        with caplog.at_level(logging.ERROR):
            reload_config(app)
        assert app.state.config is before
        assert "keeping the running config" in caplog.text

    def test_the_warning_names_only_the_bind_setting_that_changed(self, authed, tmp_path, caplog):
        """An operator who moved the port has to be able to tell that
        connect_timeout did not also drift, so one fixed sentence naming all
        three answers a question nobody asked."""
        from lmrelay.app import app, reload_config

        moved = CONFIG_TEMPLATE.format(token=TOKEN).replace("port             = 11435", "port = 11439")
        write_config(tmp_path, moved)
        with caplog.at_level(logging.WARNING):
            reload_config(app)
        assert "port 11435 -> 11439" in caplog.text
        assert "connect_timeout" not in caplog.text

    def test_and_it_quotes_the_value_it_is_still_bound_to(self, authed, tmp_path, caplog):
        """The old value is the half of this only the running relay knows. Named
        alone, the warning sent an operator to the file to read what they had
        just written there, and left the number the socket is actually on
        nowhere at all."""
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_where("connect_timeout", "42"))
        with caplog.at_level(logging.WARNING):
            reload_config(app)
        assert "connect_timeout 10 -> 42" in caplog.text

    def test_and_it_keeps_saying_so_on_every_later_reload(self, authed, tmp_path, caplog):
        """The socket is still where it was bound, so a second reload that leaves
        the port where the first one put it has not made the two agree. Measured
        against the last config read, this warning went quiet after one reload
        and left the operator believing the move had landed."""
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_where("port", "11439"))
        reload_config(app)
        # Cleared, or the first reload's warning would answer for the second.
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            reload_config(app)
        assert "port 11435 -> 11439" in caplog.text

    def test_and_putting_it_back_stops_the_warning(self, authed, tmp_path, caplog):
        """The other half of the same baseline: a file that once again names the
        bound port needs no restart, and saying it does would send an operator to
        undo something they had already undone."""
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_where("port", "11439"))
        reload_config(app)
        write_config(tmp_path, CONFIG_TEMPLATE.format(token=TOKEN))
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            reload_config(app)
        assert "port" not in caplog.text

    def test_a_rate_limit_can_be_turned_on_without_a_restart(self, authed, tmp_path):
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_limits(per_address='concurrent = 3\nrate = "3/1h"'))
        reload_config(app)
        codes = [authed.get("/api/tags").status_code for unused in range(4)]
        assert codes == [200, 200, 200, 429]

    def test_and_off_again(self, authed, tmp_path):
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_limits(per_address='concurrent = 3\nrate = "3/1h"'))
        reload_config(app)
        for unused in range(3):
            authed.get("/api/tags")
        assert authed.get("/api/tags").status_code == 429

        write_config(tmp_path, config_limits(per_address="concurrent = 0"))
        reload_config(app)
        assert authed.get("/api/tags").status_code == 200

    def test_an_unrelated_reload_leaves_the_limiter_alone(self, limited, tmp_path):
        """A fresh limiter starts every caller with a full bucket, so rebuilding
        it on a reload that changed something else would hand the allowance back
        to whoever was being limited at that moment: editing an upstream URL
        would be a way to clear the limit."""
        from lmrelay.app import app, reload_config

        for unused in range(3):
            limited.get("/api/tags")
        assert limited.get("/api/tags").status_code == 429

        write_config(tmp_path, with_setting(
            config_limits(per_address='concurrent = 3\nrate = "3/1h"'), "log_level", '"INFO"'
        ))
        reload_config(app)
        assert limited.get("/api/tags").status_code == 429

    def test_but_a_changed_number_rebuilds_it(self, limited, tmp_path):
        """The other half: the numbers cannot move without a new table, since a
        bucket holds an allowance measured against the old burst."""
        from lmrelay.app import app, reload_config

        for unused in range(3):
            limited.get("/api/tags")
        assert limited.get("/api/tags").status_code == 429

        write_config(tmp_path, config_limits(per_address='concurrent = 9\nrate = "9/1h"'))
        reload_config(app)
        codes = [limited.get("/api/tags").status_code for unused in range(10)]
        assert codes == [200] * 9 + [429]

    def test_and_only_the_scope_whose_number_moved(self, limited, tmp_path, recorder):
        """Three tables, and a caller being limited by one of them has no
        business getting its allowance back because a different number moved.
        Rebuilt as one block, raising the total would have cleared the address
        allowance of whoever was being refused at that moment."""
        from lmrelay.app import app, reload_config

        for unused in range(3):
            limited.get("/api/tags")
        assert limited.get("/api/tags").status_code == 429

        write_config(tmp_path, config_limits(
            per_address='concurrent = 3\nrate = "3/1h"', total='concurrent = 50\nrate = "50/1h"'
        ))
        reload_config(app)
        assert limited.get("/api/tags").status_code == 429

    def test_the_reload_log_names_the_scope_and_both_halves(
        self, authed, tmp_path, caplog
    ):
        """Read back afterwards to check what took, and one line per scope
        rather than one per measure: the number is the same number doing two
        jobs, and two lines about it read as two settings."""
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_limits(total='concurrent = 20\nrate = "20/1m"'))
        with caplog.at_level(logging.INFO):
            reload_config(app)
        assert "[limits.total] off -> 20/1m, 20 at once" in caplog.text

    def test_a_changed_cap_is_in_force_without_a_restart(self, authed, recorder, tmp_path):
        """Read from the config at every request, like the upstreams and the
        tokens, so a reload is the whole of applying it."""
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_limits(total="concurrent = 1"))
        reload_config(app)
        with answer_in_flight(authed, recorder):
            assert authed.post("/api/tags").status_code == 429

    def test_and_it_is_not_named_as_needing_one(self, authed, tmp_path, caplog):
        """Listing it beside host and port would send an operator to restart a
        relay that had already done what they asked."""
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_limits(total="concurrent = 2"))
        with caplog.at_level(logging.WARNING):
            reload_config(app)
        assert "restart to apply" not in caplog.text

    def test_the_counter_survives_the_reload_with_its_live_slots(
        self, capped, recorder, tmp_path
    ):
        """Rebuilding it, as the rate limiter is rebuilt, would forget the slot
        every streaming answer holds, and each of those would then release one
        that was no longer recorded, leaving the caller a free slot per answer
        that happened to be running when somebody edited the file."""
        from lmrelay.app import app, reload_config

        before = app.state.inflight
        with answer_in_flight(capped, recorder):
            write_config(tmp_path, config_limits(per_address="concurrent = 4"))
            reload_config(app)
            assert app.state.inflight is before
            assert app.state.inflight.counts == {TESTCLIENT_KEY: 1}
        assert app.state.inflight.counts == {}

    def test_and_the_new_number_governs_the_next_request(self, capped, recorder, tmp_path):
        """The other half: the answer in flight keeps its slot under the old
        number, and the raised cap admits the request that arrives after it."""
        from lmrelay.app import app, reload_config

        with answer_in_flight(capped, recorder) as held:
            assert capped.post("/api/tags").status_code == 429
            write_config(tmp_path, config_limits(per_address="concurrent = 2"))
            reload_config(app)
            admitted = held.pool.submit(capped.post, "/api/tags")
            wait_until(
                lambda: len(recorder.requests) == 2, "the raised cap admitted nothing"
            )
        assert admitted.result(timeout=10).status_code == 200

    def test_a_scope_turned_off_under_a_live_answer_still_gets_its_slot_back(
        self, capped, recorder, tmp_path
    ):
        """The release covers the set taken at admission rather than the set the
        config names on the way out. Recomputed, a scope turned off between the
        two would leave its count standing for the life of the process."""
        from lmrelay.app import app, reload_config

        with answer_in_flight(capped, recorder):
            write_config(tmp_path, config_limits(per_address="concurrent = 0"))
            reload_config(app)
            assert app.state.inflight.counts == {TESTCLIENT_KEY: 1}
        assert app.state.inflight.counts == {}

    def test_a_changed_log_level_is_in_force_for_the_next_line(
        self, authed, tmp_path, logging_restored
    ):
        """The one [server] key a reload can honour: a logger can be
        reconfigured under a live process, where a bound socket cannot."""
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_where("log_level", '"DEBUG"'))
        reload_config(app)
        assert logging.getLogger().level == logging.DEBUG

    def test_and_it_is_not_named_as_needing_a_restart(
        self, authed, tmp_path, caplog, logging_restored
    ):
        """It is applied, so listing it beside host and port would send the
        operator to restart a relay that had already done what they asked."""
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_where("log_level", '"DEBUG"'))
        with caplog.at_level(logging.WARNING):
            reload_config(app)
        assert "log_level" not in caplog.text

    def test_a_level_logging_does_not_know_is_refused_rather_than_announced(
        self, authed, tmp_path, caplog, logging_restored
    ):
        """getattr(logging, name) falls back on its own, so an unknown level was
        announced as applied and then quietly dropped: the relay ran at INFO
        while the log said it was running at something else."""
        from lmrelay.app import app, reload_config

        before = app.state.config
        write_config(tmp_path, config_where("log_level", '"verbose"'))
        with caplog.at_level(logging.ERROR):
            reload_config(app)
        assert app.state.config is before
        assert "is not a logging level" in caplog.text


class TestReloadingARelayAnyoneCanReach:
    """The bind is public, so the auth switch decides whether the upstream
    credentials are. A reload can move that switch, which makes it one of the
    ways to create the condition the startup check exists to warn about."""

    @pytest.fixture
    def public_relay(self, tmp_path, monkeypatch, recorder):
        monkeypatch.setenv(CONFIG_ENV_VAR, str(write_config(
            tmp_path, config_where("host", '"0.0.0.0"')
        )))
        write_state(tmp_path, auth_enabled=True, tokens=(TOKEN,))
        yield from build_relay(recorder)

    def test_turning_auth_off_says_the_relay_is_now_open(self, public_relay, tmp_path, caplog):
        """`lmrelay auth false` opens a relay that is already listening, and the
        operator who ran it is the one who most needs telling."""
        from lmrelay.app import app, reload_config

        write_state(tmp_path, auth_enabled=False, tokens=(TOKEN,))
        with caplog.at_level(logging.WARNING):
            reload_config(app)
        assert "caller that can reach this port" in caplog.text

    def test_and_editing_the_host_back_does_not_quiet_it(self, public_relay, tmp_path, caplog):
        """Asked about the host the socket is on, not the one now in the file:
        the relay is still bound to 0.0.0.0 until a restart, so a config that
        says 127.0.0.1 has closed nothing yet."""
        from lmrelay.app import app, reload_config

        write_config(tmp_path, CONFIG_TEMPLATE.format(token=TOKEN))
        write_state(tmp_path, auth_enabled=False, tokens=(TOKEN,))
        with caplog.at_level(logging.WARNING):
            reload_config(app)
        assert "caller that can reach this port" in caplog.text


def main():
    pass


if __name__ == "__main__":
    main()
