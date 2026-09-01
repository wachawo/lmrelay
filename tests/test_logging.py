#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The request id: what carries it into a line, and what a line without one does."""

import logging

import httpx
import pytest

# Local imports
from lmrelay.logging_setup import (
    HANDLER_NAME,
    LOG_DATEFMT,
    LOG_FORMAT,
    NO_REQUEST,
    PLAIN_FORMAT,
    new_request_id,
    setup_logging,
    supply_request_id,
)

FORMATTER = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)


def record_of(message: str, **fields) -> logging.LogRecord:
    """A record as logging would build it, with anything an `extra=` would add."""
    record = logging.LogRecord("lmrelay.app", logging.INFO, "app.py", 1, message, (), None)
    for name, value in fields.items():
        setattr(record, name, value)
    return record


def relay_records(caplog) -> list[logging.LogRecord]:
    """Only what the relay itself said. Whatever http client is installed beside
    it announces its own requests, and those records never passed our filter."""
    return [record for record in caplog.records if record.name == "lmrelay.app"]


def ids_in(caplog) -> list[str]:
    """The request id on every line the relay wrote, in the order it wrote them."""
    return [record.request_id for record in relay_records(caplog)]


def ours() -> list[logging.Handler]:
    """The root handlers setup_logging installed, which are the only ones it owns."""
    return [one for one in logging.getLogger().handlers if one.get_name() == HANDLER_NAME]


@pytest.fixture
def logging_restored():
    """Put the root logger back afterwards.

    setup_logging reconfigures it for the whole process, which is the point of
    it, so a test that calls it would otherwise hand the next one a logger it
    never asked for.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


class TestTheIdItself:
    """Short on purpose."""

    def test_it_is_eight_hex_characters(self):
        """A whole uuid4 in every line is noise. This is one relay writing one
        file, not a trace across services, and the id is read by eye rather than
        looked up in anything."""
        assert len(new_request_id()) == 8
        assert int(new_request_id(), 16) >= 0

    def test_two_requests_do_not_share_one(self):
        assert len({new_request_id() for unused_call in range(100)}) == 100


class TestALineThatBelongsToNoRequest:
    """Startup, shutdown, a reload: most of the file, and none of it has an id."""

    def test_the_format_asks_every_line_for_one(self):
        assert "%(request_id)s" in LOG_FORMAT

    def test_so_a_record_without_one_cannot_be_formatted(self):
        """The reason the filter exists. Unfilled, this is a KeyError inside
        logging, which prints the failure in place of the line."""
        with pytest.raises(ValueError):
            FORMATTER.format(record_of("lmrelay reloaded"))

    def test_and_the_filter_gives_it_the_placeholder(self):
        record = record_of("lmrelay reloaded")
        assert supply_request_id(record) is True
        assert f"[{NO_REQUEST}] lmrelay reloaded" in FORMATTER.format(record)

    def test_but_never_overwrites_an_id_that_is_set(self):
        """The id arrives by `extra=` from whatever is serving the request; this
        only covers the lines that have none."""
        record = record_of("relayed", request_id="deadbeef")
        supply_request_id(record)
        assert record.request_id == "deadbeef"

    def test_command_output_carries_no_id_at_all(self):
        """A status block is a table only without a timestamp, a logger name and
        an id in front of every row, and a command is not serving a request."""
        assert "request_id" not in PLAIN_FORMAT


class TestSettingItUpMoreThanOnce:
    """setup_logging runs at startup and again on a reload that moves the level."""

    def test_one_handler_with_one_filter_after_a_reload(self, logging_restored):
        setup_logging("INFO")
        setup_logging("DEBUG")
        assert len(ours()) == 1
        assert ours()[0].filters == [supply_request_id]

    def test_and_the_handler_it_leaves_can_format_a_bare_line(self, logging_restored):
        """The second call drops the handler the first installed, so the filter
        goes on with the handler rather than accumulating beside it."""
        setup_logging("INFO")
        setup_logging("INFO")
        record = record_of("lmrelay reloaded")
        assert all(one(record) for one in ours()[0].filters)
        assert FORMATTER.format(record).endswith("[-] lmrelay reloaded")

    def test_but_a_handler_it_did_not_install_is_left_alone(self, logging_restored, caplog):
        """Which is why it replaces its own by name instead of calling
        basicConfig with force=True: that drops every root handler, and the one
        attached here is pytest's. A reload moves the level and then writes what
        it applied, so an assertion on one of those lines was reading an empty
        capture and passing for the wrong reason."""
        with caplog.at_level(logging.INFO):
            setup_logging("INFO")
            logging.getLogger("lmrelay.app").info("lmrelay reloaded")
        assert "lmrelay reloaded" in caplog.text


class TestTyingARequestToWhatItCaused:
    """The point of the whole thing: two lines in one file, one request."""

    def test_the_access_line_carries_one(self, authed, caplog):
        with caplog.at_level(logging.INFO):
            authed.post("/api/chat", json={})
        written = ids_in(caplog)
        assert len(written) == 1
        assert len(written[0]) == 8

    def test_two_requests_are_told_apart(self, authed, caplog):
        with caplog.at_level(logging.INFO):
            authed.post("/api/chat", json={})
            authed.post("/api/chat", json={})
        first, second = ids_in(caplog)
        assert first != second

    def test_an_upstream_failure_and_its_access_line_share_one(self, authed, recorder, caplog):
        """This is what the id is for. The warning about the upstream and the
        access line for the caller who caused it land in lmrelay.log with
        everybody else's between them."""
        recorder.raises = httpx.ConnectError("no route to host")
        with caplog.at_level(logging.INFO):
            authed.post("/api/chat", json={})
        about_the_upstream, about_the_caller = relay_records(caplog)
        assert "upstream 'ollama'" in about_the_upstream.getMessage()
        assert "POST /api/chat -> ollama: 502" in about_the_caller.getMessage()
        assert about_the_upstream.request_id == about_the_caller.request_id

    def test_and_a_second_caller_does_not_borrow_it(self, authed, recorder, caplog):
        recorder.raises = httpx.ConnectError("no route to host")
        with caplog.at_level(logging.INFO):
            authed.post("/api/chat", json={})
            authed.post("/api/chat", json={})
        failed_first, served_first, failed_again, served_again = ids_in(caplog)
        assert failed_first == served_first
        assert failed_again == served_again
        assert failed_first != failed_again

    def test_a_fault_in_the_relay_and_its_access_line_share_one(self, authed, recorder, caplog):
        """The other pairing, and the one that needs the id most. lmrelay's own
        500 is answered above the middleware, so the traceback and the line
        naming the caller who provoked it are written from two different places,
        and nothing else in either of them says they are one request."""
        recorder.raises = ValueError("something in the relay gave way")
        with caplog.at_level(logging.INFO), pytest.raises(ValueError):
            authed.post("/api/chat", json={})
        about_the_caller, about_the_fault = relay_records(caplog)
        assert "POST /api/chat -> ollama: 500" in about_the_caller.getMessage()
        assert about_the_fault.getMessage().startswith("ValueError")
        assert about_the_caller.request_id == about_the_fault.request_id

    def test_a_refusal_at_the_door_carries_one_too(self, relay, caplog):
        with caplog.at_level(logging.WARNING):
            relay.post("/api/chat", json={})
        assert len(ids_in(caplog)) == 1
        assert ids_in(caplog)[0] != NO_REQUEST

    def test_a_health_check_needs_no_id_and_writes_no_line(self, relay, caplog):
        with caplog.at_level(logging.INFO):
            assert relay.get("/healthz").status_code == 200
        assert relay_records(caplog) == []


def main():
    pass


if __name__ == "__main__":
    main()
