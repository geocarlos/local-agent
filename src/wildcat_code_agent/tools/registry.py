"""Tool registry: the `@tool` decorator, JSON schemas from type hints, risk and location.

    @tool(risk=Risk.WRITE, denied_message="User denied file write.",
          approval_title=_write_title, preview=_write_preview)
    def write_file(path: str, content: str) -> str:
        \"\"\"Write content to a file (asks for approval first).\"\"\"

Parameter types come from the annotations: str, int, float, bool, list[T],
dict, and `X | None` (optional). A parameter's description comes from
`Annotated[str, "description"]`; without one the schema carries an empty
description, which is what ollama's own function conversion produces.

A tool whose schema is not built from a signature (the MCP tools) is registered with
`add_tool`, which takes the parameters schema as given.

A tool that needs per-turn limits declares a parameter named `context` typed
as ToolContext. The core fills it in; it is not part of the schema the model sees.

A tool that takes a file path names the parameter in `paths=`, for example
`paths=("path",)`. The core resolves those arguments against the workspace before
the tool runs, and asks for a separate approval if one is outside it
(see wildcat_code_agent.sandbox). The tool then receives the resolved absolute path.
A path parameter may also be a list of paths (`list[str] | None`).

Hooks for the approval step: `approval_title` and `preview` build the question and
the preview; `precheck` returns an error text (or None) and runs before anything
is asked, so a call that cannot succeed is answered without a question. `allow_always`
(a bool, or a hook that returns a bool per call) says whether "allow for this session" is
offered for a call. The hooks get the tool's arguments by name, and `context` too if they
declare it.

A tool function must not print or read input. Approval is handled by the
core (see core/loop.py) through permissions.Approver.
"""

import inspect
import threading
import types
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Annotated, Literal, Union, get_args, get_origin, get_type_hints

from wildcat_code_agent.client_tools import ClientTools
from wildcat_code_agent.config import DEFAULT_COMMAND_TIMEOUT_S, DEFAULT_MAX_OUTPUT_CHARS, DEFAULT_NETWORK_TIMEOUT_S
from wildcat_code_agent.core.events import TOOL_APPROVAL_OPTIONS, ApprovalRequest, Preview, ToolKind
from wildcat_code_agent.permissions import Risk

CONTEXT_PARAM = "context"
ONE_TIME_OPTIONS = ("allow_once", "reject_once")  # offered when a call may not be allowed for the session


class ToolCancelled(Exception):
    """Raised by a tool that stopped because the turn was cancelled. The core answers "Cancelled by user."."""


@dataclass(frozen=True)
class ToolContext:
    """Limits, the workspace and test command the core passes to a tool call, taken from the settings for the turn."""

    command_timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    workspace: Path = field(default_factory=Path.cwd)  # where run_command starts
    test_command: str | None = None                   # the configured test_command, if any
    network_timeout_s: int = DEFAULT_NETWORK_TIMEOUT_S  # web_fetch: limit for the whole fetch, and per connect or read
    cancel: threading.Event | None = None             # set when the turn is cancelled; long tools may poll it
    plugin_dir: Path | None = None                    # the plugin folder: writes into it always ask (see sandbox.py)
    client: ClientTools | None = None                 # the editor, when the client advertised file or terminal methods (T20)


class Location(StrEnum):
    """Where a tool runs. CLIENT is not used by any tool: T20 routes the core's own tools through the client
    (ToolContext.client) when the client advertises it, so every built-in tool stays CORE."""

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
    if get_origin(hint) is Literal:
        return {"type": "string", "enum": list(get_args(hint))}
    if hint is dict or get_origin(hint) is dict:
        return {"type": "object"}
    if hint is list or get_origin(hint) is list:
        args = get_args(hint)
        return {"type": "array", "items": _type_schema(args[0]) if args else {}}
    raise TypeError(f"unsupported parameter type for tool schema: {hint!r}")


def _is_path_type(hint: Any) -> bool:
    """True for `str`, `str | None` (optional path), `list[str]` and `list[str] | None`, with or without Annotated."""
    base = _strip_annotated(hint)[0]
    if get_origin(base) in (Union, types.UnionType):
        args = [a for a in get_args(base) if a is not type(None)]
        if len(args) != 1:
            return False
        base = args[0]
    if base is str:
        return True
    return get_origin(base) is list and get_args(base) == (str,)


def _takes_context(fn: Callable[..., Any]) -> bool:
    """True if the function has a `context: ToolContext` parameter."""
    hints = get_type_hints(fn)
    return hints.get(CONTEXT_PARAM) is ToolContext


def _run_hook(fn: Callable[..., Any], arguments: Mapping[str, Any], context: ToolContext | None) -> Any:
    """Call an approval hook (approval_title, preview, precheck) with the tool's arguments, and context if it takes one."""
    if CONTEXT_PARAM in inspect.signature(fn).parameters:
        return fn(**arguments, **{CONTEXT_PARAM: context or ToolContext()})
    return fn(**arguments)


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
    precheck_hook: Callable[..., str | None] | None
    allow_always: Callable[..., bool] | bool = True
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
        # Literal parameters are enums in the schema; a value outside them is refused before the user is asked.
        # MCP schemas may have "properties": null or a boolean property schema; neither lists choices.
        properties = self.parameters.get("properties") or {}
        for name, value in arguments.items():
            prop = properties.get(name)
            choices = prop.get("enum") if isinstance(prop, Mapping) else None
            if choices is not None and value is not None and value not in choices:
                raise TypeError(f"{self.name}() argument {name!r} must be one of {choices}, got {value!r}")

    def call(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> str:
        """Run the tool. Raises on bad arguments or failures; the core turns that into a result."""
        return str(self.fn(**self._full_arguments(arguments, context)))

    def precheck(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> str | None:
        """An error text if this call cannot succeed, so it is answered without asking; None to go on."""
        if self.precheck_hook is None:
            return None
        return _run_hook(self.precheck_hook, dict(arguments), context)

    def session_allowed_for(self, arguments: Mapping[str, Any], context: ToolContext | None = None) -> bool:
        """Whether this call may be offered "allow for this session" and remembered when the user says so."""
        if isinstance(self.allow_always, bool):
            return self.allow_always
        return bool(_run_hook(self.allow_always, dict(arguments), context))

    def approval_request(
        self,
        tool_call_id: str,
        arguments: Mapping[str, Any],
        context: ToolContext | None = None,
    ) -> ApprovalRequest:
        """What the user is asked before this call runs. Offers "allow for this session" as well."""
        args = dict(arguments)
        title = _run_hook(self.approval_title, args, context) if self.approval_title else f"Run {self.name}({args})"
        preview = _run_hook(self.preview, args, context) if self.preview else None
        return ApprovalRequest(
            tool_call_id=tool_call_id,
            tool_name=self.name,
            risk=self.risk.value,
            title=title,
            arguments=args,
            preview=preview,
            options=TOOL_APPROVAL_OPTIONS if self.session_allowed_for(args, context) else ONE_TIME_OPTIONS,
        )


REGISTRY: dict[str, Tool] = {}

# While plugin modules are imported (see skip_name_collisions), a @tool whose name is taken is recorded here
# and skipped, instead of raising. None the rest of the time.
_skipped_names: list[str] | None = None


@contextmanager
def skip_name_collisions(skipped: list[str]) -> Iterator[None]:
    """While this is active, a @tool on REGISTRY whose name is taken is skipped and its name appended to `skipped`.

    Outside it, a duplicate name raises ValueError as before. Used by the plugin loader, so that a plugin
    tool can never replace a built-in tool or another plugin's tool.
    """
    global _skipped_names
    previous = _skipped_names
    _skipped_names = skipped
    try:
        yield
    finally:
        _skipped_names = previous


def add_tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
    fn: Callable[..., str],
    risk: Risk,
    approval_title: Callable[..., str] | None = None,
    preview: Callable[..., Preview | None] | None = None,
    allow_always: bool = True,
    takes_context: bool = False,
    registry: dict[str, Tool] | None = None,
) -> Tool:
    """Register a tool whose schema is given, not built from `fn`'s type hints (used for MCP tools).

    `fn` is called with the model's arguments as keyword arguments, plus `context` when
    `takes_context` is set. The name must not be registered already. Returns the Tool.
    """
    table = REGISTRY if registry is None else registry
    if name in table:
        raise ValueError(f"tool {name!r} is already registered")
    registered = Tool(
        name=name,
        description=description,
        parameters=parameters,
        fn=fn,
        risk=Risk(risk),
        location=Location.CORE,
        denied_message=f"User denied {name}.",
        approval_title=approval_title,
        preview=preview,
        precheck_hook=None,
        allow_always=allow_always,
        takes_context=takes_context,
    )
    table[name] = registered
    return registered


def tool(
    *,
    risk: Risk,
    location: Location = Location.CORE,
    denied_message: str | None = None,
    approval_title: Callable[..., str] | None = None,
    preview: Callable[..., Preview | None] | None = None,
    precheck: Callable[..., str | None] | None = None,
    allow_always: Callable[..., bool] | bool = True,
    paths: tuple[str, ...] = (),
    registry: dict[str, Tool] | None = None,
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Register a function as a tool. The function is returned unchanged.

    `approval_title`, `preview` and `precheck` receive the same keyword arguments as the tool
    (and `context` when they declare it). `precheck` returns an error text, or None to go on.
    `denied_message` is what the model receives when the user says no.
    `allow_always` decides whether "allow for this session" is offered and kept for a call: True (the
    default), False, or a hook (like precheck) that returns a bool for each call, for example per path.
    `paths` names the parameters that are file paths (each must be `str` or `list[str]`, optionally
    `| None`; an absent or null optional path is not resolved, so the tool sees None).
    `registry` defaults to REGISTRY; tests pass their own dict.
    """
    risk = Risk(risk)

    def wrap(fn: Callable[..., str]) -> Callable[..., str]:
        table = REGISTRY if registry is None else registry
        name = fn.__name__
        if name in table:
            if registry is None and _skipped_names is not None:
                _skipped_names.append(name)  # a plugin's duplicate: skipped, and reported by the loader
                return fn
            raise ValueError(f"tool {name!r} is already registered")
        description = inspect.getdoc(fn)
        if not description:
            raise TypeError(f"tool {name!r} needs a docstring; it becomes the tool description")
        hints = get_type_hints(fn, include_extras=True)
        for arg in paths:
            if arg not in hints:
                raise TypeError(f"tool {name!r}: paths names {arg!r}, which is not a parameter")
            if not _is_path_type(hints[arg]):
                raise TypeError(f"tool {name!r}: path parameter {arg!r} must be str, list[str], or either with | None")
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
            precheck_hook=precheck,
            allow_always=allow_always,
            takes_context=_takes_context(fn),
            path_args=tuple(paths),
        )
        return fn

    return wrap
