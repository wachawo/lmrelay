#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Request limiting, held in memory: three scopes, how often and how many at once."""

import time
from collections.abc import Callable
from dataclasses import dataclass

# The three scopes, narrowest first. The order is the whole of the rule about
# which limit a refusal names: when several would refuse at the same instant the
# caller is told the most specific true thing, which is also the number their
# operator raises first. Being told the relay is full while you personally are
# the reason is the wrong answer even though it is true.
SCOPES = ("per_token", "per_address", "total")

# The three keys every scope has, so one sentence covers the whole table: three
# scopes, the same three keys, a request must pass every one you set.
LIMIT_KEYS = ("rate", "burst", "concurrent")

# Buckets are dropped once they have been full and untouched for this long.
# Full means the caller owes nothing, so forgetting them changes no decision;
# without it the table grows by one entry per address that ever knocked.
IDLE_EVICTION = 300.0

# How often the sweep runs. Every request would be wasteful and never would let
# the table grow between quiet periods.
SWEEP_INTERVAL = 60.0


@dataclass(frozen=True)
class ScopeLimits:
    """What one scope allows. All three off by default, which is the whole table.

    Symmetric with the other two scopes on purpose. A non-uniform table, a rate
    here and a count there, is a second thing to learn and the first thing to
    get wrong.
    """

    rate: float = 0.0
    burst: float = 0.0
    concurrent: int = 0

    def configured(self) -> bool:
        """Whether this scope asks for anything at all."""
        return self.rate > 0 or self.concurrent > 0


def default_limits() -> dict[str, ScopeLimits]:
    """Every scope off, which is what a config that says nothing gets."""
    return {scope: ScopeLimits() for scope in SCOPES}


@dataclass(frozen=True)
class Refusal:
    """Which scope turned a request away, and on which of its two measures.

    `wait` is the seconds until a rate refusal would pass, and is what an honest
    Retry-After needs. A slot refusal leaves it at zero and carries no header at
    all: a slot frees when a model finishes answering somebody else, and with no
    read timeout that may be minutes away, so a guessed number would be a lie
    and a client obeying it would retry into the same refusal.
    """

    scope: str
    kind: str
    wait: float


@dataclass
class Bucket:
    """One caller's allowance: how much is left, and when that was true."""

    tokens: float
    updated: float


@dataclass
class RateLimiter:
    """A token bucket per key, refilling at `rate` per second.

    A bucket rather than a counter per fixed window: a window lets a caller
    spend its whole allowance in the last instant of one window and again in
    the first instant of the next, which is twice the rate over the boundary
    and exactly the burst a limit is meant to prevent.

    One limiter per scope, each with its own table, because the three scopes
    hold different numbers. Asking and charging are two calls rather than one so
    that admission can ask every scope before it charges any: see `admit`.

    Not shared between processes. Under several uvicorn workers each holds its
    own table and the effective limit multiplies, which is a reason to say so
    in the documentation rather than to reach for Redis: this relay is one
    process in front of one Ollama.
    """

    rate: float
    burst: float
    buckets: dict[str, Bucket]
    swept: float

    def bucket_at(self, key: str, now: float) -> Bucket:
        """Bring one key's bucket up to date, creating it full, spending nothing."""
        bucket = self.buckets.get(key)
        if bucket is None:
            # A caller starts full, so the first request after a restart is
            # never the one that gets refused.
            bucket = Bucket(tokens=self.burst, updated=now)
            self.buckets[key] = bucket
        bucket.tokens = min(self.burst, bucket.tokens + (now - bucket.updated) * self.rate)
        bucket.updated = now
        return bucket

    def wait_for(self, key: str, now: float) -> float:
        """0.0 when a request would be allowed, else the seconds until it would be.

        Costs the caller nothing, which is what makes all-or-nothing admission
        possible: a request refused by a later scope must not have drained this
        one on its way past.
        """
        bucket = self.bucket_at(key, now)
        if bucket.tokens >= 1.0:
            return 0.0
        return (1.0 - bucket.tokens) / self.rate

    def spend(self, key: str, now: float) -> None:
        """Charge one request. Only ever called once every scope has said yes."""
        self.bucket_at(key, now).tokens -= 1.0

    def sweep(self, now: float) -> None:
        """Forget keys idle long enough that their bucket has refilled to full.

        Full is measured after the refill the next `wait_for` would apply, not on
        the count as stored. Every charged request leaves the stored count below
        `burst`, so a bucket is never recorded as full, and testing the stored
        count kept every caller that had ever knocked for the life of the
        process: the eviction this exists for could not fire at all.

        Dropping a full bucket changes no decision, since a caller with no
        bucket starts with one. Dropping a bucket that has not refilled would
        hand back an allowance the caller has not waited for, which is why the
        refill is computed rather than assumed from the idle time alone: at a
        rate low enough, IDLE_EVICTION is not long enough to refill one.
        """
        if now - self.swept < SWEEP_INTERVAL:
            return
        self.swept = now
        self.buckets = {
            key: bucket for key, bucket in self.buckets.items()
            if now - bucket.updated < IDLE_EVICTION
            or bucket.tokens + (now - bucket.updated) * self.rate < self.burst
        }


def effective_burst(rate: float, burst: float) -> float:
    """The burst the limiter will hold, which is not always the one configured.

    An unset burst is one second's worth of `rate`, floored at one. Read as a
    flat one it made `rate = 20` refuse the second of two simultaneous requests
    and then round a 50ms wait up to the one second the header takes, so an
    operator who wrote "twenty per second" got one per 50ms strictly spaced.
    A bucket too small to hold one request would refuse every request, so an
    explicit burst is floored at one too.

    Named here rather than clamped inline because the refusal message and the
    reload log quote the number as well: quoting the configured one told a
    caller "burst 0" for a limiter that was in fact allowing a request through,
    which reads as a relay refusing everything.
    """
    return max(rate if burst <= 0 else burst, 1.0)


def build_limiter(rate: float, burst: float) -> RateLimiter | None:
    """A limiter for a configured rate, or None when the limit is off."""
    if rate <= 0:
        return None
    return RateLimiter(
        rate=rate, burst=effective_burst(rate, burst), buckets={}, swept=time.monotonic()
    )


def build_limiters(limits: dict[str, ScopeLimits]) -> dict[str, RateLimiter | None]:
    """One limiter per scope, None where that scope's rate is off."""
    return {scope: build_limiter(limits[scope].rate, limits[scope].burst) for scope in SCOPES}


@dataclass
class InflightCounter:
    """How many relayed requests each key is holding open right now.

    A different measure from the bucket above, not a second spelling of it: the
    bucket says how often a caller may start, this says how many answers they
    may be receiving at once. A relayed answer runs for as long as the model
    takes, so a rate an operator would call generous still lets one caller
    occupy every generation the machine can do at once.

    One table for all three scopes, which is safe because their keys are
    prefixed apart: an address and a credential spelled the same are two rows.

    Self-cleaning, which is why there is no sweep beside RateLimiter's: an entry
    exists only while something is in flight against it, and the last release
    deletes it. Nothing to forget, and no second timer.

    Not shared between processes, exactly as the buckets are not: under several
    uvicorn workers each holds its own table and the effective cap multiplies.
    That is worse for the total scope than for the other two, since the total is
    the one protecting the machine.
    """

    counts: dict[str, int]

    def acquire(self, key: str, limit: int) -> bool:
        """Take a slot for `key`, or return False when `limit` is already spent.

        The limit is an argument rather than a field so that a reload has
        nothing to rebuild: a fresh counter would start empty, dropping the
        count of every answer already streaming, and each of those would then
        release a slot that had never been taken. Live requests keep what they
        hold and a new number governs the next arrival.
        """
        if limit > 0 and self.counts.get(key, 0) >= limit:
            return False
        self.counts[key] = self.counts.get(key, 0) + 1
        return True

    def release(self, key: str) -> None:
        """Give a slot back, forgetting the key at zero."""
        remaining = self.counts.get(key, 0) - 1
        if remaining > 0:
            self.counts[key] = remaining
        else:
            self.counts.pop(key, None)


def release_all(counter: InflightCounter, keys: tuple[str, ...]) -> Callable[[], None]:
    """A release for one admission's whole set of slots, safe to call more than once.

    The set is fixed when the slots are taken rather than recomputed on the way
    out, so a reload that turns a scope off between the two cannot leave that
    scope's slot held for the life of the process.

    A slot has several ways home: the body generator gives it back when the
    answer ends or the caller hangs up, and every path that never reaches a body
    gives it back on the way to its error. Those paths are meant not to overlap,
    but a slot released twice would decrement a count belonging to another live
    request and let that caller past the cap, so the belt and the braces are
    only worth wearing if the second one does nothing.

    An admission that took nothing, which includes every refusal, gets a release
    that does nothing, so no caller has to ask whether it holds anything.
    """
    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        for key in keys:
            counter.release(key)

    return release


def scope_keys(token: str | None, client: str) -> dict[str, str | None]:
    """What each scope counts against. None means the scope does not apply.

    With auth off there is no credential, so `per_token` matches nothing and is
    skipped rather than refused: no bucket is created and no slot is held. The
    other two still apply, and `per_address` is doing the whole job.

    With auth on every request has passed auth, so every request has a token and
    there is no third case. It is charged against its token, its address and the
    total, all three. That is not double counting, it is passing three ceilings.
    """
    return {
        "per_token": f"token:{token}" if token else None,
        "per_address": f"addr:{client}",
        "total": "total",
    }


def check_rates(
    limiters: dict[str, RateLimiter | None],
    keys: dict[str, str | None],
    now: float,
) -> Refusal | None:
    """Ask every configured scope what it would do, spending nothing anywhere."""
    for scope in SCOPES:
        key, limiter = keys[scope], limiters.get(scope)
        if key is None or limiter is None:
            continue
        wait = limiter.wait_for(key, now)
        if wait > 0:
            return Refusal(scope=scope, kind="rate", wait=wait)
    return None


def take_slots(
    limits: dict[str, ScopeLimits],
    counter: InflightCounter,
    keys: dict[str, str | None],
) -> tuple[Refusal | None, tuple[str, ...]]:
    """Take every configured scope's slot, giving them all back if one refuses."""
    taken: list[str] = []
    for scope in SCOPES:
        key, cap = keys[scope], limits[scope].concurrent
        if key is None or cap <= 0:
            continue
        if not counter.acquire(key, cap):
            release_all(counter, tuple(taken))()
            return Refusal(scope=scope, kind="concurrent", wait=0.0), ()
        taken.append(key)
    return None, tuple(taken)


def admit(
    limiters: dict[str, RateLimiter | None],
    limits: dict[str, ScopeLimits],
    counter: InflightCounter,
    keys: dict[str, str | None],
    now: float,
) -> tuple[Refusal | None, Callable[[], None]]:
    """Charge every configured scope, or none of them. Returns the refusal and the release.

    Three phases, and the order is the point. Every rate limiter is asked before
    any is charged, the slots are taken next and given back if a later scope
    refuses, and only once every scope has said yes is a single token spent
    anywhere. So a refused request costs nothing, in any scope. Without that, a
    caller refused by the total still had its own bucket drained, and an
    operator got "I was refused, and now I am rate limited too" with no way to
    see why.

    `limiters` is held state and `limits` is read from the config at every call,
    which is the difference between the two limits: a rate limiter has to be
    rebuilt when its numbers move because a bucket holds an allowance measured
    against the old burst, while a cap is a number the counter is handed per
    acquire so that answers already streaming keep the slots they hold.
    """
    refusal = check_rates(limiters, keys, now)
    taken: tuple[str, ...] = ()
    if refusal is None:
        refusal, taken = take_slots(limits, counter, keys)
    if refusal is None:
        for scope in SCOPES:
            key, limiter = keys[scope], limiters.get(scope)
            if key is not None and limiter is not None:
                limiter.spend(key, now)

    # Swept on the way out whatever the answer was, so a relay that is refusing
    # everything still forgets the callers that have gone away.
    for limiter in limiters.values():
        if limiter is not None:
            limiter.sweep(now)
    return refusal, release_all(counter, taken)


def describe_rate(limits: ScopeLimits) -> str:
    """One scope's rate as the limiter will enforce it, for a log line or a refusal.

    The burst is the effective one, not the configured one: they differ whenever
    burst is unset, and quoting the configured number told a caller "burst 0"
    while the limiter was allowing a request through.
    """
    if limits.rate <= 0:
        return "off"
    return f"{limits.rate:g}/s burst {effective_burst(limits.rate, limits.burst):g}"


def describe_scope(limits: ScopeLimits) -> str:
    """One scope in full, both measures, as `status` prints it."""
    parts = []
    if limits.rate > 0:
        parts.append(describe_rate(limits))
    if limits.concurrent > 0:
        parts.append(f"{limits.concurrent} at once")
    return ", ".join(parts) or "off"


def describe_limits(limits: dict[str, ScopeLimits]) -> str:
    """Every scope that asks for anything, narrowest first, or 'off' when none does.

    Only the configured ones, because a relay with one number set is the common
    case and two further lines saying off answer a question nobody asked. The
    single "off" answers the one they did ask, which is whether anything here is
    limited at all: without this line the only way to see the limits in effect
    was to export the whole configuration and read the bundle.
    """
    named = [
        f"{scope} {describe_scope(limits[scope])}"
        for scope in SCOPES if limits[scope].configured()
    ]
    return "; ".join(named) or "off"


def main():
    pass


if __name__ == "__main__":
    main()
