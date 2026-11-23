"""Terminal frontend: the interactive REPL.

Renders core events as text and answers approval requests with y/n.
The text matches the pre-T02 output: "[Tool Call]", "[PROPOSED CONTENT]",
"[APPROVE]" and "Agent:" lines.
"""

import ollama

from local_agent.core import loop
from local_agent.core.events import (
    ApprovalRequest,
    Decision,
    Error,
    Event,
    TextDelta,
    ToolCall,
    TurnDone,
)


class TerminalApprover:
    """Asks y/n on the terminal. Anything other than 'y' is a refusal."""

    def decide(self, request: ApprovalRequest) -> Decision:
        answer = input(f"\n[APPROVE] {request.title}? (y/n): ").strip().lower()
        return Decision.ALLOW_ONCE if answer == "y" else Decision.REJECT_ONCE


def render(event: Event) -> None:
    """Print one core event. Tool results are not shown, as before."""
    if isinstance(event, TextDelta):
        print(f"\nAgent: {event.text}")
    elif isinstance(event, ToolCall):
        print(f"\n[Tool Call] {event.name}({event.arguments})")
    elif isinstance(event, ApprovalRequest):
        preview = event.preview
        if preview is not None and preview.kind == "text" and preview.path is not None:
            print(f"\n[PROPOSED CONTENT for '{preview.path}']\n{preview.text}\n[END PROPOSED CONTENT]")
    elif isinstance(event, TurnDone):
        if event.stop_reason == "max_turn_requests":
            print(f"\nAgent: stopped after {event.rounds} tool rounds without a final answer. "
                  "Ask again or narrow the request.")
    elif isinstance(event, Error):
        print(f"\nError: {event.message}")


def run(client: loop.ChatClient = ollama) -> None:
    """Interactive REPL. Entry point used by local_agent.main()."""
    messages = loop.new_messages()
    approver = TerminalApprover()

    print(f"Local Agent ready ({loop.MODEL}). Type 'exit' to quit.")
    while True:
        user_input = input("\nYou: ").strip()
        if user_input.lower() in {"exit", "quit"}:
            break
        messages.append({"role": "user", "content": user_input})
        loop.run_turn(messages, client=client, emit=render, approver=approver)


if __name__ == "__main__":
    run()
