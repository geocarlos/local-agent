// Runs real child processes (a node script that echoes lines) through AgentProcess, so the framing, the stdin
// writes, the stderr and the exit reason are exercised with real pipes. No VS Code involved.
import * as assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { test } from "node:test";
import { AgentProcess } from "../src/stdioAgent";

// Echoes each stdin line back on stdout as one line; writes to stderr on the word "warn"; exits with code 3 on the
// line "quit", and with 0 when stdin ends. Partial lines across chunks are covered by framing.test.ts.
const ECHO = `
  let buffer = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => {
    buffer += chunk;
    let index;
    while ((index = buffer.indexOf("\\n")) >= 0) {
      const line = buffer.slice(0, index);
      buffer = buffer.slice(index + 1);
      if (line === "quit") { process.exit(3); }
      if (line === "warn") { process.stderr.write("a warning\\n"); }
      process.stdout.write(line + "\\n");
    }
  });
  process.stdin.on("end", () => process.exit(0));
`;

function waitFor(check: () => boolean, label: string, ms = 10000): Promise<void> {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const timer = setInterval(() => {
      if (check()) {
        clearInterval(timer);
        resolve();
      } else if (Date.now() - started > ms) {
        clearInterval(timer);
        reject(new Error(`timed out waiting for ${label}`));
      }
    }, 10);
  });
}

function echoAgent() {
  const messages: string[] = [];
  const stderr: string[] = [];
  const exits: string[] = [];
  const agent = new AgentProcess(
    {
      onMessage: (text) => messages.push(text),
      onStderr: (text) => stderr.push(text),
      onExit: (reason) => exits.push(reason),
    },
    (program, args, cwd) => spawn(program, args, { cwd, stdio: ["pipe", "pipe", "pipe"] }),
  );
  return { agent, messages, stderr, exits };
}

test("each message goes out as one line and each line comes back whole", async () => {
  const { agent, messages } = echoAgent();
  agent.start(process.execPath, ["-e", ECHO], process.cwd());
  try {
    const texts = ['{"jsonrpc":"2.0","id":1,"method":"initialize"}', '{"jsonrpc":"2.0","id":2,"method":"session/new"}'];
    for (const text of texts) {
      agent.send(text);
    }
    await waitFor(() => messages.length === 2, "two echoed lines");
    assert.deepEqual(messages, texts);
    agent.stop();
    await waitFor(() => !agent.running, "the agent to end after stdin closes");
  } finally {
    agent.stop();
  }
});

test("stderr is passed on, and an exit reports the exit code", async () => {
  const { agent, stderr, exits } = echoAgent();
  agent.start(process.execPath, ["-e", ECHO], process.cwd());
  agent.send("warn");
  await waitFor(() => stderr.join("").includes("a warning"), "the stderr line");
  agent.send("quit");
  await waitFor(() => exits.length === 1, "the exit");
  assert.equal(exits[0], "The agent stopped (exit code 3).");
  assert.equal(agent.running, false);
});

test("send refuses when the agent is not running, and start refuses a second run", async () => {
  const { agent, exits } = echoAgent();
  assert.throws(() => agent.send("{}"), /not running/);
  agent.start(process.execPath, ["-e", ECHO], process.cwd());
  assert.throws(() => agent.start(process.execPath, ["-e", ECHO], process.cwd()), /already running/);
  agent.send("quit");
  await waitFor(() => exits.length === 1, "the exit");
  assert.throws(() => agent.send("{}"), /not running/);
});

test("a program that does not exist reports that it cannot start", async () => {
  const { agent, exits } = echoAgent();
  agent.start("definitely-not-a-real-local-agent-program", ["serve"], process.cwd());
  await waitFor(() => exits.length === 1, "the start error");
  assert.match(exits[0], /^Cannot start the agent: /);
  assert.equal(agent.running, false);
});
