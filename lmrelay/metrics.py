#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aggregate counters and their Prometheus text exposition. In memory, reset by a restart."""

from bisect import bisect_left
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

# 0.0.4 is the exposition format version, unrelated to lmrelay's own version.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

TTFB_NAME = "lmrelay_request_ttfb_seconds"

# Seconds to the upstream's first byte. Wider than the Prometheus default (.005
# to 10): a local model loading from disk takes tens of seconds to minutes, and
# would all land in +Inf. Coarse in the middle on purpose: the questions are
# "hosted or local" and "did a model have to load".
TTFB_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)

# (sample name, labels, value). The name is per sample because a histogram
# writes three names under one family header.
Sample = tuple[str, Sequence[tuple[str, str]], float]


@dataclass
class Histogram:
    """One upstream's times to first byte: a slot per bucket, plus one for the rest.

    Counts are per bucket and made cumulative at render time; the observation
    count is the last cumulative bucket, so it is not stored separately.
    """

    counts: list[int]
    total: float = 0.0


@dataclass
class Metrics:
    """Everything the relay counts, aggregate only.

    No label identifies a caller: a label per token is unbounded cardinality and
    would amount to usage accounting. Not shared between processes: under several
    uvicorn workers a scrape reports only the worker it reached.
    """

    requests: dict[tuple[str, str], int] = field(default_factory=dict)
    ttfb: dict[str, Histogram] = field(default_factory=dict)
    refusals: dict[tuple[str, str], int] = field(default_factory=dict)
    upstream_errors: dict[tuple[str, str], int] = field(default_factory=dict)
    auth_failures: int = 0
    in_flight: int = 0


def bump(table: dict[tuple[str, str], int], key: tuple[str, str]) -> None:
    """Add one to a labelled counter, creating the series at its first sample.

    Created on use, so an unseen status has no line rather than a misleading zero.
    """
    table[key] = table.get(key, 0) + 1


def observe_request(metrics: Metrics, upstream: str, status: int, ttfb: float | None) -> None:
    """Count one answered request by upstream and status, and time it if an upstream answered.

    Requests the relay answered itself (`ttfb` None) are not timed, so their
    near-zero latency does not drag the distribution toward relay overhead.
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

    bisect_left because buckets are `le`: a value exactly on a bound belongs to it.
    """
    return bisect_left(TTFB_BUCKETS, seconds)


def count_refusal(metrics: Metrics, scope: str, kind: str) -> None:
    """One request turned away by a limit: which scope refused, and on which measure."""
    bump(metrics.refusals, (scope, kind))


def count_auth_failure(metrics: Metrics) -> None:
    """One request refused for a missing or invalid credential.

    Unlabelled and not counted as a request: no upstream was chosen yet.
    """
    metrics.auth_failures += 1


def count_upstream_error(metrics: Metrics, upstream: str, error: str) -> None:
    """One failure reaching an upstream, by upstream and exception type.

    A stream that breaks after headers arrived is not counted here; it is logged.
    """
    bump(metrics.upstream_errors, (upstream, error))


def track_in_flight(metrics: Metrics, release: Callable[[], None]) -> Callable[[], None]:
    """Count one request as in flight, wrapping the release that ends it.

    Wrapping ties the gauge to the slots' lifetime. Idempotent like `release_all`,
    since a gauge decremented twice stays wrong; the wrapped release still runs on
    every call so the slots do not depend on the gauge logic.
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

    Backslash first, or it would re-escape the backslashes the others add.
    Upstream names come from TOML keys, and one bad quote drops the whole scrape.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_labels(labels: Sequence[tuple[str, str]]) -> str:
    """The label set as the format writes it, or nothing at all when a sample has none."""
    if not labels:
        return ""
    inner = ",".join(f'{name}="{escape_label(value)}"' for name, value in labels)
    return "{" + inner + "}"


def render_value(value: float) -> str:
    """A sample value via repr, which round-trips floats and prints ints plainly."""
    return repr(value)


def render_family(name: str, kind: str, description: str, samples: Iterable[Sample]) -> list[str]:
    """One metric family: HELP and TYPE lines, then its samples, even when there are none."""
    lines = [f"# HELP {name} {description}", f"# TYPE {name} {kind}"]
    lines += [
        f"{sample_name}{render_labels(labels)} {render_value(value)}"
        for sample_name, labels, value in samples
    ]
    return lines


def ttfb_samples(histograms: dict[str, Histogram]) -> list[Sample]:
    """Every histogram's buckets, sum and count, made cumulative as the format requires.

    Prometheus does not reject non-cumulative buckets; it computes wrong quantiles.
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

    Sorted within each family so two scrapes of an unchanged relay diff cleanly.
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
