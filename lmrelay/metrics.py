#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aggregate counters and the Prometheus text they are scraped as. In memory, reset by a restart."""

from bisect import bisect_left
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

# What a scrape has to be served as. The 0.0.4 here is the version of the text
# exposition format and has nothing to do with lmrelay's own, which is the same
# number today by coincidence: Prometheus reads this header to decide how to
# parse the body, so it moves when the format does and never when the relay does.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# The one family whose samples are spread over three names, so they are spelled
# once here rather than four times below.
TTFB_NAME = "lmrelay_request_ttfb_seconds"

# Seconds to the upstream's first byte. Chosen for what this relay sits in front
# of rather than taken from the Prometheus default, which runs .005 to 10: a
# local model that is not resident has to be read off disk before it can produce
# a token, which is tens of seconds for a large one and minutes on a cold cache,
# so under the default buckets almost every local answer lands in +Inf, where a
# histogram has recorded that something took longer than ten seconds and nothing
# else at all. The low end still has to resolve a hosted provider, which answers
# in well under a second, so the range spans four orders of magnitude and is
# deliberately coarse in the middle: the questions here are "was that a hosted
# answer or a local one" and "did a model have to load", not "3.4s or 3.6s".
TTFB_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)

# One line of the exposition: the sample's own name, its labels in order, and its
# value. The name travels with the sample rather than belonging to the family
# because a histogram writes three different names under one family header.
Sample = tuple[str, Sequence[tuple[str, str]], float]


@dataclass
class Histogram:
    """One upstream's times to first byte: a slot per bucket, plus one for the rest.

    Counts are held per bucket and added up at render time. The format wants
    them cumulative, but keeping them cumulative in memory means every
    observation writes to every bucket above it, which is the same total work
    and one more thing to get wrong. The number of observations is not stored
    either: it is the last cumulative bucket, so it cannot drift from them.
    """

    counts: list[int]
    total: float = 0.0


@dataclass
class Metrics:
    """Everything the relay counts. Aggregate, and with no caller named anywhere.

    No key here identifies a caller: not a token, not an address. Two reasons,
    both load-bearing. It keeps "No token accounting, usage database or budgets"
    true, and a label per credential is unbounded cardinality: one time series
    per token that ever presented itself, kept by whatever scrapes this, forever.

    The counters live here and nowhere else, so a restart resets them. Prometheus
    understands a counter reset and reads across one; the alternative, a number
    that survives a restart, is a file on disk written on the hot path, which is
    the usage database this project says it is not.

    Not shared between processes, exactly as the limiter tables are not: under
    several uvicorn workers a scrape reaches one worker and reports that worker.
    """

    requests: dict[tuple[str, str], int] = field(default_factory=dict)
    ttfb: dict[str, Histogram] = field(default_factory=dict)
    refusals: dict[tuple[str, str], int] = field(default_factory=dict)
    upstream_errors: dict[tuple[str, str], int] = field(default_factory=dict)
    auth_failures: int = 0
    in_flight: int = 0


def bump(table: dict[tuple[str, str], int], key: tuple[str, str]) -> None:
    """Add one to a labelled counter, creating the series at its first sample.

    Series are created on use rather than declared up front, which is what keeps
    the label sets honest: a status this relay has never returned has no line,
    instead of a zero that reads as "measured, and it did not happen".
    """
    table[key] = table.get(key, 0) + 1


def observe_request(metrics: Metrics, upstream: str, status: int, ttfb: float | None) -> None:
    """One answered request: counted by upstream and status, and timed if an upstream answered.

    `ttfb` is None for a request the relay answered itself, and those are counted
    but not timed. A dialect refusal costs microseconds and a refused request
    costs the price of refusing it; a client looping on either would pull the
    distribution down until the number an operator reads is this relay's own
    overhead rather than a model's first token.
    """
    bump(metrics.requests, (upstream, str(status)))
    if ttfb is not None:
        observe_ttfb(metrics, upstream, ttfb)


def observe_ttfb(metrics: Metrics, upstream: str, seconds: float) -> None:
    """Record one time to first byte in its upstream's histogram."""
    histogram = metrics.ttfb.get(upstream)
    if histogram is None:
        histogram = Histogram(counts=[0] * (len(TTFB_BUCKETS) + 1))
        metrics.ttfb[upstream] = histogram
    histogram.counts[bucket_index(seconds)] += 1
    histogram.total += seconds


def bucket_index(seconds: float) -> int:
    """Which slot one observation falls in; the last one is everything above the top bound.

    bisect_left, not bisect_right: a bucket's label is `le`, less than or equal,
    so an observation exactly on a bound belongs to that bound's bucket and not
    to the next one up.
    """
    return bisect_left(TTFB_BUCKETS, seconds)


def count_refusal(metrics: Metrics, scope: str, kind: str) -> None:
    """One request turned away by a limit: which scope refused, and on which measure."""
    bump(metrics.refusals, (scope, kind))


def count_auth_failure(metrics: Metrics) -> None:
    """One request refused for a missing or invalid credential.

    Unlabelled, and not also counted as a request: it never reached the point
    where an upstream is chosen, so it has no upstream to be counted under, and
    a series labelled `-` would be a second meaning for that label.
    """
    metrics.auth_failures += 1


def count_upstream_error(metrics: Metrics, upstream: str, error: str) -> None:
    """One failure reaching an upstream, by upstream and exception type.

    Reaching, specifically. A stream that breaks after the headers have arrived
    is not counted here: the relay has already answered the caller with the
    upstream's own status, and the failure belongs to that answer rather than to
    getting there. It is in the log, with its traceback.
    """
    bump(metrics.upstream_errors, (upstream, error))


def track_in_flight(metrics: Metrics, release: Callable[[], None]) -> Callable[[], None]:
    """Count one request as in flight, wrapping the release that ends it.

    Wrapped rather than counted beside it so the gauge cannot drift from the
    slots. They have one lifetime and several ways home, and every path that
    gives a slot back now ends the request's time in flight in the same call
    instead of each path having to remember two.

    The result is idempotent for the same reason `release_all` is: those paths
    are meant not to overlap, but a gauge decremented twice would count a live
    request as finished, and unlike a counter a gauge that has gone wrong stays
    wrong for the life of the process.

    The wrapped release runs on every call and the gauge only on the first, so
    the two safety nets do not depend on each other: whatever a later edit does
    to the count here, the slots underneath still go home.
    """
    metrics.in_flight += 1
    finished = False

    def finish() -> None:
        nonlocal finished
        release()
        if finished:
            return
        finished = True
        metrics.in_flight -= 1

    return finish


def escape_label(value: str) -> str:
    """Escape a label value: backslash first, then quote and newline.

    Backslash first because the other two replacements produce backslashes, and
    the other order would escape what this had just added. It matters because
    label values are not all ours: an upstream name is whatever an operator put
    in a TOML table key, which may be a quoted string holding a quote, and one
    unescaped quote makes Prometheus drop the entire scrape rather than the line.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_labels(labels: Sequence[tuple[str, str]]) -> str:
    """The label set as the format writes it, or nothing at all when a sample has none."""
    if not labels:
        return ""
    inner = ",".join(f'{name}="{escape_label(value)}"' for name, value in labels)
    return "{" + inner + "}"


def render_value(value: float) -> str:
    """A sample value, in the shortest spelling that reads back as the same number.

    repr rather than a fixed format: it round-trips, so a sum of seconds is
    neither rounded to six figures the way %g would nor padded with digits it
    never had. An int reprs as itself, which is what a counter should look like.
    """
    return repr(value)


def render_family(name: str, kind: str, description: str, samples: Iterable[Sample]) -> list[str]:
    """One metric family: its two metadata lines, then all of its samples together.

    Emitted even when it has no samples. A HELP and a TYPE with nothing under
    them is valid, and it lets a relay that has served nothing yet still
    describe what it will measure rather than answer with a nearly empty page.
    """
    lines = [f"# HELP {name} {description}", f"# TYPE {name} {kind}"]
    lines += [
        f"{sample_name}{render_labels(labels)} {render_value(value)}"
        for sample_name, labels, value in samples
    ]
    return lines


def ttfb_samples(histograms: dict[str, Histogram]) -> list[Sample]:
    """Every histogram's buckets, sum and count, made cumulative as the format requires.

    Cumulative means each bucket holds everything at or below its bound, and the
    last one, +Inf, holds every observation there has been, which is also what
    `_count` reports. A Prometheus given non-cumulative buckets does not
    complain: it computes quantiles from them and answers nonsense.
    """
    samples: list[Sample] = []
    for upstream, histogram in sorted(histograms.items()):
        whose = ("upstream", upstream)
        running = 0
        for index, bound in enumerate(TTFB_BUCKETS):
            running += histogram.counts[index]
            samples.append((f"{TTFB_NAME}_bucket", (whose, ("le", render_value(bound))), running))
        running += histogram.counts[len(TTFB_BUCKETS)]
        samples.append((f"{TTFB_NAME}_bucket", (whose, ("le", "+Inf")), running))
        samples.append((f"{TTFB_NAME}_sum", (whose,), histogram.total))
        samples.append((f"{TTFB_NAME}_count", (whose,), running))
    return samples


def render(metrics: Metrics, version: str) -> str:
    """The whole exposition, one family after another, ending in a newline.

    Sorted within each family so that two scrapes of an unchanged relay are the
    same bytes: nothing requires it, but a diff between two scrapes is how an
    operator reads this by hand, and dictionary order would make every line look
    changed. A malformed body is dropped whole and silently by Prometheus, which
    is why the families here are built rather than formatted at their call sites.
    """
    lines: list[str] = []
    lines += render_family(
        "lmrelay_build_info", "gauge",
        "The version of the relay these counters came from, as a label on a constant 1.",
        [("lmrelay_build_info", (("version", version),), 1)],
    )
    lines += render_family(
        "lmrelay_requests_total", "counter",
        "Requests the relay answered, by the upstream chosen and the status returned.",
        [
            ("lmrelay_requests_total", (("upstream", upstream), ("status", status)), count)
            for (upstream, status), count in sorted(metrics.requests.items())
        ],
    )
    lines += render_family(
        TTFB_NAME, "histogram",
        "Seconds from a request arriving to the upstream's first byte, by upstream.",
        ttfb_samples(metrics.ttfb),
    )
    lines += render_family(
        "lmrelay_requests_in_flight", "gauge",
        "Answers being relayed right now, counted until the last byte of each reaches the caller.",
        [("lmrelay_requests_in_flight", (), metrics.in_flight)],
    )
    lines += render_family(
        "lmrelay_refusals_total", "counter",
        "Requests refused by a limit, by the scope that refused and which of its measures.",
        [
            ("lmrelay_refusals_total", (("scope", scope), ("kind", kind)), count)
            for (scope, kind), count in sorted(metrics.refusals.items())
        ],
    )
    lines += render_family(
        "lmrelay_auth_failures_total", "counter",
        "Requests refused for a missing or invalid credential.",
        [("lmrelay_auth_failures_total", (), metrics.auth_failures)],
    )
    lines += render_family(
        "lmrelay_upstream_errors_total", "counter",
        "Failures reaching an upstream, by upstream and exception type.",
        [
            ("lmrelay_upstream_errors_total", (("upstream", upstream), ("type", error)), count)
            for (upstream, error), count in sorted(metrics.upstream_errors.items())
        ],
    )
    return "\n".join(lines) + "\n"


def main():
    pass


if __name__ == "__main__":
    main()
