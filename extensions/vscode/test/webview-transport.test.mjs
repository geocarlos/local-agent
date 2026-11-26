// The transport seam of the shared web bundle, run without a browser: connectHostLink with a fake acquireVsCodeApi
// and a fake window. The bundle file is plain ES module JavaScript (no DOM at import time).
import assert from "node:assert/strict";
import { test } from "node:test";
import { connectHostLink } from "../../../src/wildcat_code_agent/web/js/transport.js";

function fakeWindow() {
  const listeners = new Set();
  return {
    addEventListener(type, fn) {
      if (type === "message") listeners.add(fn);
    },
    removeEventListener(type, fn) {
      if (type === "message") listeners.delete(fn);
    },
    deliver(data) {
      for (const fn of [...listeners]) fn({ data });
    },
    get count() {
      return listeners.size;
    },
  };
}

function fakeApi() {
  const posted = [];
  return { posted, postMessage: (message) => posted.push(message) };
}

test("connectHostLink says ready, resolves on init with the cwd, and relays both ways", async () => {
  const win = fakeWindow();
  const api = fakeApi();
  const pending = connectHostLink(api, win);
  assert.deepEqual(api.posted, [{ type: "ready" }]);
  win.deliver({ type: "init", cwd: "C:\\work" });
  const { link, cwd } = await pending;
  assert.equal(cwd, "C:\\work");

  const got = [];
  link.onMessage = (text) => got.push(text);
  win.deliver({ type: "message", text: '{"jsonrpc":"2.0","id":1,"result":{}}' });
  win.deliver({ type: "message", text: 42 }); // not a string: ignored
  assert.deepEqual(got, ['{"jsonrpc":"2.0","id":1,"result":{}}']);

  link.send('{"jsonrpc":"2.0","id":2,"method":"session/new"}');
  assert.deepEqual(api.posted.at(-1), { type: "send", text: '{"jsonrpc":"2.0","id":2,"method":"session/new"}' });
});

test("a closed message before init rejects the connect with the host's reason", async () => {
  const win = fakeWindow();
  const pending = connectHostLink(fakeApi(), win);
  win.deliver({ type: "closed", reason: "Cannot start the agent: nope" });
  await assert.rejects(pending, (error) => error.message === "Cannot start the agent: nope");
  assert.equal(win.count, 0);
});

test("a closed message after init calls onClose once and stops send", async () => {
  const win = fakeWindow();
  const api = fakeApi();
  const pending = connectHostLink(api, win);
  win.deliver({ type: "init", cwd: "C:\\work" });
  const { link } = await pending;
  const reasons = [];
  link.onClose = (reason) => reasons.push(reason);
  win.deliver({ type: "closed", reason: "The agent stopped (exit code 1)." });
  win.deliver({ type: "closed", reason: "again" });
  assert.deepEqual(reasons, ["The agent stopped (exit code 1)."]);
  assert.throws(() => link.send("{}"), /closed/);
  assert.equal(win.count, 0);
});

test("a second init does not resolve twice, and unrelated events are ignored", async () => {
  const win = fakeWindow();
  const pending = connectHostLink(fakeApi(), win);
  win.deliver(null);
  win.deliver("init");
  win.deliver({ type: "init", cwd: "first" });
  win.deliver({ type: "init", cwd: "second" });
  const { cwd } = await pending;
  assert.equal(cwd, "first");
});

test("a host error goes to onError and leaves the link open", async () => {
  const win = fakeWindow();
  const api = fakeApi();
  const pending = connectHostLink(api, win);
  win.deliver({ type: "init", cwd: "C:\work" });
  const { link } = await pending;
  const errors = [];
  const closes = [];
  link.onError = (text) => errors.push(text);
  link.onClose = (reason) => closes.push(reason);
  win.deliver({ type: "error", reason: "Could not send to the agent: A protocol message must be one line." });
  assert.deepEqual(errors, ["Could not send to the agent: A protocol message must be one line."]);
  assert.deepEqual(closes, []);
  assert.equal(win.count, 1);
  link.send('{"jsonrpc":"2.0","id":3,"method":"session/prompt"}');
  assert.equal(api.posted.at(-1).type, "send");
});

test("a host error with no onError is logged with console.error, and the link stays open", async () => {
  const win = fakeWindow();
  const pending = connectHostLink(fakeApi(), win);
  win.deliver({ type: "init", cwd: "C:\work" });
  const { link } = await pending;
  const logged = [];
  const original = console.error;
  console.error = (text) => logged.push(text);
  try {
    win.deliver({ type: "error", reason: "Could not send to the agent: boom" });
  } finally {
    console.error = original;
  }
  assert.deepEqual(logged, ["Could not send to the agent: boom"]);
  assert.doesNotThrow(() => link.send("{}"));
});

test("init carries the host's client capabilities to the page; without them the page gets none", async () => {
  const win = fakeWindow();
  const declared = { fs: { readTextFile: true, writeTextFile: true }, terminal: true };
  const pending = connectHostLink(fakeApi(), win);
  win.deliver({ type: "init", cwd: "C:\work", clientCapabilities: declared });
  const { clientCapabilities } = await pending;
  assert.deepEqual(clientCapabilities, declared);

  const bare = fakeWindow();
  const second = connectHostLink(fakeApi(), bare);
  bare.deliver({ type: "init", cwd: "C:\work" });
  assert.deepEqual((await second).clientCapabilities, {});
});
