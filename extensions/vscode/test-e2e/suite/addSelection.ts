// Add Selection to Chat (T21), in the real VS Code: the command builds the attachment from the active editor's
// selection, and returns it. Nothing selected adds nothing. The webview is not driven (see index.ts); the attachment
// the host builds is what the chat receives.
import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

interface SelectionResult {
  kind: string;
  uri: string;
  name: string;
  startLine: number;
  endLine: number;
  text: string;
}

export async function runAddSelection(): Promise<void> {
  const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  assert.ok(folder, "the test workspace is open");
  const commands = await vscode.commands.getCommands(true);
  assert.ok(commands.includes("wildcatCodeAgent.addSelection"), "the addSelection command is registered");

  const file = path.join(folder, "context-check.ts");
  fs.writeFileSync(file, "const alpha = 1;\nconst beta = 2;\n", "utf8");
  try {
    const doc = await vscode.workspace.openTextDocument(file);
    const editor = await vscode.window.showTextDocument(doc);

    // Nothing selected: the command adds nothing.
    editor.selection = new vscode.Selection(0, 0, 0, 0);
    const none = await vscode.commands.executeCommand("wildcatCodeAgent.addSelection");
    assert.equal(none, undefined, "an empty selection adds nothing");

    // "beta" on line 2: one line, 1-based, with the text as the editor has it.
    editor.selection = new vscode.Selection(1, 6, 1, 10);
    const added = (await vscode.commands.executeCommand("wildcatCodeAgent.addSelection")) as SelectionResult;
    assert.equal(added.kind, "selection");
    assert.equal(added.text, "beta");
    assert.equal(added.startLine, 2);
    assert.equal(added.endLine, 2);
    assert.equal(added.uri, doc.uri.toString());
    assert.equal(added.name, "context-check.ts");

    // Two lines: the range is both lines.
    editor.selection = new vscode.Selection(0, 0, 1, 10);
    const lines = (await vscode.commands.executeCommand("wildcatCodeAgent.addSelection")) as SelectionResult;
    assert.equal(lines.startLine, 1);
    assert.equal(lines.endLine, 2);
    assert.equal(lines.text, "const alpha = 1;\nconst beta");
  } finally {
    await vscode.commands.executeCommand("workbench.action.closeAllEditors");
    for (const group of vscode.window.tabGroups.all) {
      for (const tab of group.tabs) {
        if (tab.label === "wildcat-code-agent") {
          await vscode.window.tabGroups.close(tab);
        }
      }
    }
    fs.rmSync(file, { force: true });
  }
}
