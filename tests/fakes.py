"""Fake Ollama client and helpers for scripting model replies.

Replies are real `ollama.ChatResponse` objects, so both subscript access
(msg["content"]) and attribute access (msg.content) behave like the real client.

Streaming: `chat(stream=True)` returns an iterator of chunks. A plain reply is
split into chunks by `chunked()` (text in pieces, each tool call in its own
chunk). To script a stream by hand, pass a `Stream([...])` whose steps are
ChatResponse chunks, `Raise(exc)` (raised at that point of the stream) or
`Run(fn)` (called at that point, for example to set a cancel event).
"""

import copy
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import ollama


def text_reply(content: str, *, prompt_eval_count: int | None = None, eval_count: int | None = None,
               thinking: str | None = None) -> ollama.ChatResponse:
    """A scripted model reply with plain text and no tool calls. Token counts go on the final chunk."""
    return ollama.ChatResponse(
        model="fake",
        message=ollama.Message(role="assistant", content=content, thinking=thinking),
        prompt_eval_count=prompt_eval_count,
        eval_count=eval_count,
    )


def tool_reply(*calls: tuple[str, dict]) -> ollama.ChatResponse:
    """A scripted model reply that calls one or more tools: tool_reply(("read_file", {"path": "x"}))."""
    return ollama.ChatResponse(
        model="fake",
        message=ollama.Message(
            role="assistant",
            content="",
            tool_calls=[
                ollama.Message.ToolCall(function=ollama.Message.ToolCall.Function(name=name, arguments=args))
                for name, args in calls
            ],
        ),
    )


@dataclass(frozen=True)
class Raise:
    """Stream step: raise this exception at this point of the stream."""

    exc: BaseException


@dataclass(frozen=True)
class Run:
    """Stream step: call `fn` at this point of the stream (for example to set a cancel event)."""

    fn: Callable[[], None]


class Stream(list):
    """A scripted streamed reply: a list of ChatResponse chunks, Raise and Run steps."""


def chunked(response: ollama.ChatResponse, chars: int | None = None) -> Stream:
    """Split a reply into stream chunks: text in `chars`-sized pieces, one chunk per tool call, then a done chunk.

    The done chunk carries the reply's token counts, as Ollama's final chunk does.
    """
    message = response.message
    steps = Stream()
    text = message.content or ""
    if message.thinking:
        steps.append(chunk(thinking=message.thinking))
    if text:
        size = chars or len(text)
        for start in range(0, len(text), size):
            steps.append(chunk(content=text[start:start + size]))
    for call in message.tool_calls or []:
        steps.append(chunk(tool_calls=[call]))
    steps.append(chunk(content="", done=True, prompt_eval_count=response.prompt_eval_count,
                       eval_count=response.eval_count))
    return steps


def chunk(*, content: str = "", tool_calls=None, thinking: str | None = None, done: bool = False,
          prompt_eval_count: int | None = None, eval_count: int | None = None) -> ollama.ChatResponse:
    return ollama.ChatResponse(
        model="fake",
        message=ollama.Message(role="assistant", content=content, tool_calls=tool_calls, thinking=thinking),
        done=done,
        prompt_eval_count=prompt_eval_count,
        eval_count=eval_count,
    )


class FakeOllama:
    """Returns scripted responses in order and records every chat() call.

    If `repeat_last` is set, the last response is returned forever once the
    script runs out; otherwise running out fails the test. `chunk_chars` sets
    the size of text chunks when a reply is streamed (None: text in one chunk).
    `capabilities` is what show() reports; `show_error` is raised by show() instead.
    """

    def __init__(
        self,
        responses: list[ollama.ChatResponse | Stream],
        repeat_last: bool = False,
        chunk_chars: int | None = None,
        capabilities: tuple[str, ...] | None = ("completion", "tools"),
        show_error: BaseException | None = None,
        installed: tuple[str, ...] = ("qwen3-vl:8b", "qwen2.5-coder:7b"),
        list_error: BaseException | None = None,
    ):
        self.responses = list(responses)
        self.repeat_last = repeat_last
        self.chunk_chars = chunk_chars
        self.capabilities = capabilities
        self.show_error = show_error
        self.installed = installed
        self.list_error = list_error
        self.calls: list[dict] = []
        self.show_calls: list[str] = []

    def list(self):
        """The installed models (the list the model picker shows). `list_error` is raised instead."""
        if self.list_error is not None:
            raise self.list_error
        return ollama.ListResponse(models=[ollama.ListResponse.Model(model=name) for name in self.installed])

    def show(self, model):
        self.show_calls.append(model)
        if self.show_error is not None:
            raise self.show_error
        if model not in self.installed:  # Ollama answers 404 for a model it does not have
            raise ollama.ResponseError(f"model '{model}' not found", 404)
        capabilities = None if self.capabilities is None else list(self.capabilities)
        return ollama.ShowResponse(model="fake", model_info={}, capabilities=capabilities)

    def chat(self, *, model, messages, tools, stream=False, options=None):
        # Snapshot the history: the loop keeps appending to the same list, and the server may edit a message after the turn.
        self.calls.append({"model": model, "messages": copy.deepcopy(list(messages)), "tools": tools, "stream": stream,
                           "options": options})
        if len(self.calls) <= len(self.responses):
            item = self.responses[len(self.calls) - 1]
        elif self.repeat_last and self.responses:
            item = self.responses[-1]
        else:
            raise AssertionError(f"FakeOllama ran out of scripted responses after {len(self.responses)} calls")
        if not stream:
            if isinstance(item, Stream):
                raise AssertionError("a scripted Stream was requested without stream=True")
            return item
        steps = item if isinstance(item, Stream) else chunked(item, self.chunk_chars)
        return _iterate(steps)


def _iterate(steps) -> Iterator[ollama.ChatResponse]:
    for step in steps:
        if isinstance(step, Raise):
            raise step.exc
        if isinstance(step, Run):
            step.fn()
            continue
        yield step
