#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`lmrelay limits set`: one number changed in the operator's own file."""

import logging

import pytest

# Local imports
from lmrelay import service
from lmrelay.cli import build_parser
from lmrelay.config import CONFIG_ENV_VAR, load_config
from lmrelay.errors import ConfigError, LmrelayError
from lmrelay.state import STATE_ENV_VAR
from tests.conftest import write_state

# The shape `lmrelay init` writes: a commented file with all three scopes
# spelled out and every number off. What survives an edit to it is the point of
# most of the tests below.
CONFIG_BODY = """\
# lmrelay configuration. These comments are the operator's.

[server]
host = "127.0.0.1"
port = 11435

# Per credential. Skipped entirely with auth off.
[limits.per_token]
rate       = 0
burst      = 0
concurrent = 0

# The relay as a whole, whoever is asking.
[limits.total]
rate       = 0
burst      = 0
concurrent = 0

[upstream.ollama]
base_url = "http://127.0.0.1:11434"
dialect  = "ollama"
"""


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """No test here may find the operator's own files or reach a service manager."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "absent.toml"))
    monkeypatch.delenv(STATE_ENV_VAR, raising=False)
    monkeypatch.setattr(service, "detect_manager", lambda: "none")


@pytest.fixture
def config_path(tmp_path):
    """A config of its own, so the state lands beside it and not in $HOME."""
    target = tmp_path / "lmrelay.toml"
    target.write_text(CONFIG_BODY, encoding="utf-8")
    return target


def run_command(argv: list[str]) -> None:
    """Parse and dispatch exactly as main() does, minus its exit handling."""
    args = build_parser().parse_args(argv)
    args.handler(args)


def set_limit(config_path, scope: str, *flags: str) -> None:
    """`lmrelay limits set <scope> ...` against a given config."""
    run_command(["limits", "set", scope, *flags, "--config", str(config_path)])


def limits_of(config_path, scope: str):
    """What the relay would enforce for one scope, read back through the loader."""
    return load_config(config_path).limits[scope]


def changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    """Every line that is not identical, paired old with new."""
    old, new = before.splitlines(), after.splitlines()
    assert len(old) == len(new), "a line was added or removed"
    return [pair for pair in zip(old, new, strict=True) if pair[0] != pair[1]]


class TestWritingANumber:
    """The number lands in the file, and the loader reads back what was asked for."""

    def test_a_count(self, config_path):
        set_limit(config_path, "total", "--concurrent", "6")
        assert limits_of(config_path, "total").concurrent == 6

    def test_a_rate_and_its_burst_together(self, config_path):
        """One command, one write, one reload: rate and burst are two halves of
        the same decision and setting them in two goes leaves the relay running
        on half of it in between."""
        set_limit(config_path, "per_token", "--rate", "2", "--burst", "5")
        limits = limits_of(config_path, "per_token")
        assert (limits.rate, limits.burst) == (2.0, 5.0)

    def test_a_fraction_stays_a_fraction(self, config_path):
        """`rate = 0.5` is one request every two seconds, and rounding it to
        zero would silently turn the limit off."""
        set_limit(config_path, "total", "--rate", "0.5")
        assert limits_of(config_path, "total").rate == 0.5
        assert "0.5" in config_path.read_text(encoding="utf-8")

    def test_a_whole_number_is_written_without_a_decimal_point(self, config_path):
        """It is read as a float and written back as what the operator typed;
        `rate = 2.0` in a file whose other numbers are bare reads as a different
        kind of setting."""
        set_limit(config_path, "total", "--rate", "2")
        assert "rate       = 2\n" in config_path.read_text(encoding="utf-8")

    def test_zero_turns_one_off(self, config_path):
        set_limit(config_path, "total", "--concurrent", "6")
        set_limit(config_path, "total", "--concurrent", "0")
        assert limits_of(config_path, "total").concurrent == 0

    def test_and_leaves_the_scope_alone_where_it_was_not_asked(self, config_path):
        set_limit(config_path, "total", "--rate", "3", "--burst", "9", "--concurrent", "4")
        set_limit(config_path, "total", "--concurrent", "1")
        limits = limits_of(config_path, "total")
        assert (limits.rate, limits.burst, limits.concurrent) == (3.0, 9.0, 1)

    def test_one_scope_does_not_touch_another(self, config_path):
        set_limit(config_path, "total", "--concurrent", "6")
        set_limit(config_path, "per_token", "--concurrent", "2")
        assert limits_of(config_path, "total").concurrent == 6
        assert limits_of(config_path, "per_token").concurrent == 2


class TestTheFileIsStillTheOperators:
    """This is the one command that writes lmrelay.toml, and it writes one line."""

    def test_only_the_line_it_was_asked_about_moves(self, config_path):
        """`lmrelay init` ships this file with sixty lines of comment explaining
        the numbers. A command that dropped them to change one is a command
        nobody runs twice."""
        before = config_path.read_text(encoding="utf-8")
        set_limit(config_path, "total", "--concurrent", "6")
        after = config_path.read_text(encoding="utf-8")
        assert changed_lines(before, after) == [("concurrent = 0", "concurrent = 6")]

    def test_two_keys_move_two_lines_and_no_more(self, config_path):
        before = config_path.read_text(encoding="utf-8")
        set_limit(config_path, "per_token", "--rate", "2", "--burst", "5")
        after = config_path.read_text(encoding="utf-8")
        assert changed_lines(before, after) == [
            ("rate       = 0", "rate       = 2"),
            ("burst      = 0", "burst      = 5"),
        ]

    def test_a_note_beside_the_number_survives_it(self, config_path):
        """A number changed by a command is exactly the number somebody wrote a
        reason beside."""
        body = config_path.read_text(encoding="utf-8").replace(
            "concurrent = 0\n\n[upstream", "concurrent = 0   # sized for the 3090\n\n[upstream"
        )
        config_path.write_text(body, encoding="utf-8")
        set_limit(config_path, "total", "--concurrent", "6")
        assert "concurrent = 6   # sized for the 3090" in config_path.read_text(encoding="utf-8")

    def test_the_file_keeps_its_private_mode(self, config_path):
        """It is meant to hold provider keys, and every other writer of it
        leaves it 0600."""
        config_path.chmod(0o600)
        set_limit(config_path, "total", "--concurrent", "6")
        assert config_path.stat().st_mode & 0o777 == 0o600

    def test_a_key_the_scope_has_not_got_is_added_to_it(self, tmp_path):
        target = tmp_path / "lmrelay.toml"
        target.write_text(
            "[limits.total]\nrate       = 5\n\n"
            '[upstream.ollama]\nbase_url = "http://127.0.0.1:11434"\n',
            encoding="utf-8",
        )
        set_limit(target, "total", "--concurrent", "2")
        body = target.read_text(encoding="utf-8")
        # Written into the scope it belongs to, aligned like its neighbour, and
        # before the blank line that separates the tables.
        assert "rate       = 5\nconcurrent = 2\n\n[upstream.ollama]" in body

    def test_a_scope_the_file_has_not_got_is_appended_whole(self, tmp_path):
        target = tmp_path / "lmrelay.toml"
        target.write_text(
            "# a note\n[upstream.ollama]\nbase_url = \"http://127.0.0.1:11434\"\n", encoding="utf-8"
        )
        set_limit(target, "per_address", "--concurrent", "3")
        body = target.read_text(encoding="utf-8")
        assert body.startswith("# a note\n")
        assert body.endswith("\n[limits.per_address]\nconcurrent = 3\n")
        assert limits_of(target, "per_address").concurrent == 3

    def test_a_file_with_no_trailing_newline_still_parses_afterwards(self, tmp_path):
        target = tmp_path / "lmrelay.toml"
        target.write_text(
            '[upstream.ollama]\nbase_url = "http://127.0.0.1:11434"', encoding="utf-8"
        )
        set_limit(target, "total", "--concurrent", "3")
        assert limits_of(target, "total").concurrent == 3


class TestWhatItRefuses:
    """Every refusal leaves the file exactly as it was."""

    def test_no_key_at_all(self, config_path):
        with pytest.raises(LmrelayError) as raised:
            set_limit(config_path, "total")
        message = str(raised.value)
        assert "--rate" in message and "--concurrent" in message
        # Said out loud, because "set it to nothing" is a reasonable reading of
        # a command with no flags and it is not what this does.
        assert "0 to turn one off" in message

    @pytest.mark.parametrize("flags", [
        ["--rate", "abc"],
        ["--rate", "-2"],
        ["--rate", "nan"],
        ["--burst", "abc"],
        ["--concurrent", "1.5"],
        ["--concurrent", "-1"],
    ])
    def test_a_value_the_file_would_refuse(self, config_path, flags):
        """Read by the config's own readers, so a number is refused in the same
        words whether it was typed into the file or onto the command line."""
        before = config_path.read_text(encoding="utf-8")
        with pytest.raises(ConfigError) as raised:
            set_limit(config_path, "total", *flags)
        assert "[limits.total]" in str(raised.value)
        assert config_path.read_text(encoding="utf-8") == before

    def test_a_scope_that_does_not_exist(self, config_path):
        with pytest.raises(SystemExit):
            set_limit(config_path, "per_user", "--rate", "1")

    def test_a_config_that_was_already_broken(self, tmp_path):
        """Its own error, not one about the edit: nothing was wrong with the
        command, and pointing at the command would send the operator to fix the
        wrong thing."""
        target = tmp_path / "lmrelay.toml"
        target.write_text('[limits.total]\nconcurrent = "six"\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="whole number"):
            set_limit(target, "total", "--concurrent", "4")

    def test_a_spelling_this_editor_cannot_reach(self, tmp_path):
        """An inline table is legal TOML that a line rewrite cannot edit, so the
        edit is parsed before it is written and this one never reaches the
        disk."""
        target = tmp_path / "lmrelay.toml"
        body = (
            "[limits]\ntotal = { concurrent = 1 }\n\n"
            '[upstream.ollama]\nbase_url = "http://127.0.0.1:11434"\n'
        )
        target.write_text(body, encoding="utf-8")
        with pytest.raises(ConfigError) as raised:
            set_limit(target, "total", "--concurrent", "4")
        assert "nothing has been written" in str(raised.value)
        assert target.read_text(encoding="utf-8") == body

    def test_no_config_to_edit_points_at_init(self, tmp_path):
        """Writing one would produce a config with limits and no upstream, which
        is a file the relay refuses to start from."""
        with pytest.raises(ConfigError) as raised:
            set_limit(tmp_path / "absent.toml", "total", "--concurrent", "4")
        assert "lmrelay init" in str(raised.value)


class TestWhatItSays:
    """The operator should not have to run `status` to find out what happened."""

    def test_it_reports_the_scope_before_and_after(self, config_path, caplog):
        with caplog.at_level(logging.INFO):
            set_limit(config_path, "total", "--concurrent", "6")
        # The same words `status` and the reload log use for the same numbers.
        assert "[limits.total] off -> 6 at once" in caplog.text

    def test_and_names_the_file_it_wrote(self, config_path, caplog):
        with caplog.at_level(logging.INFO):
            set_limit(config_path, "total", "--concurrent", "6")
        assert str(config_path) in caplog.text

    def test_a_per_token_limit_with_auth_off_is_flagged(self, config_path, caplog):
        """Legal, because turning auth on later makes it live. Said here rather
        than only at the next start, which is a log the operator is not reading
        at the moment they can still make the other choice."""
        write_state(config_path.parent, auth_enabled=False)
        with caplog.at_level(logging.WARNING):
            set_limit(config_path, "per_token", "--rate", "2")
        assert "nothing is keyed by a token" in caplog.text
        assert "lmrelay auth true" in caplog.text

    def test_but_not_when_auth_is_on(self, config_path, caplog):
        write_state(config_path.parent, auth_enabled=True, tokens=("lmr_a",))
        with caplog.at_level(logging.WARNING):
            set_limit(config_path, "per_token", "--rate", "2")
        assert "nothing is keyed by a token" not in caplog.text

    def test_nor_for_another_scope_with_auth_off(self, config_path, caplog):
        """per_address and total are keyed by something that exists whatever the
        switch says, so the warning would be noise on both."""
        write_state(config_path.parent, auth_enabled=False)
        with caplog.at_level(logging.WARNING):
            set_limit(config_path, "total", "--rate", "2")
        assert "nothing is keyed by a token" not in caplog.text

    def test_turning_the_per_token_scope_off_is_not_flagged(self, config_path, caplog):
        """Nothing is keyed by a token, and nothing is asking to be."""
        write_state(config_path.parent, auth_enabled=False)
        with caplog.at_level(logging.WARNING):
            set_limit(config_path, "per_token", "--rate", "0")
        assert "nothing is keyed by a token" not in caplog.text

    def test_a_stopped_relay_is_told_the_change_waits(self, config_path, caplog):
        with caplog.at_level(logging.INFO):
            set_limit(config_path, "total", "--concurrent", "6")
        assert "No relay is running" in caplog.text


def main():
    pass


if __name__ == "__main__":
    main()
