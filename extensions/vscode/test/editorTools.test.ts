// The terminal side of EditorTools (T20), with a stand-in for the vscode module so the plain node test runner can
// load it. The commands are real node processes; the terminals are fakes that record what is written to them. The
// same behaviour is checked in a real VS Code by test-e2e/suite/editorTools.ts.
import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import Module = require("node:module");
import { test } from "node:test";

type Listener<T> = (value: T) => void;

class FakeEmitter<T> {
  private listeners: Listener<T>[] = [];
  disposed = false;
  readonly event = (listener: Listener<T>) => {
    this.listeners.push(listener);
    return { dispose: () => undefined };
  };
  fire(value: T): void {
    if (!this.disposed) {
      for (const listener of this.listeners) {
        listener(value);
      }
    }
  }
  dispose(): void {
    this.disposed = true;
  }
}

interface FakePty {
  onDidWrite: (listener: Listener<string>) => unknown;
  close: () => void;
}

// A terminal that records its text. dispose() closes it the way VS Code does: the pseudoterminal's close runs.
class FakeTerminal {
  disposed = false;
  text = "";
  constructor(private readonly pty: FakePty) {
    pty.onDidWrite((chunk) => {
      this.text += chunk;
    });
  }
  show(): void {
    // shown
  }
  dispose(): void {
    if (!this.disposed) {
      this.disposed = true;
      this.pty.close();
    }
  }
}

const terminals: FakeTerminal[] = [];

const fakeVscode = {
  EventEmitter: FakeEmitter,
  Position: class {},
  Range: class {},
  WorkspaceEdit: class {},
  Uri: { file: (p: string) => ({ fsPath: p, scheme: "file" }) },
  window: {
    createTerminal: (options: { pty: FakePty }) => {
      const terminal = new FakeTerminal(options.pty);
      terminals.push(terminal);
      return terminal;
    },
    showTextDocument: async () => undefined,
  },
  workspace: { textDocuments: [] },
};

// Makes require("vscode") return the stand-in, for this test file's load of ../src/editorTools.
const loader = Module as unknown as { _load: (request: string, ...rest: unknown[]) => unknown };
const originalLoad = loader._load;
loader._load = function (request: string, ...rest: unknown[]) {
  return request === "vscode" ? fakeVscode : originalLoad.call(this, request, ...rest);
};
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { EditorTools } = require("../src/editorTools") as typeof import("../src/editorTools");

const LONG = "setTimeout(() => {}, 60000);";

async function waitUntil(check: () => boolean, what: string, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!check()) {
    if (Date.now() > deadline) {
      throw new Error(`timed out waiting for ${what}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

async function start(tools: InstanceType<typeof EditorTools>, script: string): Promise<string> {
  const { terminalId } = (await tools.handle("terminal/create", {
    sessionId: "s",
    command: process.execPath,
    args: ["-e", script],
    cwd: os.tmpdir(),
  })) as { terminalId: string };
  return terminalId;
}

test("release ends a running command, and its terminal stays open with the exit line", async () => {
  const tools = new EditorTools();
  const id = await start(tools, LONG);
  const terminal = terminals[terminals.length - 1];
  await tools.handle("terminal/release", { sessionId: "s", terminalId: id });
  await waitUntil(() => /\[(exit code|signal|no exit code)/.test(terminal.text), "the exit line");
  assert.equal(terminal.disposed, false, "the terminal is still open");
  await assert.rejects(
    tools.handle("terminal/output", { sessionId: "s", terminalId: id }),
    (error: { code?: number }) => error.code === -32002,
    "a released terminal id is no longer known",
  );
  tools.dispose();
});

test("release of a finished command keeps its output and exit line, and a second release is not an error", async () => {
  const tools = new EditorTools();
  const id = await start(tools, "console.log('done here');");
  const terminal = terminals[terminals.length - 1];
  await waitUntil(() => terminal.text.includes("[exit code 0]"), "the exit line");
  assert.equal(await tools.handle("terminal/release", { sessionId: "s", terminalId: id }), null);
  assert.equal(await tools.handle("terminal/release", { sessionId: "s", terminalId: id }), null);
  assert.match(terminal.text, /done here/);
  assert.equal(terminal.disposed, false);
  tools.dispose();
});

test("the next command closes the previous command's terminal, and not a command still in use", async () => {
  const tools = new EditorTools();
  const first = await start(tools, "console.log('first');");
  const firstTerminal = terminals[terminals.length - 1];
  await tools.handle("terminal/release", { sessionId: "s", terminalId: first });
  const second = await start(tools, LONG);
  const secondTerminal = terminals[terminals.length - 1];
  assert.equal(firstTerminal.disposed, true, "the released terminal is closed");
  assert.equal(secondTerminal.disposed, false, "the running command's terminal is kept");
  await tools.handle("terminal/release", { sessionId: "s", terminalId: second });
  tools.dispose();
});

test("the user closing a terminal ends its command and forgets it", async () => {
  const tools = new EditorTools();
  const id = await start(tools, LONG);
  const terminal = terminals[terminals.length - 1];
  terminal.dispose(); // what the user does
  await assert.rejects(
    tools.handle("terminal/wait_for_exit", { sessionId: "s", terminalId: id }),
    (error: { code?: number }) => error.code === -32002,
  );
  tools.dispose();
});

test("releaseAll ends every command the agent may still use, and keeps their terminals", async () => {
  const tools = new EditorTools();
  await start(tools, LONG);
  await start(tools, LONG);
  const [a, b] = terminals.slice(-2);
  tools.releaseAll();
  await waitUntil(() => a.text.includes("[") && b.text.includes("["), "both exit lines");
  assert.equal(a.disposed, false);
  assert.equal(b.disposed, false);
  tools.dispose();
});

test("dispose ends the commands and closes their terminals", async () => {
  const tools = new EditorTools();
  await start(tools, LONG);
  const terminal = terminals[terminals.length - 1];
  tools.dispose();
  assert.equal(terminal.disposed, true);
  await assert.rejects(tools.handle("fs/read_text_file", { path: __filename }), /editor panel is closed/);
});

test("a cwd that is not an existing directory is refused", async () => {
  const tools = new EditorTools();
  const file = path.join(os.tmpdir(), `t20-cwd-file-${process.pid}.txt`);
  fs.writeFileSync(file, "x", "utf8");
  try {
    await assert.rejects(
      tools.handle("terminal/create", { command: "x", cwd: path.join(os.tmpdir(), "no-such-dir-t20-xyz") }),
      (error: { code?: number; message?: string }) => error.code === -32002 && /no such directory/.test(error.message ?? ""),
    );
    await assert.rejects(
      tools.handle("terminal/create", { command: "x", cwd: file }),
      (error: { code?: number; message?: string }) => error.code === -32602 && /not a directory/.test(error.message ?? ""),
    );
    await assert.rejects(
      tools.handle("terminal/create", { command: "x", cwd: "relative/dir" }),
      (error: { code?: number }) => error.code === -32602,
    );
  } finally {
    fs.rmSync(file, { force: true });
    tools.dispose();
  }
});
