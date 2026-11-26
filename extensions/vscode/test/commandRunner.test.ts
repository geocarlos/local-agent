// Runs real processes: the runner is the part of terminal/* that has no VS Code in it.
import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { CommandRun } from "../src/commandRunner";

const NODE = JSON.stringify(process.execPath);

function runProgram(script: string, limit = 100_000): CommandRun {
  // A program with arguments starts directly; the script is one argument, so no shell is involved.
  const run = new CommandRun(process.execPath, ["-e", script], { cwd: os.tmpdir(), outputLimit: limit });
  run.start();
  return run;
}

test("output from stdout and stderr is kept together, and the exit code is reported", async () => {
  const run = runProgram("console.log('out line'); console.error('err line'); process.exit(3);");
  const exit = await run.waitForExit();
  assert.equal(exit.exitCode, 3);
  assert.match(run.output, /out line/);
  assert.match(run.output, /err line/);
  assert.equal(run.truncated, false);
  assert.deepEqual(run.status, exit);
});

test("a command line with no arguments runs through the shell", async () => {
  const run = new CommandRun(`${NODE} -e "console.log('via shell')"`, [], { cwd: os.tmpdir(), outputLimit: 1000 });
  run.start();
  const exit = await run.waitForExit();
  assert.equal(exit.exitCode, 0);
  assert.match(run.output, /via shell/);
});

test("only the end of the output is kept when it is over the limit, and that is reported", async () => {
  const run = runProgram("process.stdout.write('x'.repeat(5000) + 'END');", 1000);
  await run.waitForExit();
  assert.equal(run.truncated, true);
  assert.ok(run.output.length <= 1000, `kept ${run.output.length} characters`);
  assert.ok(run.output.endsWith("END"));
});

test("onData gets each chunk as it arrives", async () => {
  const chunks: string[] = [];
  const run = new CommandRun(process.execPath, ["-e", "console.log('chunk')"], {
    cwd: os.tmpdir(),
    outputLimit: 1000,
    onData: (text) => chunks.push(text),
  });
  run.start();
  await run.waitForExit();
  assert.match(chunks.join(""), /chunk/);
});

test("kill ends a running command, and waitForExit resolves after it", async () => {
  const run = runProgram("setTimeout(() => {}, 60000);");
  assert.equal(run.status, null);
  const started = Date.now();
  run.kill();
  const exit = await run.waitForExit();
  assert.ok(Date.now() - started < 10_000, "the command was ended, not waited for");
  assert.notDeepEqual(exit, { exitCode: 0, signal: null });
  assert.deepEqual(run.status, exit);
});

test("a command that cannot start is reported as an exit with no code and says why", async () => {
  const run = new CommandRun("no-such-program-t20-xyz", ["arg"], { cwd: os.tmpdir(), outputLimit: 1000 });
  run.start();
  const exit = await run.waitForExit();
  assert.equal(exit.exitCode, null);
  assert.match(run.output, /Cannot start the command/);
});

test("a second start is refused", () => {
  const run = runProgram("setTimeout(() => {}, 100);");
  assert.throws(() => run.start(), /already running/);
  run.kill();
});

// A shell command that starts a node process, which starts a grandchild node process that prints its pid and sleeps.
// The grandchild inherits the pipes, as a real program under cmd.exe does. Kill must end all three.
test("kill ends the whole process tree of a shell command, and waitForExit resolves", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "t20-tree-"));
  const grandchild = path.join(dir, "grandchild.js");
  const middle = path.join(dir, "middle.js");
  fs.writeFileSync(grandchild, 'console.log("PID " + process.pid); setTimeout(() => {}, 60000);', "utf8");
  fs.writeFileSync(
    middle,
    `require("node:child_process").spawn(process.execPath, [${JSON.stringify(grandchild)}], { stdio: "inherit" });` +
      " setTimeout(() => {}, 60000);",
    "utf8",
  );
  const run = new CommandRun(`${NODE} ${JSON.stringify(middle)}`, [], { cwd: os.tmpdir(), outputLimit: 10_000 });
  run.start();
  let grandchildPid: number | undefined;
  try {
    const deadline = Date.now() + 15_000;
    while (grandchildPid === undefined && Date.now() < deadline) {
      const match = /PID (\d+)/.exec(run.output);
      grandchildPid = match ? Number(match[1]) : undefined;
      if (grandchildPid === undefined) {
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    }
    assert.ok(grandchildPid, `the grandchild started and printed its pid (output: ${run.output})`);
    run.kill();
    const started = Date.now();
    await run.waitForExit();
    assert.ok(Date.now() - started < 5_000, "waitForExit resolved within a few seconds of the kill");
    assert.throws(() => process.kill(grandchildPid as number, 0), "the grandchild is gone");
  } finally {
    run.kill();
    if (grandchildPid !== undefined) {
      try {
        process.kill(grandchildPid, "SIGKILL"); // only the pid this test read from its own output
      } catch {
        // already gone
      }
    }
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
