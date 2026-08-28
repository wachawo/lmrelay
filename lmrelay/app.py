#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastAPI application: authentication, access log and the relay route."""

import asyncio
import logging
import os
import signal
import threading
import time
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

# Local imports
from lmrelay.config import ConfigError, check_exposure, describe_upstreams, load_config
from lmrelay.daemon import pid_file, read_pid, recorded_bind, remove_pid, write_pid
from lmrelay.errors import LmrelayError
from lmrelay.logging_setup import setup_logging
from lmrelay.upstream import (
    build_upstream_request,
    check_caller_token,
    check_dialect,
    filter_response_headers,
    select_upstream,
)

logger = logging.getLogger(__name__)

HEALTH_PATH    = "/healthz"
# What the health route below actually answers: GET only — FastAPI, unlike bare
# Starlette, does not add HEAD. Every other method on /healthz matches the
# catch-all instead and is relayed, so an exemption by path alone would hand an
# anonymous caller the default upstream with its credentials attached.
HEALTH_METHODS = frozenset({"GET"})
RELAY_METHODS  = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def reload_config(app: FastAPI) -> None:
    """Re-read the config in place, keeping every in-flight response alive."""
    current = app.state.config
    try:
        config = load_config()
    except LmrelayError as exc:
        # LmrelayError, not ConfigError: load_config also reads state.json, whose
        # StateError is a sibling rather than a subclass, and letting it escape
        # would turn a typo into a traceback out of the signal handler.
        # A typo must not take the relay down: the process keeps serving the
        # config it already has until the file parses again.
        logger.error(f"{exc}; keeping the running config")
        return

    # Named individually rather than as one fixed sentence, so an operator who
    # changed the port can tell that connect_timeout did not also drift.
    unapplied = [
        name for name in ("host", "port", "connect_timeout")
        if getattr(config, name) != getattr(current, name)
    ]
    if unapplied:
        logger.warning(
            f"lmrelay: {', '.join(unapplied)} changed in {config.config_path} but a reload "
            f"cannot apply that — the socket is already bound and the client already open; "
            f"restart to apply"
        )

    # The httpx client is deliberately left alone: closing it would abort every
    # stream currently being relayed, and nothing a reload changes lives in it —
    # upstream URLs and headers are read from the config on every request.
    app.state.config = config
    logger.info(
        f"lmrelay reloaded <- {config.config_path} (upstreams: {describe_upstreams(config)}; "
        f"default: {config.default_upstream}; auth: "
        f"{'on' if config.auth_enabled else 'off'}, {len(config.auth_tokens)} tokens)"
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the config, claim the pidfile and open the single shared httpx client."""
    config = load_config()
    # Configured here as well as in the CLI so `uvicorn lmrelay.app:app` logs
    # in the same format as `lmrelay serve`.
    setup_logging(config.log_level)
    exposure_warning = check_exposure(config)
    if exposure_warning:
        logger.warning(exposure_warning)

    pidfile = pid_file(config.config_path)
    running_pid = read_pid(pidfile)
    if running_pid is not None:
        # Refused before the bind, which would otherwise fail with an "address
        # already in use" that names neither the other process nor a way out.
        raise ConfigError(
            f"lmrelay: already running (pid {running_pid}); "
            f"use 'lmrelay restart' or 'lmrelay stop'"
        )
    # Written by the relay itself rather than by whatever started it, so `run`,
    # `serve` and a service manager all leave the same file for `status` to read.
    write_pid(pidfile, os.getpid(), recorded_bind(config))

    app.state.config = config
    # No read timeout on purpose: a large local model can think for minutes
    # before its first token, and a read timeout would kill it in a way that
    # looks like a model fault. Failing fast on an unreachable host is the
    # useful half, so the connect timeout stays short.
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=config.connect_timeout, read=None, write=None, pool=None),
    )
    # POSIX only; Windows has no SIGHUP. uvicorn claims SIGINT and SIGTERM and
    # leaves SIGHUP alone, so this handler is not fighting it. Only the main
    # thread of the main interpreter may install one, and an app hosted in a
    # worker thread has to start anyway — it just cannot be reloaded by signal.
    if hasattr(signal, "SIGHUP") and threading.current_thread() is threading.main_thread():
        asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, lambda: reload_config(app))
    # No host:port here — the CLI may have overridden both, and uvicorn logs the
    # address it actually bound anyway.
    logger.info(
        f"lmrelay <- {config.config_path} (upstreams: {describe_upstreams(config)}; "
        f"default: {config.default_upstream})"
    )
    yield
    await app.state.http.aclose()
    # Only when it still names this process. uvicorn runs the shutdown half of
    # the lifespan when the bind fails too, and by then another relay may own
    # the file — deleting it would leave a live relay with no pidfile at all.
    # A pidfile that cannot be unlinked must not turn a clean shutdown into a
    # traceback; the next start treats a stale file as no file anyway.
    with suppress(OSError):
        if read_pid(pidfile) == os.getpid():
            remove_pid(pidfile)


app = FastAPI(title="lmrelay", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def log_and_authenticate(request: Request, call_next):
    """Check the caller's credential, then log the request line."""
    if request.url.path == HEALTH_PATH and request.method in HEALTH_METHODS:
        return await call_next(request)

    client = request.client.host if request.client else "-"
    config = request.app.state.config
    if config.auth_enabled and not check_caller_token(request.headers, config.auth_tokens):
        logger.warning(f"{client} {request.method} {request.url.path} -> -: 401 (auth)")
        return JSONResponse({"error": "lmrelay: missing or invalid credential"}, status_code=401)

    start_time = time.monotonic()
    response: Response = await call_next(request)
    # For a streamed response the handler returns once the upstream headers
    # arrive, so this is time to first byte, not the duration of the answer.
    ttfb = time.monotonic() - start_time
    upstream_name = getattr(request.state, "upstream", "-")
    logger.info(
        f"{client} {request.method} {request.url.path} -> {upstream_name}: "
        f"{response.status_code} ({ttfb:.2f}s)"
    )
    return response


@app.exception_handler(Exception)
async def handle_exception(request: Request, exc: Exception) -> JSONResponse:
    """Report anything unhandled as lmrelay's own 500 rather than a bare traceback."""
    logger.error(f"{type(exc).__name__}: {str(exc)}\n{traceback.format_exc()}")
    return JSONResponse({"error": f"lmrelay: {type(exc).__name__}: {str(exc)}"}, status_code=500)


@app.get(HEALTH_PATH)
async def healthz() -> dict[str, str]:
    """Relay-local liveness. Does not touch any upstream and needs no credential."""
    return {"status": "ok"}


@app.api_route("/{full_path:path}", methods=RELAY_METHODS)
async def relay_request(request: Request) -> Response:
    """Forward the request to its upstream and stream the answer back."""
    config = request.app.state.config
    upstream, forward_path = select_upstream(request.url.path, config)
    request.state.upstream = upstream.name

    refusal = check_dialect(upstream, forward_path)
    if refusal:
        # 400 rather than 404: a 404 would be indistinguishable from the
        # upstream's own, which is exactly the confusion being prevented.
        return JSONResponse({"error": refusal}, status_code=400)

    http = request.app.state.http
    upstream_request = build_upstream_request(http, request, upstream, forward_path)
    try:
        # stream=True returns as soon as the headers arrive and leaves the body
        # unread; the context-manager form would close the connection before the
        # caller had read a byte. BackgroundTask below releases it afterwards.
        upstream_response = await http.send(upstream_request, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        message = (
            f"lmrelay: upstream '{upstream.name}' at {upstream.base_url} "
            f"is unreachable: {type(exc).__name__}"
        )
        logger.warning(message)
        return JSONResponse({"error": message}, status_code=502)
    except httpx.HTTPError as exc:
        logger.error(f"{type(exc).__name__}: {str(exc)}\n{traceback.format_exc()}")
        return JSONResponse(
            {"error": f"lmrelay: upstream '{upstream.name}' failed: {type(exc).__name__}"},
            status_code=502,
        )

    return StreamingResponse(
        # aiter_raw, not aiter_bytes: the latter decompresses, which would
        # contradict the content-encoding header being forwarded alongside it.
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        headers=filter_response_headers(upstream_response.headers),
        background=BackgroundTask(upstream_response.aclose),
    )


def main():
    pass


if __name__ == "__main__":
    main()
