#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Where the config is found, what the environment and the state add, and every refusal."""

import logging

import pytest

# Local imports
from lmrelay import config as config_module
from lmrelay.config import (
    AUTH_ENABLED_ENV_VAR,
    CONFIG_ENV_VAR,
    TOKEN_ENV_VAR,
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

    def test_the_token_environment_variable_joins_the_one_in_the_file(self, tmp_path, monkeypatch):
        """Additional, not an override: a container can inject a credential
        without invalidating the one the operator wrote down."""
        monkeypatch.setenv(TOKEN_ENV_VAR, "from-env")
        target = write(tmp_path, MINIMAL + '\n[auth]\ntoken = "from-file"\n')
        assert load_config(target).auth_tokens == ("from-file", "from-env")

    def test_and_the_file_is_used_when_it_does_not(self, tmp_path, monkeypatch):
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
        target = write(tmp_path, MINIMAL + '\n[auth]\ntoken = "from-file"\n')
        assert load_config(target).auth_tokens == ("from-file",)

    def test_an_empty_token_is_no_token(self, tmp_path, monkeypatch):
        """Otherwise an empty string would be a credential every caller could
        guess, while the exposure warning stayed silent."""
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
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

    def test_a_state_token_leads_and_the_other_two_join_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "from-env")
        target = write(tmp_path, MINIMAL + '\n[auth]\ntoken = "from-file"\n')
        write_state(tmp_path, auth_enabled=True, tokens=("from-state",))
        loaded = load_config(target)
        assert loaded.auth_tokens == ("from-state", "from-file", "from-env")
        assert loaded.auth_enabled is True

    def test_the_same_token_from_two_places_is_one_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "shared")
        target = write(tmp_path, MINIMAL + '\n[auth]\ntoken = "shared"\n')
        write_state(tmp_path, tokens=("shared",))
        assert load_config(target).auth_tokens == ("shared",)

    def test_a_token_in_the_file_does_not_turn_checking_on(self, tmp_path, monkeypatch):
        """The switch is one thing in one place. A token is a credential, not a
        decision to start refusing everyone who has not got it."""
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
        target = write(tmp_path, MINIMAL + '\n[auth]\ntoken = "from-file"\n')
        loaded = load_config(target)
        assert loaded.auth_enabled is False
        assert loaded.auth_tokens == ("from-file",)

    def test_a_fresh_install_has_auth_off(self, tmp_path, monkeypatch):
        """No state file at all: a relay on loopback in front of the operator's
        own Ollama must not lock them out before they have a token."""
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
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


class TestTheEnvironmentAsASource:
    """LMRELAY_ plus the path to the key, and the environment wins."""

    def test_it_sets_a_key_the_file_never_mentions(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMRELAY_SERVER_PORT", "11440")
        assert load_config(write(tmp_path, MINIMAL)).port == 11440

    def test_and_overrides_one_the_file_does(self, tmp_path, monkeypatch):
        """The file is the shared, checked-in thing and the environment is the
        deployment. The other way round makes an environment variable a silent
        no-op whenever the file happens to mention the key."""
        monkeypatch.setenv("LMRELAY_SERVER_PORT", "11440")
        assert load_config(write(tmp_path, "[server]\nport = 11435\n" + MINIMAL)).port == 11440

    def test_the_path_is_the_whole_of_the_name(self, tmp_path, monkeypatch):
        """No abbreviations and no special cases, so the name is derivable from
        the file without a table."""
        monkeypatch.setenv("LMRELAY_LIMITS_PER_TOKEN_RATE", "2")
        monkeypatch.setenv("LMRELAY_LIMITS_PER_TOKEN_BURST", "5")
        monkeypatch.setenv("LMRELAY_LIMITS_TOTAL_CONCURRENT", "6")
        loaded = load_config(write(tmp_path, MINIMAL))
        assert loaded.limits["per_token"] == ScopeLimits(rate=2.0, burst=5.0)
        assert loaded.limits["total"] == ScopeLimits(concurrent=6)

    def test_a_value_it_cannot_read_is_refused_in_the_files_own_words(
        self, tmp_path, monkeypatch
    ):
        """One validator for both sources, so an operator who has learnt what a
        bad port looks like in the file recognises it from the environment."""
        monkeypatch.setenv("LMRELAY_SERVER_PORT", "eleven")
        with pytest.raises(ConfigError, match="whole number"):
            load_config(write(tmp_path, MINIMAL))

    def test_an_empty_value_is_unset_rather_than_zero(self, tmp_path, monkeypatch):
        """`Environment="LMRELAY_SERVER_PORT="` in a unit file and
        `LMRELAY_SERVER_PORT:` in a compose file are how people write "I am not
        setting this", and reading either as port 0 would bind something
        absurd."""
        monkeypatch.setenv("LMRELAY_SERVER_PORT", "")
        assert load_config(write(tmp_path, MINIMAL)).port == 11435

    def test_but_zero_itself_is_a_value(self, tmp_path, monkeypatch):
        """It is how a limit is turned off, so it cannot be a spelling of unset."""
        monkeypatch.setenv("LMRELAY_LIMITS_TOTAL_RATE", "0")
        assert load_config(write(tmp_path, LIMITS + MINIMAL)).limits["total"].rate == 0.0

    def test_a_variable_under_a_known_prefix_that_names_nothing_is_refused(
        self, tmp_path, monkeypatch
    ):
        """A typo silently ignored leaves an operator believing a limit is on."""
        monkeypatch.setenv("LMRELAY_LIMITS_TOTAL_CONCURENT", "6")
        with pytest.raises(ConfigError) as raised:
            load_config(write(tmp_path, MINIMAL))
        assert "LMRELAY_LIMITS_TOTAL_CONCURENT" in str(raised.value)

    def test_while_the_names_that_are_not_settings_are_left_alone(
        self, tmp_path, monkeypatch
    ):
        """LMRELAY_CONFIG and LMRELAY_STATE say where the files are, which
        cannot itself live in a file, and the CLI sets two of its own. A blanket
        refusal would break the next one to be added."""
        target = write(tmp_path, MINIMAL)
        monkeypatch.setenv("LMRELAY_BIND", "127.0.0.1:11435")
        monkeypatch.setenv("LMRELAY_SERVICE", "lmrelay")
        assert load_config(target).port == 11435


class TestSayingWhatTheEnvironmentIsOverriding:
    """The price of environment precedence: an operator edits the file, reloads,
    and nothing changes."""

    def test_a_genuine_shadow_is_named(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("LMRELAY_SERVER_PORT", "11440")
        target = write(tmp_path, "[server]\nport = 11435\n" + MINIMAL)
        with caplog.at_level(logging.WARNING):
            load_config(target)
        assert f"the environment sets server.port, overriding {target}" in caplog.text

    def test_a_key_the_file_omits_is_not(self, tmp_path, monkeypatch, caplog):
        """It overrides nothing, so naming it would make the line noise and the
        genuine shadows harder to see."""
        monkeypatch.setenv("LMRELAY_SERVER_PORT", "11440")
        with caplog.at_level(logging.WARNING):
            load_config(write(tmp_path, MINIMAL))
        assert "the environment sets" not in caplog.text

    def test_several_shadows_are_named_in_the_order_the_file_lists_them(
        self, tmp_path, monkeypatch, caplog
    ):
        """Sorted by the environment instead, two relays with identical settings
        would print different lines."""
        monkeypatch.setenv("LMRELAY_LIMITS_TOTAL_CONCURRENT", "9")
        monkeypatch.setenv("LMRELAY_SERVER_PORT", "11440")
        with caplog.at_level(logging.WARNING):
            load_config(write(tmp_path, "[server]\nport = 11435\n" + LIMITS + MINIMAL))
        assert "sets server.port, limits.total.concurrent," in caplog.text


class TestTheAuthSwitchFromTheEnvironment:
    """It lives in the state, and a container needs it without running the CLI."""

    def test_it_turns_auth_on_over_a_state_that_says_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTH_ENABLED_ENV_VAR, "true")
        write_state(tmp_path, auth_enabled=False, tokens=("a-token",))
        assert load_config(write(tmp_path, MINIMAL)).auth_enabled is True

    def test_and_off_over_a_state_that_says_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTH_ENABLED_ENV_VAR, "0")
        write_state(tmp_path, auth_enabled=True, tokens=("a-token",))
        assert load_config(write(tmp_path, MINIMAL)).auth_enabled is False

    def test_unset_leaves_the_state_to_decide(self, tmp_path, monkeypatch):
        monkeypatch.delenv(AUTH_ENABLED_ENV_VAR, raising=False)
        write_state(tmp_path, auth_enabled=True, tokens=("a-token",))
        assert load_config(write(tmp_path, MINIMAL)).auth_enabled is True

    @pytest.mark.parametrize("spelling", ["1", "TRUE", "Yes", "on"])
    def test_the_spellings_people_write(self, tmp_path, monkeypatch, spelling):
        monkeypatch.setenv(AUTH_ENABLED_ENV_VAR, spelling)
        write_state(tmp_path, tokens=("a-token",))
        assert load_config(write(tmp_path, MINIMAL)).auth_enabled is True

    def test_and_anything_else_is_refused_by_name(self, tmp_path, monkeypatch):
        """The one place liberality is dangerous: a typo read as false is auth
        turned off, on a relay whose operator believes it is on."""
        monkeypatch.setenv(AUTH_ENABLED_ENV_VAR, "ture")
        with pytest.raises(ConfigError) as raised:
            load_config(write(tmp_path, MINIMAL))
        assert AUTH_ENABLED_ENV_VAR in str(raised.value) and "ture" in str(raised.value)

    @pytest.mark.parametrize("name", ["LMRELAY_AUTH_ENABLE", "LMRELAY_AUTH_ENABLD"])
    def test_and_so_is_a_typo_in_the_name_of_the_switch(self, tmp_path, monkeypatch, name):
        """The value was strict and the name was not, so the same outcome stayed
        one keystroke away: LMRELAY_AUTH_ENABLE=true was read by nothing, the
        relay kept the state's own answer of off, and every anonymous caller was
        served with the configured upstream credentials. Every other prefix
        already refused its typos by name; this was the one that did not."""
        monkeypatch.setenv(name, "true")
        write_state(tmp_path, auth_enabled=False, tokens=("a-token",))
        with pytest.raises(ConfigError) as raised:
            load_config(write(tmp_path, MINIMAL))
        assert name in str(raised.value)

    def test_and_the_token_list_nobody_implemented(self, tmp_path, monkeypatch):
        """There is deliberately no LMRELAY_AUTH_TOKENS taking a delimited list,
        and a variable that names nothing is now told so rather than ignored."""
        monkeypatch.setenv("LMRELAY_AUTH_TOKENS", "one,two")
        with pytest.raises(ConfigError, match="LMRELAY_AUTH_TOKENS"):
            load_config(write(tmp_path, MINIMAL))

    def test_while_the_two_real_auth_variables_still_carry(self, tmp_path, monkeypatch):
        """The prefix is checked, not closed: both documented names go through."""
        monkeypatch.setenv(AUTH_ENABLED_ENV_VAR, "true")
        monkeypatch.setenv("LMRELAY_AUTH_TOKEN", "from-the-environment")
        loaded = load_config(write(tmp_path, MINIMAL))
        assert loaded.auth_enabled is True and "from-the-environment" in loaded.auth_tokens


class TestCredentialsFromTheEnvironment:
    """LMRELAY_AUTH_TOKEN by the rule, LMRELAY_TOKEN as the older spelling."""

    def test_the_rule_shaped_name_sets_the_auth_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMRELAY_AUTH_TOKEN", "from-env")
        assert load_config(write(tmp_path, MINIMAL)).auth_tokens == ("from-env",)

    def test_and_it_overrides_the_one_in_the_file(self, tmp_path, monkeypatch):
        """[auth] token is an ordinary setting, so it takes the ordinary
        precedence rather than a rule of its own."""
        monkeypatch.setenv("LMRELAY_AUTH_TOKEN", "from-env")
        target = write(tmp_path, MINIMAL + '\n[auth]\ntoken = "from-file"\n')
        assert load_config(target).auth_tokens == ("from-env",)

    def test_both_spellings_at_once_are_two_credentials_rather_than_a_conflict(
        self, tmp_path, monkeypatch
    ):
        """Each of them means "this is a valid credential" on its own, and
        collect_auth_tokens is additive, so there is nothing to resolve."""
        monkeypatch.setenv("LMRELAY_AUTH_TOKEN", "the-new-spelling")
        monkeypatch.setenv(TOKEN_ENV_VAR, "the-old-spelling")
        loaded = load_config(write(tmp_path, MINIMAL))
        assert loaded.auth_tokens == ("the-new-spelling", "the-old-spelling")


class TestUpstreamsFromTheEnvironment:
    """Scalars by the rule, and the credential by the shortcut the CLI has."""

    def test_a_base_url_and_a_dialect_define_one(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMRELAY_UPSTREAM_LOCAL_BASE_URL", "http://ollama:11434")
        monkeypatch.setenv("LMRELAY_UPSTREAM_LOCAL_DIALECT", "ollama")
        local = load_config(write(tmp_path, MINIMAL)).upstreams["local"]
        assert (local.base_url, local.dialect) == ("http://ollama:11434", "ollama")

    def test_and_a_base_url_overrides_the_file_table_of_the_same_name(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("LMRELAY_UPSTREAM_OLLAMA_BASE_URL", "http://ollama:11434")
        loaded = load_config(write(tmp_path, MINIMAL))
        assert loaded.upstreams["ollama"].base_url == "http://ollama:11434"
        # And keeps the dialect the file gave it: a path overrides its own key.
        assert loaded.upstreams["ollama"].dialect == "ollama"

    def test_a_key_takes_the_preset_a_provider_add_would(self, tmp_path, monkeypatch):
        """It routes through add_provider, so an environment-configured provider
        fails in exactly the ways a CLI-added one fails."""
        monkeypatch.setenv("LMRELAY_UPSTREAM_OPENAI_KEY", "sk-from-env")
        openai = load_config(write(tmp_path, MINIMAL)).upstreams["openai"]
        assert openai.base_url == "https://api.openai.com"
        assert openai.headers == {"Authorization": "Bearer sk-from-env"}

    def test_including_a_header_shape_no_variable_could_spell(self, tmp_path, monkeypatch):
        """x-api-key and anthropic-version contain hyphens, which an environment
        variable name cannot carry, and mapping - to _ is not reversible. The
        preset is how they arrive instead."""
        monkeypatch.setenv("LMRELAY_UPSTREAM_ANTHROPIC_KEY", "sk-ant-from-env")
        anthropic = load_config(write(tmp_path, MINIMAL)).upstreams["anthropic"]
        assert anthropic.headers == {
            "x-api-key": "sk-ant-from-env", "anthropic-version": "2023-06-01"
        }

    def test_a_name_no_preset_knows_needs_a_base_url(self, tmp_path, monkeypatch):
        """And the refusal names the variable to set, not the CLI flag: an
        operator in a compose file has no --base-url to pass."""
        monkeypatch.setenv("LMRELAY_UPSTREAM_HOUSE_KEY", "sk-house")
        with pytest.raises(ConfigError) as raised:
            load_config(write(tmp_path, MINIMAL))
        assert "LMRELAY_UPSTREAM_HOUSE_BASE_URL" in str(raised.value)

    def test_and_with_one_it_gets_a_bearer(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMRELAY_UPSTREAM_HOUSE_KEY", "sk-house")
        monkeypatch.setenv("LMRELAY_UPSTREAM_HOUSE_BASE_URL", "https://llm.house.invalid")
        house = load_config(write(tmp_path, MINIMAL)).upstreams["house"]
        assert house.base_url == "https://llm.house.invalid"
        assert house.headers == {"Authorization": "Bearer sk-house"}

    def test_a_stored_key_is_not_run_through_env_expansion(self, tmp_path, monkeypatch):
        """It arrives through the state, like a CLI-added one, so a key holding
        a $ is not rewritten with the value of an environment variable and sent
        to the provider."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LMRELAY_UPSTREAM_OPENAI_KEY", "sk-live-ab$HOME-cd")
        openai = load_config(write(tmp_path, MINIMAL)).upstreams["openai"]
        assert openai.headers["Authorization"] == "Bearer sk-live-ab$HOME-cd"

    def test_an_underscore_in_the_name_is_part_of_the_name(self, tmp_path, monkeypatch):
        """The field suffix set is closed and matched longest first, so
        MY_LLM_BASE_URL is my_llm and BASE_URL rather than my and LLM_BASE_URL."""
        monkeypatch.setenv("LMRELAY_UPSTREAM_MY_LLM_BASE_URL", "http://my-llm.invalid")
        assert "my_llm" in load_config(write(tmp_path, MINIMAL)).upstreams

    def test_even_when_it_looks_like_another_field(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMRELAY_UPSTREAM_FOO_BASE_KEY", "sk-foo")
        monkeypatch.setenv("LMRELAY_UPSTREAM_FOO_BASE_BASE_URL", "https://foo.invalid")
        assert "foo_base" in load_config(write(tmp_path, MINIMAL)).upstreams

    def test_a_field_it_does_not_know_is_refused_rather_than_ignored(
        self, tmp_path, monkeypatch
    ):
        """A variable nobody reads leaves an operator believing a provider is
        configured while the relay has never heard of it."""
        monkeypatch.setenv("LMRELAY_UPSTREAM_OPENAI_TIMEOUT", "30")
        with pytest.raises(ConfigError) as raised:
            load_config(write(tmp_path, MINIMAL))
        assert "LMRELAY_UPSTREAM_OPENAI_TIMEOUT" in str(raised.value)

    def test_a_reserved_name_is_refused_here_too(self, tmp_path, monkeypatch):
        """It would swallow the path root every Ollama and OpenAI client sends
        to, whichever source names it."""
        monkeypatch.setenv("LMRELAY_UPSTREAM_V1_BASE_URL", "http://x.invalid")
        with pytest.raises(ConfigError, match="reserved"):
            load_config(write(tmp_path, MINIMAL))


class TestARelayWithNoFileAtAll:
    """A container where the environment carries the whole configuration."""

    def empty_cwd(self, tmp_path, monkeypatch):
        """A working directory and a home with no lmrelay.toml in either."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setattr(config_module, "HOME_CONFIG_PATH", tmp_path / ".lmrelay" / "x.toml")

    def test_the_environment_alone_is_a_working_relay(self, tmp_path, monkeypatch):
        self.empty_cwd(tmp_path, monkeypatch)
        monkeypatch.setenv("LMRELAY_UPSTREAM_OLLAMA_BASE_URL", "http://ollama:11434")
        monkeypatch.setenv("LMRELAY_UPSTREAM_OPENAI_KEY", "sk-from-env")
        monkeypatch.setenv("LMRELAY_AUTH_ENABLED", "true")
        monkeypatch.setenv(TOKEN_ENV_VAR, "lmr-from-env")
        monkeypatch.setenv("LMRELAY_LIMITS_TOTAL_CONCURRENT", "6")

        loaded = load_config()
        assert sorted(loaded.upstreams) == ["ollama", "openai"]
        assert loaded.default_upstream == "ollama"
        assert loaded.auth_enabled is True
        assert loaded.auth_tokens == ("lmr-from-env",)
        assert loaded.limits["total"].concurrent == 6

    def test_and_the_state_still_sits_where_the_file_would_have(
        self, tmp_path, monkeypatch
    ):
        """The pidfile and state.json are located beside the config, so the
        path has to name somewhere even when nothing is there to read."""
        self.empty_cwd(tmp_path, monkeypatch)
        monkeypatch.setenv("LMRELAY_UPSTREAM_OLLAMA_BASE_URL", "http://ollama:11434")
        loaded = load_config()
        assert loaded.config_path == tmp_path / ".lmrelay" / "x.toml"
        assert loaded.state_path == tmp_path / ".lmrelay" / "state.json"

    def test_but_no_file_and_no_upstream_anywhere_is_still_refused(
        self, tmp_path, monkeypatch
    ):
        """It cannot invent an upstream, and starting empty would produce 404s
        the operator would have to debug."""
        self.empty_cwd(tmp_path, monkeypatch)
        with pytest.raises(ConfigError) as raised:
            load_config()
        message = str(raised.value)
        assert "lmrelay init" in message and "environment names no upstream" in message
