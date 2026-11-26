// The agent process: `<localAgent.command> serve --stdio --cwd <folder>`, one JSON message per line each way.
// Apart from the child process (injected through spawn, so tests can run a plain node script) it has no vscode import.

import { ChildProcess, spawn as nodeSpawn } from "node:child_process";
import { encodeLine, LineSplitter } from "./framing";

export type SpawnFn = (program: string, args: string[], cwd: string) => ChildProcess;

export interface AgentHandlers {
  onMessage(text: string): void;
  onStderr(text: string): void;
  onExit(reason: string): void;
}

// The program and arguments for the server. `command` is the setting: the first item is the program, the rest
// are its arguments (for example ["uv", "run", "--project", "<repo>", "local-agent"]).
export function serveArgv(command: string[], folder: string): { program: string; args: string[] } {
  const [program, ...rest] = command.filter((item) => item.trim().length > 0);
  if (!program) {
    throw new Error('localAgent.command is empty. Set it to the local-agent command, for example ["local-agent"].');
  }
  return { program, args: [...rest, "serve", "--stdio", "--cwd", folder] };
}

export const KILL_AFTER_MS = 3000;

export class AgentProcess {
  private child: ChildProcess | null = null;
  private exited = false;
  private killTimer: NodeJS.Timeout | null = null;

  constructor(
    private readonly handlers: AgentHandlers,
    private readonly spawn: SpawnFn = defaultSpawn,
  ) {}

  get running(): boolean {
    return this.child !== null && !this.exited;
  }

  start(program: string, args: string[], cwd: string): void {
    if (this.child) {
      throw new Error("The agent is already running.");
    }
    const child = this.spawn(program, args, cwd);
    this.child = child;
    const lines = new LineSplitter();
    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => {
      for (const line of lines.push(chunk)) {
        this.handlers.onMessage(line);
      }
    });
    child.stdout?.on("end", () => {
      for (const line of lines.end()) {
        this.handlers.onMessage(line);
      }
    });
    child.stderr?.setEncoding("utf8");
    child.stderr?.on("data", (chunk: string) => this.handlers.onStderr(chunk));
    child.on("error", (error) => this.finish(`Cannot start the agent: ${error.message}`));
    child.on("exit", (code, signal) => {
      this.finish(code !== null ? `The agent stopped (exit code ${code}).` : `The agent stopped (signal ${signal}).`);
    });
  }

  // Writes one message. Throws if the agent is not running.
  send(text: string): void {
    if (!this.running || !this.child?.stdin) {
      throw new Error("The agent is not running.");
    }
    this.child.stdin.write(encodeLine(text));
  }

  // Ends the input (the server finishes its turns and exits on EOF); kills it if it is still there after a while.
  stop(): void {
    const child = this.child;
    if (!child || this.exited) {
      return;
    }
    child.stdin?.end();
    this.killTimer = setTimeout(() => child.kill(), KILL_AFTER_MS);
    this.killTimer.unref();
  }

  private finish(reason: string): void {
    if (this.exited) {
      return;
    }
    this.exited = true;
    if (this.killTimer) {
      clearTimeout(this.killTimer);
    }
    this.handlers.onExit(reason);
  }
}

function defaultSpawn(program: string, args: string[], cwd: string): ChildProcess {
  return nodeSpawn(program, args, { cwd, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
}
