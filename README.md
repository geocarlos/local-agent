# local-agent

A small command-line coding assistant that runs entirely on local models through [Ollama](https://ollama.com). You chat with it in the terminal; it can read files, and with your approval, write files and run shell commands.

## Features

- **Local models only**: talks to your Ollama server, so code and prompts stay on your machine.
- **Tool calling**: the model can use `read_file`, `write_file`, and `run_command`. Models that print a tool call as text instead (for example `qwen2.5-coder:7b`) are handled too.
- **Approval gates**: every write and command asks `y/n` first. Writes show a preview of the content.
- **Guard rails**: shell commands time out after 60 s, tool output is truncated at 8000 characters, and each turn is capped at 10 tool rounds. All three are configurable.
- **Context window set explicitly**: every request sends `num_ctx` (default 8192), and each turn shows how many tokens the prompt used. A warning appears when the prompt nearly fills the window.
- **Startup check**: the model is checked once at startup. A warning shows if it does not report tool support.
- **Tool errors go back to the model**, so it can recover instead of crashing the session.
- **Streaming**: model text appears as it is generated. A `[thinking...]` line shows while a thinking model reasons.
- **Ctrl+C cancels the current turn** and returns to the `You:` prompt. Ctrl+C at the prompt exits. A tool call that had not run is reported as cancelled to the model, and a half-written reply is dropped. A message that got no reply is removed from the history, so sending it again does not repeat it.
- **Clear errors when Ollama is down or the model is missing**: the message names the host and suggests `ollama serve` or `ollama pull <model>`. The REPL keeps running.

## Requirements

- Python 3.14 (see `.python-version`)
- [uv](https://docs.astral.sh/uv/)
- Ollama, running locally (default `http://127.0.0.1:11434`)
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

Flags override the config file and the environment for one run:

```sh
uv run local-agent --model qwen3-vl:8b --num-ctx 8192
uv run local-agent --cwd C:\src\my-project --config C:\tmp\alt.toml
```

If Ollama is not running, you get `Error: Cannot reach Ollama at http://127.0.0.1:11434. Start it with ollama serve ...`. Start the server and send the request again. If the model is not installed, the error suggests `ollama pull qwen3-vl:8b`.

## Configuration

Settings come from four places, highest priority first:

1. **Command-line flags** (`--model`, `--host`, `--num-ctx`, `--cwd`, `--config`).
2. **`OLLAMA_HOST`** environment variable. It sets the host only, and may omit `http://` as the Ollama CLI allows.
3. **The config file** (TOML), described below.
4. **Built-in defaults**.

**Config file location.** The file is `config.toml` in the per-user application config folder. On Windows that is `%APPDATA%\local-agent\config.toml`, which for this account is `C:\Users\<you>\AppData\Roaming\local-agent\config.toml`. The folder is not created for you; create the file yourself. A missing file means defaults. `--config PATH` reads a different file, and that file must exist.

Example:

```toml
# %APPDATA%\local-agent\config.toml
model = "qwen3-vl:8b"
host = "http://127.0.0.1:11434"
num_ctx = 8192
max_tool_rounds = 10
max_output_chars = 8000
command_timeout_s = 60
workspace = "C:\\src\\my-project"   # or a relative path, taken from the config file's folder
```

| Key | Flag | Default | Purpose |
| --- | --- | --- | --- |
| `model` | `--model` | `qwen3-vl:8b` | Ollama model name |
| `host` | `--host` (or `OLLAMA_HOST`) | `http://127.0.0.1:11434` | Ollama server URL |
| `num_ctx` | `--num-ctx` | `8192` | Context window in tokens, sent to Ollama with every request. Minimum 512. |
| `max_tool_rounds` | | `10` | Max model/tool rounds per user message |
| `max_output_chars` | | `8000` | Max characters of tool output sent to the model |
| `command_timeout_s` | | `60` | Timeout for shell commands, in seconds |
| `workspace` | `--cwd` | the current folder | The folder the agent starts in: `--cwd` changes into it, and tools run there. Stored only for now; the sandbox that confines tools to it comes later. |

An unknown key, a wrong type or an out-of-range value stops the program at startup with a message that names the file or flag, and exit code 2. Example: `Error: C:\...\config.toml: num_ctx must be at least 512, got 100`.

**Why `num_ctx` is set.** Ollama's default context window is chosen from the GPU memory it finds. On the 8 GiB GPU this was tested on, the server log reports `default_num_ctx=4096`. A long chat would then be cut silently, so the agent always sends an explicit value. The 8192 default fits a file read and a few turns; raise it if your GPU has room (see the context warning in the output).

Model notes: `qwen3-vl:8b` makes structured tool calls. `qwen2.5-coder:7b` prints its calls as JSON text; the agent turns those into real tool calls when the reply is only the call (see `core/toolcalls.py`). Replies that mix prose and a call are not converted.

## Project layout

```
src/local_agent/
  __init__.py          # main() entry point: parses flags, loads settings, changes into the workspace
  config.py            # Settings, defaults, the TOML file, OLLAMA_HOST, validation and precedence
  core/loop.py         # agent loop: streams one user turn of model/tool rounds; emits events, asks the approver, honours cancel
  core/events.py       # event types (text_delta, thinking_delta, tool_call, tool_result, approval_request, turn_done, error), Usage
  core/model.py        # Ollama error messages, startup probe of the model's capabilities
  core/toolcalls.py    # fallback parser: tool calls printed as JSON text become real calls
  permissions.py       # risk tiers, approval policy (read auto-allowed), Approver protocol
  tools/registry.py    # @tool decorator, JSON schema from type hints, Tool objects, ToolContext
  tools/__init__.py    # TOOLS: the registered tools (imports the built-ins)
  tools/builtin.py     # read_file (read), write_file (write), run_command (exec)
  frontends/cli.py     # terminal REPL and flags: renders events, answers approvals with y/n
tests/
  fakes.py             # FakeOllama: scripted replies, show() capabilities, token counts, no real model
  conftest.py          # fake_ollama fixture
  test_loop.py         # loop, tool errors, round cap, num_ctx, token usage, thinking, settings reach tools
  test_registry.py     # schema generation, risk tiers, approval requests
  test_approval.py     # write/exec granted and denied, reads not asking, event order
  test_cli.py          # y/n approver, rendering, token line, REPL, Ctrl+C, retry after a failed turn
  test_streaming.py    # streamed deltas, tool calls across chunks, cancel and history invariant, Ollama errors
  test_config.py       # precedence (flags > OLLAMA_HOST > file > defaults), invalid values, config path, CLI flags
  test_model.py        # startup capability probe (tools missing or present), Ollama-down and model-missing at startup
  test_toolcalls.py    # fallback parser: valid forms, rejected forms, approvals through the loop
  test_tool_context.py # per-turn limits passed to tools, the context argument kept out of the schema
.github/workflows/ci.yml  # pytest on windows-latest
```

## Architecture

The core (`core/`, `tools/`, `permissions.py`, `config.py`) never prints or reads input. It reports through events and asks an `Approver`. The CLI is one frontend: `render(event)` prints events, and `TerminalApprover` asks `[APPROVE] ...? (y/n)`. A GUI or editor extension would supply its own sink and approver.

Settings are passed in explicitly: `run_turn(messages, client, settings=...)`. The loop sends `options={"num_ctx": ...}` with each chat call and gives tools a `ToolContext` (the command timeout and output limit) when they declare a `context: ToolContext` parameter. Tools never read a global setting.

Model text is sent as `text_delta` events while it streams, and reasoning as `thinking_delta`. For each tool call the events are `tool_call`, then for write/exec/network tools `approval_request`, then `tool_result`. A turn ends with `turn_done`, whose `stop_reason` is `end_turn`, `max_turn_requests` or `cancelled`, and whose `usage` gives the token counts Ollama reported. An Ollama failure ends the turn with an `error` event instead. If no approver is passed, risky tools are refused.

Cancelling: pass a `threading.Event` as `cancel=` to `run_turn`, or press Ctrl+C (the core treats `KeyboardInterrupt` as a cancel). The turn stops between stream chunks, before each round and before each tool call. Every tool call in history gets a tool message: calls that did not run get `"Cancelled by user."` as a failed result, and a reply cut off mid-stream is not added to history.

The fallback parser runs only when a reply has no structured tool calls. It accepts a reply that is nothing but JSON calls (bare, in a `json` fence, or in `<tool_call>` tags). The call name must be a registered tool, the arguments must bind to its signature, and any failure makes the whole reply plain text. Converted calls go through the same approval rules as structured ones. Limitation: the text is streamed to the screen before the end of the reply is known, so the raw JSON is visible above the `[Tool Call]` line.

Thinking text is kept in the assistant message in history, and it counts toward `num_ctx`.

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
- A tool that needs a limit from the settings declares `context: ToolContext` as a parameter (from `local_agent.tools.registry`). The core fills it in; the model never sees it. `ToolContext.command_timeout_s` and `ToolContext.max_output_chars` are the current values.
- Supported parameter types: `str`, `int`, `float`, `bool`, `list[T]`, `dict`, and `X | None` for optional parameters. Parameters without a default are required. Use `Annotated[str, "description"]` to describe a parameter.
- Tools must not `print` or call `input`. Return a string; raised exceptions go back to the model as an error message.
- Add a test next to `tests/test_registry.py` or `tests/test_approval.py` that checks the schema and, for risky tools, the approval flow.

## Development

```sh
uv sync          # installs runtime and dev dependencies (pytest)
uv run pytest    # runs the tests; no Ollama needed
```

The loop takes the Ollama client as a parameter (`run_turn(messages, client=...)`), so tests pass a `FakeOllama` that returns scripted text replies and tool calls, streamed as chunks (`chunk_chars=` splits the text; `Raise` and `Run` steps script errors and cancel points). `FakeOllama(capabilities=..., show_error=...)` scripts the startup check. Tests never read your config file: they pass a temp path (`load_settings(config_file=..., env={})`). CI runs the same tests on `windows-latest` for every push to `main` and every pull request.

## Safety

The agent can modify files and run arbitrary shell commands in the directory you start it from (or the one `--cwd` / `workspace` names). Read each approval prompt before answering `y`. Sandboxing that confines tools to the workspace is not in place yet.
