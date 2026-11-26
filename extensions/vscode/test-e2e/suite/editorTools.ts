// The T20 host answers inside a real VS Code: a read sees the unsaved buffer, a write is an undoable edit that the
// `undo` command reverts, a new file is created, and a command runs in a terminal with its output captured. The
// handlers are the ones extension.ts uses (src/editorTools.ts), called directly here, with no agent process.
import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import { EditorTools } from "../../src/editorTools";

const SESSION = "e2e-session";

// The terminals the host opened for the agent's commands (named "wildcat-code-agent", see editorTools.ts).
function agentTerminals(): vscode.Terminal[] {
  return vscode.window.terminals.filter((terminal) => terminal.name === "wildcat-code-agent");
}

async function waitFor(check: () => boolean, what: string, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!check()) {
    if (Date.now() > deadline) {
      throw new Error(`timed out waiting for ${what}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}

async function withTimeout<T>(work: Promise<T>, ms: number, what: string): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  const expired = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`timed out waiting for ${what}`)), ms);
  });
  try {
    return await Promise.race([work, expired]);
  } finally {
    clearTimeout(timer);
  }
}

async function showFile(file: string): Promise<vscode.TextDocument> {
  const document = await vscode.workspace.openTextDocument(file);
  await vscode.window.showTextDocument(document, { preview: false });
  return document;
}

export async function runEditorTools(): Promise<void> {
  const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  assert.ok(folder, "the test workspace is open");
  const tools = new EditorTools();
  try {
    // 1. A read returns the unsaved buffer, not the disk, and honours line and limit.
    const hello = path.join(folder, "hello.txt");
    const doc = await showFile(hello);
    const editor = vscode.window.activeTextEditor;
    assert.ok(editor, "hello.txt is the active editor");
    await editor.edit((builder) => builder.insert(new vscode.Position(0, 0), "unsaved "));
    assert.ok(doc.isDirty, "the buffer has unsaved changes");
    const read = (await tools.handle("fs/read_text_file", { sessionId: SESSION, path: hello })) as { content: string };
    assert.equal(read.content, "unsaved Hello world\n");
    const slice = (await tools.handle("fs/read_text_file", { sessionId: SESSION, path: hello, line: 1, limit: 1 })) as {
      content: string;
    };
    assert.equal(slice.content, "unsaved Hello world\n");

    // 2. A write over a document with the user's own unsaved edits is applied, is not saved (the user's edits are
    //    theirs to save), leaves the disk as it was, and Ctrl+Z (the undo command) brings the buffer back.
    const written = (await tools.handle("fs/write_text_file", {
      sessionId: SESSION,
      path: hello,
      content: "Written by the agent\n",
    })) as { _meta: { saved: boolean } };
    assert.equal(written._meta.saved, false);
    assert.equal(doc.getText(), "Written by the agent\n");
    assert.equal(fs.readFileSync(hello, "utf8"), "Hello world\n", "the disk is not written");
    await vscode.commands.executeCommand("undo");
    assert.equal(doc.getText(), "unsaved Hello world\n", "undo restores the buffer before the agent's edit");
    await vscode.commands.executeCommand("workbench.action.revertAndCloseActiveEditor");

    // 3. A clean document: the write is saved, and undo restores the text the editor had before the write.
    const other = path.join(folder, "other.txt");
    fs.writeFileSync(other, "one\n", "utf8");
    const otherDoc = await showFile(other);
    const saved = (await tools.handle("fs/write_text_file", { sessionId: SESSION, path: other, content: "two\n" })) as {
      _meta: { saved: boolean };
    };
    assert.equal(saved._meta.saved, true);
    assert.equal(fs.readFileSync(other, "utf8"), "two\n");
    await vscode.commands.executeCommand("undo");
    assert.equal(otherDoc.getText(), "one\n");
    await vscode.commands.executeCommand("workbench.action.revertAndCloseActiveEditor");

    // 4. A new file is created on disk.
    const created = path.join(folder, "made", "new.txt");
    const made = (await tools.handle("fs/write_text_file", { sessionId: SESSION, path: created, content: "fresh\n" })) as {
      _meta: { saved: boolean };
    };
    assert.equal(made._meta.saved, true);
    // A new file takes the editor's default line ending (CRLF on Windows), as the core's write_file does on the disk.
    assert.equal(fs.readFileSync(created, "utf8").replace(/\r\n/g, "\n"), "fresh\n");

    // 5. A missing file is the not-found error.
    await assert.rejects(
      tools.handle("fs/read_text_file", { sessionId: SESSION, path: path.join(folder, "nope.txt") }),
      (error: { code?: number }) => error.code === -32002,
    );

    // 6. A command runs in a terminal; its output and exit code are kept until the terminal is released.
    const { terminalId } = (await tools.handle("terminal/create", {
      sessionId: SESSION,
      command: process.execPath,
      args: ["-e", "console.log('from the terminal')"],
      cwd: folder,
    })) as { terminalId: string };
    const exit = (await tools.handle("terminal/wait_for_exit", { sessionId: SESSION, terminalId })) as {
      exitCode: number | null;
    };
    assert.equal(exit.exitCode, 0);
    const output = (await tools.handle("terminal/output", { sessionId: SESSION, terminalId })) as {
      output: string;
      exitStatus: { exitCode: number | null } | null;
    };
    assert.match(output.output, /from the terminal/);
    assert.equal(output.exitStatus?.exitCode, 0);
    assert.equal(await tools.handle("terminal/release", { sessionId: SESSION, terminalId }), null);
    // The released command's terminal stays open, with its output, until the next command starts (step 7 starts one).
    assert.ok(agentTerminals().length >= 1, "the released terminal is still open");

    // 7. A running command is killed: kill ends it, and waitForExit resolves, not after the command's own 60 s. Its
    //    terminal stays open after release; the next command closes the previous terminal.
    const long = (await tools.handle("terminal/create", {
      sessionId: SESSION,
      command: process.execPath,
      args: ["-e", "setTimeout(() => {}, 60000);"],
      cwd: folder,
    })) as { terminalId: string };
    await tools.handle("terminal/kill", { sessionId: SESSION, terminalId: long.terminalId });
    const killed = (await withTimeout(
      tools.handle("terminal/wait_for_exit", { sessionId: SESSION, terminalId: long.terminalId }),
      15_000,
      "the killed command to end",
    )) as { exitCode: number | null; signal: string | null };
    assert.notDeepEqual(killed, { exitCode: 0, signal: null }, "the command was ended, not finished");
    await tools.handle("terminal/release", { sessionId: SESSION, terminalId: long.terminalId });
    const next = (await tools.handle("terminal/create", {
      sessionId: SESSION,
      command: process.execPath,
      args: ["-e", "console.log('second')"],
      cwd: folder,
    })) as { terminalId: string };
    await tools.handle("terminal/release", { sessionId: SESSION, terminalId: next.terminalId });
    await waitFor(() => agentTerminals().length === 1, "the previous terminal to close");
  } finally {
    tools.dispose();
  }
}
