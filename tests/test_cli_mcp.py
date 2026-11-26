"""The REPL with MCP servers: start-up warnings, the argument preview, and shutdown at exit or Ctrl+C."""

import sys
from pathlib import Path

from wildcat_code_agent.config import McpServer, Settings
from wildcat_code_agent.core.events import ApprovalRequest, Preview
from wildcat_code_agent.frontends import cli
from wildcat_code_agent.tools.registry import REGISTRY

ECHO = str(Path(__file__).parent / "mcp_servers" / "echo_server.py")


def test_a_text_preview_without_a_path_is_shown_as_a_block(capsys):
    request = ApprovalRequest(tool_call_id="1.1", tool_name="files__add", risk="exec",
                              title="Run MCP tool 'add' on server 'files'",
                              preview=Preview(text="Server: files\nTool: add\nArguments:\n{}", kind="text"))
    cli.Renderer()(request)
    out = capsys.readouterr().out
    assert "[PREVIEW]\nServer: files\nTool: add\nArguments:\n{}\n[END PREVIEW]" in out
    assert "PROPOSED" not in out


def test_a_hostile_tool_name_and_arguments_cannot_write_controls_into_the_approval_prompt(monkeypatch, capsys):
    from mcp.types import Tool as McpTool

    from wildcat_code_agent.tools.mcp_tools import McpRuntime, _register_server

    registry = {}
    tool = McpTool(name="evil\nname\x1b[2J\r", inputSchema={"type": "object", "properties": {}})
    _register_server(McpRuntime(registry), McpServer(name="files", command="x"), (tool,))
    (name,) = registry
    request = registry[name].approval_request("1.1", {"text": "a\x1b[2J\rb\nc"})
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "n")
    cli.Renderer()(request)
    cli.TerminalApprover().decide(request)
    (prompt,) = prompts
    out = capsys.readouterr().out + prompt
    assert "\x1b" not in out and "\r" not in out
    assert prompt.count("\n") == 1  # the approval question is one line
    assert "Run MCP tool 'files__evil_name__2J_'" in prompt
    assert "Arguments:" in out and "\\u001b[2J" in out


def test_repl_shows_start_up_warnings_and_stops_the_servers_at_exit(monkeypatch, capsys):
    servers = {
        "cliecho": McpServer(name="cliecho", command=sys.executable, args=(ECHO,)),
        "cliboom": McpServer(name="cliboom", command=sys.executable,
                             args=("-c", "import sys; sys.stderr.write('kaboom'); sys.exit(1)")),
    }
    answers = iter(["exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    cli.run(settings=Settings(mcp_servers=servers, command_timeout_s=20), client=_NoModel())
    out = capsys.readouterr().out
    assert "Warning: MCP server 'cliboom' did not start" in out and "kaboom" in out
    assert out.index("Warning") < out.index("Wildcat Code Agent ready")
    assert not any(name.startswith("cliecho__") for name in REGISTRY)  # removed when the REPL exits


def test_ctrl_c_at_the_prompt_still_stops_the_servers(monkeypatch):
    servers = {"cliecho2": McpServer(name="cliecho2", command=sys.executable, args=(ECHO,))}

    def interrupt(prompt=""):
        assert "cliecho2__echo" in REGISTRY  # the server was up and its tools were offered
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    cli.run(settings=Settings(mcp_servers=servers, command_timeout_s=20), client=_NoModel())
    assert "cliecho2__echo" not in REGISTRY


class _NoModel:
    """A client that must not be used: these tests only start and stop the servers."""

    def show(self, model):
        raise ConnectionError("no model in this test")

    def chat(self, **_):
        raise AssertionError("no chat in this test")
