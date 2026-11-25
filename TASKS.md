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

### T02 Events, Approver and tool registry: `done`
- **Verify first:** whether the Agent Client Protocol fits the event set in PLAN.md §1. Record the decision (adopt, align names with it, or ignore) in the notes.
- Event types from PLAN.md §1 and an `Approver` interface. The loop emits events and asks the approver; no `print` or `input` in core or tools.
- `@tool` decorator: name, description, JSON schema derived from type hints, risk tier (`read`/`write`/`exec`/`network`), location (`core`/`client`).
- Port `read_file`, `write_file`, `run_command` to the registry.
- CLI frontend in `frontends/cli.py` renders events and answers approvals with `y/n`.
- **Done when:** CLI behaves as today on the new interfaces; tests cover schema generation, approval denied/granted, and event order.

### T03 Streaming, cancel and error handling: `done`
- `stream=True` with `text_delta` events.
- Cancel support; Ctrl+C mid-turn must not leave an assistant tool call without a tool result in history.
- Clear message when Ollama is unreachable; include `tool_name` in tool messages.
- **Done when:** tests cover cancel mid-tool-round and Ollama-down; CLI streams text.

### T04 Config, context window and model checks: `done`
- **Verify first:** Ollama's default `num_ctx` for the installed version.
- Per-user TOML config via `platformdirs` (model, host, `num_ctx`, limits, workspace root); CLI flags override it.
- Set `num_ctx` explicitly; track token use from response metadata.
- Probe the model's `tools` capability at startup and warn if missing.
- Fallback parser for tool calls printed as text (the `qwen2.5-coder:7b` case).
- **Done when:** config precedence, capability warning and fallback parser are tested.

### T05 Workspace sandbox: `done`
- Resolve all tool paths; anything outside the workspace root needs an explicit approval (and is denied in non-interactive contexts).
- Handle `..`, absolute paths, symlinks/junctions, and drive-letter differences on Windows.
- **Done when:** escape attempts are tested and blocked.

## M1: more tools

### T06 Read-tier tools: `done`
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

**T02**
- **ACP decision: align, do not adopt yet.** The Agent Client Protocol (agentclientprotocol.com) is built for editor-to-agent frontends, which is the T11/T19 case, so its shapes are a good target. We keep our own internal events (names from PLAN.md §1) and use ACP names and values where they exist: tool kinds (`read`, `edit`, `execute`, `fetch`, ...), tool call statuses, permission option kinds (`allow_once`, `reject_once`), stop reasons (`end_turn`, `max_turn_requests`, `cancelled`) and `toolCallId`. T11 maps our events 1:1 to `session/update` (`text_delta` to `agent_message_chunk`, `tool_call`/`tool_result` to `tool_call`/`tool_call_update`, `approval_request` to `session/request_permission`, `turn_done` to the `session/prompt` response). We do not build a stdio server here (T11).
- ACP gaps to handle at T11, not now: `tool_call` requires a `title` (our `ToolCall` has none yet); ACP's `tool_call` status values `pending` and `in_progress` are not emitted (we send only final results); `text_delta` needs a `messageId` for chunks. The ACP overview page does not name the discriminator values, so T11 must check the schema reference, not only the protocol pages.
- Verified against the spec (fetched 2026-11-23): `sessionUpdate` values `tool_call`/`tool_call_update`/`agent_message_chunk`; ToolCallStatus `pending`/`in_progress`/`completed`/`failed`; ToolKind includes `switch_mode`; PermissionOptionKind `allow_once`/`allow_always`/`reject_once`/`reject_always`; StopReason `end_turn`/`max_tokens`/`max_turn_requests`/`refusal`/`cancelled`.
- Design: `core/events.py` holds the event dataclasses (each with a wire `type` tag, so T11 can serialise them). `permissions.py` holds `Risk`, `needs_approval` (only `read` is auto-allowed), the `Approver` protocol and `DenyAll` (the default when no approver is passed, so risky tools are refused). `tools/registry.py` holds `@tool(risk=, location=, denied_message=, approval_title=, preview=, registry=)`, the `Tool` object (`schema()`, `call()`, `approval_request()`) and the JSON schema builder. `core/loop.py` `run_turn(messages, client, *, emit, approver)` emits events and never prints.
- The event order for a turn with a write is `tool_call`, `approval_request`, `tool_result`, then `text_delta` and `turn_done`. The core emits `approval_request` to the sink (so the frontend can show the preview) and then calls `approver.decide()`. The CLI renders the preview from the sink and asks y/n from the approver.
- Schemas: the three builtins produce the same `tools=` entries as ollama's own function conversion before T02 (checked by script and pinned in `tests/test_registry.py`), including `"description": ""` on parameters without a description.
- `core/__init__.py` no longer re-exports `loop`, to avoid an import cycle (`core` -> `loop` -> `tools` -> `core.events`). Import `local_agent.core.loop` directly.
- Terminal output is unchanged except that tool results are not printed (as before) and the max-rounds message is now rendered by the CLI from `turn_done`. Verified by a piped smoke test against `qwen3-vl:8b`: `read .python-version` returned `3.14`, and a write asked for approval, showed the preview and wrote the file after `y`.
- Behaviour kept on purpose: `text_delta` is emitted only for the final reply (intermediate text next to tool calls is still not shown, as before). Denied tools return the old messages ("User denied file write." and "User denied command execution.") to the model.
- Follow-ups: T03 streams `text_delta` and adds `cancelled`. T07 adds `ALLOW_ALWAYS` and diff previews. T11 adds `title` and `messageId` as above. Client-located tools (`Location.CLIENT`) are registered but the core refuses to run them with a clear error until T20 adds a client runner. `tool_name` in tool messages is still missing (T03). The approval `risk` field is a string on the event (`"write"`), so events stay free of imports from tools.
- Review fixes: arguments are bound against the tool's signature before the approval request is built, so bad arguments to a risky tool return the usual "Error calling <tool> ..." text (naming the real tool) and the user is not asked. An approver that raises counts as a refusal, and the tool does not run. `Decision.allowed` is the one way the loop checks a decision.
- Follow-up (before T06): Google-style `Args:` docstring blocks are not parsed into per-parameter descriptions. Only `Annotated[..., "desc"]` sets them. Revisit when the read tools with many parameters are added.
- Follow-up (T20): `Tool.fn` is mandatory, so client-located tools need a stub body. Consider making `fn` optional for `location=CLIENT`.
- Follow-up (hardening): argument types are not validated against the schema (only the signature is bound). A model that sends `"5"` for an `int` reaches the tool unchanged. Add type coercion or validation.

**T03**
- **Streaming:** `run_turn` calls the client with `stream=True` and emits `text_delta` per chunk. Content and `tool_calls` are collected across all chunks (`core/loop.py` `_stream_reply`) and appended to history as an `ollama.Message` only when the stream has finished. The CLI prints one `Agent: ` line and appends the deltas to it (`Renderer` in `frontends/cli.py`).
- **Behaviour change:** text that comes before tool calls in a round is now streamed and shown (it used to be dropped). The core cannot know a reply has tool calls until the stream ends, so it streams everything. The T02 note about final-reply-only text no longer holds.
- **Cancel:** `run_turn(..., cancel=threading.Event)` is checked between chunks, before each round and before each tool call. Ctrl+C arrives as `KeyboardInterrupt`; the core catches it at the streaming, tool and approval steps (and once more around the whole turn) and treats it as a cancel. The turn ends with `turn_done` (`stop_reason="cancelled"`). The CLI shows `[Turn cancelled]` and returns to `You:`. Ctrl+C at the prompt exits.
- **History invariant (chosen: synthetic results + drop partial reply):** every tool call in an assistant message gets a tool message. Calls that did not run get `"Cancelled by user."` as a failed `tool_result`, and the event stream shows a `tool_call` and a `tool_result` for each. A reply cut off mid-stream is not appended, so its partial tool calls never reach history. `tests/test_streaming.py` asserts the invariant with `assert_tool_calls_answered` after every cancel and error case.
- **Errors:** an `error` event is emitted and the turn ends with no `turn_done` (matches ACP, where a failed prompt is a JSON-RPC error). Mapping: `ConnectionError` and `httpx.ConnectError` give "Cannot reach Ollama at <host>. Start it with `ollama serve` ..."; `ResponseError` 404, or "not found" in its text, gives "Model '<model>' is not installed ... `ollama pull <model>`"; other `ResponseError`s and `httpx.TransportError`s are shown with their text. Other exceptions still propagate (they are bugs).
- **Verified against the client (ollama 0.6.x, httpx 0.28):** with `stream=True` the client returns a generator. A refused connection surfaces when the stream is first read, as a raw `httpx.ConnectError`, not as `ConnectionError` (that is only raised on the non-streaming path). A missing model is `ResponseError` 404 before the stream starts. `Message` has `tool_name`, which is now set on every tool message.
- **Verified live (qwen3-vl:8b):** a piped turn streams and reads `.python-version` (`3.14`). With `OLLAMA_HOST=http://127.0.0.1:1` the CLI prints the Ollama-down error, stays at the prompt, and exits 0. Model not found (`no-such-model:0b`) gives the `ollama pull` message. A cancel from another thread after 8 s on a real stream returns `cancelled` with history `[system, user]`.
- **Not verified:** a real Ctrl+C in a console. Tests inject `KeyboardInterrupt` at each step instead. UNVERIFIED, needs a manual check: a real Ctrl+C on Windows during a silent stream (model load or prompt eval, when no chunk arrives) may not interrupt until the next chunk. Fix options: an httpx read timeout via `ollama.Client(timeout=...)`, or a worker thread (T11/T16).
- **Follow-up (before T11/T16):** on Windows, a blocking socket read may not be interrupted by Ctrl+C until the next chunk arrives, and the ollama client has no read timeout by default. If real Ctrl+C feels slow, run the turn on a worker thread or add a read timeout. Check it by hand first.
- **Deferred to T04/T16:** `thinking` passthrough or a "thinking..." indicator. qwen3-vl thinks before answering and the `thinking` field is ignored, so the user sees nothing for a while. **Resolved in T04** (see T04 notes).
- **Deferred to T04:** after a cancel or an Error with no reply, the user's message stays in history, so a retry adds a second user message. Decide whether to remove it on failure. **Resolved in T04** (removed in the REPL).
- **Follow-up:** `_host_of` reads the host from the ollama client's httpx base URL; fakes fall back to `OLLAMA_HOST`. **Resolved in T04:** the host comes from `Settings` and is passed explicitly.

**T04**
- **num_ctx (verified):** the server is Ollama 0.40.17 (`/api/version`; the `ollama` CLI is not on the bash PATH). Its log (`%LOCALAPPDATA%\Ollama\server.log`) says `vram-based default context total_vram="8.0 GiB" default_num_ctx=4096`, with `OLLAMA_CONTEXT_LENGTH:0` (unset), and the loaded qwen3-vl reports `n_ctx = 4096` although the model trains at 262144. So Ollama's default here is 4096, and it depends on the VRAM the server finds (other GPUs get other defaults). The Ollama docs were not fetched; this rests on the server's own log and API. The server also logged that qwen3-vl at 4096 was predicted to need 16.5 GiB and evicted, so memory is tight on this card.
- **Default chosen: `num_ctx = 8192`** (`config.DEFAULT_NUM_CTX`), sent with every chat call as `options={"num_ctx": ...}`. It fits several file reads (read_file output is capped at 8000 chars) and a longer chat; raise it on a bigger GPU, lower it if memory is short.
- **Config location (verified):** platformdirs 4.14.8. `user_config_dir("local-agent", appauthor=False)` returns `%LOCALAPPDATA%\local-agent` (its default is non-roaming). The code passes `roaming=True` to match PLAN.md M2, so the real path is `C:\Users\geocarlos\AppData\Roaming\local-agent\config.toml` (`config_path()` checked). The app does not create the file or folder.
- **Config design (`config.py`):** `Settings` is a frozen dataclass with model, host, num_ctx, max_tool_rounds, max_output_chars, command_timeout_s and workspace. `load_settings(config_file=, env=, overrides=)` applies defaults, then the file, then `OLLAMA_HOST` (host only; a scheme is added if missing, as the Ollama CLI allows), then the flags. Unknown keys, wrong types and out-of-range values raise `ConfigError` naming the file or flag; `main()` prints it and exits 2. A missing file is defaults, but an explicit `--config` must exist. `--cwd` changes into the folder (`os.chdir` in `main()`) and is stored as the workspace. Sandbox enforcement is still T05.
- **Constants removed:** `loop.MODEL`, `MAX_TOOL_ROUNDS`, `DEFAULT_HOST`, `_host_of` (the `OLLAMA_HOST` lookup), `builtin.MAX_OUTPUT_CHARS` and `COMMAND_TIMEOUT_S`. Tests changed to pass `Settings(...)` or import the `DEFAULT_*` values from `config`; no expected behaviour changed. Tools get limits through `ToolContext` (`tools/registry.py`): a tool declares `context: ToolContext` and the core fills it; it is left out of the schema, and a model cannot send it.
- **Token tracking:** read from the final stream chunk (`prompt_eval_count`, `eval_count`). `TurnDone.usage` is `Usage(prompt_tokens, completion_tokens)`: the prompt size of the last model request, and generated tokens summed over the rounds; `None` if Ollama gave no counts. The CLI prints `[tokens: P of N prompt, G generated]` after each turn and warns at 90% of `num_ctx`. Checked live: a repeated identical prompt reported 259 prompt tokens both times, so no drop from cache reuse was seen; long chats are not checked.
- **Startup probe:** `core/model.probe` calls `client.show(model)` once. It warns when `capabilities` is a list without `tools`, and is silent when the list is absent. Ollama down or model missing at startup prints the T03 message and the REPL still starts, so the same message repeats on the first turn (cosmetic; not changed).
- **Fallback parser (`core/toolcalls.py`):** used only when a reply has no structured tool calls. The whole reply must be call blocks: one bare JSON object or array, or fenced (any or no language word) or `<tool_call>` blocks, with nothing else. A call needs `name` (a registered tool) and `arguments` (an object; absent or null is `{}`; a JSON string holding an object is decoded). Arguments must bind to the signature. Any failing call makes the whole reply plain text, so prose with braces is never a call. Converted calls take the normal approval path. Live: `qwen2.5-coder:7b` printed `{"name": "read_file", "arguments": {"path": ".python-version"}}` as text; it was run as a tool call and answered `3.14`. The raw JSON still appears on screen before the `[Tool Call]` line, because text streams before the end of the reply is known.
- **Deferred item: duplicated user message after cancel or error.** Decided: the rule lives in the REPL (`cli.run`). If a turn recorded no reply (an Ollama error, or Ctrl+C before one), its user message is removed, so resending does not repeat it. The core is unchanged: its history after a cancel or error is still `[system, user]` and tests pin that. Cost: after an error the user retypes the message. Follow-up for T11/T16: move the rule into the core when the stdio server and GUI need it too.
- **Deferred item: `thinking` ignored.** Decided: the reasoning is kept in the assistant message in history (`Message.thinking`), and a new `ThinkingDelta` event makes the CLI print `[thinking...]` once per model round. Caveat: thinking text counts toward `num_ctx`. If it grows the context too fast, drop it from history.
- **Tests:** 181 pass (`uv run pytest`). New files: `test_config.py` (precedence, validation, config path, CLI flags, `main()` exit code), `test_model.py` (probe with and without `tools`, Ollama down and model missing at startup), `test_toolcalls.py` (valid, rejected and prose cases, plus approval through the loop), `test_tool_context.py`. `test_loop.py` and `test_cli.py` gained `num_ctx` in options, usage, thinking, token line, warning and the retry case. No test reads the real config: they pass `config_file=` and `env={}`.
- **Verified live (qwen3-vl:8b, piped turn):** default run reads `.python-version` and answers `3.14` with a token line; `--model qwen3-vl:8b --num-ctx 8192` the same; `--num-ctx 10` exits 2 with a message; `--host http://127.0.0.1:1` and `OLLAMA_HOST=127.0.0.1:1` each show the Ollama-down message and keep the prompt; a bad `--config` file names the file and the key.
- **Not verified:** a real Ctrl+C in a console (still open from T03); the capability warning against a model that lacks `tools` (no such model installed; tested with fakes); a long chat near `num_ctx` on the real server; behaviour with a GPU that gives a different default.
- **Follow-ups:** the fallback parser checks that arguments bind but not their types, like the structured path (T02 hardening note). The Ollama-down message shows twice on a fresh start with Ollama down (startup, then the first turn). `uv add platformdirs` changed `pyproject.toml` and `uv.lock`; CI runs `uv sync --locked`, so commit the lock file with this change.
- **Review follow-ups (orchestrator, after T04 review):**
  - (T05) Resolve tool paths against `settings.workspace`, not the process cwd. `--cwd` does `os.chdir` today; keep it, but don't rely on it. Any test calling `main(["--cwd", ...])` must restore cwd (monkeypatch.chdir).
  - The token line and 90% warning use the last `prompt_eval_count`. With Ollama prompt caching this may count only newly evaluated tokens and under-report on long chats. Check on a long chat; if so, estimate the context size another way.
  - Fallback parser: a reply that is *only* a JSON call to a registered tool runs it (e.g. user asks "show me the JSON for a read_file call"). Writes/execs still ask; read tools run silently. Accepted for now; revisit with tool profiles (T08).
  - Fallback parser: the raw JSON is shown as `Agent: {...}` before the `[Tool Call]` line (seen live with qwen2.5-coder:7b). Cosmetic; the GUI (T16) can hide it.
  - Verified live with qwen2.5-coder:7b: text tool call converted and ran (`read_file` → `3.14`); a converted `write_file` asked for approval, `n` was honoured, no file written.

**T06**
- **read_file:** optional `start_line` and `end_line` (1-based, inclusive, `Annotated` descriptions). Without them the output is the whole file, as before. With them it is numbered (`12: text`) under a header `[lines 3-5 of 10]`. A range past the end, `start_line` past the end, or `end_line < start_line` is a clean error text; `end_line` past the end is clipped.
- **Optional paths:** `list_dir`, `glob`, `grep` and `git_diff` take an optional `path`. Omitted means the workspace (the tool uses `context.workspace`). A `null` value is now treated the same as omitted: `sandbox.check_arguments` skips it, so the tool gets `None`. Before, `null` was refused as "a path must be text". Optional path parameters may be typed `str | None`; the `@tool` check accepts that (`registry._is_path_type`).
- **Output cap:** `truncate()` moved to `tools/output.py` and is shared by `read_file`, `run_command` and the new tools. Marker unchanged: `...[truncated N chars]`. The item caps add their own line: `[stopped at N ...]` (glob, grep), `[... N more entries not shown]` (list_dir).
- **Search scope:** `glob` and `grep` drop any match whose resolved path is outside the folder they start from (junctions and symlinks followed, via `sandbox.resolve`), and skip `.git`. The base folder itself is already sandboxed (and approved if outside). So an approved outside folder is searched in full, but the search never follows a link out of it. `glob` also rejects patterns with `..`, a drive, a colon or a leading slash.
- **grep:** pure Python, `re` syntax, `ignore_case` and `include` (file-name glob) options. Binary files (NUL in the first 8 KB), files over 2 MB and `.git` are skipped. Lines are cut to 300 characters. Output is `path:line: text`, with the path relative to the workspace when inside it, else absolute.
- **git tools:** `git --no-pager -c color.ui=never` in the workspace, no shell, 30 s timeout, `GIT_TERMINAL_PROMPT=0`, `GIT_OPTIONAL_LOCKS=0` (status does not take index locks). Missing git, a non-repository workspace and other failures come back as text. `git_diff` has `path` (sandboxed, passed after `--`) and `staged`. `git_log` has `max_count` (default 20, 1 to 200). Untracked files do not appear in `git_diff`.
- **Repo config cannot run commands (review fix):** every git call also passes `-c core.fsmonitor=false`, `-c core.untrackedCache=false` and `-c core.hooksPath=<nul>`, and `git_diff` adds `--no-textconv`. A `.git/config` `core.fsmonitor` or a `.gitattributes` textconv driver used to run its command from `git_status` or `git_diff` (verified: the marker file was created before the fix). Tests: `test_git_status_and_diff_do_not_run_fsmonitor_from_the_repo_config`, `test_git_diff_does_not_run_a_textconv_from_the_repo_config`.
- **No parent repository (review fix):** `GIT_CEILING_DIRECTORIES` is set to the workspace's parent, so git does not climb above the workspace. A workspace that is a repo root works as before.
  - **Known limitation / follow-up:** a workspace that is a subfolder of a bigger repo now reports "not a git repository". Before, it showed the parent repo's status and diff. Supporting that needs a deliberate design (for example showing paths relative to the repo root).
- **Filter override (fix rounds 2 and 3):** before each git call, the repo-scope `filter.<name>.clean|smudge|process` keys (local or worktree scope, including include.path) are listed with `git config -z --show-scope` and blanked, so git_status and git_diff run no repo-defined filter drivers. The overrides go in through the environment (`GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_n`, `GIT_CONFIG_VALUE_n`), not `-c`: `-c key=` splits at the first `=`, so a filter named `a=b` (`*.txt filter=a=b`) still ran its clean filter (verified: plain `git diff` fired it, and so did the `-c` form). Env entries are appended after any already inherited, and they are command scope, so they beat local config. Each filter name also gets `filter.<name>.required=false`, so a repo with `git lfs install --local` does not fail with "clean filter 'lfs' failed". System and global filters such as Git LFS are untouched. **Fail closed:** if the listing times out, raises OSError, or exits with a code other than 0 (keys found) or 1 (none found), git is not run and the tool returns "Error: could not read the git config, so git was not run ...". A non-repository workspace still gets "The workspace is not a git repository". Tests: `test_git_tools_do_not_run_a_*_filter*`, `test_git_diff_keeps_entries_already_in_the_environment_and_still_blocks_the_filter`, `test_a_local_required_filter_does_not_make_the_diff_fail` and `test_git_does_not_run_when_the_filter_listing_fails` in `tests/test_read_tools.py`.
- **Not set: `GIT_CONFIG_NOSYSTEM=1`.** Git for Windows sets `core.autocrlf=true` in the system config. Ignoring the system config made every CRLF checkout show as modified (checked with a scratch repo). Repo-controlled settings are covered by the `-c` flags above, and the system config is not repo-controlled, so it is kept.
- **read_file line numbers (review fix):** the ranged branch splits on `\n` only (a trailing `\r` is dropped per line), not `str.splitlines()`, so \x0b, \x0c, \x1c-\x1e, \x85, \u2028 and \u2029 stay inside a line. A trailing newline does not add an empty line to the total. The whole-file output is unchanged. Test: `test_read_file_ranged_lines_split_only_on_newline`.
- **Follow-ups:**
  - Argument types are still not validated (T02 hardening note). A model that sends `"5"` for `start_line` gets a TypeError text from the loop, not a range error.
  - `glob` and `grep` can still walk into a junction that leads outside before the result is dropped. This costs time, but the walk never returns a path outside the base.
  - `grep` reads each file in full (up to 2 MB). Fine for a workspace; a streaming reader would be needed for very large searches.
  - No `git_show` or `git_blame`; PLAN.md lists only the seven tools above.
