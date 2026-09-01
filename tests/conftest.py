#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared fixtures: a config and its state on disk, and the app wired to a recording upstream."""

import logging
import os
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

import anyio
import httpx
import pytest
from starlette.testclient import TestClient

# Local imports
from lmrelay.cli import build_parser
from lmrelay.config import CONFIG_ENV_VAR
from lmrelay.logging_setup import LOG_DATEFMT, LOG_FORMAT
from lmrelay.state import STATE_NAME, CallerToken, RelayState, save_state

TOKEN = "caller-token-value"
# A second credential, so that two callers in one test are told apart by what
# they present rather than by an address the test client cannot vary.
OTHER_TOKEN = "another-callers-token"
CREATED_AT = "2026-01-01T00:00:00Z"

# Spelled out here rather than imported: the package no longer has a prefix
# constant to import, because settings no longer come from the environment. What
# is left under it are paths and the CLI's own plumbing, and the suite still has
# to be sure none of them arrived from the developer's shell.
ENV_PREFIX = "LMRELAY_"

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

# The least a config can say: one upstream, everything else defaulted.
MINIMAL = """
[upstream.ollama]
base_url = "http://127.0.0.1:11434"
dialect  = "ollama"
"""

FIRST_CHUNK = b'{"response":"a"}\n'

# Two chunks, because the hold in the recorder happens between them: an answer
# that has produced the first and not the second is an answer under way.
GATED_CHUNKS = [FIRST_CHUNK, b'{"done":true}\n']

# The relay's own formatter, for tests that read a line the way the file holds it.
FORMATTER = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep the suite inside tmp_path: no test may reach the real ~/.lmrelay.

    A machine that has ever run `lmrelay token gen` would otherwise hand a test
    real tokens and a real auth switch through $LMRELAY_STATE, and anything that
    resolves `~` at call time, a `~` in $LMRELAY_STATE for one, would write
    there. `lmrelay init` is not among them: its home path is fixed when the
    package is imported, which is why the tests of it patch that constant.

    Every LMRELAY_ name goes, not just that one. $LMRELAY_CONFIG points the
    whole suite at another file, and $LMRELAY_BIND and $LMRELAY_SERVICE are read
    by the process-control tests, so a developer with any of them exported in
    their own shell would be running a different suite from CI.
    """
    for name in [name for name in os.environ if name.startswith(ENV_PREFIX)]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))


def write_config(path, body: str) -> Path:
    """Write a config file into a directory, creating it, and return the file's path."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "lmrelay.toml"
    target.write_text(body, encoding="utf-8")
    return target


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


def run_command(argv: list[str]) -> None:
    """Parse and dispatch exactly as main() does, minus its exit handling."""
    args = build_parser().parse_args(argv)
    args.handler(args)


def free_port() -> int:
    """A port the kernel says is free, so a developer already running lmrelay
    does not see a spurious failure here."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_until(condition, what: str, timeout: float = 10.0) -> None:
    """Block until `condition` holds, or fail saying what never happened."""
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert condition(), what


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def relay_records(caplog) -> list[logging.LogRecord]:
    """Only what the relay itself said. Whatever http client is installed beside
    it announces its own requests, and a test about lmrelay's log must not pass
    or fail on that."""
    return [record for record in caplog.records if record.name == "lmrelay.app"]


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


def relay_with(tmp_path, monkeypatch, recorder, body: str, auth_enabled: bool = False):
    """A relay on a given config body, with auth off unless a test needs it.

    Auth off keys the address scope on what every request from the in-process
    client shares: one caller, sending more than one thing at once. Auth on adds
    two credentials, which is the only way two callers in one test can be told
    apart, since the client cannot vary its address.
    """
    monkeypatch.setenv(CONFIG_ENV_VAR, str(write_config(tmp_path, body)))
    write_state(
        tmp_path,
        auth_enabled=auth_enabled,
        tokens=(TOKEN, OTHER_TOKEN) if auth_enabled else (),
    )
    yield from build_relay(recorder)


def config_limits(**scopes: str) -> str:
    """The standard config with one [limits.<scope>] table per keyword argument.

    Appended rather than edited into [server], because a limit is its own table
    now: `config_limits(total="concurrent = 1")`.
    """
    body = CONFIG_TEMPLATE.format(token=TOKEN)
    return body + "".join(f"\n[limits.{scope}]\n{keys}\n" for scope, keys in scopes.items())


@pytest.fixture
def relay(tmp_path, monkeypatch, recorder):
    """A running relay whose upstream is the recorder, with the standard config."""
    monkeypatch.setenv(
        CONFIG_ENV_VAR, str(write_config(tmp_path, CONFIG_TEMPLATE.format(token=TOKEN)))
    )
    # The switch lives in the state, so the [auth] token above is only a
    # credential that gets checked once something turns checking on.
    write_state(tmp_path, auth_enabled=True)
    yield from build_relay(recorder)


@pytest.fixture
def authed(relay):
    """The same relay, with the caller credential already on every request."""
    relay.headers.update(bearer(TOKEN))
    return relay


@pytest.fixture
def limited(tmp_path, monkeypatch, recorder):
    """One address allowed 3 a second, which is also 3 at once."""
    yield from relay_with(
        tmp_path, monkeypatch, recorder,
        config_limits(per_address='concurrent = 3\nrate = "3/1s"'),
    )


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


def main():
    pass


if __name__ == "__main__":
    main()
