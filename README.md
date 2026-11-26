# Wildcat Code Agent

A small command-line coding assistant that runs entirely on local models through [Ollama](https://ollama.com). You chat with it in the terminal; it can read files, and with your approval, write files and run shell commands.

Formerly `local-agent`. The command is now `wildcat-code-agent`. On first run, an existing `%APPDATA%\local-agent` folder (config, plugins, saved sessions) is moved to `%APPDATA%\wildcat-code-agent`. The VS Code setting `localAgent.command` is now `wildcatCodeAgent.command`.

## Features

- **Local models only**: talks to your Ollama server, so code and prompts stay on your machine.
- **Tool calling**: the model can use `read_file` (optional line range), `list_dir`, `glob`, `grep`, `git_status`, `git_diff`, `git_log`, `edit_file` (replace one exact occurrence), `write_file`, `run_command`, `run_tests`, `git_commit` and, off by default, `web_fetch` (see Tool profiles). The read tools run without asking. Models that print a tool call as text instead (for example `qwen2.5-coder:7b`) are handled too.
- **Tool profiles**: `minimal` (read tools only), `coding` (the default: read, write and exec tools) or `all` (adds `web_fetch` when it is switched on). Smaller sets suit small models. Tool descriptions are kept short to save context.
- **MCP servers**: tools from stdio servers that speak the Model Context Protocol, listed in the config and offered as `<server>__<tool>`. They always ask unless the config sets a lower risk tier. See MCP servers.
- **Approval gates**: every write, edit, command, test run, commit and web fetch asks first. `y` allows once, `n` refuses, and `a` (where offered) allows the same tool in this workspace for the rest of the session. Writes and edits show a diff, commands show the command line and its shell. `git_commit` always asks (see Safety).
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
- For the desktop window (`wildcat-code-agent gui`) on Windows: the Microsoft Edge WebView2 runtime. It comes with Windows 11 and most Windows 10 installs (see [Desktop window](#desktop-window-wildcat-code-agent-gui))
- A model with tool support, pulled into Ollama, for example:

  ```sh
  ollama pull qwen3-vl:8b
  ```

## Setup

```sh
uv sync
```

## Install (Windows)

The installer puts `wildcat-code-agent` in `C:\Program Files\wildcat-code-agent\` and adds that folder to the machine PATH, so every account on the computer can run it.

1. Download `wildcat-code-agent-<version>-setup.exe` (or build it, see [Building the installer](#building-the-installer)).
2. Run it as an administrator (it asks for elevation; it needs admin rights to write to Program Files and the machine PATH). It is 64-bit only. The installer is unsigned, so SmartScreen will warn: choose **More info**, then **Run anyway**.
3. Close all terminals (including Windows Terminal and VS Code) and start a fresh one from the Start menu, or sign out and back in (terminals that were already open do not see the PATH change). Then check:

   ```powershell
   wildcat-code-agent --help
   ```

The installer also adds a **wildcat-code-agent** entry to the Start menu. It opens the desktop window (`wildcat-code-agent-gui.exe`, which has no console window); see [Desktop window](#desktop-window-wildcat-code-agent-gui).

To uninstall, use **Settings > Apps > Installed apps** (or Apps & features) and choose wildcat-code-agent. The uninstaller removes its PATH entry and the install folder. It does not remove anyone's `%APPDATA%\wildcat-code-agent` folder, which holds config and plugins (delete it by hand if you want that gone).

The installer creates nothing under any user profile. It does not change Ollama.

## Multiple Windows accounts

One install serves every account on the machine. What is per account and what is shared:

- **The program:** shared, in `C:\Program Files\wildcat-code-agent\`.
- **Config and plugins:** per account, under that account's `%APPDATA%\wildcat-code-agent\` (`config.toml`, and a `tools` folder for plugins). Each account has its own settings and plugins; changing one does not change another.
- **MCP server logs:** per account, in that account's `%TEMP%` (a temp file per server, from `tempfile`), not under `%APPDATA%\wildcat-code-agent`.
- **Workspace:** whatever folder the account runs in (`--cwd`, or the current folder). The tools can only touch files inside it, unless you approve more.
- **Ollama:** the server is one process per port, not per account (see below).
- **VS Code extension:** installed once in each account (`code --install-extension`, see [VS Code extension](#vs-code-extension)). The program it starts is the shared one.

### Ollama and several accounts

What is verified and what is not:

- **Install (docs):** Ollama's Windows installer is per user and needs no administrator (docs.ollama.com/windows, read 2026-11-25). **On this machine** Ollama 0.40.18 is installed in `C:\Program Files\Ollama\` (its uninstall entry is in the current user's registry hive), not the per-user default. So it may be installed differently on another machine.
- **Models folder (docs and this machine):** the default is `%USERPROFILE%\.ollama\models` for the account that runs the server. Set the user environment variable `OLLAMA_MODELS` to change it. **On this machine** the server runs with `OLLAMA_MODELS=E:\.ollama\models` (from its server log); the models are on E:, and `C:\Users\geocarlos\.ollama\models` exists but is empty. The source of that variable was not found in the user or machine environment, so it was not traced.
- **Address (docs and this machine):** the docs (FAQ) say Ollama binds `127.0.0.1:11434` by default. **On this machine** the running server listens on `0.0.0.0:11434` and `[::]:11434` (its log shows `OLLAMA_HOST=http://0.0.0.0:11434`), so it is reachable from the network unless the firewall blocks it. Check `netstat -ano | findstr 11434` on your machine. To keep it local, set `OLLAMA_HOST=127.0.0.1:11434` and restart Ollama.
- **Port 11434 (not verified with two accounts):** one server can bind the port at a time. A second account that starts its own `ollama serve` while the first one runs should fail to bind; the docs do not say so, and this was not tested with two accounts.
- **Other accounts (not verified):** a server started by one account listens on the port for every account on the machine, so another account can reach it at `http://127.0.0.1:11434`, and then uses that server's models. The second account should not start a second server. It should either use the running server (leave `host` at the default, or set `OLLAMA_HOST`), or stop the server and run its own with its own `OLLAMA_MODELS`.

To share one models folder between accounts, set `OLLAMA_MODELS` to one folder (for example `E:\.ollama\models`). The simplest way is a machine-level (System) variable set by an administrator, because Ollama on Windows inherits the user and system environment variables. The alternative is the user variable in each account that runs Ollama. Restart Ollama after changing it. Accounts that only use a running server do not need it.

Notes on a shared folder (not verified on this machine with two accounts):
- Each account that runs a server and pulls models needs Modify rights on the shared folder. Read-only is enough for an account that never pulls.
- Avoid two servers writing to the same folder at the same time.
- A server started by account A stops when A signs out, so account B depends on A staying signed in while B uses that server.
- Account B's Ollama tray app may start automatically and fail to bind port 11434 while A's server runs. This is expected from the port rule above, but not observed.

If Ollama is not reachable from an account, `wildcat-code-agent` prints the usual message and keeps the prompt: start Ollama from that account, or set `host` (or `OLLAMA_HOST`) to the address of the server that is running.

Manual check for a second account: [docs/manual-checks/T13-second-account.md](docs/manual-checks/T13-second-account.md).

## Usage

```sh
uv run wildcat-code-agent
```

Type a request at the `You:` prompt. Type `exit` or `quit` to leave. Press Ctrl+C during a reply to cancel it.

### Subcommands

| Command | What it does |
| --- | --- |
| `wildcat-code-agent` or `wildcat-code-agent chat` | the terminal chat (the REPL above) |
| `wildcat-code-agent serve --stdio` | runs the agent as an Agent Client Protocol (ACP) server on stdin and stdout, for editors and other programs; see [docs/protocol.md](docs/protocol.md) |
| `wildcat-code-agent serve --web` | runs the same server over a WebSocket on `127.0.0.1` with a token printed at start, for the web UI and local clients; see [Web server](#web-server-serve---web) |
| `wildcat-code-agent gui` | opens the web UI in a desktop window: the same chat, tool cards and approvals as the browser; see [Desktop window](#desktop-window-wildcat-code-agent-gui) |

With no subcommand, every flag is a chat flag, so existing commands keep working. `chat` and `serve` take the same settings flags (`--model`, `--host`, `--num-ctx`, `--cwd`, `--profile`, `--config`, `--no-plugins`). The subcommand must come first: `wildcat-code-agent serve --stdio --cwd X`. `wildcat-code-agent --cwd X serve --stdio` is refused with a message that says so. `wildcat-code-agent --help` and `wildcat-code-agent chat --help` list the subcommands.

Flags override the config file and the environment for one run:

```sh
uv run wildcat-code-agent --model qwen3-vl:8b --num-ctx 8192
uv run wildcat-code-agent chat --cwd C:\src\my-project --config C:\tmp\alt.toml
uv run wildcat-code-agent --profile minimal
```

### Serve mode (stdio)

`wildcat-code-agent serve --stdio` speaks JSON-RPC 2.0, one message per line, on stdin and stdout, with the ACP methods `initialize`, `session/new`, `session/prompt` and `session/cancel`. The agent sends `session/update` notifications while a turn runs and asks for approvals with `session/request_permission`. Logs and warnings go to stderr, so stdout carries only protocol lines. While the server runs, file descriptor 1 points at stderr too, so output from plugins and child processes (`os.system`, `subprocess`, direct writes) lands on stderr and cannot corrupt the protocol. The full message list, the permission flow, the error codes and the deviations from ACP are in [docs/protocol.md](docs/protocol.md). Session "allow always" answers are kept per session. MCP servers named in `session/new` are ignored; configure them in `config.toml`. A server keeps at most 64 sessions; each line is capped at 16 MiB. On shutdown (stdin closed or Ctrl+C) running turns get 10 s in total to stop; a turn still running after that is reported on stderr, and the MCP servers and plugins are closed.

```sh
uv run wildcat-code-agent serve --stdio --cwd C:\src\my-project
```

If Ollama is not running, you get `Error: Cannot reach Ollama at http://127.0.0.1:11434. Start it with ollama serve ...`. Start the server and send the request again. If the model is not installed, the error suggests `ollama pull qwen3-vl:8b`.

### Web server (`serve --web`)

`wildcat-code-agent serve --web` runs the same agent server as `--stdio`, over a WebSocket at `ws://127.0.0.1:PORT/ws`. Each WebSocket is one ACP session server, with the same messages, events, approvals and errors; the protocol is in [docs/protocol.md](docs/protocol.md#websocket-transport-serve---web).

```sh
uv run wildcat-code-agent serve --web                 # a free port, chosen by the system
uv run wildcat-code-agent serve --web --port 8765     # a fixed port
```

**Open the web UI.** Start the server and open the line it prints, `Open this link in a browser: http://127.0.0.1:PORT/#token=...`. The page is a chat with streamed replies, tool-call cards, and an approval dialog for each write or command, with the diff (added lines green, removed lines red) and the buttons the agent offers (allow once, allow always for this session, reject). The workspace is the `--cwd` folder. The token is in the link's fragment (after `#`), so the browser never sends it to the server; the page removes it from the address bar and keeps it in memory. Reloading the page needs the printed link again, because the token is new on every start. It was tested in Microsoft Edge; `tests/test_web_browser.py` drives it with Playwright and is skipped where Edge is not installed.

- **Address.** The server binds `127.0.0.1` only. There is no option for another address.
- **Token.** Each start prints a new random token, on stderr, in the line `WebSocket: ws://127.0.0.1:PORT/ws?token=...`. Pass it as the `token` query parameter, or as `Authorization: Bearer <token>`. A missing or wrong token is refused before the connection is accepted. Anyone who has the token can make the agent act as you, so keep it private; it changes on every start.
- **Origin.** A connection that sends an `Origin` header is accepted only from `http://127.0.0.1:PORT` or `http://localhost:PORT`. A connection with no `Origin` (a script or an editor extension) is accepted if the token is right.
- **Web UI.** `GET /` serves the page from `src/wildcat_code_agent/web/` (plain HTML, CSS and JavaScript, no build step). Only files of that folder are served; anything else, and any path with `..`, is a 404. The page has a Content-Security-Policy that allows scripts and styles from this server only. `GET /info` needs the token and returns the workspace folder.
- **Shutdown.** Ctrl+C stops the server. Running turns are cancelled with the same 10 s shared deadline as stdio, then the MCP servers and plugins are closed.
- **Port.** On Windows the port is opened for this process alone, so another program cannot take a fixed `--port` (it gets an error instead).
- **Extensions and webviews.** Their origins (`vscode-webview://`, `chrome-extension://`) are refused. Such a client sends the token as `Authorization: Bearer` and no `Origin`.
- **Build note (conda Python).** An exe built on a conda Python needs `ffi-8.dll` (the `_ctypes` library) next to it, in `_internal`. Without it `serve --web` fails at start-up with a missing DLL, because click (a dependency of uvicorn) imports `ctypes`. The release build (uv-managed Python) includes it; `packaging\smoke-exe.ps1` checks that `serve --web` starts.

### Web UI: models, settings and saved chats

The page in the browser also has:

- **Model picker** (header). The choices are the models Ollama has (`ollama list`). Choosing one switches the model of the current chat only; the next message uses it. If the model has no `tools` capability, the page shows a warning, as the terminal does. If Ollama is down, the picker holds the current model and says why.
- **Sessions** (a column on a wide screen; the Sessions button on a phone). Each chat is saved after its first message, so it is listed with its first message as the title. Choosing one reloads its history on the screen, and the next message continues it. "New chat" starts an empty one in the same folder. "Load more" shows older chats, 50 at a time.
- **Settings** (Settings button). The editor writes the per-user `config.toml` (or the `--config` file the server was started with). Whole numbers have upper limits (for example `num_ctx` up to 1048576 and `max_output_chars` up to 1000000; the file and the flags use the same limits). The Ollama address can be set here only on this machine (127.0.0.1, localhost or ::1); another address is edited by hand. It edits `model` (the default for new chats), `host`, `num_ctx`, `max_tool_rounds`, `max_output_chars`, `command_timeout_s`, `network_timeout_s` and `tool_profile`. Comments and the layout of the file are kept; a key the agent does not know is refused on load, so the editor never writes one. **The saved settings take effect when wildcat-code-agent starts again.** The model picker changes the current chat only.
- Not in the settings editor, on purpose: `network_enabled` (it turns on web_fetch), `workspace`, `plugin_dir`, `plugins_enabled`, `disabled_plugins`, `mcp_servers` and `test_command` (they run code or start programs). Edit those by hand in `config.toml`.

**Where chats are saved.** One JSON file per chat in `%APPDATA%\wildcat-code-agent\sessions\` (the per-user data folder; nothing is written into the install folder). The file holds the folder, the model, the title, the times and the messages the model sees (not the reasoning text). Writes are atomic. A file that cannot be read is skipped with a warning, so the rest of the list still shows. At most 200 chats are kept (the least recently used are deleted, with a warning), and a chat over 5 MiB is not saved. The environment variable `WILDCAT_CODE_AGENT_DATA_DIR` changes the data folder. A chat whose folder no longer exists is listed but cannot be opened, with a message that says why. The CLI (`wildcat-code-agent chat`) does not save chats. The protocol is in [docs/protocol.md](docs/protocol.md#saved-sessions).

### Desktop window (`wildcat-code-agent gui`)

`wildcat-code-agent gui` starts the web server in the same process and opens the same page in a window (pywebview, on the Microsoft Edge WebView2 control). Chat, tool cards, approvals and saved chats work as they do in the browser. Closing the window stops the server and the command exits with code 0.

```powershell
uv run wildcat-code-agent gui --cwd C:\src\my-project
```

- **What it needs:** the Microsoft Edge WebView2 runtime. Windows 11 has it, and most Windows 10 installs have it through Edge. If the window does not open, install the runtime from Microsoft (search for "WebView2 runtime"). The error says so.
- **Flags:** the same settings flags as `chat` (`--model`, `--host`, `--num-ctx`, `--cwd`, `--profile`, `--config`, `--no-plugins`). The workspace defaults to the current folder.
- **The address:** the window opens the page at `http://127.0.0.1:PORT/#token=...` for one run. The port is free at each start and the token is new on each start. Neither is printed, because the window is the way in. The page itself is served without the token, but `/info` and the WebSocket refuse anything that lacks it.
- **Installed:** the Start menu entry **wildcat-code-agent** runs `wildcat-code-agent-gui.exe` from `C:\Program Files\wildcat-code-agent\`, with no console window. A Start menu shortcut starts in the program folder, so the window's workspace is your Documents folder instead, and a relative `--cwd` or `--config` given to that exe is resolved against Documents, not the program folder. To work in another folder, edit the shortcut and add `--cwd "C:\src\my-project"` after the target.
- **Closing:** closing the window, or pressing Ctrl+C in the console that started `gui`, stops the server and exits with code 0.
- **Privacy:** the window's WebView2 profile is private (pywebview's `private_mode`, set explicitly): cookies and local storage are not kept between runs. The profile folder is in a temporary location, and a normal close clears it. If the process is killed, a temporary profile folder can remain in `%TEMP%`; it is safe to delete.
- **Problems:** from a console, the messages go to the console. The windowed program writes its messages to `%APPDATA%\wildcat-code-agent\gui.log`: each start appends a `--- wildcat-code-agent gui started ... ---` line first, and a log over 1 MB is moved to `gui.log.1` (one old copy is kept). It also shows a message box when it cannot open the window.

Status: tested on Windows 11 with Python 3.14 (pywebview 6.2.1, pythonnet 3.2.1); the frozen build was run on the same machine, and the installer compiles (it was not installed here). Only Windows is supported for the window; on other systems `wildcat-code-agent serve --web` and a browser do the same job.

### VS Code extension

The extension opens a chat panel in VS Code. The panel is the same web UI (the bundle in `src/wildcat_code_agent/web/`), and it talks to `wildcat-code-agent serve --stdio` through the extension host instead of a WebSocket. Approvals work the same way: a write shows the diff and waits for your answer. The extension does not contain the agent: it starts the app that is installed on the computer.

#### Install the extension (Windows, each account)

Needs the app installed first ([Install (Windows)](#install-windows)), and VS Code 1.146 or newer. The extension is the file `wildcat-code-agent-vscode-<version>.vsix`: take it from the GitHub release, or build it (see [Building the VS Code extension](#building-the-vs-code-extension)).

1. **Install the app** for all users, as above. Then **close VS Code and open it again**. A program keeps the PATH it started with, so a VS Code that was already open before the install cannot find `wildcat-code-agent` until it restarts.
2. **Install the extension in each Windows account** that will use it. Extensions belong to one account (they go in that account's `%USERPROFILE%\.vscode\extensions`), so an account that has never installed it needs this step, even though the app is shared. Sign in to the account, then run:

   ```powershell
   code --install-extension wildcat-code-agent-vscode-0.1.0.vsix
   ```

   `code` is VS Code's command line. The VS Code installer puts it on PATH when its "Add to PATH" option is ticked. If `code` is not found, run `code.cmd` by its full path: `%LOCALAPPDATA%\Programs\Microsoft VS Code\bin\code.cmd` for a per-user VS Code install, or `C:\Program Files\Microsoft VS Code\bin\code.cmd` for an all-users one. Or open the .vsix in VS Code and choose **Install**.
3. **Open a folder** in VS Code, then run **Wildcat Code Agent: Open chat** from the Command Palette (Ctrl+Shift+P). The panel works in the first folder of the workspace.

If VS Code says it cannot find `wildcat-code-agent`, the message has an **Open Settings** button that opens `wildcatCodeAgent.command`. The usual causes: the app is not installed; VS Code was not restarted after the install; or the setting points at a program that is not there.

#### Update, uninstall and the path override

- **Update:** in each account, run `code --install-extension` with the newer `.vsix`. VS Code replaces the old version. Reload the window if the panel was open.
- **Uninstall the extension:** in each account, run `code --uninstall-extension wildcat-code-agent.wildcat-code-agent-vscode`, or remove it from the Extensions view. Uninstalling the app does not remove the extension, and the extension does not remove the app.
- **Use another copy of the app:** the setting `wildcatCodeAgent.command` is an array. The default `["wildcat-code-agent"]` is found on PATH. To use the app from its install folder instead, set the full path in that account's user settings (**Preferences: Open User Settings (JSON)**). The default install folder is `C:\Program Files\wildcat-code-agent` (the installer's `{autopf}\wildcat-code-agent`), so the program is `C:\Program Files\wildcat-code-agent\wildcat-code-agent.exe`. In JSON, each backslash is written twice:

  ```json
  "wildcatCodeAgent.command": ["C:\\Program Files\\wildcat-code-agent\\wildcat-code-agent.exe"]
  ```

  Reload the window after a change. Each account has its own settings, so set it in each account that needs it.

#### Notes on the command

- The command is started without a shell (`shell: false`). Windows finds `wildcat-code-agent` on PATH as `wildcat-code-agent.exe`, but not a `.cmd` or `.bat` file, so the first item must be an `.exe` on PATH or a full path.
- Every account that runs the extension runs its own agent process, from the same program. Config and plugins are per account (see [Multiple Windows accounts](#multiple-windows-accounts)).

#### Use and features

- **Which agent runs:** the setting above. The extension appends `serve --stdio --cwd <the folder>`.
- **Folder:** the panel works in the first workspace folder. With no folder open, it says so and does not start the agent.
- **Logs:** the output channel **wildcat-code-agent** shows the command line and the agent's stderr.
- **Editor tools (T20):** in the panel, the agent's file and command tools run in the editor. `read_file` returns the text of an open file as it is in the editor, unsaved changes included. `write_file` and `edit_file` show the diff in the approval dialog, then change the open file as an edit, so **Ctrl+Z** (the editor's undo) reverts it while the file is open. A file the agent changes is saved when it had no unsaved changes of yours; if it had, the change stays unsaved and the agent is told to ask you to save. Undo after a save brings back the text in the editor, and the disk keeps the agent's text until you save. `run_command` and `run_tests` run in a VS Code terminal named **wildcat-code-agent**, and the output (stdout and stderr together) goes back to the agent. The terminal is a pseudoterminal, not a shell: it has no input, no colours and no shell profile. A browser or CLI session keeps the agent's own tools. Details: `docs/protocol.md`, "Client methods".
- **Editor context (T21):** select text and run **Wildcat Code Agent: Add Selection to Chat** (Ctrl+Alt+L, or the editor's right-click menu). The selection appears above the prompt as a chip with its file and lines; **×** removes it. The active editor's file is attached by default, as an **Open file** checkbox that you can untick. Type `@` in the prompt to search workspace files, then pick one with Enter or Tab. The agent reads these files through the editor, so unsaved changes are included, and it gets the editor's errors and warnings for the attached files. A file outside the workspace is not read from an `@` pick or from the open file; the agent says so and asks you if it needs the file (through `read_file`, with your approval). The browser and the CLI have no editor, so there the `@` search and the attachments are not offered. Details: `docs/protocol.md`, "Prompt content".

#### Building the VS Code extension

Needs Node 26 and npm. From `extensions\vscode`:

```powershell
cd extensions\vscode
npm ci                    # once: installs the TypeScript, VS Code type and vsce packages
npm run compile           # copies the web bundle to webview\ and compiles src\ to out\
npm test                  # the extension's own tests (no VS Code needed)
npm run test:e2e          # starts VS Code with the extension and checks the panel (set VSCODE_PATH to Code.exe)
npm run package           # compiles, then writes wildcat-code-agent-vscode-<version>.vsix in extensions\vscode
```

`npm run package` runs `vsce` (`@vscode/vsce`, pinned in `package.json`). The .vsix carries `out\src`, `webview\`, `media\`, `package.json` and the README. The sources, tests, source maps and `node_modules` are left out (`extensions/vscode/.vscodeignore`). The version is the one in `extensions/vscode/package.json`. `vsce` warns that there is no LICENSE file: the extension's license is not chosen yet.

To try the panel from the repo, open `extensions\vscode` in VS Code and press F5 (Run Extension). In the new window, open a folder and run **wildcat-code-agent: Open chat**. To run the agent from the repo instead, set `wildcatCodeAgent.command` in that window, for example `["uv", "run", "--project", "C:\\src\\wildcat-code-agent", "wildcat-code-agent"]`. Reload the window after a change.

## Configuration

Settings come from four places, highest priority first:

1. **Command-line flags** (`--model`, `--host`, `--num-ctx`, `--cwd`, `--profile`, `--config`).
2. **`OLLAMA_HOST`** environment variable. It sets the host only, and may omit `http://` as the Ollama CLI allows.
3. **The config file** (TOML), described below.
4. **Built-in defaults**.

**Config file location.** The file is `config.toml` in the per-user application config folder. On Windows that is `%APPDATA%\wildcat-code-agent\config.toml`, which for this account is `C:\Users\<you>\AppData\Roaming\wildcat-code-agent\config.toml`. The folder is not created for you; create the file yourself. A missing file means defaults. `--config PATH` reads a different file, and that file must exist.

Example:

```toml
# %APPDATA%\wildcat-code-agent\config.toml
model = "qwen3-vl:8b"
host = "http://127.0.0.1:11434"
num_ctx = 8192
max_tool_rounds = 10
max_output_chars = 8000
command_timeout_s = 60
test_command = "uv run pytest -q"
workspace = "C:\\src\\my-project"   # or a relative path, taken from the config file's folder
tool_profile = "coding"
network_enabled = false             # true offers web_fetch under the all profile
plugins_enabled = true              # false (or --no-plugins) loads no plugins
disabled_plugins = ["word_count"]   # plugins not to load, by file name (without .py) or entry point name
```

| Key | Flag | Default | Purpose |
| --- | --- | --- | --- |
| `model` | `--model` | `qwen3-vl:8b` | Ollama model name |
| `host` | `--host` (or `OLLAMA_HOST`) | `http://127.0.0.1:11434` | Ollama server URL |
| `num_ctx` | `--num-ctx` | `8192` | Context window in tokens, sent to Ollama with every request. Minimum 512. |
| `max_tool_rounds` | | `10` | Max model/tool rounds per user message |
| `max_output_chars` | | `8000` | Max characters of tool output sent to the model |
| `command_timeout_s` | | `60` | Timeout for shell commands and test runs, in seconds. On timeout the command and the processes it started are killed. It also limits MCP server start-up and each MCP call. |
| `test_command` | | (none) | Command that `run_tests` runs when the call gives none, run through cmd. Without it, `run_tests` uses `uv run pytest -q` when the workspace has a `pyproject.toml`, or `npm test` when it has a `package.json`. |
| `workspace` | `--cwd` | the current folder | The workspace folder. Relative file paths resolve against it, `run_command` starts in it, and paths outside it need approval (see Safety). `--cwd` also changes into it. |
| `tool_profile` | `--profile` | `coding` | Which tools the model is offered: `minimal`, `coding` or `all`. See Tool profiles. |
| `network_enabled` | | `false` | Lets the network tools (`web_fetch`) be offered. Off by default. |
| `network_timeout_s` | | `20` | Time limit for `web_fetch`, in seconds. It covers the whole fetch (redirects included) and each connect or read. A fetch that runs longer is stopped and the model is told. |
| `plugins_enabled` | `--no-plugins` | `true` | Loads the plugins (the tools folder and entry points). See Plugins. |
| `disabled_plugins` | | `[]` | Plugins not to load, by file stem or entry point name. |
| `plugin_dir` | | `<config folder>\tools` | The folder of plugin files. The app does not create it. A relative path is taken from the config file's folder. |

An unknown key, a wrong type or an out-of-range value stops the program at startup with a message that names the file or flag, and exit code 2. Example: `Error: C:\...\config.toml: num_ctx must be at least 512, got 100`.

**Why `num_ctx` is set.** Ollama's default context window is chosen from the GPU memory it finds. On the 8 GiB GPU this was tested on, the server log reports `default_num_ctx=4096`. A long chat would then be cut silently, so the agent always sends an explicit value. The 8192 default fits a file read and a few turns; raise it if your GPU has room (see the context warning in the output).

### Tool profiles and the network switch

A profile decides which tools are sent to the model and which the agent will run. A call to any other tool is refused with a message, without a question.

| Profile | Tools |
| --- | --- |
| `minimal` | the read tools: `read_file`, `list_dir`, `glob`, `grep`, `git_status`, `git_diff`, `git_log` |
| `coding` (default) | the read tools, plus `edit_file`, `write_file`, `git_commit`, `run_command`, `run_tests` |
| `all` | every tool, including `web_fetch` when `network_enabled = true` |

`web_fetch` is in the network tier. It is not offered unless `network_enabled = true` is set in the config, whatever the profile. Even then it asks every time, and "allow for this session" is never offered for it. If the model calls it while it is off, the answer is `Error: web_fetch is disabled; enable it in config`.

An unknown profile name is a config error (exit code 2), as is an unknown value for `network_enabled`.

Model notes: `qwen3-vl:8b` makes structured tool calls. `qwen2.5-coder:7b` prints its calls as JSON text; the agent turns those into real tool calls when the reply is only the call (see `core/toolcalls.py`). Replies that mix prose and a call are not converted.

### MCP servers

The agent can use tools from MCP servers. Each server is a program that speaks the Model Context Protocol over stdio. Add one per `[mcp_servers.<name>]` table in the config. Each server is started when the agent starts, its tools are listed, and they are offered to the model with the built-in tools. Only stdio servers are supported for now.

```toml
[mcp_servers.files]
command = "uvx"                 # the program to start
args = ["some-server"]          # optional
env = { KEY = "value" }         # optional, added to the environment the server gets (see Environment below)
cwd = "C:\\src\\my-project"     # optional, the server's start folder (a relative path is taken from the config file's folder)
risk = "exec"                   # optional, the tier for all its tools: read, write, exec or network (default exec)
enabled = true                  # optional; false leaves the server out

[mcp_servers.files.tools.some_tool]
risk = "read"                   # optional, the tier for this tool only
```

- **Names.** A tool is named `<server>__<tool>`, for example `files__some_tool`. Server names use letters, digits and hyphens. In a tool name, any character other than a letter, digit, `_` or `-` becomes `_`, and the name is cut to 64 characters. A tool whose name is already taken is skipped with a warning, and so is a tool with an argument named `context` (the agent uses that name itself).
- **Risk tiers.** The default is `exec`, so each call asks first. The question shows the server, the tool and the arguments as JSON. `read` tools run without asking. `write` and `exec` tools can be allowed for the session, per tool and per workspace, the same as `run_command`. `network` tools are off unless `network_enabled = true`, like `web_fetch`, and are never allowed for the session.
- **Profiles.** MCP tools are filtered by tier like the built-in tools. The `coding` profile offers `exec` tools and `minimal` offers only `read` tools. Set a server's tier to `read` to offer its tools under `minimal`.
- **Start-up and failures.** Servers start together. A server that does not start, or does not list its tools within `command_timeout_s`, prints a warning with the last lines it wrote to stderr, and the agent starts without it. A server that exits during a session makes its calls return an error result, and the agent keeps running. A call that runs past `command_timeout_s` is cancelled and reported as an error, and Ctrl+C cancels it. Servers are stopped when the REPL exits.
- **Results.** Text is passed to the model as it is. Images, audio, resource links and binary resources are shown as one short line each. An error reported by the server comes back as `Error: ...`. Output is capped by `max_output_chars`.
- **Environment.** A server does not inherit the whole environment. The MCP SDK passes only a safe subset: on Windows `APPDATA`, `HOMEDRIVE`, `HOMEPATH`, `LOCALAPPDATA`, `PATH`, `PATHEXT`, `PROCESSOR_ARCHITECTURE`, `SYSTEMDRIVE`, `SYSTEMROOT`, `TEMP`, `USERNAME`, `USERPROFILE`; elsewhere `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`. Anything else must be set in the server's `env` table, for example API keys, `VIRTUAL_ENV` or `PYTHONPATH`.
- **Limits.** `command_timeout_s` (60 s by default) is the one time limit for start-up and for each call. The approval preview is cut at 1500 characters; the `[Tool Call]` line shows the full arguments. Arguments are not checked against the schema by the agent; the server does that. Nested fields of an object are not sent to the model (see Schemas).
- **Not sandboxed.** An MCP tool runs in the server's own process, so the workspace path rules do not apply to it. The server decides what it can reach.
- **Display.** Names and text from a server that appear on the terminal (warnings, the approval title and preview, and the stderr shown for a server that did not start) have control characters shown as escapes such as `\x1b`, so a server cannot clear the screen or add a line to the approval. A tool description is sent to the model with control characters replaced by spaces.
- **Schemas.** Each property is passed to the model with only its type, items, description and enum, because the ollama client keeps no other keys. An `Optional` (`anyOf` with `null`) becomes its type, a `$ref` to a definition in the same schema is inlined one level (a cycle leaves only the description), and a title becomes the description when there is none. Nested fields of an object are not sent, so the model sees such a field as an object with its description.

The official `mcp` Python SDK (2.x) does the protocol work. It negotiates the protocol version with each server, and servers that only know the older `initialize` handshake also work.

### Plugins

Plugins add tools without changing the agent's code. They are loaded once at startup, after the built-in tools and before the MCP servers. There are two sources.

**1. The tools folder.** Put a `*.py` file in `%APPDATA%\wildcat-code-agent\tools\` (the per-user config folder plus `tools`; `plugin_dir` in the config changes it). The folder is not created for you. Only `*.py` files directly in it are loaded (not subfolders, and not files whose name starts with `_`). Each file is one plugin, named by its file name without `.py`. Its `@tool` functions are registered exactly like the built-in tools.

Adding a tool is one decorated function. This file is `%APPDATA%\wildcat-code-agent\tools\word_count.py`:

```python
from wildcat_code_agent.plugin import Risk, tool


@tool(risk=Risk.READ)
def count_words(text: str) -> str:
    """Count the words in a piece of text."""
    return f"{len(text.split())} words"
```

And a test for it. Keep a copy of the file in the repository (for example `tests/word_plugin/word_count.py`), so the test does not depend on the user's folder. The test loads that folder with `load_plugins`, as `tests/test_plugins.py` does:

```python
# tests/test_word_count.py
from pathlib import Path

from wildcat_code_agent.config import Settings
from wildcat_code_agent.tools.plugins import load_plugins
from wildcat_code_agent.tools.registry import REGISTRY

PLUGIN_FOLDER = Path(__file__).parent / "word_plugin"  # holds word_count.py


def test_count_words(tmp_path):
    runtime = load_plugins(Settings(plugin_dir=PLUGIN_FOLDER, workspace=tmp_path))
    try:
        # Only this plugin is checked: other installed plugins may have their own warnings.
        assert not [w for w in runtime.warnings if "word_count" in w]
        assert [t for p in runtime.loaded if p.name == "word_count" for t in p.tools] == ["count_words"]
        assert REGISTRY["count_words"].call({"text": "one two three"}) == "3 words"
    finally:
        runtime.close()  # removes the plugin's tools from the registry again
```

The test suite also has an autouse fixture (`tests/conftest.py`) that restores the registry after every test, so a plugin tool never leaks into another test.

**Helper files.** A file whose name starts with `_` is not a plugin, but a plugin can import it with a relative import, so plugins in the folder can share code:

```python
# %APPDATA%\wildcat-code-agent\tools\_text_utils.py  (not loaded as a plugin)
def shout(text: str) -> str:
    return text.upper()
```

```python
# %APPDATA%\wildcat-code-agent\tools\loud.py
from wildcat_code_agent.plugin import Risk, tool
from . import _text_utils


@tool(risk=Risk.READ)
def shout(text: str) -> str:
    """Upper-case a piece of text."""
    return _text_utils.shout(text)
```

Use relative imports (`from . import _helper`) and names that start with `_` for helpers. Absolute imports of a helper by its file name do not work. Each load gets its own `wildcat_code_agent_plugins` package, so two plugin folders in one process do not share helpers.

**2. Entry points.** A package can install plugins with the entry point group `wildcat_code_agent.tools`. The entry point's value names a module, and importing that module registers its `@tool` functions:

```toml
# pyproject.toml of the package that provides the tools
[project.entry-points."wildcat_code_agent.tools"]
my_tools = "my_package.tools"
```

Entry points are loaded sorted by name, after the folder. A module that was already imported by other code in this process is not run again, so its tools are not registered; the loader warns when a plugin registers no tools. An entry point that names a function or attribute (`module:function`) is not supported: it must name a module. It is refused before anything is imported, so it registers nothing and does not affect the module's other entry point.

**Public API.** Import from `wildcat_code_agent.plugin` only; the rest of the package is internal and may change.

- `tool(risk=..., ...)`: the same decorator as the built-in tools (see Adding a tool). `risk` is required.
- `Risk`: `READ`, `WRITE`, `EXEC`, `NETWORK`.
- `ToolContext`: passed as `context: ToolContext`; it has `cancel` (a `threading.Event` that is set when the turn is cancelled), `workspace`, and the limits.
- `ToolCancelled`: raise it from a long tool that stopped because `context.cancel` was set.
- `Preview`: returned by a `preview=` hook.

**Risk rule.** A plugin tool must declare its risk: `@tool(risk=Risk.READ)`. A tool without `risk` (or with a value that is not a tier) is not loaded, and the warning says so. It is never assumed to be safe. Profiles and approvals then apply as for the built-in tools: `read` runs without asking, `write`, `exec` and `network` ask, and `network` needs `network_enabled`. A plugin tool's path parameters (`paths=("path",)`) get the workspace check, so a path outside the workspace is asked about separately.

**Turning plugins off.** `plugins_enabled = false` in the config, or `--no-plugins` on the command line, loads nothing. `disabled_plugins = ["name"]` skips single plugins. The match is case-insensitive, and one name is checked against both the file names (without `.py`) and the entry point names, so a name given there disables a file plugin and an entry point that share it.

**Failures.** A plugin that fails to import, has a syntax error, raises an exception, or defines an invalid tool prints one warning that names the plugin and the error, and the agent starts without it. Every tool the failed plugin had registered is removed again, so a plugin is either fully loaded or not loaded at all. The other plugins still load. A plugin that loads but registers no tools prints a warning (`Warning: plugin 'x' registered no tools.`) and is left out of the loaded line. A plugin tool whose name is already used (by a built-in tool or by another plugin's tool) is skipped with a warning; a plugin can never replace an existing tool. At startup the CLI prints one line such as `[plugins] loaded: word_count (1 tool), my_tools (3 tools)` when at least one plugin loaded.

**Self-modification guard.** The agent cannot quietly plant code where it will be loaded from. Any `write_file` or `edit_file` whose resolved target is inside the plugin folder always asks, and "allow for this session" is never offered or used for it. This is the same rule that covers `.git` folders (see `sandbox.py`). Writes made by `run_command` always ask anyway, as before.

**Load order.** Built-in tools (registered when the package is imported), then the plugin folder in name order, then the entry points in name order, then the MCP servers. A name that is already taken when a plugin registers it is skipped, so the built-in and earlier tools keep their names.

**Security.** A plugin is Python code that runs with the agent's permissions, not a sandboxed tool. Put in the folder only code you would run yourself.

## Project layout

```
src/wildcat_code_agent/
  __init__.py          # main() entry point: subcommands chat, serve --stdio, serve --web, gui; parses flags, loads settings, changes into the workspace
  __main__.py          # python -m wildcat_code_agent
  frontends/server.py  # ACP over JSON-RPC lines (StdioServer: sessions, prompt turns on a worker thread, permission requests); serve --stdio; the start-up shared with --web
  frontends/web.py     # serve --web: FastAPI and uvicorn on 127.0.0.1; a WebSocket per connection running a StdioServer; token and Origin checks
  frontends/webassets.py # the web UI's static files: the folder, the path rules, the Content-Security-Policy
  sessions.py          # saved chats: one JSON file each in the per-user data folder (atomic writes, limits, corrupt-file handling)
  config_write.py      # the settings editor's allowlist and its write to config.toml (tomlkit: comments and the layout are kept)
  atomic.py            # atomic file writes (temp file, flush, os.replace) used by sessions and the settings editor
  web/                 # the web UI: index.html, style.css and js/ (transport.js, acp.js, chat.js, panels.js, main.js); no build step
  extensions/vscode/   # the VS Code extension (T19 to T22): the panel, the stdio relay, the editor tools, the .vsix packaging and the tests; see the VS Code section
  config.py            # Settings, defaults, the TOML file, OLLAMA_HOST, validation and precedence (tool profile names, network switch)
  core/loop.py         # agent loop: streams one user turn of model/tool rounds; emits events, asks the approver, honours cancel
  core/events.py       # event types (text_delta, thinking_delta, tool_call, tool_started, tool_result, approval_request, turn_done, error), Usage
  core/model.py        # Ollama error messages, startup probe of the model's capabilities
  core/toolcalls.py    # fallback parser: tool calls printed as JSON text become real calls
  permissions.py       # risk tiers, approval policy (read auto-allowed), Approver protocol
  sandbox.py           # resolves path arguments against the workspace; inside, outside or refused
  mcp_hub.py           # MCP client runtime: stdio servers on one background thread with its own asyncio loop
  tools/registry.py    # @tool decorator (risk, paths=), add_tool (explicit schema), JSON schema from type hints, Tool objects, ToolContext
  tools/mcp_tools.py   # MCP servers' tools: <server>__<tool> names, risk tiers from the config, results as text
  tools/plugins.py     # plugin loading: the tools folder and the wildcat_code_agent.tools entry points; roll back and warn per plugin
  plugin.py            # the public API for plugin authors: tool, Risk, ToolContext, ToolCancelled, Preview
  tools/__init__.py    # TOOLS: the registered tools (imports the built-ins)
  tools/builtin.py     # read_file (read), write_file (write), run_command and run_tests (exec; cmd by default, or PowerShell)
  tools/edit_tools.py  # edit_file (write): exact-match replace, line endings and BOM, unified diff for previews
  tools/git_write.py   # git_commit (write): stages and commits with repository code turned off; never remembered for the session
  tools/read_tools.py  # list_dir, glob, grep, git_status, git_diff, git_log (read; searches stay inside their base folder)
  tools/process.py     # runs a child process with a time limit and kills its whole process tree on timeout
  tools/web_tools.py   # web_fetch (network): GET over http/https, redirect and size caps, HTML to text
  tools/profiles.py    # tool profiles (minimal, coding, all) and the network switch: what is offered and allowed to run
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
  test_profiles.py     # tool profiles and the network switch: what the model is offered, refusals without asking, short descriptions
  test_web_fetch.py    # web_fetch with a mock transport: asks every time, URL refusals, size and redirect caps, HTML to text
  test_mcp_config.py   # [mcp_servers] validation: the example, defaults, bad names, keys and risk values
  test_mcp_tools.py    # MCP naming and sanitising, name collisions, reserved argument, descriptions, result text
  test_mcp_runtime.py  # real stdio servers in tests/mcp_servers: listing, risk tiers, calls, errors, timeout, cancel, exit, shutdown
  test_cli_mcp.py      # REPL with MCP servers: start-up warnings, argument preview, stop at exit and at Ctrl+C
  test_plugins.py      # plugin folder and entry points, failure isolation and rollback, name clashes, risk, switches, the plugin-folder write guard
  plugins/             # sample plugin files used by test_plugins.py (sample_plugin.py, entry_sample.py)
  test_frozen_exe.py   # the built wildcat-code-agent.exe: --help and serve --stdio initialize; skipped unless the exe is built
  test_release_workflow.py  # release.yml read as YAML: triggers, permissions, tag check, pinned actions
packaging/
  build.ps1            # builds dist\wildcat-code-agent\wildcat-code-agent.exe (uv sync --locked, then PyInstaller)
  wildcat-code-agent.spec     # PyInstaller spec: --onedir, console app, hidden imports and package metadata
  launcher.py          # the script PyInstaller freezes; calls the same main() as the wildcat-code-agent script
  smoke-exe.ps1        # smoke test of the built exe: --help, a serve --stdio run with a smoke plugin, standard-library imports
  smoke/smoke_stdlib.py  # the plugin smoke-exe.ps1 copies into a temp folder
  wildcat-code-agent.iss      # Inno Setup 6 script for the installer; build-installer.ps1 compiles it
  build-installer.ps1  # builds dist\installer\wildcat-code-agent-<version>-setup.exe
.github/workflows/ci.yml       # pytest on windows-latest, every push to main and every pull request
.github/workflows/release.yml  # build, test and package on windows-latest; publishes a GitHub Release for version tags
```

## Architecture

The core (`core/`, `tools/`, `permissions.py`, `config.py`) never prints or reads input. It reports through events and asks an `Approver`. The CLI is one frontend: `render(event)` prints events, and `TerminalApprover` asks `[APPROVE] ...? [y]es / [a]lways for this session / [n]o`, or `(y/n)` when the request does not offer `a`. A GUI or editor extension would supply its own sink and approver.

Settings are passed in explicitly: `run_turn(messages, client, settings=...)`. The loop sends `options={"num_ctx": ...}` with each chat call and gives tools a `ToolContext` (the command timeout and output limit) when they declare a `context: ToolContext` parameter. Tools never read a global setting.

Model text is sent as `text_delta` events while it streams, and reasoning as `thinking_delta`. For each tool call the events are `tool_call`, then for write/exec/network tools `approval_request`, then `tool_result`. An `approval_request` lists its answers in `options` (`allow_once`, `allow_always`, `reject_once`). `allow_always` appears only for a call that may be allowed for the session, and an answer of it counts as `allow_once` otherwise. A command's `preview` names its shell. A turn ends with `turn_done`, whose `stop_reason` is `end_turn`, `max_turn_requests` or `cancelled`, and whose `usage` gives the token counts Ollama reported. An Ollama failure ends the turn with an `error` event instead. If no approver is passed, risky tools are refused.

Cancelling: pass a `threading.Event` as `cancel=` to `run_turn`, or press Ctrl+C (the core treats `KeyboardInterrupt` as a cancel). The turn stops between stream chunks, before each round and before each tool call. Every tool call in history gets a tool message: calls that did not run get `"Cancelled by user."` as a failed result, and a reply cut off mid-stream is not added to history.

The fallback parser runs only when a reply has no structured tool calls. It accepts a reply that is nothing but JSON calls (bare, in a `json` fence, or in `<tool_call>` tags). The call name must be a registered tool, the arguments must bind to its signature, and any failure makes the whole reply plain text. Converted calls go through the same approval rules as structured ones. Limitation: the text is streamed to the screen before the end of the reply is known, so the raw JSON is visible above the `[Tool Call]` line.

Thinking text is kept in the assistant message in history, and it counts toward `num_ctx`.

## Adding a tool

A tool is one decorated function. A tool can live in the repository (below) or in a plugin file (see Plugins). Its docstring becomes the description the model sees, and type hints become the JSON schema:

```python
# src/wildcat_code_agent/tools/builtin.py (or any module imported by tools/__init__.py)
from typing import Annotated

from wildcat_code_agent.permissions import Risk
from wildcat_code_agent.tools.registry import tool


@tool(risk=Risk.READ)
def count_lines(path: Annotated[str, "File to count"]) -> str:
    """Count the lines in a file."""
    with open(path, encoding="utf-8") as f:
        return str(sum(1 for _ in f))
```

- `risk`: `Risk.READ` runs without asking. `WRITE`, `EXEC` and `NETWORK` ask the approver first. A `NETWORK` tool is offered only with `network_enabled = true` (see Tool profiles), and a tool that should never be remembered for the session sets `allow_always=False`.
- Keep the docstring to one or two short sentences: it is sent with every request, and small models lose track of long schemas.
- `paths=("path",)`: names the parameters that are file paths (each must be a `str`, or `str | None` when optional). The core resolves them against the workspace before the tool runs, asks separately for a path outside it, and passes the tool the resolved absolute path. Do not check paths inside the tool.
- For risky tools, add `approval_title=` (a function returning the question, for example `"Write 12 chars to 'a.txt'"`) and `preview=` (a function returning a `Preview` shown before approval). Both receive the tool's arguments by name. `denied_message=` is what the model receives when the user says no.
- A tool that needs a limit from the settings declares `context: ToolContext` as a parameter (from `wildcat_code_agent.tools.registry`). The core fills it in; the model never sees it. `ToolContext.command_timeout_s` and `ToolContext.max_output_chars` are the current values.
- Supported parameter types: `str`, `int`, `float`, `bool`, `list[T]`, `dict`, and `X | None` for optional parameters. Parameters without a default are required. Use `Annotated[str, "description"]` to describe a parameter.
- Tools must not `print` or call `input`. Return a string; raised exceptions go back to the model as an error message.
- Add a test next to `tests/test_registry.py` or `tests/test_approval.py` that checks the schema and, for risky tools, the approval flow.

## Building the Windows executable

`packaging\build.ps1` builds `wildcat-code-agent.exe` with PyInstaller, in `--onedir` mode. Run it from the repo root (or from anywhere). Close any running `wildcat-code-agent` that was started from the repo's venv (`.venv\Scripts\wildcat-code-agent.exe`) first: Windows locks it, and the build's `uv sync` then fails with "being used by another process".

```powershell
packaging\build.ps1
```

With Windows PowerShell 5.1 and a restrictive execution policy, run it with the policy bypassed for that one script (pwsh 7 runs the line above directly):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build.ps1
```

It runs `uv sync --locked`, then PyInstaller with `packaging\wildcat-code-agent.spec`. The output is the folder `dist\wildcat-code-agent\`: `wildcat-code-agent.exe` and an `_internal` folder beside it. Copy that whole folder to run the app elsewhere; the exe needs no Python to run. `build\` is PyInstaller's work folder and can be deleted. Both `build\` and `dist\` are git-ignored.

- **Two executables (T18):** `wildcat-code-agent.exe` (console: chat, `serve`, `gui` from a terminal) and `wildcat-code-agent-gui.exe` (windowed: `gui` only, for the Start menu), built from one spec into one folder (`MERGE`). Both share `_internal\`. The window needs the pywebview Windows backend: `pythonnet`, `clr_loader`, the WebView2 loader DLLs and `Microsoft.Web.WebView2`; PyInstaller's hooks for them are in `pyinstaller-hooks-contrib`. `packaging\smoke-exe.ps1` checks the subsystem of both exes and that the backend imports.
- **Size (T12, this machine):** 46.4 MiB in 212 files (48,661,837 bytes), after adding the standard-library modules below.
- **Python version:** PyInstaller 6.22.3 (a dev dependency) declares Python 3.14 support and builds on the project's Python 3.14, so `requires-python` is unchanged.
- **Tests against the exe:** `$env:WILDCAT_CODE_AGENT_EXE = "dist\wildcat-code-agent\wildcat-code-agent.exe"; uv run pytest tests\test_frozen_exe.py`. Without an exe, that file is skipped; a plain `uv run pytest` never builds anything.

Limitations of the built app:

- **Entry-point plugins are not loaded.** A package installed with a `wildcat_code_agent.tools` entry point cannot be added to the frozen app. The loader finds none and prints nothing. **File plugins still load:** a `*.py` file in the tools folder can `from wildcat_code_agent.plugin import tool` and use the standard-library modules bundled in the app. The build lists these extra ones in `hiddenimports` (`packaging\wildcat-code-agent.spec`): `sqlite3`, `xml.etree.ElementTree`, `xml.dom.minidom`, `unittest`, `csv`, `decimal` and `zipfile`. Other standard-library modules are not guaranteed: a module the build does not bundle fails with a clear per-plugin warning, and the agent runs on without that plugin. Pure-Python modules are sometimes in the bundle without being listed (`colorsys` loaded in the check below), so add a module to `hiddenimports` before relying on it. `tkinter` is excluded. A package that is not standard library (a plugin importing `rich`, for example) is refused with a `ModuleNotFoundError` warning in the same way.
- **Standard-library DLLs on the build machine.** The build machine's base Python is miniforge/conda (`C:\ProgramData\miniforge3`). Its DLLs for `_ctypes` (`ffi-8.dll`), `_bz2` (`libbz2.dll`), `_lzma` (`liblzma.dll`) and `_sqlite3` (`sqlite3.dll`) are in its `Library\bin` folder, which PyInstaller does not collect here (the build log reports `Library not found`). So on this build, plugins that import `ctypes`, `bz2`, `lzma` or `sqlite3` fail with a warning, even though `sqlite3` is in the list above. A build on a python.org or uv-managed Python (T14 CI) may differ; that is not verified.
- **The T18 build (this machine, conda):** `wildcat-code-agent-gui.exe` also needs `ffi-8.dll` (`ctypes`), and the smoke test needs `sqlite3.dll` (`sqlite3`), `libbz2.dll` and `liblzma.dll` (the optional `bz2` and `lzma`) copied from `C:\ProgramData\miniforge3\Library\bin` into `dist\wildcat-code-agent\_internal\`. Without the copies, the windowed exe stops at import, with no console to show why; it now writes the traceback to `gui.log` and shows a message box (`packaging\gui_launcher.py`).
- **`ctypes` is now required by the smoke test.** `serve --web` imports it through click (uvicorn's dependency), so an exe without `_ctypes` cannot start the web server. On the conda build machine, copy `ffi-8.dll` into the app's `_internal` folder; with that copy, the reviewer ran `serve --web` and `initialize` succeeded. Without it, `packaging\smoke-exe.ps1` fails at step 4.
- **MCP servers run the program they name.** `command` is looked up on PATH, so a Python MCP server needs a Python of its own; the exe does not provide one.
- **Ollama is not included.** It must run as before (see Requirements).
- **Not signed.** Windows SmartScreen and Defender may warn about an unsigned executable.
- **State stays per user.** Config, plugins and temp files go to the per-user folders (`%APPDATA%\wildcat-code-agent`, the system temp folder for MCP stderr). Nothing is written next to the exe.

## Building the installer

`packaging\build-installer.ps1` makes `dist\installer\wildcat-code-agent-<version>-setup.exe` from the T12 build. It builds `dist\wildcat-code-agent\` first only if that exe is missing (`-Rebuild` forces it), then compiles `packaging\wildcat-code-agent.iss` with Inno Setup 6. The version comes from `pyproject.toml`. When it reuses an existing `dist\wildcat-code-agent\`, it prints the exe's build time and warns if any file under `src\` is newer; run it with `-Rebuild` in that case.

Inno Setup 6 is needed (`ISCC.exe`). The script looks for it in `%ProgramFiles(x86)%\Inno Setup 6\`, `%ProgramFiles%\Inno Setup 6\`, `%LOCALAPPDATA%\Programs\Inno Setup 6\` and on PATH. If it is not found, the script stops and says how to install it:

```powershell
winget install --id JRSoftware.InnoSetup -e
```

```powershell
packaging\build-installer.ps1
```

## Releasing

A release is a version tag. `.github/workflows/release.yml` runs on `windows-latest`, builds `wildcat-code-agent.exe` and the installer, and attaches them to a GitHub Release.

1. Set `version` in `pyproject.toml` (for example `0.2.0`) and merge it to `main`.
2. Tag that commit as `v` plus the version, and push the tag:
   ```sh
   git tag v0.2.0
   git push origin v0.2.0
   ```
   The tag must equal the `pyproject.toml` version exactly (`v0.2.0` for `0.2.0`). If it does not, the build stops at the "Read the version and check the tag" step and nothing is published.
3. The workflow runs the tests, builds `dist\wildcat-code-agent\`, runs the frozen-exe tests and `packaging\smoke-exe.ps1`, builds the installer, and then creates the release.

The release has four files:

- `wildcat-code-agent-<version>-setup.exe`: the machine-wide installer (see "Building the installer").
- `wildcat-code-agent-<version>-win-x64.zip`: the app folder, unzipped to `wildcat-code-agent-<version>-win-x64\` with `wildcat-code-agent.exe` and `_internal\`. Nothing is installed; run the exe from any folder.
- `wildcat-code-agent-vscode-<version>.vsix`: the VS Code extension, built by `npm run package` in `extensions/vscode` (the version is the extension's, in its `package.json`). Install it in each account (see "VS Code extension").
- `SHA256SUMS.txt`: the SHA-256 of all three files, in `sha256sum` format. To check a download in Git Bash, run `sha256sum -c SHA256SUMS.txt` in the folder that holds the files. In PowerShell, compare `Get-FileHash <file> -Algorithm SHA256` with the line for that file.

A tag with a suffix, such as `v0.2.0-rc1`, becomes a pre-release. Its `pyproject.toml` version must be the same suffixed string.

**Dry runs (no release, no tag):**

- Open a pull request that changes `packaging/**`, `.github/workflows/release.yml`, `pyproject.toml`, `uv.lock` or `extensions/vscode/**`. The workflow builds, tests and uploads the files as the run's artifact `wildcat-code-agent-release-<version>` (kept 14 days). The tag check is skipped.
- Or run the workflow by hand: Actions, Release, Run workflow. It does the same and never publishes, even if the branch selected is a tag.

**Build details:**

- Python is a uv-managed Python 3.14 (`UV_PYTHON_PREFERENCE=only-managed`), installed by `uv python install 3.14`. The runner's own Python and conda are not used.
- Inno Setup 6.7.1 is installed with Chocolatey only when the runner does not already have `ISCC.exe` in one of the places `build-installer.ps1` looks.
- Nothing is signed. Windows SmartScreen may warn about the installer and the exe.

**If a tag build fails:** a failure before the publish job leaves no release. Fix the problem, then either push a new version or delete the tag (`git push --delete origin v0.2.0`, `git tag -d v0.2.0`) and push it again. If the publish job created the release, delete the release on GitHub first.

## Development

```sh
uv sync          # installs runtime and dev dependencies (pytest, pyinstaller)
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
- **`web_fetch` is the only network tool.** It is off unless `network_enabled = true` is set in the config. When on, it asks every time (no "allow for this session"). It accepts only `http` and `https` URLs without a username or password, makes one plain `GET` per page with no cookies or credentials, and sends the User-Agent `wildcat-code-agent/<version>`. Redirects are followed by hand (at most 5, each target checked), the body is read up to 2 MB, and `network_timeout_s` is a limit on the whole fetch, redirects included, as well as on each connect or read. A redirect from a public host to a local or private address (`localhost`, a loopback, private, link-local or similar IP address, or a name with no dot) is refused; the model is told to call `web_fetch` with that URL, so the user is asked. Such a fetch is labelled "local/private address" in the approval, and the approval shows a note. Proxy settings from the environment are used, except for `localhost` and loopback addresses, which are always reached directly. Host names are not resolved, so a public name that points at a private address is not caught. The page's text reaches the model, so a fetched page can try to steer it: read the URL before answering `y`.
- **MCP tools** run in their server's own process, so the workspace and path rules do not cover them. Each call asks first (exec tier by default), and its preview shows the server, the tool and the arguments. Only the server's tier decides whether it asks: a server or tool set to `read` runs without a question, so set `read` only for servers you trust.
- **Not detected**: a hard link inside the workspace that points at a file outside it looks like an inside path. A file swapped for a link after the check is a race that is not closed.
Windows path cases are covered by `tests/test_sandbox.py`: case and drive-letter case, `C:foo`, `\foo`, UNC, `\\?\` and `\\?\UNC\`, 8.3 short names, junctions (created with `mklink /J` in the test; skipped with a reason if that is not allowed), prefix tricks such as `work-evil` next to `work`, and symlinks (skipped here: Windows refused to create one without Developer Mode or admin).

You still need to read each prompt. The sandbox limits where file tools go; it does not judge what a command or a file's content will do.
