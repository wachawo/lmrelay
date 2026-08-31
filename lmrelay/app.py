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
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from math import ceil

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

# Local imports
from lmrelay.config import ConfigError, check_exposure, describe_upstreams, load_config
from lmrelay.daemon import pid_file, read_pid, recorded_bind, remove_pid, write_pid
from lmrelay.errors import LmrelayError
from lmrelay.logging_setup import setup_logging
from lmrelay.ratelimit import (
    InflightCounter,
    build_limiter,
    effective_burst,
    limiter_key,
    release_once,
)
from lmrelay.upstream import (
    build_upstream_request,
    check_caller_token,
    check_dialect,
    extract_caller_token,
    filter_response_headers,
    select_upstream,
)

logger = logging.getLogger(__name__)

HEALTH_PATH    = "/healthz"
# What the health route below actually answers: GET only. FastAPI, unlike bare
# Starlette, does not add HEAD. Every other method on /healthz matches the
# catch-all instead and is relayed, so an exemption by path alone would hand an
# anonymous caller the default upstream with its credentials attached.
HEALTH_METHODS = frozenset({"GET"})
RELAY_METHODS  = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def describe_rate(config) -> str:
    """The limit as the limiter will enforce it, for a log line or a refusal.

    The burst is the effective one, not the configured one. They differ at the
    default: `rate_burst = 0` is read as 1, and quoting the configured number
    told a caller "burst 0" while the limiter was allowing one request through,
    which reads as a relay that refuses everything.
    """
    if config.rate_limit <= 0:
        return "off"
    return f"{config.rate_limit:g}/s burst {effective_burst(config.rate_burst):g}"


def describe_concurrency(config) -> str:
    """The cap as an operator wrote it, for a log line."""
    if config.max_concurrent <= 0:
        return "off"
    return f"{config.max_concurrent} in flight"


def caller_key(request: Request, config) -> str:
    """Who this request counts against, for the rate limit and the cap alike.

    Both read it through the same function so that the two keys cannot drift
    apart: an operator who has learnt what `rate_limit` counts has learnt what
    `max_concurrent` counts. Read after authentication in both places, so a
    guessed credential is never the key.
    """
    client = request.client.host if request.client else "-"
    presented = extract_caller_token(request.headers) if config.auth_enabled else None
    return limiter_key(presented, client)


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

    # Measured against what this process started with, not against what it last
    # read: no reload moves these three, so a second reload that leaves the port
    # where the first one put it has still not moved the socket, and must say so
    # again. Named individually rather than as one fixed sentence, so an
    # operator who changed the port can tell that connect_timeout did not drift.
    started = app.state.startup_config
    unapplied = [
        name for name in ("host", "port", "connect_timeout")
        if getattr(config, name) != getattr(started, name)
    ]
    if unapplied:
        logger.warning(
            f"lmrelay: {', '.join(unapplied)} changed in {config.config_path} but a reload "
            f"cannot apply that: the socket is already bound and the client already open; "
            f"restart to apply"
        )

    # Re-checked because a reload is one of the ways to create the condition it
    # warns about: `lmrelay auth false` opens a relay that is already listening.
    # Asked about the host the socket is on rather than the one now in the file,
    # since a file edited back to 127.0.0.1 has closed nothing until a restart.
    exposure_warning = check_exposure(replace(config, host=started.host))
    if exposure_warning:
        logger.warning(exposure_warning)

    # Applied rather than listed above: a logger can be reconfigured under a
    # live process, where the socket is already bound and the client already
    # open, so log_level is the one server setting a reload can honour.
    if config.log_level != current.log_level:
        message = f"lmrelay: log_level {current.log_level} -> {config.log_level}"
        # Said under whichever of the two levels admits an INFO line: the old one
        # where it still does, the new one otherwise. Only ever before the switch
        # would lose every change made out of WARNING or above.
        said_already = logger.isEnabledFor(logging.INFO)
        if said_already:
            logger.info(message)
        setup_logging(config.log_level)
        if not said_already:
            logger.info(message)

    # The httpx client is deliberately left alone: closing it would abort every
    # stream currently being relayed, and nothing a reload changes lives in it:
    # upstream URLs and headers are read from the config on every request.
    if (config.rate_limit, config.rate_burst) != (current.rate_limit, current.rate_burst):
        # Rebuilt only when the numbers move: a new limiter starts every caller
        # full, so doing this on an unrelated reload would clear the allowance
        # of whoever was being limited at that moment.
        app.state.limiter = build_limiter(config.rate_limit, config.rate_burst)
        logger.info(
            f"lmrelay: rate limit {describe_rate(current)} -> {describe_rate(config)}"
        )

    if config.max_concurrent != current.max_concurrent:
        # Nothing is rebuilt here, unlike the limiter above: the counter is
        # asked for the limit at every acquire, so replacing app.state.config
        # below is the whole of applying it. A fresh counter would forget the
        # slots held by every answer still streaming, and each of them would
        # release one that had never been taken.
        logger.info(
            f"lmrelay: concurrency {describe_concurrency(current)} -> "
            f"{describe_concurrency(config)} (from the next request; answers in flight "
            f"keep the slot they hold)"
        )

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
    # Kept alongside it as the baseline a reload measures host, port and
    # connect_timeout against: they are what this process bound and opened with,
    # and app.state.config moves out from under them at every accepted reload.
    app.state.startup_config = config
    # No read timeout on purpose: a large local model can think for minutes
    # before its first token, and a read timeout would kill it in a way that
    # looks like a model fault. Failing fast on an unreachable host is the
    # useful half, so the connect timeout stays short.
    app.state.limiter = build_limiter(config.rate_limit, config.rate_burst)
    # Always built, even with the cap off, so that no reload has to replace it
    # and no request has to ask whether it exists. An empty table costs nothing:
    # with max_concurrent = 0 every acquire is allowed, and the release at the
    # end of each answer takes the entry out again.
    app.state.inflight = InflightCounter({})
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=config.connect_timeout, read=None, write=None, pool=None),
    )
    # POSIX only; Windows has no SIGHUP. uvicorn claims SIGINT and SIGTERM and
    # leaves SIGHUP alone, so this handler is not fighting it. Only the main
    # thread of the main interpreter may install one, and an app hosted in a
    # worker thread has to start anyway; it just cannot be reloaded by signal.
    if hasattr(signal, "SIGHUP") and threading.current_thread() is threading.main_thread():
        asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, lambda: reload_config(app))
    # No host:port here: the CLI may have overridden both, and uvicorn logs the
    # address it actually bound anyway.
    logger.info(
        f"lmrelay <- {config.config_path} (upstreams: {describe_upstreams(config)}; "
        f"default: {config.default_upstream})"
    )
    yield
    await app.state.http.aclose()
    # Only when it still names this process. uvicorn runs the shutdown half of
    # the lifespan when the bind fails too, and by then another relay may own
    # the file, and deleting it would leave a live relay with no pidfile at all.
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

    limiter = request.app.state.limiter
    if limiter is not None:
        # After auth on purpose: a guessed credential must not spend the
        # allowance of the caller whose token was being guessed at. Keyed on
        # the token only when auth is on, since otherwise it proves nothing.
        now = time.monotonic()
        wait = limiter.take(caller_key(request, config), now)
        limiter.sweep(now)
        if wait > 0:
            logger.warning(
                f"{client} {request.method} {request.url.path} -> -: 429 (rate limit)"
            )
            return JSONResponse(
                {"error": f"lmrelay: rate limit of {describe_rate(config)} exceeded"},
                status_code=429,
                # Whole seconds, rounded up: the header takes no fractions, and
                # rounding down would invite a retry that is refused again.
                headers={"Retry-After": str(max(1, ceil(wait)))},
            )

    start_time = time.monotonic()
    response: Response = await call_next(request)
    # For a streamed response the handler returns once the upstream headers
    # arrive, so this is time to first byte, not the duration of the answer.
    ttfb = time.monotonic() - start_time
    upstream_name = getattr(request.state, "upstream", "-")
    # A route that refused leaves the name of the limit it refused on: two of
    # them answer 429 and only one carries a Retry-After, so the line has to say
    # which. It stands in place of the elapsed time, which for a request that
    # was never forwarded would only be the cost of refusing it, and it keeps
    # the refusal to one line, at the level the other refusals are logged at.
    refused = getattr(request.state, "refused", "")
    line = (
        f"{client} {request.method} {request.url.path} -> {upstream_name}: "
        f"{response.status_code} ({refused or f'{ttfb:.2f}s'})"
    )
    if refused:
        logger.warning(line)
    else:
        logger.info(line)
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


async def relay_body(
    upstream_response: httpx.Response, release: Callable[[], None]
) -> AsyncIterator[bytes]:
    """Hand the upstream's bytes on, and give the caller's slot back at the end.

    The slot cannot be released where the handler returns. For a streamed answer
    the handler returns as soon as the upstream's headers arrive, before a byte
    of body has been written, so a cap that freed the slot there would be
    counting the time it takes to reach a model rather than the time a caller
    spends occupying it, and would bound nothing.

    Released from this `finally` rather than from the BackgroundTask alongside
    it because this one runs on both ways out: the answer ending, and the caller
    hanging up part way through it, which closes the generator here.
    """
    try:
        # aiter_raw, not aiter_bytes: the latter decompresses, which would
        # contradict the content-encoding header being forwarded alongside it.
        async for chunk in upstream_response.aiter_raw():
            yield chunk
    finally:
        release()


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

    # Taken here, with nothing between it and the send below that can raise:
    # after the refusals that cost no upstream call, and after the request has
    # been built, so that the slot is held for exactly as long as the relay is
    # occupying the upstream with it.
    inflight = request.app.state.inflight
    key = caller_key(request, config)
    if not inflight.acquire(key, config.max_concurrent):
        # 429 rather than 503: nothing is wrong with the relay or with the
        # upstream, and this same request from another caller would be served.
        # A 503 would say the service is unavailable, which is both untrue and
        # the status many clients retry hardest against.
        #
        # And no Retry-After, unlike the rate limiter's 429, which computes an
        # honest one: a slot here frees when a model finishes answering someone
        # else, and with no read timeout that can be minutes away. The relay
        # does not know the number, so it does not name one.
        message = (
            f"lmrelay: too many simultaneous requests "
            f"(limit {config.max_concurrent}); one of yours must finish first"
        )
        # Named rather than logged here: the middleware writes one access line
        # per request, and a second line from this route would say the same
        # thing again. It logs this one as a refusal, and names the limit.
        request.state.refused = "concurrency"
        return JSONResponse({"error": message}, status_code=429)

    # Held from here until the answer ends. Idempotent because the ways out
    # below are meant not to overlap and a leaked slot is unrecoverable: it
    # would lock that caller out for the life of the process.
    release = release_once(inflight, key)

    try:
        # stream=True returns as soon as the headers arrive and leaves the body
        # unread; the context-manager form would close the connection before the
        # caller had read a byte. BackgroundTask below releases it afterwards.
        upstream_response = await http.send(upstream_request, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        release()
        message = (
            f"lmrelay: upstream '{upstream.name}' at {upstream.base_url} "
            f"is unreachable: {type(exc).__name__}"
        )
        logger.warning(message)
        return JSONResponse({"error": message}, status_code=502)
    except httpx.HTTPError as exc:
        release()
        logger.error(f"{type(exc).__name__}: {str(exc)}\n{traceback.format_exc()}")
        return JSONResponse(
            {"error": f"lmrelay: upstream '{upstream.name}' failed: {type(exc).__name__}"},
            status_code=502,
        )
    except BaseException:
        # BaseException, not Exception: a caller that hangs up while the model
        # is still being reached cancels this task, and CancelledError is not an
        # Exception. There is no body generator yet to release the slot, so it
        # has to happen here or not at all.
        release()
        raise

    try:
        return StreamingResponse(
            relay_body(upstream_response, release),
            status_code=upstream_response.status_code,
            headers=filter_response_headers(upstream_response.headers),
            background=BackgroundTask(upstream_response.aclose),
        )
    except BaseException:
        # Guarded because building the response can fail on the upstream's own
        # headers, and the two things that would otherwise give the slot back
        # are both parts of the object being built: the generator's `finally`
        # never runs because nothing iterates it, and the BackgroundTask that
        # closes the connection is never attached to anything that will.
        #
        # Starlette encodes header values as latin-1 while httpx decodes them
        # as UTF-8, so one non-ASCII header from a provider raises here. Without
        # this the slot was held for the life of the process, and the caller was
        # locked out of its own cap after `max_concurrent` such answers, with
        # nothing in the log naming the cap.
        release()
        await upstream_response.aclose()
        raise


def main():
    pass


if __name__ == "__main__":
    main()
