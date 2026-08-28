#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The relay end to end: what a caller gets, and what the upstream is sent."""

import logging
import os

import httpx
import pytest
from starlette.testclient import TestClient

# Local imports
from lmrelay.daemon import PID_NAME, read_pid, write_pid
from lmrelay.state import STATE_NAME
from tests.conftest import CONFIG_TEMPLATE, TOKEN, build_relay, write_config

EXTRA_UPSTREAM = """
[upstream.second]
base_url = "http://second.invalid:11434"
dialect  = "ollama"
"""


class TestTheDoor:
    """Who gets in."""

    def test_a_request_with_no_credential_is_refused(self, relay, recorder):
        response = relay.post("/api/chat", json={})
        assert response.status_code == 401
        # And nothing reached the upstream — a 401 that still forwarded would
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
        """The health route answers GET only — FastAPI does not add HEAD — so
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
        authenticates them HERE and is meaningless — and dangerous — onward."""
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
    """Failures of the connection, reported as the relay's own."""

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

    def test_a_read_failure_mid_answer_is_also_a_502(self, authed, recorder):
        recorder.raises = httpx.ReadError("connection reset")
        response = authed.post("/api/chat", json={})
        assert response.status_code == 502
        assert "ollama" in response.json()["error"]


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
        the live relay that beat it to the port — leaving a running relay that
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
