"""Terminal frontend: the interactive REPL.

Renders core events as text and answers approval requests with y/n.
The text matches the pre-T02 output: "[Tool Call]", "[PROPOSED CONTENT]",
"[APPROVE]" and "Agent:" lines. Model text is printed as it streams, after one
"Agent: " prefix per reply.

Ctrl+C during a turn cancels the turn and returns to the "You:" prompt. Ctrl+C
at the prompt exits.
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


class Renderer:
    """Prints events. Streamed text stays on one "Agent: " line until another event arrives."""

    def __init__(self) -> None:
        self._text_open = False

    def __call__(self, event: Event) -> None:
        if isinstance(event, TextDelta):
            if not self._text_open:
                print("\nAgent: ", end="", flush=True)
                self._text_open = True
            print(event.text, end="", flush=True)
            return
        self._end_text()
        if isinstance(event, ToolCall):
            print(f"\n[Tool Call] {event.name}({event.arguments})")
        elif isinstance(event, ApprovalRequest):
            preview = event.preview
            if preview is not None and preview.kind == "text" and preview.path is not None:
                print(f"\n[PROPOSED CONTENT for '{preview.path}']\n{preview.text}\n[END PROPOSED CONTENT]")
        elif isinstance(event, TurnDone):
            if event.stop_reason == "max_turn_requests":
                print(f"\nAgent: stopped after {event.rounds} tool rounds without a final answer. "
                      "Ask again or narrow the request.")
            elif event.stop_reason == "cancelled":
                print("\n[Turn cancelled]")
        elif isinstance(event, Error):
            print(f"\nError: {event.message}")

    def _end_text(self) -> None:
        if self._text_open:
            print()
            self._text_open = False


def render(event: Event) -> None:
    """Print one event with a fresh renderer (for events that are not part of a streamed reply)."""
    Renderer()(event)


def run(client: loop.ChatClient = ollama) -> None:
    """Interactive REPL. Entry point used by local_agent.main()."""
    messages = loop.new_messages()
    approver = TerminalApprover()

    print(f"Local Agent ready ({loop.MODEL}). Type 'exit' to quit.")
    while True:
        try:
            user_input = input("\nYou: ").strip()
        except KeyboardInterrupt:
            print()
            break
        if user_input.lower() in {"exit", "quit"}:
            break
        messages.append({"role": "user", "content": user_input})
        # Ctrl+C during the turn is handled by the core: the turn ends as cancelled and the prompt returns.
        loop.run_turn(messages, client=client, emit=Renderer(), approver=approver)


if __name__ == "__main__":
    run()
