"""Settings: defaults, the per-user TOML file, OLLAMA_HOST and CLI flags.

Precedence, highest first: CLI flags > OLLAMA_HOST > config file > defaults.

The config file is `config.toml` in the per-user config folder, which is
%APPDATA%\\local-agent\\ on Windows (roaming profile). A missing file means
defaults. Invalid values raise ConfigError with the file or flag that set them.

Nothing here reads a global: callers pass `env` and `config_file` explicitly
(tests pass a temp path and an empty env, never the real user config).
"""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import platformdirs

APP_NAME = "local-agent"
CONFIG_FILE_NAME = "config.toml"

DEFAULT_MODEL = "qwen3-vl:8b"
DEFAULT_HOST = "http://127.0.0.1:11434"
# Ollama's own default is VRAM-based and 4096 on the 8 GiB card this was tested on (see TASKS.md, T04).
DEFAULT_NUM_CTX = 8192
DEFAULT_MAX_TOOL_ROUNDS = 10
DEFAULT_MAX_OUTPUT_CHARS = 8000
DEFAULT_COMMAND_TIMEOUT_S = 60

MIN_NUM_CTX = 512


class ConfigError(ValueError):
    """A setting has an invalid value, or the config file cannot be read or parsed."""


@dataclass(frozen=True)
class Settings:
    """Every setting the agent uses. Built once at startup and passed explicitly."""

    model: str = DEFAULT_MODEL
    host: str = DEFAULT_HOST
    num_ctx: int = DEFAULT_NUM_CTX
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    command_timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S
    # Stored only for now: the sandbox that confines tools to it is T05.
    workspace: Path = field(default_factory=Path.cwd)


def config_path() -> Path:
    """Where the config file lives for this user (%APPDATA%\\local-agent\\config.toml on Windows)."""
    folder = platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=True)
    return Path(folder) / CONFIG_FILE_NAME


def load_settings(
    *,
    config_file: Path | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Defaults, then the config file, then OLLAMA_HOST, then `overrides` (flags, None = not given).

    `config_file=None` reads the per-user file if it exists. An explicit path
    must exist. `env=None` reads os.environ; tests pass a dict.
    """
    env = os.environ if env is None else env
    values: dict[str, Any] = {}
    values.update(_read_file(config_file if config_file is not None else config_path(),
                             explicit=config_file is not None))

    host_from_env = env.get("OLLAMA_HOST", "").strip()
    if host_from_env:
        values["host"] = _check("host", host_from_env, source="OLLAMA_HOST")

    for key, value in (overrides or {}).items():
        if value is not None:
            values[key] = _check(key, value, source=_FLAG_NAMES.get(key, key), base=Path.cwd())

    return Settings(**values)


_FLAG_NAMES = {"model": "--model", "host": "--host", "num_ctx": "--num-ctx", "workspace": "--cwd"}
_KNOWN_KEYS = {f.name for f in fields(Settings)}


def _read_file(path: Path, *, explicit: bool) -> dict[str, Any]:
    if not path.exists():
        if explicit:
            raise ConfigError(f"config file not found: {path}")
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from None
    except OSError as e:
        raise ConfigError(f"{path}: cannot read the config file: {e}") from None

    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        known = ", ".join(sorted(_KNOWN_KEYS))
        raise ConfigError(f"{path}: unknown setting(s) {', '.join(unknown)} (known: {known})")
    return {key: _check(key, value, source=f"{path}: {key}", base=path.parent)
            for key, value in data.items()}


def _check(key: str, value: Any, *, source: str, base: Path | None = None) -> Any:
    """Validate one value. `source` names where it came from, for the error message."""
    if key == "model":
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{source} must be a non-empty model name, got {value!r}")
        return value.strip()
    if key == "host":
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{source} must be a host such as http://127.0.0.1:11434, got {value!r}")
        host = value.strip()
        if "://" not in host:  # OLLAMA_HOST may omit the scheme, as in the ollama CLI
            host = f"http://{host}"
        if not host.startswith(("http://", "https://")):
            raise ConfigError(f"{source} must start with http:// or https://, got {value!r}")
        return host.rstrip("/")
    if key == "num_ctx":
        return _int_at_least(key, value, MIN_NUM_CTX, source)
    if key == "max_tool_rounds":
        return _int_at_least(key, value, 1, source)
    if key == "max_output_chars":
        return _int_at_least(key, value, 1, source)
    if key == "command_timeout_s":
        return _int_at_least(key, value, 1, source)
    if key == "workspace":
        return _directory(value, source, base)
    raise ConfigError(f"unknown setting {key!r}")


def _int_at_least(key: str, value: Any, minimum: int, source: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{source} must be a whole number, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{source} must be at least {minimum}, got {value}")
    return value


def _directory(value: Any, source: str, base: Path | None) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{source} must be a folder path, got {value!r}")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        if base is None:
            raise ConfigError(f"{source} must be an absolute path, got {value!r}")
        path = base / path
    if not path.is_dir():
        raise ConfigError(f"{source} is not an existing folder: {path}")
    return path.resolve()
