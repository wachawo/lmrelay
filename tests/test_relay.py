#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The relay end to end: what a caller gets, and what the upstream is sent."""

import json
import logging

import anyio
import httpx
import pytest
from starlette.requests import Request

# Local imports
from lmrelay.config import CONFIG_ENV_VAR
from tests.conftest import (
    CONFIG_TEMPLATE,
    FIRST_CHUNK,
    TOKEN,
    build_relay,
    fails_after_the_first_chunk,
    relay_records,
    write_config,
    write_state,
)


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

    def test_an_unhandled_fault_tells_the_caller_the_request_id_and_nothing_else(self):
        """An exception message can carry paths, header values or a fragment of an
        upstream's answer; the operator finds the rest in the log by the id."""
        from lmrelay.app import handle_exception

        request = Request({"type": "http", "headers": [], "state": {"request_id": "1a2b3c4d"}})
        response = anyio.run(handle_exception, request, ValueError("/home/u/.lmrelay sk-secret"))
        assert response.status_code == 500
        error = json.loads(response.body)["error"]
        assert "1a2b3c4d" in error
        assert "sk-secret" not in error and "ValueError" not in error

    def test_and_it_leaves_one_access_line_marked_as_a_refusal(self, authed, caplog):
        """The handler runs inside the middleware, so its answer returns through
        the access log. Logging it there as well wrote two lines for one request."""
        with caplog.at_level(logging.INFO):
            authed.request("TRACE", "/api/tags")
        assert [(record.levelname, record.getMessage()) for record in relay_records(caplog)] == [
            ("WARNING", "testclient TRACE /api/tags -> -: 405 (refused)")
        ]

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
        body = CONFIG_TEMPLATE.format(token="").replace('token = ""', "")
        monkeypatch.setenv(CONFIG_ENV_VAR, str(write_config(tmp_path, body)))
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
        monkeypatch.setenv(
            CONFIG_ENV_VAR, str(write_config(tmp_path, CONFIG_TEMPLATE.format(token=TOKEN)))
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


def main():
    pass


if __name__ == "__main__":
    main()
