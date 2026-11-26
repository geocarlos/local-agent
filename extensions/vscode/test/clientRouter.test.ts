import * as assert from "node:assert/strict";
import * as path from "node:path";
import { test } from "node:test";
import {
  answerClientRequest,
  clientRequestOf,
  ClientMethodError,
  INTERNAL_ERROR,
  INVALID_PARAMS,
  METHOD_NOT_FOUND,
  optionalIntParam,
  absolutePathParam,
  recordParams,
} from "../src/clientRouter";

const line = (message: unknown) => JSON.stringify(message);

test("an fs/* or terminal/* request is taken by the host", () => {
  const request = clientRequestOf(line({ jsonrpc: "2.0", id: "srv-1", method: "fs/read_text_file", params: { path: "C:\a" } }));
  assert.deepEqual(request, { id: "srv-1", method: "fs/read_text_file", params: { path: "C:\a" } });
  assert.equal(clientRequestOf(line({ jsonrpc: "2.0", id: 7, method: "terminal/create", params: {} }))?.method, "terminal/create");
  // Missing params become an empty object, so a handler sees a well-formed value.
  assert.deepEqual(clientRequestOf(line({ jsonrpc: "2.0", id: 8, method: "fs/write_text_file" }))?.params, {});
});

test("everything else goes to the webview: permissions, responses, notifications, other requests", () => {
  // The agent's permission request is answered by the webview, not the host.
  assert.equal(clientRequestOf(line({ jsonrpc: "2.0", id: "srv-2", method: "session/request_permission", params: {} })), null);
  // A response to the webview's own request.
  assert.equal(clientRequestOf(line({ jsonrpc: "2.0", id: 1, result: { protocolVersion: 1 } })), null);
  // A notification (no id): nobody answers it.
  assert.equal(clientRequestOf(line({ jsonrpc: "2.0", method: "fs/read_text_file", params: {} })), null);
  assert.equal(clientRequestOf(line({ jsonrpc: "2.0", method: "session/update", params: {} })), null);
  // A line that is not JSON, or not an object.
  assert.equal(clientRequestOf("not json"), null);
  assert.equal(clientRequestOf("[1,2]"), null);
  // An id that is neither a number nor a string.
  assert.equal(clientRequestOf(line({ jsonrpc: "2.0", id: null, method: "fs/read_text_file" })), null);
});

test("a handler's result is the reply, and an undefined result is null", async () => {
  const request = { id: 3, method: "fs/read_text_file", params: {} };
  const reply = JSON.parse(await answerClientRequest(request, async () => ({ content: "hi" })));
  assert.deepEqual(reply, { jsonrpc: "2.0", id: 3, result: { content: "hi" } });
  const none = JSON.parse(await answerClientRequest(request, async () => undefined));
  assert.deepEqual(none, { jsonrpc: "2.0", id: 3, result: null });
});

test("a ClientMethodError keeps its code; any other error is an internal error", async () => {
  const request = { id: "x", method: "fs/read_text_file", params: {} };
  const missing = JSON.parse(await answerClientRequest(request, async () => {
    throw new ClientMethodError(-32002, "no such file");
  }));
  assert.deepEqual(missing, { jsonrpc: "2.0", id: "x", error: { code: -32002, message: "no such file" } });
  const crash = JSON.parse(await answerClientRequest(request, async () => {
    throw new TypeError("boom");
  }));
  assert.deepEqual(crash, { jsonrpc: "2.0", id: "x", error: { code: INTERNAL_ERROR, message: "boom" } });
});

test("the handler receives the method and the params it was sent", async () => {
  const seen: Array<[string, unknown]> = [];
  await answerClientRequest({ id: 1, method: "terminal/kill", params: { terminalId: "t1" } }, async (method, params) => {
    seen.push([method, params]);
    return null;
  });
  assert.deepEqual(seen, [["terminal/kill", { terminalId: "t1" }]]);
});

test("parameter checks name the bad param with invalid params", () => {
  assert.throws(() => recordParams([]), (e: unknown) => e instanceof ClientMethodError && e.code === INVALID_PARAMS);
  assert.throws(() => absolutePathParam({ path: "relative.txt" }, "path"), /absolute path/);
  const absolute = path.resolve("some-file.txt");
  assert.equal(absolutePathParam({ path: absolute }, "path"), absolute);
  assert.equal(optionalIntParam({}, "line", 1), null);
  assert.equal(optionalIntParam({ line: null }, "line", 1), null);
  assert.equal(optionalIntParam({ line: 3 }, "line", 1), 3);
  assert.throws(() => optionalIntParam({ line: 0 }, "line", 1), /at least 1/);
  assert.throws(() => optionalIntParam({ line: 1.5 }, "line", 1), /integer/);
  assert.throws(() => optionalIntParam({ line: "2" }, "line", 1), /integer/);
});

test("an unknown method is answered by the handler's own error", async () => {
  const reply = JSON.parse(await answerClientRequest({ id: 9, method: "fs/rename", params: {} }, async (method) => {
    throw new ClientMethodError(METHOD_NOT_FOUND, `Method not found: ${method}`);
  }));
  assert.equal(reply.error.code, METHOD_NOT_FOUND);
});
