"""Interactive local setup. Never start generation or transmit credentials during setup."""

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import tomllib
import warnings
from importlib.resources import files
from pathlib import Path

from . import config

VERSION = "0.4.0"
SOURCE = f"git+https://github.com/alexmeckes/reactor-mcp@v{VERSION}"
DOCS_URL = "https://docs.reactor.inc/mcp"


def confirm(message: str) -> bool:
    return input(f"{message} [y/N] ").strip().lower() in {"y", "yes"}


def client_entries(client: str) -> dict:
    """Inspect only user-scoped entries; do not launch servers or log configuration."""
    if client == "codex":
        path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
        parser, field = tomllib.loads, "mcp_servers"
    else:
        directory = os.environ.get("CLAUDE_CONFIG_DIR")
        path = Path(directory) / ".claude.json" if directory else Path.home() / ".claude.json"
        parser, field = json.loads, "mcpServers"
    if not path.exists():
        return {}
    try:
        entries = parser(path.read_text()).get(field, {})
        if not isinstance(entries, dict):
            raise ValueError
        return entries
    except (OSError, ValueError, AttributeError):
        raise config.ConfigError(
            f"Cannot read {client}'s user configuration. No entries changed."
        ) from None


def desired_entries(uvx: str) -> dict:
    inference = {
        "command": uvx,
        "args": ["--python", "3.12", "--from", SOURCE, "--no-env-file", "reactor-mcp", "serve"],
    }
    # Preserve explicit test/custom settings location in the launched server.
    if "REACTOR_CONFIG_DIR" in os.environ:
        inference["env"] = {"REACTOR_CONFIG_DIR": str(config.config_dir().resolve())}
    return {"reactor_inference": inference, "reactor_docs": {"url": DOCS_URL}}


def matches(existing: dict, desired: dict) -> bool:
    if not isinstance(existing, dict):
        return False
    # Claude Code persists stdio entries with an empty env, which changes nothing.
    existing = {k: v for k, v in existing.items() if not (k == "env" and v == {})}
    if any(existing.get(k) != v for k, v in desired.items()):
        return False
    # Extra environment/auth settings can change behavior; never silently accept them.
    allowed = set(desired) | {"type", "transport", "enabled"}
    return not (set(existing) - allowed) and existing.get("enabled", True) is not False


def registration_command(client: str, executable: str, name: str, entry: dict) -> list[str]:
    cmd = [executable, "mcp", "add"]
    if client == "claude":
        cmd += ["--scope", "user", "--transport", "http" if "url" in entry else "stdio"]
    cmd += [name]
    if "url" in entry:
        return cmd + (["--url"] if client == "codex" else []) + [entry["url"]]
    for key, value in entry.get("env", {}).items():
        cmd += ["--env", f"{key}={value}"]
    return cmd + ["--", entry["command"], *entry["args"]]


def register(client: str, executable: str, entries: dict) -> bool:
    current = client_entries(client)
    success = True
    for name, entry in entries.items():
        if name in current:
            if matches(current[name], entry):
                print(f"{client}: {name} already configured.")
            else:
                print(f"{client}: {name} already exists with different settings; left unchanged.")
                print(
                    f"Review it with `{client} mcp get {name}`; remove it if you want setup to replace it."
                )
                success = False
            continue
        try:
            result = subprocess.run(
                registration_command(client, executable, name, entry),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode:
                raise config.ConfigError("registration failed")
            # Verify persisted configuration without invoking any server or health check.
            if not matches(client_entries(client).get(name, {}), entry):
                raise config.ConfigError("configuration not confirmed")
            print(f"{client}: added {name}.")
        except (OSError, subprocess.TimeoutExpired, config.ConfigError):
            print(
                f"{client}: could not confirm {name}. Check `{client} mcp get {name}` and rerun setup."
            )
            success = False
    return success


def skill_text() -> str:
    bundled = files("reactor_mcp").joinpath("data/SKILL.md")
    if bundled.is_file():
        return bundled.read_text()
    # Editable source checkout; built wheels contain the same canonical skill above.
    return (Path(__file__).resolve().parents[2] / "skills/reactor-workflow/SKILL.md").read_text()


def install_skill(client: str) -> bool:
    root = Path.home() / (".agents" if client == "codex" else ".claude")
    if client == "claude" and os.environ.get("CLAUDE_CONFIG_DIR"):
        root = Path(os.environ["CLAUDE_CONFIG_DIR"])
    path = root / "skills/reactor-workflow/SKILL.md"
    content = skill_text()
    if path.exists():
        if path.read_text() == content:
            print(f"{client}: workflow skill already installed.")
            return True
        print(f"{client}: existing workflow skill differs; left unchanged: {path}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(content)
    print(f"{client}: installed workflow skill.")
    return True


def setup(client: str | None) -> int:
    if not sys.stdin.isatty():
        raise config.ConfigError(
            "Run setup in an interactive terminal; keys are never accepted as arguments."
        )
    client = (
        client or input("Connect Claude Code, Codex, or both? [claude/codex/both] ").strip().lower()
    )
    if client not in {"claude", "codex", "both"}:
        raise config.ConfigError("Choose claude, codex, or both.")
    clients = ["claude", "codex"] if client == "both" else [client]
    executables = {c: shutil.which(c) for c in clients}
    uvx = shutil.which("uvx")
    missing = [c for c, path in executables.items() if not path] + ([] if uvx else ["uv (uvx)"])
    if missing:
        raise config.ConfigError(f"Install first: {', '.join(missing)}. Then rerun setup.")
    # Check readable configs before collecting credentials or changing anything.
    for c in clients:
        client_entries(c)
    settings = config.read_settings()
    print("Setup adds two user-scoped MCP connections. No generation or paid test is run.")
    if sys.platform == "darwin":
        print("Paste your Reactor key into the hidden prompt. It stays in macOS Keychain.")
        print("Press Enter to keep a previously stored key, or skip credentials for docs-only use.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                key = getpass.getpass("Reactor API key: ").strip()
            except getpass.GetPassWarning:
                raise config.ConfigError(
                    "Hidden input is unavailable. Run setup in a real terminal."
                ) from None
        if key:
            config.save_key(key)
            settings["keychain"] = True
        del key
    else:
        print(
            "On this OS, supply REACTOR_API_KEY through your client environment; no key is saved."
        )
    settings["live_enabled"] = confirm(
        "Allow future inference requests to start paid Reactor sessions?"
    )
    settings.setdefault("output_dir", str(Path.home() / "Movies/Reactor"))
    config.write_settings(settings)
    if any(
        k in os.environ for k in ("REACTOR_ENABLE_LIVE", "REACTOR_API_KEY", "REACTOR_OUTPUT_DIR")
    ):
        print("Note: REACTOR_* environment overrides take precedence over saved settings.")
    entries = desired_entries(str(uvx))
    results = [register(c, str(executables[c]), entries) for c in clients]
    if confirm("Also install the Reactor workflow skill for these clients?"):
        results.extend(install_skill(c) for c in clients)
    print(f"Clips will be saved to {settings['output_dir']} (unless overridden by environment).")
    print("Restart your client, then ask: 'Check Reactor setup and search its docs about Helios.'")
    print("API access and generation are not verified. For local diagnostics run:")
    print(f"uvx --python 3.12 --from {SOURCE} reactor-mcp doctor")
    return 0 if all(results) else 1


def doctor() -> int:
    from .server import create_server
    from .service import Service

    service = Service()
    create_server(service)  # Validate server construction without opening a transport or session.
    info = service.describe()
    print(f"Reactor MCP {VERSION}: server imports and tool registration OK.")
    print(f"Live generation: {'enabled' if info['live_enabled'] else 'disabled'}")
    print(f"Credential configuration: {'present' if info['api_key_configured'] else 'missing'}")
    print("Keychain was not opened. Credential validity and network access are not checked.")
    for client in ("claude", "codex"):
        if not shutil.which(client):
            print(f"{client}: CLI not on PATH.")
            continue
        entries = client_entries(client)
        for name in ("reactor_inference", "reactor_docs"):
            print(f"{client}: {name} {'entry present' if name in entries else 'entry missing'}")
    print(f"Clip directory: {info['output_dir']}")
    print("No API requests or paid sessions were started.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reactor inference MCP and local client setup")
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="Run stdio MCP (default)")
    wizard = commands.add_parser(
        "setup", help="Connect Claude Code/Codex and securely store your key"
    )
    wizard.add_argument("--client", choices=["claude", "codex", "both"])
    commands.add_parser("doctor", help="Check local setup without network calls or Keychain access")
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            return setup(args.client)
        if args.command == "doctor":
            return doctor()
        from .server import main as serve

        serve()
        return 0
    except config.ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    except OSError:
        # OS errors can contain paths and subprocess data; never dump raw exceptions.
        print(
            "Local file or process operation failed. Check permissions and rerun.", file=sys.stderr
        )
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Cancelled. Completed setup steps remain; rerun to finish.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
