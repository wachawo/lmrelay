#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Per-caller request limiting, held in memory: how often, and how many at once."""

import time
from collections.abc import Callable
from dataclasses import dataclass

# Buckets are dropped once they have been full and untouched for this long.
# Full means the caller owes nothing, so forgetting them changes no decision;
# without it the table grows by one entry per address that ever knocked.
IDLE_EVICTION = 300.0

# How often the sweep runs. Every request would be wasteful and never would let
# the table grow between quiet periods.
SWEEP_INTERVAL = 60.0


@dataclass
class Bucket:
    """One caller's allowance: how much is left, and when that was true."""

    tokens: float
    updated: float


@dataclass
class RateLimiter:
    """A token bucket per caller, refilling at `rate` per second.

    A bucket rather than a counter per fixed window: a window lets a caller
    spend its whole allowance in the last instant of one window and again in
    the first instant of the next, which is twice the rate over the boundary
    and exactly the burst a limit is meant to prevent.

    Not shared between processes. Under several uvicorn workers each holds its
    own table and the effective limit multiplies, which is a reason to say so
    in the documentation rather than to reach for Redis: this relay is one
    process in front of one Ollama.
    """

    rate: float
    burst: float
    buckets: dict[str, Bucket]
    swept: float

    def take(self, key: str, now: float) -> float:
        """Spend one request for `key`. Returns 0.0 when allowed, else the wait.

        The wait is what an honest Retry-After needs: the time until one token
        has refilled, not a fixed guess the caller cannot act on.
        """
        bucket = self.buckets.get(key)
        if bucket is None:
            # A caller starts full, so the first request after a restart is
            # never the one that gets refused.
            bucket = Bucket(tokens=self.burst, updated=now)
            self.buckets[key] = bucket

        bucket.tokens = min(self.burst, bucket.tokens + (now - bucket.updated) * self.rate)
        bucket.updated = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0
        return (1.0 - bucket.tokens) / self.rate

    def sweep(self, now: float) -> None:
        """Forget callers idle long enough that their bucket has refilled to full.

        Full is measured after the refill the next `take` would apply, not on
        the count as stored. Every `take` leaves the stored count below `burst`
        (the allowed path spends a token, the refused path only gets there with
        less than one left), so a bucket is never recorded as full, and testing
        the stored count kept every caller that had ever knocked for the life of
        the process: the eviction this exists for could not fire at all.

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


def effective_burst(burst: float) -> float:
    """The burst the limiter will hold, which is not always the one configured.

    A bucket too small to hold one request would refuse every request, so the
    floor is one, and `rate_burst = 0` (the default, meaning "not set" rather
    than "none") is read as one.

    Named here rather than clamped inline because the refusal message and the
    reload log quote the number too: quoting the configured one told a caller
    "burst 0" for a limiter that was in fact allowing a request, which reads as
    a relay refusing everything.
    """
    return max(burst, 1.0)


def build_limiter(rate: float, burst: float) -> RateLimiter | None:
    """A limiter for a configured rate, or None when the limit is off."""
    if rate <= 0:
        return None
    return RateLimiter(
        rate=rate, burst=effective_burst(burst), buckets={}, swept=time.monotonic()
    )


@dataclass
class InflightCounter:
    """How many relayed requests each caller is holding open right now.

    A different measure from the bucket above, not a second spelling of it: the
    bucket says how often a caller may start, this says how many answers they
    may be receiving at once. A relayed answer runs for as long as the model
    takes, so a rate an operator would call generous still lets one caller
    occupy every generation the machine can do at once.

    Self-cleaning, which is why there is no sweep beside RateLimiter's: an entry
    exists only while that caller has something in flight, and the last release
    deletes it. Nothing to forget, and no second timer.

    Not shared between processes, exactly as the bucket is not: under several
    uvicorn workers each holds its own table and the effective cap multiplies.
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
        """Give a slot back, forgetting the caller at zero."""
        remaining = self.counts.get(key, 0) - 1
        if remaining > 0:
            self.counts[key] = remaining
        else:
            self.counts.pop(key, None)


def release_once(counter: InflightCounter, key: str) -> Callable[[], None]:
    """A release for one held slot that is safe to call more than once.

    A slot has several ways home: the body generator gives it back when the
    answer ends or the caller hangs up, and every path that never reaches a body
    gives it back on the way to its error. Those paths are meant not to overlap,
    but a slot released twice would decrement a count belonging to another live
    request and let that caller past the cap, so the belt and the braces are
    only worth wearing if the second one does nothing.
    """
    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        counter.release(key)

    return release


def limiter_key(token: str | None, client: str) -> str:
    """What the allowance belongs to: the credential if there is one, else the address.

    One rule for both limits above, so an operator learns how a caller is
    identified once rather than once per key in the config file.

    Keyed on the token first because that is what identifies a caller: two
    machines sharing one token are one caller and should share one allowance,
    while an address behind NAT is many callers wearing one number. The address
    is the fallback for a relay with auth off, where nothing else distinguishes
    anyone.
    """
    return f"token:{token}" if token else f"addr:{client}"


def main():
    pass


if __name__ == "__main__":
    main()
