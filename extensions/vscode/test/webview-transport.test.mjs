// The transport seam of the shared web bundle, run without a browser: connectHostLink with a fake acquireVsCodeApi
// and a fake window. The bundle file is plain ES module JavaScript (no DOM at import time).
import assert from "node:assert/strict";
import { test } from "node:test";
import { connectHostLink } from "../../../src/local_agent/web/js/transport.js";

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
