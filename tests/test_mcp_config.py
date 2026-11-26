"""[mcp_servers] in config.toml: the example parses, and bad values raise ConfigError naming the setting."""

import re
from pathlib import Path

import pytest

from wildcat_code_agent.config import ConfigError, McpServer, load_settings


def settings_from(tmp_path: Path, text: str):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return load_settings(config_file=path, env={})


def test_the_documented_example_parses_into_servers(tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    settings = settings_from(tmp_path, f"""
[mcp_servers.files]
command = "uvx"
args = ["some-server", "--flag"]
env = {{ KEY = "value" }}
cwd = "{folder.as_posix()}"
risk = "exec"
enabled = true
[mcp_servers.files.tools.some_tool]
risk = "read"
""")
    server = settings.mcp_servers["files"]
    assert server == McpServer(name="files", command="uvx", args=("some-server", "--flag"),
                               env={"KEY": "value"}, cwd=folder.resolve(), risk="exec", enabled=True,
                               tool_risks={"some_tool": "read"})


def test_only_the_command_is_needed_and_the_defaults_are_exec_and_enabled(tmp_path):
    server = settings_from(tmp_path, '[mcp_servers.calc]\ncommand = "calc-server"\n').mcp_servers["calc"]
    assert (server.args, server.env, server.cwd, server.risk, server.enabled, dict(server.tool_risks)) == \
        ((), None, None, "exec", True, {})


def test_no_servers_is_the_default(tmp_path):
    assert settings_from(tmp_path, "model = 'x'\n").mcp_servers == {}


@pytest.mark.parametrize("text, message", [
    ('[mcp_servers.calc]\nargs = ["x"]\n', "mcp_servers.calc.command is required"),
    ('[mcp_servers.calc]\ncommand = ""\n', "mcp_servers.calc.command must be a non-empty program name"),
    ('[mcp_servers.calc]\ncommand = "x"\ntimeout = 5\n', "unknown setting(s) timeout"),
    ('[mcp_servers.calc]\ncommand = "x"\nargs = "one"\n', "mcp_servers.calc.args must be a list of strings"),
    ('[mcp_servers.calc]\ncommand = "x"\nargs = [1]\n', "mcp_servers.calc.args must be a list of strings"),
    ('[mcp_servers.calc]\ncommand = "x"\nenv = { A = 1 }\n', "mcp_servers.calc.env must be a table of string values"),
    ('[mcp_servers.calc]\ncommand = "x"\nrisk = "admin"\n', "mcp_servers.calc.risk must be one of read, write, exec, network"),
    ('[mcp_servers.calc]\ncommand = "x"\nenabled = "yes"\n', "mcp_servers.calc.enabled must be true or false"),
    ('[mcp_servers.calc]\ncommand = "x"\n[mcp_servers.calc.tools.t]\nrisk = "bogus"\n',
     "mcp_servers.calc.tools.t.risk must be one of"),
    ('[mcp_servers.calc]\ncommand = "x"\n[mcp_servers.calc.tools.t]\nrisk = "read"\nextra = 1\n',
     "mcp_servers.calc.tools.t must be a table with only risk"),
    ('[mcp_servers.calc]\ncommand = "x"\ntools = ["t"]\n', "mcp_servers.calc.tools must be a table of tool names"),
    ('[mcp_servers."bad name"]\ncommand = "x"\n', "a server name may use letters, digits and hyphens only"),
    ('[mcp_servers.under_score]\ncommand = "x"\n', "a server name may use letters, digits and hyphens only"),
    ('[mcp_servers]\ncalc = "x"\n', "mcp_servers.calc must be a table with at least command"),
])
def test_bad_values_raise_config_error_naming_the_setting(tmp_path, text, message):
    with pytest.raises(ConfigError, match=re.escape(message)):
        settings_from(tmp_path, text)


def test_a_cwd_that_is_not_a_folder_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="mcp_servers.calc.cwd is not an existing folder"):
        settings_from(tmp_path, f'[mcp_servers.calc]\ncommand = "x"\ncwd = "{(tmp_path / "missing").as_posix()}"\n')


def test_a_relative_cwd_is_taken_from_the_config_file_folder(tmp_path):
    (tmp_path / "sub").mkdir()
    server = settings_from(tmp_path, '[mcp_servers.calc]\ncommand = "x"\ncwd = "sub"\n').mcp_servers["calc"]
    assert server.cwd == (tmp_path / "sub").resolve()
