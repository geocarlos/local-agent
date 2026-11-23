"""Fake Ollama client and helpers for scripting model replies.

Replies are real `ollama.ChatResponse` objects, so both subscript access
(msg["content"]) and attribute access (msg.content) behave like the real client.
"""

import ollama


def text_reply(content: str) -> ollama.ChatResponse:
    """A scripted model reply with plain text and no tool calls."""
    return ollama.ChatResponse(
        model="fake",
        message=ollama.Message(role="assistant", content=content),
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


class FakeOllama:
    """Returns scripted responses in order and records every chat() call.

    If `repeat_last` is set, the last response is returned forever once the
    script runs out; otherwise running out fails the test.
    """

    def __init__(self, responses: list[ollama.ChatResponse], repeat_last: bool = False):
        self.responses = list(responses)
        self.repeat_last = repeat_last
        self.calls: list[dict] = []

    def chat(self, *, model, messages, tools):
        # Snapshot the history: the loop keeps appending to the same list.
        self.calls.append({"model": model, "messages": list(messages), "tools": tools})
        if len(self.calls) <= len(self.responses):
            return self.responses[len(self.calls) - 1]
        if self.repeat_last and self.responses:
            return self.responses[-1]
        raise AssertionError(f"FakeOllama ran out of scripted responses after {len(self.responses)} calls")
