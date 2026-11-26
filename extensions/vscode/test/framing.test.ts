import * as assert from "node:assert/strict";
import { test } from "node:test";
import { encodeLine, LineSplitter } from "../src/framing";
import { parseWebviewMessage } from "../src/messages";
import { serveArgv } from "../src/stdioAgent";

test("a line split across chunks is joined once the newline arrives", () => {
  const splitter = new LineSplitter();
  assert.deepEqual(splitter.push('{"jsonrpc":"2.0",'), []);
  assert.deepEqual(splitter.push('"id":1}\n{"id":2'), ['{"jsonrpc":"2.0","id":1}']);
  assert.deepEqual(splitter.push("}\n"), ['{"id":2}']);
});

test("several lines in one chunk come out in order, empty lines are skipped, CRLF is trimmed", () => {
  const splitter = new LineSplitter();
  assert.deepEqual(splitter.push('a\r\n\nb\nc\n'), ["a", "b", "c"]);
});

test("an unfinished last line is returned by end() once, then the buffer is empty", () => {
  const splitter = new LineSplitter();
  assert.deepEqual(splitter.push("one\ntwo"), ["one"]);
  assert.deepEqual(splitter.end(), ["two"]);
  assert.deepEqual(splitter.end(), []);
});

test("multi-byte text survives when the caller decodes UTF-8 (the relay uses setEncoding)", () => {
  const splitter = new LineSplitter();
  assert.deepEqual(splitter.push("žluťoučký kůň 🙂\n"), ["žluťoučký kůň 🙂"]);
});

test("encodeLine adds one newline and refuses a message with a newline inside", () => {
  assert.equal(encodeLine('{"id":1}'), '{"id":1}\n');
  assert.throws(() => encodeLine('{"a":1}\n{"b":2}'), /one line/);
});

test("parseWebviewMessage accepts ready and send, and ignores anything else", () => {
  assert.deepEqual(parseWebviewMessage({ type: "ready" }), { type: "ready" });
  assert.deepEqual(parseWebviewMessage({ type: "send", text: '{"id":1}' }), { type: "send", text: '{"id":1}' });
  assert.equal(parseWebviewMessage({ type: "send", text: 3 }), null);
  assert.equal(parseWebviewMessage({ type: "unknown" }), null);
  assert.equal(parseWebviewMessage("ready"), null);
  assert.equal(parseWebviewMessage(null), null);
});

test("serveArgv puts the setting's extra items before serve --stdio --cwd", () => {
  assert.deepEqual(serveArgv(["wildcat-code-agent"], "C:\\work"), {
    program: "wildcat-code-agent",
    args: ["serve", "--stdio", "--cwd", "C:\\work"],
  });
  assert.deepEqual(serveArgv(["uv", "run", "--project", "C:\\repo", "wildcat-code-agent"], "C:\\work"), {
    program: "uv",
    args: ["run", "--project", "C:\\repo", "wildcat-code-agent", "serve", "--stdio", "--cwd", "C:\\work"],
  });
});

test("serveArgv refuses an empty command with a message that names the setting", () => {
  assert.throws(() => serveArgv([], "C:\\work"), /wildcatCodeAgent\.command is empty/);
  assert.throws(() => serveArgv(["  "], "C:\\work"), /wildcatCodeAgent\.command is empty/);
});
