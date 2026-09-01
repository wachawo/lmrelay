#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Streaming, over real sockets.

The relay's reason for being a StreamingResponse is that a caller sees the
first token while the model is still writing the rest. That cannot be measured
through an in-process test client, which drives the app through a portal that
settles the response before handing it back, so this module runs the relay
under uvicorn against an upstream that answers slowly, and reads it with an
ordinary HTTP client.
"""

import os
import threading
import time

import anyio
import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

# Local imports
from tests.conftest import free_port

# Set and restored together. LMRELAY_STATE and LMRELAY_TOKEN are cleared beside
# the config because a state file or token in the developer's environment would
# switch auth on for a relay this module reaches with no credential.
RELAY_ENV = ("LMRELAY_CONFIG", "LMRELAY_STATE", "LMRELAY_TOKEN")

CHUNKS = [b"first\n", b"second\n", b"third\n"]
# Long enough that "the whole answer was written before anything was sent" and
# "the first chunk was forwarded as it arrived" cannot be confused for one
# another on a loaded machine.
CHUNK_DELAY = 0.6


async def dribble(unused_request):
    """An upstream that writes its answer a chunk at a time, like a model does."""

    async def produce():
        for index, chunk in enumerate(CHUNKS):
            if index:
                await anyio.sleep(CHUNK_DELAY)
            yield chunk

    return StreamingResponse(produce(), media_type="application/x-ndjson")


class Server(uvicorn.Server):
    """A uvicorn that can be started and stopped from another thread."""

    def install_signal_handlers(self) -> None:
        # Would fail outside the main thread, and nothing here sends signals.
        pass


def run_server(app, port: int):
    server = Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError(f"the server on port {port} did not start")
    return server, thread


@pytest.fixture(scope="module")
def relay_url(tmp_path_factory):
    """A real relay on a real port, in front of a real slow upstream."""
    upstream_port = free_port()
    relay_port = free_port()

    upstream_app = Starlette(routes=[Route("/api/generate", dribble, methods=["POST"])])
    upstream, upstream_thread = run_server(upstream_app, upstream_port)

    config = tmp_path_factory.mktemp("relay") / "lmrelay.toml"
    config.write_text(
        f'[server]\nhost = "127.0.0.1"\nport = {relay_port}\ndefault_upstream = "ollama"\n\n'
        f'[upstream.ollama]\nbase_url = "http://127.0.0.1:{upstream_port}"\ndialect = "ollama"\n',
        encoding="utf-8",
    )

    # The app reads the config at startup, from the environment. Set here rather
    # than through monkeypatch: this fixture outlives a single test.
    previous = {name: os.environ.get(name) for name in RELAY_ENV}
    os.environ["LMRELAY_CONFIG"] = str(config)
    os.environ.pop("LMRELAY_STATE", None)
    os.environ.pop("LMRELAY_TOKEN", None)
    try:
        from lmrelay.app import app

        relay, relay_thread = run_server(app, relay_port)
        yield f"http://127.0.0.1:{relay_port}"
    finally:
        relay.should_exit = True
        upstream.should_exit = True
        relay_thread.join(timeout=10)
        upstream_thread.join(timeout=10)
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_the_first_chunk_arrives_before_the_upstream_has_written_the_last(relay_url):
    """A token that only arrives once the whole answer is written arrived late.

    The upstream holds for CHUNK_DELAY between chunks, so a relay that read the
    answer whole before sending could not deliver line one until every line
    existed, which is the arithmetic the assertion rests on.
    """
    started = time.monotonic()
    with (
        httpx.Client(timeout=30) as client,
        client.stream("POST", f"{relay_url}/api/generate", json={"stream": True}) as response,
    ):
        assert response.status_code == 200
        lines = response.iter_lines()
        first = next(lines)
        first_at = time.monotonic() - started
        rest = list(lines)
        whole_at = time.monotonic() - started

    assert first == "first"
    assert rest == ["second", "third"]
    # The first line beat the second chunk being written at all.
    assert first_at < CHUNK_DELAY, f"the first line took {first_at:.2f}s"
    # And the answer really was spread out, so the check above means something.
    assert whole_at >= CHUNK_DELAY * (len(CHUNKS) - 1)


def test_every_byte_arrives_and_in_order(relay_url):
    with httpx.Client(timeout=30) as client:
        response = client.post(f"{relay_url}/api/generate", json={})
    assert response.content == b"".join(CHUNKS)
