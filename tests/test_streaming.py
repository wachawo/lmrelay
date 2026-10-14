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

import socket
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
from lmrelay.config import CONFIG_ENV_VAR
from lmrelay.state import STATE_ENV_VAR

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


def bound_socket() -> socket.socket:
    """A socket on a free loopback port, held open until uvicorn serves on it.

    Handing uvicorn the bound socket, rather than a port number to bind later,
    leaves no gap in which another process can take the port.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


def run_server(app, sock: socket.socket):
    server = Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError(f"the server on {sock.getsockname()} did not start")
    return server, thread


@pytest.fixture(scope="module")
def relay_url(tmp_path_factory):
    """A real relay on a real port, in front of a real slow upstream."""
    upstream_socket, relay_socket = bound_socket(), bound_socket()
    upstream_port = upstream_socket.getsockname()[1]
    relay_port = relay_socket.getsockname()[1]

    upstream_app = Starlette(routes=[Route("/api/generate", dribble, methods=["POST"])])
    servers = [run_server(upstream_app, upstream_socket)]

    config = tmp_path_factory.mktemp("relay") / "lmrelay.toml"
    config.write_text(
        f'[server]\nhost = "127.0.0.1"\nport = {relay_port}\ndefault_upstream = "ollama"\n\n'
        f'[upstream.ollama]\nbase_url = "http://127.0.0.1:{upstream_port}"\ndialect = "ollama"\n',
        encoding="utf-8",
    )

    # The app reads the config at startup, from the environment. A MonkeyPatch
    # of this fixture's own rather than the function-scoped one, because this
    # fixture outlives a single test. The state variable goes too: conftest's
    # sweep runs per test, after this, and a state file in the developer's
    # environment would switch auth on for a relay reached with no credential.
    patch = pytest.MonkeyPatch()
    patch.setenv(CONFIG_ENV_VAR, str(config))
    patch.delenv(STATE_ENV_VAR, raising=False)
    try:
        from lmrelay.app import app

        servers.append(run_server(app, relay_socket))
        yield f"http://127.0.0.1:{relay_port}"
    finally:
        # Only the servers that started: a relay that failed to start must not
        # turn its own error into a NameError here.
        for server, unused_thread in servers:
            server.should_exit = True
        for unused_server, thread in servers:
            thread.join(timeout=10)
        patch.undo()
        upstream_socket.close()
        relay_socket.close()


class TestStreamingOverASocket:
    """What only a real socket can show."""

    def test_the_first_chunk_arrives_before_the_upstream_has_written_the_last(self, relay_url):
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


    def test_every_byte_arrives_and_in_order(self, relay_url):
        with httpx.Client(timeout=30) as client:
            response = client.post(f"{relay_url}/api/generate", json={})
        assert response.content == b"".join(CHUNKS)


def main():
    pass


if __name__ == "__main__":
    main()
