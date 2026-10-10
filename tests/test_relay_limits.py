#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The limits end to end: refusals a caller sees, and slots given back on every way out."""

import logging
import re

import anyio
import httpx
import pytest

# Local imports
from tests.conftest import (
    FIRST_CHUNK,
    GATED_CHUNKS,
    OTHER_TOKEN,
    TESTCLIENT_KEY,
    TOKEN,
    answer_in_flight,
    bearer,
    config_limits,
    fails_after_the_first_chunk,
    relay_with,
    wait_until,
)

# A response header a provider can legally send and starlette cannot re-emit:
# the UTF-8 encoding of U+2713, which httpx decodes to a str holding a codepoint
# outside latin-1. Given as raw bytes because that is what arrives off a socket,
# and because httpx itself refuses to build a response from a str header it
# cannot encode, which would fail in the test rather than in the relay.
UNENCODABLE_HEADER = (b"x-provider-note", b"\xe2\x9c\x93")


@pytest.fixture
def capped_by_token(tmp_path, monkeypatch, recorder):
    """The same cap on the credential rather than the address."""
    yield from relay_with(
        tmp_path, monkeypatch, recorder,
        config_limits(per_token="concurrent = 1"), auth_enabled=True,
    )


@pytest.fixture
def capped_in_total(tmp_path, monkeypatch, recorder):
    """One answer at a time for the whole relay, whoever is asking."""
    yield from relay_with(
        tmp_path, monkeypatch, recorder,
        config_limits(total="concurrent = 1"), auth_enabled=True,
    )


@pytest.fixture
def limited_by_token(tmp_path, monkeypatch, recorder):
    """The same rate on the credential rather than the address."""
    yield from relay_with(
        tmp_path, monkeypatch, recorder,
        config_limits(per_token='concurrent = 3\nrate = "3/1h"'), auth_enabled=True,
    )


@pytest.fixture
def limited_in_total(tmp_path, monkeypatch, recorder):
    """The same rate for the whole relay, whoever is asking."""
    yield from relay_with(
        tmp_path, monkeypatch, recorder,
        config_limits(total='concurrent = 3\nrate = "3/1h"'), auth_enabled=True,
    )


@pytest.fixture
def limited_everywhere(tmp_path, monkeypatch, recorder):
    """All three scopes set, the narrowest tightest, so a test can watch which
    one a refusal names and what a refusal costs the scopes that said yes."""
    yield from relay_with(
        tmp_path, monkeypatch, recorder,
        config_limits(
            per_token='concurrent = 1\nrate = "1/1h"',
            per_address='concurrent = 4\nrate = "4/1h"',
            total='concurrent = 6\nrate = "6/1h"',
        ),
        auth_enabled=True,
    )


class TestHowOftenACallerMayAsk:
    """`period`, which makes `requests` a how-often as well, in each scope."""

    def test_the_allowance_gets_through_and_the_next_one_does_not(self, limited):
        codes = [limited.get("/api/tags").status_code for unused in range(4)]
        assert codes == [200, 200, 200, 429]

    def test_the_refusal_names_the_scope_and_the_number(self, limited):
        """`429` on its own leaves an operator guessing which of six numbers to
        raise, and the message has to be distinguishable from a provider's."""
        for unused in range(3):
            limited.get("/api/tags")
        error = limited.get("/api/tags").json()["error"]
        assert error == (
            "lmrelay: rate limit exceeded for your address: 3/1h ([limits.per_address])"
        )

    def test_the_token_scope_says_it_is_your_token(self, limited_by_token):
        for unused in range(3):
            limited_by_token.get("/api/tags", headers=bearer(TOKEN))
        error = limited_by_token.get("/api/tags", headers=bearer(TOKEN)).json()["error"]
        assert "your token" in error and "[limits.per_token]" in error

    def test_and_the_total_says_it_is_the_relay(self, limited_in_total):
        """Not "yours": at this scope the allowance being spent is everybody's,
        and a caller told to slow down when they have sent one request would go
        looking for a limit of their own that is not set."""
        for unused in range(3):
            limited_in_total.get("/api/tags", headers=bearer(TOKEN))
        error = limited_in_total.get("/api/tags", headers=bearer(OTHER_TOKEN)).json()["error"]
        assert "the relay's rate limit" in error and "[limits.total]" in error

    def test_it_quotes_the_period_the_operator_wrote(
        self, tmp_path, monkeypatch, recorder
    ):
        """`60s` and not `1m`. The refusal is the one place a caller ever sees
        the limit, and quoting it back in a spelling their operator never used
        sends them asking about a setting nobody can find."""
        for client in relay_with(
            tmp_path, monkeypatch, recorder,
            config_limits(per_address='concurrent = 3\nrate = "3/60s"'),
        ):
            assert [client.get("/api/tags").status_code for unused in range(3)] == [200] * 3
            refusal = client.get("/api/tags")
            assert refusal.status_code == 429
            assert "3/60s" in refusal.json()["error"]

    def test_the_refusal_carries_a_retry_after_the_caller_can_act_on(self, limited):
        """Unlike a slot refusal, this one is computable: it is the time until
        one token has refilled. Whole seconds, because the header takes no
        fraction, and rounded up because rounding down invites a retry that is
        refused again: three an hour refill one every 1200 seconds."""
        for unused in range(3):
            limited.get("/api/tags")
        assert limited.get("/api/tags").headers["retry-after"] == "1200"

    def test_a_refused_request_reaches_no_upstream(self, limited, recorder):
        """The limit exists to keep work off the upstream, so nothing is
        forwarded before the whole admission decision has been made."""
        for unused in range(3):
            limited.get("/api/tags")
        assert limited.get("/api/tags").status_code == 429
        assert len(recorder.requests) == 3

    def test_health_is_exempt_by_being_a_different_route(self, limited):
        """It touches no upstream, and a liveness probe that trips the limit
        would report the relay dead for being polled. Structural now: the
        decision lives in the relay route, so /healthz never reaches it and
        needs no path-and-method exemption of its own."""
        for unused in range(3):
            limited.get("/api/tags")
        assert limited.get("/api/tags").status_code == 429
        assert [limited.get("/healthz").status_code for unused in range(5)] == [200] * 5

    def test_callers_do_not_share_an_allowance_at_the_token_scope(self, limited_by_token):
        """One caller spending its burst must not refuse everybody else, which
        is the whole argument for having a scope narrower than the total."""
        for unused in range(3):
            limited_by_token.get("/api/tags", headers=bearer(TOKEN))
        assert limited_by_token.get("/api/tags", headers=bearer(TOKEN)).status_code == 429
        assert limited_by_token.get(
            "/api/tags", headers=bearer(OTHER_TOKEN)
        ).status_code == 200

    def test_but_they_do_share_the_total(self, limited_in_total):
        """Which is the point of it: ten callers each inside their own limit
        still arrive together, and only this scope sees that."""
        for unused in range(3):
            limited_in_total.get("/api/tags", headers=bearer(TOKEN))
        assert limited_in_total.get(
            "/api/tags", headers=bearer(OTHER_TOKEN)
        ).status_code == 429

    def test_a_guessed_credential_does_not_spend_the_real_callers_allowance(
        self, limited_by_token
    ):
        """The reason the limits are charged after the credential is checked.
        Charged before, anyone could exhaust a token's allowance by guessing at
        it, which turns a rate limit into a way to deny service to its owner."""
        for unused in range(10):
            assert limited_by_token.get(
                "/api/tags", headers=bearer("not-a-token")
            ).status_code == 401
        codes = [
            limited_by_token.get("/api/tags", headers=bearer(TOKEN)).status_code
            for unused in range(4)
        ]
        assert codes == [200, 200, 200, 429]

    def test_the_refusal_is_logged_as_one_line_naming_its_upstream(self, limited, caplog):
        """It used to say `-> -`, because the limit was charged before an
        upstream had been selected. One decision made in the route means every
        refusal can say which upstream it was headed for."""
        for unused in range(3):
            limited.get("/api/tags")
        with caplog.at_level(logging.INFO):
            limited.get("/api/tags")
        assert "GET /api/tags -> ollama: 429 (rate, per_address)" in caplog.text

    def test_a_relay_that_never_asked_for_a_limit_keeps_no_table_at_all(self, authed):
        """The default. A scope with its rate off builds no limiter, so an
        install predating these keys carries none of their cost."""
        from lmrelay.app import app

        assert set(app.state.limiters.values()) == {None}
        assert [authed.get("/api/tags").status_code for unused in range(20)] == [200] * 20


class TestPassingEveryScopeOrNone:
    """The scopes apply together, and a refused request costs nothing anywhere."""

    def test_a_request_is_charged_to_every_scope_that_is_set(
        self, limited_everywhere, recorder
    ):
        """They are ceilings, not alternatives. Passing three of them is being
        counted by three of them, which is what makes `total` a protection
        rather than something a generous `per_token` could win against."""
        from lmrelay.app import app

        with answer_in_flight(limited_everywhere, recorder, headers=bearer(TOKEN)):
            assert set(app.state.inflight.counts) == {
                f"token:{TOKEN}", TESTCLIENT_KEY, "total"
            }

    def test_a_refusal_by_the_narrow_scope_leaves_the_wide_ones_unspent(
        self, limited_everywhere
    ):
        """Without all-or-nothing charging, a caller refused by one scope still
        drained the others on the way past, and an operator got "I was refused,
        and now I am rate limited too" with no way to see why.

        The address scope allows 4 an hour and is asked six times by one token:
        one served, five refused by the token scope. Charged for the refusals,
        it would be spent, and the other caller at the same address refused."""
        limited_everywhere.get("/api/tags", headers=bearer(TOKEN))
        for unused in range(5):
            assert limited_everywhere.get(
                "/api/tags", headers=bearer(TOKEN)
            ).status_code == 429
        assert limited_everywhere.get(
            "/api/tags", headers=bearer(OTHER_TOKEN)
        ).status_code == 200

    def test_the_narrowest_scope_is_the_one_named(self, limited_everywhere):
        """Being told the relay is full while you personally are the reason is
        the wrong answer even though it is true, and the total is not the number
        an operator would raise first."""
        limited_everywhere.get("/api/tags", headers=bearer(TOKEN))
        error = limited_everywhere.get("/api/tags", headers=bearer(TOKEN)).json()["error"]
        assert "[limits.per_token]" in error


class TestHowManyAnswersMayBeOpenAtOnce:
    """`concurrent`, in each of the three scopes.

    A different measure from the rate, which is why it is a third key: a
    generation runs for as long as the model takes, so a rate an operator would
    call generous still lets one caller occupy the machine for minutes.
    """

    def test_a_second_request_while_the_first_is_still_answering_is_refused(
        self, capped, recorder
    ):
        with answer_in_flight(capped, recorder):
            assert capped.post("/api/tags").status_code == 429

    def test_the_refusal_says_who_refused_and_what_the_limit_was(self, capped, recorder):
        """Distinguishable from a provider's own 429, and from the three rate
        refusals that answer 429 here: an operator has to know which number to
        change."""
        with answer_in_flight(capped, recorder):
            error = capped.post("/api/tags").json()["error"]
        assert error == (
            "lmrelay: your address already has 1 requests in flight "
            "([limits.per_address]); one of yours must finish first"
        )

    def test_the_token_scope_says_it_is_your_token(self, capped_by_token, recorder):
        with answer_in_flight(capped_by_token, recorder, headers=bearer(TOKEN)):
            error = capped_by_token.post("/api/tags", headers=bearer(TOKEN)).json()["error"]
        assert error == (
            "lmrelay: your token already has 1 requests in flight "
            "([limits.per_token]); one of yours must finish first"
        )

    def test_the_total_asks_for_one_of_theirs_rather_than_one_of_yours(
        self, capped_in_total, recorder
    ):
        """At this scope the request that has to end may be anybody's, and
        telling a caller to wait for something of their own that does not exist
        is a refusal they cannot act on."""
        with answer_in_flight(capped_in_total, recorder, headers=bearer(TOKEN)):
            error = capped_in_total.post("/api/tags", headers=bearer(OTHER_TOKEN)).json()["error"]
        assert error == (
            "lmrelay: the relay is already carrying 1 requests "
            "([limits.total]); one of them must finish first"
        )

    def test_and_it_names_no_retry_after(self, capped, recorder):
        """A rate refusal carries one because it can compute it from the refill.
        A slot frees when a model finishes answering somebody else, and with no
        read timeout that may be minutes away: a guessed number would be a lie,
        and a client obeying it would retry into the same refusal."""
        with answer_in_flight(capped, recorder):
            assert "retry-after" not in capped.post("/api/tags").headers

    def test_the_refused_request_costs_no_upstream_call(self, capped, recorder):
        """The cap exists to keep work off the upstream. One that forwarded
        first and refused afterwards would have spent what it was protecting."""
        with answer_in_flight(capped, recorder):
            capped.post("/api/tags")
            assert len(recorder.requests) == 1

    def test_the_answer_already_streaming_is_never_interrupted(self, capped, recorder):
        """Admission is the only lever. Reclaiming a slot by cutting an answer
        short would break the promise the whole relay is built around."""
        with answer_in_flight(capped, recorder) as held:
            capped.post("/api/tags")
        assert held.answer.result(timeout=10).content == b"".join(GATED_CHUNKS)

    def test_the_slot_is_held_until_the_body_ends_and_then_given_back(self, capped, recorder):
        """For a streamed answer the handler returns as soon as the upstream's
        headers arrive, before a byte of body has been written; a release moved
        there would free the slot while the answer was still running, and the
        cap would bound nothing."""
        with answer_in_flight(capped, recorder):
            assert capped.post("/api/tags").status_code == 429
        assert capped.post("/api/tags").status_code == 200

    def test_another_caller_is_not_held_up_by_a_scope_of_their_own(
        self, capped_by_token, recorder
    ):
        """One caller filling their own scope must not be an outage for
        everybody else. Without a scope like this one, the total is first come
        first served and one client with fifty threads owns all of it."""
        with answer_in_flight(capped_by_token, recorder, headers=bearer(TOKEN)) as held:
            other = held.pool.submit(
                capped_by_token.post, "/api/tags", headers=bearer(OTHER_TOKEN)
            )
            wait_until(
                lambda: len(recorder.requests) == 2,
                "the second caller never reached the upstream",
            )
        assert other.result(timeout=10).status_code == 200

    def test_but_the_total_holds_them_both(self, capped_in_total, recorder):
        """Which is the one that protects the upstream: a per-caller cap does
        not, because ten callers each inside their own still arrive together."""
        with answer_in_flight(capped_in_total, recorder, headers=bearer(TOKEN)):
            assert capped_in_total.post(
                "/api/tags", headers=bearer(OTHER_TOKEN)
            ).status_code == 429

    def test_a_refusal_by_the_total_gives_back_the_slot_it_had_already_taken(
        self, tmp_path, monkeypatch, recorder
    ):
        """The all-or-nothing property for slots. The token scope is taken
        first; when the total then refuses, the caller must not be left holding
        a slot in a scope that admitted them."""
        from lmrelay.app import app

        body = config_limits(per_token="concurrent = 4", total="concurrent = 1")
        for client in relay_with(tmp_path, monkeypatch, recorder, body, auth_enabled=True):
            with answer_in_flight(client, recorder, headers=bearer(TOKEN)):
                assert client.post(
                    "/api/tags", headers=bearer(OTHER_TOKEN)
                ).status_code == 429
                assert app.state.inflight.counts == {f"token:{TOKEN}": 1, "total": 1}

    def test_the_log_line_says_which_scope_refused(self, capped, recorder, caplog):
        """Six limits answer 429 and only three carry a Retry-After, so a line
        saying only that a 429 went out leaves an operator sizing the wrong
        number. One line, not two: the name stands where the elapsed time would,
        which for a request that was never forwarded is only the cost of
        refusing it."""
        with caplog.at_level(logging.INFO), answer_in_flight(capped, recorder):
            capped.post("/api/tags")
        # The relay's own lines only: httpx logs the same exchange from the
        # client side of the test, which is not the log an operator reads.
        refusals = [
            line for line in caplog.text.splitlines()
            if "429" in line and "lmrelay.app" in line
        ]
        assert len(refusals) == 1, refusals
        assert "-> ollama: 429 (concurrent, per_address)" in refusals[0]
        assert "WARNING" in refusals[0], "a refusal is not an ordinary served request"

    def test_and_a_served_request_still_says_how_long_it_took(self, capped, recorder, caplog):
        """The other half of that substitution: it must not have taken the time
        to first byte off the requests that were served."""
        with caplog.at_level(logging.INFO):
            capped.post("/api/tags")
        assert re.search(r"-> ollama: 200 \(\d+\.\d\ds\)", caplog.text)

    def test_a_relay_that_never_asked_for_a_cap_admits_them_all(self, authed, recorder):
        """The default, and what every install that predates these keys gets."""
        from lmrelay.app import app

        assert app.state.config.limits["total"].concurrent == 0
        with answer_in_flight(authed, recorder) as held:
            second = held.pool.submit(authed.post, "/api/tags")
            wait_until(
                lambda: len(recorder.requests) == 2, "the second request was not forwarded"
            )
        assert second.result(timeout=10).status_code == 200
        assert app.state.inflight.counts == {}


class TestGivingTheSlotBackWhenThereIsNoAnswer:
    """Every way out that never reaches a body. A slot leaked on one of these
    is not recovered by anything: it locks that caller out until the process
    restarts, which is worse than the failure that leaked it."""

    def test_a_dialect_refusal_costs_no_slot(self, capped, recorder):
        """It is refused before the upstream is touched, so it never took one.
        Counting it would let a mistyped path lock a caller out."""
        assert capped.post("/anthropic/api/chat", json={}).status_code == 400
        assert capped.post("/api/tags").status_code == 200

    def test_and_costs_no_rate_token_either(self, limited, recorder):
        """It used to spend one, because the rate was charged in the middleware
        and this refusal happens in the route. One decision made in one place
        leaves one rule with no exception: nothing forwarded, nothing charged.

        The cost, stated so it is a choice: a client looping against a
        wrong-dialect path is no longer rate limited. It costs microseconds per
        400 and cannot touch a model, and fail2ban is the answer if it matters.
        """
        for unused in range(10):
            assert limited.post("/anthropic/api/chat", json={}).status_code == 400
        assert [limited.get("/api/tags").status_code for unused in range(3)] == [200] * 3

    def test_an_unreachable_upstream_gives_it_back(self, capped, recorder):
        """An Ollama that was down for one request must not leave the caller
        unable to send another once it is up."""
        recorder.raises = httpx.ConnectError("nothing listening")
        assert capped.post("/api/chat", json={}).status_code == 502
        recorder.raises = None
        assert capped.post("/api/chat", json={}).status_code == 200

    def test_any_other_transport_failure_gives_it_back_too(self, capped, recorder):
        """The second 502 arm. Both of them return rather than raise, so
        neither is covered by the generator that releases at the end of a body."""
        recorder.raises = httpx.ReadError("connection reset")
        assert capped.post("/api/chat", json={}).status_code == 502
        recorder.raises = None
        assert capped.post("/api/chat", json={}).status_code == 200

    def test_a_stream_that_breaks_part_way_through_gives_it_back(self, capped, recorder):
        """Here the body did start, so the release is the generator's `finally`
        rather than an error arm: the exception leaves through it."""
        recorder.chunks = fails_after_the_first_chunk()
        with pytest.raises(httpx.ReadError):
            capped.post("/api/generate", json={"stream": True})
        recorder.chunks = None
        assert capped.post("/api/generate", json={}).status_code == 200

    def test_a_response_the_relay_cannot_build_gives_it_back(self, capped, recorder):
        """The one path out that is not an error arm and not the body generator.

        httpx decodes a response header as UTF-8 and starlette re-encodes it as
        latin-1, so a single non-ASCII header from a provider raises where the
        StreamingResponse is constructed. That sits after the slot is taken and
        after every `except` that releases it, and neither of the two things
        that would otherwise give the slot back is reachable: nothing iterates
        the generator whose `finally` releases, and the BackgroundTask that
        closes the connection is part of the object that failed to be built.
        """
        from lmrelay.app import app

        recorder.headers = [(b"content-type", b"application/json"), UNENCODABLE_HEADER]
        with pytest.raises(UnicodeEncodeError):
            capped.post("/api/generate", json={})
        assert app.state.inflight.counts == {}

    def test_and_that_caller_is_not_locked_out_of_its_own_cap(self, capped, recorder):
        """What the assertion above stands for. Held, the slot is unrecoverable:
        `max_concurrent` such answers and that caller is refused for the life of
        the process, with nothing in the log naming the cap it is refused by."""
        recorder.headers = [UNENCODABLE_HEADER]
        with pytest.raises(UnicodeEncodeError):
            capped.post("/api/generate", json={})
        recorder.headers = {"content-type": "application/json"}
        assert capped.post("/api/generate", json={}).status_code == 200

    def test_a_caller_that_hangs_up_part_way_through_gives_it_back(self):
        """Starlette closes the body generator when the connection goes, and the
        `finally` is what makes that a release rather than a leak.

        Driven directly because the in-process client cannot hang up: it settles
        a response before handing it back. Measured over a real socket while
        this was written, with the same result: the slot came back a moment
        after the client closed, and the next request from that caller was
        admitted.

        Every scope the admission took goes back together, from that one
        `finally`, because the release covers the whole set.
        """
        from lmrelay.app import relay_body
        from lmrelay.ratelimit import InflightCounter, release_all

        async def produce():
            yield FIRST_CHUNK
            yield b'{"done":true}\n'

        held = (TESTCLIENT_KEY, "total")
        counter = InflightCounter(dict.fromkeys(held, 1))
        body = relay_body(
            httpx.Response(200, content=produce()), release_all(counter, held)
        )

        async def hang_up_after_the_first_chunk():
            assert await anext(body) == FIRST_CHUNK
            # Still held: the caller has part of an answer and is reading it.
            assert counter.counts == dict.fromkeys(held, 1)
            await body.aclose()

        anyio.run(hang_up_after_the_first_chunk)
        assert counter.counts == {}


def main():
    pass


if __name__ == "__main__":
    main()
