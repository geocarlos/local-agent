# local-agent

A small command-line coding assistant that runs entirely on local models through [Ollama](https://ollama.com). You chat with it in the terminal; it can read files, and with your approval, write files and run shell commands.

## Features

- **Local models only**: talks to your Ollama server, so code and prompts stay on your machine.
- **Tool calling**: the model can use `read_file`, `write_file`, and `run_command`.
- **Approval gates**: every write and command asks `y/n` first. Writes show a preview of the content.
- **Guard rails**: shell commands time out after 60 s, tool output is truncated, and each turn is capped at 10 tool rounds.
- **Tool errors go back to the model**, so it can recover instead of crashing the session.
- **Streaming**: model text appears as it is generated.
- **Ctrl+C cancels the current turn** and returns to the `You:` prompt. Ctrl+C at the prompt exits. A tool call that had not run is reported as cancelled to the model, and a half-written reply is dropped.
- **Clear errors when Ollama is down or the model is missing**: the message names the host and suggests `ollama serve` or `ollama pull <model>`. The REPL keeps running.

## Requirements

- Python 3.14 (see `.python-version`)
- [uv](https://docs.astral.sh/uv/)
- Ollama, running locally (default `http://localhost:11434`)
- A model with tool support, pulled into Ollama, for example:

  ```sh
  ollama pull qwen3-vl:8b
  ```

## Setup

```sh
uv sync
```

## Usage

```sh
uv run local-agent
```

Type a request at the `You:` prompt. Type `exit` or `quit` to leave. Press Ctrl+C during a reply to cancel it.

If Ollama is not running, you get `Error: Cannot reach Ollama at http://127.0.0.1:11434. Start it with ollama serve ...`. Start the server and send the request again. If the model is not installed, the error suggests `ollama pull qwen3-vl:8b`.

## Configuration

Settings are module constants for now (a config file comes later):

| Constant | Default | Where | Purpose |
| --- | --- | --- | --- |
| `MODEL` | `qwen3-vl:8b` | `core/loop.py` | Ollama model name |
| `MAX_TOOL_ROUNDS` | `10` | `core/loop.py` | Max model/tool rounds per user message |
| `MAX_OUTPUT_CHARS` | `8000` | `tools/builtin.py` | Max characters of tool output sent to the model |
| `COMMAND_TIMEOUT_S` | `60` | `tools/builtin.py` | Timeout for shell commands, in seconds |

Model notes: `qwen2.5-coder:7b` did not emit structured tool calls in testing. `qwen3-vl:8b` did.

## Project layout

```
src/local_agent/
  __init__.py          # main() entry point
  core/loop.py         # agent loop: streams one user turn of model/tool rounds; emits events, asks the approver, honours cancel
  core/events.py       # event types (text_delta, tool_call, tool_result, approval_request, turn_done, error)
  permissions.py       # risk tiers, approval policy (read auto-allowed), Approver protocol
  tools/registry.py    # @tool decorator, JSON schema from type hints, Tool objects
  tools/__init__.py    # TOOLS: the registered tools (imports the built-ins)
  tools/builtin.py     # read_file (read), write_file (write), run_command (exec)
  frontends/cli.py     # terminal REPL: renders events, answers approvals with y/n
tests/
  fakes.py             # FakeOllama: scripted model replies, no real model
  conftest.py          # fake_ollama fixture
  test_loop.py         # loop, tool errors, round cap, event payloads
  test_registry.py     # schema generation, risk tiers, approval requests
  test_approval.py     # write/exec granted and denied, reads not asking, event order
  test_cli.py          # y/n approver, rendering, REPL, Ctrl+C at the prompt and during a turn
  test_streaming.py    # streamed deltas, tool calls across chunks, cancel and history invariant, Ollama errors
.github/workflows/ci.yml  # pytest on windows-latest
```

## Architecture

The core (`core/`, `tools/`, `permissions.py`) never prints or reads input. It reports through events and asks an `Approver`. The CLI is one frontend: `render(event)` prints events, and `TerminalApprover` asks `[APPROVE] ...? (y/n)`. A GUI or editor extension would supply its own sink and approver.

Model text is sent as `text_delta` events while it streams. For each tool call the events are `tool_call`, then for write/exec/network tools `approval_request`, then `tool_result`. A turn ends with `turn_done`, whose `stop_reason` is `end_turn`, `max_turn_requests` or `cancelled`. An Ollama failure ends the turn with an `error` event instead. If no approver is passed, risky tools are refused.

Cancelling: pass a `threading.Event` as `cancel=` to `run_turn`, or press Ctrl+C (the core treats `KeyboardInterrupt` as a cancel). The turn stops between stream chunks, before each round and before each tool call. Every tool call in history gets a tool message: calls that did not run get `"Cancelled by user."` as a failed result, and a reply cut off mid-stream is not added to history.

## Adding a tool

A tool is one decorated function. Its docstring becomes the description the model sees, and type hints become the JSON schema:

```python
# src/local_agent/tools/builtin.py (or any module imported by tools/__init__.py)
from typing import Annotated

from local_agent.permissions import Risk
from local_agent.tools.registry import tool


@tool(risk=Risk.READ)
def count_lines(path: Annotated[str, "File to count"]) -> str:
    """Count the lines in a file."""
    with open(path, encoding="utf-8") as f:
        return str(sum(1 for _ in f))
```

- `risk`: `Risk.READ` runs without asking. `WRITE`, `EXEC` and `NETWORK` ask the approver first.
- For risky tools, add `approval_title=` (a function returning the question, for example `"Write 12 chars to 'a.txt'"`) and `preview=` (a function returning a `Preview` shown before approval). Both receive the tool's arguments by name. `denied_message=` is what the model receives when the user says no.
- Supported parameter types: `str`, `int`, `float`, `bool`, `list[T]`, `dict`, and `X | None` for optional parameters. Parameters without a default are required. Use `Annotated[str, "description"]` to describe a parameter.
- Tools must not `print` or call `input`. Return a string; raised exceptions go back to the model as an error message.
- Add a test next to `tests/test_registry.py` or `tests/test_approval.py` that checks the schema and, for risky tools, the approval flow.

## Development

```sh
uv sync          # installs runtime and dev dependencies (pytest)
uv run pytest    # runs the tests; no Ollama needed
```

The loop takes the Ollama client as a parameter (`run_turn(messages, client=...)`), so tests pass a `FakeOllama` that returns scripted text replies and tool calls, streamed as chunks (`chunk_chars=` splits the text; `Raise` and `Run` steps script errors and cancel points). CI runs the same tests on `windows-latest` for every push to `main` and every pull request.

## Safety

The agent can modify files and run arbitrary shell commands in the directory you start it from. Read each approval prompt before answering `y`.
