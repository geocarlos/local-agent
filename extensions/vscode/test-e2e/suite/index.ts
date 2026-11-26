// Runs inside the VS Code extension host (started by ../runTest.ts). Checks that the extension activates, that the
// command opens one panel (a second call reveals it), and that closing the panel releases it.
// The chat itself (typing a prompt in the panel) is not driven here: the webview has no API for the host to type
// into it. That part is covered by tests/test_webview_bundle.py (the bundle in Edge against a real agent).
import * as assert from "node:assert/strict";
import * as vscode from "vscode";
import { runEditorTools } from "./editorTools";

const EXTENSION_ID = "wildcat-code-agent.wildcat-code-agent-vscode";
const PANEL_TITLE = "wildcat-code-agent";

function panelTabs(): vscode.Tab[] {
  return vscode.window.tabGroups.all
    .flatMap((group) => group.tabs)
    .filter((tab) => tab.label === PANEL_TITLE);
}

async function waitFor(check: () => boolean, what: string, timeoutMs = 15000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (check()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`timed out waiting for ${what}`);
}

export async function run(): Promise<void> {
  const extension = vscode.extensions.getExtension(EXTENSION_ID);
  assert.ok(extension, `the extension ${EXTENSION_ID} is loaded`);
  await extension.activate();

  const commands = await vscode.commands.getCommands(true);
  assert.ok(commands.includes("wildcatCodeAgent.openChat"), "the openChat command is registered");

  await vscode.commands.executeCommand("wildcatCodeAgent.openChat");
  await waitFor(() => panelTabs().length === 1, "the chat panel to open");

  // A second call reveals the same panel instead of opening another one.
  await vscode.commands.executeCommand("wildcatCodeAgent.openChat");
  await new Promise((resolve) => setTimeout(resolve, 500));
  assert.equal(panelTabs().length, 1, "one chat panel");

  // Closing the panel disposes it; the next call opens a new one.
  await vscode.window.tabGroups.close(panelTabs()[0]);
  await waitFor(() => panelTabs().length === 0, "the chat panel to close");
  await vscode.commands.executeCommand("wildcatCodeAgent.openChat");
  await waitFor(() => panelTabs().length === 1, "the chat panel to open again");
  await vscode.window.tabGroups.close(panelTabs()[0]);
  await waitFor(() => panelTabs().length === 0, "the second chat panel to close");

  await runEditorTools();
}
