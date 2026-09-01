#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Where the config is found, what the state adds, and every refusal."""

import logging

import pytest

# Local imports
from lmrelay import config as config_module
from lmrelay.config import (
    CONFIG_ENV_VAR,
    ConfigError,
    check_exposure,
    find_config_path,
    load_config,
)
from lmrelay.ratelimit import SCOPES, ScopeLimits, default_limits
from tests.conftest import write_state

MINIMAL = """
[upstream.ollama]
base_url = "http://127.0.0.1:11434"
dialect  = "ollama"
"""

LIMITS = """
[limits.total]
rate       = 20
burst      = 40
concurrent = 6
"""

OLLAMA_PROVIDER = {"base_url": "http://127.0.0.1:11434", "dialect": "ollama", "headers": {}}
OPENAI_PROVIDER = {
    "base_url": "https://from-state.invalid",
    "dialect": "openai",
    "headers": {"Authorization": "Bearer sk-from-state"},
}


def write(path, body: str):
    target = path / "lmrelay.toml"
    target.write_text(body, encoding="utf-8")
    return target


class TestFindingTheFile:
    """First hit wins, and a wrong pointer reports itself."""

    def test_the_environment_variable_wins(self, tmp_path, monkeypatch):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        chosen = write(elsewhere, MINIMAL)
        # A config in the working directory too, so the test distinguishes
        # "the env var was read" from "the first location happened to hit".
        write(tmp_path, MINIMAL)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(CONFIG_ENV_VAR, str(chosen))
        assert find_config_path() == chosen

    def test_then_the_working_directory(self, tmp_path, monkeypatch):
        monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
        monkeypatch.chdir(tmp_path)
        local = write(tmp_path, MINIMAL)
        assert find_config_path() == local

    def test_then_the_home_directory(self, tmp_path, monkeypatch):
        monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
        monkeypatch.chdir(tmp_path)
        home_config = tmp_path / "home" / ".lmrelay" / "lmrelay.toml"
        home_config.parent.mkdir(parents=True)
        home_config.write_text(MINIMAL, encoding="utf-8")
        monkeypatch.setattr(config_module, "HOME_CONFIG_PATH", home_config)
        assert find_config_path() == home_config

    def test_a_pointer_at_nothing_is_returned_rather_than_skipped(self, tmp_path, monkeypatch):
        """A wrong LMRELAY_CONFIG must report itself, not silently fall through
        to an unrelated file that happens to be in the working directory."""
        monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "absent.toml"))
        monkeypatch.chdir(tmp_path)
        write(tmp_path, MINIMAL)
        assert find_config_path() == tmp_path / "absent.toml"

    def test_and_the_error_names_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "absent.toml"))
        with pytest.raises(ConfigError) as raised:
            load_config()
        assert "absent.toml" in str(raised.value)

    def test_no_config_anywhere_is_refused_with_both_places_named(self, tmp_path, monkeypatch):
        """It cannot invent an upstream, and starting empty would produce 404s
        the operator would have to debug."""
        monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config_module, "HOME_CONFIG_PATH", tmp_path / "nohome.toml")
        with pytest.raises(ConfigError) as raised:
            load_config()
        message = str(raised.value)
        assert "lmrelay.toml" in message and "lmrelay init" in message


class TestRefusingABrokenConfig:
    """Each refusal names the setting, so it can be acted on without a bisect."""

    def test_a_file_that_is_not_toml(self, tmp_path):
        target = write(tmp_path, "this is not = = toml")
        with pytest.raises(ConfigError) as raised:
            load_config(target)
        assert str(target) in str(raised.value)

    def test_no_upstreams_at_all(self, tmp_path):
        target = write(tmp_path, '[server]\nhost = "127.0.0.1"\n')
        with pytest.raises(ConfigError, match=r"\[upstream"):
            load_config(target)

    def test_a_default_upstream_that_does_not_exist(self, tmp_path):
        target = write(tmp_path, MINIMAL + '\n[server]\ndefault_upstream = "typo"\n')
        with pytest.raises(ConfigError) as raised:
            load_config(target)
        # The known names are listed, because the mistake is almost always a typo.
        assert "typo" in str(raised.value) and "ollama" in str(raised.value)

    @pytest.mark.parametrize("reserved", ["api", "v1"])
    def test_an_upstream_named_after_the_path_root(self, tmp_path, reserved):
        """Either name would swallow the root every Ollama and OpenAI client
        already sends to, and the breakage would surface as an unexplained 404
        far from its cause."""
        target = write(tmp_path, f'[upstream.{reserved}]\nbase_url = "http://x"\n')
        with pytest.raises(ConfigError, match="reserved"):
            load_config(target)

    def test_a_missing_base_url(self, tmp_path):
        target = write(tmp_path, '[upstream.ollama]\ndialect = "ollama"\n')
        with pytest.raises(ConfigError, match="base_url"):
            load_config(target)

    def test_a_base_url_with_no_scheme(self, tmp_path):
        target = write(tmp_path, '[upstream.ollama]\nbase_url = "127.0.0.1:11434"\n')
        with pytest.raises(ConfigError, match="base_url"):
            load_config(target)

    def test_a_dialect_nobody_speaks(self, tmp_path):
        target = write(tmp_path, '[upstream.x]\nbase_url = "http://x"\ndialect = "llama-ish"\n')
        with pytest.raises(ConfigError) as raised:
            load_config(target)
        assert "llama-ish" in str(raised.value)
        # The three that are accepted are listed rather than left to the README.
        assert "ollama" in str(raised.value) and "anthropic" in str(raised.value)


class TestKeepingSecretsOutOfTheFile:
    """${VAR} in a header value, and where caller tokens come from."""

    def test_a_header_reads_its_value_from_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROVIDER_KEY", "sk-from-env")
        target = write(tmp_path, """
[server]
default_upstream = "openai"

[upstream.openai]
base_url = "https://api.openai.com"
headers  = { Authorization = "Bearer ${PROVIDER_KEY}" }
""")
        loaded = load_config(target)
        assert loaded.upstreams["openai"].headers["Authorization"] == "Bearer sk-from-env"

    def test_an_unset_variable_is_refused_by_name(self, tmp_path, monkeypatch):
        """Starting without it would send an unauthenticated request and report
        the provider's 401 as though the key were wrong."""
        monkeypatch.delenv("PROVIDER_KEY", raising=False)
        target = write(tmp_path, """
[upstream.openai]
base_url = "https://api.openai.com"
headers  = { Authorization = "Bearer ${PROVIDER_KEY}" }
""")
        with pytest.raises(ConfigError) as raised:
            load_config(target)
        message = str(raised.value)
        assert "PROVIDER_KEY" in message and "openai" in message and "Authorization" in message

    def test_a_literal_value_is_left_alone(self, tmp_path):
        target = write(tmp_path, """
[server]
default_upstream = "openai"

[upstream.openai]
base_url = "https://api.openai.com"
headers  = { Authorization = "Bearer sk-literal" }
""")
        loaded = load_config(target)
        assert loaded.upstreams["openai"].headers["Authorization"] == "Bearer sk-literal"

    def test_the_token_in_the_file_is_a_credential(self, tmp_path):
        """How an install that never runs the CLI gets one. It joins the tokens
        in state.json rather than replacing them."""
        target = write(tmp_path, MINIMAL + '\n[auth]\ntoken = "from-file"\n')
        assert load_config(target).auth_tokens == ("from-file",)

    def test_an_empty_token_is_no_token(self, tmp_path):
        """Otherwise an empty string would be a credential every caller could
        guess, while the exposure warning stayed silent."""
        target = write(tmp_path, MINIMAL + '\n[auth]\ntoken = ""\n')
        assert load_config(target).auth_tokens == ()


class TestWhatTheStateAdds:
    """state.json is the CLI's half of the same config."""

    def test_a_cli_added_provider_overrides_an_upstream_of_the_same_name(self, tmp_path):
        """`lmrelay provider add` is the newer statement of intent; losing to a
        stale hand-written table would make rotating a key silently ineffective."""
        target = write(tmp_path, MINIMAL + '\n[upstream.openai]\n'
                                           'base_url = "https://from-file.invalid"\n')
        write_state(tmp_path, providers={"openai": OPENAI_PROVIDER})
        openai = load_config(target).upstreams["openai"]
        assert openai.base_url == "https://from-state.invalid"
        assert openai.headers["Authorization"] == "Bearer sk-from-state"

    def test_and_an_upstream_it_does_not_name_is_left_alone(self, tmp_path):
        target = write(tmp_path, MINIMAL + '\n[upstream.openai]\n'
                                           'base_url = "https://from-file.invalid"\n')
        write_state(tmp_path, providers={"openai": OPENAI_PROVIDER})
        assert load_config(target).upstreams["ollama"].base_url == "http://127.0.0.1:11434"

    def test_a_config_with_no_upstream_table_is_legal_once_the_state_has_one(self, tmp_path):
        """Nothing has to be hand-written any more: `provider add` on its own is
        a working relay, and refusing here would make the CLI half a config."""
        target = write(tmp_path, '[server]\nhost = "127.0.0.1"\n')
        write_state(tmp_path, providers={"ollama": OLLAMA_PROVIDER})
        assert load_config(target).upstreams["ollama"].dialect == "ollama"

    def test_a_dollar_in_a_stored_key_is_not_expanded_again(self, tmp_path, monkeypatch):
        """${VAR} is a feature of the hand-written file. `provider add`
        substitutes {token} literally and writes the finished value down, so
        running it back through Template would rewrite an API key containing a
        $ with an environment variable's value, and send that value to the
        provider in a header."""
        monkeypatch.setenv("HOME", "/home/somebody")
        write_state(tmp_path, providers={"openai": {
            "base_url": "https://api.openai.com",
            "dialect": "openai",
            "headers": {"Authorization": "Bearer sk-live-ab$HOME-cd"},
        }})
        loaded = load_config(write(tmp_path, MINIMAL))
        assert loaded.upstreams["openai"].headers["Authorization"] == "Bearer sk-live-ab$HOME-cd"

    def test_and_a_stored_key_that_looks_like_a_bad_reference_still_loads(self, tmp_path):
        """Otherwise `provider add` saves cleanly and every command afterwards
        fails, blaming a ${...} reference the operator never wrote."""
        write_state(tmp_path, providers={"openai": {
            "base_url": "https://api.openai.com",
            "dialect": "openai",
            "headers": {"Authorization": "Bearer sk-live-9f$1abc"},
        }})
        loaded = load_config(write(tmp_path, MINIMAL))
        assert loaded.upstreams["openai"].headers["Authorization"] == "Bearer sk-live-9f$1abc"

    def test_a_state_token_leads_and_the_file_one_joins_it(self, tmp_path):
        target = write(tmp_path, MINIMAL + '\n[auth]\ntoken = "from-file"\n')
        write_state(tmp_path, auth_enabled=True, tokens=("from-state",))
        loaded = load_config(target)
        assert loaded.auth_tokens == ("from-state", "from-file")
        assert loaded.auth_enabled is True

    def test_the_same_token_from_two_places_is_one_token(self, tmp_path):
        target = write(tmp_path, MINIMAL + '\n[auth]\ntoken = "shared"\n')
        write_state(tmp_path, tokens=("shared",))
        assert load_config(target).auth_tokens == ("shared",)

    def test_a_token_in_the_file_does_not_turn_checking_on(self, tmp_path):
        """The switch is one thing in one place. A token is a credential, not a
        decision to start refusing everyone who has not got it."""
        target = write(tmp_path, MINIMAL + '\n[auth]\ntoken = "from-file"\n')
        loaded = load_config(target)
        assert loaded.auth_enabled is False
        assert loaded.auth_tokens == ("from-file",)

    def test_a_fresh_install_has_auth_off(self, tmp_path):
        """No state file at all: a relay on loopback in front of the operator's
        own Ollama must not lock them out before they have a token."""
        loaded = load_config(write(tmp_path, MINIMAL))
        assert loaded.auth_enabled is False
        assert loaded.auth_tokens == ()


class TestDefaults:
    """What a config that says nothing gets."""

    def test_it_binds_loopback_beside_ollama_rather_than_on_top_of_it(self, tmp_path):
        """11434 stays Ollama's, so an existing install needs no change at all;
        the clients are what move."""
        loaded = load_config(write(tmp_path, MINIMAL))
        assert (loaded.host, loaded.port) == ("127.0.0.1", 11435)
        assert loaded.default_upstream == "ollama"

    def test_an_upstream_that_does_not_say_gets_the_openai_dialect(self, tmp_path):
        """Four of the five providers an operator is likely to add are
        OpenAI-shaped."""
        target = write(tmp_path, '[upstream.ollama]\nbase_url = "http://127.0.0.1:11434"\n')
        assert load_config(target).upstreams["ollama"].dialect == "openai"

    def test_a_trailing_slash_on_base_url_is_removed(self, tmp_path):
        """It would double against the forwarded path, which some upstreams
        answer with a redirect and others with a 404."""
        target = write(tmp_path, '[server]\ndefault_upstream = "x"\n\n'
                                '[upstream.x]\nbase_url = "https://api.openai.com/"\n')
        assert load_config(target).upstreams["x"].base_url == "https://api.openai.com"

    def test_every_scope_of_every_limit_is_off(self, tmp_path):
        """A relay in front of one operator's own Ollama has nobody to limit,
        and an install that predates these keys must behave as it did."""
        assert load_config(write(tmp_path, MINIMAL)).limits == default_limits()


class TestTheThreeScopes:
    """[limits.*]: the same three keys in each, and every value that is a slip
    rather than a setting."""

    def test_a_scope_is_read_as_written(self, tmp_path):
        target = write(tmp_path, LIMITS + MINIMAL)
        assert load_config(target).limits["total"] == ScopeLimits(
            rate=20.0, burst=40.0, concurrent=6
        )

    def test_and_the_scopes_it_does_not_name_stay_off(self, tmp_path):
        target = write(tmp_path, LIMITS + MINIMAL)
        loaded = load_config(target)
        assert loaded.limits["per_token"] == ScopeLimits()
        assert loaded.limits["per_address"] == ScopeLimits()

    def test_a_fraction_of_a_request_per_second_is_a_rate(self, tmp_path):
        """Rounded to a whole number, `rate = 0.5` would silently turn the limit
        off rather than allow one request every two seconds."""
        target = write(tmp_path, "[limits.total]\nrate = 0.5\n" + MINIMAL)
        assert load_config(target).limits["total"].rate == 0.5

    def test_a_negative_cap_is_refused_rather_than_read_as_off(self, tmp_path):
        """0 already means off, so a negative one is a mistake. Admitting it as
        a second spelling would hide the mistake behind the behaviour the
        operator was trying to change."""
        target = write(tmp_path, "[limits.total]\nconcurrent = -1\n" + MINIMAL)
        with pytest.raises(ConfigError, match="concurrent"):
            load_config(target)

    def test_and_so_is_one_that_is_not_a_number(self, tmp_path):
        """int() raises ValueError, which a reload's `except LmrelayError` does
        not catch: it would leave the signal handler as a traceback."""
        target = write(tmp_path, '[limits.total]\nconcurrent = "lots"\n' + MINIMAL)
        with pytest.raises(ConfigError, match="whole number"):
            load_config(target)

    def test_a_negative_rate_is_refused_too(self, tmp_path):
        target = write(tmp_path, "[limits.per_token]\nrate = -1\n" + MINIMAL)
        with pytest.raises(ConfigError, match="negative"):
            load_config(target)

    @pytest.mark.parametrize("spelling", ["nan", "inf", "-inf"])
    def test_and_a_number_that_is_not_finite(self, tmp_path, spelling):
        """TOML has literals for all three and JSON reads two of them, and none
        is a rate. nan is the one that mattered: it is not negative, so it
        walked past the only guard here, and then compared false against every
        threshold. A limiter was built for it and swept for the life of the
        process, and it refused nobody, while `status` and the reload log both
        printed the scope as off."""
        target = write(tmp_path, f"[limits.total]\nrate = {spelling}\n" + MINIMAL)
        with pytest.raises(ConfigError, match="must be a number"):
            load_config(target)

    def test_the_error_names_the_table_it_read(self, tmp_path):
        """One reader serves [server] and three [limits.*] tables now, and an
        error naming the wrong one sends an operator to edit a key that is not
        there."""
        target = write(tmp_path, '[limits.per_address]\nrate = "fast"\n' + MINIMAL)
        with pytest.raises(ConfigError) as raised:
            load_config(target)
        assert "[limits.per_address] rate" in str(raised.value)

    def test_a_misspelt_scope_is_refused_by_name(self, tmp_path):
        """Ignored, it would leave an operator believing a limit is on when it
        is off, which is the whole failure this table is shaped against."""
        target = write(tmp_path, "[limits.per_toke]\nrate = 2\n" + MINIMAL)
        with pytest.raises(ConfigError) as raised:
            load_config(target)
        assert "per_toke" in str(raised.value) and "per_token" in str(raised.value)

    def test_and_so_is_a_misspelt_key(self, tmp_path):
        target = write(tmp_path, "[limits.total]\nconcurent = 6\n" + MINIMAL)
        with pytest.raises(ConfigError) as raised:
            load_config(target)
        assert "concurent" in str(raised.value) and "concurrent" in str(raised.value)

    def test_a_port_is_still_read_without_a_floor(self, tmp_path):
        """The floor is passed by one key. Ports and timeouts were not given
        one, and this change must not have quietly handed them one."""
        target = write(tmp_path, "[server]\nport = 65535\n" + MINIMAL)
        assert load_config(target).port == 65535

    def test_configuring_the_token_scope_with_auth_off_is_said_out_loud(
        self, tmp_path, caplog
    ):
        """Legal, because turning auth on later makes it live, but nothing is
        keyed by a token until then and a limit that quietly does nothing is the
        failure this redesign exists to remove."""
        target = write(tmp_path, "[limits.per_token]\nrate = 2\n" + MINIMAL)
        with caplog.at_level(logging.WARNING):
            load_config(target)
        assert "[limits.per_token] is configured but auth is off" in caplog.text

    def test_and_nothing_is_said_when_auth_is_on(self, tmp_path):
        target = write(tmp_path, "[limits.per_token]\nrate = 2\n" + MINIMAL)
        write_state(tmp_path, auth_enabled=True, tokens=("a-token",))
        assert load_config(target).limits["per_token"].rate == 2


class TestTheKeysThatWereReplaced:
    """The per-caller [server] keys, refused with a pointer for one release."""

    @pytest.mark.parametrize(
        ("key", "replacement"),
        [("rate_limit", "rate"), ("rate_burst", "burst"), ("max_concurrent", "concurrent")],
    )
    def test_an_old_limit_key_is_refused_rather_than_ignored(
        self, tmp_path, key, replacement
    ):
        """Ignored, it leaves an operator believing a limit is on when it is
        off. Refused, they find out at the moment they reload."""
        target = write(tmp_path, f"[server]\n{key} = 2\n" + MINIMAL)
        with pytest.raises(ConfigError) as raised:
            load_config(target)
        message = str(raised.value)
        assert key in message
        # Every scope it could have meant, since the old key named none of them.
        assert all(f"[limits.{scope}] {replacement}" in message for scope in SCOPES)


class TestSayingWhenThePortIsOpen:
    """A warning, not a refusal: uncredentialed behind an authenticated nginx
    is a legitimate deployment, and refusing would break it."""

    def make(self, tmp_path, host: str, auth_enabled: bool = False):
        write_state(
            tmp_path,
            auth_enabled=auth_enabled,
            tokens=("a-token",) if auth_enabled else (),
        )
        return load_config(write(tmp_path, MINIMAL + f'\n[server]\nhost = "{host}"\n'))

    def test_loopback_with_auth_off_is_not_warned_about(self, tmp_path):
        assert check_exposure(self.make(tmp_path, "127.0.0.1")) is None

    def test_a_wildcard_bind_with_auth_off_is(self, tmp_path):
        warning = check_exposure(self.make(tmp_path, "0.0.0.0"))
        assert warning is not None and "0.0.0.0" in warning

    def test_a_routable_address_with_auth_off_is(self, tmp_path):
        assert check_exposure(self.make(tmp_path, "10.4.100.247")) is not None

    def test_a_hostname_that_is_not_localhost_is_treated_as_exposed(self, tmp_path):
        """It cannot be resolved here, and the safe reading of an unknown bind
        is the one that warns."""
        assert check_exposure(self.make(tmp_path, "gpu2.internal")) is not None

    def test_localhost_by_name_is_not(self, tmp_path):
        assert check_exposure(self.make(tmp_path, "localhost")) is None

    def test_auth_being_on_settles_it_wherever_it_binds(self, tmp_path):
        """The switch, not the presence of a token: tokens that nothing checks
        leave the port as open as none at all."""
        assert check_exposure(self.make(tmp_path, "0.0.0.0", auth_enabled=True)) is None


class TestTheEnvironmentIsNotASource:
    """Settings come from the file and from state.json, and from nowhere else.

    There was briefly an environment spelling for every key. It is gone, and
    these tests are what keeps it gone: a name under one of the old prefixes is
    now ordinary shell furniture, so it must neither be read nor refused.
    """

    def test_a_setting_shaped_name_is_not_read(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMRELAY_SERVER_PORT", "11440")
        assert load_config(write(tmp_path, MINIMAL)).port == 11435

    def test_nor_is_a_limit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMRELAY_LIMITS_TOTAL_CONCURRENT", "6")
        assert load_config(write(tmp_path, MINIMAL)).limits["total"].concurrent == 0

    def test_nor_the_auth_switch(self, tmp_path, monkeypatch):
        """The switch is state.json's, and `lmrelay auth true` is what moves it.
        Read from the environment it could turn checking off on a relay whose
        operator had just turned it on."""
        monkeypatch.setenv("LMRELAY_AUTH_ENABLED", "true")
        write_state(tmp_path, auth_enabled=False)
        assert load_config(write(tmp_path, MINIMAL)).auth_enabled is False

    def test_nor_a_credential(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMRELAY_TOKEN", "from-env")
        target = write(tmp_path, MINIMAL + '\n[auth]\ntoken = "from-file"\n')
        assert load_config(target).auth_tokens == ("from-file",)

    def test_nor_an_upstream(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMRELAY_UPSTREAM_OPENAI_KEY", "sk-from-env")
        assert sorted(load_config(write(tmp_path, MINIMAL)).upstreams) == ["ollama"]

    def test_and_none_of_them_is_refused_either(self, tmp_path, monkeypatch):
        """Refusing would be the other way to have an opinion about a name the
        relay no longer owns, and LMRELAY_ is a prefix somebody else's script
        may reasonably use."""
        monkeypatch.setenv("LMRELAY_SERVER_PROT", "11440")
        monkeypatch.setenv("LMRELAY_LIMITS_TOTAL_CONCURENT", "6")
        assert load_config(write(tmp_path, MINIMAL)).port == 11435

    def test_but_the_config_path_still_comes_from_the_environment(
        self, tmp_path, monkeypatch
    ):
        """The one variable that survived, and it names a file rather than a
        setting: `--config` publishes it so the app, which loads the config
        again from scratch under uvicorn, reads the file the command chose."""
        chosen = write(tmp_path, MINIMAL)
        monkeypatch.setenv(CONFIG_ENV_VAR, str(chosen))
        assert load_config().config_path == chosen


class TestARelayWithNoFileAtAll:
    """There is no such relay: with no file there is nothing to configure it."""

    def test_no_file_anywhere_is_refused_and_names_both_places_looked(
        self, tmp_path, monkeypatch
    ):
        """It cannot invent an upstream, and starting empty would produce 404s
        the operator would have to debug."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setattr(config_module, "HOME_CONFIG_PATH", tmp_path / ".lmrelay" / "x.toml")
        with pytest.raises(ConfigError) as raised:
            load_config()
        message = str(raised.value)
        assert "lmrelay init" in message
        assert "lmrelay.toml" in message and "x.toml" in message
