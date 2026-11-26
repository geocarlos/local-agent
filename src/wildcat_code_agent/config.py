"""Settings: defaults, the per-user TOML file, OLLAMA_HOST and CLI flags.

Precedence, highest first: CLI flags > OLLAMA_HOST > config file > defaults.

The config file is `config.toml` in the per-user config folder, which is
%APPDATA%\\wildcat-code-agent\\ on Windows (roaming profile). A missing file means
defaults. Invalid values raise ConfigError with the file or flag that set them.

Nothing here reads a global: callers pass `env` and `config_file` explicitly
(tests pass a temp path and an empty env, never the real user config).
"""

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import platformdirs

APP_NAME = "wildcat-code-agent"
# The name before the rename. Its per-user folders move to the new name on first use (see adopt_legacy_folder).
LEGACY_APP_NAME = "local-agent"
# Replaces the per-user data folder (see sessions.py).
ENV_DATA_DIR = "WILDCAT_CODE_AGENT_DATA_DIR"
CONFIG_FILE_NAME = "config.toml"

DEFAULT_MODEL = "qwen3-vl:8b"
DEFAULT_HOST = "http://127.0.0.1:11434"
# Ollama's own default is VRAM-based and 4096 on the 8 GiB card this was tested on (see TASKS.md, T04).
DEFAULT_NUM_CTX = 8192
DEFAULT_MAX_TOOL_ROUNDS = 10
DEFAULT_MAX_OUTPUT_CHARS = 8000
DEFAULT_COMMAND_TIMEOUT_S = 60
DEFAULT_NETWORK_TIMEOUT_S = 20
# The tool profiles (names only; what each one offers is in tools/profiles.py).
TOOL_PROFILES = ("minimal", "coding", "all")
DEFAULT_TOOL_PROFILE = "coding"
# MCP servers (see TASKS.md T09): the risk tiers a server or one of its tools may be set to.
MCP_RISKS = ("read", "write", "exec", "network")
DEFAULT_MCP_RISK = "exec"  # always ask, unless the config says otherwise
MCP_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,31}")  # no underscore: "__" separates server and tool
# Plugins (see tools/plugins.py): the folder under the config folder that holds *.py plugin files.
PLUGIN_DIR_NAME = "tools"

MIN_NUM_CTX = 512
# Upper limits for the whole-number settings (T17). Every integer is also capped at 2**63 - 1.
# num_ctx: 1 Mi tokens is far above any model's window in use; the output and time limits keep one tool
# call or one reply from being unbounded.
MAX_INT = 2**63 - 1
MAX_VALUES = {
    "num_ctx": 1_048_576,
    "max_tool_rounds": 200,
    "max_output_chars": 1_000_000,
    "command_timeout_s": 3600,
    "network_timeout_s": 3600,
}


class ConfigError(ValueError):
    """A setting has an invalid value, or the config file cannot be read or parsed."""


@dataclass(frozen=True)
class McpServer:
    """One stdio MCP server, from a `[mcp_servers.<name>]` table. Started by tools/mcp_tools.py."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    # The server gets only a small safe subset of this process's environment (PATH, the home and temp
    # folders, and so on; see mcp.client.stdio.get_default_environment), plus these. API keys and
    # VIRTUAL_ENV or PYTHONPATH are not passed on unless set here.
    env: Mapping[str, str] | None = None
    cwd: Path | None = None
    risk: str = DEFAULT_MCP_RISK          # default tier for this server's tools
    enabled: bool = True
    tool_risks: Mapping[str, str] = field(default_factory=dict)  # per-tool override, by the server's tool name


@dataclass(frozen=True)
class Settings:
    """Every setting the agent uses. Built once at startup and passed explicitly."""

    model: str = DEFAULT_MODEL
    host: str = DEFAULT_HOST
    num_ctx: int = DEFAULT_NUM_CTX
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    command_timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S
    # The command run_tests uses when it is not given one (None: detect from the workspace).
    test_command: str | None = None
    # Which tools the model is offered and may run (see tools/profiles.py).
    tool_profile: str = DEFAULT_TOOL_PROFILE
    # Network-tier tools (web_fetch) are offered only when this is true. Off by default.
    network_enabled: bool = False
    # web_fetch's time limit for the whole fetch (redirects included), and for each connect or read.
    network_timeout_s: int = DEFAULT_NETWORK_TIMEOUT_S
    # MCP servers started at launch, by name (see TASKS.md T09). Empty: none.
    mcp_servers: Mapping[str, McpServer] = field(default_factory=dict)
    # Plugins (see tools/plugins.py): tools from the plugin folder and from entry points. On by default.
    plugins_enabled: bool = True
    # Plugins not to load, by file stem or entry point name.
    disabled_plugins: tuple[str, ...] = ()
    # The plugin folder. None: the tools folder under the config folder (see plugin_dir_of).
    plugin_dir: Path | None = None
    # Relative tool paths resolve against it, and paths outside it need approval (see sandbox.py).
    workspace: Path = field(default_factory=Path.cwd)


def adopt_legacy_folder(folder: Path, legacy: Path) -> Path:
    """The folder to use: `folder`, after moving `legacy` (the pre-rename folder) there if only `legacy` exists.

    Nothing moves while ENV_DATA_DIR is set (the tests set it), so a test run never moves the real folder.
    If the move fails (a file in it is open, for example), the legacy folder is used as it is.
    """
    if os.environ.get(ENV_DATA_DIR, "").strip() or folder.exists() or not legacy.is_dir():
        return folder
    try:
        folder.parent.mkdir(parents=True, exist_ok=True)
        os.replace(legacy, folder)
    except OSError:
        return legacy
    return folder


def config_path() -> Path:
    """Where the config file lives for this user (%APPDATA%\\wildcat-code-agent\\config.toml on Windows)."""
    folder = platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=True)
    legacy = platformdirs.user_config_dir(LEGACY_APP_NAME, appauthor=False, roaming=True)
    return adopt_legacy_folder(Path(folder), Path(legacy)) / CONFIG_FILE_NAME


def default_plugin_dir() -> Path:
    """The per-user plugin folder: <config folder>\\tools. The app does not create it."""
    return config_path().parent / PLUGIN_DIR_NAME


def plugin_dir_of(settings: Settings) -> Path:
    """The plugin folder in use for these settings: `plugin_dir` if set, else the per-user folder."""
    return settings.plugin_dir if settings.plugin_dir is not None else default_plugin_dir()


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


_FLAG_NAMES = {"model": "--model", "host": "--host", "num_ctx": "--num-ctx", "workspace": "--cwd",
               "tool_profile": "--profile", "plugins_enabled": "--no-plugins"}
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
    if key == "network_timeout_s":
        return _int_at_least(key, value, 1, source)
    if key == "network_enabled":
        if not isinstance(value, bool):
            raise ConfigError(f"{source} must be true or false, got {value!r}")
        return value
    if key == "tool_profile":
        if not isinstance(value, str) or value.strip() not in TOOL_PROFILES:
            known = ", ".join(TOOL_PROFILES)
            raise ConfigError(f"{source} must be one of {known}, got {value!r}")
        return value.strip()
    if key == "test_command":
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{source} must be a non-empty command line, got {value!r}")
        return value.strip()
    if key == "workspace":
        return _directory(value, source, base)
    if key == "mcp_servers":
        return _mcp_servers(value, source, base)
    if key == "plugins_enabled":
        if not isinstance(value, bool):
            raise ConfigError(f"{source} must be true or false, got {value!r}")
        return value
    if key == "disabled_plugins":
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise ConfigError(f"{source} must be a list of plugin names (file stems or entry point names), got {value!r}")
        return tuple(v.strip() for v in value)
    if key == "plugin_dir":
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{source} must be a folder path, got {value!r}")
        path = Path(value.strip()).expanduser()  # the folder need not exist: nothing is created or required
        if not path.is_absolute():
            if base is None:
                raise ConfigError(f"{source} must be an absolute path, got {value!r}")
            path = base / path
        return path.resolve()
    raise ConfigError(f"unknown setting {key!r}")


_MCP_SERVER_KEYS = ("command", "args", "env", "cwd", "risk", "enabled", "tools")


def _mcp_servers(value: Any, source: str, base: Path | None) -> dict[str, McpServer]:
    """Validate the [mcp_servers] table. `source` is the file and key (for messages)."""
    if not isinstance(value, dict):
        raise ConfigError(f"{source} must be a table of servers, got {value!r}")
    return {name: _mcp_server(name, table, f"{source}.{name}", base) for name, table in value.items()}


def _mcp_server(name: str, table: Any, source: str, base: Path | None) -> McpServer:
    if not MCP_NAME_PATTERN.fullmatch(name):
        raise ConfigError(f"{source}: a server name may use letters, digits and hyphens only (got {name!r})")
    if not isinstance(table, dict):
        raise ConfigError(f"{source} must be a table with at least command, got {table!r}")
    unknown = sorted(set(table) - set(_MCP_SERVER_KEYS))
    if unknown:
        known = ", ".join(_MCP_SERVER_KEYS)
        raise ConfigError(f"{source}: unknown setting(s) {', '.join(unknown)} (known: {known})")
    if "command" not in table:
        raise ConfigError(f"{source}.command is required")
    command = table["command"]
    if not isinstance(command, str) or not command.strip():
        raise ConfigError(f"{source}.command must be a non-empty program name, got {command!r}")
    args = table.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ConfigError(f"{source}.args must be a list of strings, got {args!r}")
    env = table.get("env")
    if env is not None:
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ConfigError(f"{source}.env must be a table of string values, got {env!r}")
    cwd = table.get("cwd")
    if cwd is not None:
        cwd = _directory(cwd, f"{source}.cwd", base)
    risk = table.get("risk", DEFAULT_MCP_RISK)
    _check_risk(risk, f"{source}.risk")
    enabled = table.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{source}.enabled must be true or false, got {enabled!r}")
    tools = table.get("tools", {})
    if not isinstance(tools, dict):
        raise ConfigError(f"{source}.tools must be a table of tool names, got {tools!r}")
    tool_risks: dict[str, str] = {}
    for tool_name, tool_table in tools.items():
        if not isinstance(tool_table, dict) or set(tool_table) != {"risk"}:
            raise ConfigError(f"{source}.tools.{tool_name} must be a table with only risk, got {tool_table!r}")
        _check_risk(tool_table["risk"], f"{source}.tools.{tool_name}.risk")
        tool_risks[tool_name] = tool_table["risk"]
    return McpServer(name=name, command=command.strip(), args=tuple(args), env=env, cwd=cwd, risk=risk,
                     enabled=enabled, tool_risks=tool_risks)


def _check_risk(value: Any, source: str) -> None:
    if not isinstance(value, str) or value not in MCP_RISKS:
        raise ConfigError(f"{source} must be one of {', '.join(MCP_RISKS)}, got {value!r}")


def _int_at_least(key: str, value: Any, minimum: int, source: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{source} must be a whole number, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{source} must be at least {minimum}, got {value}")
    maximum = MAX_VALUES.get(key, MAX_INT)
    if value > maximum:
        raise ConfigError(f"{source} must be at most {maximum}, got {value}")
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
