#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Where the config is found, what the state adds to it, and every way it can be refused."""

import pytest

# Local imports
from lmrelay import config as config_module
from lmrelay.config import (
    CONFIG_ENV_VAR,
    TOKEN_ENV_VAR,
    ConfigError,
    check_exposure,
    find_config_path,
    load_config,
)
from tests.conftest import write_state

MINIMAL = """
[upstream.ollama]
base_url = "http://127.0.0.1:11434"
dialect  = "ollama"
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

    def test_every_limit_is_off(self, tmp_path):
        """A relay in front of one operator's own Ollama has nobody to limit,
        and an install that predates these keys must behave as it did."""
        loaded = load_config(write(tmp_path, MINIMAL))
        assert (loaded.rate_limit, loaded.rate_burst, loaded.max_concurrent) == (0, 0, 0)


class TestRefusingALimitNobodyMeant:
    """The numbers that bound a caller, and the values that are a slip rather
    than a setting."""

    def test_a_cap_is_read_as_written(self, tmp_path):
        target = write(tmp_path, "[server]\nmax_concurrent = 3\n" + MINIMAL)
        assert load_config(target).max_concurrent == 3

    def test_a_negative_cap_is_refused_rather_than_read_as_off(self, tmp_path):
        """0 already means off, so a negative one is a mistake. Admitting it as
        a second spelling would hide the mistake behind the behaviour the
        operator was trying to change."""
        target = write(tmp_path, "[server]\nmax_concurrent = -1\n" + MINIMAL)
        with pytest.raises(ConfigError, match="max_concurrent"):
            load_config(target)

    def test_and_so_is_one_that_is_not_a_number(self, tmp_path):
        """int() raises ValueError, which a reload's `except LmrelayError` does
        not catch: it would leave the signal handler as a traceback."""
        target = write(tmp_path, '[server]\nmax_concurrent = "lots"\n' + MINIMAL)
        with pytest.raises(ConfigError, match="whole number"):
            load_config(target)

    def test_a_port_is_still_read_without_a_floor(self, tmp_path):
        """The floor is passed by one key. Ports and timeouts were not given
        one, and this change must not have quietly handed them one."""
        target = write(tmp_path, "[server]\nport = 65535\n" + MINIMAL)
        assert load_config(target).port == 65535


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
