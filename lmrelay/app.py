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
from starlette.exceptions import HTTPException as StarletteHTTPException

# Local imports
from lmrelay import __version__
from lmrelay.config import ConfigError, check_exposure, describe_upstreams, load_config
from lmrelay.daemon import (
    STARTUP_KEYS,
    pid_file,
    read_pid,
    recorded_bind,
    remove_pid,
    restart_warning,
    unapplied_settings,
    write_pid,
)
from lmrelay.errors import LmrelayError
from lmrelay.logging_setup import NO_REQUEST, new_request_id, setup_logging
from lmrelay.metrics import (
    CONTENT_TYPE,
    Metrics,
    count_auth_failure,
    count_refusal,
    count_upstream_error,
    observe_request,
    render,
    track_in_flight,
)
from lmrelay.ratelimit import (
    SCOPES,
    InflightCounter,
    Refusal,
    ScopeLimits,
    admit,
    build_limiter,
    build_limiters,
    describe_rate,
    describe_scope,
    scope_keys,
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
# Exempt by method as well as path: FastAPI does not add HEAD, and any other method
# on /healthz falls through to the relay, so a path-only exemption would hand an
# anonymous caller the default upstream with its credentials attached.
HEALTH_METHODS = frozenset({"GET"})
METRICS_PATH   = "/metrics"
# Same reasoning as HEALTH_METHODS.
METRICS_METHODS = frozenset({"GET"})
RELAY_METHODS  = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


# Each refusal names its scope and the enforced limit, so an operator knows
# which of the six numbers to raise.
RATE_REFUSALS = {
    "per_token":   "lmrelay: rate limit exceeded for your token: {limit} ([limits.per_token])",
    "per_address": "lmrelay: rate limit exceeded for your address: {limit} ([limits.per_address])",
    "total":       "lmrelay: the relay's rate limit is exceeded: {limit} ([limits.total])",
}

# "one of them", not "one of yours": at the total scope the request that must
# end may be anybody's.
SLOT_REFUSALS = {
    "per_token":   "lmrelay: your token already has {count} requests in flight "
                   "([limits.per_token]); one of yours must finish first",
    "per_address": "lmrelay: your address already has {count} requests in flight "
                   "([limits.per_address]); one of yours must finish first",
    "total":       "lmrelay: the relay is already carrying {count} requests "
                   "([limits.total]); one of them must finish first",
}


def refusal_message(refusal: Refusal, limits: ScopeLimits) -> str:
    """What the caller is told: which scope refused, and the number it enforced."""
    if refusal.kind == "rate":
        return RATE_REFUSALS[refusal.scope].format(limit=describe_rate(limits))
    return SLOT_REFUSALS[refusal.scope].format(count=limits.concurrent)


def log_extra(request: Request) -> dict[str, str]:
    """The request id, as logging's `extra`, for a line written while serving a request.

    Defaulted so a line written before the middleware has run still formats.
    """
    return {"request_id": getattr(request.state, "request_id", NO_REQUEST)}


def record_request(request: Request, client: str, status: int, elapsed: float) -> None:
    """Count one request the relay answered, and write its one access line.

    Called on both ways out of the middleware: an unhandled exception becomes a 500
    in a handler starlette runs outside the user middleware stack, so it never
    comes back through `call_next`.
    """
    upstream_name = getattr(request.state, "upstream", "-")
    # Measure and scope of a refusal; six limits answer 429 and only three carry
    # a Retry-After. Shown instead of the elapsed time.
    refused = getattr(request.state, "refused", "")
    # Only a forwarded request has a real time to first byte; refusals and
    # connect failures would pollute the histogram.
    forwarded = getattr(request.state, "forwarded", False)
    observe_request(
        request.app.state.metrics, upstream_name, status, elapsed if forwarded else None
    )
    line = (
        f"{client} {request.method} {request.url.path} -> {upstream_name}: "
        f"{status} ({refused or f'{elapsed:.2f}s'})"
    )
    if refused:
        logger.warning(line, extra=log_extra(request))
    else:
        logger.info(line, extra=log_extra(request))


def caller_scopes(request: Request, config) -> dict[str, str | None]:
    """What each scope counts this request against.

    Read after authentication, so a forged token never spends the allowance of
    the caller being guessed at.
    """
    client = request.client.host if request.client else "-"
    presented = extract_caller_token(request.headers) if config.auth_enabled else None
    return scope_keys(presented, client)


def reload_config(app: FastAPI) -> None:
    """Re-read the config in place, keeping every in-flight response alive."""
    current = app.state.config
    try:
        config = load_config()
    except LmrelayError as exc:
        # LmrelayError, not ConfigError: a StateError from state.json must not
        # escape the signal handler. A bad file keeps the running config.
        logger.error(f"{exc}; keeping the running config")
        return

    # Compared with the startup config, not the last reload: a reload never
    # moves these settings, so the warning must repeat until a restart.
    started = app.state.startup_config
    unapplied = unapplied_settings(
        {name: getattr(started, name) for name in STARTUP_KEYS}, config
    )
    if unapplied:
        logger.warning(restart_warning(unapplied, config.config_path))

    # A reload can open the relay (e.g. auth off); check against the host the
    # socket is actually bound to, not the one now in the file.
    exposure_warning = check_exposure(replace(config, host=started.host))
    if exposure_warning:
        logger.warning(exposure_warning)

    # log_level is the one server setting a live process can apply.
    if config.log_level != current.log_level:
        message = f"lmrelay: log_level {current.log_level} -> {config.log_level}"
        # Logged under whichever of the two levels admits INFO.
        said_already = logger.isEnabledFor(logging.INFO)
        if said_already:
            logger.info(message)
        setup_logging(config.log_level)
        if not said_already:
            logger.info(message)

    # The httpx client is left alone: closing it would abort every live stream,
    # and upstream URLs and headers are read from the config per request.
    for scope in SCOPES:
        before, after = current.limits[scope], config.limits[scope]
        if before == after:
            continue
        # Rebuilt only when this scope changes: a new limiter refills every caller.
        # The in-flight counter is never rebuilt; it takes the cap at each acquire,
        # and a fresh one would forget slots held by answers still streaming.
        app.state.limiters[scope] = build_limiter(after)
        logger.info(
            f"lmrelay: [limits.{scope}] {describe_scope(before)} -> {describe_scope(after)} "
            f"(from the next request; answers in flight keep the slot they hold)"
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
    # Also set here so `uvicorn lmrelay.app:app` logs like `lmrelay serve`.
    setup_logging(config.log_level)
    exposure_warning = check_exposure(config)
    if exposure_warning:
        logger.warning(exposure_warning)

    pidfile = pid_file(config.config_path)
    running_pid = read_pid(pidfile)
    if running_pid is not None:
        # Before the bind, whose "address already in use" names no way out.
        raise ConfigError(
            f"lmrelay: already running (pid {running_pid}); "
            f"use 'lmrelay restart' or 'lmrelay stop'"
        )
    # Written by the relay itself so every launcher leaves the same file.
    write_pid(pidfile, os.getpid(), recorded_bind(config), config.connect_timeout)

    app.state.config = config
    # Baseline for settings a reload cannot apply (host, port, connect_timeout).
    app.state.startup_config = config
    # One limiter per scope (None when off), so a reload can rebuild just one.
    app.state.limiters = build_limiters(config.limits)
    # One counter for all scopes, always built, so no reload has to replace it.
    app.state.inflight = InflightCounter({})
    # Never rebuilt by a reload: a counter reset by a config edit would read as
    # a restart on every chart.
    app.state.metrics = Metrics()
    # No read timeout on purpose: a large local model can think for minutes before
    # its first token. Only the connect timeout is short, to fail fast.
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=config.connect_timeout, read=None, write=None, pool=None),
    )
    # POSIX only (no SIGHUP on Windows), and only the main thread may install a
    # handler; an app hosted in a worker thread starts without reload-by-signal.
    if hasattr(signal, "SIGHUP") and threading.current_thread() is threading.main_thread():
        asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, lambda: reload_config(app))
    # No host:port: the CLI may override both, and uvicorn logs the real bind.
    logger.info(
        f"lmrelay <- {config.config_path} (upstreams: {describe_upstreams(config)}; "
        f"default: {config.default_upstream})"
    )
    yield
    await app.state.http.aclose()
    # Only if it still names this process: uvicorn runs shutdown after a failed
    # bind too, when the file may belong to another live relay.
    with suppress(OSError):
        if read_pid(pidfile) == os.getpid():
            remove_pid(pidfile)


app = FastAPI(title="lmrelay", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def log_and_authenticate(request: Request, call_next):
    """Check the caller's credential, then log the request line."""
    # Set first, so every line logged for this request carries the same id.
    request.state.request_id = new_request_id()

    if request.url.path == HEALTH_PATH and request.method in HEALTH_METHODS:
        return await call_next(request)

    client = request.client.host if request.client else "-"
    config = request.app.state.config
    if config.auth_enabled and not check_caller_token(request.headers, config.auth_tokens):
        count_auth_failure(request.app.state.metrics)
        logger.warning(
            f"{client} {request.method} {request.url.path} -> -: 401 (auth)",
            extra=log_extra(request),
        )
        return JSONResponse({"error": "lmrelay: missing or invalid credential"}, status_code=401)

    # After auth but before logging and counting: a scrape is not relay traffic
    # and must not move the counters it reports.
    if request.url.path == METRICS_PATH and request.method in METRICS_METHODS:
        return await call_next(request)

    # Limits are charged once, in the relay route, so a request refused by one
    # scope is never charged to another and nothing unforwarded is charged.
    start_time = time.monotonic()
    try:
        response: Response = await call_next(request)
    except Exception:
        # The 500 handler runs outside this middleware, so a relay fault is
        # recorded here or not at all. Exception, not BaseException: a cancelled
        # request (caller hung up) was answered with nothing, not a 500.
        record_request(request, client, 500, time.monotonic() - start_time)
        raise
    # For a streamed response this is time to first byte, not total duration.
    record_request(request, client, response.status_code, time.monotonic() - start_time)
    return response


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Answer a framework refusal (e.g. 405 for TRACE) in the relay's own error shape.

    Not logged here: the middleware's access line covers it, and marking it
    refused makes that line a warning.
    """
    request.state.refused = "refused"
    return JSONResponse(
        {"error": f"lmrelay: {exc.detail.lower()} for {request.method} {request.url.path}"},
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def handle_exception(request: Request, exc: Exception) -> JSONResponse:
    """Report anything unhandled as lmrelay's own 500 rather than a bare traceback."""
    logger.error(
        f"{type(exc).__name__}: {str(exc)}\n{traceback.format_exc()}", extra=log_extra(request)
    )
    return JSONResponse({"error": f"lmrelay: {type(exc).__name__}: {str(exc)}"}, status_code=500)


@app.get(HEALTH_PATH)
async def healthz() -> dict[str, str]:
    """Relay-local liveness. Does not touch any upstream and needs no credential."""
    return {"status": "ok"}


@app.get(METRICS_PATH)
async def metrics(request: Request) -> Response:
    """The Prometheus scrape: aggregate counters, behind the same credential as everything else.

    Unlike /healthz it reveals usage, so it is authenticated (Prometheus supports
    `bearer_token`). Its own route, so scrapes never spend a caller's rate limit.
    """
    return Response(render(request.app.state.metrics, __version__), media_type=CONTENT_TYPE)


async def relay_body(
    upstream_response: httpx.Response, release: Callable[[], None]
) -> AsyncIterator[bytes]:
    """Hand the upstream's bytes on, and give the caller's slot back at the end.

    The handler returns once headers arrive, so the slot must be held until the
    body ends. This `finally` also runs when the caller hangs up mid-stream.
    """
    try:
        # aiter_raw, not aiter_bytes: decompressing would contradict the
        # forwarded content-encoding header.
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

    wrong_dialect = check_dialect(upstream, forward_path)
    if wrong_dialect:
        # 400, not 404, so it cannot be mistaken for the upstream's own 404.
        return JSONResponse({"error": wrong_dialect}, status_code=400)

    http = request.app.state.http
    upstream_request = build_upstream_request(http, request, upstream, forward_path)

    # Admitted with nothing between here and the send that can raise. `release`
    # frees every slot taken and is idempotent: a leaked slot locks that scope
    # out for the life of the process. A refusal returns a no-op release.
    refusal, release = admit(
        request.app.state.limiters,
        config.limits,
        request.app.state.inflight,
        caller_scopes(request, config),
        time.monotonic(),
    )
    if refusal is not None:
        # 429, not 503: the service is fine, and clients retry 503 hardest.
        # Logged by the middleware's access line, not here.
        request.state.refused = f"{refusal.kind}, {refusal.scope}"
        count_refusal(request.app.state.metrics, refusal.scope, refusal.kind)
        headers = None
        if refusal.kind == "rate":
            # Rounded up so the retry is not refused again. Slot refusals get no
            # Retry-After: with no read timeout, when a slot frees is unknowable.
            headers = {"Retry-After": str(max(1, ceil(refusal.wait)))}
        return JSONResponse(
            {"error": refusal_message(refusal, config.limits[refusal.scope])},
            status_code=429,
            headers=headers,
        )

    # Wrapped around release so the in-flight gauge cannot outlive the slots.
    release = track_in_flight(request.app.state.metrics, release)

    try:
        # stream=True returns at the headers and leaves the body unread; the
        # BackgroundTask below closes the connection afterwards.
        upstream_response = await http.send(upstream_request, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        release()
        count_upstream_error(request.app.state.metrics, upstream.name, type(exc).__name__)
        message = (
            f"lmrelay: upstream '{upstream.name}' at {upstream.base_url} "
            f"is unreachable: {type(exc).__name__}"
        )
        logger.warning(message, extra=log_extra(request))
        return JSONResponse({"error": message}, status_code=502)
    except httpx.HTTPError as exc:
        release()
        count_upstream_error(request.app.state.metrics, upstream.name, type(exc).__name__)
        logger.error(
            f"{type(exc).__name__}: {str(exc)}\n{traceback.format_exc()}",
            extra=log_extra(request),
        )
        return JSONResponse(
            {"error": f"lmrelay: upstream '{upstream.name}' failed: {type(exc).__name__}"},
            status_code=502,
        )
    except BaseException:
        # BaseException: a caller hang-up raises CancelledError, and there is no
        # body generator yet to release the slot.
        release()
        raise

    # Set explicitly rather than inferred from the status: an upstream may
    # answer 400 or 502 itself, and that is still a relayed response.
    request.state.forwarded = True

    try:
        return StreamingResponse(
            relay_body(upstream_response, release),
            status_code=upstream_response.status_code,
            headers=filter_response_headers(upstream_response.headers),
            background=BackgroundTask(upstream_response.aclose),
        )
    except BaseException:
        # Building the response can fail (starlette encodes headers as latin-1,
        # httpx decodes UTF-8), and then neither the generator nor the
        # BackgroundTask would ever run to free the slot and the connection.
        release()
        await upstream_response.aclose()
        raise


def main():
    pass


if __name__ == "__main__":
    main()
