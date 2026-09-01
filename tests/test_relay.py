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


def config_limits(**scopes: str) -> str:
    """The standard config with one [limits.<scope>] table per keyword argument.

    Appended rather than edited into [server], because a limit is its own table
    now: `config_limits(total="concurrent = 1")`.
    """
    body = CONFIG_TEMPLATE.format(token=TOKEN)
    return body + "".join(f"\n[limits.{scope}]\n{keys}\n" for scope, keys in scopes.items())


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


def relay_with(tmp_path, monkeypatch, recorder, body: str, auth_enabled: bool = False):
    """A relay on a given config body, with auth off unless a test needs it.

    Auth off keys the address scope on what every request from the in-process
    client shares: one caller, sending more than one thing at once. Auth on adds
    two credentials, which is the only way two callers in one test can be told
    apart, since the client cannot vary its address.
    """
    monkeypatch.setenv("LMRELAY_CONFIG", write_config(tmp_path, body))
    monkeypatch.delenv("LMRELAY_TOKEN", raising=False)
    write_state(
        tmp_path,
        auth_enabled=auth_enabled,
        tokens=(TOKEN, OTHER_TOKEN) if auth_enabled else (),
    )
    yield from build_relay(recorder)


@pytest.fixture
def capped(tmp_path, monkeypatch, recorder):
    """A relay that admits one answer at a time from one address."""
    yield from relay_with(
        tmp_path, monkeypatch, recorder, config_limits(per_address="concurrent = 1")
    )


@pytest.fixture
def capped_by_token(tmp_path, monkeypatch, recorder):
    """The same cap on the credential rather than the address."""
    yield from relay_with(
        tmp_path, monkeypatch, recorder,
        config_limits(per_token="concurrent = 1"), auth_enabled=True,
    )


@pytest.fixture
def capped_in_total(tmp_path, monkeypatch, recorder):
    """One answer at a time for the whole relay, whoever is asking."""
    yield from relay_with(
        tmp_path, monkeypatch, recorder,
        config_limits(total="concurrent = 1"), auth_enabled=True,
    )


@pytest.fixture
def limited(tmp_path, monkeypatch, recorder):
    """One address allowed 3 a second, which is also 3 at once."""
    yield from relay_with(
        tmp_path, monkeypatch, recorder,
        config_limits(per_address='concurrent = 3\nrate = "3/1s"'),
    )


@pytest.fixture
def limited_by_token(tmp_path, monkeypatch, recorder):
    """The same rate on the credential rather than the address."""
    yield from relay_with(
        tmp_path, monkeypatch, recorder,
        config_limits(per_token='concurrent = 3\nrate = "3/1s"'), auth_enabled=True,
    )


@pytest.fixture
def limited_in_total(tmp_path, monkeypatch, recorder):
    """The same rate for the whole relay, whoever is asking."""
    yield from relay_with(
        tmp_path, monkeypatch, recorder,
        config_limits(total='concurrent = 3\nrate = "3/1s"'), auth_enabled=True,
    )


@pytest.fixture
def limited_everywhere(tmp_path, monkeypatch, recorder):
    """All three scopes set, the narrowest tightest, so a test can watch which
    one a refusal names and what a refusal costs the scopes that said yes."""
    yield from relay_with(
        tmp_path, monkeypatch, recorder,
        config_limits(
            per_token='concurrent = 1\nrate = "1/1s"',
            per_address='concurrent = 4\nrate = "4/1s"',
            total='concurrent = 6\nrate = "6/1s"',
        ),
        auth_enabled=True,
    )


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

    def test_a_framework_refusal_is_phrased_by_the_relay(self, authed, recorder):
        """TRACE is outside RELAY_METHODS, so Starlette refuses it before any of
        this project's code runs and used to answer {"detail": "Method Not
        Allowed"}: no `lmrelay: ` prefix, and a key no other refusal here uses.
        It was the one error a caller could not attribute, against a README that
        promises every error this relay generates begins with that prefix.

        Sent with a credential on purpose: the middleware authenticates before
        routing decides the method is unroutable, so an anonymous TRACE is a 401
        and never reaches the refusal under test."""
        response = authed.request("TRACE", "/api/tags")
        assert response.status_code == 405
        body = response.json()
        assert "detail" not in body
        assert body["error"].startswith("lmrelay: ")
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


class TestHowOftenACallerMayAsk:
    """`period`, which makes `requests` a how-often as well, in each scope."""

    def test_the_allowance_gets_through_and_the_next_one_does_not(self, limited):
        codes = [limited.get("/api/tags").status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]

    def test_the_refusal_names_the_scope_and_the_number(self, limited):
        """`429` on its own leaves an operator guessing which of six numbers to
        raise, and the message has to be distinguishable from a provider's."""
        for _ in range(3):
            limited.get("/api/tags")
        error = limited.get("/api/tags").json()["error"]
        assert error == (
            "lmrelay: rate limit exceeded for your address: 3/1s ([limits.per_address])"
        )

    def test_the_token_scope_says_it_is_your_token(self, limited_by_token):
        for _ in range(3):
            limited_by_token.get("/api/tags", headers=bearer(TOKEN))
        error = limited_by_token.get("/api/tags", headers=bearer(TOKEN)).json()["error"]
        assert "your token" in error and "[limits.per_token]" in error

    def test_and_the_total_says_it_is_the_relay(self, limited_in_total):
        """Not "yours": at this scope the allowance being spent is everybody's,
        and a caller told to slow down when they have sent one request would go
        looking for a limit of their own that is not set."""
        for _ in range(3):
            limited_in_total.get("/api/tags", headers=bearer(TOKEN))
        error = limited_in_total.get("/api/tags", headers=bearer(OTHER_TOKEN)).json()["error"]
        assert "the relay's rate limit" in error and "[limits.total]" in error

    def test_it_quotes_the_period_the_operator_wrote(
        self, tmp_path, monkeypatch, recorder
    ):
        """`60s` and not `1m`. The refusal is the one place a caller ever sees
        the limit, and quoting it back in a spelling their operator never used
        sends them asking about a setting nobody can find."""
        for client in relay_with(
            tmp_path, monkeypatch, recorder,
            config_limits(per_address='concurrent = 3\nrate = "3/60s"'),
        ):
            assert [client.get("/api/tags").status_code for _ in range(3)] == [200] * 3
            refusal = client.get("/api/tags")
            assert refusal.status_code == 429
            assert "3/60s" in refusal.json()["error"]

    def test_the_refusal_carries_a_retry_after_the_caller_can_act_on(self, limited):
        """Unlike a slot refusal, this one is computable: it is the time until
        one token has refilled. Whole seconds, because the header takes no
        fraction, and rounded up because rounding down invites a retry that is
        refused again."""
        for _ in range(3):
            limited.get("/api/tags")
        assert limited.get("/api/tags").headers["retry-after"] == "1"

    def test_a_refused_request_reaches_no_upstream(self, limited, recorder):
        """The limit exists to keep work off the upstream, so nothing is
        forwarded before the whole admission decision has been made."""
        for _ in range(3):
            limited.get("/api/tags")
        assert limited.get("/api/tags").status_code == 429
        assert len(recorder.requests) == 3

    def test_health_is_exempt_by_being_a_different_route(self, limited):
        """It touches no upstream, and a liveness probe that trips the limit
        would report the relay dead for being polled. Structural now: the
        decision lives in the relay route, so /healthz never reaches it and
        needs no path-and-method exemption of its own."""
        for _ in range(3):
            limited.get("/api/tags")
        assert limited.get("/api/tags").status_code == 429
        assert [limited.get("/healthz").status_code for _ in range(5)] == [200] * 5

    def test_callers_do_not_share_an_allowance_at_the_token_scope(self, limited_by_token):
        """One caller spending its burst must not refuse everybody else, which
        is the whole argument for having a scope narrower than the total."""
        for _ in range(3):
            limited_by_token.get("/api/tags", headers=bearer(TOKEN))
        assert limited_by_token.get("/api/tags", headers=bearer(TOKEN)).status_code == 429
        assert limited_by_token.get(
            "/api/tags", headers=bearer(OTHER_TOKEN)
        ).status_code == 200

    def test_but_they_do_share_the_total(self, limited_in_total):
        """Which is the point of it: ten callers each inside their own limit
        still arrive together, and only this scope sees that."""
        for _ in range(3):
            limited_in_total.get("/api/tags", headers=bearer(TOKEN))
        assert limited_in_total.get(
            "/api/tags", headers=bearer(OTHER_TOKEN)
        ).status_code == 429

    def test_a_guessed_credential_does_not_spend_the_real_callers_allowance(
        self, limited_by_token
    ):
        """The reason the limits are charged after the credential is checked.
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

    def test_the_refusal_is_logged_as_one_line_naming_its_upstream(self, limited, caplog):
        """It used to say `-> -`, because the limit was charged before an
        upstream had been selected. One decision made in the route means every
        refusal can say which upstream it was headed for."""
        for _ in range(3):
            limited.get("/api/tags")
        with caplog.at_level(logging.INFO):
            limited.get("/api/tags")
        assert "GET /api/tags -> ollama: 429 (rate, per_address)" in caplog.text

    def test_a_relay_that_never_asked_for_a_limit_keeps_no_table_at_all(self, authed):
        """The default. A scope with its rate off builds no limiter, so an
        install predating these keys carries none of their cost."""
        from lmrelay.app import app

        assert set(app.state.limiters.values()) == {None}
        assert [authed.get("/api/tags").status_code for _ in range(20)] == [200] * 20


class TestPassingEveryScopeOrNone:
    """The scopes apply together, and a refused request costs nothing anywhere."""

    def test_a_request_is_charged_to_every_scope_that_is_set(
        self, limited_everywhere, recorder
    ):
        """They are ceilings, not alternatives. Passing three of them is being
        counted by three of them, which is what makes `total` a protection
        rather than something a generous `per_token` could win against."""
        from lmrelay.app import app

        with answer_in_flight(limited_everywhere, recorder, headers=bearer(TOKEN)):
            assert set(app.state.inflight.counts) == {
                f"token:{TOKEN}", TESTCLIENT_KEY, "total"
            }

    def test_a_refusal_by_the_narrow_scope_leaves_the_wide_ones_unspent(
        self, limited_everywhere
    ):
        """Without all-or-nothing charging, a caller refused by one scope still
        drained the others on the way past, and an operator got "I was refused,
        and now I am rate limited too" with no way to see why."""
        from lmrelay.app import app

        limited_everywhere.get("/api/tags", headers=bearer(TOKEN))
        for _ in range(5):
            assert limited_everywhere.get(
                "/api/tags", headers=bearer(TOKEN)
            ).status_code == 429
        # The address scope allows 4 and has been asked six times; one of those
        # was served and five were refused by the token scope before this one
        # was reached, so it must still have three.
        assert app.state.limiters["per_address"].buckets["addr:testclient"].tokens == 3.0

    def test_and_the_other_caller_is_untouched_by_all_of_it(self, limited_everywhere):
        for _ in range(6):
            limited_everywhere.get("/api/tags", headers=bearer(TOKEN))
        assert limited_everywhere.get(
            "/api/tags", headers=bearer(OTHER_TOKEN)
        ).status_code == 200

    def test_the_narrowest_scope_is_the_one_named(self, limited_everywhere):
        """Being told the relay is full while you personally are the reason is
        the wrong answer even though it is true, and the total is not the number
        an operator would raise first."""
        limited_everywhere.get("/api/tags", headers=bearer(TOKEN))
        error = limited_everywhere.get("/api/tags", headers=bearer(TOKEN)).json()["error"]
        assert "[limits.per_token]" in error


class TestHowManyAnswersMayBeOpenAtOnce:
    """`concurrent`, in each of the three scopes.

    A different measure from the rate, which is why it is a third key: a
    generation runs for as long as the model takes, so a rate an operator would
    call generous still lets one caller occupy the machine for minutes.
    """

    def test_a_second_request_while_the_first_is_still_answering_is_refused(
        self, capped, recorder
    ):
        with answer_in_flight(capped, recorder):
            assert capped.post("/api/tags").status_code == 429

    def test_the_refusal_says_who_refused_and_what_the_limit_was(self, capped, recorder):
        """Distinguishable from a provider's own 429, and from the three rate
        refusals that answer 429 here: an operator has to know which number to
        change."""
        with answer_in_flight(capped, recorder):
            error = capped.post("/api/tags").json()["error"]
        assert error == (
            "lmrelay: your address already has 1 requests in flight "
            "([limits.per_address]); one of yours must finish first"
        )

    def test_the_token_scope_says_it_is_your_token(self, capped_by_token, recorder):
        with answer_in_flight(capped_by_token, recorder, headers=bearer(TOKEN)):
            error = capped_by_token.post("/api/tags", headers=bearer(TOKEN)).json()["error"]
        assert error == (
            "lmrelay: your token already has 1 requests in flight "
            "([limits.per_token]); one of yours must finish first"
        )

    def test_the_total_asks_for_one_of_theirs_rather_than_one_of_yours(
        self, capped_in_total, recorder
    ):
        """At this scope the request that has to end may be anybody's, and
        telling a caller to wait for something of their own that does not exist
        is a refusal they cannot act on."""
        with answer_in_flight(capped_in_total, recorder, headers=bearer(TOKEN)):
            error = capped_in_total.post("/api/tags", headers=bearer(OTHER_TOKEN)).json()["error"]
        assert error == (
            "lmrelay: the relay is already carrying 1 requests "
            "([limits.total]); one of them must finish first"
        )

    def test_and_it_names_no_retry_after(self, capped, recorder):
        """A rate refusal carries one because it can compute it from the refill.
        A slot frees when a model finishes answering somebody else, and with no
        read timeout that may be minutes away: a guessed number would be a lie,
        and a client obeying it would retry into the same refusal."""
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

    def test_another_caller_is_not_held_up_by_a_scope_of_their_own(
        self, capped_by_token, recorder
    ):
        """One caller filling their own scope must not be an outage for
        everybody else. Without a scope like this one, the total is first come
        first served and one client with fifty threads owns all of it."""
        with answer_in_flight(capped_by_token, recorder, headers=bearer(TOKEN)) as held:
            other = held.pool.submit(
                capped_by_token.post, "/api/tags", headers=bearer(OTHER_TOKEN)
            )
            wait_until(
                lambda: len(recorder.requests) == 2,
                "the second caller never reached the upstream",
            )
        assert other.result(timeout=10).status_code == 200

    def test_but_the_total_holds_them_both(self, capped_in_total, recorder):
        """Which is the one that protects the upstream: a per-caller cap does
        not, because ten callers each inside their own still arrive together."""
        with answer_in_flight(capped_in_total, recorder, headers=bearer(TOKEN)):
            assert capped_in_total.post(
                "/api/tags", headers=bearer(OTHER_TOKEN)
            ).status_code == 429

    def test_a_refusal_by_the_total_gives_back_the_slot_it_had_already_taken(
        self, tmp_path, monkeypatch, recorder
    ):
        """The all-or-nothing property for slots. The token scope is taken
        first; when the total then refuses, the caller must not be left holding
        a slot in a scope that admitted them."""
        from lmrelay.app import app

        body = config_limits(per_token="concurrent = 4", total="concurrent = 1")
        for client in relay_with(tmp_path, monkeypatch, recorder, body, auth_enabled=True):
            with answer_in_flight(client, recorder, headers=bearer(TOKEN)):
                assert client.post(
                    "/api/tags", headers=bearer(OTHER_TOKEN)
                ).status_code == 429
                assert app.state.inflight.counts == {f"token:{TOKEN}": 1, "total": 1}

    def test_the_log_line_says_which_scope_refused(self, capped, recorder, caplog):
        """Six limits answer 429 and only three carry a Retry-After, so a line
        saying only that a 429 went out leaves an operator sizing the wrong
        number. One line, not two: the name stands where the elapsed time would,
        which for a request that was never forwarded is only the cost of
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
        assert "-> ollama: 429 (concurrent, per_address)" in refusals[0]
        assert "WARNING" in refusals[0], "a refusal is not an ordinary served request"

    def test_and_a_served_request_still_says_how_long_it_took(self, capped, recorder, caplog):
        """The other half of that substitution: it must not have taken the time
        to first byte off the requests that were served."""
        with caplog.at_level(logging.INFO):
            capped.post("/api/tags")
        assert re.search(r"-> ollama: 200 \(\d+\.\d\ds\)", caplog.text)

    def test_a_relay_that_never_asked_for_a_cap_admits_them_all(self, authed, recorder):
        """The default, and what every install that predates these keys gets."""
        from lmrelay.app import app

        assert app.state.config.limits["total"].concurrent == 0
        with answer_in_flight(authed, recorder) as held:
            second = held.pool.submit(authed.post, "/api/tags")
            wait_until(
                lambda: len(recorder.requests) == 2, "the second request was not forwarded"
            )
        assert second.result(timeout=10).status_code == 200
        assert app.state.inflight.counts == {}

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

    def test_and_costs_no_rate_token_either(self, limited, recorder):
        """It used to spend one, because the rate was charged in the middleware
        and this refusal happens in the route. One decision made in one place
        leaves one rule with no exception: nothing forwarded, nothing charged.

        The cost, stated so it is a choice: a client looping against a
        wrong-dialect path is no longer rate limited. It costs microseconds per
        400 and cannot touch a model, and fail2ban is the answer if it matters.
        """
        for _ in range(10):
            assert limited.post("/anthropic/api/chat", json={}).status_code == 400
        assert [limited.get("/api/tags").status_code for _ in range(3)] == [200] * 3

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
        this was written, with the same result: the slot came back a moment
        after the client closed, and the next request from that caller was
        admitted.

        Every scope the admission took goes back together, from that one
        `finally`, because the release covers the whole set.
        """
        from lmrelay.app import relay_body
        from lmrelay.ratelimit import InflightCounter, release_all

        async def produce():
            yield FIRST_CHUNK
            yield b'{"done":true}\n'

        held = (TESTCLIENT_KEY, "total")
        counter = InflightCounter(dict.fromkeys(held, 1))
        body = relay_body(
            httpx.Response(200, content=produce()), release_all(counter, held)
        )

        async def hang_up_after_the_first_chunk():
            assert await anext(body) == FIRST_CHUNK
            # Still held: the caller has part of an answer and is reading it.
            assert counter.counts == dict.fromkeys(held, 1)
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

        assert app.state.limiters["per_address"] is None
        write_config(tmp_path, config_limits(per_address='concurrent = 3\nrate = "3/1s"'))
        reload_config(app)
        limiter = app.state.limiters["per_address"]
        assert (limiter.rate, limiter.burst) == (3.0, 3.0)

    def test_and_off_again(self, authed, tmp_path):
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_limits(per_address='concurrent = 3\nrate = "3/1s"'))
        reload_config(app)
        write_config(tmp_path, config_limits(per_address="concurrent = 0"))
        reload_config(app)
        assert app.state.limiters["per_address"] is None

    def test_an_unrelated_reload_leaves_the_limiter_alone(self, limited, tmp_path):
        """A fresh limiter starts every caller with a full bucket, so rebuilding
        it on a reload that changed something else would hand the allowance back
        to whoever was being limited at that moment: editing an upstream URL
        would be a way to clear the limit."""
        from lmrelay.app import app, reload_config

        before = app.state.limiters["per_address"]
        for _ in range(3):
            limited.get("/api/tags")
        assert limited.get("/api/tags").status_code == 429

        write_config(tmp_path, with_setting(
            config_limits(per_address='concurrent = 3\nrate = "3/1s"'), "log_level", '"INFO"'
        ))
        reload_config(app)
        assert app.state.limiters["per_address"] is before
        assert limited.get("/api/tags").status_code == 429

    def test_but_a_changed_number_rebuilds_it(self, limited, tmp_path):
        """The other half: the numbers cannot move without a new table, since a
        bucket holds an allowance measured against the old burst."""
        from lmrelay.app import app, reload_config

        before = app.state.limiters["per_address"]
        write_config(tmp_path, config_limits(per_address='concurrent = 9\nrate = "9/1s"'))
        reload_config(app)
        limiter = app.state.limiters["per_address"]
        assert limiter is not before
        assert (limiter.rate, limiter.burst) == (9.0, 9.0)

    def test_and_only_the_scope_whose_number_moved(self, limited, tmp_path, recorder):
        """Three tables, and a caller being limited by one of them has no
        business getting its allowance back because a different number moved.
        Rebuilt as one block, raising the total would have cleared the address
        allowance of whoever was being refused at that moment."""
        from lmrelay.app import app, reload_config

        before = app.state.limiters["per_address"]
        for _ in range(3):
            limited.get("/api/tags")
        assert limited.get("/api/tags").status_code == 429

        write_config(tmp_path, config_limits(
            per_address='concurrent = 3\nrate = "3/1s"', total='concurrent = 50\nrate = "50/1s"'
        ))
        reload_config(app)
        assert app.state.limiters["per_address"] is before
        assert app.state.limiters["total"] is not None
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

    def test_a_changed_cap_is_in_force_without_a_restart(self, authed, tmp_path):
        """Read from the config at every request, like the upstreams and the
        tokens, so a reload is the whole of applying it."""
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_limits(total="concurrent = 2"))
        reload_config(app)
        assert app.state.config.limits["total"].concurrent == 2

    def test_and_it_is_not_named_as_needing_one(self, authed, tmp_path, caplog):
        """Listing it beside host and port would send an operator to restart a
        relay that had already done what they asked."""
        from lmrelay.app import app, reload_config

        write_config(tmp_path, config_limits(total="concurrent = 2"))
        with caplog.at_level(logging.WARNING):
            reload_config(app)
        assert "requests" not in caplog.text

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
