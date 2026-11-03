"""Non-secret settings and explicitly selected macOS Keychain storage."""

import json
import os
import sys
import tempfile
from pathlib import Path

SERVICE = "reactor-inference-mcp"
ACCOUNT = "api-key"


class ConfigError(Exception):
    pass


def config_dir() -> Path:
    return Path(os.environ.get("REACTOR_CONFIG_DIR", "~/.config/reactor-mcp")).expanduser()


def read_settings() -> dict:
    path = config_dir() / "settings.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError
        for key in ("keychain", "live_enabled"):
            if key in value and type(value[key]) is not bool:
                raise ValueError
        if "output_dir" in value and not isinstance(value["output_dir"], str):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise ConfigError("Cannot read Reactor settings.json; repair it or rerun setup.") from None


def write_settings(settings: dict) -> None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Only these non-secret fields can ever be persisted.
    clean = {k: settings[k] for k in ("keychain", "live_enabled", "output_dir") if k in settings}
    fd, name = tempfile.mkstemp(dir=directory, prefix=".settings-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(clean, stream, indent=2)
            stream.write("\n")
        os.replace(name, directory / "settings.json")
    finally:
        Path(name).unlink(missing_ok=True)


def keychain():
    if sys.platform != "darwin":
        raise ConfigError("Keychain setup requires macOS. Elsewhere use REACTOR_API_KEY.")
    # Never select an automatically discovered or plaintext fallback backend.
    from keyring.backends.macOS import Keyring

    return Keyring()


def save_key(value: str) -> None:
    try:
        keychain().set_password(SERVICE, ACCOUNT, value)
    except Exception:
        raise ConfigError(
            "Could not save the key to macOS Keychain; no file fallback was used."
        ) from None


def api_key() -> str:
    if "REACTOR_API_KEY" in os.environ:
        return os.environ["REACTOR_API_KEY"].strip()
    if not read_settings().get("keychain"):
        return ""
    try:
        return (keychain().get_password(SERVICE, ACCOUNT) or "").strip()
    except Exception:
        raise ConfigError(
            "Keychain access failed. Unlock Keychain or rerun reactor-mcp setup."
        ) from None


def key_configured() -> bool:
    # Discovery must not open Keychain or trigger a password dialog.
    if "REACTOR_API_KEY" in os.environ:
        return bool(os.environ["REACTOR_API_KEY"].strip())
    return bool(read_settings().get("keychain"))


def live_enabled() -> bool:
    if "REACTOR_ENABLE_LIVE" in os.environ:
        return os.environ["REACTOR_ENABLE_LIVE"] == "true"
    return read_settings().get("live_enabled") is True


def output_dir() -> Path:
    return (
        Path(os.environ.get("REACTOR_OUTPUT_DIR") or read_settings().get("output_dir") or "outputs")
        .expanduser()
        .resolve()
    )
