#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared fixtures: a config and its state on disk, and the app wired to a recording upstream."""

import os
import threading
from pathlib import Path

import anyio
import httpx
import pytest
from starlette.testclient import TestClient

# Local imports
from lmrelay.config import CONFIG_ENV_VAR, ENV_PREFIX, TOKEN_ENV_VAR
from lmrelay.state import STATE_NAME, CallerToken, RelayState, save_state

TOKEN = "caller-token-value"
CREATED_AT = "2026-01-01T00:00:00Z"

# Two upstreams pointed at hosts nothing resolves: every request in these tests
# is answered by the recording transport below, so a test that accidentally
# escapes to the network fails loudly instead of hanging on a real connection.
CONFIG_TEMPLATE = """
[server]
host             = "127.0.0.1"
port             = 11435
default_upstream = "ollama"

[auth]
token = "{token}"

[upstream.ollama]
base_url = "http://ollama.invalid:11434"
dialect  = "ollama"

[upstream.anthropic]
base_url = "https://anthropic.invalid"
dialect  = "anthropic"
headers  = {{ "x-api-key" = "provider-secret", "anthropic-version" = "2023-06-01" }}

[upstream.openai]
base_url = "https://openai.invalid/"
dialect  = "openai"
headers  = {{ "Authorization" = "Bearer provider-bearer" }}
"""


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep the suite inside tmp_path: no test may reach the real ~/.lmrelay.

    A machine that has ever run `lmrelay token gen` would otherwise hand a test
    real tokens and a real auth switch through $LMRELAY_STATE, and anything that
    resolves `~` at call time, `lmrelay init` above all, would write there.

    Every LMRELAY_ name goes, not just that one: the environment is now a
    config source of its own, so a developer who exports LMRELAY_LIMITS_TOTAL_RATE
    or LMRELAY_AUTH_ENABLED in their own shell would otherwise be running a
    different suite from CI, and the difference would show up as a limit test
    failing for a reason nothing in the file explains.
    """
    for name in [name for name in os.environ if name.startswith(ENV_PREFIX)]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))


def write_config(path, body: str) -> str:
    """Write a config file and return its path as a string."""
    target = path / "lmrelay.toml"
    target.write_text(body, encoding="utf-8")
    return str(target)


def write_state(path, auth_enabled: bool = False, tokens=(), providers=None) -> Path:
    """Write state.json beside the config and return its path.

    Built through save_state rather than by hand: the file is the CLI's, and a
    test that wrote its own JSON would stop testing the format the CLI produces.
    """
    state = RelayState(
        auth_enabled=auth_enabled,
        tokens=tuple(
            CallerToken(id=index, token=value, label="", created_at=CREATED_AT)
            for index, value in enumerate(tokens, start=1)
        ),
        providers=dict(providers or {}),
        next_token_id=len(tokens) + 1,
        state_path=Path(path) / STATE_NAME,
    )
    save_state(state)
    return state.state_path


class Recorder:
    """The upstream: records what arrived and answers with what a test asked for."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.headers = {"content-type": "application/json"}
        self.body = b'{"ok": true}'
        self.chunks: list[bytes] | None = None
        self.raises: Exception | None = None
        # Held shut between the first chunk and the rest when a test wants to
        # look at what the caller has while the upstream is still producing.
        self.gate: threading.Event | None = None
        # Appended to as the response body is pulled, so a test can tell whether
        # the caller was reading while the upstream was still producing.
        self.produced: list[bytes] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        # Read rather than kept as a stream: the assertions are about the bytes
        # that arrived, and an unread stream would leave them unavailable. Async
        # because the relay forwards the caller's body as an async iterator, and
        # a sync read cannot consume one.
        await request.aread()
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        recorder = self
        # Always a generator, never bytes. A Response built from bytes is
        # already read, and the relay asks for `aiter_raw()` on a response it
        # opened with stream=True, which a real upstream leaves unread. Handing
        # back a consumed one would fail here for a reason no caller can meet.
        chunks = self.chunks if self.chunks is not None else [self.body]

        # An async generator, because an AsyncClient requires an async stream.
        async def produce():
            for index, chunk in enumerate(chunks):
                if index and recorder.gate is not None:
                    # Waited on in a worker thread so the event loop stays free
                    # to hand the chunk already yielded to the caller.
                    await anyio.to_thread.run_sync(recorder.gate.wait)
                recorder.produced.append(chunk)
                yield chunk

        return httpx.Response(self.status, headers=self.headers, content=produce())

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "the upstream was never called"
        return self.requests[-1]


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def relay(tmp_path, monkeypatch, recorder):
    """A running relay whose upstream is the recorder, with the standard config."""
    monkeypatch.setenv(CONFIG_ENV_VAR, write_config(tmp_path, CONFIG_TEMPLATE.format(token=TOKEN)))
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    # The switch lives in the state, so the [auth] token above is only a
    # credential that gets checked once something turns checking on.
    write_state(tmp_path, auth_enabled=True)
    yield from build_relay(recorder)


def build_relay(recorder):
    """Start the app and swap its client for one that answers from the recorder."""
    from lmrelay.app import app

    with TestClient(app) as client:
        # The real client was opened by the lifespan and is replaced rather than
        # reconfigured, so nothing in these tests can reach a socket. Closing it
        # is this fixture's job: the shutdown hook will close the replacement.
        real_client = app.state.http
        app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handle))
        try:
            yield client
        finally:
            client.portal.call(real_client.aclose)


@pytest.fixture
def authed(relay):
    """The same relay, with the caller credential already on every request."""
    relay.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return relay
