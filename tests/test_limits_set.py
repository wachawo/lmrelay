#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`lmrelay limits set`: two numbers changed in the operator's own file."""

import logging

import pytest

# Local imports
from lmrelay import service
from lmrelay.config import CONFIG_ENV_VAR, load_config
from lmrelay.errors import ConfigError, LmrelayError
from tests.conftest import run_command, write_state

# The shape `lmrelay init` writes: a commented file with the scopes spelled out
# and every number off. What survives an edit to it is the point of most of the
# tests below.
CONFIG_BODY = """\
# lmrelay configuration. These comments are the operator's.

[server]
host = "127.0.0.1"
port = 11435

# Per credential. Skipped entirely with auth off.
[limits.per_token]
concurrent = 0
rate       = ""

# The relay as a whole, whoever is asking.
[limits.total]
concurrent = 0
rate       = ""

[upstream.ollama]
base_url = "http://127.0.0.1:11434"
dialect  = "ollama"
"""


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """No test here may find the operator's own config or reach a service manager."""
    monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "absent.toml"))
    monkeypatch.setattr(service, "detect_manager", lambda: "none")


@pytest.fixture
def config_path(tmp_path):
    """A config of its own, so the state lands beside it and not in $HOME."""
    target = tmp_path / "lmrelay.toml"
    target.write_text(CONFIG_BODY, encoding="utf-8")
    return target


def set_limit(config_path, *words: str) -> None:
    """`lmrelay limits set <scope> <N|N/PERIOD> [N/PERIOD]` against a given config."""
    run_command(["limits", "set", *words, "--config", str(config_path)])


def limits_of(config_path, scope: str):
    """What the relay would enforce for one scope, read back through the loader."""
    return load_config(config_path).limits[scope]


def changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    """Every line that is not identical, paired old with new."""
    old, new = before.splitlines(), after.splitlines()
    assert len(old) == len(new), "a line was added or removed"
    return [pair for pair in zip(old, new, strict=True) if pair[0] != pair[1]]


class TestTheThreeForms:
    """A cap, a rate, or both, and the shape of the argument says which."""

    def test_a_count_on_its_own_is_a_cap(self, config_path):
        set_limit(config_path, "total", "6")
        limits = limits_of(config_path, "total")
        assert (limits.concurrent, limits.rate) == (6, "")

    def test_a_rate_on_its_own_carries_a_cap_of_its_own_count(self, config_path):
        """"One a minute" said with nothing about at-once means one at a time.
        Without this, `total 1/60s` would let ten arrive together on the minute
        they are allowed, which is not what anybody writing it means."""
        set_limit(config_path, "total", "1/60s")
        limits = limits_of(config_path, "total")
        assert (limits.concurrent, limits.rate) == (1, "1/60s")
        assert limits.per_second() == pytest.approx(1 / 60)

    def test_two_arguments_are_two_different_numbers(self, config_path):
        """The form the one-number shape could not express: ten every half hour,
        of which two may run together. On a machine holding one model in memory
        that difference is the whole point."""
        set_limit(config_path, "per_address", "2", "10/30m")
        limits = limits_of(config_path, "per_address")
        assert (limits.concurrent, limits.rate) == (2, "10/30m")
        assert limits.per_second() == pytest.approx(10 / 1800)

    def test_zero_turns_the_scope_off(self, config_path):
        set_limit(config_path, "total", "6")
        set_limit(config_path, "total", "0")
        assert limits_of(config_path, "total").configured() is False

    def test_setting_it_again_without_a_rate_clears_the_rate(self, config_path):
        """The command sets a scope, not a key. A rate left behind from last
        time would make the same command mean different things on two
        machines."""
        set_limit(config_path, "total", "2", "10/30m")
        set_limit(config_path, "total", "4")
        limits = limits_of(config_path, "total")
        assert (limits.concurrent, limits.rate) == (4, "")

    def test_one_scope_does_not_touch_another(self, config_path):
        set_limit(config_path, "total", "6")
        set_limit(config_path, "per_token", "2", "10/30m")
        assert limits_of(config_path, "total").concurrent == 6
        assert limits_of(config_path, "per_token").rate == "10/30m"

    def test_the_rate_is_written_as_it_was_typed(self, config_path):
        """`1/60s` must not come back as `1/1m`. It is the operator's file, and
        this is the command whose whole point is leaving it alone."""
        set_limit(config_path, "total", "1/60s")
        assert 'rate       = "1/60s"' in config_path.read_text(encoding="utf-8")
        assert limits_of(config_path, "total").rate == "1/60s"


class TestTheFileIsStillTheOperators:
    """This is the one command that writes lmrelay.toml, and it writes two lines."""

    def test_only_the_line_it_was_asked_about_moves(self, config_path):
        """`lmrelay init` ships this file with sixty lines of comment explaining
        the numbers. A command that dropped them to change one is a command
        nobody runs twice."""
        before = config_path.read_text(encoding="utf-8")
        set_limit(config_path, "total", "6")
        after = config_path.read_text(encoding="utf-8")
        # The rate was already "" and stays "", so its line is rewritten to
        # exactly what it said and does not appear here.
        assert changed_lines(before, after) == [("concurrent = 0", "concurrent = 6")]

    def test_a_rate_moves_its_own_line_and_no_other(self, config_path):
        before = config_path.read_text(encoding="utf-8")
        set_limit(config_path, "total", "2", "10/30m")
        after = config_path.read_text(encoding="utf-8")
        assert changed_lines(before, after) == [
            ("concurrent = 0", "concurrent = 2"),
            ('rate       = ""', 'rate       = "10/30m"'),
        ]

    def test_a_note_beside_the_number_survives_it(self, config_path):
        """A number changed by a command is exactly the number somebody wrote a
        reason beside."""
        body = config_path.read_text(encoding="utf-8").replace(
            'concurrent = 0\nrate       = ""\n\n[upstream',
            'concurrent = 0   # sized for the 3090\nrate       = ""\n\n[upstream',
        )
        config_path.write_text(body, encoding="utf-8")
        set_limit(config_path, "total", "6")
        assert "concurrent = 6   # sized for the 3090" in config_path.read_text(encoding="utf-8")

    def test_the_file_keeps_its_private_mode(self, config_path):
        """It is meant to hold provider keys, and every other writer of it
        leaves it 0600."""
        config_path.chmod(0o600)
        set_limit(config_path, "total", "6")
        assert config_path.stat().st_mode & 0o777 == 0o600

    def test_a_key_the_scope_has_not_got_is_added_to_it(self, tmp_path):
        target = tmp_path / "lmrelay.toml"
        target.write_text(
            "[limits.total]\nconcurrent = 5\n\n"
            '[upstream.ollama]\nbase_url = "http://127.0.0.1:11434"\n',
            encoding="utf-8",
        )
        set_limit(target, "total", "5", "5/1h")
        body = target.read_text(encoding="utf-8")
        # Written into the scope it belongs to, aligned like its neighbour, and
        # before the blank line that separates the tables.
        assert 'concurrent = 5\nrate       = "5/1h"\n\n[upstream.ollama]' in body

    def test_a_scope_the_file_has_not_got_is_appended_whole(self, tmp_path):
        target = tmp_path / "lmrelay.toml"
        target.write_text(
            '# a note\n[upstream.ollama]\nbase_url = "http://127.0.0.1:11434"\n', encoding="utf-8"
        )
        set_limit(target, "per_address", "3")
        body = target.read_text(encoding="utf-8")
        assert body.startswith("# a note\n")
        assert body.endswith('\n[limits.per_address]\nconcurrent = 3\nrate       = ""\n')
        assert limits_of(target, "per_address").concurrent == 3

    def test_a_file_with_no_trailing_newline_still_parses_afterwards(self, tmp_path):
        target = tmp_path / "lmrelay.toml"
        target.write_text(
            '[upstream.ollama]\nbase_url = "http://127.0.0.1:11434"', encoding="utf-8"
        )
        set_limit(target, "total", "3")
        assert limits_of(target, "total").concurrent == 3


class TestWhatItRefuses:
    """Every refusal leaves the file exactly as it was."""

    def test_a_scope_with_no_number(self, config_path):
        """`limits set total` is an unfinished command, not a command."""
        with pytest.raises(SystemExit):
            set_limit(config_path, "total")

    def test_a_scope_that_does_not_exist(self, config_path):
        with pytest.raises(SystemExit):
            set_limit(config_path, "per_user", "1")

    def test_the_two_arguments_the_wrong_way_round(self, config_path):
        """`total 10/30m 2` is a rate then a cap, which is the order nobody
        wrote down. Named rather than refused as a bad number, because the
        numbers are both fine and only their places are wrong."""
        before = config_path.read_text(encoding="utf-8")
        with pytest.raises(LmrelayError) as raised:
            set_limit(config_path, "total", "10/30m", "2")
        assert "wrong way round" in str(raised.value)
        assert "limits set total 2 10/30m" in str(raised.value)
        assert config_path.read_text(encoding="utf-8") == before

    @pytest.mark.parametrize("words", [
        ["total", "abc"],
        ["total", "-1"],
        ["total", "1.5"],
        ["total", "5", "3"],
        ["total", "5", "10/30"],
        ["total", "10/0s"],
    ])
    def test_a_value_the_file_would_refuse(self, config_path, words):
        """Read by the config's own readers, so a value is refused in the same
        words whether it was typed into the file or onto the command line."""
        before = config_path.read_text(encoding="utf-8")
        with pytest.raises(ConfigError, match=r"\[limits\.total\]"):
            set_limit(config_path, *words)
        assert config_path.read_text(encoding="utf-8") == before

    def test_and_a_rate_refusal_shows_the_shape(self, config_path):
        with pytest.raises(ConfigError) as raised:
            set_limit(config_path, "total", "5", "10/30")
        message = str(raised.value)
        assert "a count, a slash and a period" in message
        assert '"10/30m"' in message

    def test_a_config_that_was_already_broken(self, tmp_path):
        """Its own error, not one about the edit: nothing was wrong with the
        command, and pointing at the command would send the operator to fix the
        wrong thing."""
        target = tmp_path / "lmrelay.toml"
        target.write_text('[limits.total]\nconcurrent = "six"\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="whole number"):
            set_limit(target, "total", "4")

    @pytest.mark.parametrize("key", ["burst", "requests", "period"])
    def test_a_config_carrying_a_key_this_replaced(self, tmp_path, key):
        """`requests` and `period` shipped in 0.0.5, so this refusal is the
        upgrade note for anybody who set a limit with them."""
        target = tmp_path / "lmrelay.toml"
        target.write_text(f'[limits.total]\n{key} = 6\n', encoding="utf-8")
        with pytest.raises(ConfigError) as raised:
            set_limit(target, "total", "4")
        assert key in str(raised.value)
        assert "concurrent and rate" in str(raised.value)

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
        with pytest.raises(ConfigError, match="nothing has been written"):
            set_limit(target, "total", "4")
        assert target.read_text(encoding="utf-8") == body

    def test_no_config_to_edit_points_at_init(self, tmp_path):
        """Writing one would produce a config with limits and no upstream, which
        is a file the relay refuses to start from."""
        with pytest.raises(ConfigError, match="lmrelay init"):
            set_limit(tmp_path / "absent.toml", "total", "4")


class TestWhatItSays:
    """The operator should not have to run `status` to find out what happened."""

    def test_it_reports_the_scope_before_and_after(self, config_path, caplog):
        with caplog.at_level(logging.INFO):
            set_limit(config_path, "total", "6")
        # The same words `status` and the reload log use for the same numbers.
        assert "[limits.total] off -> 6 at once" in caplog.text

    def test_and_names_both_halves_when_both_are_set(self, config_path, caplog):
        with caplog.at_level(logging.INFO):
            set_limit(config_path, "total", "2", "10/30m")
        assert "[limits.total] off -> 10/30m, 2 at once" in caplog.text

    def test_and_names_the_file_it_wrote(self, config_path, caplog):
        with caplog.at_level(logging.INFO):
            set_limit(config_path, "total", "6")
        assert str(config_path) in caplog.text

    def test_a_cap_the_rate_can_never_reach_is_flagged(self, config_path, caplog):
        """The bucket holds the rate's own count, so at most that many can start
        together and a cap above it will never refuse anybody. Legal, and almost
        always a slip: the two numbers sit beside each other looking as though
        they agree."""
        with caplog.at_level(logging.WARNING):
            set_limit(config_path, "total", "10", "2/60s")
        assert "will never refuse anybody" in caplog.text

    def test_but_not_when_the_two_agree(self, config_path, caplog):
        with caplog.at_level(logging.WARNING):
            set_limit(config_path, "total", "2", "10/60s")
        assert "will never refuse anybody" not in caplog.text

    def test_nor_when_there_is_no_rate_to_disagree_with(self, config_path, caplog):
        with caplog.at_level(logging.WARNING):
            set_limit(config_path, "total", "10")
        assert "will never refuse anybody" not in caplog.text

    def test_a_per_token_limit_with_auth_off_is_flagged(self, config_path, caplog):
        """Legal, because turning auth on later makes it live. Said here rather
        than only at the next start, which is a log the operator is not reading
        at the moment they can still make the other choice."""
        write_state(config_path.parent, auth_enabled=False)
        with caplog.at_level(logging.WARNING):
            set_limit(config_path, "per_token", "2")
        assert "nothing is keyed by a token" in caplog.text
        assert "lmrelay auth true" in caplog.text

    def test_but_not_when_auth_is_on(self, config_path, caplog):
        write_state(config_path.parent, auth_enabled=True, tokens=("lmr_a",))
        with caplog.at_level(logging.WARNING):
            set_limit(config_path, "per_token", "2")
        assert "nothing is keyed by a token" not in caplog.text

    def test_nor_for_another_scope_with_auth_off(self, config_path, caplog):
        """per_address and total are keyed by something that exists whatever the
        switch says, so the warning would be noise on both."""
        write_state(config_path.parent, auth_enabled=False)
        with caplog.at_level(logging.WARNING):
            set_limit(config_path, "total", "2")
        assert "nothing is keyed by a token" not in caplog.text

    def test_turning_the_per_token_scope_off_is_not_flagged(self, config_path, caplog):
        """Nothing is keyed by a token, and nothing is asking to be."""
        write_state(config_path.parent, auth_enabled=False)
        with caplog.at_level(logging.WARNING):
            set_limit(config_path, "per_token", "0")
        assert "nothing is keyed by a token" not in caplog.text

    def test_a_stopped_relay_is_told_the_change_waits(self, config_path, caplog):
        with caplog.at_level(logging.INFO):
            set_limit(config_path, "total", "6")
        assert "No relay is running" in caplog.text


def main():
    pass


if __name__ == "__main__":
    main()
