#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Selection, dialect checks and header filtering, without a server."""

import pytest

# Local imports
from lmrelay.config import RelayConfig, Upstream
from lmrelay.upstream import (
    build_upstream_headers,
    build_upstream_url,
    check_caller_token,
    check_dialect,
    filter_response_headers,
    has_request_body,
    select_upstream,
)

OLLAMA = Upstream(name="ollama", base_url="http://localhost:11434", dialect="ollama", headers={})
CLAUDE = Upstream(
    name="anthropic", base_url="https://api.anthropic.com", dialect="anthropic",
    headers={"x-api-key": "provider-secret"},
)
GPT = Upstream(name="openai", base_url="https://api.openai.com", dialect="openai", headers={})

# The configured set, as load_config hands it over: one entry is still a list.
TOKENS = ("secret",)


def make_config(**overrides) -> RelayConfig:
    settings = {
        "host": "127.0.0.1", "port": 11435, "default_upstream": "ollama",
        "connect_timeout": 10, "log_level": "INFO",
        "auth_enabled": True, "auth_tokens": TOKENS,
        "upstreams": {"ollama": OLLAMA, "anthropic": CLAUDE, "openai": GPT},
        "config_path": None, "state_path": None,
    }
    settings.update(overrides)
    return RelayConfig(**settings)


class TestTheCallerCredential:
    """Who is allowed to use the relay at all."""

    def test_a_bearer_token_is_accepted(self):
        assert check_caller_token({"authorization": "Bearer secret"}, TOKENS)

    def test_and_so_is_x_api_key(self):
        """An Anthropic SDK pointed here sends x-api-key and cannot easily be
        made to send a bearer. Accepting only one carrier would leave half the
        callers unable to authenticate at all."""
        assert check_caller_token({"x-api-key": "secret"}, TOKENS)

    def test_the_scheme_word_is_not_case_sensitive(self):
        assert check_caller_token({"authorization": "bearer secret"}, TOKENS)

    def test_but_the_token_itself_is(self):
        assert not check_caller_token({"authorization": "Bearer SECRET"}, TOKENS)

    def test_a_wrong_token_is_refused(self):
        assert not check_caller_token({"authorization": "Bearer wrong"}, TOKENS)

    def test_a_token_that_merely_starts_right_is_refused(self):
        """A prefix must not pass: the comparison is over the whole value."""
        assert not check_caller_token({"authorization": "Bearer sec"}, TOKENS)

    def test_a_longer_token_carrying_the_right_one_is_refused(self):
        assert not check_caller_token({"authorization": "Bearer secretmore"}, TOKENS)

    def test_no_credential_at_all_is_refused(self):
        assert not check_caller_token({}, TOKENS)

    def test_another_scheme_is_not_read_as_a_bearer(self):
        """Basic auth carrying the token as its blob must not be accepted, and
        an Authorization header that is present but unusable must not fall
        through to the x-api-key branch either."""
        assert not check_caller_token(
            {"authorization": "Basic secret", "x-api-key": "secret"}, TOKENS
        )

    def test_surrounding_space_is_ignored(self):
        assert check_caller_token({"authorization": "Bearer  secret "}, TOKENS)

    def test_an_empty_token_list_refuses_everyone(self):
        """Auth switched on with nothing to match means closed, not open. The
        open case is the switch being off, and it is decided before this."""
        assert not check_caller_token({"authorization": "Bearer secret"}, ())

    def test_any_token_in_the_list_is_accepted(self):
        """Several callers hold several tokens, and deleting one of them must
        not turn the rest away."""
        assert check_caller_token({"authorization": "Bearer second"}, ("first", "second"))
        assert check_caller_token({"x-api-key": "third"}, ("first", "second", "third"))

    def test_and_one_that_is_in_none_of_them_is_not(self):
        assert not check_caller_token({"authorization": "Bearer fourth"}, ("first", "second"))

    def test_a_credential_with_a_non_ascii_character_is_refused_not_a_crash(self):
        """hmac.compare_digest raises on a non-ASCII str, and Starlette decodes
        headers as latin-1, so one raw byte from an anonymous caller would turn
        every refusal into a 500 with a traceback in the log."""
        assert not check_caller_token({"authorization": "Bearer caf\xe9"}, TOKENS)
        assert not check_caller_token({"x-api-key": "caf\xe9"}, TOKENS)

    def test_a_stored_token_with_a_non_ascii_character_does_not_break_the_others(self):
        """Nothing validates what `token add` is given, and every token is
        compared before any result is read, so one such entry would refuse
        every other caller in the set rather than only itself."""
        assert check_caller_token({"authorization": "Bearer first"}, ("first", "cl\xe9-andre"))


class TestChoosingAnUpstream:
    """Which upstream a path goes to, and what path is forwarded."""

    def test_an_ollama_path_falls_through_to_the_default(self):
        """The whole point of the default: a client that has never heard of
        lmrelay keeps sending /api/chat to the port Ollama already uses."""
        upstream, path = select_upstream("/api/chat", make_config())
        assert upstream.name == "ollama"
        assert path == "/api/chat"

    def test_a_leading_segment_that_names_an_upstream_selects_it(self):
        upstream, path = select_upstream("/anthropic/v1/messages", make_config())
        assert upstream.name == "anthropic"
        assert path == "/v1/messages"

    def test_only_the_first_segment_is_consumed(self):
        upstream, path = select_upstream("/openai/v1/chat/completions", make_config())
        assert upstream.name == "openai"
        assert path == "/v1/chat/completions"

    def test_a_bare_upstream_name_forwards_the_root(self):
        upstream, path = select_upstream("/anthropic", make_config())
        assert upstream.name == "anthropic"
        assert path == "/"

    def test_a_segment_that_merely_contains_a_name_does_not_select_it(self):
        """Prefix matching here would send /anthropic-proxy/... to Anthropic and
        strip a segment the caller meant to keep."""
        upstream, path = select_upstream("/anthropic-proxy/v1/messages", make_config())
        assert upstream.name == "ollama"
        assert path == "/anthropic-proxy/v1/messages"

    def test_an_unknown_first_segment_goes_to_the_default_whole(self):
        upstream, path = select_upstream("/v1/chat/completions", make_config())
        assert upstream.name == "ollama"
        assert path == "/v1/chat/completions"

    def test_the_default_can_be_a_hosted_provider(self):
        """Nothing about the default is Ollama-specific: an operator with no
        local model points it at a provider and the same paths keep working."""
        upstream, path = select_upstream("/v1/messages", make_config(default_upstream="anthropic"))
        assert upstream.name == "anthropic"
        assert path == "/v1/messages"


class TestRefusingAPathTheUpstreamCannotHave:
    """Refusals lmrelay makes itself, because it does not translate dialects."""

    def test_ollama_refuses_nothing(self):
        """It serves both its own /api/* and an OpenAI-compatible /v1/*, so no
        path is certainly wrong."""
        assert check_dialect(OLLAMA, "/api/chat") is None
        assert check_dialect(OLLAMA, "/v1/chat/completions") is None
        assert check_dialect(OLLAMA, "/v1/messages") is None

    def test_an_ollama_path_at_a_hosted_provider_is_refused(self):
        refusal = check_dialect(CLAUDE, "/api/chat")
        assert refusal is not None
        # Named as lmrelay's own refusal: an unattributed 400 reads as the
        # provider's, which is the confusion this exists to prevent.
        assert refusal.startswith("lmrelay:")
        assert "anthropic" in refusal

    def test_an_anthropic_path_at_an_openai_upstream_is_refused(self):
        assert check_dialect(GPT, "/v1/messages") is not None

    def test_an_openai_path_at_an_anthropic_upstream_is_refused(self):
        assert check_dialect(CLAUDE, "/v1/chat/completions") is not None

    def test_a_path_in_neither_set_is_allowed_through(self):
        """A denylist, not an allowlist: an allowlist would start refusing
        legitimate traffic the day a provider ships a new endpoint."""
        assert check_dialect(CLAUDE, "/v1/models") is None
        assert check_dialect(GPT, "/v1/responses") is None

    def test_each_upstream_keeps_its_own_paths(self):
        assert check_dialect(CLAUDE, "/v1/messages") is None
        assert check_dialect(GPT, "/v1/chat/completions") is None


class TestWhatIsSentOnward:
    """The headers a provider receives, and the ones it must never receive."""

    def test_the_callers_bearer_never_reaches_an_upstream(self):
        """The caller's credential authenticates them to lmrelay and means
        nothing to a provider. Forwarding it would hand every caller's token to
        whichever upstream they happened to route through."""
        sent = build_upstream_headers({"authorization": "Bearer caller-token"}, OLLAMA)
        assert "authorization" not in {name.lower() for name in sent}

    def test_nor_does_the_callers_x_api_key(self):
        sent = build_upstream_headers({"x-api-key": "caller-token"}, OLLAMA)
        assert "x-api-key" not in {name.lower() for name in sent}

    def test_an_upstream_with_no_headers_receives_no_credential(self):
        """Ollama is the case: it wants nothing, and it must not be handed the
        caller's credential merely because one arrived."""
        sent = build_upstream_headers(
            {"authorization": "Bearer caller-token", "content-type": "application/json"}, OLLAMA
        )
        assert sent == {"content-type": "application/json"}

    def test_the_upstreams_own_credential_is_added(self):
        sent = build_upstream_headers({"authorization": "Bearer caller-token"}, CLAUDE)
        assert sent["x-api-key"] == "provider-secret"

    def test_and_it_wins_over_one_the_caller_sent_under_the_same_name(self):
        """Otherwise a caller could choose which key pays for their request by
        sending the header themselves."""
        sent = build_upstream_headers({"X-Api-Key": "caller-supplied"}, CLAUDE)
        assert sent["x-api-key"] == "provider-secret"
        assert "caller-supplied" not in sent.values()

    def test_a_configured_header_replaces_the_callers_whatever_its_case(self):
        upstream = Upstream(
            name="x", base_url="http://x", dialect="openai", headers={"X-Trace": "ours"}
        )
        sent = build_upstream_headers({"x-trace": "theirs"}, upstream)
        assert list(sent.values()).count("theirs") == 0
        assert sent["X-Trace"] == "ours"

    def test_hop_by_hop_headers_are_not_relayed(self):
        """They are meaningful only to the connection they arrived on."""
        sent = build_upstream_headers(
            {"connection": "keep-alive", "te": "trailers", "upgrade": "h2c"}, OLLAMA
        )
        assert sent == {}

    def test_the_host_header_is_dropped_so_httpx_can_recompute_it(self):
        sent = build_upstream_headers({"host": "127.0.0.1:11434"}, OLLAMA)
        assert sent == {}

    def test_everything_else_is_forwarded_untouched(self):
        sent = build_upstream_headers(
            {"content-type": "application/json", "user-agent": "ollama-python/0.3"}, OLLAMA
        )
        assert sent == {"content-type": "application/json", "user-agent": "ollama-python/0.3"}


class TestWhatComesBack:
    """The response headers, minus the ones that would break the answer."""

    def test_content_length_is_dropped(self):
        """Starlette re-frames the relayed body as chunked, so a forwarded
        content-length no longer matches: callers hang or truncate."""
        assert "content-length" not in filter_response_headers({"content-length": "42"})

    def test_hop_by_hop_headers_are_dropped(self):
        assert filter_response_headers({"connection": "close", "transfer-encoding": "chunked"}) == {}

    def test_content_type_survives(self):
        kept = filter_response_headers({"content-type": "text/event-stream"})
        assert kept == {"content-type": "text/event-stream"}


class TestBuildingTheUrl:
    """base_url and the forwarded path, joined."""

    def test_the_path_is_appended(self):
        assert build_upstream_url(OLLAMA, "/api/chat", "") == "http://localhost:11434/api/chat"

    def test_a_query_string_rides_along(self):
        url = build_upstream_url(OLLAMA, "/api/tags", "verbose=1")
        assert url == "http://localhost:11434/api/tags?verbose=1"

    def test_a_base_url_with_a_path_prefix_keeps_it(self):
        """What makes an Azure-style or subpath-hosted endpoint work with no code."""
        hosted = Upstream(name="h", base_url="https://host/openai", dialect="openai", headers={})
        assert build_upstream_url(hosted, "/v1/models", "") == "https://host/openai/v1/models"


class TestWhetherThereIsABody:
    """A GET with no body must not be re-framed as a chunked stream."""

    @pytest.mark.parametrize("headers", [{}, {"content-length": "0"}])
    def test_an_empty_request_has_no_body(self, headers):
        assert not has_request_body(headers)

    def test_a_sized_body_is_a_body(self):
        assert has_request_body({"content-length": "17"})

    def test_and_so_is_a_chunked_one(self):
        assert has_request_body({"transfer-encoding": "chunked"})
