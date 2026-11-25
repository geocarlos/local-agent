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
| `GET /info` | `{"cwd": <workspace>, "protocolVersion": 1}`, `Cache-Control: no-store`. Needs the token (`Authorization: Bearer` or `?token=`); without it, `403` |
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
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,"agentCapabilities":{"loadSession":false,"promptCapabilities":{"image":false,"audio":false,"embeddedContext":false}},"authMethods":[],"agentInfo":{"name":"local-agent","version":"0.1.0"}}}
```

`protocolVersion` must be an integer (else `-32602`). The agent always answers `1`. Prompts take text blocks only, so all prompt capabilities are `false`. `authMethods` is empty: there is no login.

### `session/new` (client request)

```json
{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"C:\\work\\my-project","mcpServers":[]}}
```
```json
{"jsonrpc":"2.0","id":2,"result":{"sessionId":"sess-6f1c9d0a2b7e4c3f8a1d5e9b0c2f4a6d"}}
```

- `cwd` must be an absolute path to an existing folder (else `-32602`). It is the session's workspace: file tools are sandboxed to it, and `run_command` starts there. Each session has its own workspace and its own "allow always" answers.
- Any existing absolute folder is accepted, including one outside the server's `--cwd`. The client is trusted: the workspace is not restricted to a list of allowed roots, and read tools inside the folder run without asking. A client should only name folders its user has chosen.
- A server keeps at most 64 sessions. A `session/new` beyond that gets `-32600` (the message says so). Finished sessions are not removed, so start another server for more.
- `mcpServers` must be a list (else `-32602`). **Not supported yet:** servers named in the request are ignored, and a warning goes to stderr. Configure MCP servers in `config.toml` instead.
- `additionalDirectories` and `systemPrompt` are ignored.
- Each session has its own conversation history.

### `session/prompt` (client request)

```json
{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"sess-6f1c9d0a2b7e4c3f8a1d5e9b0c2f4a6d","prompt":[{"type":"text","text":"What is in README.md?"}]}}
```

The server streams `session/update` notifications while the turn runs (see below). The response comes when the turn ends:

```json
{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn","_meta":{"usage":{"promptTokens":1921,"completionTokens":561}}}}
```

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

- `toolCall.preview` is an extension (not in ACP): what the user approves, when the tool has one. `{"kind": "diff" | "text" | "command", "text": ..., "path": ... or null, "shell": ... or null}`. A `diff` is a unified diff (`---`, `+++`, `@@`, then lines starting with `+`, `-` or a space), capped at 1500 characters for display. A `text` is proposed file content or a preview of a call's arguments. A `command` is a command line with the shell that runs it. A tool without a preview has no `preview` key.
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

- **Not implemented:** `session/load`, `session/list`, `session/resume`, `session/close`, `session/delete`, `session/set_mode`, `session/set_config_option`, authentication, `$/cancel_request`, the client's `fs/*` and `terminal/*` methods (the agent runs its own tools in the workspace), plans, slash commands and subagents.
- **Prompts:** text blocks only.
- **`session/new`:** `mcpServers` is accepted and ignored (warning on stderr). `systemPrompt` and `additionalDirectories` are ignored.
- **Tool calls:** the `title` is generated from the tool name and arguments, not the approval question (the permission request carries that). `status` is `pending` before approval.
- **Token counts:** ACP has no place for them in the prompt result, so `promptTokens` and `completionTokens` are under `_meta.usage`. The `usage_update` `used` value is the prompt size of the last request, and `size` is `num_ctx`.
- **Permission options:** `reject_always` is never offered. The `allow_always` option is offered only in the cases listed above.
- **Error codes:** a failed turn is `-32603`, a busy session is `-32600`. ACP does not name codes for these cases.
- **Permission preview:** the `preview` of a permission request is an extension (the diff or proposed content the user approves). ACP's own `diff` content type needs the old and new text; the agent has a unified diff, so it is sent as text.
- **Thinking:** sent as `agent_thought_chunk`, which the schema allows. Its text stays in the agent's history, so it counts towards the context window.
