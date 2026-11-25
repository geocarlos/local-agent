"""MCP tools: start the configured servers and register their tools in the tool registry.

A server's tool is registered as `<server>__<tool>` (server names cannot contain "_", so the
first "__" always ends the server part). Characters outside [A-Za-z0-9_-] become "_", and the
name is cut to 64 characters. A name that is already taken is skipped with a warning.

The tool's parameters are the server's input schema, as it is. The risk tier is the server's
`risk` (default exec, which always asks), or the tool's own `risk` in the config. Each call
runs on the server, so it is not path-sandboxed; the approval shows the server, the tool and
the arguments (JSON). Profiles filter these tools by tier like any other tool.
"""

import json
import re
from collections.abc import Mapping
from typing import Any

from mcp.client.stdio import StdioServerParameters
from mcp.types import (
    AudioContent,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
    TextResourceContents,
)
from mcp.types import Tool as McpTool

from local_agent.config import McpServer
from local_agent.core.events import Preview
from local_agent.mcp_hub import CallCancelled, CallFailed, McpHub, ServerSpec
from local_agent.permissions import Risk
from local_agent.tools.edit_tools import cap_preview
from local_agent.tools.output import printable, truncate, without_controls
from local_agent.tools.registry import REGISTRY, Tool, ToolCancelled, ToolContext, add_tool

NAME_SEPARATOR = "__"
MAX_TOOL_NAME_CHARS = 64    # the limit most model APIs put on function names
DESCRIPTION_CHARS = 200     # the T08 limit for built-in descriptions, applied to MCP ones too
RESERVED_ARGUMENT = "context"  # the registry passes ToolContext under this name, so a server argument cannot use it
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")
# What the ollama client keeps of a property (its Tool model drops every other key), and so what the model sees.
_KEPT_PROPERTY_KEYS = ("type", "items", "description", "enum")


def qualified_name(server: str, tool: str) -> str:
    """The name the model sees for a server's tool: `<server>__<tool>`, sanitised and at most 64 characters."""
    return f"{server}{NAME_SEPARATOR}{_UNSAFE_CHARS.sub('_', tool)}"[:MAX_TOOL_NAME_CHARS]


class McpRuntime:
    """The servers started for this run and the tools registered from them. `close()` on exit."""

    def __init__(self, registry: dict[str, Tool]) -> None:
        self.hub: McpHub | None = None
        self.warnings: list[str] = []  # lines for the user, for servers or tools that were skipped
        self.names: list[str] = []     # the registered tool names, removed again on close
        self.registry = registry

    def close(self) -> None:
        for name in self.names:
            self.registry.pop(name, None)
        self.names.clear()
        if self.hub is not None:
            self.hub.close()


def start_mcp_servers(servers: Mapping[str, McpServer], *, timeout_s: float,
                      registry: dict[str, Tool] = REGISTRY) -> McpRuntime:
    """Start the enabled servers together, list their tools and register them.

    A server that fails to start (or does not list its tools in `timeout_s`) gives a warning and is left out;
    nothing is raised for it. Call `close()` on the result when the agent exits.
    """
    runtime = McpRuntime(registry)
    enabled = [server for server in servers.values() if server.enabled]
    if not enabled:
        return runtime
    runtime.hub = McpHub()
    try:
        starts = runtime.hub.start_servers([ServerSpec(s.name, _launch_params(s)) for s in enabled], timeout_s)
    except BaseException:
        runtime.close()
        raise
    by_name = {server.name: server for server in enabled}
    for start in starts:
        server = by_name[start.name]
        if start.error is not None:
            runtime.warnings.append(f"Warning: MCP server {_quote(start.name)} did not start "
                                    f"({printable(start.error, single_line=True)}). "
                                    "The agent continues without its tools.")
            continue
        _register_server(runtime, server, start.tools)
    return runtime


def _launch_params(server: McpServer) -> StdioServerParameters:
    return StdioServerParameters(command=server.command, args=list(server.args),
                                 env=dict(server.env) if server.env else None, cwd=server.cwd)


def _register_server(runtime: McpRuntime, server: McpServer, tools: tuple[McpTool, ...]) -> None:
    listed = {tool.name for tool in tools}
    for configured in server.tool_risks:
        if configured not in listed:
            runtime.warnings.append(f"Warning: MCP server {_quote(server.name)} has no tool {_quote(configured)}; "
                                    "its risk setting in the config is ignored.")
    for mcp_tool in tools:
        name = qualified_name(server.name, mcp_tool.name)
        problem = _problem(name, mcp_tool, runtime.registry)
        if problem is not None:
            runtime.warnings.append(f"Warning: MCP tool {_quote(mcp_tool.name)} on server {_quote(server.name)} "
                                    f"was skipped: {problem}.")
            continue
        tier = Risk(server.tool_risks.get(mcp_tool.name, server.risk))
        add_tool(
            name=name,
            description=_short_description(mcp_tool, server.name),
            parameters=_parameters(mcp_tool),
            fn=_make_call(runtime, server.name, mcp_tool.name, name),
            risk=tier,
            approval_title=_title(server.name, mcp_tool.name, name),
            preview=_preview(server.name, mcp_tool.name),
            # Network tools never get "allow for this session", as web_fetch does not.
            allow_always=tier is not Risk.NETWORK,
            takes_context=True,
            registry=runtime.registry,
        )
        runtime.names.append(name)


def _problem(name: str, mcp_tool: McpTool, registry: Mapping[str, Tool]) -> str | None:
    """Why this tool cannot be registered under `name`, or None."""
    properties = (mcp_tool.input_schema or {}).get("properties") or {}
    if RESERVED_ARGUMENT in properties:
        return f"its argument '{RESERVED_ARGUMENT}' is reserved by the agent"
    if name in registry:
        return f"the name '{name}' is already taken"
    return None


def _parameters(mcp_tool: McpTool) -> dict[str, Any]:
    """The server's input schema for the model. Each property is reduced to what the ollama client keeps
    (see `_property`); the arguments the model sends are passed on unchanged."""
    schema = dict(mcp_tool.input_schema or {})
    defs = {**_as_mapping(schema.pop("$defs", None)), **_as_mapping(schema.pop("definitions", None))}
    schema.setdefault("type", "object")
    properties = schema.get("properties") or {}
    schema["properties"] = {name: _property(prop, defs) for name, prop in properties.items()}
    return schema


def _property(schema: Any, defs: Mapping[str, Any], active: tuple[str, ...] = ()) -> dict[str, Any]:
    """One property schema, reduced to `_KEPT_PROPERTY_KEYS` (the description also gets the title, if it has none).

    A `$ref` to `#/$defs/...` or `#/definitions/...` is inlined. A reference that is missing or cycles back to
    itself leaves only the description, since its type is unknown here. `anyOf`/`oneOf` with one non-null schema
    becomes that schema; with several, `type` lists their types when each one has a simple type. A boolean or
    missing schema accepts anything and becomes `{}`.
    """
    if not isinstance(schema, Mapping):
        return {}
    if isinstance(ref := schema.get("$ref"), str):
        rest = {k: v for k, v in schema.items() if k != "$ref"}
        target = _resolve(ref, defs)
        if target is None or ref in active:
            return _described(rest)
        return _property({**target, **rest}, defs, (*active, ref))
    for key in ("anyOf", "oneOf"):
        if isinstance(options := schema.get(key), list):
            return _choice(options, {k: v for k, v in schema.items() if k != key}, defs, active)
    out = _described(schema)
    kind = schema.get("type")
    if isinstance(kind, list):  # JSON-schema style nullable, for example ["string", "null"]
        kinds = [k for k in kind if k != "null"]
        kind = kinds[0] if len(kinds) == 1 else (kinds or None)
    if kind:
        out["type"] = kind
    if "enum" in schema:
        out["enum"] = schema["enum"]
    if "items" in schema:
        out["items"] = _property(schema["items"], defs, active)
    return out


def _choice(options: list, rest: Mapping[str, Any], defs: Mapping[str, Any], active: tuple[str, ...]) -> dict:
    """An anyOf/oneOf: the non-null options, with the outer schema's own keys (such as its description) kept."""
    concrete = [option for option in options if not _is_null(option)]
    if not concrete:
        return _described(rest)
    if len(concrete) == 1:
        return _property({**_as_mapping(concrete[0]), **rest}, defs, active)
    out = _described(rest)
    kinds = [_property(option, defs, active).get("type") for option in concrete]
    if all(isinstance(kind, str) for kind in kinds):
        names = list(dict.fromkeys(kinds))
        out["type"] = names[0] if len(names) == 1 else names
    return out


def _is_null(schema: Any) -> bool:
    return isinstance(schema, Mapping) and schema.get("type") == "null"


def _resolve(ref: str, defs: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The schema a one-level local reference names, or None if it is not in the schema."""
    for prefix in ("#/$defs/", "#/definitions/"):
        if ref.startswith(prefix):
            return _as_mapping(defs.get(ref[len(prefix):])) or None
    return None


def _described(source: Mapping[str, Any]) -> dict[str, Any]:
    """{"description": ...} from the description (or else the title), with control characters removed."""
    text = source.get("description") or source.get("title")
    if not isinstance(text, str):
        return {}
    cleaned = " ".join(without_controls(text).split())
    return {"description": cleaned} if cleaned else {}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _short_description(mcp_tool: McpTool, server: str) -> str:
    text = mcp_tool.description or mcp_tool.title or f"Tool {mcp_tool.name} on MCP server {server}."
    text = " ".join(without_controls(text).split())
    if len(text) <= DESCRIPTION_CHARS:
        return text
    return text[:DESCRIPTION_CHARS - 3].rstrip() + "..."


def _make_call(runtime: McpRuntime, server: str, tool: str, name: str):
    """The function the registry calls for this tool: one call to the server, answered as text."""

    def call(context: ToolContext, **arguments: Any) -> str:
        if runtime.hub is None:  # not reachable after start; kept so the tool fails clearly
            return f"Error: {name} did not complete: MCP server '{server}' is not running."
        try:
            result = runtime.hub.call_tool(server, tool, arguments, timeout_s=context.command_timeout_s,
                                           cancel=context.cancel)
        except CallCancelled:
            raise ToolCancelled() from None
        except CallFailed as e:
            return f"Error: {name} did not complete: {e}."
        return result_text(result, context.max_output_chars)

    return call


def _quote(text: str) -> str:
    """A server-derived name in quotes, for a line on the terminal: control characters shown as escapes."""
    return "'" + printable(text, single_line=True) + "'"


def _alias_note(tool: str) -> str:
    """" (the server calls it '...')" when the server's tool name has characters the model name does not,
    with the server's own name shown with escapes; otherwise ""."""
    return "" if _UNSAFE_CHARS.sub("_", tool) == tool else f" (the server calls it {_quote(tool)})"


def _tool_label(tool: str) -> str:
    """The tool name as the model sees it (letters, digits, "_" and "-"), plus the server's own name if it differs."""
    return _UNSAFE_CHARS.sub("_", tool) + _alias_note(tool)


def _title(server: str, tool: str, name: str):
    def title(**_arguments: Any) -> str:
        return f"Run MCP tool '{name}'{_alias_note(tool)} on server {_quote(server)}"

    return title


def _preview(server: str, tool: str):
    def preview(**arguments: Any) -> Preview:
        # json.dumps escapes control characters below 0x20 only, so the rest are escaped here.
        body = printable(json.dumps(arguments, indent=2, ensure_ascii=False, default=str))
        text = (f"Server: {printable(server, single_line=True)}\n"
                f"Tool: {_tool_label(tool)}\nArguments:\n{body}")
        return Preview(text=cap_preview(text), kind="text")

    return preview


def result_text(result: CallToolResult, limit: int) -> str:
    """The model's text for a tool result: text parts joined, other parts summarised in a line, errors marked."""
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif isinstance(block, ImageContent):
            parts.append(f"[image: {block.mime_type}, about {_decoded_size(block.data)} bytes]")
        elif isinstance(block, AudioContent):
            parts.append(f"[audio: {block.mime_type}, about {_decoded_size(block.data)} bytes]")
        elif isinstance(block, ResourceLink):
            parts.append(f"[resource link: {block.uri}]")
        elif isinstance(block, EmbeddedResource):
            resource = block.resource
            if isinstance(resource, TextResourceContents):
                parts.append(f"[resource: {resource.uri}]\n{resource.text}")
            else:
                parts.append(f"[binary resource: {resource.uri}, {resource.mime_type or 'unknown type'}]")
        else:
            parts.append(f"[{type(block).__name__} content]")
    text = "\n".join(parts)
    if not text and result.structured_content is not None:
        text = json.dumps(result.structured_content, ensure_ascii=False, default=str)
    if result.is_error:
        text = "Error: " + (text or "the MCP tool reported an error without a message.")
    return truncate(text or "(the tool returned no content)", limit)


def _decoded_size(data: str) -> int:
    """Approximate size in bytes of base64 data (the summary says "about")."""
    return len(data) * 3 // 4
