import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from reactor_mcp import cli, config
from reactor_mcp.backend import ReactorFailure
from reactor_mcp.service import Service


@pytest.fixture(autouse=True)
def no_real_keychain(monkeypatch):
    monkeypatch.setattr(
        config, "keychain", Mock(side_effect=AssertionError("Real Keychain forbidden"))
    )


def test_settings_are_private_and_never_persist_arbitrary_fields():
    config.write_settings({"live_enabled": False, "keychain": True, "api_key": "not-a-secret"})
    path = config.config_dir() / "settings.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert config.read_settings() == {"live_enabled": False, "keychain": True}
    assert "not-a-secret" not in path.read_text()
    config.write_settings({"live_enabled": True})
    assert config.read_settings() == {"live_enabled": True}


@pytest.mark.parametrize("text", ["[]", "{invalid", '{"live_enabled":"true"}', '{"output_dir":3}'])
def test_corrupt_settings_fail_clearly(text):
    config.config_dir().mkdir()
    (config.config_dir() / "settings.json").write_text(text)
    with pytest.raises(config.ConfigError, match="Cannot read"):
        config.read_settings()


def test_keychain_is_lazy_and_environment_wins(monkeypatch):
    vault = Mock()
    vault.get_password.return_value = "fake-key"
    monkeypatch.setattr(config, "keychain", lambda: vault)
    config.write_settings({"keychain": True, "live_enabled": True})
    assert Service().describe()["api_key_configured"]
    vault.get_password.assert_not_called()
    assert not config.live_enabled()  # Fixture's explicit environment false beats stored true.
    monkeypatch.delenv("REACTOR_ENABLE_LIVE")
    assert config.live_enabled()
    assert config.api_key() == "fake-key"
    vault.get_password.assert_called_once_with(config.SERVICE, config.ACCOUNT)
    monkeypatch.setenv("REACTOR_API_KEY", "")
    assert config.api_key() == ""
    assert not config.key_configured()
    monkeypatch.setenv("REACTOR_API_KEY", "environment-key")
    assert config.api_key() == "environment-key"
    assert vault.get_password.call_count == 1


def test_keychain_errors_redacted_and_disabled_does_not_read_key(monkeypatch):
    config.write_settings({"keychain": True})
    monkeypatch.setattr(config, "keychain", Mock(side_effect=RuntimeError("fake-sensitive-data")))
    with pytest.raises(ReactorFailure, match="LIVE_DISABLED"):
        Service().start("forest")
    config.keychain.assert_not_called()
    monkeypatch.setenv("REACTOR_ENABLE_LIVE", "true")
    with pytest.raises(ReactorFailure, match="CREDENTIALS_UNAVAILABLE") as error:
        Service().start("forest")
    assert "fake-sensitive-data" not in str(error.value)
    with pytest.raises(config.ConfigError, match="no file fallback"):
        config.save_key("test-key")
    assert not list(config.config_dir().glob("*key*"))


def test_missing_keychain_item_is_missing_key(monkeypatch):
    config.write_settings({"keychain": True})
    monkeypatch.setenv("REACTOR_ENABLE_LIVE", "true")
    monkeypatch.setattr(config, "keychain", lambda: SimpleNamespace(get_password=lambda *_: None))
    with pytest.raises(ReactorFailure, match="MISSING_API_KEY"):
        Service().start("forest")


async def test_saved_key_and_opt_in_drive_mock_inference(monkeypatch, tmp_path):
    from conftest import FakeBackend, ready

    config.write_settings({"keychain": True, "live_enabled": True})
    monkeypatch.delenv("REACTOR_ENABLE_LIVE")
    monkeypatch.setattr(
        config, "keychain", lambda: SimpleNamespace(get_password=lambda *_: "mock-key")
    )
    backend = FakeBackend()
    received = []

    def factory(key, seconds):
        received.append((key, seconds))
        return backend

    service = Service(factory, tmp_path)
    handle = await ready(service)
    assert received == [("mock-key", 60)]
    assert service.status(handle)["phase"] == "ready"
    await service.close(handle)
    assert backend.disconnected == 1


@pytest.fixture
def interactive(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "fake-key-for-test-only")
    monkeypatch.setattr("builtins.input", lambda _: "n")


def test_wizard_both_clients_secret_never_in_arguments_or_files(interactive, monkeypatch, capsys):
    saved = []
    monkeypatch.setattr(config, "save_key", saved.append)
    registry = {"claude": {}, "codex": {}}
    monkeypatch.setattr(cli, "client_entries", lambda c: registry[c])
    entries = cli.desired_entries("/tools/uvx")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        client = Path(command[0]).name
        name = next(n for n in entries if n in command)
        registry[client][name] = entries[name]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli.setup("both") == 0
    assert saved == ["fake-key-for-test-only"]
    assert len(calls) == 4
    assert not config.read_settings()["live_enabled"]
    assert config.read_settings()["keychain"]
    assert "fake-key-for-test-only" not in repr(calls)
    assert "fake-key-for-test-only" not in capsys.readouterr().out
    assert "fake-key-for-test-only" not in (config.config_dir() / "settings.json").read_text()
    # Safe rerun skips exactly matching entries rather than overwriting.
    assert cli.setup("both") == 0
    assert len(calls) == 4


def test_blank_key_keeps_existing_and_live_requires_opt_in(interactive, monkeypatch):
    config.write_settings({"keychain": True, "live_enabled": True})
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "")
    monkeypatch.setattr(cli, "register", lambda *args: True)
    assert cli.setup("codex") == 0
    assert config.read_settings()["keychain"]
    assert not config.read_settings()["live_enabled"]


def test_failed_key_save_does_not_register_or_write_settings(interactive, monkeypatch):
    register = Mock()
    monkeypatch.setattr(cli, "register", register)
    assert cli.main(["setup", "--client", "claude"]) == 1
    register.assert_not_called()
    assert not (config.config_dir() / "settings.json").exists()


def test_noninteractive_setup_refuses_before_reading_key(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    getpass = Mock()
    monkeypatch.setattr(cli.getpass, "getpass", getpass)
    assert cli.main(["setup", "--client", "codex"]) == 1
    getpass.assert_not_called()


def test_missing_client_fails_before_credentials(interactive, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)
    prompt = Mock()
    monkeypatch.setattr(cli.getpass, "getpass", prompt)
    assert cli.main(["setup", "--client", "both"]) == 1
    prompt.assert_not_called()


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_conflict_never_overwrites(client, monkeypatch):
    monkeypatch.setattr(
        cli, "client_entries", lambda _: {"reactor_docs": {"url": "https://other.test"}}
    )
    run = Mock()
    monkeypatch.setattr(cli.subprocess, "run", run)
    assert not cli.register(client, client, {"reactor_docs": {"url": cli.DOCS_URL}})
    run.assert_not_called()


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "not-persisted"])
def test_registration_failures_are_redacted(failure, monkeypatch, capsys):
    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("sensitive-command", 30, output="sensitive-output")
        return SimpleNamespace(
            returncode=1 if failure == "nonzero" else 0, stderr="sensitive-output"
        )

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert not cli.register("codex", "codex", {"reactor_docs": {"url": cli.DOCS_URL}})
    assert "sensitive" not in capsys.readouterr().out


def test_registration_commands():
    entries = cli.desired_entries("/abs/uvx")
    assert cli.registration_command(
        "claude", "claude", "reactor_docs", entries["reactor_docs"]
    ) == [
        "claude",
        "mcp",
        "add",
        "--scope",
        "user",
        "--transport",
        "http",
        "reactor_docs",
        cli.DOCS_URL,
    ]
    assert cli.registration_command("codex", "codex", "reactor_docs", entries["reactor_docs"]) == [
        "codex",
        "mcp",
        "add",
        "reactor_docs",
        "--url",
        cli.DOCS_URL,
    ]
    stdio = cli.registration_command(
        "claude", "claude", "reactor_inference", entries["reactor_inference"]
    )
    assert stdio[stdio.index("--") + 1] == "/abs/uvx"
    assert "--no-env-file" in stdio
    assert cli.SOURCE in stdio
    assert not any("API_KEY" in part for part in stdio)


def test_client_config_reading_and_custom_locations(monkeypatch, tmp_path):
    codex_dir = Path(os.environ["CODEX_HOME"])
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        '[mcp_servers.reactor_docs]\nurl = "https://docs.reactor.inc/mcp"\n'
    )
    assert cli.client_entries("codex")["reactor_docs"] == {"url": cli.DOCS_URL}
    claude_dir = tmp_path / "alternate-claude"
    claude_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    (claude_dir / ".claude.json").write_text(
        json.dumps({"mcpServers": {"test": {"command": "test"}}})
    )
    assert cli.client_entries("claude") == {"test": {"command": "test"}}
    (claude_dir / ".claude.json").write_text("{bad")
    with pytest.raises(config.ConfigError, match="No entries changed"):
        cli.client_entries("claude")


@pytest.mark.parametrize("client,folder", [("codex", ".agents"), ("claude", ".claude")])
def test_skill_install_is_repeatable_and_preserves_edits(client, folder):
    assert cli.install_skill(client)
    assert cli.install_skill(client)
    path = Path.home() / folder / "skills/reactor-workflow/SKILL.md"
    assert "name: reactor-workflow" in path.read_text()
    path.write_text("user-customized")
    assert not cli.install_skill(client)
    assert path.read_text() == "user-customized"


def test_doctor_never_reads_keychain_or_starts_inference(monkeypatch, capsys):
    config.write_settings({"keychain": True, "live_enabled": True})
    monkeypatch.setattr(Service, "start", Mock(side_effect=AssertionError("Generation forbidden")))
    assert cli.main(["doctor"]) == 0
    assert "not checked" in capsys.readouterr().out
    config.keychain.assert_not_called()


@pytest.mark.parametrize("argv", [[], ["serve"]])
def test_default_command_preserves_stdio(argv, monkeypatch):
    from reactor_mcp import server

    serve = Mock()
    monkeypatch.setattr(server, "main", serve)
    assert cli.main(argv) == 0
    serve.assert_called_once_with()
