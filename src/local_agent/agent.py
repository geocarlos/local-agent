import subprocess
from pathlib import Path
import ollama

MODEL = "qwen3-vl:8b"  # Swap for gemma4:12b, qwen2.5-coder:7b, etc.
MAX_OUTPUT_CHARS = 8000      # Cap tool output so it can't overflow the model's context
MAX_TOOL_ROUNDS = 10         # Max model<->tool rounds per user turn
COMMAND_TIMEOUT_S = 60       # Kill shell commands that hang


def _truncate(text: str) -> str:
    """Trim long tool output and say so, so the model knows it was cut."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n...[truncated {len(text) - MAX_OUTPUT_CHARS} chars]"


# 1. Define simple, safe tools
def read_file(path: str) -> str:
    """Read the contents of a file."""
    try:
        return _truncate(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        return f"Error: {e}"


def write_file(path: str, content: str) -> str:
    """Write content to a file (asks for approval first)."""
    preview = content if len(content) <= 1500 else content[:1500] + f"\n...[{len(content) - 1500} more chars]"
    print(f"\n[PROPOSED CONTENT for '{path}']\n{preview}\n[END PROPOSED CONTENT]")
    confirm = input(f"\n[APPROVE] Write {len(content)} chars to '{path}'? (y/n): ").strip().lower()
    if confirm != "y":
        return "User denied file write."
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
    except Exception as e:
        return f"Error writing {path}: {e}"
    return f"Successfully wrote to {path}."


def run_command(command: str) -> str:
    """Run a shell command (asks for approval first)."""
    confirm = input(f"\n[APPROVE] Run command `{command}`? (y/n): ").strip().lower()
    if confirm != "y":
        return "User denied command execution."
    try:
        res = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {COMMAND_TIMEOUT_S}s and was killed."
    except Exception as e:
        return f"Error running command: {e}"
    return _truncate(f"EXIT CODE: {res.returncode}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")


TOOLS = {
    "read_file": read_file,
    "write_file": write_file,
    "run_command": run_command,
}


def execute_tool(fn_name: str, fn_args: dict) -> str:
    """Run one tool call; any failure comes back as text so the model can recover."""
    if fn_name not in TOOLS:
        return f"Unknown tool: {fn_name}"
    try:
        return str(TOOLS[fn_name](**fn_args))
    except Exception as e:
        return f"Error calling {fn_name} with arguments {fn_args}: {type(e).__name__}: {e}"


# 2. Main Agent Loop
def run() -> None:
    """Interactive REPL. Entry point used by local_agent.main()."""
    messages = [
        {"role": "system", "content": "You are a concise local coding assistant. Use tools to inspect and edit files."}
    ]

    print(f"Local Agent ready ({MODEL}). Type 'exit' to quit.")
    while True:
        user_input = input("\nYou: ").strip()
        if user_input.lower() in {"exit", "quit"}:
            break
        messages.append({"role": "user", "content": user_input})

        for _ in range(MAX_TOOL_ROUNDS):
            response = ollama.chat(
                model=MODEL,
                messages=messages,
                tools=list(TOOLS.values()),
            )
            msg = response["message"]
            messages.append(msg)

            # If the model didn't call a tool, print its reply and wait for user
            if not msg.get("tool_calls"):
                print(f"\nAgent: {msg['content']}")
                break

            # Execute each requested tool and feed results back
            for call in msg["tool_calls"]:
                fn_name = call["function"]["name"]
                fn_args = call["function"]["arguments"]
                print(f"\n[Tool Call] {fn_name}({fn_args})")
                result = execute_tool(fn_name, fn_args)
                messages.append({"role": "tool", "content": result})
        else:
            print(f"\nAgent: stopped after {MAX_TOOL_ROUNDS} tool rounds without a final answer. Ask again or narrow the request.")


if __name__ == "__main__":
    run()
