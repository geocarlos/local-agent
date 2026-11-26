// Runs one command for a terminal/* request (T20): starts it, keeps its output (stdout and stderr together, the last
// `outputLimit` characters), reports its exit, and ends its whole process tree on kill. Pure (no vscode import): the
// editor's terminal shows the output through `onData`, and the tests run real node processes.
//
// A command with no arguments is a command line, and runs through the platform shell (cmd.exe on Windows, /bin/sh
// elsewhere), as the core sends cmd commands. A command with arguments starts that program directly with them.

import { ChildProcess, spawn } from "node:child_process";

export interface CommandExit {
  exitCode: number | null;
  signal: string | null;
}

export interface CommandOptions {
  cwd: string;
  outputLimit: number;               // characters of output kept; the start is dropped beyond this
  onData?: (text: string) => void;   // every chunk as it arrives
}

export class CommandRun {
  private child: ChildProcess | null = null;
  private exit: CommandExit | null = null;
  private waiters: Array<(exit: CommandExit) => void> = [];
  private text = "";
  private dropped = false;

  constructor(
    readonly command: string,
    readonly args: string[],
    private readonly options: CommandOptions,
  ) {}

  // Starts the command. Throws only for bad input; a command that cannot start reports it through its output and exit.
  start(): void {
    if (this.child) {
      throw new Error("The command is already running.");
    }
    const shell = this.args.length === 0;
    const child = spawn(this.command, this.args, {
      cwd: this.options.cwd,
      shell,
      windowsHide: true,
      // Its own process group on POSIX, so kill() can end the tree. On Windows the tree is ended with taskkill.
      detached: process.platform !== "win32",
      stdio: ["ignore", "pipe", "pipe"],
    });
    this.child = child;
    child.stdout?.setEncoding("utf8");
    child.stderr?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => this.append(chunk));
    child.stderr?.on("data", (chunk: string) => this.append(chunk));
    child.on("error", (error) => {
      this.append(`Cannot start the command: ${error.message}\r\n`);
      this.finish({ exitCode: null, signal: null });
    });
    child.on("close", (code, signal) => this.finish({ exitCode: code, signal }));
  }

  get output(): string {
    return this.text;
  }

  get truncated(): boolean {
    return this.dropped;
  }

  // The exit once the command has ended (its output is complete then), or null while it runs.
  get status(): CommandExit | null {
    return this.exit;
  }

  waitForExit(): Promise<CommandExit> {
    if (this.exit) {
      return Promise.resolve(this.exit);
    }
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  // Ends the command and every process it started. Returns at once; waitForExit() resolves when it has ended.
  kill(): void {
    const child = this.child;
    if (!child || this.exit) {
      return;
    }
    if (process.platform === "win32") {
      if (child.pid !== undefined) {
        const killer = spawn("taskkill", ["/T", "/F", "/PID", String(child.pid)], {
          windowsHide: true,
          stdio: "ignore",
        });
        killer.on("error", () => undefined); // no taskkill: the direct child is killed below
      }
      child.kill();
      return;
    }
    try {
      process.kill(-(child.pid ?? 0), "SIGKILL");
    } catch {
      child.kill("SIGKILL");
    }
  }

  private append(chunk: string): void {
    this.options.onData?.(chunk);
    this.text += chunk;
    if (this.text.length > this.options.outputLimit) {
      this.text = this.text.slice(this.text.length - this.options.outputLimit);
      this.dropped = true;
    }
  }

  private finish(exit: CommandExit): void {
    if (this.exit) {
      return;
    }
    this.exit = exit;
    for (const waiter of this.waiters.splice(0)) {
      waiter(exit);
    }
  }
}
