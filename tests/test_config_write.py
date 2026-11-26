"""The settings editor's write (T17): the allowlist, the rules config.py uses, comments and unknown keys kept."""

import pytest

from local_agent import config, config_write
from local_agent.config import ConfigError, Settings


def test_a_value_written_is_read_back_equal(tmp_path):
    path = tmp_path / "config.toml"
    values = config_write.write_editable(path, {"model": "qwen2.5-coder:7b", "num_ctx": 4096,
                                                "tool_profile": "minimal", "host": "http://127.0.0.1:9999"})
    assert values["model"] == "qwen2.5-coder:7b"
    assert values["num_ctx"] == 4096
    assert values["tool_profile"] == "minimal"
    assert values["host"] == "http://127.0.0.1:9999"
    again = config_write.read_editable(path)
    assert again == values
    # The agent itself reads the same values from the file.
    settings = config.load_settings(config_file=path, env={})
    assert settings.model == "qwen2.5-coder:7b" and settings.num_ctx == 4096


def test_a_missing_file_is_created_with_its_folder(tmp_path):
    path = tmp_path / "new-folder" / "config.toml"
    config_write.write_editable(path, {"max_tool_rounds": 7})
    assert path.is_file()
    assert config_write.read_editable(path)["max_tool_rounds"] == 7


def test_reading_a_missing_file_gives_the_defaults(tmp_path):
    values = config_write.read_editable(tmp_path / "absent.toml")
    defaults = Settings()
    assert values == {key: getattr(defaults, key) for key in config_write.EDITABLE}


def test_comments_and_unknown_keys_of_the_user_survive(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "# my own note at the top\n"
        "model = \"qwen3-vl:8b\"  # the default model\n"
        "\n"
        "[mcp_servers.echo]\n"
        "command = \"python\"\n"
        "risk = \"read\"\n",
        encoding="utf-8",
    )
    config_write.write_editable(path, {"num_ctx": 8192})
    text = path.read_text(encoding="utf-8")
    assert "# my own note at the top" in text
    assert "# the default model" in text
    assert "[mcp_servers.echo]" in text and 'command = "python"' in text
    settings = config.load_settings(config_file=path, env={})
    assert settings.num_ctx == 8192 and "echo" in settings.mcp_servers


@pytest.mark.parametrize("key", ["network_enabled", "workspace", "plugin_dir", "plugins_enabled",
                                 "disabled_plugins", "mcp_servers", "test_command", "not_a_setting"])
def test_keys_outside_the_allowlist_are_refused_and_nothing_is_written(tmp_path, key):
    path = tmp_path / "config.toml"
    path.write_text("model = \"a:1\"\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot be changed from the web UI"):
        config_write.write_editable(path, {key: True})
    assert path.read_text(encoding="utf-8") == "model = \"a:1\"\n"


@pytest.mark.parametrize("key,value", [
    ("num_ctx", 100),           # below MIN_NUM_CTX
    ("num_ctx", "8192"),        # a string, not a whole number
    ("num_ctx", True),          # a bool is not a number here
    ("max_tool_rounds", 0),
    ("tool_profile", "everything"),
    ("model", "   "),
    ("host", "ftp://example"),
])
def test_invalid_values_are_refused_with_the_config_rules(tmp_path, key, value):
    path = tmp_path / "config.toml"
    path.write_text("model = \"a:1\"\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        config_write.write_editable(path, {key: value})
    assert path.read_text(encoding="utf-8") == "model = \"a:1\"\n"


def test_a_file_that_is_not_valid_toml_is_not_overwritten(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("model = [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        config_write.write_editable(path, {"num_ctx": 4096})
    assert path.read_text(encoding="utf-8") == "model = [unclosed\n"


def test_a_file_the_agent_would_refuse_is_not_written(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("model = \"a:1\"\nunknown_key = 1\n", encoding="utf-8")  # the agent already refuses this file
    with pytest.raises(ConfigError, match="unknown setting"):
        config_write.write_editable(path, {"num_ctx": 4096})
    assert "unknown_key" in path.read_text(encoding="utf-8")
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_host_is_stored_normalised_like_the_flag(tmp_path):
    path = tmp_path / "config.toml"
    values = config_write.write_editable(path, {"host": "127.0.0.1:11434/"})
    assert values["host"] == "http://127.0.0.1:11434"
