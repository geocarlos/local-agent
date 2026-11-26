import * as assert from "node:assert/strict";
import { test } from "node:test";
import { GuardedSender } from "../src/guardedSender";

test("messages reach the webview until the panel is disposed, then none do", () => {
  const delivered: string[] = [];
  const sender = new GuardedSender<string>((message) => delivered.push(message));
  sender.send("init");
  sender.send("message");
  sender.dispose();
  sender.send("closed");
  sender.send("message after dispose");
  assert.deepEqual(delivered, ["init", "message"]);
});

test("a send after dispose never reaches a webview that would throw", () => {
  // The stand-in for panel.webview after dispose: any use throws, as VS Code does.
  let disposed = false;
  const sender = new GuardedSender<string>(() => {
    if (disposed) {
      throw new Error("Webview is disposed");
    }
  });
  sender.dispose();
  disposed = true;
  assert.doesNotThrow(() => sender.send("closed"));
});
