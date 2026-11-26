"""Fallback for models that print a tool call as text instead of a structured tool call.

Some models (qwen2.5-coder:7b in testing) answer with JSON such as
{"name": "read_file", "arguments": {"path": "a.txt"}}, either bare, in a
```json fence, or inside <tool_call> tags. parse_text_tool_calls turns such a
reply into real tool calls, so approvals and the history invariant apply as usual.

Rules, deliberately strict. Anything that does not match is plain text:
- The reply is only call blocks: one bare JSON object or array, or one or more
  fenced or <tool_call> blocks, with nothing else except whitespace.
- A call is an object with "name" (a registered tool) and "arguments" (an
  object; absent or null means {}; a JSON string holding an object is accepted).
- The arguments must bind to the tool's signature, as for a structured call.
- If any call in the reply fails a rule, the whole reply is plain text.
"""

import json
import re
from collections.abc import Mapping
from typing import Any

import ollama

from wildcat_code_agent.tools.registry import Tool

# A fenced block (optional language word) or a <tool_call> block. The group that matched holds the body.
_BLOCK = re.compile(
    r"```[ \t]*(?:[A-Za-z_]+)?[ \t]*\r?\n?(?P<fence>.*?)```|<tool_call>(?P<tag>.*?)</tool_call>",
    re.DOTALL | re.IGNORECASE,
)


def parse_text_tool_calls(content: str, registry: Mapping[str, Tool]) -> list[ollama.Message.ToolCall] | None:
    """The tool calls printed in `content`, or None if the reply is plain text."""
    text = content.strip()
    if not text:
        return None

    blocks = list(_BLOCK.finditer(text))
    if blocks:
        if _BLOCK.sub("", text).strip():  # prose around the blocks: not a bare tool call
            return None
        bodies = [m.group("fence") if m.group("fence") is not None else m.group("tag") for m in blocks]
    else:
        bodies = [text]

    payloads: list[Any] = []
    for body in bodies:
        try:
            value = json.loads(body.strip())
        except ValueError:
            return None
        if isinstance(value, dict):
            payloads.append(value)
        elif isinstance(value, list):
            payloads.extend(value)
        else:
            return None
    if not payloads:
        return None

    calls = []
    for payload in payloads:
        call = _to_call(payload, registry)
        if call is None:
            return None
        calls.append(call)
    return calls


def _to_call(payload: Any, registry: Mapping[str, Tool]) -> ollama.Message.ToolCall | None:
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    tool = registry.get(name) if isinstance(name, str) else None
    if tool is None:
        return None

    arguments = payload.get("arguments")
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    if not isinstance(arguments, dict):
        return None
    try:
        tool.bind(arguments)
    except TypeError:
        return None

    return ollama.Message.ToolCall(function=ollama.Message.ToolCall.Function(name=name, arguments=arguments))
