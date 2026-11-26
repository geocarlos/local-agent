# local-agent protocol

`local-agent serve --stdio` runs the agent as an Agent Client Protocol (ACP) agent. An editor or a script starts it as a child process and talks to it over stdin and stdout. `local-agent serve --web` serves the same messages over a WebSocket on localhost, for the web UI and other local clients (see [WebSocket transport](#websocket-transport-serve---web)).

- **ACP version:** protocol version **1** (`protocolVersion: 1`). Checked against the v1 schema (release `schema.json` of agentclientprotocol.com, fetched 2026-11-25) and the v1 pages for initialization, session setup, prompt turn, tool calls, cancellation and errors. ACP v2 is in preview upstream and is not implemented; the server answers `protocolVersion: 1` to any client version.
- **Transport:** JSON-RPC 2.0, one message per line, UTF-8, each line ends with `\n`. Output is ASCII-only JSON (non-ASCII characters are escaped), so any console encoding reads it.
- **Scope:** the subset the agent needs, listed below. Everything else in ACP is not implemented.

## Starting the server

```
local-agent serve --stdio [--model NAME] [--host URL] [--num-ctx N] [--cwd FOLDER] [--profile NAME] [--config FILE] [--no-plugins]
```

- Choose one transport: `--stdio` (this section) or `--web` (see [WebSocket transport](#websocket-transport-serve---web)). `--port` applies to `--web` only.
- The settings flags are the same as for `local-agent chat`, and use the same config file, `OLLAMA_HOST` and precedence (flags, then environment, then config file, then defaults; see README).
- `--cwd` is the default workspace. A session can name its own `cwd` in `session/new`.
- Plugins and MCP servers from the config start before the first message and close at EOF.
- **stdout carries only protocol lines.** Warnings and logs (plugin and MCP problems, the model check at start-up, unsupported request fields) go to stderr. While the server runs, file descriptor 1 (the process's stdout) is pointed at stderr, and `sys.stdout` too. So a stray `print`, `os.system`, a `subprocess` with no capture, or a direct `os.write(1, ...)` from a plugin or a tool lands on stderr. The protocol writes to a copy of the original stdout, and fd 1 is restored when the server returns.

Shutdown: when stdin reaches EOF, the server cancels every running turn and waits for them with one shared deadline of 10 s in total. A turn still running after that is reported on stderr. Then the server closes the MCP servers and the plugins and exits with code 0. Ctrl+C does the same.

## WebSocket transport (`serve --web`)

```
local-agent serve --web [--port N] [the same settings flags as --stdio]
```

The server listens on `127.0.0.1` only, on port `N` (default `0`: a free port the system picks). It prints these lines to stderr at start-up:

```
local-agent web server: http://127.0.0.1:52811/
Open this link in a browser: http://127.0.0.1:52811/#token=<token>
WebSocket: ws://127.0.0.1:52811/ws?token=<token>
```

**Routes (HTTP).** The token is never needed for the page itself:

| Route | Answer |
| --- | --- |
| `GET /` | the web UI (`index.html`) |
| `GET /<file>` | a file of the web folder (`src/local_agent/web/`): `.html`, `.css`, `.js`, `.svg`, `.png`, `.ico`, with its content type. Any other path, a folder, a `..` or backslash path, a symbolic link out of the folder: `404` |
| `GET /info` | `{"cwd": <workspace>, "protocolVersion": 1}`, `Cache-Control: no-store`. Needs the token in an `Authorization: Bearer` header (not in the query, so it stays out of logs and history); without it, `403` |
| `GET /ws` | the WebSocket (below) |

Every HTTP response carries `Content-Security-Policy` (`default-src 'none'`; `script-src 'self'`; `style-src 'self'`; `img-src 'self'`; `connect-src 'self' ws://127.0.0.1:PORT ws://localhost:PORT`; no inline scripts or styles), `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. The OpenAPI and docs routes are not served.

**The web UI and the token.** The page reads the token from the URL fragment (`#token=...`). A browser does not send a fragment to the server, so the token is not in any request line or log. The page removes the fragment from the address bar and keeps the token in memory only. It then calls `/info` and opens `/ws?token=...`.

**Token.** Each process makes a new random token (32 bytes, URL-safe, `secrets.token_urlsafe`) when it starts; it is not saved and not reused. Every WebSocket must present it, in one of two ways:

- the query parameter `token` on the WebSocket URL (`ws://127.0.0.1:PORT/ws?token=...`). Browsers cannot set headers on a WebSocket, so this is the way the web UI connects;
- or the header `Authorization: Bearer <token>`, for scripts and editor extensions. If an `Authorization` header is sent, it is the one that is checked.

The comparison is constant-time (`hmac.compare_digest`). A missing or wrong token is refused.

**Origin.** A browser always sends `Origin` on a WebSocket. The server accepts an `Origin` only if it is one of its own origins, `http://127.0.0.1:<port>` or `http://localhost:<port>` (the scheme, host and port must match exactly). Any other origin is refused, even with the right token: `https://`, another port, another host, `null`, and the origins of browser-based extensions and webviews (`vscode-webview://...`, `chrome-extension://...`). So a client that runs in a webview or an extension sends the token as `Authorization: Bearer` and sends no `Origin`; a page served from this server (T16) uses the same origin and is accepted. **A request with no `Origin` header is accepted if the token is right.** Such a request comes from a non-browser client (a script, an extension, a test tool). A web page cannot send a request without `Origin`, so this does not weaken the check against web pages. The token still stops other local programs that do not have it.

**Refusals.** A refused connection is closed before it is accepted, so the handshake gets HTTP `403`; the server logs one warning line on stderr that gives the reason and does not print the token. The check runs before any message is read.

**Messages.** One JSON-RPC message per WebSocket text frame. It is the same JSON as one stdio line, without the trailing newline. The agent sends each message as one text frame. The methods, the events, the errors and the permission flow are the same as on stdio, so everything in this document applies. Each WebSocket is its own server: sessions, the "allow always" answers and the one-prompt-per-session rule are per connection, and a `sessionId` from another connection is `-32002`. A binary frame is read as UTF-8 text. A line longer than 16 MiB of characters gets `-32600`, as on stdio, and the connection stays open. The WebSocket layer accepts a message of up to 64 MiB of UTF-8 bytes (the most a 16 MiB line can take), so every such line reaches the server; a larger message is closed by the WebSocket layer with code 1009.

**Shared runtime.** Plugins, MCP servers and the model check start once for the process, before the server accepts connections, and are closed after the last connection's turns stop. Ctrl+C stops the server; the running turns are cancelled as on stdio, with the same 10 s shared deadline.

**Logs.** uvicorn's access log is off, so the token is not written in the log. Nothing else is printed to stdout by the server.

**No HTTP protocol routes.** The protocol is only on `/ws`. The routes above are for the web UI.

## Framing

Every line is one JSON object:

- A **request** has `jsonrpc`, `id`, `method` and `params`. The response has the same `id` and either `result` or `error`.
- A **notification** has no `id` and gets no response. The agent sends `session/update` notifications while a prompt runs.
- The agent also sends **requests** to the client, with its own ids (`srv-1`, `srv-2`, ...). The only one is `session/request_permission`. The client answers it with a response that carries the same id.

A line that is not valid JSON gets a `-32700` response with `id: null`, and the server keeps running. JSON nested too deeply to parse is `-32700` too. A line that is not a JSON-RPC request gets `-32600`. A line longer than 16 MiB (characters) gets `-32600` with `id: null`; the rest of that line is discarded and the server keeps running.

A response from the client whose `id` is not a string (the server's own request ids are strings) is logged on stderr and ignored.

## Methods

Requests must follow this order: `initialize` first, then `session/new`, then `session/prompt` as often as needed. Any other request before `initialize` gets `-32600`.

### `initialize` (client request)

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}
```
```json
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,"agentCapabilities":{"loadSession":true,"sessionCapabilities":{"list":{}},"promptCapabilities":{"image":false,"audio":false,"embeddedContext":false}},"authMethods":[],"agentInfo":{"name":"local-agent","version":"0.1.0"}}}
```

`protocolVersion` must be an integer (else `-32602`). The agent always answers `1`. Prompts take text blocks only, so all prompt capabilities are `false`. `authMethods` is empty: there is no login. `loadSession` and `sessionCapabilities.list` are advertised: the agent supports `session/load` and `session/list` (below).

`clientCapabilities` is read (T20): with `fs.readTextFile`, `fs.writeTextFile` or `terminal` set to `true`, the agent sends the client methods in "Client methods" below for the file and command tools. Each one is separate. The agent does not advertise anything for them of its own. The CLI, the browser and the pywebview window declare none, so they keep the local tools.

### `session/new` (client request)

```json
{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"C:\\work\\my-project","mcpServers":[]}}
```
```json
{"jsonrpc":"2.0","id":2,"result":{"sessionId":"sess-6f1c9d0a2b7e4c3f8a1d5e9b0c2f4a6d","configOptions":[{"id":"model","name":"Model","category":"model","type":"select","currentValue":"qwen3-vl:8b","options":[{"value":"qwen2.5-coder:7b","name":"qwen2.5-coder:7b"},{"value":"qwen3-vl:8b","name":"qwen3-vl:8b"}]}],"_meta":{"model":{"name":"qwen3-vl:8b","tools":true}}}}
```

- `cwd` must be an absolute path to an existing folder (else `-32602`). It is the session's workspace: file tools are sandboxed to it, and `run_command` starts there. Each session has its own workspace and its own "allow always" answers.
- Any existing absolute folder is accepted, including one outside the server's `--cwd`. The client is trusted: the workspace is not restricted to a list of allowed roots, and read tools inside the folder run without asking. A client should only name folders its user has chosen.
- A server keeps at most 64 sessions. A `session/new` beyond that gets `-32600` (the message says so). Finished sessions are not removed, so start another server for more.
- `mcpServers` must be a list (else `-32602`). **Not supported yet:** servers named in the request are ignored, and a warning goes to stderr. Configure MCP servers in `config.toml` instead.
- `additionalDirectories` and `systemPrompt` are ignored.
- Each session has its own conversation history.
- `configOptions` is the session's model choice (see `session/set_config_option`). Its choices are the models Ollama has. If Ollama does not answer within 5 s, the only choice is the current model and `_meta.warnings` says why. `_meta.model.tools` is `true`, `false` (the model does not report the `tools` capability; a warning is added to `_meta.warnings`) or `null` (Ollama reports no capabilities).
- A session is saved after its first turn, not now (see Saved sessions below).

### `session/list` (client request)

Lists the saved sessions (ACP v1; needs `sessionCapabilities.list`). Only sessions that were saved (after a turn) are listed.

```json
{"jsonrpc":"2.0","id":4,"method":"session/list","params":{"cwd":"C:\\work\\my-project","cursor":"50"}}
```
```json
{"jsonrpc":"2.0","id":4,"result":{"sessions":[{"sessionId":"sess-6f1c...","cwd":"C:\\work\\my-project","title":"What is in README.md?","updatedAt":"2026-11-25T20:40:12+00:00","_meta":{"model":"qwen3-vl:8b"}}],"nextCursor":"50"}}
```

- Both params are optional. `cwd` (absolute) keeps only the sessions of that folder. `cursor` is the `nextCursor` of the previous page. A page has 50 sessions; `nextCursor` is absent on the last page. A bad cursor is `-32602`.
- Newest first. A saved file that cannot be read is skipped, with a warning on stderr.

### `session/load` (client request)

Reopens a saved session (ACP v1; needs `loadSession`). The saved history is replayed as `session/update` notifications, and the response comes after the replay:

```json
{"jsonrpc":"2.0","id":5,"method":"session/load","params":{"sessionId":"sess-6f1c...","cwd":"C:\\work\\my-project","mcpServers":[]}}
```
```json
{"jsonrpc":"2.0","id":5,"result":{"configOptions":[{"id":"model","name":"Model","category":"model","type":"select","currentValue":"qwen3-vl:8b","options":[...]}],"_meta":{"model":{"name":"qwen3-vl:8b","tools":true}}}}
```

- The replay uses the same updates as a live turn: `user_message_chunk` (a user message, one `messageId` each), `agent_message_chunk` (a reply), and for each tool call a `tool_call` (status `pending`) followed by a `tool_call_update` with status `completed`, or `failed` for a call that was cancelled. The reasoning text is not saved, so it is not replayed.
- After the load, `session/prompt` continues the conversation: the model sees the saved history.
- `cwd` is optional. If given, it must be the session's saved folder (else `-32602`). The saved folder is the session's workspace.
- A session whose folder no longer exists is `-32602`, with a message that says so; the session file is kept. An unknown or unreadable session is `-32002`. A session that is running a prompt in this process is `-32600`.
- "Allow always" answers are not saved: a reopened session asks again. The model is the saved one; change it with `session/set_config_option`.

### `session/set_config_option` (client request)

Switches the model of one session (ACP v1 session configuration; the only option is `model`):

```json
{"jsonrpc":"2.0","id":6,"method":"session/set_config_option","params":{"sessionId":"sess-6f1c...","configId":"model","value":"qwen2.5-coder:7b"}}
```
```json
{"jsonrpc":"2.0","id":6,"result":{"configOptions":[{"id":"model","name":"Model","category":"model","type":"select","currentValue":"qwen2.5-coder:7b","options":[...]}],"_meta":{"model":{"name":"qwen2.5-coder:7b","tools":true}}}}
```

- `value` must be an installed model. A model Ollama does not have is `-32602`. Ollama unreachable or not answering: `-32603` with the usual Ollama message, and the session keeps its model.
- A model without the `tools` capability is accepted, with a `_meta.warnings` entry.
- A prompt running in the session: `-32600`. The change applies to the next prompt. Other sessions keep their own model.
- The default model for new sessions is the `model` setting (see the settings methods below).

### Extension methods

ACP reserves method names that start with `_` for extensions. These are local-agent's own, the same on stdio and on the WebSocket. They need `initialize` first.

#### `_local-agent/models`

```json
{"jsonrpc":"2.0","id":7,"method":"_local-agent/models","params":{"sessionId":"sess-6f1c..."}}
```
```json
{"jsonrpc":"2.0","id":7,"result":{"current":"qwen3-vl:8b","models":["qwen2.5-coder:7b","qwen3-vl:8b"],"error":null}}
```

The installed models from Ollama, for a refresh of the picker. `sessionId` is optional (with it, `current` is that session's model; otherwise the default). When Ollama cannot be reached, `models` is empty and `error` holds the message; the request itself does not fail.

#### `_local-agent/settings/get` and `_local-agent/settings/set`

The settings editor reads and writes the per-user `config.toml`, or the `--config` file the server was started with.

```json
{"jsonrpc":"2.0","id":8,"method":"_local-agent/settings/get","params":{}}
```
```json
{"jsonrpc":"2.0","id":8,"result":{"file":"C:\\Users\\me\\AppData\\Roaming\\local-agent\\config.toml","exists":true,"values":{"model":"qwen3-vl:8b","host":"http://127.0.0.1:11434","num_ctx":8192,"max_tool_rounds":10,"max_output_chars":8000,"command_timeout_s":60,"network_timeout_s":20,"tool_profile":"coding"},"kinds":{"model":"text","host":"text","num_ctx":"int","max_tool_rounds":"int","max_output_chars":"int","command_timeout_s":"int","network_timeout_s":"int","tool_profile":"choice"},"applies":"restart"}}
```

- `values` are the file's values with the defaults filled in. A missing file gives the defaults and `exists: false`. A file the agent cannot load is `-32602`, with the reason.
- `_local-agent/settings/set` takes `{"values": {...}}` with any subset of the editable keys. It checks each value with the same rules as the file and the flags, then writes the file atomically (a temp file, checked as a whole, then renamed over it). The response is the `get` answer with `"saved": true`. The folder is created if needed.
- **Allowlist (decided).** Only `model`, `host`, `num_ctx`, `max_tool_rounds`, `max_output_chars`, `command_timeout_s`, `network_timeout_s` and `tool_profile` can be set. Any other key is `-32602` and nothing is written. Kept out on purpose: `network_enabled` (turns on the network tool), `workspace` (the default folder for the file tools), `plugin_dir`, `plugins_enabled`, `disabled_plugins` (plugins are Python files that run in the process), `mcp_servers` and `test_command` (they name programs that are started). Those are edited by hand.
- Comments and the layout of the file are kept (tomlkit). A key this version does not know is refused when the file is loaded, so the editor never writes one. Integer limits: `num_ctx` up to 1048576, `max_output_chars` up to 1000000, `max_tool_rounds` up to 200, `command_timeout_s` and `network_timeout_s` up to 3600; every integer is at most 2**63 - 1. The same limits hold for the file and the flags.
- **Ollama address (decided).** The editor accepts `host` only when it is loopback (`127.0.0.1`, `localhost` or `::1`, any port). Another host is `-32602` with a message that says to edit `config.toml` by hand. A change of `host` is logged on stderr (it takes effect at the next start).
- **Applies on restart (decided).** The server reads the file when it starts. A running server keeps its settings; a chat's model changes at any time with `session/set_config_option`. `applies` says `restart`.

## Saved sessions

Each session is saved after each turn, to one JSON file in the per-user data folder: `%APPDATA%\local-agent\sessions\sess-<32 hex digits>.json` (platformdirs, roaming). Nothing is written into the install folder. The environment variable `LOCAL_AGENT_DATA_DIR` replaces the data folder (the tests use it).

```json
{"format": 1, "id": "sess-...", "cwd": "C:\\work\\my-project", "model": "qwen3-vl:8b", "title": "What is in README.md?", "created": "2026-11-25T20:39:01+00:00", "updated": "2026-11-25T20:40:12+00:00", "messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]}
```

- Writes are atomic: the JSON goes to a temp file beside the target, is flushed, and is renamed over it. A crash leaves the old file or the new one.
- A file that cannot be parsed, has another `format`, or whose `id` does not match its name is skipped by `session/list` (a warning on stderr) and refused by `session/load`. One bad file does not hide the others.
- The title is the first user message, whitespace collapsed and cut to 80 characters.
- **Limits (decided):** a session file over 5 MiB is not saved. A failed save (too large, or the disk refuses) is logged on stderr and reported to the user: the `session/prompt` answer carries `_meta.warnings` (a list of text), and the chat window shows them. The chat goes on, but its later messages are lost after a restart. At most 200 session files are kept: after a save, the least recently updated files beyond 200 are deleted, with a warning that says how many. Corrupt files count toward the 200 (they are not separated out). A session id from a client must match `sess-` plus 32 lowercase hex digits before any path is built.
- The reasoning text is not saved. "Allow always" answers are not saved.
- The CLI (`local-agent chat`) does not save sessions. Only the server does.

## Worked example: Ollama down
### `session/prompt` (client request)

```json
{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"sess-6f1c9d0a2b7e4c3f8a1d5e9b0c2f4a6d","prompt":[{"type":"text","text":"What is in README.md?"}]}}
```

The server streams `session/update` notifications while the turn runs (see below). The response comes when the turn ends:

```json
{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn","_meta":{"usage":{"promptTokens":1921,"completionTokens":561}}}}
```

- `_meta.warnings` (optional) is a list of text, for example that the chat could not be saved. A failed turn's error carries the same list in `data.warnings`.
- `stopReason` is `end_turn` (the model finished), `cancelled` (the client sent `session/cancel`, or the server was shut down), or `max_turn_requests` (the tool round cap of `max_tool_rounds` was reached). `max_tokens` and `refusal` are not produced.
- `_meta.usage` is an extension (ACP has no token counts in the prompt result): `promptTokens` is the prompt size of the last model request, and `completionTokens` the tokens generated over all rounds. It is absent when Ollama reported no counts.
- Only one prompt runs per session. A second `session/prompt` to a session that is busy gets `-32600` at once; the running turn is not affected.
- `prompt` must be a non-empty list of `text` blocks. Other block types (images, resource links, embedded resources) get `-32602`, since the agent does not declare those capabilities. An empty or all-whitespace text is `-32602` too. Text that is not valid Unicode, such as a lone surrogate (JSON `"\ud800"`), is `-32602`; the message is not added to history.

### `session/cancel` (client notification)

```json
{"jsonrpc":"2.0","method":"session/cancel","params":{"sessionId":"sess-6f1c9d0a2b7e4c3f8a1d5e9b0c2f4a6d"}}
```

No response. The turn stops as soon as it can: between streamed chunks, before each tool call, and at a pending permission request (see below). The prompt then answers with `stopReason: "cancelled"`. A cancel for a session that is not running a prompt, or for an unknown session, is ignored.

Cancelling keeps the history valid. A reply cut off mid-stream is dropped. Tool calls that did not run get a failed result (`Cancelled by user.`), so each tool call in history has its answer. If the turn recorded no reply at all, the user message is removed from history, so sending the same text again does not duplicate it.

### `session/update` (agent notification)

All updates have `params: {"sessionId": ..., "update": {...}}`. `sessionUpdate` is the discriminator.

| `sessionUpdate` | When | Fields |
| --- | --- | --- |
| `agent_message_chunk` | a piece of the reply text | `content` (a text block), `messageId` |
| `agent_thought_chunk` | a piece of the model's reasoning (thinking models) | `content`, `messageId` |
| `tool_call` | the model asked for a tool; sent before any approval | `toolCallId`, `title`, `kind`, `status: "pending"`, `rawInput` |
| `tool_call_update` | the tool started (`in_progress`) or finished (`completed` or `failed`) | `toolCallId`, `status`, and for a finished call `content` |
| `usage_update` | at the end of a turn that reported token counts | `used` (prompt tokens), `size` (the context window, `num_ctx`) |

`messageId` is a new id for each run of text. A tool step or a switch between text and thought starts a new message. Text chunks of one reply concatenate in order.

Examples (one line each in the real output):

```json
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"sess-6f1c...","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"The README says "},"messageId":"4b9d0e7a2c1f4e8b9a3d5c7e1f2a4b6c"}}}
```
```json
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"sess-6f1c...","update":{"sessionUpdate":"tool_call","toolCallId":"t1-1.1","title":"read_file(path='README.md')","kind":"read","status":"pending","rawInput":{"path":"README.md"}}}}
```
```json
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"sess-6f1c...","update":{"sessionUpdate":"tool_call_update","toolCallId":"t1-1.1","status":"completed","content":[{"type":"content","content":{"type":"text","text":"# local-agent ..."}}]}}}
```
```json
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"sess-6f1c...","update":{"sessionUpdate":"usage_update","used":1921,"size":8192}}}
```

- `title` is a short label made from the tool name and its arguments (at most 120 characters). The approval question, which is more readable, is sent in the permission request.
- `kind` is the ACP tool kind from the tool's risk tier: `read` (read tools), `edit` (write), `execute` (exec), `fetch` (network), `other` (unknown tool names).
- `content` of a finished call is the text the model receives, as shown by the tools (already truncated).
- A call the client refused, or one blocked by the profile, goes `pending` then `failed` with no `in_progress`.

### `session/request_permission` (agent request to the client)

Sent before any tool that needs approval runs: write, exec and network tools, and a path outside the workspace. Read tools do not ask.

```json
{"jsonrpc":"2.0","id":"srv-1","method":"session/request_permission","params":{"sessionId":"sess-6f1c...","toolCall":{"toolCallId":"t1-1.1","title":"Write 12 chars to 'out.txt'","status":"pending","rawInput":{"path":"out.txt","content":"hello world\n"}},"options":[{"optionId":"allow_once","name":"Allow once","kind":"allow_once"},{"optionId":"allow_always","name":"Allow always for this session","kind":"allow_always"},{"optionId":"reject_once","name":"Reject","kind":"reject_once"}]}}
```

The client answers with the same `id`:

```json
{"jsonrpc":"2.0","id":"srv-1","result":{"outcome":{"outcome":"selected","optionId":"allow_once"}}}
```

- `toolCall._meta.preview` is an extension (ACP's `_meta` is the place for them, as `_meta.usage` is in the prompt result): what the user approves, when the tool has one. `{"kind": "diff" | "text" | "command", "text": ..., "path": ... or null, "shell": ... or null}`. A `diff` is a unified diff (`---`, `+++`, `@@`, then lines starting with `+`, `-` or a space). A `text` is proposed file content (with a `path`) or a preview of a call's arguments or a fetch (no path). A `command` is a command line with the shell that runs it. The text is capped at 1500 characters for display; a capped text ends with `\n...[N more chars]`, and the UI says the preview is truncated. A tool without a preview has no `_meta.preview` key.
- The options offered are these, by `optionId` (the ACP kind):
  - `allow_once`: run this call only.
  - `allow_always`: run it and allow the same tool in the same workspace for the rest of the session. Offered only where the agent allows it: not for a path outside the workspace, a write under a `.git` folder, a write into the plugin folder, `git_commit`, or `web_fetch`.
  - `reject_once`: do not run it. The model is told the call was refused.
  - `reject_always` is never offered.
- An `optionId` that was not offered in that request is treated as a refusal. The agent does not accept an option it did not offer, even one a more lenient frontend would honour.
- Outcome `{"outcome":"cancelled"}` refuses that one call. The model is told the call was refused, and the turn goes on. The turn ends with `stopReason: "cancelled"` only when `session/cancel` arrives (see above). To stop a turn, send `session/cancel`.
- Answers are per session. A new session asks again.
- When `session/cancel` arrives while a permission request is pending, the agent stops waiting and refuses the call. The client's later answer is ignored, so the client may answer `cancelled` or just skip it.

Full example, a write approved once:

```
-> {"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{...,"prompt":[{"type":"text","text":"write hello to out.txt"}]}}
<- {"jsonrpc":"2.0","method":"session/update","params":{...,"update":{"sessionUpdate":"tool_call","toolCallId":"t1-1.1","title":"write_file(...)","kind":"edit","status":"pending",...}}}
<- {"jsonrpc":"2.0","id":"srv-1","method":"session/request_permission","params":{...,"options":[...]}}
-> {"jsonrpc":"2.0","id":"srv-1","result":{"outcome":{"outcome":"selected","optionId":"allow_once"}}}
<- {"jsonrpc":"2.0","method":"session/update","params":{...,"update":{"sessionUpdate":"tool_call_update","toolCallId":"t1-1.1","status":"in_progress"}}}
<- {"jsonrpc":"2.0","method":"session/update","params":{...,"update":{"sessionUpdate":"tool_call_update","toolCallId":"t1-1.1","status":"completed","content":[...]}}}
<- {"jsonrpc":"2.0","method":"session/update","params":{...,"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"Done."},"messageId":"..."}}}
<- {"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}
```

`toolCallId` is `t<turn>-<call>`, where `<turn>` counts the prompts of the session (1, 2, ...), so ids stay unique within a session.

### Client methods (agent requests to the client, T20)

The agent's file and command tools run in the client when the client declared the capability in `initialize`:

| Capability | Tools | Requests |
|---|---|---|
| `fs.readTextFile` | `read_file`, and the current text for `edit_file` and the `write_file` preview | `fs/read_text_file` |
| `fs.writeTextFile` | `write_file`, `edit_file` | `fs/write_text_file` |
| `terminal` | `run_command`, `run_tests` | `terminal/create`, `terminal/wait_for_exit`, `terminal/output`, `terminal/kill`, `terminal/release` |

The names and shapes are ACP v1's, checked against the v1 release `schema.json` (agentclientprotocol.com, fetched 2026-11-26). All requests carry `sessionId`.

```json
{"jsonrpc":"2.0","id":"srv-4","method":"fs/read_text_file","params":{"sessionId":"sess-6f1c...","path":"C:\\work\\a.txt"}}
{"jsonrpc":"2.0","id":"srv-4","result":{"content":"unsaved text\n"}}
{"jsonrpc":"2.0","id":"srv-5","method":"fs/write_text_file","params":{"sessionId":"sess-6f1c...","path":"C:\\work\\a.txt","content":"new text\n"}}
{"jsonrpc":"2.0","id":"srv-5","result":{"_meta":{"saved":true}}}
{"jsonrpc":"2.0","id":"srv-6","method":"terminal/create","params":{"sessionId":"sess-6f1c...","command":"echo hi","args":[],"cwd":"C:\\work"}}
{"jsonrpc":"2.0","id":"srv-6","result":{"terminalId":"t1"}}
```

- `fs/read_text_file`: params `path` (absolute, already checked against the workspace), optional `line` (1-based) and `limit` (lines). Result `{content}`: the text the editor has, unsaved changes included. The agent asks for the whole file (no `line`/`limit`) and numbers the lines itself. A missing file is the error `-32002`.
- `fs/write_text_file`: params `path`, `content`. Result `null`, or `{"_meta": {"saved": false}}` when the change is in the editor but not on disk (the user had unsaved edits of their own). The agent tells the model in that case.
- `terminal/create`: params `command`, `args` (a list, may be empty), `cwd` (absolute), optional `outputByteLimit`. Result `{terminalId}`. The agent sends a `cmd` command as one command line with `args: []`. The client runs a command with no arguments through the platform shell (`cmd.exe /d /s /c` on Windows, `/bin/sh -c` elsewhere) and a command with arguments as a program. PowerShell is sent as `powershell -NoProfile -NonInteractive -Command <text>` with those arguments.
- `terminal/wait_for_exit` → `{exitCode, signal}`. `terminal/output` → `{output, truncated, exitStatus}`, with `exitStatus` `null` while the command runs. `terminal/kill` and `terminal/release` → `null`. `output` is stdout and stderr together.
- Time limits: an fs request waits 30 s, and `terminal/output`, `kill` and `release` 5 s. `terminal/wait_for_exit` waits for the command timeout (`command_timeout_s`). On a timeout the agent kills the command, reads its output and releases it, and the tool result says it timed out.
- Cancel: a `session/cancel` while the agent waits for an fs request or `terminal/wait_for_exit` ends the turn as `cancelled`. A command that was running is killed and released. The client does not need to answer after a cancel; a late answer is dropped without a warning.
- An error answer, or no answer in time, is text for the model (`Error: ...`), and the turn goes on. The agent does not retry.
- Approval and the sandbox are unchanged: the agent asks `session/request_permission` first where a tool asks, a path outside the workspace is asked for, and nothing is sent to the client for a call that is refused.
- The approval diff for `write_file` and `edit_file` is computed from the client's text when the client can read, so the user sees the change against what is in the editor.

## Errors

Errors are JSON-RPC error responses with `code`, `message` and, where useful, `data`.

| Code | Meaning | Where |
| --- | --- | --- |
| `-32700` | Parse error: the line is not valid JSON, or is nested too deeply to parse | any; `id` is `null` |
| `-32600` | Invalid request: not a JSON-RPC 2.0 object; a request other than `initialize` before `initialize`; a second prompt while one runs in the session; a line over 16 MiB; a `session/new` beyond 64 sessions | any |
| `-32601` | Method not found | any request |
| `-32602` | Invalid params: a missing or wrong field, a relative or missing `cwd`, an unsupported prompt block, an empty prompt, prompt text that is not valid Unicode | `initialize`, `session/new`, `session/prompt` |
| `-32002` | Resource not found: unknown `sessionId` (`data.sessionId`) | `session/prompt` |
| `-32603` | Internal error. Also used for a failed turn: Ollama unreachable, model not installed, or an unexpected bug. The message says which, for example `Cannot reach Ollama at http://127.0.0.1:11434. Start it with ollama serve ...` | `session/prompt` |

A failed turn is an error response to `session/prompt`, not a `stopReason`, as in ACP. The server keeps running and the session stays usable. The user message of the failed turn is removed from history only when the turn recorded no reply (for example Ollama was down before the first answer). If a reply was recorded before the failure, the user message stays, so a retry repeats it.

A notification with an unknown method, or a `session/cancel` for an unknown session, is ignored and logged to stderr. A request the server cannot match to a pending call is ignored.

## Worked example: Ollama down

```
-> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}
<- {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,...}}
-> {"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"C:\\work\\my-project","mcpServers":[]}}
<- {"jsonrpc":"2.0","id":2,"result":{"sessionId":"sess-..."}}
-> {"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"sess-...","prompt":[{"type":"text","text":"hi"}]}}
<- {"jsonrpc":"2.0","id":3,"error":{"code":-32603,"message":"Cannot reach Ollama at http://127.0.0.1:11434. Start it with `ollama serve` (or check OLLAMA_HOST), then try again."}}
```

## Deviations from ACP v1

Checked for T17 against the v1 release `schema.json` (agentclientprotocol.com, downloaded 2026-11-25): `session/list`, `session/load`, `session/set_config_option`, `user_message_chunk`, `sessionCapabilities.list` (an object, `{}`) and `loadSession` (a boolean) are all there, and none is marked unstable.

- **Not implemented:** `session/resume`, `session/close`, `session/delete`, `session/set_mode`, authentication, `$/cancel_request`, plans, slash commands and subagents. The client's `fs/*` and `terminal/*` methods are used when the client declares them (see Client methods above); the agent does not offer them itself.
- **Client methods (T20):** the agent sends `terminal/create` with `args: []` for a command line (it runs through the client's shell, not as a program). ACP says `command` is the program; a client that starts it directly would not run `cmd` lines. The VS Code host runs them through the shell. `fs/read_text_file` `line` and `limit` are not sent by the agent yet.
- **Session configuration:** only the `model` option, through `session/set_config_option` (ACP v1 shape: `configOptions` with `category: "model"`, `type: "select"`). Checked against the v1 schema (see below).
- **Extensions:** `_local-agent/models`, `_local-agent/settings/get` and `_local-agent/settings/set` (see above).
- **Prompts:** text blocks only.
- **`session/new`:** `mcpServers` is accepted and ignored (warning on stderr). `systemPrompt` and `additionalDirectories` are ignored. The answer carries `configOptions` and `_meta`.
- **Tool calls:** the `title` is generated from the tool name and arguments, not the approval question (the permission request carries that). `status` is `pending` before approval.
- **Token counts:** ACP has no place for them in the prompt result, so `promptTokens` and `completionTokens` are under `_meta.usage`. The `usage_update` `used` value is the prompt size of the last request, and `size` is `num_ctx`.
- **Permission options:** `reject_always` is never offered. The `allow_always` option is offered only in the cases listed above.
- **Error codes:** a failed turn is `-32603`, a busy session is `-32600`. ACP does not name codes for these cases.
- **Permission preview:** `toolCall._meta.preview` of a permission request is an extension (the diff or proposed content the user approves). ACP's own `diff` content type needs the old and new text; the agent has a unified diff, so it is sent as text.
- **Thinking:** sent as `agent_thought_chunk`, which the schema allows. Its text stays in the agent's history, so it counts towards the context window.
