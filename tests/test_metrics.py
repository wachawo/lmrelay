#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The counters, the text they are scraped as, and who is allowed to scrape it."""

import logging
import re

import httpx
import pytest

# Local imports
from lmrelay.metrics import (
    CONTENT_TYPE,
    TTFB_BUCKETS,
    Metrics,
    count_auth_failure,
    count_refusal,
    count_upstream_error,
    observe_request,
    render,
    track_in_flight,
)
from tests.conftest import CONFIG_TEMPLATE, TOKEN, write_config
from tests.test_relay import answer_in_flight, config_limits, relay_with

# The three sample names a histogram family writes under one family header, so a
# sample can be traced back to the HELP and TYPE that declared it.
HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")

TYPES = {"counter", "gauge", "histogram"}

# The euro sign, which latin-1 has no room for, spelled as an escape so this file
# stays ascii. Sent as raw bytes it is a response header httpx hands over and
# starlette cannot encode, which is how a fault in the relay is provoked below.
NOT_IN_LATIN_1 = "\u20ac"

SAMPLE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?P<labels>\{.*\})? (?P<value>\S+)$")
LABEL  = re.compile(r'(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:[^"\\]|\\.)*)"')

UNESCAPED = {"n": "\n", '"': '"', "\\": "\\"}


def unescape(value: str) -> str:
    """Read a label value back, in one pass.

    One pass matters: undoing the three replacements one after another turns a
    literal backslash-n that the relay escaped as four characters into a
    newline, and the test would then agree with a renderer that had corrupted it.
    """
    return re.sub(r"\\(.)", lambda found: UNESCAPED[found.group(1)], value)


def parse_labels(text: str) -> tuple[tuple[str, str], ...]:
    """One sample's labels, sorted, so a lookup does not depend on the order written."""
    if not text:
        return ()
    pairs = [(found["name"], unescape(found["value"])) for found in LABEL.finditer(text)]
    return tuple(sorted(pairs))


def parse_exposition(body: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Read a scrape the way Prometheus does, refusing everything it would refuse.

    This is the test for the format as much as it is a reader for the others.
    Prometheus does not report a malformed body: it drops the whole scrape and
    the relay goes quiet on a dashboard for a reason nothing in lmrelay.log
    mentions. So every rule the format has is checked here rather than trusted,
    and a mistake in the renderer fails a test instead of being invisible.
    """
    assert body.endswith("\n"), "the last sample has no newline after it"
    declared: dict[str, str] = {}
    described: set[str] = set()
    samples: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    order: list[str] = []

    for line in body.splitlines():
        assert line, "a blank line is not part of the format"
        if line.startswith("# HELP "):
            name, unused_text = line[len("# HELP "):].split(" ", 1)
            assert name not in described, f"{name} is described twice"
            described.add(name)
            continue
        if line.startswith("# TYPE "):
            name, kind = line[len("# TYPE "):].split(" ", 1)
            assert kind in TYPES, f"{name} is typed {kind}"
            assert name not in declared, f"{name} is typed twice"
            assert name in described, f"{name} is typed before it is described"
            declared[name] = kind
            order.append(name)
            continue
        assert not line.startswith("#"), f"unreadable comment: {line}"

        found = SAMPLE.match(line)
        assert found, f"unreadable sample: {line}"
        name = found["name"]
        family = family_of(name, declared)
        assert family == order[-1], f"{name} arrives after {order[-1]} has begun"
        key = (name, parse_labels(found["labels"] or ""))
        assert key not in samples, f"{line} is a series written twice"
        samples[key] = float(found["value"])

    for name, kind in declared.items():
        if kind == "counter":
            assert name.endswith("_total"), f"the counter {name} does not end in _total"
    return samples


def family_of(name: str, declared: dict[str, str]) -> str:
    """Which family a sample belongs to, refusing a sample no family declared."""
    if name in declared:
        return name
    for suffix in HISTOGRAM_SUFFIXES:
        stem = name[: -len(suffix)]
        if name.endswith(suffix) and declared.get(stem) == "histogram":
            return stem
    raise AssertionError(f"{name} has no HELP and TYPE above it")


def value_of(samples, name: str, **labels) -> float:
    """One series, or 0.0 where the relay has never had occasion to create it."""
    return samples.get((name, tuple(sorted(labels.items()))), 0.0)


def relay_lines(caplog) -> list[str]:
    """Only what the relay itself said. Whatever http client happens to be
    installed beside it announces its own requests, and a test about lmrelay's
    log must not pass or fail on that."""
    return [record.getMessage() for record in caplog.records if record.name == "lmrelay.app"]


def scrape(client) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """What a Prometheus scrape of this relay would read."""
    response = client.get("/metrics")
    assert response.status_code == 200, response.text
    return parse_exposition(response.text)


def counted(metrics: Metrics) -> Metrics:
    """A metrics table with one of everything in it, for the rendering tests."""
    observe_request(metrics, "ollama", 200, 0.42)
    observe_request(metrics, "ollama", 429, None)
    count_refusal(metrics, "per_token", "rate")
    count_auth_failure(metrics)
    count_upstream_error(metrics, "ollama", "ConnectError")
    return metrics


@pytest.fixture
def limited(tmp_path, monkeypatch, recorder):
    """A relay that admits three from an address and then refuses."""
    yield from relay_with(
        tmp_path, monkeypatch, recorder, config_limits(per_address='concurrent = 3\nrate = "3/1s"')
    )


class TestTheExpositionFormat:
    """What a scrape has to look like for Prometheus to read it at all."""

    def test_it_parses(self):
        assert parse_exposition(render(counted(Metrics()), "0.0.4"))

    def test_a_relay_that_has_served_nothing_parses_too(self):
        """The first scrape after a restart, and the one an operator makes while
        setting the job up. Families with no samples still carry their HELP and
        TYPE, which is valid and is what makes that scrape describe itself."""
        body = render(Metrics(), "0.0.4")
        assert parse_exposition(body) == {
            ("lmrelay_build_info", (("version", "0.0.4"),)): 1.0,
            ("lmrelay_requests_in_flight", ()): 0.0,
            ("lmrelay_auth_failures_total", ()): 0.0,
        }
        assert "# TYPE lmrelay_requests_total counter" in body

    def test_the_content_type_names_the_format_version(self):
        """Not lmrelay's version, which is the same number today by coincidence:
        it is the exposition format's, and Prometheus parses by it."""
        assert CONTENT_TYPE == "text/plain; version=0.0.4; charset=utf-8"

    def test_the_version_rides_on_a_build_info_gauge(self):
        samples = parse_exposition(render(Metrics(), "9.9.9"))
        assert value_of(samples, "lmrelay_build_info", version="9.9.9") == 1.0

    def test_a_label_value_that_would_break_the_format_is_escaped(self):
        """An upstream name is whatever an operator put in a TOML table key, and
        a quoted key can hold a quote. One unescaped quote here costs the whole
        scrape, not the line."""
        metrics = Metrics()
        observe_request(metrics, 'weird"name\\here', 200, None)
        samples = parse_exposition(render(metrics, "0.0.4"))
        assert value_of(
            samples, "lmrelay_requests_total", upstream='weird"name\\here', status="200"
        ) == 1.0

    def test_and_a_newline_in_one_cannot_forge_a_sample(self):
        metrics = Metrics()
        observe_request(metrics, "one\nlmrelay_requests_total 99", 200, None)
        body = render(metrics, "0.0.4")
        assert "\\n" in body
        parse_exposition(body)


class TestTheHistogram:
    """The one family the format has real rules about."""

    def test_the_buckets_are_cumulative(self):
        metrics = Metrics()
        for seconds in (0.01, 0.3, 4.0, 90.0):
            observe_request(metrics, "ollama", 200, seconds)
        samples = parse_exposition(render(metrics, "0.0.4"))
        counts = [
            value_of(
                samples, "lmrelay_request_ttfb_seconds_bucket",
                upstream="ollama", le=repr(bound),
            )
            for bound in TTFB_BUCKETS
        ]
        assert counts == sorted(counts)
        assert counts[0] == 1
        assert counts[-1] == 4

    def test_the_last_bucket_is_inf_and_holds_everything(self):
        metrics = Metrics()
        for seconds in (0.01, 900.0):
            observe_request(metrics, "ollama", 200, seconds)
        samples = parse_exposition(render(metrics, "0.0.4"))
        everything = value_of(
            samples, "lmrelay_request_ttfb_seconds_bucket", upstream="ollama", le="+Inf"
        )
        assert everything == 2
        assert value_of(
            samples, "lmrelay_request_ttfb_seconds_count", upstream="ollama"
        ) == everything

    def test_the_sum_is_the_seconds_that_went_in(self):
        metrics = Metrics()
        observe_request(metrics, "ollama", 200, 0.5)
        observe_request(metrics, "ollama", 200, 2.0)
        samples = parse_exposition(render(metrics, "0.0.4"))
        assert value_of(samples, "lmrelay_request_ttfb_seconds_sum", upstream="ollama") == 2.5

    def test_an_observation_on_a_bound_belongs_to_that_bound(self):
        """`le` is less than or equal, so bisect_left is the rule and
        bisect_right would file every exact bound one bucket too high."""
        metrics = Metrics()
        observe_request(metrics, "ollama", 200, TTFB_BUCKETS[0])
        samples = parse_exposition(render(metrics, "0.0.4"))
        assert value_of(
            samples, "lmrelay_request_ttfb_seconds_bucket",
            upstream="ollama", le=repr(TTFB_BUCKETS[0]),
        ) == 1.0

    def test_the_buckets_reach_far_enough_for_a_model_that_has_to_load(self):
        """The reason these are not the Prometheus defaults. Those top out at ten
        seconds, and a cold local model is minutes: every one of them would land
        in +Inf, where a histogram has recorded that something was slow and
        nothing else."""
        assert TTFB_BUCKETS[-1] >= 300
        metrics = Metrics()
        observe_request(metrics, "ollama", 200, 100.0)
        samples = parse_exposition(render(metrics, "0.0.4"))
        assert value_of(
            samples, "lmrelay_request_ttfb_seconds_bucket", upstream="ollama", le="10.0"
        ) == 0.0
        assert value_of(
            samples, "lmrelay_request_ttfb_seconds_bucket", upstream="ollama", le="120.0"
        ) == 1.0

    def test_and_still_resolve_a_hosted_provider(self):
        """The other end of the same choice: an answer in a tenth of a second is
        not all one bucket with an answer in five."""
        assert TTFB_BUCKETS[0] <= 0.05
        assert len([bound for bound in TTFB_BUCKETS if bound <= 1.0]) >= 4

    def test_two_upstreams_keep_two_histograms(self):
        metrics = Metrics()
        observe_request(metrics, "ollama", 200, 30.0)
        observe_request(metrics, "openai", 200, 0.2)
        samples = parse_exposition(render(metrics, "0.0.4"))
        assert value_of(samples, "lmrelay_request_ttfb_seconds_count", upstream="ollama") == 1
        assert value_of(samples, "lmrelay_request_ttfb_seconds_count", upstream="openai") == 1


class TestCountersOnlyGoUp:
    """What a counter promises within one process."""

    def test_no_series_ever_reads_lower_than_it_did(self, authed, recorder):
        """A restart resets these, and Prometheus knows how to read across that.
        A counter going backwards inside one process is a different thing, and it
        would make every rate() over the gap read as an enormous spike."""
        before = scrape(authed)
        authed.post("/api/chat", json={})
        recorder.raises = httpx.ConnectError("no route")
        authed.post("/api/chat", json={})
        recorder.raises = None
        after = scrape(authed)
        for key, was in before.items():
            if key[0].endswith("_total"):
                assert after.get(key, -1.0) >= was, f"{key} went backwards"

    def test_a_reload_does_not_reset_them(self, authed, tmp_path):
        """The counters are not rebuilt with the config: a chart must not show a
        restart that did not happen because somebody edited a file."""
        from lmrelay.app import app, reload_config

        authed.post("/api/chat", json={})
        write_config(tmp_path, CONFIG_TEMPLATE.format(token=TOKEN))
        reload_config(app)
        samples = scrape(authed)
        assert value_of(
            samples, "lmrelay_requests_total", upstream="ollama", status="200"
        ) == 1.0


class TestWhatTheRelayCounts:
    """The counters end to end, read the way a scrape reads them."""

    def test_a_relayed_request_by_upstream_and_status(self, authed, recorder):
        recorder.status = 201
        authed.post("/anthropic/v1/messages", json={})
        samples = scrape(authed)
        assert value_of(
            samples, "lmrelay_requests_total", upstream="anthropic", status="201"
        ) == 1.0

    def test_and_its_time_to_first_byte(self, authed):
        authed.post("/api/chat", json={})
        samples = scrape(authed)
        assert value_of(samples, "lmrelay_request_ttfb_seconds_count", upstream="ollama") == 1.0

    def test_a_refusal_by_scope_and_by_measure(self, limited):
        for unused_attempt in range(4):
            limited.post("/api/chat", json={})
        samples = scrape(limited)
        assert value_of(samples, "lmrelay_refusals_total", scope="per_address", kind="rate") == 1.0

    def test_a_refused_request_is_counted_but_not_timed(self, limited):
        """It never reached an upstream, so the only thing its elapsed time
        measures is what refusing costs. A client retrying into a limit would
        otherwise drag the whole distribution down to that."""
        for unused_attempt in range(4):
            limited.post("/api/chat", json={})
        samples = scrape(limited)
        assert value_of(
            samples, "lmrelay_requests_total", upstream="ollama", status="429"
        ) == 1.0
        assert value_of(samples, "lmrelay_request_ttfb_seconds_count", upstream="ollama") == 3.0

    def test_a_dialect_refusal_is_counted_but_not_timed_either(self, authed):
        authed.post("/anthropic/api/chat", json={})
        samples = scrape(authed)
        assert value_of(
            samples, "lmrelay_requests_total", upstream="anthropic", status="400"
        ) == 1.0
        assert value_of(samples, "lmrelay_request_ttfb_seconds_count", upstream="anthropic") == 0.0

    def test_an_unreachable_upstream_by_type(self, authed, recorder):
        recorder.raises = httpx.ConnectError("no route to host")
        authed.post("/api/chat", json={})
        samples = scrape(authed)
        assert value_of(
            samples, "lmrelay_upstream_errors_total", upstream="ollama", type="ConnectError"
        ) == 1.0

    def test_and_it_is_not_timed_as_an_answer(self, authed, recorder):
        """The elapsed time of a failed connection is a connect timeout, not a
        model's first token."""
        recorder.raises = httpx.ConnectTimeout("slow")
        authed.post("/api/chat", json={})
        samples = scrape(authed)
        assert value_of(samples, "lmrelay_request_ttfb_seconds_count", upstream="ollama") == 0.0

    def test_a_missing_credential(self, relay):
        relay.post("/api/chat", json={})
        relay.headers.update({"Authorization": f"Bearer {TOKEN}"})
        assert value_of(scrape(relay), "lmrelay_auth_failures_total") == 1.0

    def test_which_is_not_also_counted_as_a_request(self, relay):
        """It never got as far as choosing an upstream, so there is no upstream
        to count it under, and a series labelled `-` would be a second meaning
        for a label that otherwise always names one."""
        relay.post("/api/chat", json={})
        relay.headers.update({"Authorization": f"Bearer {TOKEN}"})
        samples = scrape(relay)
        assert not [name for name, labels in samples if name == "lmrelay_requests_total"]

    def test_an_upstream_status_the_relay_did_not_choose_is_still_its_own(self, authed, recorder):
        """A 502 from the provider is a relayed answer, not a relay failure, and
        it is timed like any other: the bytes did arrive."""
        recorder.status = 502
        authed.post("/api/chat", json={})
        samples = scrape(authed)
        assert value_of(samples, "lmrelay_requests_total", upstream="ollama", status="502") == 1.0
        assert value_of(samples, "lmrelay_request_ttfb_seconds_count", upstream="ollama") == 1.0
        assert not [name for name, labels in samples if name == "lmrelay_upstream_errors_total"]


class TestAFaultInTheRelayItself:
    """The 500 lmrelay answers with when its own code raises.

    It is counted here and not in `lmrelay_upstream_errors_total`, which is about
    failing to reach a provider. And it has to be counted somewhere: the handler
    that turns an exception into that 500 is lifted outside the user middleware
    by starlette, so its answer never returns through the middleware, and an
    alert on `status=~"5.."` that catches every 502 and no 500 is an alert that
    is blind to exactly the errors this relay is responsible for.
    """

    def test_it_is_counted_as_a_500(self, authed, recorder):
        recorder.raises = ValueError("something in the relay gave way")
        with pytest.raises(ValueError):
            authed.post("/api/chat", json={})
        samples = scrape(authed)
        assert value_of(samples, "lmrelay_requests_total", upstream="ollama", status="500") == 1.0

    def test_and_writes_the_access_line_every_other_answer_writes(self, authed, recorder, caplog):
        """The traceback alone says what broke and not who asked for it. The
        access line is the half carrying the caller, the path and the upstream.

        It comes first here, which is the other way round from a 502: that one is
        the route's own warning followed by the access line, and this one is the
        access line followed by a traceback written above the middleware, on the
        way out. Either order reads, because the two lines share a request id.
        """
        recorder.raises = ValueError("something in the relay gave way")
        with caplog.at_level(logging.INFO), pytest.raises(ValueError):
            authed.post("/api/chat", json={})
        access_line, traceback_line = relay_lines(caplog)
        assert "testclient POST /api/chat -> ollama: 500" in access_line
        assert traceback_line.startswith("ValueError: something in the relay gave way")

    def test_but_is_not_timed_when_no_upstream_had_answered(self, authed, recorder):
        """Same rule as a refusal and a failed connection: that elapsed time is
        the cost of failing, not a model's first token."""
        recorder.raises = ValueError("something in the relay gave way")
        with pytest.raises(ValueError):
            authed.post("/api/chat", json={})
        samples = scrape(authed)
        assert value_of(samples, "lmrelay_request_ttfb_seconds_count", upstream="ollama") == 0.0

    def test_and_is_timed_when_one_had(self, authed, recorder):
        """The measured case the relay route already guards: starlette encodes
        header values as latin-1 and httpx decodes them as utf-8, so one header
        a provider is entitled to send raises while the response is being built.
        The upstream did answer, so the elapsed time is a real first byte."""
        recorder.headers = [
            (b"content-type", b"application/json"), (b"x-note", NOT_IN_LATIN_1.encode()),
        ]
        with pytest.raises(UnicodeEncodeError):
            authed.post("/api/chat", json={})
        samples = scrape(authed)
        assert value_of(samples, "lmrelay_requests_total", upstream="ollama", status="500") == 1.0
        assert value_of(samples, "lmrelay_request_ttfb_seconds_count", upstream="ollama") == 1.0


class TestRequestsInFlight:
    """The one gauge, which has to come back down."""

    def test_nothing_in_flight_at_rest(self, authed):
        assert value_of(scrape(authed), "lmrelay_requests_in_flight") == 0.0

    def test_an_answer_being_streamed_is_in_flight(self, authed, recorder):
        with answer_in_flight(authed, recorder):
            assert value_of(scrape(authed), "lmrelay_requests_in_flight") == 1.0

    def test_and_is_not_once_it_has_ended(self, authed, recorder):
        with answer_in_flight(authed, recorder) as held:
            recorder.gate.set()
            held.answer.result()
        assert value_of(scrape(authed), "lmrelay_requests_in_flight") == 0.0

    def test_a_request_that_never_reached_an_upstream_was_never_in_flight(self, authed, recorder):
        recorder.raises = httpx.ConnectError("no route")
        authed.post("/api/chat", json={})
        assert value_of(scrape(authed), "lmrelay_requests_in_flight") == 0.0

    def test_a_refused_one_is_not_counted_as_carried(self, limited):
        for unused_attempt in range(4):
            limited.post("/api/chat", json={})
        assert value_of(scrape(limited), "lmrelay_requests_in_flight") == 0.0

    def test_the_gauge_is_given_back_only_once(self):
        """Every path out of a request gives its slot back, and they are meant
        not to overlap; a gauge decremented twice would count a live request as
        finished and, unlike a counter, would stay wrong for good."""
        metrics = Metrics()
        finish = track_in_flight(metrics, lambda: None)
        finish()
        finish()
        assert metrics.in_flight == 0

    def test_and_it_still_releases_the_slots_underneath_it(self):
        released = []
        metrics = Metrics()
        track_in_flight(metrics, lambda: released.append(1))()
        assert released == [1]


class TestWhoMayScrape:
    """The one decision that separates this endpoint from /healthz."""

    def test_a_scrape_with_no_credential_is_refused(self, relay):
        assert relay.get("/metrics").status_code == 401

    def test_because_it_says_how_the_relay_is_used(self, authed):
        """/healthz tells a stranger that a process is alive. This tells them how
        busy it is and what it is in front of, so it goes behind the credential
        everything else is behind. Prometheus takes a bearer_token in a job."""
        assert authed.get("/metrics").status_code == 200

    def test_the_body_is_served_as_the_text_format(self, authed):
        assert authed.get("/metrics").headers["content-type"] == CONTENT_TYPE

    def test_a_scrape_touches_no_upstream(self, authed, recorder):
        authed.get("/metrics")
        assert recorder.requests == []

    def test_a_scrape_is_not_counted_as_traffic(self, authed):
        """Every scrape would otherwise move a counter it is itself reporting,
        and the series would grow by one on each poll with nothing behind it."""
        scrape(authed)
        samples = scrape(authed)
        assert not [name for name, labels in samples if name == "lmrelay_requests_total"]

    def test_and_does_not_spend_a_callers_allowance(self, limited):
        """A poll every fifteen seconds must not use up the rate limit of
        whichever address the monitoring happens to share."""
        for unused_attempt in range(3):
            limited.get("/metrics")
        assert limited.post("/api/chat", json={}).status_code == 200

    def test_a_scrape_writes_no_access_line(self, authed, caplog):
        """A line every fifteen seconds about the monitoring, forever, in the
        file an operator greps for what the relay did."""
        with caplog.at_level(logging.INFO):
            authed.get("/metrics")
        assert relay_lines(caplog) == []

    def test_but_a_scrape_refused_at_the_door_does(self, relay, caplog):
        """That one is about the relay: it is either a misconfigured job or
        somebody who is not the monitoring."""
        with caplog.at_level(logging.WARNING):
            relay.get("/metrics")
        assert relay_lines(caplog) == ["testclient GET /metrics -> -: 401 (auth)"]

    def test_any_other_method_on_the_path_is_relayed(self, authed, recorder):
        """The route answers GET, and a POST to /metrics is a caller's request
        for an upstream that may well have a /metrics of its own."""
        assert authed.post("/metrics", json={}).status_code == 200
        assert str(recorder.last.url) == "http://ollama.invalid:11434/metrics"


def main():
    pass


if __name__ == "__main__":
    main()
