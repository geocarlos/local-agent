"""Tool registry: the `@tool` decorator, JSON schemas from type hints, risk and location.

    @tool(risk=Risk.WRITE, denied_message="User denied file write.",
          approval_title=_write_title, preview=_write_preview)
    def write_file(path: str, content: str) -> str:
        \"\"\"Write content to a file (asks for approval first).\"\"\"

Parameter types come from the annotations: str, int, float, bool, list[T],
dict, and `X | None` (optional). A parameter's description comes from
`Annotated[str, "description"]`; without one the schema carries an empty
description, which is what ollama's own function conversion produces.

A tool that needs per-turn limits declares a parameter named `context` typed
as ToolContext. The core fills it in; it is not part of the schema the model sees.

A tool that takes a file path names the parameter in `paths=`, for example
`paths=("path",)`. The core resolves those arguments against the workspace before
the tool runs, and asks for a separate approval if one is outside it
(see local_agent.sandbox). The tool then receives the resolved absolute path.

A tool function must not print or read input. Approval is handled by the
core (see core/loop.py) through permissions.Approver.
"""

import inspect
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Annotated, Union, get_args, get_origin, get_type_hints

from local_agent.config import DEFAULT_COMMAND_TIMEOUT_S, DEFAULT_MAX_OUTPUT_CHARS
from local_agent.core.events import ApprovalRequest, Preview, ToolKind
from local_agent.permissions import Risk

CONTEXT_PARAM = "context"


@dataclass(frozen=True)
class ToolContext:
    """Limits and the workspace the core passes to a tool call, taken from the settings for the turn."""

    command_timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    workspace: Path = field(default_factory=Path.cwd)  # where run_command starts


class Location(StrEnum):
    """Where a tool runs. CLIENT tools are executed by the frontend (T20), not the core."""

    CORE = "core"
    CLIENT = "client"


_KIND_FOR_RISK: dict[Risk, ToolKind] = {
    Risk.READ: "read",
    Risk.WRITE: "edit",
    Risk.EXEC: "execute",
    Risk.NETWORK: "fetch",
}


def _strip_annotated(hint: Any) -> tuple[Any, str]:
    """Remove Annotated wrappers, also inside unions. Returns (plain hint, description)."""
    if get_origin(hint) is Annotated:
        base, *meta = get_args(hint)
        description = next((m for m in meta if isinstance(m, str)), "")
        return _strip_annotated(base)[0], description
    if get_origin(hint) in (Union, types.UnionType):
        stripped = [_strip_annotated(a) for a in get_args(hint)]
        description = next((d for _, d in stripped if d), "")
        return Union[tuple(b for b, _ in stripped)], description
    return hint, ""


def _type_schema(hint: Any) -> dict:
    """JSON schema for one type hint (Annotated wrappers are ignored here)."""
    if get_origin(hint) is Annotated:
        return _type_schema(get_args(hint)[0])
    if get_origin(hint) in (Union, types.UnionType):
        args = [a for a in get_args(hint) if a is not type(None)]
        if len(args) != 1:
            raise TypeError(f"unsupported union type: {hint!r}")
        return _type_schema(args[0])
    if hint is bool:  # before int: bool is a subclass of int
        return {"type": "boolean"}
    if hint is int:
        return {"type": "integer"}
    if hint is float:
        return {"type": "number"}
    if hint is str:
        return {"type": "string"}
    if hint is dict or get_origin(hint) is dict:
        return {"type": "object"}
    if hint is list or get_origin(hint) is list:
        args = get_args(hint)
        return {"type": "array", "items": _type_schema(args[0]) if args else {}}
    raise TypeError(f"unsupported parameter type for tool schema: {hint!r}")


def _takes_context(fn: Callable[..., Any]) -> bool:
    """True if the function has a `context: ToolContext` parameter."""
    hints = get_type_hints(fn)
    return hints.get(CONTEXT_PARAM) is ToolContext


def _parameters_schema(fn: Callable[..., Any]) -> dict:
    """The `parameters` object of a function's JSON schema (the context parameter is left out)."""
    hints = get_type_hints(fn, include_extras=True)
    properties: dict[str, dict] = {}
    required: list[str] = []
    for name, param in inspect.signature(fn).parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise TypeError(f"{fn.__name__}: *args and **kwargs are not supported in tools")
        if name not in hints:
            raise TypeError(f"{fn.__name__}: parameter {name!r} needs a type hint")
        if name == CONTEXT_PARAM and hints[name] is ToolContext:
            continue
        hint, description = _strip_annotated(hints[name])
        properties[name] = {**_type_schema(hint), "description": description}
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "required": required, "properties": properties}


@dataclass(frozen=True)
class Tool:
    """A registered tool. Built by the `@tool` decorator; do not construct by hand."""

    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., str]
    risk: Risk
    location: Location
    denied_message: str
    approval_title: Callable[..., str] | None
    preview: Callable[..., Preview | None] | None
    takes_context: bool = False
    path_args: tuple[str, ...] = ()

    @property
    def kind(self) -> ToolKind:
        """ACP ToolKind for this tool, derived from its risk tier."""
        return _KIND_FOR_RISK[self.risk]

    def schema(self) -> dict:
        """The tool as ollama's `tools=` entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def _full_arguments(self, arguments: Mapping[str, Any], context: ToolContext | None) -> dict[str, Any]:
        """The keyword arguments for `fn`: the model's arguments plus the context, if the tool takes one."""
        args = dict(arguments)
        if self.takes_context:
            if CONTEXT_PARAM in args:
                raise TypeError(f"{self.name}() got an unexpected argument {CONTEXT_PARAM!r}")
            args[CONTEXT_PARAM] = context or ToolContext()
        return args

    def bind(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> None:
        """Check the arguments against the function signature without calling it.

        Raises TypeError naming this tool, for example "write_file() missing a required argument: 'content'".
        """
        try:
            inspect.signature(self.fn).bind(**self._full_arguments(arguments, context))
        except TypeError as e:
            raise TypeError(f"{self.name}() {e}") from None

    def call(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> str:
        """Run the tool. Raises on bad arguments or failures; the core turns that into a result."""
        return str(self.fn(**self._full_arguments(arguments, context)))

    def approval_request(self, tool_call_id: str, arguments: Mapping[str, Any]) -> ApprovalRequest:
        """What the user is asked before this call runs."""
        args = dict(arguments)
        title = self.approval_title(**args) if self.approval_title else f"Run {self.name}({args})"
        preview = self.preview(**args) if self.preview else None
        return ApprovalRequest(
            tool_call_id=tool_call_id,
            tool_name=self.name,
            risk=self.risk.value,
            title=title,
            arguments=args,
            preview=preview,
        )


REGISTRY: dict[str, Tool] = {}


def tool(
    *,
    risk: Risk,
    location: Location = Location.CORE,
    denied_message: str | None = None,
    approval_title: Callable[..., str] | None = None,
    preview: Callable[..., Preview | None] | None = None,
    paths: tuple[str, ...] = (),
    registry: dict[str, Tool] | None = None,
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Register a function as a tool. The function is returned unchanged.

    `approval_title` and `preview` receive the same keyword arguments as the tool.
    `denied_message` is what the model receives when the user says no.
    `paths` names the parameters that are file paths (each must be a `str` parameter).
    `registry` defaults to REGISTRY; tests pass their own dict.
    """
    risk = Risk(risk)

    def wrap(fn: Callable[..., str]) -> Callable[..., str]:
        table = REGISTRY if registry is None else registry
        name = fn.__name__
        if name in table:
            raise ValueError(f"tool {name!r} is already registered")
        description = inspect.getdoc(fn)
        if not description:
            raise TypeError(f"tool {name!r} needs a docstring; it becomes the tool description")
        hints = get_type_hints(fn, include_extras=True)
        for arg in paths:
            if arg not in hints:
                raise TypeError(f"tool {name!r}: paths names {arg!r}, which is not a parameter")
            if _strip_annotated(hints[arg])[0] is not str:
                raise TypeError(f"tool {name!r}: path parameter {arg!r} must be str")
        table[name] = Tool(
            name=name,
            description=description,
            parameters=_parameters_schema(fn),
            fn=fn,
            risk=risk,
            location=location,
            denied_message=denied_message or f"User denied {name}.",
            approval_title=approval_title,
            preview=preview,
            takes_context=_takes_context(fn),
            path_args=tuple(paths),
        )
        return fn

    return wrap
