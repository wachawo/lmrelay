#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""In-memory request limiting: three scopes, each with a rate and a concurrency cap."""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

# Narrowest first: when several scopes would refuse, the refusal names the most
# specific one, which is also the limit the operator raises first.
SCOPES = ("per_token", "per_address", "total")

# Two separate questions: "10/30m" says nothing about whether those ten may run
# together, and for a machine holding one model the answer is usually no.
LIMIT_KEYS = ("concurrent", "rate")

# A rate is `count/period`, e.g. `10/30m`. A bare number is refused rather than
# read as seconds, since `30` means half an hour as often as half a minute.
PERIOD_UNITS   = {"s": 1, "m": 60, "h": 3600}
PERIOD_PATTERN = re.compile(r"^(\d+)([smh])$")
RATE_PATTERN   = re.compile(r"^(\d+)/(\d+[smh])$")
NO_RATE        = ""

# A bucket that is full and idle this long is dropped: forgetting it changes no
# decision, and without it the table grows by one entry per address ever seen.
IDLE_EVICTION = 300.0

SWEEP_INTERVAL = 60.0


def parse_period(value: str) -> float | None:
    """Seconds for a duration like `30m`, or None when it is not one."""
    match = PERIOD_PATTERN.match(value.strip())
    if match is None:
        return None
    return float(match.group(1)) * PERIOD_UNITS[match.group(2)]


def parse_rate(value: str) -> tuple[int, float] | None:
    """A count and its period in seconds for `10/30m`, or None when it is not one."""
    match = RATE_PATTERN.match(value.strip())
    if match is None:
        return None
    seconds = parse_period(match.group(2))
    if not seconds:
        # `5/0s` would be an infinite rate; "off" is spelled as an empty rate.
        return None
    return int(match.group(1)), seconds


@dataclass(frozen=True)
class ScopeLimits:
    """What one scope allows: how many at once, and how often. Both off by default.

    `rate` is kept as the operator spelled it, so everything that quotes it back,
    refusals included, shows `10/60s` rather than a normalised `10/1m`.
    """

    concurrent: int = 0
    rate: str = NO_RATE

    def configured(self) -> bool:
        """Whether this scope asks for anything at all."""
        return self.concurrent > 0 or self.per_second() > 0

    def allowance(self) -> tuple[int, float] | None:
        """The rate as a count and a period in seconds, or None when it is off."""
        return parse_rate(self.rate) if self.rate else None

    def per_second(self) -> float:
        """Requests per second, or 0 when only the concurrency cap applies."""
        allowance = self.allowance()
        if allowance is None:
            return 0.0
        count, seconds = allowance
        return count / seconds if count > 0 and seconds > 0 else 0.0


def default_limits() -> dict[str, ScopeLimits]:
    """Every scope off, which is what a config that says nothing gets."""
    return {scope: ScopeLimits() for scope in SCOPES}


@dataclass(frozen=True)
class Refusal:
    """Which scope turned a request away, and on which of its two measures.

    `wait` is the seconds until a rate refusal would pass, for Retry-After. A slot
    refusal leaves it at zero: a slot frees when someone else's answer ends, which
    may be minutes away, so any guessed number would be wrong.
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

    A bucket rather than a fixed window, which would allow the whole allowance at
    the end of one window and again at the start of the next. Not shared between
    processes: under several uvicorn workers the effective limit multiplies.
    """

    rate: float
    burst: float
    buckets: dict[str, Bucket]
    swept: float

    def bucket_at(self, key: str, now: float) -> Bucket:
        """Bring one key's bucket up to date, creating it full, spending nothing."""
        bucket = self.buckets.get(key)
        if bucket is None:
            bucket = Bucket(tokens=self.burst, updated=now)
            self.buckets[key] = bucket
        bucket.tokens = min(self.burst, bucket.tokens + (now - bucket.updated) * self.rate)
        bucket.updated = now
        return bucket

    def wait_for(self, key: str, now: float) -> float:
        """0.0 when a request would be allowed, else the seconds until it would be.

        Spends nothing, so a request refused by a later scope has not drained this one.
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

        Fullness is computed with the pending refill, not read from the stored
        count, which is never full after a charge. At a low enough rate
        IDLE_EVICTION alone does not refill a bucket, and dropping it early would
        hand back an allowance the caller has not waited for.
        """
        if now - self.swept < SWEEP_INTERVAL:
            return
        self.swept = now
        self.buckets = {
            key: bucket for key, bucket in self.buckets.items()
            if now - bucket.updated < IDLE_EVICTION
            or bucket.tokens + (now - bucket.updated) * self.rate < self.burst
        }


def build_limiter(limits: ScopeLimits) -> RateLimiter | None:
    """A limiter for a scope that has a rate, or None when it has not.

    The bucket holds the rate's own count, so `10/30m` allows ten together and
    then a wait. How many of those run at once is `concurrent`, not this.
    """
    allowance = limits.allowance()
    per_second = limits.per_second()
    if allowance is None or per_second <= 0:
        return None
    return RateLimiter(
        rate=per_second, burst=float(allowance[0]), buckets={}, swept=time.monotonic()
    )


def build_limiters(limits: dict[str, ScopeLimits]) -> dict[str, RateLimiter | None]:
    """One limiter per scope, None where that scope has no rate."""
    return {scope: build_limiter(limits[scope]) for scope in SCOPES}


@dataclass
class InflightCounter:
    """How many relayed requests each key is holding open right now.

    One table for all scopes; keys are prefixed apart. Self-cleaning, since the
    last release deletes an entry, so it needs no sweep. Not shared between
    processes, which matters most for `total`, the scope protecting the machine.
    """

    counts: dict[str, int]

    def acquire(self, key: str, limit: int) -> bool:
        """Take a slot for `key`, or return False when `limit` is already spent.

        The limit is passed per call rather than stored so a reload needs no new
        counter: answers already streaming keep their slots and release them
        correctly, and the new limit governs the next arrival.
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

    The keys are fixed at acquire time, so a reload that turns a scope off cannot
    strand its slot. Idempotent because a slot has several release paths, and a
    double release would let another live caller past the cap.
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

    With auth off there is no token, so `per_token` is skipped, not refused.
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

    Order matters: check every rate, then take slots (rolled back on refusal),
    and only then spend tokens, so a refused request costs nothing in any scope.
    `limiters` hold bucket state and are rebuilt when rates change; `limits` are
    read per call, so caps change without disturbing slots already held.
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

    # Swept regardless of the outcome, so a relay refusing everything still forgets.
    for limiter in limiters.values():
        if limiter is not None:
            limiter.sweep(now)
    return refusal, release_all(counter, taken)


def describe_rate(limits: ScopeLimits) -> str:
    """The rate as the operator wrote it, e.g. `10/30m`, or 'off'."""
    return limits.rate if limits.per_second() > 0 else "off"


def describe_slots(limits: ScopeLimits) -> str:
    """How many at once, for the refusal that is about how many at once."""
    return f"{limits.concurrent} at once" if limits.concurrent > 0 else "off"


def describe_scope(limits: ScopeLimits) -> str:
    """One scope in full, both halves when both are set, as `status` and logs print it."""
    parts = [
        part for part in (describe_rate(limits), describe_slots(limits)) if part != "off"
    ]
    return ", ".join(parts) or "off"


def describe_limits(limits: dict[str, ScopeLimits]) -> str:
    """Every configured scope, narrowest first, or 'off' when none is."""
    named = [
        f"{scope} {describe_scope(limits[scope])}"
        for scope in SCOPES if limits[scope].configured()
    ]
    return "; ".join(named) or "off"


def main():
    pass


if __name__ == "__main__":
    main()
