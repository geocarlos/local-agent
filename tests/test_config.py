"""Config precedence (flags > OLLAMA_HOST > file > defaults), validation and the config path.

Every test passes an explicit config file (a temp path) and an explicit env dict,
so none of them reads the real user config or the real environment.
"""

import os
import sys
from pathlib import Path

import pytest

from local_agent import main
from local_agent.config import (
    DEFAULT_HOST,
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    ConfigError,
    Settings,
    config_path,
    load_settings,
)
from local_agent.frontends import cli


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def empty_config(tmp_path: Path) -> Path:
    return write_config(tmp_path, "")


def test_defaults_when_the_file_is_empty(tmp_path):
    settings = load_settings(config_file=empty_config(tmp_path), env={})

    assert settings.model == DEFAULT_MODEL == "qwen3-vl:8b"
    assert settings.host == DEFAULT_HOST == "http://127.0.0.1:11434"
    assert settings.num_ctx == DEFAULT_NUM_CTX == 8192
    assert settings.max_tool_rounds == 10
    assert settings.max_output_chars == 8000
    assert settings.command_timeout_s == 60
    assert settings.workspace == Path.cwd()


def test_settings_defaults_match_load_settings_defaults(tmp_path):
    assert load_settings(config_file=empty_config(tmp_path), env={}) == Settings()


def test_missing_config_file_is_defaults_when_not_given_explicitly(tmp_path, monkeypatch):
    # With no --config, a missing per-user file means defaults. Point the per-user path at a temp file.
    monkeypatch.setattr("local_agent.config.config_path", lambda: tmp_path / "absent.toml")

    assert load_settings(env={}) == Settings()


def test_file_values_override_defaults(tmp_path):
    path = write_config(tmp_path, '''
model = "qwen2.5-coder:7b"
host = "http://10.0.0.5:11434"
num_ctx = 4096
max_tool_rounds = 4
max_output_chars = 2000
command_timeout_s = 20
''')

    settings = load_settings(config_file=path, env={})

    assert settings.model == "qwen2.5-coder:7b"
    assert settings.host == "http://10.0.0.5:11434"
    assert settings.num_ctx == 4096
    assert settings.max_tool_rounds == 4
    assert settings.max_output_chars == 2000
    assert settings.command_timeout_s == 20


def test_ollama_host_overrides_the_file_host(tmp_path):
    path = write_config(tmp_path, 'host = "http://file:1"\n')

    settings = load_settings(config_file=path, env={"OLLAMA_HOST": "http://env:2"})

    assert settings.host == "http://env:2"


def test_empty_ollama_host_is_ignored(tmp_path):
    path = write_config(tmp_path, 'host = "http://file:1"\n')

    assert load_settings(config_file=path, env={"OLLAMA_HOST": "  "}).host == "http://file:1"


def test_flags_override_environment_and_file(tmp_path):
    path = write_config(tmp_path, 'model = "file-model"\nnum_ctx = 2048\nhost = "http://file:1"\n')

    settings = load_settings(
        config_file=path,
        env={"OLLAMA_HOST": "http://env:2"},
        overrides={"model": "flag-model", "num_ctx": 16384, "host": "http://flag:3", "workspace": None},
    )

    assert settings.model == "flag-model"
    assert settings.num_ctx == 16384
    assert settings.host == "http://flag:3"


def test_a_flag_that_is_not_given_keeps_the_lower_layer(tmp_path):
    path = write_config(tmp_path, "num_ctx = 2048\n")

    settings = load_settings(config_file=path, env={"OLLAMA_HOST": "http://env:2"},
                             overrides={"model": "flag-model", "num_ctx": None, "host": None})

    assert settings.num_ctx == 2048  # file, not default
    assert settings.host == "http://env:2"  # env, not default
    assert settings.model == "flag-model"


def test_full_precedence_chain_for_one_setting(tmp_path):
    """Each layer wins over the ones below it, for the same setting."""
    empty = empty_config(tmp_path)
    from_file = tmp_path / "file.toml"
    from_file.write_text('host = "http://file:1"\n', encoding="utf-8")

    assert load_settings(config_file=empty, env={}).host == DEFAULT_HOST
    assert load_settings(config_file=from_file, env={}).host == "http://file:1"
    assert load_settings(config_file=from_file, env={"OLLAMA_HOST": "http://env:2"}).host == "http://env:2"
    assert load_settings(config_file=from_file, env={"OLLAMA_HOST": "http://env:2"},
                         overrides={"host": "http://flag:3"}).host == "http://flag:3"


def test_explicit_config_file_must_exist(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_settings(config_file=tmp_path / "nope.toml", env={})


def test_invalid_toml_names_the_file(tmp_path):
    path = write_config(tmp_path, "num_ctx = [1, 2\n")

    with pytest.raises(ConfigError, match="invalid TOML") as info:
        load_settings(config_file=path, env={})

    assert str(path) in str(info.value)


def test_unknown_key_is_an_error_that_lists_the_known_keys(tmp_path):
    path = write_config(tmp_path, "num_ctxx = 4096\n")

    with pytest.raises(ConfigError, match="unknown setting") as info:
        load_settings(config_file=path, env={})

    assert "num_ctxx" in str(info.value)
    assert "num_ctx" in str(info.value)  # the known names are listed


@pytest.mark.parametrize(("line", "fragment"), [
    ('num_ctx = "8192"', "num_ctx must be a whole number"),
    ("num_ctx = true", "num_ctx must be a whole number"),
    ("num_ctx = 100", "num_ctx must be at least 512"),
    ("max_tool_rounds = 0", "max_tool_rounds must be at least 1"),
    ("max_output_chars = 0", "max_output_chars must be at least 1"),
    ("command_timeout_s = -5", "command_timeout_s must be at least 1"),
    ('model = ""', "model must be a non-empty model name"),
    ('host = "ftp://example.com"', "must start with http:// or https://"),
    ("host = 11434", "must be a host"),
])
def test_invalid_values_name_the_setting_and_the_file(tmp_path, line, fragment):
    path = write_config(tmp_path, line + "\n")

    with pytest.raises(ConfigError, match=fragment) as info:
        load_settings(config_file=path, env={})

    assert str(path) in str(info.value)


def test_host_without_a_scheme_gets_http(tmp_path):
    path = write_config(tmp_path, 'host = "127.0.0.1:11434/"\n')

    assert load_settings(config_file=path, env={}).host == "http://127.0.0.1:11434"


def test_ollama_host_without_a_scheme_is_accepted_like_the_ollama_cli(tmp_path):
    settings = load_settings(config_file=empty_config(tmp_path), env={"OLLAMA_HOST": "0.0.0.0:11434"})

    assert settings.host == "http://0.0.0.0:11434"


def test_invalid_ollama_host_names_the_variable(tmp_path):
    with pytest.raises(ConfigError, match="OLLAMA_HOST must start with"):
        load_settings(config_file=empty_config(tmp_path), env={"OLLAMA_HOST": "ftp://x"})


def test_workspace_in_the_file_is_relative_to_the_file(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    path = write_config(tmp_path, 'workspace = "project"\n')

    assert load_settings(config_file=path, env={}).workspace == project.resolve()


def test_workspace_must_exist(tmp_path):
    path = write_config(tmp_path, 'workspace = "missing-folder"\n')

    with pytest.raises(ConfigError, match="not an existing folder"):
        load_settings(config_file=path, env={})


def test_workspace_must_be_absolute_or_relative_to_the_file(tmp_path):
    path = write_config(tmp_path, "workspace = 5\n")

    with pytest.raises(ConfigError, match="folder path"):
        load_settings(config_file=path, env={})


def test_cli_flags_build_settings_with_the_same_precedence(tmp_path):
    project = tmp_path / "work"
    project.mkdir()
    path = write_config(tmp_path, 'model = "file-model"\nnum_ctx = 2048\n')
    args = cli.build_parser().parse_args([
        "--config", str(path), "--model", "flag-model", "--num-ctx", "16384", "--cwd", str(project),
    ])

    settings = cli.settings_from_args(args, env={"OLLAMA_HOST": "http://env:2"})

    assert settings.model == "flag-model"
    assert settings.num_ctx == 16384
    assert settings.host == "http://env:2"
    assert settings.workspace == project.resolve()


def test_cli_without_flags_uses_the_file_and_defaults(tmp_path):
    path = write_config(tmp_path, "max_tool_rounds = 3\n")
    args = cli.build_parser().parse_args(["--config", str(path)])

    settings = cli.settings_from_args(args, env={})

    assert settings.max_tool_rounds == 3
    assert settings.model == DEFAULT_MODEL


def test_cli_num_ctx_flag_is_validated_like_the_file(tmp_path):
    args = cli.build_parser().parse_args(["--config", str(empty_config(tmp_path)), "--num-ctx", "10"])

    with pytest.raises(ConfigError, match="--num-ctx must be at least 512"):
        cli.settings_from_args(args, env={})


def test_main_reports_a_bad_config_on_stderr_and_exits_2(tmp_path, capsys):
    path = write_config(tmp_path, "num_ctx = 1\n")

    with pytest.raises(SystemExit) as info:
        main(["--config", str(path)])

    assert info.value.code == 2
    err = capsys.readouterr().err
    assert "num_ctx must be at least 512" in err
    assert str(path) in err


def test_main_reports_a_missing_explicit_config_on_stderr(tmp_path, capsys):
    with pytest.raises(SystemExit) as info:
        main(["--config", str(tmp_path / "nope.toml")])

    assert info.value.code == 2
    assert "config file not found" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "win32", reason="the %APPDATA% location is Windows-specific")
def test_config_path_is_under_appdata_on_windows():
    assert config_path() == Path(os.environ["APPDATA"]) / "local-agent" / "config.toml"


def test_config_path_is_named_config_toml():
    assert config_path().name == "config.toml"
    assert config_path().parent.name == "local-agent"

def test_test_command_defaults_to_none_and_is_read_from_the_file(tmp_path):
    empty = write_config(tmp_path, "")
    assert load_settings(config_file=empty, env={}).test_command is None

    configured = write_config(tmp_path, 'test_command = "  uv run pytest -q  "\n')
    assert load_settings(config_file=configured, env={}).test_command == "uv run pytest -q"


@pytest.mark.parametrize("value", ['""', '"   "', "5", "true"])
def test_test_command_must_be_a_non_empty_string(tmp_path, value):
    path = write_config(tmp_path, f"test_command = {value}\n")

    with pytest.raises(ConfigError, match="test_command must be a non-empty command line"):
        load_settings(config_file=path, env={})


def test_network_is_off_and_the_profile_is_coding_by_default(tmp_path):
    settings = load_settings(config_file=empty_config(tmp_path), env={})

    assert settings.network_enabled is False
    assert settings.tool_profile == "coding"
    assert settings.network_timeout_s == 20


def test_network_enabled_and_timeout_come_from_the_file(tmp_path):
    path = write_config(tmp_path, "network_enabled = true\nnetwork_timeout_s = 5\n")

    settings = load_settings(config_file=path, env={})

    assert settings.network_enabled is True
    assert settings.network_timeout_s == 5


@pytest.mark.parametrize("text, message", [
    ('network_enabled = "yes"', "must be true or false"),
    ("network_enabled = 1", "must be true or false"),
    ("network_timeout_s = 0", "must be at least 1"),
    ('tool_profile = "everything"', "must be one of minimal, coding, all"),
    ("tool_profile = 3", "must be one of minimal, coding, all"),
])
def test_bad_network_or_profile_values_are_config_errors(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(config_file=write_config(tmp_path, text), env={})


def test_the_profile_comes_from_the_file_and_the_flag_overrides_it(tmp_path):
    path = write_config(tmp_path, 'tool_profile = "minimal"\n')

    assert load_settings(config_file=path, env={}).tool_profile == "minimal"
    assert load_settings(config_file=path, env={}, overrides={"tool_profile": "all"}).tool_profile == "all"


def test_an_unknown_profile_flag_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="--profile must be one of minimal, coding, all"):
        load_settings(config_file=empty_config(tmp_path), env={}, overrides={"tool_profile": "nope"})


def test_the_profile_flag_is_parsed_and_overrides_the_file(tmp_path):
    path = write_config(tmp_path, 'tool_profile = "minimal"\n')
    args = cli.build_parser().parse_args(["--config", str(path), "--profile", "all"])

    assert cli.settings_from_args(args, env={}).tool_profile == "all"


def test_main_exits_2_for_an_unknown_profile_flag(tmp_path, capsys):
    with pytest.raises(SystemExit) as info:
        main(["--config", str(empty_config(tmp_path)), "--profile", "nope"])

    assert info.value.code == 2
    assert "--profile must be one of" in capsys.readouterr().err
