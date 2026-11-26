# Wildcat Code Agent for VS Code

Chat with the Wildcat Code Agent coding assistant in a VS Code panel. The agent runs on your machine with a local
Ollama model, reads and edits files in your workspace, and asks before it writes or runs anything. A write shows a
diff in the approval dialog, and an edit it makes can be undone with Ctrl+Z.

This extension does not contain the agent. It runs `wildcat-code-agent serve --stdio` from the Windows app, which
must be installed first.

## Requirements

- Windows 10 or 11, 64-bit.
- The **Wildcat Code Agent** app, installed for all users (the installer puts `wildcat-code-agent.exe` on the machine
  PATH). The extension finds it through PATH by default.
- Ollama running with a model that supports tool calls.

## Install

The extension is a `.vsix` file. Install it once for each Windows account that uses it (extensions belong to the
account, not to the computer):

```powershell
code --install-extension wildcat-code-agent-vscode-0.1.0.vsix
```

Run that in each account, after signing in to it. Use the version number of the file you downloaded.

If VS Code was already open when the app was installed, close it and open it again first: a running program keeps
the PATH it started with.

## Use

Open a folder, then run **Wildcat Code Agent: Open chat** from the Command Palette (Ctrl+Shift+P). The panel works in the
first folder of the workspace. Select text and run **Wildcat Code Agent: Add Selection to Chat** (Ctrl+Alt+L) to
send it with your next message.

If VS Code says it cannot find `wildcat-code-agent`, the app is not on PATH for this session. The error has an
**Open Settings** button that opens `wildcatCodeAgent.command`.

## Settings

- `wildcatCodeAgent.command`: the command that starts the agent. It is a list: the first item is the program and
  the rest are its arguments. The default is `["wildcat-code-agent"]`, which must be an `.exe` on PATH. `["wca"]`
  runs the same program under its short name (`wca.exe`, also installed on PATH); use it if you prefer the short
  name, since the default stays the long one. To use a program that is not on PATH, give its full path. Reload the
  window after a change:

  ```json
  "wildcatCodeAgent.command": ["C:\Program Files\wildcat-code-agent\wildcat-code-agent.exe"]
  ```

## Update and uninstall

- Update: install the new `.vsix` with the same `code --install-extension` command. VS Code replaces the old version.
- Uninstall: `code --uninstall-extension wildcat-code-agent.wildcat-code-agent-vscode`, or remove it from the
  Extensions view.

Full install notes for several accounts, and the app's own setup, are in the project's README:
https://github.com/geocarlos/wildcat-code-agent
