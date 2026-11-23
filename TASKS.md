# local-agent: task list

Tasks that carry out [PLAN.md](PLAN.md). They run one at a time, in order. Each task goes implementer → reviewer → (fix loop) → human approval before the next one starts.

Rules for every task:
- Keep the existing CLI working at the end of each task.
- Add or update pytest tests for new behaviour. `uv run pytest` must pass.
- Update README.md when user-visible behaviour or setup changes.
- Resolve any **verify** item the task owns and record the finding in the task's notes below.
- Stay inside the task's scope; note follow-ups instead of doing them.

Status: `todo` · `in progress` · `in review` · `done`

## M0: core refactor and hardening

### T01 Project layout and test harness: `done`
- Create the target layout from PLAN.md §1 (`core/`, `tools/`, `frontends/`, `tests/`), moving the current code without changing behaviour.
- Add pytest as a dev dependency and a fake Ollama client fixture that returns scripted responses (text and tool calls).
- First tests: the existing loop handles one tool call round, a tool error, and the `MAX_TOOL_ROUNDS` cap, all without a real model.
- GitHub Actions workflow on `windows-latest` running `uv sync` and `uv run pytest`.
- **Done when:** `uv run local-agent` behaves as before and `uv run pytest` passes locally.

### T02 Events, Approver and tool registry: `todo`
- **Verify first:** whether the Agent Client Protocol fits the event set in PLAN.md §1. Record the decision (adopt, align names with it, or ignore) in the notes.
- Event types from PLAN.md §1 and an `Approver` interface. The loop emits events and asks the approver; no `print` or `input` in core or tools.
- `@tool` decorator: name, description, JSON schema derived from type hints, risk tier (`read`/`write`/`exec`/`network`), location (`core`/`client`).
- Port `read_file`, `write_file`, `run_command` to the registry.
- CLI frontend in `frontends/cli.py` renders events and answers approvals with `y/n`.
- **Done when:** CLI behaves as today on the new interfaces; tests cover schema generation, approval denied/granted, and event order.

### T03 Streaming, cancel and error handling: `todo`
- `stream=True` with `text_delta` events.
- Cancel support; Ctrl+C mid-turn must not leave an assistant tool call without a tool result in history.
- Clear message when Ollama is unreachable; include `tool_name` in tool messages.
- **Done when:** tests cover cancel mid-tool-round and Ollama-down; CLI streams text.

### T04 Config, context window and model checks: `todo`
- **Verify first:** Ollama's default `num_ctx` for the installed version.
- Per-user TOML config via `platformdirs` (model, host, `num_ctx`, limits, workspace root); CLI flags override it.
- Set `num_ctx` explicitly; track token use from response metadata.
- Probe the model's `tools` capability at startup and warn if missing.
- Fallback parser for tool calls printed as text (the `qwen2.5-coder:7b` case).
- **Done when:** config precedence, capability warning and fallback parser are tested.

### T05 Workspace sandbox: `todo`
- Resolve all tool paths; anything outside the workspace root needs an explicit approval (and is denied in non-interactive contexts).
- Handle `..`, absolute paths, symlinks/junctions, and drive-letter differences on Windows.
- **Done when:** escape attempts are tested and blocked.

## M1: more tools

### T06 Read-tier tools: `todo`
- `read_file` with line ranges, `list_dir`, `glob`, `grep`, `git_status`, `git_diff`, `git_log`. Auto-allowed, sandboxed, output truncated.
- **Done when:** each tool has a test against a temp workspace (git tools against a temp repo).

### T07 Write/exec tools and session approvals: `todo`
- `edit_file` (exact-match replace, fails on 0 or multiple matches, approval shows a unified diff), `write_file` with diff for existing files, `git_commit`.
- `run_command` with PowerShell or cmd choice; `run_tests`.
- "Allow always for this session" option, scoped per tool and per workspace.
- **Done when:** diff shown in approval, session-allow scoping, and edit match errors are tested.

### T08 Network tool and tool profiles: `todo`
- `web_fetch` in the `network` tier, disabled unless enabled in config, always asks.
- Tool profiles `minimal`, `coding`, `all` selectable in config/CLI; short descriptions.
- **Done when:** profile selection and network-off default are tested.

### T09 MCP client: `todo`
- Use the official `mcp` Python SDK. Configure servers in the TOML config; expose their tools through the registry with a default risk tier of `exec` (always ask) unless configured otherwise.
- **Done when:** a test MCP server (stdio) is listed, called, and its result returned to the model.

### T10 Plugin loading: `todo`
- Load tools from a per-user tools directory and from Python entry points.
- **Done when:** a sample plugin is loaded in tests; adding a tool is one decorated function plus a test.

## M2: standalone Windows app

### T11 Subcommands and stdio server: `todo`
- `local-agent` (chat), `local-agent serve --stdio` (JSON-lines event protocol), `local-agent gui` (placeholder until T18); `--model`, `--cwd` flags.
- Document the protocol in `docs/protocol.md`.
- **Done when:** protocol tests drive a full turn including an approval over stdio.

### T12 Executable build: `todo`
- **Verify first:** PyInstaller (or Nuitka) supports Python 3.14; if not, lower `requires-python` for the build and record why.
- `packaging/` spec for an `--onedir` build; a script that builds it.
- **Done when:** the built `local-agent.exe` runs chat and `serve --stdio` from a folder outside the repo.

### T13 Machine-wide installer: `todo`
- **Verify first:** where Windows Ollama installs and stores models (per user or per machine).
- Inno Setup script: admin install to `C:\Program Files\local-agent\`, adds it to the machine PATH, clean uninstall removes it from PATH.
- All state (config, logs, sessions) in `%APPDATA%\local-agent` per user; nothing written to the install dir.
- README section on multi-account use and Ollama per user / `OLLAMA_MODELS` / port 11434.
- **Done when:** a second Windows account can run `local-agent` from any terminal after one install (manual check, documented steps).

### T14 Release CI: `todo`
- GitHub Actions builds the exe and installer on tag push and attaches them to a GitHub Release.
- **Done when:** workflow is valid (lint/dry-run) and documented.

## M3: GUI

### T15 Local web server: `todo`
- FastAPI + WebSocket streaming the same events as stdio.
- Bind `127.0.0.1` only, random per-launch token, `Origin` check.
- **Done when:** tests reject missing token, wrong token, and foreign `Origin`, and drive a turn over WebSocket.

### T16 Web frontend: chat: `todo`
- Standalone static app (built assets in `src/local_agent/web/`, reusable in VS Code): streaming chat, tool-call cards, approval dialog with diff view.
- **Done when:** a chat with an approved edit works in a browser against the T15 server.

### T17 Web frontend: models, settings, history: `todo`
- Model picker (from Ollama), settings editor (writes the TOML config), saved session history per user.
- **Done when:** sessions persist and reopen; settings round-trip.

### T18 Desktop window: `todo`
- `local-agent gui` opens the web UI in a `pywebview` window; included in the T12 build and T13 installer.
- **Done when:** `local-agent gui` opens a window to chat, see tool calls, and approve or deny writes.

## M4: VS Code extension

### T19 Extension scaffold and chat panel: `todo`
- **Verify first:** current VS Code Chat Participant API shape (for the later option; the first UI is our own webview).
- TypeScript extension in `extensions/vscode/`; spawns `local-agent serve --stdio`; webview panel reuses the T16 bundle.
- **Done when:** you can chat in a VS Code panel and approve a core-side write.

### T20 Client-side tools: `todo`
- Tools that run in the extension: read unsaved buffers, apply edits as a `WorkspaceEdit` with diff preview (undo works), run commands in an integrated terminal.
- **Done when:** an edit is approved as a diff and undone with Ctrl+Z.

### T21 Editor context: `todo`
- "Add selection to chat", `@file` mentions, open files and diagnostics as context.
- **Done when:** asking about the open file uses its content without the model calling `read_file`.

### T22 Extension packaging: `todo`
- `vsce package` produces a `.vsix`; uses `local-agent` from PATH, with a setting to override the path. Install docs for each Windows account.
- **Done when:** the `.vsix` installs with `code --install-extension` and works against the installed app.

## Not scheduled (optional, from PLAN.md M5)

Inline completions (FIM), code signing, auto-update.

## Notes

Findings from **verify** items and follow-ups go here, by task id.

**T01**
- Layout: `core/loop.py` (loop, `MODEL`, `MAX_TOOL_ROUNDS`), `tools/__init__.py` (`TOOLS`, `execute_tool`), `tools/builtin.py` (the three tools, `MAX_OUTPUT_CHARS`, `COMMAND_TIMEOUT_S`), `frontends/cli.py` (REPL). `agent.py` is removed; code moved without behaviour changes.
- Test seam: `run_turn(messages, client=ollama)` and `cli.run(client=ollama)`. The client only needs `chat(model=, messages=, tools=)`; the `ollama` module fits. `run_turn` now returns the final reply text, or `None` when the round cap is hit (the CLI ignores it).
- Fake client lives in `tests/fakes.py` (`FakeOllama`, `text_reply`, `tool_reply`); fixture `fake_ollama` in `tests/conftest.py`. Replies are real `ollama.ChatResponse`/`Message` objects, so subscript and attribute access both behave like the real client. `tests/` is on `pythonpath` in pyproject.toml.
- CI uses `uv sync --locked` / `uv run --locked pytest` with the setup-uv cache.
- Not yet created from the PLAN.md §1 layout: `permissions.py`, `config.py`, `frontends/server.py`, `web/`, `extensions/`, `packaging/`. They belong to later tasks.
- Follow-up (T02): `core/loop.py` and `tools/builtin.py` still use `print`/`input`; T02 replaces them with events and the `Approver`.
- Follow-up: the CI workflow has not run on GitHub yet (not pushed). Check the first run after pushing.
