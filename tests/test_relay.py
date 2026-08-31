#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The relay end to end: what a caller gets, and what the upstream is sent."""

import logging
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from typing import NamedTuple

import anyio
import httpx
import pytest
from starlette.testclient import TestClient

# Local imports
from lmrelay.daemon import PID_NAME, read_pid, write_pid
from lmrelay.state import STATE_NAME
from tests.conftest import CONFIG_TEMPLATE, TOKEN, build_relay, write_config, write_state

EXTRA_UPSTREAM = """
[upstream.second]
base_url = "http://second.invalid:11434"
dialect  = "ollama"
"""

FIRST_CHUNK = b'{"response":"a"}\n'

# A response header a provider can legally send and starlette cannot re-emit:
# the UTF-8 encoding of U+2713, which httpx decodes to a str holding a codepoint
# outside latin-1. Given as raw bytes because that is what arrives off a socket,
# and because httpx itself refuses to build a response from a str header it
# cannot encode, which would fail in the test rather than in the relay.
UNENCODABLE_HEADER = (b"x-provider-note", b"\xe2\x9c\x93")

# A second credential, so that two callers in one test are told apart by what
# they present rather than by an address the test client cannot vary.
OTHER_TOKEN = "another-callers-token"

# What the in-process client's address resolves to, and therefore the key a
# relay with auth off counts every request in this module against.
TESTCLIENT_KEY = "addr:testclient"

# Two chunks, because the hold in the recorder happens between them: an answer
# that has produced the first and not the second is an answer under way.
GATED_CHUNKS = [FIRST_CHUNK, b'{"done":true}\n']


def fails_after_the_first_chunk():
    """An upstream body that starts an answer and then breaks.

    Synchronous because the recorder iterates its chunks synchronously, and a
    generator rather than a list because a list has no way to stop badly.
    """
    yield FIRST_CHUNK
    raise httpx.ReadError("connection reset")


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


def config_rate(rate: str, burst: str | None = None) -> str:
    """The standard config with the rate limit set, and optionally its burst."""
    body = config_where("rate_limit", rate)
    return body if burst is None else with_setting(body, "rate_burst", burst)


def wait_until(condition, what: str, timeout: float = 10.0) -> None:
    """Block until `condition` holds, or fail saying what never happened."""
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert condition(), what


class Held(NamedTuple):
    """An answer part-way through, and the pool it and its rivals run in."""

    answer: Future
    pool: ThreadPoolExecutor


@contextmanager
def answer_in_flight(client, recorder, **kwargs):
    """Hold one answer open part way through, for the length of the block.

    The recorder stops before its second chunk, and reaching that point proves
    the caller already has the headers: starlette writes the response start
    before it pulls the first chunk from the body, so an upstream that has
    produced anything is an answer that has begun arriving. Inside the block a
    caller therefore holds part of an answer while the relay is still streaming
    the rest, which is the only state a cap on simultaneous requests is about.

    On its own thread because the in-process client settles a response before
    handing it back: two requests can only overlap here if two threads make
    them.
    """
    recorder.gate = threading.Event()
    recorder.chunks = list(GATED_CHUNKS)
    with ThreadPoolExecutor(max_workers=2) as pool:
        held = pool.submit(client.post, "/api/generate", json={}, **kwargs)
        wait_until(lambda: recorder.produced, "the upstream never began answering")
        try:
            yield Held(answer=held, pool=pool)
        finally:
            recorder.gate.set()


@pytest.fixture
def capped(tmp_path, monkeypatch, recorder):
    """A relay that admits one answer at a time, with auth off.

    Auth off so the cap keys on the address, which every request from the test
    client shares: one caller, sending more than one thing at once.
    """
    monkeypatch.setenv("LMRELAY_CONFIG", write_config(
        tmp_path, config_where("max_concurrent", "1")
    ))
    monkeypatch.delenv("LMRELAY_TOKEN", raising=False)
    write_state(tmp_path, auth_enabled=False)
    yield from build_relay(recorder)


@pytest.fixture
def limited(tmp_path, monkeypatch, recorder):
    """A relay allowing 2/s with a burst of 3, auth off so the key is the address."""
    monkeypatch.setenv("LMRELAY_CONFIG", write_config(tmp_path, config_rate("2", "3")))
    monkeypatch.delenv("LMRELAY_TOKEN", raising=False)
    write_state(tmp_path, auth_enabled=False)
    yield from build_relay(recorder)


@pytest.fixture
def limited_by_token(tmp_path, monkeypatch, recorder):
    """The same limit with auth on and two credentials, so that two callers in
    one test are told apart by what they present rather than by an address the
    in-process client cannot vary."""
    monkeypatch.setenv("LMRELAY_CONFIG", write_config(tmp_path, config_rate("2", "3")))
    monkeypatch.delenv("LMRELAY_TOKEN", raising=False)
    write_state(tmp_path, auth_enabled=True, tokens=(TOKEN, OTHER_TOKEN))
    yield from build_relay(recorder)


@pytest.fixture
def capped_by_token(tmp_path, monkeypatch, recorder):
    """The same cap with auth on and two credentials configured, so that the
    two callers in a test are told apart by what they present."""
    monkeypatch.setenv("LMRELAY_CONFIG", write_config(
        tmp_path, config_where("max_concurrent", "1")
    ))
    monkeypatch.delenv("LMRELAY_TOKEN", raising=False)
    write_state(tmp_path, auth_enabled=True, tokens=(TOKEN, OTHER_TOKEN))
    yield from build_relay(recorder)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestTheDoor:
    """Who gets in."""

    def test_a_request_with_no_credential_is_refused(self, relay, recorder):
        response = relay.post("/api/chat", json={})
        assert response.status_code == 401
        # And nothing reached the upstream: a 401 that still forwarded would
        # spend the provider's quota on a caller who was turned away.
        assert recorder.requests == []

    def test_a_wrong_credential_is_refused(self, relay, recorder):
        response = relay.post("/api/chat", json={}, headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401
        assert recorder.requests == []

    def test_the_refusal_says_who_refused(self, relay):
        """Distinguishable from the provider's own 401, which is the difference
        between checking the config and rotating a provider key."""
        assert "lmrelay" in relay.post("/api/chat", json={}).json()["error"]

    def test_the_right_credential_gets_through(self, authed, recorder):
        assert authed.post("/api/chat", json={}).status_code == 200
        assert len(recorder.requests) == 1

    def test_x_api_key_works_as_well_as_a_bearer(self, relay):
        response = relay.post("/api/chat", json={}, headers={"x-api-key": TOKEN})
        assert response.status_code == 200

    def test_health_needs_no_credential(self, relay):
        """It is what a container healthcheck calls, and it must not need the
        secret to say the process is alive."""
        response = relay.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_health_touches_no_upstream(self, relay, recorder):
        relay.get("/healthz")
        assert recorder.requests == []

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    def test_health_is_exempt_by_method_as_well_as_by_path(self, relay, recorder, method):
        """The health route answers GET only, and FastAPI does not add HEAD, so
        every other method on /healthz falls through to the catch-all relay.
        Exempting the path alone would let an anonymous caller reach the default
        upstream with the operator's provider credential attached, choosing the
        body, the query and the headers; only the path is pinned."""
        response = relay.request(method, "/healthz", content=b'{"anonymous": true}')
        assert response.status_code == 401
        assert recorder.requests == []


class TestAnOpenRelay:
    """No state file, so the auth switch is off: every caller is let through,
    deliberately. It is what a fresh install in front of a local Ollama is."""

    @pytest.fixture
    def open_relay(self, tmp_path, monkeypatch, recorder):
        monkeypatch.delenv("LMRELAY_TOKEN", raising=False)
        body = CONFIG_TEMPLATE.format(token="").replace('token = ""', "")
        monkeypatch.setenv("LMRELAY_CONFIG", write_config(tmp_path, body))
        yield from build_relay(recorder)

    def test_the_switch_being_off_means_no_check(self, open_relay):
        assert open_relay.post("/api/chat", json={}).status_code == 200

    def test_and_a_caller_credential_is_still_not_forwarded(self, open_relay, recorder):
        """The relay not checking a credential is not a reason to hand one to a
        provider."""
        open_relay.post("/api/chat", json={}, headers={"Authorization": "Bearer whatever"})
        assert "authorization" not in recorder.last.headers


class TestATokenNobodyIsChecking:
    """A valid token in state.json with the switch off, from an operator who ran
    `lmrelay token gen` and never ran `lmrelay auth true`. The switch is what
    decides, not whether tokens exist, so the relay is open either way. Half of
    it would be worse than both: a relay that honoured the token and refused
    its absence would have auth on with the switch off, and `lmrelay auth true`
    would then be the command that changes nothing."""

    @pytest.fixture
    def unchecked(self, tmp_path, monkeypatch, recorder):
        monkeypatch.delenv("LMRELAY_TOKEN", raising=False)
        monkeypatch.setenv(
            "LMRELAY_CONFIG", write_config(tmp_path, CONFIG_TEMPLATE.format(token=TOKEN))
        )
        write_state(tmp_path, auth_enabled=False, tokens=(TOKEN,))
        yield from build_relay(recorder)

    def test_a_caller_presenting_the_token_gets_through(self, unchecked, recorder):
        response = unchecked.post(
            "/api/chat", json={}, headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 200
        # And the genuine relay-issued credential is stripped like any other.
        # Nothing read it on the way in, which is exactly how it could have been
        # forwarded, handing a provider a working lmrelay token.
        assert "authorization" not in recorder.last.headers

    def test_and_a_caller_presenting_nothing_gets_through_too(self, unchecked):
        assert unchecked.post("/api/chat", json={}).status_code == 200


class TestWhatTheUpstreamReceives:
    """The request that leaves, once one is let in."""

    def test_the_default_upstream_gets_an_unprefixed_path(self, authed, recorder):
        authed.post("/api/chat", json={"model": "qwen3:30b"})
        assert str(recorder.last.url) == "http://ollama.invalid:11434/api/chat"

    def test_a_prefixed_path_goes_to_the_upstream_it_names(self, authed, recorder):
        authed.post("/anthropic/v1/messages", json={})
        assert str(recorder.last.url) == "https://anthropic.invalid/v1/messages"

    def test_the_method_is_kept(self, authed, recorder):
        authed.delete("/api/delete")
        assert recorder.last.method == "DELETE"

    def test_the_query_string_rides_along(self, authed, recorder):
        authed.get("/api/tags?verbose=1&filter=x")
        assert recorder.last.url.params["verbose"] == "1"
        assert recorder.last.url.params["filter"] == "x"

    def test_the_body_arrives_byte_for_byte(self, authed, recorder):
        sent = b'{"model":"qwen3:30b","prompt":"\xd0\xbf\xd1\x80\xd0\xb8"}'
        authed.post("/api/generate", content=sent, headers={"content-type": "application/json"})
        assert recorder.last.content == sent

    def test_the_callers_credential_is_stripped(self, authed, recorder):
        """The property this whole relay exists to hold: a caller's token
        authenticates them HERE and is meaningless, and dangerous, onward."""
        authed.post("/api/chat", json={})
        assert "authorization" not in recorder.last.headers

    def test_the_upstreams_own_credential_is_added(self, authed, recorder):
        authed.post("/anthropic/v1/messages", json={})
        assert recorder.last.headers["x-api-key"] == "provider-secret"
        assert recorder.last.headers["anthropic-version"] == "2023-06-01"

    def test_a_caller_cannot_substitute_the_provider_key(self, authed, recorder):
        """Otherwise a caller chooses which key pays for their request."""
        authed.post("/anthropic/v1/messages", json={}, headers={"x-api-key": TOKEN})
        assert recorder.last.headers["x-api-key"] == "provider-secret"

    def test_one_upstreams_credential_never_reaches_another(self, authed, recorder):
        """Each request is built from its own upstream's headers, so a shared
        client cannot carry Anthropic's key to Ollama."""
        authed.post("/anthropic/v1/messages", json={})
        authed.post("/api/chat", json={})
        assert "x-api-key" not in recorder.last.headers
        assert "authorization" not in recorder.last.headers

    def test_the_body_is_framed_once(self, authed, recorder):
        """A forwarded content-length beside httpx's own chunked framing is a
        request some upstreams reject and others read short."""
        authed.post("/api/generate", json={"prompt": "hello"})
        headers = recorder.last.headers
        if "transfer-encoding" in headers:
            assert "content-length" not in headers
        else:
            assert int(headers["content-length"]) == len(recorder.last.content)

    def test_a_get_with_no_body_is_not_reframed_as_a_stream(self, authed, recorder):
        """Some upstreams reject a chunked GET outright."""
        authed.get("/api/tags")
        assert recorder.last.headers.get("transfer-encoding") != "chunked"
        assert recorder.last.content == b""

    def test_no_header_is_invented_that_the_caller_never_sent(self, authed, recorder):
        """The request is built by hand rather than through client.build_request
        so httpx's defaults do not join it. An Accept-Encoding we made up comes
        back as a compressed body the caller never asked for and cannot read;
        a User-Agent we made up is attributed to them in the provider's logs."""
        # The test client adds these itself, so they have to go before the
        # question can be asked at all.
        for invented in ("accept", "accept-encoding", "user-agent", "connection"):
            authed.headers.pop(invented, None)
        authed.post("/api/chat", content=b"{}", headers={"content-type": "application/json"})

        arrived = {name.lower() for name in recorder.last.headers}
        assert "accept-encoding" not in arrived
        assert "user-agent" not in arrived
        # What the caller did send is still there.
        assert recorder.last.headers["content-type"] == "application/json"


class TestWhatTheCallerGetsBack:
    """The answer, as unchanged as it can be made."""

    def test_the_status_is_the_upstreams(self, authed, recorder):
        recorder.status = 404
        recorder.body = b'{"error":"model not found"}'
        response = authed.post("/api/chat", json={})
        assert response.status_code == 404
        # The provider's own words, not a summary of them.
        assert response.json() == {"error": "model not found"}

    def test_a_provider_refusal_is_passed_through_unchanged(self, authed, recorder):
        """A 401 from the provider must not be reshaped into lmrelay's own, or
        an operator rotates the wrong credential."""
        recorder.status = 401
        recorder.body = b'{"error":{"message":"invalid x-api-key"}}'
        response = authed.post("/anthropic/v1/messages", json={})
        assert response.status_code == 401
        assert "invalid x-api-key" in response.text

    def test_the_content_type_survives(self, authed, recorder):
        recorder.headers = {"content-type": "text/event-stream"}
        response = authed.post("/api/chat", json={})
        assert response.headers["content-type"] == "text/event-stream"

    def test_a_streamed_answer_arrives_whole_and_in_order(self, authed, recorder):
        recorder.chunks = [b'{"response":"a"}\n', b'{"response":"b"}\n', b'{"done":true}\n']
        response = authed.post("/api/generate", json={"stream": True})
        assert response.content == b"".join(recorder.chunks)

    # That the answer reaches the caller BEFORE the upstream has finished is
    # the property this design exists for, and it cannot be seen from the
    # in-process test client: it drives the app through a portal that settles
    # the response before handing it back. Measured over a real socket in
    # test_streaming.py instead.


class TestRefusingWhatCannotWork:
    """Refusals lmrelay makes rather than passing on a certain failure."""

    def test_an_ollama_path_at_a_hosted_provider_is_refused(self, authed, recorder):
        response = authed.post("/anthropic/api/chat", json={})
        assert response.status_code == 400
        assert recorder.requests == [], "the refusal must cost no upstream call"

    def test_the_refusal_explains_itself(self, authed):
        error = authed.post("/anthropic/api/chat", json={}).json()["error"]
        assert error.startswith("lmrelay:")
        # Named so it is not read as the provider's own 404, which is the
        # confusion the status code is chosen to avoid.
        assert "does not translate" in error

    def test_a_matching_dialect_is_forwarded(self, authed, recorder):
        assert authed.post("/anthropic/v1/messages", json={}).status_code == 200
        assert len(recorder.requests) == 1

    def test_ollama_paths_reach_ollama(self, authed, recorder):
        assert authed.post("/api/chat", json={}).status_code == 200
        assert len(recorder.requests) == 1


class TestWhenTheUpstreamIsNotThere:
    """Failures of the connection, reported as the relay's own for as long as
    there is still a status to report them in."""

    def test_an_unreachable_upstream_is_a_502(self, authed, recorder):
        recorder.raises = httpx.ConnectError("nothing listening")
        response = authed.post("/api/chat", json={})
        assert response.status_code == 502

    def test_and_the_message_names_the_upstream_and_its_address(self, authed, recorder):
        """So the operator knows which of several upstreams is down, and can
        check the address without opening the config."""
        recorder.raises = httpx.ConnectError("nothing listening")
        error = authed.post("/api/chat", json={}).json()["error"]
        assert "ollama" in error and "ollama.invalid:11434" in error

    def test_a_connect_timeout_is_the_same_502(self, authed, recorder):
        recorder.raises = httpx.ConnectTimeout("too slow")
        assert authed.post("/api/chat", json={}).status_code == 502

    def test_a_failure_before_the_first_byte_is_a_502(self, authed, recorder):
        """`raises` fires in the upstream's handler, so no response exists yet
        and the relay is still free to choose the status the caller gets."""
        recorder.raises = httpx.ReadError("connection reset")
        response = authed.post("/api/chat", json={})
        assert response.status_code == 502
        assert "ollama" in response.json()["error"]

    def test_a_failure_part_way_through_a_stream_cannot_become_a_502(self, authed, recorder):
        """The status is spent. It went out with the first chunk, so a body that
        breaks afterwards has no status left to be reported in: the iterator
        raises, the relay's 500 handler cannot send a response that has already
        started, and the exception leaves the app. Recorded rather than endorsed
        Nothing better is available once the 200 is gone."""
        recorder.chunks = fails_after_the_first_chunk()
        with pytest.raises(httpx.ReadError):
            authed.post("/api/generate", json={"stream": True})
        # And the first chunk really was produced, which is what makes this the
        # case above's opposite rather than a second spelling of it.
        assert recorder.produced == [FIRST_CHUNK]

    def test_and_the_only_account_of_it_is_two_log_lines(self, authed, recorder, caplog):
        """The access line is written when the headers leave, so it says 200 and
        keeps saying it; the failure is a separate line. An operator reading
        either one alone sees a request that went fine."""
        recorder.chunks = fails_after_the_first_chunk()
        with caplog.at_level(logging.INFO), pytest.raises(httpx.ReadError):
            authed.post("/api/generate", json={"stream": True})
        assert "-> ollama: 200" in caplog.text
        assert "ReadError: connection reset" in caplog.text


class TestHowOftenOneCallerMayAsk:
    """`rate_limit` and `rate_burst`, through the middleware that charges them."""

    def test_the_burst_gets_through_and_the_next_one_does_not(self, limited):
        codes = [limited.get("/api/tags").status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]

    def test_the_refusal_names_the_limit_it_broke(self, limited):
        """Distinguishable from the other 429 the relay can answer, and from a
        provider's own: an operator has to know which number to change."""
        for _ in range(3):
            limited.get("/api/tags")
        error = limited.get("/api/tags").json()["error"]
        assert error == "lmrelay: rate limit of 2/s burst 3 exceeded"

    def test_and_it_quotes_the_burst_the_limiter_actually_holds(
        self, tmp_path, monkeypatch, recorder
    ):
        """`rate_burst` defaults to 0, meaning unset, and the limiter reads that
        as 1. Quoting the configured 0 told a caller no request was permitted
        while one had just been served, which reads as a broken relay."""
        monkeypatch.setenv("LMRELAY_CONFIG", write_config(tmp_path, config_rate("1")))
        monkeypatch.delenv("LMRELAY_TOKEN", raising=False)
        write_state(tmp_path, auth_enabled=False)
        for client in build_relay(recorder):
            assert client.get("/api/tags").status_code == 200
            refusal = client.get("/api/tags")
            assert refusal.status_code == 429
            assert "burst 1 exceeded" in refusal.json()["error"]

    def test_the_refusal_carries_a_retry_after_the_caller_can_act_on(self, limited):
        """Unlike the concurrency 429, this one is computable: it is the time
        until one token has refilled. Whole seconds, because the header takes no
        fraction, and rounded up because rounding down invites a retry that is
        refused again."""
        for _ in range(3):
            limited.get("/api/tags")
        assert limited.get("/api/tags").headers["retry-after"] == "1"

    def test_a_refused_request_reaches_no_upstream(self, limited, recorder):
        """The limit exists to keep work off the upstream, so it is charged in
        the middleware, before any upstream has been chosen."""
        for _ in range(3):
            limited.get("/api/tags")
        assert limited.get("/api/tags").status_code == 429
        assert len(recorder.requests) == 3

    def test_health_is_exempt_as_it_is_from_auth(self, limited):
        """It touches no upstream, and a liveness probe that trips the limit
        would report the relay dead for being polled."""
        for _ in range(3):
            limited.get("/api/tags")
        assert limited.get("/api/tags").status_code == 429
        assert [limited.get("/healthz").status_code for _ in range(5)] == [200] * 5

    def test_callers_do_not_share_an_allowance(self, limited_by_token):
        """One caller spending its burst must not refuse everybody else."""
        for _ in range(3):
            limited_by_token.get("/api/tags", headers=bearer(TOKEN))
        assert limited_by_token.get("/api/tags", headers=bearer(TOKEN)).status_code == 429
        assert limited_by_token.get(
            "/api/tags", headers=bearer(OTHER_TOKEN)
        ).status_code == 200

    def test_a_guessed_credential_does_not_spend_the_real_callers_allowance(
        self, limited_by_token
    ):
        """The reason the limit is charged after the credential is checked.
        Charged before, anyone could exhaust a token's allowance by guessing at
        it, which turns a rate limit into a way to deny service to its owner."""
        for _ in range(10):
            assert limited_by_token.get(
                "/api/tags", headers=bearer("not-a-token")
            ).status_code == 401
        codes = [
            limited_by_token.get("/api/tags", headers=bearer(TOKEN)).status_code
            for _ in range(4)
        ]
        assert codes == [200, 200, 200, 429]

    def test_the_refusal_is_logged_as_one_line_naming_no_upstream(self, limited, caplog):
        """`-` rather than a name, because the limit is charged before an
        upstream is selected. Logged as a refusal, at the level the others are."""
        for _ in range(3):
            limited.get("/api/tags")
        with caplog.at_level(logging.INFO):
            limited.get("/api/tags")
        assert "GET /api/tags -> -: 429 (rate limit)" in caplog.text

    def test_a_relay_that_never_asked_for_a_limit_keeps_no_table_at_all(self, authed):
        """The default. The middleware skips the whole block on None, so an
        install predating this key carries none of its cost."""
        from lmrelay.app import app

        assert app.state.limiter is None
        assert [authed.get("/api/tags").status_code for _ in range(20)] == [200] * 20


class TestHowManyAnswersOneCallerMayHold:
    """`max_concurrent`: how many relayed requests one caller may have open.

    A different measure from the rate limit, which is why it is a second key: a
    generation runs for as long as the model takes, so a rate an operator would
    call generous still lets one caller occupy the machine for minutes.
    """

    def test_a_second_request_while_the_first_is_still_answering_is_refused(
        self, capped, recorder
    ):
        with answer_in_flight(capped, recorder):
            assert capped.post("/api/tags").status_code == 429

    def test_the_refusal_says_who_refused_and_what_the_limit_was(self, capped, recorder):
        """Distinguishable from a provider's own 429, and from the other limit
        that answers 429 here: an operator has to know which number to change."""
        with answer_in_flight(capped, recorder):
            error = capped.post("/api/tags").json()["error"]
        assert error.startswith("lmrelay:")
        assert "limit 1" in error

    def test_and_it_names_no_retry_after(self, capped, recorder):
        """The rate limiter's 429 carries one because it can compute it from the
        refill. A slot here frees when a model finishes answering somebody else,
        and with no read timeout that may be minutes away: a guessed number
        would be a lie, and a client obeying it would retry into the same
        refusal."""
        with answer_in_flight(capped, recorder):
            assert "retry-after" not in capped.post("/api/tags").headers

    def test_the_refused_request_costs_no_upstream_call(self, capped, recorder):
        """The cap exists to keep work off the upstream. One that forwarded
        first and refused afterwards would have spent what it was protecting."""
        with answer_in_flight(capped, recorder):
            capped.post("/api/tags")
            assert len(recorder.requests) == 1

    def test_the_answer_already_streaming_is_never_interrupted(self, capped, recorder):
        """Admission is the only lever. Reclaiming a slot by cutting an answer
        short would break the promise the whole relay is built around."""
        with answer_in_flight(capped, recorder) as held:
            capped.post("/api/tags")
        assert held.answer.result(timeout=10).content == b"".join(GATED_CHUNKS)

    def test_the_slot_is_still_held_once_the_caller_has_the_headers(self, capped, recorder):
        """The regression this shape exists to prevent. For a streamed answer
        the handler returns as soon as the upstream's headers arrive, before a
        byte of body has been written; a release moved there would free the slot
        while the answer was still running, and the cap would bound nothing."""
        from lmrelay.app import app

        with answer_in_flight(capped, recorder):
            assert app.state.inflight.counts == {TESTCLIENT_KEY: 1}

    def test_and_it_is_given_back_when_the_body_ends(self, capped, recorder):
        """The entry disappears rather than staying at zero, which is what
        saves the counter from needing a sweep of its own."""
        from lmrelay.app import app

        with answer_in_flight(capped, recorder):
            pass
        assert app.state.inflight.counts == {}

    def test_so_the_next_request_is_served(self, capped, recorder):
        with answer_in_flight(capped, recorder):
            assert capped.post("/api/tags").status_code == 429
        assert capped.post("/api/tags").status_code == 200

    def test_another_caller_is_not_held_up_by_it(self, capped_by_token, recorder):
        """The cap is per caller, keyed like the rate limit: on the credential
        when auth is on. One caller filling theirs must not be an outage for
        everybody else."""
        with answer_in_flight(capped_by_token, recorder, headers=bearer(TOKEN)) as held:
            other = held.pool.submit(
                capped_by_token.post, "/api/tags", headers=bearer(OTHER_TOKEN)
            )
            wait_until(
                lambda: len(recorder.requests) == 2,
                "the second caller never reached the upstream",
            )
        assert other.result(timeout=10).status_code == 200

    def test_the_log_line_says_which_limit_refused(self, capped, recorder, caplog):
        """Two limits answer 429 and only one of them carries a Retry-After, so
        a line saying only that a 429 went out leaves an operator sizing the
        wrong number. One line, not two: the name stands where the elapsed time
        would, which for a request that was never forwarded is only the cost of
        refusing it."""
        with caplog.at_level(logging.INFO), answer_in_flight(capped, recorder):
            capped.post("/api/tags")
        # The relay's own lines only: httpx logs the same exchange from the
        # client side of the test, which is not the log an operator reads.
        refusals = [
            line for line in caplog.text.splitlines()
            if "429" in line and "lmrelay.app" in line
        ]
        assert len(refusals) == 1, refusals
        assert "-> ollama: 429 (concurrency)" in refusals[0]
        assert "WARNING" in refusals[0], "a refusal is not an ordinary served request"

    def test_and_a_served_request_still_says_how_long_it_took(self, capped, recorder, caplog):
        """The other half of that substitution: it must not have taken the time
        to first byte off the requests that were served."""
        with caplog.at_level(logging.INFO):
            capped.post("/api/tags")
        assert re.search(r"-> ollama: 200 \(\d+\.\d\ds\)", caplog.text)

    def test_a_relay_that_never_asked_for_a_cap_admits_them_all(self, authed, recorder):
        """The default, and what every install that predates this key gets."""
        from lmrelay.app import app

        assert app.state.config.max_concurrent == 0
        with answer_in_flight(authed, recorder) as held:
            second = held.pool.submit(authed.post, "/api/tags")
            wait_until(
                lambda: len(recorder.requests) == 2, "the second request was not forwarded"
            )
        assert second.result(timeout=10).status_code == 200


class TestGivingTheSlotBackWhenThereIsNoAnswer:
    """Every way out that never reaches a body. A slot leaked on one of these
    is not recovered by anything: it locks that caller out until the process
    restarts, which is worse than the failure that leaked it."""

    def test_a_dialect_refusal_costs_no_slot(self, capped, recorder):
        """It is refused before the upstream is touched, so it never took one.
        Counting it would let a mistyped path lock a caller out."""
        from lmrelay.app import app

        assert capped.post("/anthropic/api/chat", json={}).status_code == 400
        assert app.state.inflight.counts == {}

    def test_an_unreachable_upstream_gives_it_back(self, capped, recorder):
        from lmrelay.app import app

        recorder.raises = httpx.ConnectError("nothing listening")
        assert capped.post("/api/chat", json={}).status_code == 502
        assert app.state.inflight.counts == {}

    def test_and_the_caller_is_not_locked_out_by_it(self, capped, recorder):
        """The behaviour the assertion above stands for: an Ollama that was
        down for one request must not leave the caller unable to send another
        once it is up."""
        recorder.raises = httpx.ConnectError("nothing listening")
        assert capped.post("/api/chat", json={}).status_code == 502
        recorder.raises = None
        assert capped.post("/api/chat", json={}).status_code == 200

    def test_any_other_transport_failure_gives_it_back_too(self, capped, recorder):
        """The second 502 arm. Both of them return rather than raise, so
        neither is covered by the generator that releases at the end of a body."""
        from lmrelay.app import app

        recorder.raises = httpx.ReadError("connection reset")
        assert capped.post("/api/chat", json={}).status_code == 502
        assert app.state.inflight.counts == {}

    def test_a_stream_that_breaks_part_way_through_gives_it_back(self, capped, recorder):
        """Here the body did start, so the release is the generator's `finally`
        rather than an error arm: the exception leaves through it."""
        from lmrelay.app import app

        recorder.chunks = fails_after_the_first_chunk()
        with pytest.raises(httpx.ReadError):
            capped.post("/api/generate", json={"stream": True})
        assert app.state.inflight.counts == {}

    def test_a_response_the_relay_cannot_build_gives_it_back(self, capped, recorder):
        """The one path out that is not an error arm and not the body generator.

        httpx decodes a response header as UTF-8 and starlette re-encodes it as
        latin-1, so a single non-ASCII header from a provider raises where the
        StreamingResponse is constructed. That sits after the slot is taken and
        after every `except` that releases it, and neither of the two things
        that would otherwise give the slot back is reachable: nothing iterates
        the generator whose `finally` releases, and the BackgroundTask that
        closes the connection is part of the object that failed to be built.
        """
        from lmrelay.app import app

        recorder.headers = [(b"content-type", b"application/json"), UNENCODABLE_HEADER]
        with pytest.raises(UnicodeEncodeError):
            capped.post("/api/generate", json={})
        assert app.state.inflight.counts == {}

    def test_and_that_caller_is_not_locked_out_of_its_own_cap(self, capped, recorder):
        """What the assertion above stands for. Held, the slot is unrecoverable:
        `max_concurrent` such answers and that caller is refused for the life of
        the process, with nothing in the log naming the cap it is refused by."""
        recorder.headers = [UNENCODABLE_HEADER]
        with pytest.raises(UnicodeEncodeError):
            capped.post("/api/generate", json={})
        recorder.headers = {"content-type": "application/json"}
        assert capped.post("/api/generate", json={}).status_code == 200

    def test_a_caller_that_hangs_up_part_way_through_gives_it_back(self):
        """Starlette closes the body generator when the connection goes, and the
        `finally` is what makes that a release rather than a leak.

        Driven directly because the in-process client cannot hang up: it settles
        a response before handing it back. Measured over a real socket while
        this was written, with the same result — the slot came back a moment
        after the client closed, and the next request from that caller was
        admitted.
        """
        from lmrelay.app import relay_body
        from lmrelay.ratelimit import InflightCounter, release_once

        async def produce():
            yield FIRST_CHUNK
            yield b'{"done":true}\n'

        counter = InflightCounter({TESTCLIENT_KEY: 1})
        body = relay_body(
            httpx.Response(200, content=produce()), release_once(counter, TESTCLIENT_KEY)
        )

        async def hang_up_after_the_first_chunk():
            assert await anext(body) == FIRST_CHUNK
            # Still held: the caller has part of an answer and is reading it.
            assert counter.counts == {TESTCLIENT_KEY: 1}
            await body.aclose()

        anyio.run(hang_up_after_the_first_chunk)
        assert counter.counts == {}


class TestStartup:
    """What the process does before it serves anything."""

    def test_a_broken_config_stops_it_from_starting(self, tmp_path, monkeypatch, recorder):
        """Better than a process that binds a port and answers every request
        with the same 500."""
        bad = tmp_path / "lmrelay.toml"
        bad.write_text('[server]\nhost = "127.0.0.1"\n', encoding="utf-8")
        monkeypatch.setenv("LMRELAY_CONFIG", str(bad))
        from lmrelay.app import app

        with pytest.raises(Exception, match=r"\[upstream"), TestClient(app):
            pass

    def test_it_records_its_pid_and_clears_it_again(self, tmp_path, monkeypatch):
        from lmrelay.app import app

        monkeypatch.setenv("LMRELAY_CONFIG", write_config(tmp_path, CONFIG_TEMPLATE.format(
            token=TOKEN
        )))
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

        monkeypatch.setenv("LMRELAY_CONFIG", write_config(tmp_path, CONFIG_TEMPLATE.format(
            token=TOKEN
        )))
        pidfile = tmp_path / PID_NAME
        with TestClient(app):
            # Alive, and not this process: exactly what a relay that lost the
            # race to the port finds in the file it is about to unlink.
            write_pid(pidfile, os.getppid())
        assert read_pid(pidfile) == os.getppid()


class TestReloadingInPlace:
    """What SIGHUP does, called directly: the config changes under a live relay."""

    @pytest.fixture
    def logging_restored(self):
        """Put the root logger back afterwards.

        setup_logging reconfigures it for the whole process, which is the point
        of the setting, so a test that moves the level would otherwise hand the
        next one a logger it never asked for.
        """
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        yield
        root.handlers[:] = handlers
        root.setLevel(level)

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
        assert "port changed" in caplog.text
        assert "connect_timeout" not in caplog.text

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
        assert "port changed" in caplog.text

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

        assert app.state.limiter is None
        write_config(tmp_path, config_rate("2", "3"))
        reload_config(app)
        assert (app.state.limiter.rate, app.state.limiter.burst) == (2.0, 3.0)

    def test_and_off_again(self, authed, tmp_path):
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_rate("2", "3"))
        reload_config(app)
        write_config(tmp_path, config_rate("0"))
        reload_config(app)
        assert app.state.limiter is None

    def test_an_unrelated_reload_leaves_the_limiter_alone(self, limited, tmp_path):
        """A fresh limiter starts every caller with a full bucket, so rebuilding
        it on a reload that changed something else would hand the allowance back
        to whoever was being limited at that moment: editing an upstream URL
        would be a way to clear the limit."""
        from lmrelay.app import app, reload_config

        before = app.state.limiter
        for _ in range(3):
            limited.get("/api/tags")
        assert limited.get("/api/tags").status_code == 429

        write_config(tmp_path, with_setting(config_rate("2", "3"), "log_level", '"INFO"'))
        reload_config(app)
        assert app.state.limiter is before
        assert limited.get("/api/tags").status_code == 429

    def test_but_a_changed_number_rebuilds_it(self, limited, tmp_path):
        """The other half: the numbers cannot move without a new table, since a
        bucket holds an allowance measured against the old burst."""
        from lmrelay.app import app, reload_config

        before = app.state.limiter
        write_config(tmp_path, config_rate("5", "9"))
        reload_config(app)
        assert app.state.limiter is not before
        assert (app.state.limiter.rate, app.state.limiter.burst) == (5.0, 9.0)

    def test_the_reload_log_names_the_effective_burst(self, authed, tmp_path, caplog):
        """Read back afterwards to check what took, so it has to name the number
        the limiter holds rather than the one the file spells: `rate_burst = 0`
        means unset, and the limiter reads it as 1."""
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_rate("2"))
        with caplog.at_level(logging.INFO):
            reload_config(app)
        assert "rate limit off -> 2/s burst 1" in caplog.text

    def test_a_changed_cap_is_in_force_without_a_restart(self, authed, tmp_path):
        """Read from the config at every request, like the upstreams and the
        tokens, so a reload is the whole of applying it."""
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_where("max_concurrent", "2"))
        reload_config(app)
        assert app.state.config.max_concurrent == 2

    def test_and_it_is_not_named_as_needing_one(self, authed, tmp_path, caplog):
        """Listing it beside host and port would send an operator to restart a
        relay that had already done what they asked."""
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_where("max_concurrent", "2"))
        with caplog.at_level(logging.WARNING):
            reload_config(app)
        assert "max_concurrent" not in caplog.text

    def test_the_counter_survives_the_reload_with_its_live_slots(
        self, capped, recorder, tmp_path
    ):
        """Rebuilding it, as the rate limiter is rebuilt, would forget the slot
        every streaming answer holds, and each of those would then release one
        that was no longer recorded — leaving the caller a free slot per answer
        that happened to be running when somebody edited the file."""
        from lmrelay.app import app, reload_config

        before = app.state.inflight
        with answer_in_flight(capped, recorder):
            write_config(tmp_path, config_where("max_concurrent", "4"))
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
            write_config(tmp_path, config_where("max_concurrent", "2"))
            reload_config(app)
            admitted = held.pool.submit(capped.post, "/api/tags")
            wait_until(
                lambda: len(recorder.requests) == 2, "the raised cap admitted nothing"
            )
        assert admitted.result(timeout=10).status_code == 200

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
        monkeypatch.delenv("LMRELAY_TOKEN", raising=False)
        monkeypatch.setenv("LMRELAY_CONFIG", write_config(
            tmp_path, config_where("host", '"0.0.0.0"')
        ))
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
