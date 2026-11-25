# local-agent

A small command-line coding assistant that runs entirely on local models through [Ollama](https://ollama.com). You chat with it in the terminal; it can read files, and with your approval, write files and run shell commands.

## Features

- **Local models only**: talks to your Ollama server, so code and prompts stay on your machine.
- **Tool calling**: the model can use `read_file` (optional line range), `list_dir`, `glob`, `grep`, `git_status`, `git_diff`, `git_log`, `edit_file` (replace one exact occurrence), `write_file`, `run_command`, `run_tests` and `git_commit`. The read tools run without asking. Models that print a tool call as text instead (for example `qwen2.5-coder:7b`) are handled too.
- **Approval gates**: every write, edit, command, test run and commit asks first. `y` allows once, `n` refuses, and `a` (where offered) allows the same tool in this workspace for the rest of the session. Writes and edits show a diff, commands show the command line and its shell. `git_commit` always asks (see Safety).
- **Workspace sandbox**: file paths resolve against the workspace folder. A path outside it, including through `..`, a junction or a symlink, asks on its own, even for reads, and only `y` or `n` is accepted there. Without an answer of `y` it is refused.
- **Guard rails**: commands and tests time out after 60 s (the processes they started are killed too), tool output is truncated at 8000 characters, and each turn is capped at 10 tool rounds. All three are configurable.
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
test_command = "uv run pytest -q"
workspace = "C:\\src\\my-project"   # or a relative path, taken from the config file's folder
```

| Key | Flag | Default | Purpose |
| --- | --- | --- | --- |
| `model` | `--model` | `qwen3-vl:8b` | Ollama model name |
| `host` | `--host` (or `OLLAMA_HOST`) | `http://127.0.0.1:11434` | Ollama server URL |
| `num_ctx` | `--num-ctx` | `8192` | Context window in tokens, sent to Ollama with every request. Minimum 512. |
| `max_tool_rounds` | | `10` | Max model/tool rounds per user message |
| `max_output_chars` | | `8000` | Max characters of tool output sent to the model |
| `command_timeout_s` | | `60` | Timeout for shell commands and test runs, in seconds. On timeout the command and the processes it started are killed. |
| `test_command` | | (none) | Command that `run_tests` runs when the call gives none, run through cmd. Without it, `run_tests` uses `uv run pytest -q` when the workspace has a `pyproject.toml`, or `npm test` when it has a `package.json`. |
| `workspace` | `--cwd` | the current folder | The workspace folder. Relative file paths resolve against it, `run_command` starts in it, and paths outside it need approval (see Safety). `--cwd` also changes into it. |

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
  sandbox.py           # resolves path arguments against the workspace; inside, outside or refused
  tools/registry.py    # @tool decorator (risk, paths=), JSON schema from type hints, Tool objects, ToolContext
  tools/__init__.py    # TOOLS: the registered tools (imports the built-ins)
  tools/builtin.py     # read_file (read), write_file (write), run_command and run_tests (exec; cmd by default, or PowerShell)
  tools/edit_tools.py  # edit_file (write): exact-match replace, line endings and BOM, unified diff for previews
  tools/git_write.py   # git_commit (write): stages and commits with repository code turned off; never remembered for the session
  tools/read_tools.py  # list_dir, glob, grep, git_status, git_diff, git_log (read; searches stay inside their base folder)
  tools/process.py     # runs a child process with a time limit and kills its whole process tree on timeout
  tools/output.py      # truncate(): the shared output cap with its "[truncated N chars]" marker
  frontends/cli.py     # terminal REPL and flags: renders events, answers approvals with y, a (where offered) or n
tests/
  fakes.py             # FakeOllama: scripted replies, show() capabilities, token counts, no real model
  conftest.py          # fake_ollama fixture
  test_loop.py         # loop, tool errors, round cap, num_ctx, token usage, thinking, settings reach tools
  test_registry.py     # schema generation, risk tiers, approval requests
  test_approval.py     # write/exec granted and denied, reads not asking, event order
  test_cli.py          # y/n/a approver, rendering (diffs, commands), token line, REPL, Ctrl+C, retry after a failed turn
  test_streaming.py    # streamed deltas, tool calls across chunks, cancel and history invariant, Ollama errors
  test_config.py       # precedence (flags > OLLAMA_HOST > file > defaults), invalid values, config path, CLI flags
  test_model.py        # startup capability probe (tools missing or present), Ollama-down and model-missing at startup
  test_toolcalls.py    # fallback parser: valid forms, rejected forms, approvals through the loop
  test_tool_context.py # per-turn limits passed to tools, the context argument kept out of the schema
  test_read_tools.py   # each read tool against temp folders and a temp git repo; auto-allowed; outside paths asked
  test_sandbox.py      # path resolution (..., prefix tricks, drives, UNC, ADS, 8.3, junctions) and outside-workspace approvals
  test_edit_tools.py   # edit_file match rules (0, several, overlapping, empty), CRLF/LF, BOM, write_file diff previews
  test_exec_tools.py   # run_command shell choice (cmd default), run_tests resolution, timeouts that kill the process tree
  test_git_write.py    # git_commit: what is committed, refusals before the question, no repo hooks or filters, no signing
  test_session_approvals.py # "allow for this session": per tool and workspace, never outside paths, .git writes or git_commit
.github/workflows/ci.yml  # pytest on windows-latest
```

## Architecture

The core (`core/`, `tools/`, `permissions.py`, `config.py`) never prints or reads input. It reports through events and asks an `Approver`. The CLI is one frontend: `render(event)` prints events, and `TerminalApprover` asks `[APPROVE] ...? [y]es / [a]lways for this session / [n]o`, or `(y/n)` when the request does not offer `a`. A GUI or editor extension would supply its own sink and approver.

Settings are passed in explicitly: `run_turn(messages, client, settings=...)`. The loop sends `options={"num_ctx": ...}` with each chat call and gives tools a `ToolContext` (the command timeout and output limit) when they declare a `context: ToolContext` parameter. Tools never read a global setting.

Model text is sent as `text_delta` events while it streams, and reasoning as `thinking_delta`. For each tool call the events are `tool_call`, then for write/exec/network tools `approval_request`, then `tool_result`. An `approval_request` lists its answers in `options` (`allow_once`, `allow_always`, `reject_once`). `allow_always` appears only for a call that may be allowed for the session, and an answer of it counts as `allow_once` otherwise. A command's `preview` names its shell. A turn ends with `turn_done`, whose `stop_reason` is `end_turn`, `max_turn_requests` or `cancelled`, and whose `usage` gives the token counts Ollama reported. An Ollama failure ends the turn with an `error` event instead. If no approver is passed, risky tools are refused.

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
- `paths=("path",)`: names the parameters that are file paths (each must be a `str`, or `str | None` when optional). The core resolves them against the workspace before the tool runs, asks separately for a path outside it, and passes the tool the resolved absolute path. Do not check paths inside the tool.
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

The agent works in one folder, the **workspace**: the folder you start it from, or the one `--cwd` / `workspace` names. What it can do there:

- **Read tools** (`read_file`, `list_dir`, `glob`, `grep`, `git_status`, `git_diff`, `git_log`) run without a question, but their paths are sandboxed too. `glob` and `grep` do not return or read anything reached through a junction or symlink that leads out of the folder they start from, and skip `.git`. `grep` also skips binary files. `git_*` run `git` in the workspace with a 30 s timeout. They never run commands from the repo's own config (fsmonitor, textconv, hooks are turned off). They do not look above the workspace, so a workspace that is a subfolder of a bigger repo says it is not a repository. Outside a repository they say so.
- **File tools** (`read_file`, `write_file`, `edit_file`) resolve every path against the workspace, not the process folder. A path inside the workspace runs without an extra question. A path outside it (`..\other`, an absolute path, a junction or symlink that leads out, a short 8.3 name of such a folder, a UNC path, another drive) gets its own prompt: `[OUTSIDE WORKSPACE] ... Allow read_file to access 'C:\...', which is outside the workspace ...`. Answer `y` only if you expect it. A write outside the workspace asks twice, once for the location and once for the write. With no answer of `y` (or no terminal to ask at) the tool does not run, and the model is told why.
- **Refused without asking**: names that Windows would read as something else. These are alternate data streams (`file.txt:stream`), names ending in a dot or space (Windows drops them, so `jn.` can open the junction `jn`), reserved device names (`NUL`, `CON`, `COM1`, `LPT1`, with or without an extension), the device namespace (`\\.\...`), and empty paths or paths with a NUL character. The model is told the path was refused.
- **`edit_file`** replaces one exact occurrence of `old_text` in an existing UTF-8 text file. Zero matches, several matches (overlapping ones included) and an empty `old_text` are errors, and nothing is written. The preview is a unified diff. CRLF files are matched with LF and written back with CRLF. A leading BOM stays in front of the file's first line, so `old_text` does not need to include it.
- **`run_command` is not sandboxed by path.** It starts in the workspace (`cwd`), but a shell command can name any file on the machine, and Windows applies its own rules to it. Every command asks first, so read each one before answering `y`. `shell` chooses `cmd` (the default) or `powershell` (`pwsh` if installed, else Windows PowerShell). `run_tests` runs the test command through cmd: the `command` argument if given, else `test_command` from the config, else `uv run pytest -q` (with a `pyproject.toml`) or `npm test` (with a `package.json`).
- **`git_commit`** commits in the workspace's repository. With `paths` it stages exactly those files and commits only them; without, it commits what is already staged. It runs no repository code: hooks are off, fsmonitor is off, and the commit is not signed (`--no-verify`, `commit.gpgsign=false`), so a pre-commit hook or a gpg program in the repo does not run. With `paths`, a path that uses a repo-defined filter (for example Git LFS after `git lfs install --local`) is refused before the question: stage it yourself with `git add` and call `git_commit` without paths, which commits what is staged with the filter applied. The preview lists what will be committed. If the commit fails after `git add`, the paths stay staged and the error says so.
- **Session answers (`a`).** Answering `a` to a tool's own question allows that tool in that workspace folder until the REPL exits; a new run starts with none. It is kept per tool and per workspace, so the same tool in another folder asks again. It is never offered or kept for:
  - a path outside the workspace (those always ask, and only `y` or `n` is accepted),
  - a write or edit whose path has a `.git` folder in it (hooks and config, in any case), which asks every time,
  - `git_commit`, which asks every time.
- **Not detected**: a hard link inside the workspace that points at a file outside it looks like an inside path. A file swapped for a link after the check is a race that is not closed.
Windows path cases are covered by `tests/test_sandbox.py`: case and drive-letter case, `C:foo`, `\foo`, UNC, `\\?\` and `\\?\UNC\`, 8.3 short names, junctions (created with `mklink /J` in the test; skipped with a reason if that is not allowed), prefix tricks such as `work-evil` next to `work`, and symlinks (skipped here: Windows refused to create one without Developer Mode or admin).

You still need to read each prompt. The sandbox limits where file tools go; it does not judge what a command or a file's content will do.
