"""Terminal frontend: the interactive REPL."""

import ollama

from local_agent.core import loop


def run(client: loop.ChatClient = ollama) -> None:
    """Interactive REPL. Entry point used by local_agent.main()."""
    messages = loop.new_messages()

    print(f"Local Agent ready ({loop.MODEL}). Type 'exit' to quit.")
    while True:
        user_input = input("\nYou: ").strip()
        if user_input.lower() in {"exit", "quit"}:
            break
        messages.append({"role": "user", "content": user_input})
        loop.run_turn(messages, client=client)


if __name__ == "__main__":
    run()
