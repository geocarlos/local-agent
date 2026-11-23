# local-agent

A small command-line coding assistant that runs entirely on local models through [Ollama](https://ollama.com). You chat with it in the terminal; it can read files, and with your approval, write files and run shell commands.

## Features

- **Local models only**: talks to your Ollama server, so code and prompts stay on your machine.
- **Tool calling**: the model can use `read_file`, `write_file`, and `run_command`.
- **Approval gates**: every write and command asks `y/n` first. Writes show a preview of the content.
- **Guard rails**: shell commands time out after 60 s, tool output is truncated, and each turn is capped at 10 tool rounds.
- **Tool errors go back to the model**, so it can recover instead of crashing the session.

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

Type a request at the `You:` prompt. Type `exit` or `quit` to leave.

## Configuration

Settings are constants at the top of `src/local_agent/agent.py`:

| Constant | Default | Purpose |
| --- | --- | --- |
| `MODEL` | `qwen3-vl:8b` | Ollama model name |
| `MAX_OUTPUT_CHARS` | `8000` | Max characters of tool output sent to the model |
| `MAX_TOOL_ROUNDS` | `10` | Max model/tool rounds per user message |
| `COMMAND_TIMEOUT_S` | `60` | Timeout for shell commands, in seconds |

Model notes: `qwen2.5-coder:7b` did not emit structured tool calls in testing. `qwen3-vl:8b` did.

## Project layout

```
src/local_agent/
  __init__.py   # main() entry point
  agent.py      # tools, tool dispatch, and the chat loop
```

## Safety

The agent can modify files and run arbitrary shell commands in the directory you start it from. Read each approval prompt before answering `y`.
