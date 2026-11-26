"""MCP tool naming, registration rules and result text, without starting any server."""

from mcp.types import (
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
    TextResourceContents,
)
import ollama
import pytest
from mcp.types import Tool as McpTool

from wildcat_code_agent.config import McpServer, Settings
from wildcat_code_agent.permissions import Risk
from wildcat_code_agent.tools import profiles
from wildcat_code_agent.tools.mcp_tools import (
    DESCRIPTION_CHARS,
    MAX_TOOL_NAME_CHARS,
    McpRuntime,
    _parameters,
    _register_server,
    qualified_name,
    result_text,
)
from wildcat_code_agent.tools.output import printable, without_controls
from wildcat_code_agent.tools.registry import Tool, ToolContext, add_tool

SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}}


def mcp_tool(name: str, description: str | None = "Does a thing.", schema: dict | None = None) -> McpTool:
    return McpTool(name=name, description=description, input_schema=schema or SCHEMA)


def register(tools, *, server: McpServer | None = None, registry: dict | None = None) -> McpRuntime:
    """Register the tools of one server into a private registry, as start_mcp_servers does."""
    registry = {} if registry is None else registry
    runtime = McpRuntime(registry)
    _register_server(runtime, server or McpServer(name="files", command="x"), tuple(tools))
    return runtime


def test_the_name_is_server_and_tool_joined_by_two_underscores():
    assert qualified_name("files", "read_file") == "files__read_file"


def test_characters_outside_letters_digits_underscore_and_hyphen_become_underscores():
    assert qualified_name("files", "read.file name!") == "files__read_file_name_"


def test_a_long_name_is_cut_to_the_limit_most_model_apis_accept():
    name = qualified_name("files", "x" * 200)
    assert len(name) == MAX_TOOL_NAME_CHARS == 64


def test_two_tools_that_sanitise_to_the_same_name_keep_the_first_and_warn_about_the_second():
    runtime = register([mcp_tool("a.b"), mcp_tool("a_b")])
    assert runtime.names == ["files__a_b"]
    (warning,) = runtime.warnings
    assert "'a_b'" in warning and "already taken" in warning


def test_a_name_already_in_the_registry_is_not_replaced():
    existing = {}
    add_tool(name="files__echo", description="built in", parameters=SCHEMA, fn=lambda **_: "x", risk=Risk.READ,
             registry=existing)
    runtime = register([mcp_tool("echo")], registry=existing)
    assert runtime.names == []
    assert existing["files__echo"].description == "built in"
    assert "already taken" in runtime.warnings[0]


def test_a_tool_with_an_argument_named_context_is_skipped_with_a_warning():
    schema = {"type": "object", "properties": {"context": {"type": "string"}}}
    runtime = register([mcp_tool("search", schema=schema), mcp_tool("other")])
    assert runtime.names == ["files__other"]
    assert "reserved" in runtime.warnings[0]


def test_a_plain_schema_keeps_its_type_and_required_list_and_its_title_becomes_the_description():
    schema = {"type": "object", "properties": {"a": {"type": "integer", "title": "A"}}, "required": ["a"]}
    expected = {"type": "object", "properties": {"a": {"type": "integer", "description": "A"}}, "required": ["a"]}
    registry = {}
    register([mcp_tool("add", schema=schema)], registry=registry)
    assert registry["files__add"].parameters == expected


def test_a_missing_schema_gets_an_empty_object_so_the_model_can_call_it():
    tool = McpTool.model_construct(name="ping", description=None, input_schema={})
    registry = {}
    register([tool], registry=registry)
    assert registry["files__ping"].parameters == {"type": "object", "properties": {}}


def test_a_long_description_is_trimmed_and_whitespace_is_collapsed():
    long = "Line one.\n\n" + "word " * 100
    registry = {}
    register([mcp_tool("wordy", description=long)], registry=registry)
    description = registry["files__wordy"].description
    assert len(description) <= DESCRIPTION_CHARS
    assert description.endswith("...") and "\n" not in description
    assert description.startswith("Line one. word word")


def test_the_default_risk_is_exec_and_the_server_and_tool_settings_apply():
    server = McpServer(name="files", command="x", risk="write", tool_risks={"read_it": "read"})
    registry = {}
    register([mcp_tool("read_it"), mcp_tool("write_it")], server=server, registry=registry)
    assert registry["files__read_it"].risk is Risk.READ
    assert registry["files__write_it"].risk is Risk.WRITE


def test_a_risk_override_for_a_tool_the_server_does_not_have_is_warned_about():
    server = McpServer(name="files", command="x", tool_risks={"missing": "read"})
    runtime = register([mcp_tool("here")], server=server)
    assert any("has no tool 'missing'" in w for w in runtime.warnings)


def test_the_approval_title_and_preview_show_the_server_tool_and_arguments():
    registry = {}
    register([mcp_tool("add", schema={"type": "object", "properties": {"a": {"type": "integer"}}})],
             registry=registry)
    tool = registry["files__add"]
    request = tool.approval_request("1.1", {"a": 2})
    assert "add" in request.title and "files" in request.title
    assert request.preview.kind == "text" and request.preview.path is None
    assert "Server: files" in request.preview.text and "Tool: add" in request.preview.text
    assert '"a": 2' in request.preview.text


def test_an_exec_tool_may_be_allowed_for_the_session_but_a_network_tool_may_not():
    registry = {}
    register([mcp_tool("ask")], registry=registry)
    register([mcp_tool("fetch")], server=McpServer(name="net", command="x", risk="network"), registry=registry)
    assert "allow_always" in registry["files__ask"].approval_request("1.1", {"text": "x"}).options
    assert "allow_always" not in registry["net__fetch"].approval_request("1.1", {"text": "x"}).options


def test_mcp_tools_are_filtered_by_profile_like_any_other_tool():
    registry = {}
    server = McpServer(name="net", command="x", risk="network")
    register([mcp_tool("fetch")], server=server, registry=registry)
    assert "net__fetch" not in profiles.offered_tools(registry, Settings())
    assert "net__fetch" in profiles.offered_tools(registry, Settings(tool_profile="all", network_enabled=True))
    register([mcp_tool("ask")], registry=registry)  # exec by default
    assert "files__ask" in profiles.offered_tools(registry, Settings())
    assert "files__ask" not in profiles.offered_tools(registry, Settings(tool_profile="minimal"))


def test_a_tool_takes_the_context_so_it_gets_the_limits_and_the_cancel_event():
    registry = {}
    register([mcp_tool("ping")], registry=registry)
    tool: Tool = registry["files__ping"]
    assert tool.takes_context is True
    assert "context" not in tool.parameters["properties"]
    assert ToolContext().cancel is None


def test_text_parts_are_joined_with_newlines():
    result = CallToolResult(content=[TextContent(text="first"), TextContent(text="second")])
    assert result_text(result, 1000) == "first\nsecond"


def test_images_audio_and_links_are_summarised_in_a_short_line():
    result = CallToolResult(content=[
        ImageContent(data="AAAA", mime_type="image/png"),
        ResourceLink(name="doc", uri="file:///notes.md"),
    ])
    text = result_text(result, 1000)
    assert text == "[image: image/png, about 3 bytes]\n[resource link: file:///notes.md]"


def test_an_embedded_text_resource_shows_its_text_and_a_binary_one_is_summarised():
    text_resource = EmbeddedResource(resource=TextResourceContents(uri="mem://a", text="inside"))
    binary = EmbeddedResource(resource=BlobResourceContents(uri="mem://b", blob="AAAA", mime_type="application/pdf"))
    text = result_text(CallToolResult(content=[text_resource, binary]), 1000)
    assert "[resource: mem://a]\ninside" in text
    assert "[binary resource: mem://b, application/pdf]" in text


def test_an_error_result_is_marked_as_an_error():
    text = result_text(CallToolResult(content=[TextContent(text="bad input")], is_error=True), 1000)
    assert text == "Error: bad input"


def test_an_error_result_without_text_still_says_so():
    assert result_text(CallToolResult(content=[], is_error=True), 1000) == \
        "Error: the MCP tool reported an error without a message."


def test_structured_content_is_used_when_there_is_no_text():
    assert result_text(CallToolResult(content=[], structured_content={"result": 5}), 1000) == '{"result": 5}'


def test_an_empty_result_says_it_had_no_content():
    assert result_text(CallToolResult(content=[]), 1000) == "(the tool returned no content)"


def test_long_output_is_truncated_with_the_shared_marker():
    text = result_text(CallToolResult(content=[TextContent(text="x" * 50)]), 10)
    assert text.startswith("x" * 10) and "...[truncated 40 chars]" in text


# --- terminal safety: a server's names and text are shown with control characters escaped -----------

CONTROL_CHARS = [chr(c) for c in range(0x20)] + [chr(c) for c in range(0x7F, 0xA0)] + [" ", "‮"]


def _assert_no_controls(text: str, *, allow_newlines: bool = False) -> None:
    banned = set(CONTROL_CHARS) - ({"\n"} if allow_newlines else set())
    assert not banned & set(text), repr(text)


def test_printable_escapes_controls_and_keeps_newlines_only_when_asked():
    assert printable("a\x1b[2Jb") == "a\\x1b[2Jb"
    assert printable("line\nnext\tx\r") == "line\nnext\tx\\r"
    assert printable("line\nnext\tx", single_line=True) == "line\\nnext\\tx"
    assert printable("‮\x85") == "\\u202e\\x85"
    assert printable("plain text, ünïcode") == "plain text, ünïcode"


def test_without_controls_replaces_each_control_with_a_space():
    assert without_controls("a\x1b[31mb\r\nc") == "a [31mb  c"


def test_a_tool_name_with_controls_shows_only_safe_names_in_the_title_and_preview():
    evil = "evil\nname\x1b[2J"
    registry = {}
    register([mcp_tool(evil, schema={"type": "object", "properties": {"text": {"type": "string"}}})],
             registry=registry)
    (name,) = registry
    assert name == "files__evil_name__2J"
    request = registry[name].approval_request("1.1", {"text": "hi"})
    _assert_no_controls(request.title)
    _assert_no_controls(request.preview.text, allow_newlines=True)
    assert "Run MCP tool 'files__evil_name__2J'" in request.title
    assert "(the server calls it 'evil\\nname\\x1b[2J')" in request.title
    assert "Tool: evil_name__2J (the server calls it 'evil\\nname\\x1b[2J')" in request.preview.text


def test_a_plain_tool_name_has_no_alias_note():
    registry = {}
    register([mcp_tool("add")], registry=registry)
    request = registry["files__add"].approval_request("1.1", {"text": "x"})
    assert "calls it" not in request.title and "Tool: add\n" in request.preview.text


def test_control_characters_in_arguments_are_escaped_in_the_preview():
    registry = {}
    register([mcp_tool("ask")], registry=registry)
    request = registry["files__ask"].approval_request("1.1", {"text": "a\x1b[2Jb\x85‮"})
    _assert_no_controls(request.preview.text, allow_newlines=True)
    # json.dumps writes ESC as \u001b (below 0x20 only); the C1 control and the bidi override are escaped here.
    assert '"a\\u001b[2Jb\\x85\\u202e"' in request.preview.text


def test_control_characters_in_a_description_are_removed_before_the_model_sees_it():
    registry = {}
    register([mcp_tool("wild", description="Red\x1b[31m text\r\nsecond\x00line")], registry=registry)
    description = registry["files__wild"].description
    _assert_no_controls(description)
    assert description == "Red [31m text second line"


def test_a_skipped_tool_warning_shows_the_server_name_escaped():
    runtime = register([mcp_tool("p.q"), mcp_tool("p\x1bq")])
    (warning,) = runtime.warnings
    _assert_no_controls(warning)
    assert "'p\\x1bq'" in warning and "already taken" in warning


def test_a_risk_setting_for_a_missing_tool_warns_with_the_name_escaped():
    server = McpServer(name="files", command="x", tool_risks={"no\nsuch\x1b": "read"})
    runtime = register([mcp_tool("here")], server=server)
    (warning,) = runtime.warnings
    _assert_no_controls(warning)
    assert "'no\\nsuch\\x1b'" in warning


# --- schema normalisation: what the ollama client keeps of each property --------------------------

def _params(schema: dict) -> dict:
    return _parameters(McpTool(name="t", description="d", inputSchema=schema))


def test_an_optional_property_becomes_its_type_with_its_description():
    schema = {"type": "object", "properties": {
        "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "title": "Note", "description": "A note"},
        "count": {"anyOf": [{"type": "integer"}, {"type": "null"}], "title": "Count"},
    }}
    props = _params(schema)["properties"]
    assert props["note"] == {"type": "string", "description": "A note"}
    assert props["count"] == {"type": "integer", "description": "Count"}  # the title stands in for a description


def test_a_reference_to_a_definition_is_inlined_with_the_property_description():
    schema = {
        "type": "object",
        "$defs": {"Address": {"type": "object", "description": "A postal address", "properties": {}}},
        "properties": {"home": {"$ref": "#/$defs/Address", "description": "Home"}},
        "required": ["home"],
    }
    params = _params(schema)
    assert params["properties"] == {"home": {"type": "object", "description": "Home"}}
    assert "$defs" not in params and params["required"] == ["home"]


def test_an_array_of_definitions_keeps_its_items_type():
    schema = {"type": "object", "$defs": {"Item": {"type": "object", "description": "One item"}},
              "properties": {"items": {"type": "array", "items": {"$ref": "#/definitions/Item"},
                                       "description": "Items"}}}
    assert _params(schema)["properties"]["items"] == {
        "type": "array", "description": "Items", "items": {"type": "object", "description": "One item"}}


def test_a_reference_that_cycles_back_to_itself_stops_with_only_its_description():
    schema = {"type": "object", "$defs": {"Loop": {"$ref": "#/$defs/Loop"}},
              "properties": {"x": {"$ref": "#/$defs/Loop", "description": "Loops"}, "y": {"$ref": "#/$defs/Gone"}}}
    assert _params(schema)["properties"] == {"x": {"description": "Loops"}, "y": {}}


def test_several_simple_types_are_listed_and_an_untyped_option_leaves_no_type():
    schema = {"type": "object", "properties": {
        "mixed": {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}], "description": "Either"},
        "shape": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
        "loose": {"oneOf": [{"const": 1}, {"type": "string"}], "description": "Loose"},
    }}
    props = _params(schema)["properties"]
    assert props["mixed"] == {"type": ["string", "integer"], "description": "Either"}
    assert props["shape"] == {"type": ["string", "array"]}
    assert props["loose"] == {"description": "Loose"}


def test_a_nullable_type_list_drops_the_null():
    schema = {"type": "object", "properties": {"n": {"type": ["integer", "null"], "description": "N"}}}
    assert _params(schema)["properties"]["n"] == {"type": "integer", "description": "N"}


def test_null_properties_and_boolean_property_schemas_do_not_break_the_schema():
    assert _params({"type": "object", "properties": None})["properties"] == {}
    assert _params({"type": "object", "properties": {"any": True, "none": False}})["properties"] == {
        "any": {}, "none": {}}


def test_a_pydantic_model_schema_round_trips_through_the_ollama_tool_model():
    from pydantic import BaseModel, Field

    class Address(BaseModel):
        street: str
        city: str

    class Args(BaseModel):
        note: str | None = Field(None, description="Optional note")
        home: Address = Field(description="Home address")
        stops: list[Address] = Field(description="Stops on the route")
        count: int | None = None

    mcp = McpTool(name="route", description="Plan a route.", inputSchema=Args.model_json_schema())
    registry = {}
    register([mcp], registry=registry)
    dumped = ollama.Tool.model_validate(registry["files__route"].schema()).model_dump(exclude_none=True)
    params = dumped["function"]["parameters"]
    props = params["properties"]
    assert props["note"] == {"type": "string", "description": "Optional note"}
    assert props["home"]["type"] == "object" and props["home"]["description"] == "Home address"
    assert props["stops"]["type"] == "array" and props["stops"]["description"] == "Stops on the route"
    assert props["stops"]["items"]["type"] == "object"
    assert props["count"]["type"] == "integer"
    assert "$defs" not in params


def test_bind_accepts_null_properties_and_boolean_property_schemas_and_still_checks_enums():
    registry = {}
    add_tool(name="wild__tool", description="d", parameters={"type": "object", "properties": None},
             fn=lambda **_: "x", risk=Risk.READ, registry=registry)
    add_tool(name="wild__bool", description="d", parameters={"type": "object", "properties": {"a": True}},
             fn=lambda **_: "x", risk=Risk.READ, registry=registry)
    add_tool(name="wild__enum", description="d",
             parameters={"type": "object", "properties": {"a": {"enum": ["x"]}}},
             fn=lambda **_: "x", risk=Risk.READ, registry=registry)
    registry["wild__tool"].bind({"anything": 1})
    registry["wild__bool"].bind({"a": 1})
    registry["wild__enum"].bind({"a": "x"})
    with pytest.raises(TypeError, match="must be one of"):
        registry["wild__enum"].bind({"a": "y"})

