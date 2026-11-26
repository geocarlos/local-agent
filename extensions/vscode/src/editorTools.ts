// The host's answers to the agent's fs/* and terminal/* requests (T20), with VS Code. Files: the open document is the
// truth (unsaved text included); a write is a WorkspaceEdit, so it is on the editor's undo stack. Commands: each runs
// in a VS Code terminal (a pseudoterminal that shows the output) and its output is kept for terminal/output.
// The parameter checks and the reply shape are in clientRouter.ts; the slicing is in lineSlice.ts.

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as vscode from "vscode";
import { CommandExit, CommandRun } from "./commandRunner";
import {
  absolutePathParam,
  ClientMethodError,
  INTERNAL_ERROR,
  INVALID_PARAMS,
  METHOD_NOT_FOUND,
  optionalIntParam,
  recordParams,
  RESOURCE_NOT_FOUND,
  stringParam,
} from "./clientRouter";
import { sliceLines } from "./lineSlice";

// Characters of output kept per command. The start is dropped beyond this, and terminal/output says so.
export const OUTPUT_LIMIT = 1_000_000;

interface TerminalEntry {
  run: CommandRun;
  terminal: vscode.Terminal;
  writer: vscode.EventEmitter<string>;
  closed: boolean;
}

export class EditorTools {
  private readonly terminals = new Map<string, TerminalEntry>();
  private nextTerminal = 1;
  private disposed = false;

  // Answers one request. Throws ClientMethodError for an error the agent should see.
  async handle(method: string, params: unknown): Promise<unknown> {
    if (this.disposed) {
      throw new ClientMethodError(INTERNAL_ERROR, "The editor panel is closed.");
    }
    switch (method) {
      case "fs/read_text_file":
        return this.readTextFile(params);
      case "fs/write_text_file":
        return this.writeTextFile(params);
      case "terminal/create":
        return this.createTerminal(params);
      case "terminal/output":
        return this.terminalOutput(params);
      case "terminal/wait_for_exit":
        return this.waitForExit(params);
      case "terminal/kill":
        return this.kill(params);
      case "terminal/release":
        return this.release(params);
      default:
        throw new ClientMethodError(METHOD_NOT_FOUND, `Method not found: ${method}`);
    }
  }

  // Ends every command and closes its terminal (the panel closed, or the extension stopped).
  dispose(): void {
    this.disposed = true;
    for (const id of [...this.terminals.keys()]) {
      this.releaseEntry(id);
    }
  }

  // --- files ---

  private async readTextFile(params: unknown): Promise<{ content: string }> {
    const p = recordParams(params);
    const file = absolutePathParam(p, "path");
    const line = optionalIntParam(p, "line", 1);
    const limit = optionalIntParam(p, "limit", 0);
    const open = openDocument(file);
    const text = open ? open.getText() : await readDisk(file);
    return { content: sliceLines(text, line, limit) };
  }

  // Replaces the file's text in the editor. The change is saved when the document was clean before it, so the disk
  // matches what the agent was told; a document with the user's own unsaved edits is left unsaved. The answer's
  // _meta.saved says which, and the agent tells the model.
  private async writeTextFile(params: unknown): Promise<{ _meta: { saved: boolean } }> {
    const p = recordParams(params);
    const file = absolutePathParam(p, "path");
    const content = stringParam(p, "content");
    const document = openDocument(file) ?? (await openIfExists(file));
    if (!document) {
      return { _meta: { saved: await createFile(file, content) } };
    }
    const wasDirty = document.isDirty;
    if (normalizeEol(document.getText()) !== normalizeEol(content)) {
      const edit = new vscode.WorkspaceEdit();
      edit.replace(document.uri, fullRange(document), content);
      if (!(await vscode.workspace.applyEdit(edit))) {
        throw new ClientMethodError(INTERNAL_ERROR, "The editor did not apply the change.");
      }
    }
    await vscode.window.showTextDocument(document, { preview: false, preserveFocus: false });
    let saved: boolean;
    if (wasDirty) {
      saved = false; // the user's unsaved edits are theirs to save
    } else {
      saved = document.isDirty ? await document.save() : true;
    }
    return { _meta: { saved } };
  }

  // --- terminals ---

  private createTerminal(params: unknown): { terminalId: string } {
    const p = recordParams(params);
    const command = stringParam(p, "command");
    if (command.trim() === "") {
      throw new ClientMethodError(INVALID_PARAMS, "Invalid params: command is empty.");
    }
    const args = p.args === undefined || p.args === null ? [] : stringArray(p.args, "args");
    const cwd = p.cwd === undefined || p.cwd === null ? os.homedir() : absolutePathParam(p, "cwd");
    const byteLimit = optionalIntParam(p, "outputByteLimit", 1);
    const writer = new vscode.EventEmitter<string>();
    const run = new CommandRun(command, args, {
      cwd,
      outputLimit: Math.min(byteLimit ?? OUTPUT_LIMIT, OUTPUT_LIMIT),
      onData: (text) => writer.fire(toTerminalLines(text)),
    });
    try {
      run.start();
    } catch (error) {
      writer.dispose();
      throw new ClientMethodError(INTERNAL_ERROR, `Cannot run the command: ${errorText(error)}`);
    }
    const pty: vscode.Pseudoterminal = {
      onDidWrite: writer.event,
      open: () => writer.fire(`$ ${[command, ...args].join(" ")}\r\n`),
      close: () => run.kill(),
    };
    const terminal = vscode.window.createTerminal({ name: "local-agent", pty });
    terminal.show(true); // shown, without taking the focus from the chat
    const id = `t${this.nextTerminal++}`;
    const entry: TerminalEntry = { run, terminal, writer, closed: false };
    this.terminals.set(id, entry);
    void run.waitForExit().then((exit) => {
      if (!entry.closed) {
        writer.fire(`\r\n[${describeExit(exit)}]\r\n`);
      }
    });
    return { terminalId: id };
  }

  private terminalOutput(params: unknown): { output: string; truncated: boolean; exitStatus: CommandExit | null } {
    const { run } = this.terminalOf(params);
    return { output: run.output, truncated: run.truncated, exitStatus: run.status };
  }

  private async waitForExit(params: unknown): Promise<CommandExit> {
    const { run } = this.terminalOf(params);
    return run.waitForExit();
  }

  private kill(params: unknown): null {
    this.terminalOf(params).run.kill();
    return null;
  }

  // Releasing an unknown terminal is not an error: the agent may release twice.
  private release(params: unknown): null {
    const id = stringParam(recordParams(params), "terminalId");
    this.releaseEntry(id);
    return null;
  }

  private terminalOf(params: unknown): TerminalEntry {
    const id = stringParam(recordParams(params), "terminalId");
    const entry = this.terminals.get(id);
    if (!entry) {
      throw new ClientMethodError(RESOURCE_NOT_FOUND, `Terminal not found: ${id}`);
    }
    return entry;
  }

  private releaseEntry(id: string): void {
    const entry = this.terminals.get(id);
    if (!entry) {
      return;
    }
    this.terminals.delete(id);
    entry.closed = true;
    entry.run.kill();
    entry.writer.dispose();
    entry.terminal.dispose();
  }
}

// --- helpers ---

// The open document for a file path, or undefined. Paths are compared without case on Windows.
function openDocument(file: string): vscode.TextDocument | undefined {
  return vscode.workspace.textDocuments.find((doc) => doc.uri.scheme === "file" && samePath(doc.uri.fsPath, file));
}

function samePath(a: string, b: string): boolean {
  const x = path.normalize(a);
  const y = path.normalize(b);
  return process.platform === "win32" ? x.toLowerCase() === y.toLowerCase() : x === y;
}

async function readDisk(file: string): Promise<string> {
  try {
    return await fs.promises.readFile(file, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      throw new ClientMethodError(RESOURCE_NOT_FOUND, `no such file: ${file}`);
    }
    throw new ClientMethodError(INTERNAL_ERROR, `cannot read ${file}: ${errorText(error)}`);
  }
}

async function openIfExists(file: string): Promise<vscode.TextDocument | undefined> {
  try {
    return await vscode.workspace.openTextDocument(vscode.Uri.file(file));
  } catch {
    return undefined; // not there (or not a readable file): the caller creates it
  }
}

// A new file with the content, saved to disk. Returns whether it was saved (it is, unless the save fails).
async function createFile(file: string, content: string): Promise<boolean> {
  const uri = vscode.Uri.file(file);
  await vscode.workspace.fs.createDirectory(vscode.Uri.file(path.dirname(file)));
  const edit = new vscode.WorkspaceEdit();
  edit.createFile(uri, { ignoreIfExists: false, overwrite: false });
  edit.insert(uri, new vscode.Position(0, 0), content);
  if (!(await vscode.workspace.applyEdit(edit))) {
    throw new ClientMethodError(INTERNAL_ERROR, `The editor did not create ${file}.`);
  }
  const document = await vscode.workspace.openTextDocument(uri);
  await vscode.window.showTextDocument(document, { preview: false, preserveFocus: false });
  return document.isDirty ? document.save() : true;
}

function fullRange(document: vscode.TextDocument): vscode.Range {
  return new vscode.Range(new vscode.Position(0, 0), document.lineAt(document.lineCount - 1).range.end);
}

// Compares texts without the difference between CRLF and LF (the document's own line endings are kept by VS Code).
function normalizeEol(text: string): string {
  return text.replace(/\r\n/g, "\n");
}

// The terminal shows CRLF line ends; the command's own output may have LF only.
function toTerminalLines(text: string): string {
  return text.replace(/\r?\n/g, "\r\n");
}

function describeExit(exit: CommandExit): string {
  if (exit.exitCode !== null) {
    return `exit code ${exit.exitCode}`;
  }
  return exit.signal ? `signal ${exit.signal}` : "no exit code";
}

function stringArray(value: unknown, name: string): string[] {
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) {
    throw new ClientMethodError(INVALID_PARAMS, `Invalid params: ${name} must be a list of strings.`);
  }
  return value as string[];
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
