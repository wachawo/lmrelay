#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The transfer bundle: what an export carries, and everything an import refuses."""

import io
import json
import logging
import tomllib

import pytest

# Local imports
from lmrelay.bundle import BUNDLE_VERSION, LIMIT_TYPES, SERVER_TYPES, STDIO_PATH
from lmrelay.cli import build_parser
from lmrelay.config import CONFIG_ENV_VAR, SERVER_KEYS, RelayConfig, load_config
from lmrelay.errors import BundleError, LmrelayError
from lmrelay.ratelimit import LIMIT_KEYS, SCOPES
from lmrelay.state import MASKED_TOKEN, STATE_ENV_VAR, load_state, state_path_for

# A relay worth moving: a bind that is not the default, every limit scope, a
# credential in the file, one in the environment, one from `token gen`, and an
# upstream whose key is a ${VAR} the target machine will not have.
SOURCE_CONFIG = """
# A comment of the operator's own, which the bundle does not carry.
[server]
host             = "0.0.0.0"
port             = 11500
default_upstream = "ollama"
connect_timeout  = 25
log_level        = "DEBUG"

[limits.per_token]
requests = 2
period   = "120s"

[limits.total]
requests = 6
period   = "30m"

[auth]
token = "token-from-the-file"

[upstream.ollama]
base_url = "http://127.0.0.1:11434"
dialect  = "ollama"

[upstream.anthropic]
base_url = "https://api.anthropic.com"
dialect  = "anthropic"
headers  = { "x-api-key" = "${ANTHROPIC_KEY}", "anthropic-version" = "2023-06-01" }
"""

MINIMAL_CONFIG = """
[upstream.ollama]
base_url = "http://127.0.0.1:11434"
dialect  = "ollama"
"""

PROVIDER_KEY = "sk-openai-live"
FILE_TOKEN   = "token-from-the-file"
# A $ in it on purpose: a key run back through Template would come out as an
# environment variable's value, and that value would go to the provider.
ANTHROPIC_KEY = "sk-ant-with-a-$dollar-in-it"


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """No test here may find the operator's own config, state or credentials."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    monkeypatch.delenv(STATE_ENV_VAR, raising=False)


def run_command(argv: list[str]) -> None:
    """Parse and dispatch exactly as main() does, minus its exit handling."""
    args = build_parser().parse_args(argv)
    args.handler(args)


def write_config(directory, body: str):
    """Put a config in a directory of its own, so the state lands beside it."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "lmrelay.toml"
    target.write_text(body, encoding="utf-8")
    return target


@pytest.fixture
def source(tmp_path, monkeypatch):
    """A configured relay: two credentials, a CLI-added provider, limits set."""
    config_path = write_config(tmp_path / "source", SOURCE_CONFIG)
    monkeypatch.setenv("ANTHROPIC_KEY", ANTHROPIC_KEY)
    run_command(["provider", "add", "openai", PROVIDER_KEY, "--config", str(config_path)])
    run_command(["token", "add", "lmr_generated", "--label", "laptop",
                 "--config", str(config_path)])
    run_command(["auth", "true", "--config", str(config_path)])
    return config_path


@pytest.fixture
def bundle_path(tmp_path):
    """Where an export is written."""
    return tmp_path / "relay.toml"


@pytest.fixture
def target(tmp_path):
    """An empty directory, standing in for the machine being moved to."""
    destination = tmp_path / "target"
    destination.mkdir()
    return destination / "lmrelay.toml"


def export(source_path, bundle_path, *flags) -> dict:
    """Export from a config and hand back what landed on disk."""
    run_command(["export", str(bundle_path), "--config", str(source_path), *flags])
    return tomllib.loads(bundle_path.read_text(encoding="utf-8"))


def leave_the_source_machine(monkeypatch) -> None:
    """Drop the environment the exported relay ran with.

    An import happens somewhere else, and somewhere else is exactly where
    ${ANTHROPIC_KEY} is not set. A bundle that reproduced the relay only with
    it is a bundle that reproduces nothing.
    """
    monkeypatch.delenv("ANTHROPIC_KEY", raising=False)


def effective(config: RelayConfig) -> dict:
    """The relay a config describes, without the paths it happens to live at.

    auth_tokens is sorted rather than compared in order: it is the set of
    credentials a caller may present, and nothing reads it positionally.
    """
    return {
        "server": {name: getattr(config, name) for name in SERVER_KEYS},
        "limits": config.limits,
        "auth_enabled": config.auth_enabled,
        "auth_tokens": tuple(sorted(config.auth_tokens)),
        "upstream": {
            name: (upstream.base_url, upstream.dialect, upstream.headers)
            for name, upstream in sorted(config.upstreams.items())
        },
    }


def as_toml(data: dict, prefix: str = "") -> str:
    """Render any dict as TOML, including things the real writer would refuse.

    Deliberately not `bundle.render_bundle`: these tests hand an import the
    values a hand edit produces, an unknown table, a `nan`, a port spelled as a
    word, and a writer that refused them could not write the test.
    """
    scalars, tables = {}, {}
    for key, value in data.items():
        (tables if isinstance(value, dict) else scalars)[key] = value

    lines = []
    for key, value in scalars.items():
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            continue
        lines.append(f"{json.dumps(key)} = {toml_scalar(value)}")
    for key, value in tables.items():
        title = f"{prefix}{json.dumps(key)}"
        if all(isinstance(inner, dict) for inner in value.values()) and value:
            lines.append(as_toml(value, prefix=f"{title}."))
        else:
            lines.append(f"[{title}]\n" + as_toml(value, prefix=f"{title}."))
    for key, value in data.items():
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            for item in value:
                lines.append(f"[[{prefix}{json.dumps(key)}]]\n" + as_toml(item))
    return "\n".join(line for line in lines if line) + "\n"


def toml_scalar(value) -> str:
    """One value as TOML, nan and inf included."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_scalar(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = ", ".join(f"{json.dumps(k)} = {toml_scalar(v)}" for k, v in value.items())
        return f"{{ {pairs} }}"
    return json.dumps(value)


def edited(bundle_path, **changes):
    """Rewrite a bundle on disk, for the hand-edit an import has to refuse."""
    data = tomllib.loads(bundle_path.read_text(encoding="utf-8"))
    data.update(changes)
    bundle_path.write_text(as_toml(data), encoding="utf-8")
    return bundle_path


class TestTheRoundTrip:
    """Export here, import there, and the relay is the same relay."""

    def test_the_imported_relay_is_the_exported_one(
        self, source, bundle_path, target, monkeypatch
    ):
        """The whole point of the format, and the test that keeps the three
        parts honest as they change: same bind, same limits, same upstreams with
        the same headers, same credentials, same auth switch."""
        export(source, bundle_path)
        before = effective(load_config(source))
        leave_the_source_machine(monkeypatch)
        run_command(["import", str(bundle_path), "--config", str(target)])
        assert effective(load_config(target)) == before

    def test_a_header_read_from_the_environment_survives_the_move(
        self, source, bundle_path, target, monkeypatch
    ):
        """${ANTHROPIC_KEY} means nothing on a host that does not export it, so
        the bundle carries what it expanded to. Exporting the source instead
        would produce a bundle that reproduces the relay only on a machine with
        the same environment, which is the one thing guaranteed to differ."""
        export(source, bundle_path)
        leave_the_source_machine(monkeypatch)
        run_command(["import", str(bundle_path), "--config", str(target)])
        headers = load_config(target).upstreams["anthropic"].headers
        assert headers["x-api-key"] == ANTHROPIC_KEY

    def test_a_dollar_in_a_key_is_not_expanded_on_the_way_back_in(
        self, source, bundle_path, target, monkeypatch
    ):
        """The value in a bundle is already the finished one. Running it through
        Template again would rewrite a key containing a $ with the value of an
        environment variable, and send that to the provider."""
        monkeypatch.setenv("dollar", "SOMETHING-ELSE")
        export(source, bundle_path)
        leave_the_source_machine(monkeypatch)
        run_command(["import", str(bundle_path), "--config", str(target)])
        assert "$dollar" in load_config(target).upstreams["anthropic"].headers["x-api-key"]

    def test_a_credential_from_the_file_still_works_after_the_move(
        self, source, bundle_path, target, monkeypatch
    ):
        """[auth] token has no token record, and leaving it out would import a
        relay that refuses a caller the exported one served."""
        export(source, bundle_path)
        leave_the_source_machine(monkeypatch)
        run_command(["import", str(bundle_path), "--config", str(target)])
        tokens = load_config(target).auth_tokens
        assert FILE_TOKEN in tokens

    def test_a_token_keeps_the_id_that_was_printed_for_it(
        self, source, bundle_path, target, monkeypatch
    ):
        """`token list` prints ids, and an operator's notes name them."""
        before = {token.id: token.token for token in load_state(state_path_for(source)).tokens}
        export(source, bundle_path)
        leave_the_source_machine(monkeypatch)
        run_command(["import", str(bundle_path), "--config", str(target)])
        after = {token.id: token.token for token in load_state(state_path_for(target)).tokens}
        assert before.items() <= after.items()

    def test_the_next_token_carries_on_from_the_highest_id(
        self, source, bundle_path, target, monkeypatch
    ):
        """An id printed by `token list` must never come to name a second token."""
        export(source, bundle_path)
        leave_the_source_machine(monkeypatch)
        run_command(["import", str(bundle_path), "--config", str(target)])
        state = load_state(state_path_for(target))
        assert state.next_token_id > max(token.id for token in state.tokens)

    def test_the_comments_in_the_source_config_are_not_carried(
        self, source, bundle_path, target, monkeypatch
    ):
        """A bundle is a transfer format, not a copy of the file. The notes in
        the exporting operator's lmrelay.toml are theirs."""
        export(source, bundle_path)
        leave_the_source_machine(monkeypatch)
        run_command(["import", str(bundle_path), "--config", str(target)])
        assert "A comment of the operator's own" not in target.read_text(encoding="utf-8")

    def test_a_bundle_can_be_piped_from_one_relay_into_another(
        self, source, bundle_path, target, monkeypatch, capsys
    ):
        """'-' on both verbs, so moving a relay is one line and never has to
        leave a file full of keys lying about."""
        run_command(["export", STDIO_PATH, "--config", str(source)])
        bundle_path.write_text(capsys.readouterr().out, encoding="utf-8")
        leave_the_source_machine(monkeypatch)
        monkeypatch.setattr("sys.stdin", bundle_path.open(encoding="utf-8"))
        run_command(["import", STDIO_PATH, "--config", str(target)])
        assert load_config(target).port == 11500


class TestExportWritesNowhereItWouldRuin:
    """The one verb in the set that used to overwrite in silence."""

    def test_it_will_not_write_over_the_state_file_it_just_read(self, source, tmp_path):
        """Measured before the guard: the bundle landed on state.json, the
        command reported success, and the relay lost every caller token, every
        CLI-added provider and the auth switch in one line. The file was still
        readable to load_state, which found nothing in it and turned auth off
        without a word, on a relay that was still running."""
        with pytest.raises(BundleError) as raised:
            run_command(["export", str(state_path_for(source)),
                         "--config", str(source)])
        assert "state file" in str(raised.value)
        assert load_state(state_path_for(source)).tokens

    def test_nor_over_the_config_file(self, source):
        with pytest.raises(BundleError, match="config file"):
            run_command(["export", str(source), "--config", str(source)])
        assert "[upstream.ollama]" in source.read_text(encoding="utf-8")

    def test_and_a_relative_spelling_of_them_is_the_same_file(
        self, source, monkeypatch
    ):
        """`cd ~/.lmrelay && lmrelay config export state.json` names the same
        file as the absolute path does, and the check has to know that."""
        monkeypatch.chdir(source.parent)
        with pytest.raises(BundleError, match="state file"):
            run_command(["export", "state.json", "--config", str(source)])

    def test_any_other_file_that_exists_is_refused_until_asked_twice(
        self, source, bundle_path
    ):
        """Symmetric with `init` and with `config import`, which both refuse to
        overwrite what is already there."""
        bundle_path.write_text("not mine to lose\n", encoding="utf-8")
        with pytest.raises(BundleError) as raised:
            run_command(["export", str(bundle_path), "--config", str(source)])
        assert "--force" in str(raised.value)
        assert bundle_path.read_text(encoding="utf-8") == "not mine to lose\n"

    def test_and_force_then_replaces_it(self, source, bundle_path):
        bundle_path.write_text("stale\n", encoding="utf-8")
        export(source, bundle_path, "--force")
        assert tomllib.loads(bundle_path.read_text(encoding="utf-8"))["bundle_version"]

    def test_stdout_is_not_a_path_and_needs_no_permission(self, source, capsys):
        """'-' has nothing to overwrite, so the guard has nothing to say."""
        run_command(["export", STDIO_PATH, "--config", str(source)])
        assert tomllib.loads(capsys.readouterr().out)["bundle_version"] == BUNDLE_VERSION


class TestTheTerminalIsWhatNoPathMeans:
    """`lmrelay export | ssh there lmrelay import` is the whole of moving a relay."""

    def test_export_with_no_path_writes_the_bundle_to_stdout(self, source, capsys):
        run_command(["export", "--config", str(source)])
        assert tomllib.loads(capsys.readouterr().out)["bundle_version"] == BUNDLE_VERSION

    def test_and_every_word_about_it_to_stderr(self, source, capsys, caplog):
        """Otherwise the pipe carries the bundle and a sentence about the
        bundle, and what comes out the far end is not a bundle."""
        with caplog.at_level(logging.INFO):
            run_command(["export", "--config", str(source)])
        printed = capsys.readouterr()
        assert "Wrote standard output" not in printed.out
        assert "Wrote standard output" in caplog.text

    def test_secrets_on_a_terminal_are_said_louder(self, source, capsys, caplog):
        """A file at 0600 is one thing; the same bytes in a scrollback, and in
        any screenshot of it, is another. --no-secrets is named rather than
        described, so the fix is one thing to copy."""
        with caplog.at_level(logging.WARNING):
            run_command(["export", "--config", str(source)])
        assert "on your terminal" in caplog.text
        assert "--no-secrets" in caplog.text

    def test_and_not_when_it_went_to_a_file(self, source, bundle_path, caplog):
        with caplog.at_level(logging.WARNING):
            run_command(["export", str(bundle_path), "--config", str(source)])
        assert "on your terminal" not in caplog.text

    def test_import_with_no_path_reads_stdin(self, source, bundle_path, target, monkeypatch):
        export(source, bundle_path)
        leave_the_source_machine(monkeypatch)
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(bundle_path.read_text(encoding="utf-8"))
        )
        run_command(["import", "--config", str(target)])
        assert sorted(load_config(target).upstreams) == ["anthropic", "ollama", "openai"]

    def test_but_refuses_rather_than_waiting_when_nothing_is_piped_in(self, target, monkeypatch):
        """Reading a terminal is a command that hangs with no output, which
        reads as a relay that has locked up rather than as a missing argument."""
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
        with pytest.raises(BundleError) as raised:
            run_command(["import", "--config", str(target)])
        assert "no bundle to read" in str(raised.value)
        assert not target.exists()


class TestWhatTheBundleHolds:
    """Config and state together, at their effective values."""

    def test_it_says_which_lmrelay_wrote_it_and_when(self, source, bundle_path):
        """bundle_version is load bearing; written_by is for the person reading
        a bundle six months later."""
        data = export(source, bundle_path)
        assert data["bundle_version"] == BUNDLE_VERSION
        assert data["written_by"].startswith("lmrelay ") and data["exported_at"].endswith("Z")

    def test_every_limit_scope_is_in_it_including_the_ones_left_off(self, source, bundle_path):
        """Three scopes, the same three keys in each. A scope omitted because it
        was zero is one the importing operator cannot see they have."""
        limits = export(source, bundle_path)["limits"]
        assert set(limits) == set(SCOPES)
        assert all(set(scope) == set(LIMIT_KEYS) for scope in limits.values())

    def test_an_upstream_from_the_file_and_one_from_the_cli_are_both_in_it(
        self, source, bundle_path
    ):
        """A bundle without the CLI-added providers reproduces a relay with no
        providers, and one without the file's reproduces half of one."""
        upstreams = export(source, bundle_path)["upstream"]
        assert set(upstreams) == {"anthropic", "ollama", "openai"}
        assert upstreams["openai"]["headers"]["Authorization"] == f"Bearer {PROVIDER_KEY}"

    def test_the_auth_switch_travels_with_the_tokens(self, source, bundle_path):
        """A bundle carrying credentials but not the decision to require them
        imports a relay that is open, which is not the one that was exported."""
        assert export(source, bundle_path)["auth"]["enabled"] is True

    def test_the_file_is_readable_only_by_its_owner(self, source, bundle_path):
        """It holds every caller token and every provider key in clear, so 0600
        from creation rather than from a chmod after the write."""
        export(source, bundle_path)
        assert bundle_path.stat().st_mode & 0o777 == 0o600

    def test_the_command_says_what_it_just_wrote_down(self, source, bundle_path, caplog):
        """This is a file people attach to an issue without thinking."""
        with caplog.at_level(logging.INFO):
            export(source, bundle_path)
        assert "caller token" in caplog.text and "in clear" in caplog.text


class TestLeavingTheSecretsOut:
    """--no-secrets, and what an import of one is honest about."""

    def test_no_secret_survives_the_masking(self, source, bundle_path):
        """The difference between a shareable config and a leaked key."""
        export(source, bundle_path, "--no-secrets")
        written = bundle_path.read_text(encoding="utf-8")
        for secret in (PROVIDER_KEY, ANTHROPIC_KEY, FILE_TOKEN, "lmr_generated"):
            assert secret not in written

    def test_everything_that_is_not_a_secret_still_is(self, source, bundle_path):
        """It has to remain a config, or the flag is just a delete."""
        data = export(source, bundle_path, "--no-secrets")
        assert data["server"]["port"] == 11500
        assert data["upstream"]["openai"]["base_url"] == "https://api.openai.com"
        assert data["auth"]["tokens"][0]["label"] == "laptop"

    def test_importing_one_takes_everything_else(self, source, bundle_path, target, monkeypatch):
        """Reporting beats refusing: the settings and the upstream list are
        worth having, and the keys are two commands away."""
        export(source, bundle_path, "--no-secrets")
        leave_the_source_machine(monkeypatch)
        run_command(["import", str(bundle_path), "--config", str(target)])
        assert load_config(target).port == 11500

    def test_and_a_masked_key_is_dropped_rather_than_sent_to_the_provider(
        self, source, bundle_path, target, monkeypatch
    ):
        """A header holding *** is forwarded, refused, and reads as a wrong key
        rather than as a bundle that was exported without one."""
        export(source, bundle_path, "--no-secrets")
        leave_the_source_machine(monkeypatch)
        run_command(["import", str(bundle_path), "--config", str(target)])
        assert load_config(target).upstreams["openai"].headers == {}

    def test_the_import_names_what_it_could_not_restore(
        self, source, bundle_path, target, monkeypatch, caplog
    ):
        """Rather than pretending. The operator is told which two commands
        replace what is missing."""
        export(source, bundle_path, "--no-secrets")
        leave_the_source_machine(monkeypatch)
        with caplog.at_level(logging.INFO):
            run_command(["import", str(bundle_path), "--config", str(target)])
        assert "caller token 1 (laptop)" in caplog.text
        assert "upstream openai header Authorization" in caplog.text
        assert "token gen" in caplog.text and "provider add" in caplog.text

    def test_and_warns_that_auth_is_on_with_nothing_to_present(
        self, source, bundle_path, target, monkeypatch, caplog
    ):
        """Auth on and no usable token refuses every request, the operator's own
        included, which is the same thing `token delete` warns about."""
        export(source, bundle_path, "--no-secrets")
        leave_the_source_machine(monkeypatch)
        with caplog.at_level(logging.INFO):
            run_command(["import", str(bundle_path), "--config", str(target)])
        assert "every request will now be refused" in caplog.text


class TestImportReplacesRatherThanMerges:
    """The relay after an import is the relay that was exported, and nothing else."""

    def test_an_existing_config_is_not_overwritten_without_being_asked(
        self, source, bundle_path, tmp_path
    ):
        """Symmetric with `lmrelay init`, which refuses to overwrite too."""
        export(source, bundle_path)
        occupied = write_config(tmp_path / "occupied", MINIMAL_CONFIG)
        with pytest.raises(BundleError) as raised:
            run_command(["import", str(bundle_path), "--config", str(occupied)])
        assert "--force" in str(raised.value)

    def test_and_is_left_exactly_as_it_was(self, source, bundle_path, tmp_path):
        occupied = write_config(tmp_path / "occupied", MINIMAL_CONFIG)
        export(source, bundle_path)
        with pytest.raises(BundleError):
            run_command(["import", str(bundle_path), "--config", str(occupied)])
        assert occupied.read_text(encoding="utf-8") == MINIMAL_CONFIG

    def test_force_moves_the_old_pair_aside_first(self, source, bundle_path, tmp_path):
        occupied = write_config(tmp_path / "occupied", MINIMAL_CONFIG)
        run_command(["token", "add", "lmr_was_here", "--config", str(occupied)])
        export(source, bundle_path)
        run_command(["import", str(bundle_path), "--config", str(occupied), "--force"])
        backup = occupied.with_name(occupied.name + ".bak")
        assert backup.read_text(encoding="utf-8") == MINIMAL_CONFIG
        assert state_path_for(occupied).with_name("state.json.bak").exists()

    def test_and_the_state_that_was_there_is_replaced_not_merged(
        self, source, bundle_path, tmp_path, monkeypatch
    ):
        """A merge produces a third relay that is neither the exported one nor
        the existing one, and nobody can predict it."""
        occupied = write_config(tmp_path / "occupied", MINIMAL_CONFIG)
        run_command(["token", "add", "lmr_was_here", "--config", str(occupied)])
        export(source, bundle_path)
        leave_the_source_machine(monkeypatch)
        run_command(["import", str(bundle_path), "--config", str(occupied), "--force"])
        assert "lmr_was_here" not in load_config(occupied).auth_tokens

    def test_a_backup_that_already_exists_is_never_written_over(
        self, source, bundle_path, tmp_path
    ):
        """The .bak from the previous import is exactly the file an operator
        would reach for, and accumulating timestamped copies instead would leave
        secrets lying about."""
        occupied = write_config(tmp_path / "occupied", MINIMAL_CONFIG)
        export(source, bundle_path)
        run_command(["import", str(bundle_path), "--config", str(occupied), "--force"])
        with pytest.raises(BundleError) as raised:
            run_command(["import", str(bundle_path), "--config", str(occupied),
                         "--force"])
        assert ".bak" in str(raised.value)

    def test_the_operator_is_told_the_bind_needs_a_restart(
        self, source, bundle_path, target, monkeypatch, caplog
    ):
        """host, port and connect_timeout are read at startup only, and an
        import is the most likely single command to move all three."""
        export(source, bundle_path)
        leave_the_source_machine(monkeypatch)
        with caplog.at_level(logging.INFO):
            run_command(["import", str(bundle_path), "--config", str(target)])
        assert "lmrelay restart" in caplog.text
        # The same words every other mutating command uses.
        assert "applies at the next start" in caplog.text


class TestRefusingABundleItCannotApply:
    """Validated whole, before anything is written."""

    def test_one_from_a_newer_lmrelay(self, source, bundle_path, target):
        """Refused rather than partially read: a newer bundle may carry a
        setting this version does not enforce, and importing it would produce a
        relay that looks configured and is not."""
        export(source, bundle_path)
        with pytest.raises(BundleError) as raised:
            run_command(["import",
                         str(edited(bundle_path, bundle_version=BUNDLE_VERSION + 1)),
                         "--config", str(target)])
        assert "newer lmrelay" in str(raised.value)

    def test_one_that_does_not_say_what_it_is(self, tmp_path, target):
        """A TOML file is not a bundle, and bundle_version is what says it is:
        an lmrelay.toml handed to `import` by mistake parses perfectly."""
        stray = tmp_path / "notes.toml"
        stray.write_text('[server]\nport = 1\n', encoding="utf-8")
        with pytest.raises(BundleError, match="bundle_version"):
            run_command(["import", str(stray), "--config", str(target)])

    def test_one_written_by_a_build_that_wrote_json(self, tmp_path, target):
        """Named rather than reported as a syntax error. A TOML parser meets `{`
        and complains about line 1 column 1, which reads as a corrupt file
        rather than as one written in the format before this."""
        stray = tmp_path / "relay.json"
        stray.write_text('{"bundle_version": 1, "server": {"port": 11435}}', encoding="utf-8")
        with pytest.raises(BundleError) as raised:
            run_command(["import", str(stray), "--config", str(target)])
        assert "is JSON, and a bundle is TOML" in str(raised.value)
        assert not target.exists()

    def test_a_file_that_is_not_toml_at_all(self, tmp_path, target):
        stray = tmp_path / "notes.toml"
        stray.write_text("not toml", encoding="utf-8")
        with pytest.raises(BundleError, match="not TOML"):
            run_command(["import", str(stray), "--config", str(target)])

    def test_a_file_that_is_not_there(self, tmp_path, target):
        with pytest.raises(BundleError, match="cannot read"):
            run_command(["import", str(tmp_path / "absent.json"),
                         "--config", str(target)])

    def test_an_unknown_key_at_a_known_version(self, source, bundle_path, target):
        """At one version both ends agree on the key set, so an unknown key is a
        hand edit or a wrong bundle_version. Forward compatibility is what
        bundle_version is for, and it is one line we control at both ends."""
        export(source, bundle_path)
        with pytest.raises(BundleError) as raised:
            run_command(["import", str(edited(bundle_path, limitz={})),
                         "--config", str(target)])
        assert "limitz" in str(raised.value)

    def test_an_unknown_server_key(self, source, bundle_path, target):
        data = export(source, bundle_path)
        data["server"]["rate_limit"] = 20
        with pytest.raises(BundleError, match="rate_limit"):
            run_command(["import", str(edited(bundle_path, server=data["server"])),
                         "--config", str(target)])

    def test_an_unknown_limit_scope(self, source, bundle_path, target):
        data = export(source, bundle_path)
        data["limits"]["per_model"] = {"requests": 1}
        with pytest.raises(BundleError, match="per_model"):
            run_command(["import", str(edited(bundle_path, limits=data["limits"])),
                         "--config", str(target)])

    def test_a_value_the_config_could_not_load(self, source, bundle_path, target):
        """port = "eleven" parses as TOML and then refuses to start, which is a
        bundle that half applied. In the config's own words for that key, which
        says a whole number: told "not a number" about 2.5, an operator goes
        looking for a different mistake than the one they made."""
        data = export(source, bundle_path)
        data["server"]["port"] = "eleven"
        with pytest.raises(BundleError, match="not a whole number"):
            run_command(["import", str(edited(bundle_path, server=data["server"])),
                         "--config", str(target)])

    def test_a_whole_number_key_holding_a_fraction(self, source, bundle_path, target):
        """requests = 2.5 is a JSON number and not a count of requests."""
        data = export(source, bundle_path)
        data["limits"]["total"]["requests"] = 2.5
        with pytest.raises(BundleError, match="not a whole number"):
            run_command(["import", str(edited(bundle_path, limits=data["limits"])),
                         "--config", str(target)])

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), 30, "1d", "1.5m", ""])
    def test_a_period_that_is_not_a_duration(self, source, bundle_path, target, value):
        """The second value validator, beside log_level's, and for the same
        reason: each of these is the right JSON type or close enough to pass one,
        and each writes a pair of files the relay then refuses to start from, on
        a machine whose working config the import has already moved aside.

        `30` matters most: read as seconds it would be a limit nobody wrote, and
        half an hour to whoever did write it about as often as half a minute."""
        data = export(source, bundle_path)
        data["limits"]["total"]["period"] = value
        with pytest.raises(BundleError):
            run_command(["import", str(edited(bundle_path, limits=data["limits"])),
                         "--config", str(target)])

    def test_a_log_level_that_is_a_string_and_still_not_a_level(
        self, source, bundle_path, target
    ):
        """The type table cannot reach this one: "VERBOSE" is a string, and the
        pair of files written with it in them is a relay that refuses to start on
        every command, on a machine whose own config the import moved aside."""
        data = export(source, bundle_path)
        data["server"]["log_level"] = "VERBOSE"
        with pytest.raises(BundleError, match="not a logging level"):
            run_command(["import", str(edited(bundle_path, server=data["server"])),
                         "--config", str(target)])

    def test_a_default_upstream_it_only_defaults_to(self, source, bundle_path, target):
        """A hand-written bundle that leaves default_upstream out is a documented
        way to provision a machine, and one carrying a single upstream under any
        other name falls back to 'ollama' and cannot be loaded. The guard fired
        only when the key was present, so the defaulted case walked past it."""
        data = export(source, bundle_path)
        del data["server"]["default_upstream"]
        with pytest.raises(BundleError) as raised:
            run_command(["import",
                         str(edited(bundle_path, server=data["server"],
                                    upstream={"openai": data["upstream"]["openai"]})),
                         "--config", str(target)])
        assert "ollama" in str(raised.value) and "openai" in str(raised.value)

    def test_a_negative_limit(self, source, bundle_path, target):
        """0 is off, so a negative is a mistake rather than another spelling of
        it, and config.py refuses one for the same reason."""
        data = export(source, bundle_path)
        data["limits"]["total"]["requests"] = -1
        with pytest.raises(BundleError, match="negative"):
            run_command(["import", str(edited(bundle_path, limits=data["limits"])),
                         "--config", str(target)])

    def test_a_default_upstream_it_does_not_define(self, source, bundle_path, target):
        """Refused here rather than at the next start, so the import does not
        write a pair of files that cannot be loaded."""
        data = export(source, bundle_path)
        data["server"]["default_upstream"] = "typo"
        with pytest.raises(BundleError) as raised:
            run_command(["import", str(edited(bundle_path, server=data["server"])),
                         "--config", str(target)])
        assert "typo" in str(raised.value) and "ollama" in str(raised.value)

    def test_no_upstreams_at_all(self, source, bundle_path, target):
        export(source, bundle_path)
        with pytest.raises(BundleError, match="no upstreams"):
            run_command(["import", str(edited(bundle_path, upstream={})),
                         "--config", str(target)])

    def test_an_upstream_that_would_shadow_the_path_root(self, source, bundle_path, target):
        """Validated through the parser a hand-written table goes through, so a
        bundle and an lmrelay.toml fail in exactly the same ways. The refusal is
        that parser's ConfigError rather than a BundleError, which is the point:
        there is one validator, not a second one that could disagree with it."""
        data = export(source, bundle_path)
        data["upstream"]["v1"] = {"base_url": "http://x", "dialect": "openai"}
        with pytest.raises(LmrelayError, match="reserved"):
            run_command(["import", str(edited(bundle_path, upstream=data["upstream"])),
                         "--config", str(target)])

    def test_two_tokens_with_one_id(self, source, bundle_path, target):
        """An id printed by `token list` must never name two credentials."""
        data = export(source, bundle_path)
        data["auth"]["tokens"][1]["id"] = data["auth"]["tokens"][0]["id"]
        with pytest.raises(BundleError, match="two tokens"):
            run_command(["import", str(edited(bundle_path, auth=data["auth"])),
                         "--config", str(target)])

    def test_a_token_entry_with_no_token_in_it(self, source, bundle_path, target):
        data = export(source, bundle_path)
        data["auth"]["tokens"] = [{"id": 1, "label": "empty"}]
        with pytest.raises(BundleError, match="no token"):
            run_command(["import", str(edited(bundle_path, auth=data["auth"])),
                         "--config", str(target)])

    def test_an_auth_switch_that_is_not_a_switch(self, source, bundle_path, target):
        data = export(source, bundle_path)
        data["auth"]["enabled"] = "yes"
        with pytest.raises(BundleError, match="true or false"):
            run_command(["import", str(edited(bundle_path, auth=data["auth"])),
                         "--config", str(target)])

    @pytest.mark.parametrize("change", [
        {"bundle_version": BUNDLE_VERSION + 1},
        {"limitz": {}},
        {"upstream": {}},
        {"server": {"log_level": "VERBOSE"}},
        {"limits": {"total": {"period": "1d"}}},
    ])
    def test_and_none_of_them_writes_anything_at_all(
        self, source, bundle_path, target, change
    ):
        """An import that failed halfway would leave a relay configured by
        neither the bundle nor what was there before."""
        export(source, bundle_path)
        with pytest.raises(BundleError):
            run_command(["import", str(edited(bundle_path, **change)),
                         "--config", str(target)])
        assert not target.exists() and not state_path_for(target).exists()


class TestTheKeysCannotDrift:
    """The bundle spells the config's own keys, and a test says so."""

    def test_every_server_key_has_a_type_the_import_checks(self):
        """A key added to [server] and forgotten here would import as anything
        at all, and fail at the next start instead of at this suite."""
        assert set(SERVER_TYPES) == set(SERVER_KEYS)

    def test_and_so_does_every_limit_key(self):
        assert set(LIMIT_TYPES) == set(LIMIT_KEYS)

    def test_a_masked_value_is_the_one_the_state_module_already_uses(self):
        """One spelling of "not shown", so `token list` and a bundle agree."""
        assert MASKED_TOKEN == "***"
