"""ToolContext: per-turn limits passed to tools, kept out of the schema the model sees."""

import sys
from typing import Annotated

import pytest

from local_agent.permissions import Risk
from local_agent.tools import TOOLS, builtin, tool
from local_agent.tools.registry import ToolContext


def make_tool(fn, risk=Risk.READ):
    table: dict = {}
    tool(risk=risk, registry=table)(fn)
    return table[fn.__name__]


def test_the_context_parameter_is_not_in_the_schema():
    assert set(TOOLS["read_file"].parameters["properties"]) == {"path"}
    assert set(TOOLS["run_command"].parameters["properties"]) == {"command"}
    assert TOOLS["run_command"].parameters["required"] == ["command"]


def test_a_tool_without_context_is_unchanged():
    def echo(x: Annotated[str, "text"]) -> str:
        """Echo."""
        return x

    echo_tool = make_tool(echo)

    assert echo_tool.takes_context is False
    assert echo_tool.call({"x": "hi"}) == "hi"


def test_read_file_truncates_to_the_context_limit(tmp_path):
    target = tmp_path / "long.txt"
    target.write_text("abcdefghij", encoding="utf-8")

    result = TOOLS["read_file"].call({"path": str(target)}, ToolContext(max_output_chars=4))

    assert result == "abcd\n...[truncated 6 chars]"


def test_read_file_without_a_context_uses_the_defaults(tmp_path):
    target = tmp_path / "short.txt"
    target.write_text("abc", encoding="utf-8")

    assert TOOLS["read_file"].call({"path": str(target)}) == "abc"


def test_run_command_uses_the_context_timeout():
    command = f'"{sys.executable}" -c "import time; time.sleep(5)"'

    result = TOOLS["run_command"].call({"command": command}, ToolContext(command_timeout_s=1))

    assert result == "Error: command timed out after 1s and was killed."


def test_a_model_cannot_pass_the_context_argument():
    with pytest.raises(TypeError, match="unexpected argument 'context'"):
        TOOLS["read_file"].bind({"path": "x", "context": "forged"})


def test_bind_reports_missing_arguments_for_a_context_tool():
    with pytest.raises(TypeError, match="read_file\\(\\) missing a required argument: 'path'"):
        TOOLS["read_file"].bind({}, ToolContext())


def test_a_context_parameter_must_use_the_context_type_to_be_special():
    def takes_str(context: str) -> str:
        """Not a context: a plain string parameter."""
        return context

    plain = make_tool(takes_str)

    assert plain.takes_context is False
    assert "context" in plain.parameters["properties"]


def test_builtin_module_registers_the_context_tools():
    assert builtin.read_file is TOOLS["read_file"].fn
    assert TOOLS["read_file"].takes_context and TOOLS["run_command"].takes_context
    assert not TOOLS["write_file"].takes_context
