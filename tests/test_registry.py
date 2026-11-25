"""Tool registry: schema generation from type hints, risk, kind and approval requests."""

from typing import Annotated

import pytest

from local_agent import tools
from local_agent.core.events import Preview
from local_agent.permissions import Risk, needs_approval
from local_agent.tools import Location, tool
from local_agent.tools.builtin import MAX_PREVIEW_CHARS


def registry_with(fn, **kwargs) -> dict:
    table: dict = {}
    tool(registry=table, **kwargs)(fn)
    return table


def test_builtin_schemas_match_what_ollama_received_before_t02():
    # These are the exact `tools=` entries ollama produced from the plain functions before T02.
    # read_file gained optional line range arguments in T06; its path schema is otherwise unchanged.
    assert tools.TOOLS["read_file"].schema()["function"] == {
        "name": "read_file",
        "description": "Read a text file: all of it, or only lines start_line..end_line (1-based, inclusive), numbered.",
        "parameters": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": ""},
                "start_line": {"type": "integer", "description": "First line to return, 1-based. Omit to start at line 1."},
                "end_line": {"type": "integer", "description": "Last line to return, inclusive. Omit to read to the end."},
            },
        },
    }
    assert tools.TOOLS["write_file"].schema()["function"] == {
        "name": "write_file",
        "description": "Write content to a file (asks for approval first).",
        "parameters": {
            "type": "object",
            "required": ["path", "content"],
            "properties": {
                "path": {"type": "string", "description": ""},
                "content": {"type": "string", "description": ""},
            },
        },
    }
    assert tools.TOOLS["run_command"].schema()["function"] == {
        "name": "run_command",
        "description": "Run a shell command (asks for approval first).",
        "parameters": {
            "type": "object",
            "required": ["command"],
            "properties": {"command": {"type": "string", "description": ""}},
        },
    }


def test_builtin_risk_location_and_kind():
    assert tools.TOOLS["read_file"].risk is Risk.READ
    assert tools.TOOLS["write_file"].risk is Risk.WRITE
    assert tools.TOOLS["run_command"].risk is Risk.EXEC
    assert all(t.location is Location.CORE for t in tools.TOOLS.values())
    assert [tools.TOOLS[n].kind for n in ("read_file", "write_file", "run_command")] == [
        "read", "edit", "execute",
    ]


def test_scalar_list_dict_and_optional_types_and_required_rules():
    def sample(a: int, b: float, c: bool, d: list[str], e: dict, f: str | None = None, g: str = "x") -> str:
        """Sample tool."""
        return ""

    schema = registry_with(sample, risk=Risk.READ)["sample"].parameters
    assert schema["properties"] == {
        "a": {"type": "integer", "description": ""},
        "b": {"type": "number", "description": ""},
        "c": {"type": "boolean", "description": ""},
        "d": {"type": "array", "items": {"type": "string"}, "description": ""},
        "e": {"type": "object", "description": ""},
        "f": {"type": "string", "description": ""},
        "g": {"type": "string", "description": ""},
    }
    assert schema["required"] == ["a", "b", "c", "d", "e"]


def test_annotated_string_becomes_parameter_description():
    def fetch(url: Annotated[str, "Absolute http(s) URL"], limit: Annotated[int, "Max bytes"] = 10) -> str:
        """Fetch a URL."""
        return ""

    schema = registry_with(fetch, risk=Risk.NETWORK)["fetch"].parameters
    assert schema["properties"]["url"] == {"type": "string", "description": "Absolute http(s) URL"}
    assert schema["properties"]["limit"] == {"type": "integer", "description": "Max bytes"}
    assert schema["required"] == ["url"]


def test_description_is_the_docstring():
    def documented(x: str) -> str:
        """First line.

        More detail that is still part of the description.
        """
        return x

    table = registry_with(documented, risk=Risk.READ)
    assert table["documented"].description == "First line.\n\nMore detail that is still part of the description."


def test_missing_docstring_is_rejected():
    def bare(x: str) -> str:
        return x

    with pytest.raises(TypeError, match="needs a docstring"):
        registry_with(bare, risk=Risk.READ)


def test_missing_type_hint_is_rejected():
    def untyped(x) -> str:
        """No hint."""
        return str(x)

    with pytest.raises(TypeError, match="needs a type hint"):
        registry_with(untyped, risk=Risk.READ)


def test_unsupported_type_is_rejected_at_decoration():
    def odd(x: complex) -> str:
        """Unsupported."""
        return str(x)

    with pytest.raises(TypeError, match="unsupported parameter type"):
        registry_with(odd, risk=Risk.READ)


def test_duplicate_name_is_rejected():
    table: dict = {}

    @tool(risk=Risk.READ, registry=table)
    def dup(x: str) -> str:
        """First."""
        return x

    with pytest.raises(ValueError, match="already registered"):
        @tool(risk=Risk.READ, registry=table)
        def dup(x: str) -> str:  # noqa: F811
            """Second."""
            return x


def test_decorator_returns_the_function_unchanged():
    table: dict = {}

    @tool(risk=Risk.READ, registry=table)
    def echo(x: str) -> str:
        """Echo."""
        return x

    assert echo("hi") == "hi"
    assert table["echo"].call({"x": "hi"}) == "hi"


def test_only_read_is_auto_allowed():
    assert needs_approval(Risk.READ) is False
    assert needs_approval(Risk.WRITE) is True
    assert needs_approval(Risk.EXEC) is True
    assert needs_approval(Risk.NETWORK) is True


def test_write_approval_request_carries_path_and_capped_preview():
    write = tools.TOOLS["write_file"]
    content = "x" * (MAX_PREVIEW_CHARS + 25)

    request = write.approval_request("1.1", {"path": "a.txt", "content": content})

    assert request.tool_name == "write_file"
    assert request.risk == "write"
    assert request.title == f"Write {len(content)} chars to 'a.txt'"
    assert request.preview == Preview(
        text="x" * MAX_PREVIEW_CHARS + "\n...[25 more chars]",
        path="a.txt",
        kind="text",
    )
    assert request.arguments == {"path": "a.txt", "content": content}


def test_write_preview_under_limit_is_full_content():
    request = tools.TOOLS["write_file"].approval_request("1.1", {"path": "a.txt", "content": "short"})
    assert request.preview.text == "short"


def test_exec_approval_request_carries_the_command():
    request = tools.TOOLS["run_command"].approval_request("1.1", {"command": "dir /b"})

    assert request.title == "Run command `dir /b`"
    assert request.preview == Preview(text="dir /b", kind="command")
    assert request.arguments == {"command": "dir /b"}


def test_tool_without_approval_text_gets_a_generic_title():
    table: dict = {}

    @tool(risk=Risk.NETWORK, registry=table)
    def ping(host: str) -> str:
        """Ping a host."""
        return host

    request = table["ping"].approval_request("9.9", {"host": "example.com"})
    assert request.title == "Run ping({'host': 'example.com'})"
    assert request.preview is None
    assert table["ping"].denied_message == "User denied ping."


def test_annotated_inside_optional_union_is_supported():
    def search(query: Annotated[str, "What to find"], limit: Annotated[int, "Max hits"] | None = None) -> str:
        """Search."""
        return ""

    schema = registry_with(search, risk=Risk.READ)["search"].parameters
    assert schema["properties"]["query"] == {"type": "string", "description": "What to find"}
    assert schema["properties"]["limit"] == {"type": "integer", "description": "Max hits"}
    assert schema["required"] == ["query"]


def test_optional_plain_type_has_no_description():
    def plain(x: str | None = None) -> str:
        """Plain."""
        return ""

    schema = registry_with(plain, risk=Risk.READ)["plain"].parameters
    assert schema["properties"]["x"] == {"type": "string", "description": ""}
