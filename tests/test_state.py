#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The CLI-owned state: caller tokens, the auth switch and provider keys."""

import json
import os
from pathlib import Path

import pytest

# Local imports
from lmrelay.errors import StateError
from lmrelay.state import (
    PROVIDER_PRESETS,
    STATE_ENV_VAR,
    STATE_VERSION,
    TOKEN_PREFIX,
    add_provider,
    add_token,
    delete_provider,
    delete_token,
    generate_token,
    load_state,
    mask_token,
    save_state,
    set_auth_enabled,
    state_path_for,
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Nothing here may read or write the operator's real ~/.lmrelay."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(STATE_ENV_VAR, raising=False)


@pytest.fixture
def permissive_umask():
    """A developer whose own umask is 0o077 would pass the file-mode test below
    by accident, and the bug it guards only shows under a permissive one."""
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def fresh(tmp_path):
    """The empty default, addressed at a state file that does not exist yet."""
    return load_state(tmp_path / "state.json")


class TestWhereTheStateLives:
    """Beside the config, unless the environment says otherwise."""

    def test_it_sits_beside_the_config(self, tmp_path):
        assert state_path_for(tmp_path / "lmrelay.toml") == tmp_path / "state.json"

    def test_the_environment_variable_moves_it(self, tmp_path, monkeypatch):
        elsewhere = tmp_path / "elsewhere" / "state.json"
        monkeypatch.setenv(STATE_ENV_VAR, str(elsewhere))
        assert state_path_for(tmp_path / "lmrelay.toml") == elsewhere

    def test_and_a_tilde_in_it_is_expanded(self, tmp_path, monkeypatch):
        """Shells expand ~ in a value they pass through, but a systemd unit or a
        plist hands the variable over literally."""
        monkeypatch.setenv(STATE_ENV_VAR, "~/state.json")
        assert state_path_for(tmp_path / "lmrelay.toml") == Path.home() / "state.json"


class TestAStateFileThatIsNotThereYet:
    """What a relay that has never been configured starts from."""

    def test_a_missing_file_reads_as_the_empty_default(self, tmp_path):
        state = fresh(tmp_path)
        assert state.tokens == () and state.providers == {}

    def test_auth_starts_off(self, tmp_path):
        """A freshly installed relay on loopback is a transparent proxy: it must
        not refuse the operator's own Ollama before they have a token."""
        assert fresh(tmp_path).auth_enabled is False

    def test_the_first_token_will_be_number_one(self, tmp_path):
        assert fresh(tmp_path).next_token_id == 1


class TestKeepingCallerTokens:
    """Add, delete, and the ids the operator sees."""

    def test_a_token_is_stored_with_its_label(self, tmp_path):
        state, record = add_token(fresh(tmp_path), "lmr_first", label="laptop")
        assert record.token == "lmr_first" and record.label == "laptop"
        assert state.tokens == (record,)

    def test_a_token_with_no_label_gets_an_empty_one(self, tmp_path):
        unused_state, record = add_token(fresh(tmp_path), "lmr_first")
        assert record.label == ""

    def test_it_is_stamped_with_the_time_it_was_made(self, tmp_path):
        unused_state, record = add_token(fresh(tmp_path), "lmr_first")
        assert record.created_at.endswith("Z")

    def test_ids_are_not_reused_after_a_delete(self, tmp_path):
        """An id printed by `token list` has to keep meaning the same token
        after an unrelated delete, or a copied id deletes the wrong one."""
        state, unused_first = add_token(fresh(tmp_path), "lmr_first")
        state, second = add_token(state, "lmr_second")
        state = delete_token(state, second.id)
        state, third = add_token(state, "lmr_third")
        assert third.id > second.id

    def test_the_same_token_twice_is_refused(self, tmp_path):
        state, unused_record = add_token(fresh(tmp_path), "lmr_first")
        with pytest.raises(StateError):
            add_token(state, "lmr_first")

    @pytest.mark.parametrize("empty", ["", "   "])
    def test_an_empty_token_is_refused(self, tmp_path, empty):
        """It would be a credential every caller could guess."""
        with pytest.raises(StateError):
            add_token(fresh(tmp_path), empty)

    def test_deleting_an_unknown_id_names_the_ones_that_exist(self, tmp_path):
        state, first = add_token(fresh(tmp_path), "lmr_first")
        with pytest.raises(StateError) as raised:
            delete_token(state, 99)
        assert str(first.id) in str(raised.value)

    def test_deleting_one_leaves_the_others(self, tmp_path):
        state, first = add_token(fresh(tmp_path), "lmr_first")
        state, unused_second = add_token(state, "lmr_second")
        state = delete_token(state, first.id)
        assert [record.token for record in state.tokens] == ["lmr_second"]

    def test_adding_writes_nothing_by_itself(self, tmp_path):
        """The caller decides when the file changes, so a command that fails
        half way leaves the state as it found it."""
        add_token(fresh(tmp_path), "lmr_first")
        assert not (tmp_path / "state.json").exists()


class TestGeneratedTokens:
    """The ones `lmrelay token gen` hands out."""

    def test_it_carries_the_prefix_that_marks_it_as_ours(self):
        assert generate_token().startswith(TOKEN_PREFIX)

    def test_two_of_them_are_never_the_same(self):
        assert generate_token() != generate_token()

    def test_it_is_long_enough_to_be_worth_generating(self):
        assert len(generate_token()) >= 40


class TestShowingATokenWithoutShowingIt:
    """What `token list` prints when it was not asked for the whole thing."""

    def test_a_normal_token_keeps_its_ends(self):
        masked = mask_token("lmr_ABCDEFGHIJKLMNOPqrst")
        assert masked.startswith("lmr_ABCD") and masked.endswith("qrst")

    def test_and_loses_its_middle(self):
        assert "IJKLMNOP" not in mask_token("lmr_ABCDEFGHIJKLMNOPqrst")

    def test_a_short_token_is_masked_whole(self):
        """Showing the first eight characters of a nine-character token would
        leak nearly all of it."""
        assert "short" not in mask_token("lmr_short")


class TestTheAuthSwitch:
    """The one thing that decides whether a credential is required."""

    def test_it_can_be_turned_on(self, tmp_path):
        assert set_auth_enabled(fresh(tmp_path), True).auth_enabled is True

    def test_and_off_again(self, tmp_path):
        state = set_auth_enabled(fresh(tmp_path), True)
        assert set_auth_enabled(state, False).auth_enabled is False

    def test_the_tokens_are_left_alone(self, tmp_path):
        """Turning auth off is not a reason to lose the key ring."""
        state, unused_record = add_token(fresh(tmp_path), "lmr_first")
        assert set_auth_enabled(state, False).tokens == state.tokens


class TestProvidersAddedByName:
    """`lmrelay provider add openai sk-...` instead of a TOML table."""

    def test_a_preset_supplies_the_base_url_and_the_dialect(self, tmp_path):
        state = add_provider(fresh(tmp_path), "openai", "sk-test")
        provider = state.providers["openai"]
        assert provider["base_url"] == PROVIDER_PRESETS["openai"]["base_url"]
        assert provider["dialect"] == "openai"

    def test_the_token_is_substituted_into_the_header(self, tmp_path):
        state = add_provider(fresh(tmp_path), "openai", "sk-test")
        assert state.providers["openai"]["headers"]["Authorization"] == "Bearer sk-test"

    def test_every_header_a_preset_names_is_kept(self, tmp_path):
        """Anthropic refuses a request without its version header, and the
        operator never typed one."""
        state = add_provider(fresh(tmp_path), "anthropic", "sk-ant")
        headers = state.providers["anthropic"]["headers"]
        assert headers["x-api-key"] == "sk-ant"
        assert headers["anthropic-version"] == "2023-06-01"

    def test_a_dollar_in_the_key_is_not_read_as_a_variable(self, tmp_path):
        """Substitution is literal: a key containing a $ must survive it."""
        state = add_provider(fresh(tmp_path), "openai", "sk-$HOME-x")
        assert state.providers["openai"]["headers"]["Authorization"] == "Bearer sk-$HOME-x"

    def test_an_unknown_name_without_a_base_url_is_refused(self, tmp_path):
        """The presets are listed, because the next move is either to correct
        the name or to pass --base-url."""
        with pytest.raises(StateError) as raised:
            add_provider(fresh(tmp_path), "acme", "tok")
        message = str(raised.value)
        assert "--base-url" in message and "openai" in message

    def test_an_extra_header_replaces_a_preset_one_whatever_its_case(self, tmp_path):
        """Header names are case-insensitive, so a plain dict update would keep
        both and the request would carry two Authorization lines, and the operator
        would still be shipping the key they meant to replace, and which one the
        provider honours would be the provider's choice."""
        state = add_provider(
            fresh(tmp_path), "openai", "sk-real",
            extra_headers={"authorization": "Bearer sk-chosen"},
        )
        headers = state.providers["openai"]["headers"]
        assert list(headers.values()) == ["Bearer sk-chosen"]

    def test_adding_the_same_name_again_rotates_the_key(self, tmp_path):
        """Refusing would make a key rotation a delete followed by an add."""
        state = add_provider(fresh(tmp_path), "openai", "sk-old")
        state = add_provider(state, "openai", "sk-new")
        assert state.providers["openai"]["headers"]["Authorization"] == "Bearer sk-new"

    def test_and_rotating_one_no_preset_knows_needs_no_second_base_url(self, tmp_path):
        """An upstream the relay already carries is its own preset. Without
        this, the operator could not act on the line `config import` prints
        after a --no-secrets bundle: the bundle carried the custom endpoint, the
        import wrote it into the state, and `provider add` still asked for a
        --base-url that was already on disk. A custom endpoint is exactly the
        upstream such a bundle is most likely to carry."""
        state = add_provider(
            fresh(tmp_path), "myllm", "tok",
            base_url="https://llm.example.test", dialect="anthropic",
        )
        state = add_provider(state, "myllm", "sk-restored")
        provider = state.providers["myllm"]
        assert provider["base_url"] == "https://llm.example.test"
        assert provider["dialect"] == "anthropic"

    def test_while_a_name_nothing_has_ever_heard_of_is_still_refused(self, tmp_path):
        """The fallback is the state's own record, not a way past the check."""
        state = add_provider(fresh(tmp_path), "myllm", "tok", base_url="https://llm.example.test")
        with pytest.raises(StateError, match="--base-url"):
            add_provider(state, "nowhere", "tok")


class TestProvidersSpelledOut:
    """The explicit form, validated by the same rules as the TOML."""

    def test_an_explicit_base_url_makes_any_name_legal(self, tmp_path):
        state = add_provider(fresh(tmp_path), "acme", "tok", base_url="https://acme.test")
        assert state.providers["acme"]["base_url"] == "https://acme.test"

    def test_a_base_url_with_no_scheme_is_refused(self, tmp_path):
        with pytest.raises(StateError):
            add_provider(fresh(tmp_path), "acme", "tok", base_url="acme.test")

    def test_a_dialect_nobody_speaks_is_refused(self, tmp_path):
        with pytest.raises(StateError) as raised:
            add_provider(
                fresh(tmp_path), "acme", "tok", base_url="https://acme.test", dialect="llama-ish"
            )
        assert "llama-ish" in str(raised.value)

    @pytest.mark.parametrize("reserved", ["api", "v1"])
    def test_a_name_that_would_shadow_the_path_root_is_refused(self, tmp_path, reserved):
        """Either name would swallow the root every Ollama and OpenAI client
        already sends to, and the breakage would surface as a 404."""
        with pytest.raises(StateError):
            add_provider(fresh(tmp_path), reserved, "tok", base_url="https://x")

    def test_extra_headers_go_on_top_of_a_preset(self, tmp_path):
        state = add_provider(
            fresh(tmp_path), "openai", "sk-test", extra_headers={"OpenAI-Organization": "org-1"}
        )
        headers = state.providers["openai"]["headers"]
        assert headers["OpenAI-Organization"] == "org-1"
        assert headers["Authorization"] == "Bearer sk-test"

    def test_deleting_an_unknown_provider_is_refused_by_name(self, tmp_path):
        with pytest.raises(StateError) as raised:
            delete_provider(fresh(tmp_path), "openai")
        assert "openai" in str(raised.value)

    def test_deleting_one_leaves_the_others(self, tmp_path):
        state = add_provider(fresh(tmp_path), "openai", "sk-a")
        state = add_provider(state, "deepseek", "sk-b")
        state = delete_provider(state, "openai")
        assert list(state.providers) == ["deepseek"]


class TestWritingItDown:
    """The file itself: what it holds and who may read it."""

    def test_what_was_saved_is_what_is_read_back(self, tmp_path):
        state, unused_record = add_token(fresh(tmp_path), "lmr_first", label="laptop")
        state = add_provider(set_auth_enabled(state, True), "openai", "sk-test")
        save_state(state)
        loaded = load_state(state.state_path)
        assert loaded.auth_enabled is True
        assert loaded.tokens == state.tokens
        assert loaded.providers == state.providers
        assert loaded.next_token_id == state.next_token_id

    def test_nobody_else_may_read_it(self, tmp_path):
        """It holds every caller token and every provider key in the clear."""
        state = fresh(tmp_path)
        save_state(state)
        assert state.state_path.stat().st_mode & 0o777 == 0o600

    def test_the_directory_is_made_if_it_is_not_there(self, tmp_path):
        state = load_state(tmp_path / "new" / "state.json")
        save_state(state)
        assert state.state_path.exists()

    def test_no_half_written_file_is_left_behind(self, tmp_path):
        """The write goes to a sibling temp file and is renamed over the real
        one, so a crash mid-save cannot leave a truncated key ring."""
        directory = tmp_path / "dir"
        save_state(load_state(directory / "state.json"))
        assert [path.name for path in directory.iterdir()] == ["state.json"]

    def test_the_secret_is_never_on_disk_world_readable(
        self, tmp_path, monkeypatch, permissive_umask
    ):
        """A chmod after the write is too late: under a default umask the temp
        file holds every token at 0644 until the two calls are apart, and a
        crash in between leaves it that way for good. Sampled at the moment the
        bytes land, not after the rename."""
        state, unused_record = add_token(fresh(tmp_path), "lmr_a_secret_token_value")
        observed = []
        real_replace = os.replace

        def sample(source, destination):
            observed.append(Path(source).stat().st_mode & 0o777)
            return real_replace(source, destination)

        monkeypatch.setattr(os, "replace", sample)
        save_state(state)
        assert observed == [0o600]

    def test_two_writers_do_not_share_one_temp_file(self, tmp_path):
        """A fixed `state.json.tmp` is one file two saves open with O_TRUNC, and
        the loser can be told the write failed for content that landed while the
        winner reports success for content that did not."""
        state, unused_record = add_token(fresh(tmp_path), "lmr_a")
        names = []
        real_replace = os.replace

        def sample(source, destination):
            names.append(Path(source).name)
            return real_replace(source, destination)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "replace", sample)
            save_state(state)
            save_state(state)
        assert names[0] != names[1]

    def test_it_ends_with_a_newline(self, tmp_path):
        """It is read by people and by `git diff` as often as by lmrelay."""
        state = fresh(tmp_path)
        save_state(state)
        assert state.state_path.read_text(encoding="utf-8").endswith("\n")


class TestRefusingAStateItCannotUse:
    """Each refusal names the file, because the operator has to go and look."""

    def test_a_file_that_is_not_json(self, tmp_path):
        target = tmp_path / "state.json"
        target.write_text("{not json", encoding="utf-8")
        with pytest.raises(StateError) as raised:
            load_state(target)
        assert str(target) in str(raised.value)

    def test_a_providers_entry_that_is_not_a_table(self, tmp_path):
        """Refused here as the StateError naming the file that every command is
        written to report, rather than reaching the upstream parser and coming
        out of `status` as an AttributeError traceback."""
        target = tmp_path / "state.json"
        target.write_text(
            json.dumps({"version": STATE_VERSION, "providers": {"openai": "sk-oops"}}),
            encoding="utf-8",
        )
        with pytest.raises(StateError) as raised:
            load_state(target)
        assert str(target) in str(raised.value)

    def test_a_file_written_by_a_newer_lmrelay(self, tmp_path):
        """Reading it as though the fields meant what they mean here would drop
        whatever the newer version added on the next save."""
        target = tmp_path / "state.json"
        target.write_text(json.dumps({"version": STATE_VERSION + 1}), encoding="utf-8")
        with pytest.raises(StateError):
            load_state(target)
