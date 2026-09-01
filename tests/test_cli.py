#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The command surface, driven through the parser without starting anything."""

import logging
import subprocess
import sys

import pytest

# Local imports
from lmrelay import cli, service
from lmrelay.cli import build_parser
from lmrelay.config import CONFIG_ENV_VAR
from lmrelay.errors import LmrelayError
from lmrelay.service import LAUNCHD_PLIST_PATH
from lmrelay.state import STATE_ENV_VAR, TOKEN_PREFIX, load_state, state_path_for

CONFIG_BODY = """
[server]
host = "127.0.0.1"
port = 11435

[upstream.ollama]
base_url = "http://127.0.0.1:11434"
dialect  = "ollama"
"""

# Every command in the documented surface, in the forms an operator types.
DOCUMENTED = [
    ["init"],
    ["run"],
    ["run", "--host", "0.0.0.0", "--port", "9000"],
    ["serve"],
    ["stop"],
    ["restart"],
    ["reload"],
    ["status"],
    ["enable"],
    ["disable"],
    ["auth", "true"],
    ["auth", "false"],
    ["token", "gen"],
    ["token", "gen", "--label", "laptop"],
    ["token", "add", "lmr_pasted"],
    ["token", "add", "lmr_pasted", "--label", "ci"],
    ["token", "list"],
    ["token", "list", "--show"],
    ["token", "delete", "3"],
    ["provider", "add", "openai", "sk-test"],
    ["provider", "add", "acme", "tok", "--base-url", "https://acme.test",
     "--dialect", "openai", "--header", "X-Trace=on", "--header", "X-Team=ml"],
    ["provider", "list"],
    ["provider", "list", "--show"],
    ["provider", "delete", "openai"],
    ["limits", "set", "total", "--concurrent", "6"],
    ["limits", "set", "per_token", "--rate", "2", "--burst", "5"],
    ["limits", "set", "per_address", "--rate", "0.5"],
    ["config", "export", "relay.json"],
    ["config", "export", "relay.json", "--no-secrets"],
    ["config", "export", "relay.json", "--force"],
    ["config", "export", "-"],
    ["config", "import", "relay.json"],
    ["config", "import", "relay.json", "--force"],
    ["config", "import", "-"],
]

# The ones that touch the config or the state, and so have to accept --config.
CONFIGURABLE = [
    ["run"], ["serve"], ["stop"], ["restart"], ["reload"], ["status"], ["enable"],
    ["auth", "true"], ["token", "gen"], ["token", "add", "lmr_pasted"], ["token", "list"],
    ["token", "delete", "3"], ["provider", "add", "openai", "sk-test"], ["provider", "list"],
    ["provider", "delete", "openai"], ["limits", "set", "total", "--concurrent", "6"],
    ["config", "export", "relay.json"], ["config", "import", "relay.json"],
]


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """No handler here may find the operator's own config or state, and none may
    reach a real service manager: a systemctl call would act on their session."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "absent.toml"))
    monkeypatch.delenv(STATE_ENV_VAR, raising=False)
    monkeypatch.setattr(service, "detect_manager", lambda: "none")

    def refuse(argv, **unused_kwargs):
        # A non-zero code rather than a raise, so a status line still renders if
        # something reaches this despite the manager above reporting none.
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(service.subprocess, "run", refuse)


@pytest.fixture
def config_path(tmp_path):
    """A valid config of its own, so the state lands beside it and not in $HOME."""
    target = tmp_path / "lmrelay.toml"
    target.write_text(CONFIG_BODY, encoding="utf-8")
    return target


def run_command(argv: list[str]) -> None:
    """Parse and dispatch exactly as main() does, minus its exit handling."""
    args = build_parser().parse_args(argv)
    args.handler(args)


def state_for(config_path):
    """The state the last command wrote, read back from disk."""
    return load_state(state_path_for(config_path))


class TestTheCommandSurface:
    """Parsing only: nothing here runs, forks or binds."""

    @pytest.mark.parametrize("argv", DOCUMENTED, ids=" ".join)
    def test_every_documented_command_selects_a_handler(self, argv):
        assert callable(build_parser().parse_args(argv).handler)

    @pytest.mark.parametrize("argv", CONFIGURABLE, ids=" ".join)
    def test_and_takes_the_config_it_should_act_on(self, argv, tmp_path):
        """--config has to reach app.py, which loads the config itself, so every
        command that touches one accepts it."""
        target = tmp_path / "elsewhere.toml"
        args = build_parser().parse_args([*argv, "--config", str(target)])
        assert str(args.config) == str(target)

    def test_a_group_with_no_verb_is_refused(self):
        """`lmrelay token` alone is an unfinished command, not a command."""
        with pytest.raises(SystemExit) as raised:
            build_parser().parse_args(["token"])
        assert raised.value.code != 0

    def test_the_auth_switch_takes_only_true_or_false(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["auth", "on"])


class TestTurningAuthOn:
    """The switch, and the one state it refuses to be put into."""

    def test_with_no_tokens_it_is_refused(self, config_path):
        """It would 401 every request, including the operator's own."""
        with pytest.raises(LmrelayError) as raised:
            run_command(["auth", "true", "--config", str(config_path)])
        assert "token gen" in str(raised.value)

    def test_and_the_command_exits_non_zero(self, config_path, monkeypatch):
        monkeypatch.setattr(
            sys, "argv", ["lmrelay", "auth", "true", "--config", str(config_path)]
        )
        with pytest.raises(SystemExit) as raised:
            cli.main()
        assert raised.value.code == 1

    def test_a_token_in_the_config_file_counts_as_a_token(self, config_path):
        """[auth] token is a valid credential, so auth on with one configured
        does not 401 everybody, and load_config warns about exactly this setup
        by telling the operator to run the command that used to refuse it."""
        config_path.write_text(CONFIG_BODY + '\n[auth]\ntoken = "from-the-toml"\n', encoding="utf-8")
        run_command(["auth", "true", "--config", str(config_path)])
        assert state_for(config_path).auth_enabled is True

    def test_with_a_token_it_is_written_down(self, config_path):
        run_command(["token", "add", "lmr_pasted", "--config", str(config_path)])
        run_command(["auth", "false", "--config", str(config_path)])
        run_command(["auth", "true", "--config", str(config_path)])
        assert state_for(config_path).auth_enabled is True

    def test_turning_it_off_is_too(self, config_path):
        run_command(["token", "add", "lmr_pasted", "--config", str(config_path)])
        run_command(["auth", "false", "--config", str(config_path)])
        assert state_for(config_path).auth_enabled is False


class TestCallerTokens:
    """gen, add, delete, and what the first one does to the switch."""

    def test_the_first_token_is_stored_without_being_required(self, config_path):
        """Minting a credential is not the same decision as requiring one, and
        `lmrelay auth true` is where the second one is made."""
        run_command(["token", "gen", "--config", str(config_path)])
        state = state_for(config_path)
        assert len(state.tokens) == 1 and state.auth_enabled is False

    def test_a_generated_token_is_recognisably_ours(self, config_path):
        run_command(["token", "gen", "--config", str(config_path)])
        assert state_for(config_path).tokens[0].token.startswith(TOKEN_PREFIX)

    def test_the_label_is_kept(self, config_path):
        run_command(["token", "gen", "--label", "laptop", "--config", str(config_path)])
        assert state_for(config_path).tokens[0].label == "laptop"

    def test_a_pasted_token_is_stored_as_it_was_given(self, config_path):
        run_command(["token", "add", "lmr_pasted", "--config", str(config_path)])
        assert state_for(config_path).tokens[0].token == "lmr_pasted"

    def test_the_same_token_twice_is_refused(self, config_path):
        run_command(["token", "add", "lmr_pasted", "--config", str(config_path)])
        with pytest.raises(LmrelayError):
            run_command(["token", "add", "lmr_pasted", "--config", str(config_path)])

    def test_minting_a_token_does_not_start_requiring_one(self, config_path):
        """Two decisions, two commands: a relay already serving other callers
        must not begin refusing them because a token was created."""
        run_command(["token", "gen", "--config", str(config_path)])
        assert state_for(config_path).auth_enabled is False

    def test_a_later_token_leaves_the_switch_where_the_operator_put_it(self, config_path):
        run_command(["token", "gen", "--config", str(config_path)])
        run_command(["auth", "true", "--config", str(config_path)])
        run_command(["token", "gen", "--config", str(config_path)])
        assert state_for(config_path).auth_enabled is True

    def test_deleting_by_the_id_that_was_printed(self, config_path):
        run_command(["token", "gen", "--config", str(config_path)])
        token_id = state_for(config_path).tokens[0].id
        run_command(["token", "delete", str(token_id), "--config", str(config_path)])
        assert state_for(config_path).tokens == ()

    def test_deleting_an_id_that_is_not_there_is_refused(self, config_path):
        with pytest.raises(LmrelayError):
            run_command(["token", "delete", "7", "--config", str(config_path)])


class TestProviders:
    """Adding a hosted provider without opening the TOML."""

    def test_a_preset_needs_only_a_name_and_a_key(self, config_path):
        run_command(["provider", "add", "openai", "sk-test", "--config", str(config_path)])
        provider = state_for(config_path).providers["openai"]
        assert provider["base_url"] == "https://api.openai.com"
        assert provider["headers"]["Authorization"] == "Bearer sk-test"

    def test_the_header_flag_can_be_repeated(self, config_path):
        run_command([
            "provider", "add", "acme", "tok", "--base-url", "https://acme.test",
            "--header", "X-Trace=on", "--header", "X-Team=ml", "--config", str(config_path),
        ])
        headers = state_for(config_path).providers["acme"]["headers"]
        assert headers["X-Trace"] == "on" and headers["X-Team"] == "ml"

    def test_a_header_value_keeps_the_equals_signs_inside_it(self, config_path):
        """Only the first = separates the name from the value, and a base64
        value ends in one."""
        run_command([
            "provider", "add", "acme", "tok", "--base-url", "https://acme.test",
            "--header", "X-Blob=a=b==", "--config", str(config_path),
        ])
        assert state_for(config_path).providers["acme"]["headers"]["X-Blob"] == "a=b=="

    def test_one_the_cli_added_can_be_deleted_again(self, config_path):
        run_command(["provider", "add", "openai", "sk-test", "--config", str(config_path)])
        run_command(["provider", "delete", "openai", "--config", str(config_path)])
        assert "openai" not in state_for(config_path).providers

    def test_a_hand_written_upstream_is_refused_and_the_file_is_named(self, config_path):
        """It is visible in `provider list`, so a silent no-op would read as a
        bug. The operator is told which file to edit instead."""
        with pytest.raises(LmrelayError) as raised:
            run_command(["provider", "delete", "ollama", "--config", str(config_path)])
        assert "lmrelay.toml" in str(raised.value)


class TestHandingOverToTheServiceManager:
    """When one owns the process, the CLI asks it rather than the pidfile."""

    def launchctl_calls(self, monkeypatch) -> list[list[str]]:
        calls: list[list[str]] = []
        monkeypatch.setattr(cli, "detect_manager", lambda: "launchd")

        def record(argv, **unused_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(cli.subprocess, "run", record)
        return calls

    def test_stopping_under_launchd_does_not_disable_the_agent(self, config_path, monkeypatch):
        """`unload -w` writes the job into launchd's disabled database, which
        survives a reboot. A stop is meant to be temporary; only `lmrelay
        disable` decides the relay stops coming back at login."""
        calls = self.launchctl_calls(monkeypatch)
        cli.service_control("stop", config_path)
        assert calls == [["launchctl", "unload", str(LAUNCHD_PLIST_PATH)]]

    def test_and_neither_does_restarting_it(self, config_path, monkeypatch):
        calls = self.launchctl_calls(monkeypatch)
        cli.service_control("restart", config_path)
        assert all("-w" not in argv for argv in calls)


class TestSayingWhatWasDone:
    """The CLI reports what it did, not what it hopes followed."""

    def test_a_reload_is_reported_as_a_signal_rather_than_as_a_result(
        self, config_path, monkeypatch, caplog
    ):
        """SIGHUP is delivered, not acknowledged: a relay that cannot parse what
        it re-reads keeps the config it had. Claiming the change is live would
        tell an operator who just revoked a token that the revocation took."""
        monkeypatch.setattr(cli, "reload_daemon", lambda unused_path: True)
        with caplog.at_level(logging.INFO):
            cli.reload_running_relay(config_path)
        assert "SIGHUP" in caplog.text
        assert "is live" not in caplog.text


class TestReporting:
    """The commands that only print. Completing is the whole assertion."""

    def test_the_status_of_a_stopped_relay_is_not_a_failure(self, config_path):
        """Exit code 0 either way: `status` reports, it does not assert."""
        run_command(["status", "--config", str(config_path)])

    def test_it_says_which_limits_are_in_force(self, config_path, capsys):
        """Nothing showed an operator the limits at all: not `status`, not the
        startup log. The only way to read the numbers in effect was to export
        the whole configuration and read the bundle."""
        config_path.write_text(
            CONFIG_BODY + "\n[limits.per_address]\nrate = 2\nconcurrent = 4\n"
            "\n[limits.total]\nconcurrent = 6\n",
            encoding="utf-8",
        )
        run_command(["status", "--config", str(config_path)])
        printed = capsys.readouterr()
        assert "per_address 2/s burst 2, 4 at once; total 6 at once" in printed.err

    def test_and_says_off_once_when_none_is(self, config_path, capsys):
        """One line answering the question that was asked, rather than three
        saying off about scopes nobody set."""
        run_command(["status", "--config", str(config_path)])
        assert "limits       off" in capsys.readouterr().err

    def test_listing_an_empty_token_set_is_not_a_failure(self, config_path):
        run_command(["token", "list", "--config", str(config_path)])

    def test_listing_providers_from_the_file_and_the_state_together_is_not_a_failure(
        self, config_path
    ):
        run_command(["provider", "add", "openai", "sk-test", "--config", str(config_path)])
        run_command(["provider", "list", "--config", str(config_path)])
