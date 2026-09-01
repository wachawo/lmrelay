#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The limits' arithmetic: the bucket that refills, the counter in flight, and admission."""

import pytest

# Local imports
from lmrelay.ratelimit import (
    IDLE_EVICTION,
    SCOPES,
    SWEEP_INTERVAL,
    InflightCounter,
    RateLimiter,
    ScopeLimits,
    admit,
    build_limiter,
    build_limiters,
    default_limits,
    describe_rate,
    describe_scope,
    parse_period,
    release_all,
    scope_keys,
)

CALLER = "addr:198.51.100.4"
OTHER = "addr:198.51.100.9"

# An arbitrary starting point: every test here drives the clock by hand rather
# than sleeping, since the limiter is handed the time by its caller.
NOW = 1_000.0


def fresh(rate: float, burst: float):
    """A bucket with its sweep clock at NOW rather than at time.monotonic().

    Built directly rather than through `build_limiter`, for two reasons. The
    clock: `build_limiter` reads the real monotonic clock, which on a machine up
    for a month is millions of seconds ahead of NOW, so every sweep would find
    `now - swept` negative, return early and evict nothing, which reads exactly
    like a sweep that ran and found nothing to do. That is the shape of the bug
    these tests exist for. And the arithmetic below is the bucket's own, which
    holds a rate and a capacity whatever the configuration that produced them
    happened to say.
    """
    return RateLimiter(rate=rate, burst=burst, buckets={}, swept=NOW)


def take(limiter, key: str, now: float) -> float:
    """Ask and charge in one call, which is what a single-scope test means.

    The limiter splits the two so that admission can ask every scope before it
    charges any; a test about one bucket's arithmetic wants them together.
    """
    wait = limiter.wait_for(key, now)
    if wait == 0.0:
        limiter.spend(key, now)
    return wait


def limits_of(**scopes: ScopeLimits) -> dict[str, ScopeLimits]:
    """A full limits table with the named scopes set and the rest off."""
    return default_limits() | scopes


class TestSpendingTheAllowance:
    """The token bucket: what it lets through, and what it says to wait."""

    def test_a_caller_starts_with_a_full_bucket(self):
        """Otherwise the first request after every restart is the refused one."""
        limiter = fresh(1.0, 3.0)
        assert [take(limiter, CALLER, NOW) for _ in range(3)] == [0.0, 0.0, 0.0]

    def test_and_the_one_after_the_burst_is_refused(self):
        limiter = fresh(1.0, 3.0)
        for _ in range(3):
            take(limiter, CALLER, NOW)
        assert take(limiter, CALLER, NOW) > 0.0

    def test_the_wait_is_the_time_until_one_token_has_refilled(self):
        """The number becomes a Retry-After, so a caller that obeys it must be
        admitted when it returns, rather than refused a second time."""
        limiter = fresh(2.0, 1.0)
        take(limiter, CALLER, NOW)
        assert take(limiter, CALLER, NOW) == 0.5
        assert take(limiter, CALLER, NOW + 0.5) == 0.0

    def test_the_allowance_refills_over_time(self):
        limiter = fresh(2.0, 5.0)
        for _ in range(5):
            take(limiter, CALLER, NOW)
        assert take(limiter, CALLER, NOW) > 0.0
        assert [take(limiter, CALLER, NOW + 1.05) for _ in range(2)] == [0.0, 0.0]
        assert take(limiter, CALLER, NOW + 1.05) > 0.0

    def test_but_never_past_full(self):
        """A bucket that banked an idle hour would let a caller spend an hour's
        worth at once, which is the burst the limit exists to bound."""
        limiter = fresh(1.0, 2.0)
        take(limiter, CALLER, NOW)
        allowed = [take(limiter, CALLER, NOW + 3600) == 0.0 for _ in range(3)]
        assert allowed == [True, True, False]

    def test_callers_are_counted_apart(self):
        """One caller spending its allowance must not refuse anybody else."""
        limiter = fresh(1.0, 1.0)
        take(limiter, CALLER, NOW)
        assert take(limiter, CALLER, NOW) > 0.0
        assert take(limiter, OTHER, NOW) == 0.0

    def test_a_refusal_does_not_deepen_the_debt(self):
        """A caller that keeps retrying must not push its own recovery further
        away with every attempt, or a busy client can never come back."""
        limiter = fresh(1.0, 1.0)
        take(limiter, CALLER, NOW)
        waits = [take(limiter, CALLER, NOW) for _ in range(5)]
        assert waits == [1.0, 1.0, 1.0, 1.0, 1.0]


class TestAskingWithoutPaying:
    """`wait_for` and `spend` apart, which is what all-or-nothing admission needs."""

    def test_asking_costs_nothing(self):
        """Asked ten times and charged none of them, the caller still has its
        whole burst: this is the property a refusal at a later scope relies on."""
        limiter = fresh(1.0, 3.0)
        assert [limiter.wait_for(CALLER, NOW) for _ in range(10)] == [0.0] * 10
        assert [take(limiter, CALLER, NOW) for _ in range(3)] == [0.0, 0.0, 0.0]

    def test_and_charging_takes_exactly_one(self):
        limiter = fresh(1.0, 3.0)
        limiter.wait_for(CALLER, NOW)
        limiter.spend(CALLER, NOW)
        assert limiter.buckets[CALLER].tokens == 2.0

    def test_charging_a_caller_never_asked_about_still_works(self):
        """`spend` creates the bucket rather than trusting `wait_for` to have
        done it, so a mis-ordered call is a charge rather than a KeyError out of
        the middle of a route."""
        limiter = fresh(1.0, 3.0)
        limiter.spend(CALLER, NOW)
        assert limiter.buckets[CALLER].tokens == 2.0


class TestForgettingIdleCallers:
    """`RateLimiter.sweep`: the table must not grow by one row per address."""

    def test_a_caller_idle_long_enough_to_be_full_again_is_forgotten(self):
        """The regression this class exists for. The keep-condition tested the
        STORED token count against the burst, but a charged request always
        leaves the stored count below the burst, so no bucket was ever seen as
        full and nothing was ever evicted: the table grew for the life of the
        process."""
        limiter = fresh(2.0, 5.0)
        for index in range(1000):
            take(limiter, f"addr:198.51.100.{index}", NOW)
        assert len(limiter.buckets) == 1000
        limiter.sweep(NOW + IDLE_EVICTION + 1)
        assert limiter.buckets == {}

    def test_a_caller_that_has_been_active_recently_is_kept(self):
        limiter = fresh(2.0, 5.0)
        take(limiter, CALLER, NOW)
        limiter.sweep(NOW + IDLE_EVICTION - 1)
        assert list(limiter.buckets) == [CALLER]

    def test_a_bucket_that_has_not_refilled_is_kept_however_idle(self):
        """Forgetting it would hand back an allowance the caller has not waited
        for: a new bucket starts full, so dropping a half-empty one is a refund.
        At a low enough rate, IDLE_EVICTION is not long enough to refill one."""
        limiter = fresh(0.001, 5.0)
        take(limiter, CALLER, NOW)
        limiter.sweep(NOW + IDLE_EVICTION + 1)
        assert list(limiter.buckets) == [CALLER]

    def test_and_forgotten_once_it_genuinely_has(self):
        limiter = fresh(0.001, 5.0)
        take(limiter, CALLER, NOW)
        limiter.sweep(NOW + 100_000)
        assert limiter.buckets == {}

    def test_a_forgotten_caller_is_not_a_punished_one(self):
        """Eviction has to be invisible from outside: a caller that comes back
        after an idle hour gets a full bucket either way."""
        limiter = fresh(2.0, 5.0)
        take(limiter, CALLER, NOW)
        limiter.sweep(NOW + 3600)
        assert [take(limiter, CALLER, NOW + 3600) for _ in range(5)] == [0.0] * 5

    def test_the_sweep_does_not_run_on_every_request(self):
        """It rebuilds the whole table, so doing it per request would put the
        size of the table into the latency of every caller."""
        limiter = fresh(2.0, 5.0)
        take(limiter, CALLER, NOW)
        limiter.sweep(NOW + SWEEP_INTERVAL - 1)
        assert list(limiter.buckets) == [CALLER]

    def test_a_bucket_only_asked_about_is_forgotten_too(self):
        """A scope that said yes and was then overruled by a narrower one leaves
        a full bucket behind. Full is the same as absent, so it must not be the
        one row the table keeps for ever."""
        limiter = fresh(2.0, 5.0)
        limiter.wait_for(CALLER, NOW)
        limiter.sweep(NOW + IDLE_EVICTION + 1)
        assert limiter.buckets == {}


class TestTheBucketOneNumberBuys:
    """`build_limiter`: what a scope with a period gets, and what one without does not."""

    def test_a_scope_that_asks_for_nothing_has_no_limiter(self):
        """0 is the default, and admission skips a None scope entirely, so an
        install that never asked for a limit keeps no table."""
        assert build_limiter(ScopeLimits()) is None

    def test_nor_has_one_that_only_says_how_many_at_once(self):
        """`requests = 4` with no period is a cap, and the counter enforces
        caps. A bucket here would be a second limit nobody asked for."""
        assert build_limiter(ScopeLimits(requests=4)) is None

    def test_the_bucket_holds_the_whole_number_of_requests(self):
        """Ten a minute is ten that may arrive together and then a wait, which
        is what the operator who wrote it expects. The separate burst this
        replaced got that wrong in both directions: unset it was a second's
        worth of the rate, and set it was a third number to keep in step."""
        limiter = build_limiter(ScopeLimits(requests=10, period="1m"))
        assert [take(limiter, CALLER, NOW) for _ in range(10)] == [0.0] * 10
        assert take(limiter, CALLER, NOW) > 0.0

    def test_and_refills_across_the_period_it_was_given(self):
        limiter = build_limiter(ScopeLimits(requests=10, period="1m"))
        for _ in range(10):
            take(limiter, CALLER, NOW)
        assert take(limiter, CALLER, NOW + 6.1) == 0.0

    def test_one_request_a_period_still_admits_one(self):
        """A bucket too small to hold a request would refuse every request, and
        `requests = 1` is the tightest limit an operator can ask for."""
        limiter = build_limiter(ScopeLimits(requests=1, period="2h"))
        assert take(limiter, CALLER, NOW) == 0.0
        assert take(limiter, CALLER, NOW) > 0.0


class TestSpellingAPeriod:
    """One spelling on the command line, in the file, and in every line quoting it."""

    @pytest.mark.parametrize("spelling, seconds", [
        ("0s", 0.0), ("30s", 30.0), ("90s", 90.0), ("1m", 60.0), ("30m", 1800.0),
        ("1h", 3600.0), ("2h", 7200.0),
    ])
    def test_a_whole_number_and_a_unit(self, spelling, seconds):
        assert parse_period(spelling) == seconds

    @pytest.mark.parametrize(
        "spelling", ["30", "", "m", "-5m", "1.5m", "5 m", "1d", "5M", "inf", "nan"]
    )
    def test_and_nothing_else(self, spelling):
        """A bare number above all: `period = 30` is half an hour to whoever
        wrote it about as often as it is half a minute. `inf` and `nan` matter
        for a different reason: a bucket built on nan compares false against
        every threshold, so it would refuse nobody while printing as though it
        were on."""
        assert parse_period(spelling) is None

    def test_the_spelling_is_kept_rather_than_canonicalised(self):
        """`limits set total 1 60s` must not answer `1m`. It is the operator's
        file, and this is the command whose whole point is leaving it alone."""
        assert describe_scope(ScopeLimits(requests=1, period="60s")) == "1 per 60s, 1 at once"

    def test_but_two_spellings_of_one_duration_limit_the_same(self):
        assert ScopeLimits(requests=1, period="60s").rate() == \
            ScopeLimits(requests=1, period="1m").rate()


class TestTakingASlot:
    """What `acquire` admits and what it turns away."""

    def test_the_first_request_is_admitted(self):
        counter = InflightCounter({})
        assert counter.acquire(CALLER, 2) is True
        assert counter.counts == {CALLER: 1}

    def test_and_so_is_one_that_fills_the_limit(self):
        """The cap is how many may be held, not how many may be held before the
        last one is refused: `concurrent = 1` has to admit the first."""
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
        """One caller filling their scope must not refuse anyone else, or a
        single busy client is an outage for the rest."""
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


class TestReleasingTheWholeSetExactlyOnce:
    """`release_all`, which is what the relay hands to the paths out."""

    def test_it_gives_every_slot_back(self):
        counter = InflightCounter({CALLER: 1, "total": 1})
        release_all(counter, (CALLER, "total"))()
        assert counter.counts == {}

    def test_a_second_call_does_nothing(self):
        counter = InflightCounter({CALLER: 1})
        release = release_all(counter, (CALLER,))
        release()
        release()
        assert counter.counts == {}

    def test_and_that_is_the_point_of_it(self):
        """Without the flag the second call would decrement the count of a
        different live request, and let that caller past the cap. The relay's
        error paths and its body generator are meant not to overlap; this is
        what makes it safe that they might."""
        counter = InflightCounter({CALLER: 1})
        release = release_all(counter, (CALLER,))
        release()
        counter.acquire(CALLER, 2)
        release()
        assert counter.counts == {CALLER: 1}

    def test_an_admission_that_took_nothing_releases_nothing(self):
        """Every refusal gets one of these, so that no path out has to ask
        whether it is holding anything."""
        counter = InflightCounter({CALLER: 1})
        release_all(counter, ())()
        assert counter.counts == {CALLER: 1}

    def test_two_slots_have_two_releases(self):
        counter = InflightCounter({})
        counter.acquire(CALLER, 0)
        counter.acquire(CALLER, 0)
        release_all(counter, (CALLER,))()
        assert counter.counts == {CALLER: 1}


class TestWhatEachScopeCounts:
    """`scope_keys`: the token, the address and the relay, told apart."""

    def test_all_three_are_named_when_there_is_a_credential(self):
        assert scope_keys("secret", "198.51.100.4") == {
            "per_token": "token:secret",
            "per_address": "addr:198.51.100.4",
            "total": "total",
        }

    def test_without_one_the_token_scope_matches_nothing(self):
        """None rather than a fallback to the address: with auth off there is no
        credential, so the scope is skipped rather than quietly turned into a
        second address limit with different numbers."""
        assert scope_keys(None, "198.51.100.4")["per_token"] is None

    def test_the_keys_are_prefixed_apart_so_one_table_can_hold_them(self):
        """A credential and an address spelled the same must be two rows: they
        share the in-flight counter."""
        keys = scope_keys("198.51.100.4", "198.51.100.4")
        assert keys["per_token"] != keys["per_address"]

    def test_a_credential_is_the_caller_wherever_it_asks_from(self):
        """Two machines sharing one token are one caller and share one
        allowance, which an address-only key could not express."""
        counter = InflightCounter({})
        counter.acquire(scope_keys("secret", "198.51.100.4")["per_token"], 1)
        assert counter.acquire(scope_keys("secret", "203.0.113.9")["per_token"], 1) is False

    def test_and_the_relay_counts_everybody_against_one_key(self):
        keys = [scope_keys(f"tok-{index}", f"10.0.0.{index}")["total"] for index in range(3)]
        assert keys == ["total"] * 3


class TestAdmissionIsAllOrNothing:
    """Every scope is asked before any is charged, so a refusal costs nothing."""

    def test_a_refusal_by_the_total_leaves_the_token_bucket_unspent(self):
        """Without this, a caller refused by the relay's own limit still had its
        own bucket drained, and got "I was refused, and now I am rate limited
        too" with no way to see why."""
        limits = limits_of(
            per_token=ScopeLimits(requests=10, period="1s"),
            total=ScopeLimits(requests=1, period="1s"),
        )
        limiters, counter = build_limiters(limits), InflightCounter({})
        keys = scope_keys("tok", "10.0.0.1")

        assert admit(limiters, limits, counter, keys, NOW)[0] is None
        assert limiters["per_token"].buckets["token:tok"].tokens == 9.0

        refusal, unused_release = admit(limiters, limits, counter, keys, NOW)
        assert (refusal.scope, refusal.kind) == ("total", "rate")
        assert limiters["per_token"].buckets["token:tok"].tokens == 9.0

    def test_a_refusal_by_the_total_cap_gives_back_the_token_slot(self):
        limits = limits_of(
            per_token=ScopeLimits(requests=4), total=ScopeLimits(requests=1)
        )
        limiters, counter = build_limiters(limits), InflightCounter({})
        first = scope_keys("tok-a", "10.0.0.1")
        second = scope_keys("tok-b", "10.0.0.2")

        assert admit(limiters, limits, counter, first, NOW)[0] is None
        refusal, unused_release = admit(limiters, limits, counter, second, NOW)
        assert (refusal.scope, refusal.kind) == ("total", "concurrent")
        assert counter.counts == {"token:tok-a": 1, "total": 1}

    def test_a_slot_refusal_leaves_every_bucket_unspent_too(self):
        """The rates are asked first and charged last, so a request the caps
        turn away has not touched a single bucket."""
        limits = limits_of(
            per_address=ScopeLimits(requests=1),
            total=ScopeLimits(requests=10, period="1s"),
        )
        limiters, counter = build_limiters(limits), InflightCounter({})
        keys = scope_keys(None, "10.0.0.1")

        admit(limiters, limits, counter, keys, NOW)
        refusal, unused_release = admit(limiters, limits, counter, keys, NOW)
        assert refusal.kind == "concurrent"
        assert limiters["total"].buckets["total"].tokens == 9.0

    def test_an_admitted_request_is_charged_to_every_configured_scope(self):
        """The scopes are ceilings, not alternatives: passing three of them is
        being counted by three of them."""
        limits = limits_of(
            per_token=ScopeLimits(requests=10, period="1s"),
            per_address=ScopeLimits(requests=10, period="1s"),
            total=ScopeLimits(requests=10, period="1s"),
        )
        limiters, counter = build_limiters(limits), InflightCounter({})
        admit(limiters, limits, counter, scope_keys("tok", "10.0.0.1"), NOW)
        assert counter.counts == {"token:tok": 1, "addr:10.0.0.1": 1, "total": 1}
        assert [limiters[scope].buckets[key].tokens for scope, key in (
            ("per_token", "token:tok"),
            ("per_address", "addr:10.0.0.1"),
            ("total", "total"),
        )] == [9.0, 9.0, 9.0]

    def test_and_one_release_gives_the_whole_set_back(self):
        limits = limits_of(
            per_token=ScopeLimits(requests=3),
            per_address=ScopeLimits(requests=3),
            total=ScopeLimits(requests=3),
        )
        limiters, counter = build_limiters(limits), InflightCounter({})
        unused_refusal, release = admit(
            limiters, limits, counter, scope_keys("tok", "10.0.0.1"), NOW
        )
        release()
        release()
        assert counter.counts == {}

    def test_the_release_set_is_fixed_when_the_slots_are_taken(self):
        """Recomputed on the way out instead, a scope turned off by a reload
        between the two would leave its slot held for the life of the process."""
        limits = limits_of(total=ScopeLimits(requests=3))
        limiters, counter = build_limiters(limits), InflightCounter({})
        unused_refusal, release = admit(
            limiters, limits, counter, scope_keys(None, "10.0.0.1"), NOW
        )
        limits["total"] = ScopeLimits()
        release()
        assert counter.counts == {}


class TestWhichLimitTheRefusalNames:
    """Narrowest first, because that is the number the operator raises first."""

    def test_the_token_is_named_when_all_three_would_refuse(self):
        """Being told the relay is full while you personally are the reason is
        the wrong answer even though it is true."""
        limits = limits_of(
            **{scope: ScopeLimits(requests=1, period="1s") for scope in SCOPES}
        )
        limiters, counter = build_limiters(limits), InflightCounter({})
        keys = scope_keys("tok", "10.0.0.1")
        admit(limiters, limits, counter, keys, NOW)
        assert admit(limiters, limits, counter, keys, NOW)[0].scope == "per_token"

    def test_the_address_is_named_when_there_is_no_credential(self):
        limits = limits_of(
            per_token=ScopeLimits(requests=1, period="1s"),
            per_address=ScopeLimits(requests=1, period="1s"),
            total=ScopeLimits(requests=1, period="1s"),
        )
        limiters, counter = build_limiters(limits), InflightCounter({})
        keys = scope_keys(None, "10.0.0.1")
        admit(limiters, limits, counter, keys, NOW)
        assert admit(limiters, limits, counter, keys, NOW)[0].scope == "per_address"

    def test_the_wait_comes_from_the_scope_that_refused(self):
        """Not from the tightest one configured: a Retry-After computed off a
        different scope's rate is a number the caller cannot act on."""
        limits = limits_of(
            per_token=ScopeLimits(requests=1, period="2s"),
            total=ScopeLimits(requests=10, period="1s"),
        )
        limiters, counter = build_limiters(limits), InflightCounter({})
        keys = scope_keys("tok", "10.0.0.1")
        admit(limiters, limits, counter, keys, NOW)
        refusal, unused_release = admit(limiters, limits, counter, keys, NOW)
        assert (refusal.scope, round(refusal.wait, 3)) == ("per_token", 2.0)

    def test_a_slot_refusal_names_no_wait_at_all(self):
        """A slot frees when a model finishes, and with no read timeout the
        relay cannot know when that is."""
        limits = limits_of(total=ScopeLimits(requests=1))
        limiters, counter = build_limiters(limits), InflightCounter({})
        keys = scope_keys(None, "10.0.0.1")
        admit(limiters, limits, counter, keys, NOW)
        assert admit(limiters, limits, counter, keys, NOW)[0].wait == 0.0


class TestWithAuthOff:
    """The token scope is skipped, not refused, and the other two do the work."""

    def test_a_configured_token_scope_admits_everybody(self):
        limits = limits_of(per_token=ScopeLimits(requests=1, period="1s"))
        limiters, counter = build_limiters(limits), InflightCounter({})
        keys = scope_keys(None, "10.0.0.1")
        assert [admit(limiters, limits, counter, keys, NOW)[0] for _ in range(3)] == [None] * 3

    def test_and_holds_nothing_against_anyone(self):
        """No bucket created and no slot held, so turning auth on later starts
        every caller full rather than mid-way through an allowance nobody spent."""
        limits = limits_of(per_token=ScopeLimits(requests=1, period="1s"))
        limiters, counter = build_limiters(limits), InflightCounter({})
        admit(limiters, limits, counter, scope_keys(None, "10.0.0.1"), NOW)
        assert limiters["per_token"].buckets == {}
        assert counter.counts == {}

    def test_while_the_address_scope_still_refuses(self):
        limits = limits_of(per_address=ScopeLimits(requests=1, period="1s"))
        limiters, counter = build_limiters(limits), InflightCounter({})
        keys = scope_keys(None, "10.0.0.1")
        admit(limiters, limits, counter, keys, NOW)
        assert admit(limiters, limits, counter, keys, NOW)[0].scope == "per_address"


class TestEverythingOff:
    """The default, and what an install that never asked for a limit carries."""

    def test_no_limiters_are_built(self):
        assert build_limiters(default_limits()) == {scope: None for scope in SCOPES}

    def test_and_nothing_is_ever_refused_or_held(self):
        limits = default_limits()
        limiters, counter = build_limiters(limits), InflightCounter({})
        keys = scope_keys("tok", "10.0.0.1")
        assert [admit(limiters, limits, counter, keys, NOW)[0] for _ in range(50)] == [None] * 50
        assert counter.counts == {}


class TestSayingWhatALimitIs:
    """The words a log line, a refusal and `status` all use."""

    def test_a_scope_that_asks_for_nothing_is_off(self):
        assert describe_scope(ScopeLimits()) == "off"
        assert describe_rate(ScopeLimits()) == "off"

    def test_a_cap_on_its_own_names_only_the_cap(self):
        assert describe_scope(ScopeLimits(requests=6)) == "6 at once"
        assert describe_rate(ScopeLimits(requests=6)) == "off"

    def test_a_period_names_both_halves(self):
        """The same number doing two jobs, so both are said. A line that named
        one would leave the other to be found out by being refused by it."""
        assert describe_scope(ScopeLimits(requests=10, period="30m")) == "10 per 30m, 10 at once"
        assert describe_rate(ScopeLimits(requests=10, period="30m")) == "10 per 30m"

    def test_and_a_period_of_zero_is_no_period(self):
        assert describe_scope(ScopeLimits(requests=6, period="0s")) == "6 at once"
