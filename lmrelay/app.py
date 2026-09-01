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
from lmrelay.daemon import pid_file, read_pid, recorded_bind, remove_pid, write_pid
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
# What the health route below actually answers: GET only. FastAPI, unlike bare
# Starlette, does not add HEAD. Every other method on /healthz matches the
# catch-all instead and is relayed, so an exemption by path alone would hand an
# anonymous caller the default upstream with its credentials attached.
HEALTH_METHODS = frozenset({"GET"})
METRICS_PATH   = "/metrics"
# Read the same way as HEALTH_METHODS above, and for the same reason: this is
# what the metrics route answers, and every other method on the path is relayed.
METRICS_METHODS = frozenset({"GET"})
RELAY_METHODS  = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


# What a caller is told, per scope and per measure. "429" on its own leaves an
# operator guessing which of six numbers to raise, so each one names the scope
# and quotes the limit as the relay is enforcing it rather than as the file
# spells it.
RATE_REFUSALS = {
    "per_token":   "lmrelay: rate limit exceeded for your token: {limit} ([limits.per_token])",
    "per_address": "lmrelay: rate limit exceeded for your address: {limit} ([limits.per_address])",
    "total":       "lmrelay: the relay's rate limit is exceeded: {limit} ([limits.total])",
}

# The last one says "one of them", not "one of yours": at the total scope the
# request that has to end may be anybody's, and telling a caller to wait for
# something of their own that does not exist is a refusal they cannot act on.
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


def describe_slots(limits: ScopeLimits) -> str:
    """One scope's cap as a log line names it."""
    return f"{limits.concurrent} at once" if limits.concurrent > 0 else "off"


def log_extra(request: Request) -> dict[str, str]:
    """The request id, as logging's `extra`, for a line written while serving a request.

    Read off the request rather than out of a context variable, so that the id
    travels the same way the upstream name already does and nothing has to be
    reset when a task ends. Defaulted for the same reason the log filter has a
    default: a line written before the middleware has run must still format.
    """
    return {"request_id": getattr(request.state, "request_id", NO_REQUEST)}


def record_request(request: Request, client: str, status: int, elapsed: float) -> None:
    """Count one request the relay answered, and write its one access line.

    Called on both ways out of the middleware, so that the counters and the log
    agree with what the caller was actually sent. The second way out is the whole
    reason this is a function rather than the tail of the middleware: an
    unhandled exception becomes lmrelay's own 500 in a handler that starlette
    lifts outside the user middleware stack, so it never comes back through
    `call_next`, and until this it was the one status counted in no family and
    given no access line at all.
    """
    upstream_name = getattr(request.state, "upstream", "-")
    # A route that refused leaves the measure and the scope it refused on: six
    # limits answer 429 and only three of them carry a Retry-After, so the line
    # has to say which. It stands in place of the elapsed time, which for a
    # request that was never forwarded would only be the cost of refusing it,
    # and it keeps the refusal to one line, at the level the others use.
    refused = getattr(request.state, "refused", "")
    # Timed only when an upstream answered, which is what this flag says. A
    # dialect refusal, an unreachable host and a fault in the relay all leave a
    # status and an elapsed time here, and none of the three is a time to first
    # byte: they are the cost of refusing, of failing to connect, and of failing.
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

    Read after authentication, so a guessed credential is never the key: a
    forged token must not spend the allowance of the caller being guessed at.
    With auth off nothing is keyed by a token, and the address scope is doing
    the whole job.
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
    #
    # Both values, not just the name: the running relay is the only thing that
    # knows what it bound with, and a warning that says the port changed sends
    # the operator to the file to read the half of the answer the file has.
    started = app.state.startup_config
    unapplied = [
        f"{name} {getattr(started, name)} -> {getattr(config, name)}"
        for name in ("host", "port", "connect_timeout")
        if getattr(config, name) != getattr(started, name)
    ]
    if unapplied:
        logger.warning(
            f"lmrelay: {', '.join(unapplied)} in {config.config_path} but a reload "
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
    # Scope by scope, so that changing one leaves the other two untouched: they
    # hold separate tables and a caller being limited by one of them has no
    # business getting its allowance back because a different number moved.
    for scope in SCOPES:
        before, after = current.limits[scope], config.limits[scope]
        if (before.rate, before.burst) != (after.rate, after.burst):
            # Rebuilt only when the numbers move: a new limiter starts every
            # caller full, so doing this on an unrelated reload would clear the
            # allowance of whoever was being limited at that moment.
            app.state.limiters[scope] = build_limiter(after.rate, after.burst)
            logger.info(
                f"lmrelay: [limits.{scope}] rate {describe_rate(before)} -> "
                f"{describe_rate(after)}"
            )
        if before.concurrent != after.concurrent:
            # Nothing is rebuilt here, unlike the limiter above: the counter is
            # asked for the limit at every acquire, so replacing app.state.config
            # below is the whole of applying it. A fresh counter would forget the
            # slots held by every answer still streaming, and each of them would
            # release one that had never been taken.
            logger.info(
                f"lmrelay: [limits.{scope}] concurrent {describe_slots(before)} -> "
                f"{describe_slots(after)} (from the next request; answers in flight "
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
    # One limiter per scope, None where that scope's rate is off, so a reload
    # can rebuild one without touching the other two.
    app.state.limiters = build_limiters(config.limits)
    # One counter for all three scopes, whose keys are prefixed apart. Always
    # built, even with every cap off, so that no reload has to replace it and no
    # request has to ask whether it exists. An empty table costs nothing: with
    # `concurrent = 0` no slot is taken at all.
    app.state.inflight = InflightCounter({})
    # Never rebuilt by a reload, unlike the limiters above: these numbers are
    # what a chart is drawn from, and a counter that went back to zero because
    # somebody edited a config file would read as a restart that never happened.
    # A real restart does reset them, which Prometheus knows how to read across.
    app.state.metrics = Metrics()
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
    # Set before anything can log, so that every line written while serving this
    # request carries the same one: the access line at the end, the refusal, and
    # whatever the route said about an upstream in between. That is the whole
    # point of it, since all of them land in one file with everybody else's.
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

    # Answered above the access log and above the counters, and after the
    # credential rather than before it, which is the one way this differs from
    # /healthz. A scrape is not traffic the relay is relaying: counting it would
    # have every scrape move a counter it is itself reporting, and logging it
    # would write a line every fifteen seconds about the monitoring rather than
    # about the relay. An unauthenticated scrape is still refused and still
    # logged, above, because that one is about the relay.
    if request.url.path == METRICS_PATH and request.method in METRICS_METHODS:
        return await call_next(request)

    # The limits are not charged here. They are one decision made once, in the
    # relay route, so that a request refused by one scope has not been charged
    # to another on its way past. Three things follow, all improvements: every
    # refusal names its upstream, nothing that was not forwarded is charged at
    # all, and /healthz is exempt by being a different route rather than by a
    # path check. The cost, stated so it is a choice: a client looping against a
    # wrong-dialect path is no longer rate limited. It costs microseconds per
    # 400 and cannot touch a model, and fail2ban is the answer if it matters.
    start_time = time.monotonic()
    try:
        response: Response = await call_next(request)
    except Exception:
        # A fault in the relay itself, on its way to the 500 handler below. It is
        # recorded here because that handler runs outside this middleware and its
        # answer never returns through the call above, so without this the one
        # error class caused by the relay rather than by an upstream is the one
        # class that moves no counter and leaves no access line: an alert on
        # `status=~"5.."` would catch every 502 and never a 500, and look like it
        # worked. The status is the one handle_exception answers with.
        #
        # Exception and not BaseException: a caller that hangs up before the
        # upstream answers cancels this task, and a cancelled request was
        # answered with nothing at all rather than with a 500.
        record_request(request, client, 500, time.monotonic() - start_time)
        raise
    # For a streamed response the handler returns once the upstream headers
    # arrive, so this is time to first byte, not the duration of the answer.
    record_request(request, client, response.status_code, time.monotonic() - start_time)
    return response


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Answer a framework refusal in the relay's own shape.

    Starlette phrases these itself, as {"detail": "Method Not Allowed"}, and that
    is the one error a caller cannot attribute: it carries neither the
    `lmrelay: ` prefix that the README promises of every error this relay
    generates, nor the `error` key that the other five refusals use. A client
    parsing one shape got another for the cases nobody writes a handler for.

    Reachable in practice for a method outside RELAY_METHODS, TRACE being the
    one a scanner tries, and for anything a future route rejects before the
    relay route sees it. Not reachable for a bad URL: uvicorn answers that in
    plain text before the application is entered at all, which is a layer no
    handler here can reach.
    """
    logger.warning(
        f"{request.client.host if request.client else '-'} "
        f"{request.method} {request.url.path} -> -: "
        f"{exc.status_code} (refused)",
        extra=log_extra(request),
    )
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

    Not exempt from authentication the way /healthz is, and what separates them
    is what each tells a stranger: /healthz says a process is alive, this says
    how the relay is used and how busy it is right now. Prometheus takes a
    `bearer_token` in a scrape job, so the credential costs an operator one line
    of scrape config.

    Its own route rather than a branch in the relay, which is also what keeps it
    out of the limits: a scrape every fifteen seconds must not spend the
    allowance of whichever address the monitoring happens to share.
    """
    return Response(render(request.app.state.metrics, __version__), media_type=CONTENT_TYPE)


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

    wrong_dialect = check_dialect(upstream, forward_path)
    if wrong_dialect:
        # 400 rather than 404: a 404 would be indistinguishable from the
        # upstream's own, which is exactly the confusion being prevented.
        # Refused before admission, so it is charged to nothing at all.
        return JSONResponse({"error": wrong_dialect}, status_code=400)

    http = request.app.state.http
    upstream_request = build_upstream_request(http, request, upstream, forward_path)

    # Decided here, with nothing between it and the send below that can raise:
    # after the refusals that cost no upstream call, and after the request has
    # been built, so that a slot is held for exactly as long as the relay is
    # occupying the upstream with it.
    #
    # `release` covers every slot this admission took, in every scope, and is
    # idempotent because the ways out below are meant not to overlap and a
    # leaked slot is unrecoverable: it would lock that scope out for the life of
    # the process. A refusal returns a release that does nothing, so no path has
    # to ask whether it holds anything.
    refusal, release = admit(
        request.app.state.limiters,
        config.limits,
        request.app.state.inflight,
        caller_scopes(request, config),
        time.monotonic(),
    )
    if refusal is not None:
        # 429 for all six, rather than 503: nothing is wrong with the relay or
        # with the upstream, and this same request from another caller would be
        # served at that instant. A 503 would say the service is unavailable,
        # which is both untrue and the status many clients retry hardest
        # against.
        #
        # Named rather than logged here: the middleware writes one access line
        # per request, and a second line from this route would say the same
        # thing again. It logs this one as a refusal, and names the scope.
        request.state.refused = f"{refusal.kind}, {refusal.scope}"
        count_refusal(request.app.state.metrics, refusal.scope, refusal.kind)
        headers = None
        if refusal.kind == "rate":
            # Whole seconds, rounded up: the header takes no fractions, and
            # rounding down would invite a retry that is refused again. A slot
            # refusal carries none: a slot frees when a model finishes answering
            # somebody else, and with no read timeout the relay cannot know when
            # that is, so a guessed number would be a lie.
            headers = {"Retry-After": str(max(1, ceil(refusal.wait)))}
        return JSONResponse(
            {"error": refusal_message(refusal, config.limits[refusal.scope])},
            status_code=429,
            headers=headers,
        )

    # From here to the release is what "in flight" means, and it is wrapped
    # around the release rather than counted beside it so the gauge cannot
    # outlive the slots: every way out below already gives the slots back.
    # After the refusal above, so that a refused request is never counted as
    # being carried by a relay that never carried it.
    release = track_in_flight(request.app.state.metrics, release)

    try:
        # stream=True returns as soon as the headers arrive and leaves the body
        # unread; the context-manager form would close the connection before the
        # caller had read a byte. BackgroundTask below releases it afterwards.
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
        # BaseException, not Exception: a caller that hangs up while the model
        # is still being reached cancels this task, and CancelledError is not an
        # Exception. There is no body generator yet to release the slot, so it
        # has to happen here or not at all.
        release()
        raise

    # The upstream has answered, so the elapsed time the access log is about to
    # measure is a real time to first byte and belongs in the histogram. Set
    # here rather than inferred from the status, because an upstream is entitled
    # to answer 400 or 502 itself and those answers were still relayed.
    request.state.forwarded = True

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
