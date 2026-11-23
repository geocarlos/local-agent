"""The agent loop: one user turn of model <-> tool rounds.

Output still goes through print(); T02 replaces it with events.
"""

from typing import Any, Protocol

import ollama

from local_agent import tools

MODEL = "qwen3-vl:8b"  # Swap for gemma4:12b, qwen2.5-coder:7b, etc.
MAX_TOOL_ROUNDS = 10         # Max model<->tool rounds per user turn
SYSTEM_PROMPT = "You are a concise local coding assistant. Use tools to inspect and edit files."


class ChatClient(Protocol):
    """The part of the Ollama client the loop uses. The `ollama` module itself fits."""

    def chat(self, *, model: str, messages: list, tools: list) -> Any: ...


def new_messages() -> list:
    """Fresh conversation history with the system prompt."""
    return [{"role": "system", "content": SYSTEM_PROMPT}]


def run_turn(messages: list, client: ChatClient = ollama) -> str | None:
    """Run one user turn. `messages` must already end with the user message.

    Appends the model replies and tool results to `messages`. Returns the final
    reply text, or None if the turn hit MAX_TOOL_ROUNDS.
    """
    for _ in range(MAX_TOOL_ROUNDS):
        response = client.chat(
            model=MODEL,
            messages=messages,
            tools=list(tools.TOOLS.values()),
        )
        msg = response["message"]
        messages.append(msg)

        # If the model didn't call a tool, print its reply and end the turn
        if not msg.get("tool_calls"):
            print(f"\nAgent: {msg['content']}")
            return msg["content"]

        # Execute each requested tool and feed results back
        for call in msg["tool_calls"]:
            fn_name = call["function"]["name"]
            fn_args = call["function"]["arguments"]
            print(f"\n[Tool Call] {fn_name}({fn_args})")
            result = tools.execute_tool(fn_name, fn_args)
            messages.append({"role": "tool", "content": result})

    print(f"\nAgent: stopped after {MAX_TOOL_ROUNDS} tool rounds without a final answer. Ask again or narrow the request.")
    return None
