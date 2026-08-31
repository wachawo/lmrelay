#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Both limits' arithmetic: the bucket that refills, and the counter in flight."""

# Local imports
from lmrelay.ratelimit import (
    IDLE_EVICTION,
    SWEEP_INTERVAL,
    InflightCounter,
    build_limiter,
    effective_burst,
    limiter_key,
    release_once,
)

CALLER = "addr:198.51.100.4"
OTHER = "addr:198.51.100.9"

# An arbitrary starting point: every test here drives the clock by hand rather
# than sleeping, since the limiter is handed the time by its caller.
NOW = 1_000.0


def fresh(rate: float, burst: float):
    """A limiter whose sweep clock starts at NOW rather than at time.monotonic().

    Re-seeded because `build_limiter` reads the real monotonic clock, which on a
    machine up for a month is millions of seconds ahead of NOW: every sweep would
    then find `now - swept` negative, return early, and evict nothing, which
    reads exactly like a sweep that ran and found nothing to do. That is the
    shape of the bug these tests exist for, so the test must not be able to
    reproduce it by accident.
    """
    limiter = build_limiter(rate, burst)
    limiter.swept = NOW
    return limiter


class TestSpendingTheAllowance:
    """`RateLimiter.take`: what it lets through, and what it says to wait."""

    def test_a_caller_starts_with_a_full_bucket(self):
        """Otherwise the first request after every restart is the refused one."""
        limiter = fresh(1.0, 3.0)
        assert [limiter.take(CALLER, NOW) for _ in range(3)] == [0.0, 0.0, 0.0]

    def test_and_the_one_after_the_burst_is_refused(self):
        limiter = fresh(1.0, 3.0)
        for _ in range(3):
            limiter.take(CALLER, NOW)
        assert limiter.take(CALLER, NOW) > 0.0

    def test_the_wait_is_the_time_until_one_token_has_refilled(self):
        """The number becomes a Retry-After, so a caller that obeys it must be
        admitted when it returns, rather than refused a second time."""
        limiter = fresh(2.0, 1.0)
        limiter.take(CALLER, NOW)
        assert limiter.take(CALLER, NOW) == 0.5
        assert limiter.take(CALLER, NOW + 0.5) == 0.0

    def test_the_allowance_refills_over_time(self):
        limiter = fresh(2.0, 5.0)
        for _ in range(5):
            limiter.take(CALLER, NOW)
        assert limiter.take(CALLER, NOW) > 0.0
        assert [limiter.take(CALLER, NOW + 1.05) for _ in range(2)] == [0.0, 0.0]
        assert limiter.take(CALLER, NOW + 1.05) > 0.0

    def test_but_never_past_full(self):
        """A bucket that banked an idle hour would let a caller spend an hour's
        worth at once, which is the burst the limit exists to bound."""
        limiter = fresh(1.0, 2.0)
        limiter.take(CALLER, NOW)
        allowed = [limiter.take(CALLER, NOW + 3600) == 0.0 for _ in range(3)]
        assert allowed == [True, True, False]

    def test_callers_are_counted_apart(self):
        """One caller spending its allowance must not refuse anybody else."""
        limiter = fresh(1.0, 1.0)
        limiter.take(CALLER, NOW)
        assert limiter.take(CALLER, NOW) > 0.0
        assert limiter.take(OTHER, NOW) == 0.0

    def test_a_refusal_does_not_deepen_the_debt(self):
        """A caller that keeps retrying must not push its own recovery further
        away with every attempt, or a busy client can never come back."""
        limiter = fresh(1.0, 1.0)
        limiter.take(CALLER, NOW)
        waits = [limiter.take(CALLER, NOW) for _ in range(5)]
        assert waits == [1.0, 1.0, 1.0, 1.0, 1.0]


class TestForgettingIdleCallers:
    """`RateLimiter.sweep`: the table must not grow by one row per address."""

    def test_a_caller_idle_long_enough_to_be_full_again_is_forgotten(self):
        """The regression this class exists for. The keep-condition tested the
        STORED token count against the burst, but `take` always leaves the
        stored count below the burst, so no bucket was ever seen as full and
        nothing was ever evicted: the table grew for the life of the process."""
        limiter = fresh(2.0, 5.0)
        for index in range(1000):
            limiter.take(f"addr:198.51.100.{index}", NOW)
        assert len(limiter.buckets) == 1000
        limiter.sweep(NOW + IDLE_EVICTION + 1)
        assert limiter.buckets == {}

    def test_a_caller_that_has_been_active_recently_is_kept(self):
        limiter = fresh(2.0, 5.0)
        limiter.take(CALLER, NOW)
        limiter.sweep(NOW + IDLE_EVICTION - 1)
        assert list(limiter.buckets) == [CALLER]

    def test_a_bucket_that_has_not_refilled_is_kept_however_idle(self):
        """Forgetting it would hand back an allowance the caller has not waited
        for: a new bucket starts full, so dropping a half-empty one is a refund.
        At a low enough rate, IDLE_EVICTION is not long enough to refill one."""
        limiter = fresh(0.001, 5.0)
        limiter.take(CALLER, NOW)
        limiter.sweep(NOW + IDLE_EVICTION + 1)
        assert list(limiter.buckets) == [CALLER]

    def test_and_forgotten_once_it_genuinely_has(self):
        limiter = fresh(0.001, 5.0)
        limiter.take(CALLER, NOW)
        limiter.sweep(NOW + 100_000)
        assert limiter.buckets == {}

    def test_a_forgotten_caller_is_not_a_punished_one(self):
        """Eviction has to be invisible from outside: a caller that comes back
        after an idle hour gets a full bucket either way."""
        limiter = fresh(2.0, 5.0)
        limiter.take(CALLER, NOW)
        limiter.sweep(NOW + 3600)
        assert [limiter.take(CALLER, NOW + 3600) for _ in range(5)] == [0.0] * 5

    def test_the_sweep_does_not_run_on_every_request(self):
        """It rebuilds the whole table, so doing it per request would put the
        size of the table into the latency of every caller."""
        limiter = fresh(2.0, 5.0)
        limiter.take(CALLER, NOW)
        limiter.sweep(NOW + SWEEP_INTERVAL - 1)
        assert list(limiter.buckets) == [CALLER]


class TestWhenTheLimitIsOff:
    """`build_limiter` and the burst floor."""

    def test_no_rate_means_no_limiter_at_all(self):
        """0 is the default, and the middleware skips the whole block on None,
        so an install that never asked for a limit keeps no table."""
        assert build_limiter(0, 0) is None

    def test_a_negative_rate_is_off_too(self):
        assert build_limiter(-1, 5) is None

    def test_an_unset_burst_still_admits_one_request(self):
        """`rate_burst` defaults to 0, meaning "not set" rather than "none". A
        bucket that could hold less than one request would refuse every one."""
        assert effective_burst(0) == 1.0
        limiter = build_limiter(1.0, 0)
        assert limiter.burst == 1.0
        assert limiter.take(CALLER, NOW) == 0.0

    def test_a_burst_below_one_is_read_as_one(self):
        assert effective_burst(0.5) == 1.0

    def test_and_a_real_burst_is_left_alone(self):
        assert effective_burst(5.0) == 5.0


class TestTakingASlot:
    """What `acquire` admits and what it turns away."""

    def test_the_first_request_is_admitted(self):
        counter = InflightCounter({})
        assert counter.acquire(CALLER, 2) is True
        assert counter.counts == {CALLER: 1}

    def test_and_so_is_one_that_fills_the_limit(self):
        """The cap is how many may be held, not how many may be held before the
        last one is refused: `max_concurrent = 1` has to admit the first."""
        counter = InflightCounter({CALLER: 1})
        assert counter.acquire(CALLER, 2) is True
        assert counter.counts == {CALLER: 2}

    def test_the_one_after_that_is_not(self):
        counter = InflightCounter({CALLER: 2})
        assert counter.acquire(CALLER, 2) is False

    def test_and_a_refusal_does_not_count_against_the_caller(self):
        """It never held a slot, so recording one would let a caller who kept
        retrying raise their own count and never be admitted again."""
        counter = InflightCounter({CALLER: 2})
        counter.acquire(CALLER, 2)
        assert counter.counts == {CALLER: 2}

    def test_a_limit_of_zero_admits_everything(self):
        """0 is the default, and it has to leave an install that never asked for
        a cap behaving as it did before there was one."""
        counter = InflightCounter({})
        for expected in (1, 2, 3):
            assert counter.acquire(CALLER, 0) is True
            assert counter.counts == {CALLER: expected}

    def test_callers_are_counted_apart(self):
        """The cap is per caller. One caller filling theirs must not refuse
        anyone else, or a single busy client is an outage for the rest."""
        counter = InflightCounter({CALLER: 2})
        assert counter.acquire(OTHER, 2) is True
        assert counter.counts == {CALLER: 2, OTHER: 1}


class TestGivingItBack:
    """`release`, and the table emptying itself."""

    def test_a_release_frees_the_next_request(self):
        counter = InflightCounter({CALLER: 2})
        counter.release(CALLER)
        assert counter.acquire(CALLER, 2) is True

    def test_the_last_release_forgets_the_caller_entirely(self):
        """This is why there is no sweep beside the rate limiter's: an entry
        lives only as long as a request does, so the table cannot grow by one
        row per address that ever knocked."""
        counter = InflightCounter({CALLER: 1})
        counter.release(CALLER)
        assert counter.counts == {}

    def test_releasing_a_caller_that_holds_nothing_leaves_no_trace(self):
        """A zero or negative entry would be a caller the table remembers for
        nothing, and a negative one would hand out a free slot later."""
        counter = InflightCounter({})
        counter.release(CALLER)
        assert counter.counts == {}

    def test_and_it_does_not_touch_anyone_else(self):
        counter = InflightCounter({CALLER: 1, OTHER: 1})
        counter.release(CALLER)
        assert counter.counts == {OTHER: 1}


class TestChangingTheNumberUnderLiveRequests:
    """The limit is an argument, not a field, and a reload rebuilds nothing."""

    def test_a_lower_limit_governs_the_next_arrival(self):
        counter = InflightCounter({CALLER: 2})
        assert counter.acquire(CALLER, 1) is False

    def test_but_the_requests_already_holding_slots_keep_them(self):
        """A counter rebuilt to change the number would start empty, so every
        answer already streaming would release a slot it was no longer recorded
        as holding, and the count would drift below zero for the rest of the
        process."""
        counter = InflightCounter({CALLER: 2})
        counter.acquire(CALLER, 1)
        counter.release(CALLER)
        counter.release(CALLER)
        assert counter.counts == {}

    def test_raising_it_admits_the_next_one_without_disturbing_anything(self):
        counter = InflightCounter({CALLER: 2})
        assert counter.acquire(CALLER, 4) is True
        assert counter.counts == {CALLER: 3}


class TestReleasingExactlyOnce:
    """`release_once`, which is what the relay hands to the paths out."""

    def test_it_gives_the_slot_back(self):
        counter = InflightCounter({CALLER: 1})
        release_once(counter, CALLER)()
        assert counter.counts == {}

    def test_a_second_call_does_nothing(self):
        counter = InflightCounter({CALLER: 1})
        release = release_once(counter, CALLER)
        release()
        release()
        assert counter.counts == {}

    def test_and_that_is_the_point_of_it(self):
        """Without the flag the second call would decrement the count of a
        different live request, and let that caller past the cap. The relay's
        error paths and its body generator are meant not to overlap; this is
        what makes it safe that they might."""
        counter = InflightCounter({CALLER: 1})
        release = release_once(counter, CALLER)
        release()
        counter.acquire(CALLER, 2)
        release()
        assert counter.counts == {CALLER: 1}

    def test_two_slots_have_two_releases(self):
        counter = InflightCounter({})
        counter.acquire(CALLER, 0)
        counter.acquire(CALLER, 0)
        release_once(counter, CALLER)()
        assert counter.counts == {CALLER: 1}


class TestBothLimitsCountTheSameCaller:
    """The cap and the bucket share `limiter_key`, so an operator learns how a
    caller is identified once rather than once per key in the config file."""

    def test_a_credential_is_the_caller(self):
        counter = InflightCounter({})
        counter.acquire(limiter_key("secret", "198.51.100.4"), 1)
        assert counter.acquire(limiter_key("secret", "203.0.113.9"), 1) is False

    def test_and_without_one_the_address_is(self):
        counter = InflightCounter({})
        counter.acquire(limiter_key(None, "198.51.100.4"), 1)
        assert counter.acquire(limiter_key(None, "203.0.113.9"), 1) is True
