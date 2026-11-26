"""The settings the web UI may read and change (T17), and the write to config.toml.

Only the keys in EDITABLE can be written from a client. Every other key is refused by the server and is
edited by hand in the file. Why each excluded key is out:
- network_enabled: switches on web_fetch, which sends requests to the network. Off by default; turn it on by hand.
- workspace: the default folder the file tools work in. A session chooses its own folder (session/new cwd).
- plugin_dir, plugins_enabled, disabled_plugins: plugins are Python files that run in this process.
- mcp_servers, test_command: each names a program that is started.

The write keeps the user's comments and the layout of the file (tomlkit). The new text is checked as a
whole with config.load_settings before it replaces the file, so a write can never leave a config that the
agent refuses to start with (a key this version does not know is refused on load, so it is refused here too).

The Ollama address (host) can be set here only when it is a loopback address (127.0.0.1, localhost or ::1).
Any other host would send chats to another machine, so it is edited by hand.

Changes take effect when the server next starts. A running server keeps the settings it started with, except
the model, which a chat can switch at any time (session/set_config_option).
"""

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import tomlkit

from local_agent import config
from local_agent.atomic import atomic_write_text
from local_agent.config import ConfigError, Settings

# The editable keys, with the kind of value each one takes (for the settings form in the web UI).
EDITABLE: dict[str, str] = {
    "model": "text",
    "host": "text",
    "num_ctx": "int",
    "max_tool_rounds": "int",
    "max_output_chars": "int",
    "command_timeout_s": "int",
    "network_timeout_s": "int",
    "tool_profile": "choice",
}

def read_editable(path: Path) -> dict[str, Any]:
    """The editable settings as the agent reads them from `path`: the file's values, defaults for the rest.
    A missing file gives the defaults. Raises ConfigError for a file the agent cannot load."""
    if path.exists():
        settings = config.load_settings(config_file=path, env={})
    else:
        settings = Settings()
    return {key: getattr(settings, key) for key in EDITABLE}


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def check_values(values: dict[str, Any]) -> dict[str, Any]:
    """Validate a client's values: only editable keys, each checked with the rules config.py uses (limits
    included), and host only on loopback."""
    checked: dict[str, Any] = {}
    for key, value in values.items():
        if key not in EDITABLE:
            raise ConfigError(f"{key} cannot be changed from the web UI; edit config.toml by hand")
        checked[key] = config._check(key, value, source=key)  # the same rules as the file and the flags
    if "host" in checked and urlsplit(checked["host"]).hostname not in LOOPBACK_HOSTS:
        raise ConfigError(f"the Ollama address must be on this machine (127.0.0.1, localhost or ::1), "
                          f"not {checked['host']}; edit host in config.toml by hand")
    return checked


def write_editable(path: Path, values: dict[str, Any]) -> dict[str, Any]:
    """Write the given editable values into `path` (created with its folder if missing), keeping everything
    else in the file. Returns read_editable(path) after the write. Raises ConfigError and writes nothing when a
    value is invalid, the file cannot be parsed, or the new file would not load."""
    checked = check_values(values)
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise ConfigError(f"{path}: cannot read the config file: {e}") from None
    else:
        text = ""
    try:
        document = tomlkit.parse(text)
    except tomlkit.exceptions.ParseError as e:
        raise ConfigError(f"{path}: invalid TOML, fix it by hand first: {e}") from None
    for key, value in checked.items():
        document[key] = value
    new_text = document.as_string()

    def check(temp: Path) -> None:
        config.load_settings(config_file=temp, env={})  # raises ConfigError: the file is then left as it was

    atomic_write_text(path, new_text, check=check)
    return read_editable(path)
