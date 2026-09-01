#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The shipped fail2ban filter, held against the lines the relay actually writes.

The filter reads `lmrelay.log` by position, so it is the one file in this
repository that a change to the log format can break silently: the jail ships
disabled, nothing raises, and an operator who enables it gets a jail that never
bans. That is what happened when the request id was added to the format, and it
is what these tests exist to make loud.

Every line checked here is produced by the relay and formatted by the relay's own
formatter, never typed out. A hand-written example is exactly what was wrong: it
can agree with a filter and disagree with the program.
"""

import logging
import re
from pathlib import Path

import pytest

# Local imports
from lmrelay.config import CONFIG_ENV_VAR
from lmrelay.logging_setup import LOG_DATEFMT, LOG_FORMAT
from tests.conftest import CONFIG_TEMPLATE, TOKEN, build_relay, write_config, write_state

FILTER_PATH = (
    Path(__file__).resolve().parents[1] / "contrib" / "fail2ban" / "filter.d" / "lmrelay-auth.conf"
)

FORMATTER = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

# What fail2ban expands <HOST> to, reduced to the part that decides this: the
# character class it will accept an address in. A bracket is not in it, in any
# fail2ban version, which is why the request id has to be matched and stepped
# over rather than left for <HOST> to absorb.
HOST_PATTERN = r"(?P<host>[\w\-.^_]+)"

# The strptime directives the filter's datepattern uses, as the expression
# fail2ban strips the timestamp with. An unknown directive fails the test rather
# than being guessed at: a datepattern this cannot read is one it cannot check.
DATE_DIRECTIVES = {
    "Y": r"\d{4}", "m": r"\d{2}", "d": r"\d{2}",
    "H": r"\d{2}", "M": r"\d{2}", "S": r"\d{2}", "f": r"\d+",
}


def filter_settings() -> dict[str, str]:
    """`datepattern` and `failregex` as fail2ban reads them, with `%%` collapsed to `%`."""
    body = FILTER_PATH.read_text(encoding="utf-8")
    found = dict(re.findall(r"^(datepattern|failregex)\s*=\s*(.+)$", body, flags=re.MULTILINE))
    assert set(found) == {"datepattern", "failregex"}, f"{FILTER_PATH.name} is missing a key"
    return {name: value.strip().replace("%%", "%") for name, value in found.items()}


def date_expression(datepattern: str) -> re.Pattern[str]:
    """The datepattern as a regex, so a line can be stripped the way fail2ban strips it."""
    def directive(match: re.Match[str]) -> str:
        letter = match.group(1)
        assert letter in DATE_DIRECTIVES, f"unknown strptime directive %{letter}"
        return DATE_DIRECTIVES[letter]

    return re.compile(re.sub(r"%(.)", directive, datepattern))


def failregex() -> re.Pattern[str]:
    """The shipped expression with <HOST> expanded, ready to match a stripped line."""
    return re.compile(filter_settings()["failregex"].replace("<HOST>", HOST_PATTERN))


def as_fail2ban_sees_it(record: logging.LogRecord) -> str:
    """One record formatted as the relay writes it, minus the timestamp fail2ban strips."""
    return date_expression(filter_settings()["datepattern"]).sub("", FORMATTER.format(record), 1)


def relay_lines(caplog) -> list[str]:
    """Every line the relay itself wrote, as the file would hold it."""
    return [
        as_fail2ban_sees_it(record) for record in caplog.records if record.name == "lmrelay.app"
    ]


@pytest.fixture
def rate_limited(tmp_path, monkeypatch, recorder):
    """A relay that refuses a caller's second request in the same second.

    Auth off, so the address scope is doing the whole job and no request is
    refused at the door: what this fixture is for is the one other WARNING the
    relay writes, which the filter must not confuse with a refused credential.
    """
    body = CONFIG_TEMPLATE.format(token=TOKEN) + '\n[limits.per_address]\nrequests = 1\nperiod = "1s"\n'
    monkeypatch.setenv(CONFIG_ENV_VAR, write_config(tmp_path, body))
    write_state(tmp_path, auth_enabled=False)
    yield from build_relay(recorder)


class TestWhatTheFilterMatches:
    """A credential the relay refused, and nothing else in the file."""

    def test_a_refused_credential(self, relay, caplog):
        with caplog.at_level(logging.WARNING):
            assert relay.post("/api/chat", json={}).status_code == 401
        refused, = relay_lines(caplog)
        assert failregex().match(refused), f"the shipped filter does not match {refused!r}"

    def test_and_reads_the_address_out_of_the_field_that_holds_it(self, relay, caplog):
        """The failure mode this guards is quiet: an expression can match a line
        and still take the wrong field as the address, and a jail then bans
        whatever that field held. The line is the relay's own, with only the
        client name swapped for an address, since the test client has none."""
        with caplog.at_level(logging.WARNING):
            relay.post("/api/chat", json={})
        refused, = relay_lines(caplog)
        matched = failregex().match(refused.replace("testclient", "203.0.113.7"))
        assert matched is not None
        assert matched.group("host") == "203.0.113.7"

    def test_the_id_is_matched_rather_than_absorbed(self, relay, caplog):
        """Which is the difference between a filter that works and one that
        looks like it does: <HOST> cannot cross a bracket, so the same line with
        the id taken out is a line the current filter is right to refuse."""
        with caplog.at_level(logging.WARNING):
            relay.post("/api/chat", json={})
        refused, = relay_lines(caplog)
        without_the_id = re.sub(r"\(lmrelay\.app\) \[[0-9a-f]+\] ", "(lmrelay.app) ", refused)
        assert failregex().match(without_the_id) is None


class TestWhatItLeavesAlone:
    """Three lines that are not an attacker, and none of them may reach the jail."""

    def test_a_request_the_relay_served(self, authed, caplog):
        with caplog.at_level(logging.INFO):
            assert authed.post("/api/chat", json={}).status_code == 200
        served, = relay_lines(caplog)
        assert failregex().match(served) is None

    def test_a_401_the_upstream_itself_returned(self, authed, recorder, caplog):
        """An expired provider key. The caller whose key stopped working is not
        an attacker, and the line says so by naming the upstream that answered."""
        recorder.status = 401
        with caplog.at_level(logging.INFO):
            assert authed.post("/api/chat", json={}).status_code == 401
        relayed, = relay_lines(caplog)
        assert "-> ollama: 401" in relayed
        assert failregex().match(relayed) is None

    def test_a_limit_refusal(self, rate_limited, caplog):
        """The other WARNING the access log writes. A caller getting 429s is a
        misconfigured client far more often than an attacker, and is already
        being refused."""
        with caplog.at_level(logging.WARNING):
            rate_limited.post("/api/chat", json={})
            assert rate_limited.post("/api/chat", json={}).status_code == 429
        refused, = relay_lines(caplog)
        assert "429 (rate, per_address)" in refused
        assert failregex().match(refused) is None


def main():
    pass


if __name__ == "__main__":
    main()
