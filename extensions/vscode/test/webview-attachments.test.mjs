// The composer's editor context (T21), run without a DOM: the @ mention, the attachment list, and the ACP content
// blocks a prompt is sent with. The module is the bundle's own file (src/local_agent/web/js/attachments.js).
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  AttachmentList,
  attachmentBlock,
  attachmentLabel,
  mentionAt,
  promptBlocks,
  removeMention,
} from "../../../src/local_agent/web/js/attachments.js";

const selection = { kind: "selection", uri: "file:///w/app.ts", name: "app.ts", startLine: 2, endLine: 4, text: "x = 1" };
const file = { kind: "file", uri: "file:///w/notes.md", name: "notes.md" };

test("an @ mention is the query after the @ at the start or after a space", () => {
  assert.deepEqual(mentionAt("@app", 4), { query: "app", start: 0, end: 4 });
  assert.deepEqual(mentionAt("see @src/a", 10), { query: "src/a", start: 4, end: 10 });
  assert.deepEqual(mentionAt("line\n@x", 7), { query: "x", start: 5, end: 7 });
});

test("no mention when the @ is inside a word, after a second @, or has a space after it", () => {
  assert.equal(mentionAt("mail me@x", 9), null);
  assert.equal(mentionAt("@a@b", 4), null);
  assert.deepEqual(mentionAt("@a @b", 5), { query: "b", start: 3, end: 5 });
  assert.equal(mentionAt("@ab cd", 6), null);
  assert.equal(mentionAt("plain", 5), null);
});

test("the caret, not the end, decides which mention is typed", () => {
  // The caret is after "@a", so the mention is "a" even though "@ab" follows.
  assert.deepEqual(mentionAt("@ab", 2), { query: "a", start: 0, end: 2 });
});

test("removing a mention takes it out of the text and puts the caret where it was", () => {
  const text = "look at @no and more";
  const mention = mentionAt(text, 11);
  assert.deepEqual(removeMention(text, mention), { text: "look at  and more", caret: 8 });
});

test("a selection block is an embedded text resource with its lines in the uri fragment", () => {
  assert.deepEqual(attachmentBlock(selection), {
    type: "resource",
    resource: { uri: "file:///w/app.ts#L2-L4", text: "x = 1" },
  });
});

test("a file block is a resource_link", () => {
  assert.deepEqual(attachmentBlock(file), { type: "resource_link", uri: "file:///w/notes.md", name: "notes.md" });
});

test("labels show the lines of a selection", () => {
  assert.equal(attachmentLabel(selection), "app.ts, lines 2-4");
  assert.equal(attachmentLabel({ ...selection, startLine: 7, endLine: 7 }), "app.ts, line 7");
  assert.equal(attachmentLabel(file), "notes.md");
});

test("the list adds, removes and de-duplicates files, and the open file is a toggle that is on by default", () => {
  const list = new AttachmentList();
  assert.equal(list.isEmpty(), true);
  const first = list.add(file);
  assert.ok(first && first.id === 1);
  assert.equal(list.add(file), null, "a file picked twice is one attachment");
  list.add(selection);
  assert.equal(list.items.length, 2);
  list.remove(first.id);
  assert.deepEqual(list.items.map((item) => item.kind), ["selection"]);

  list.setActive({ kind: "file", uri: "file:///w/open.ts", name: "open.ts" });
  assert.equal(list.activeOn, true);
  assert.equal(list.blocks().length, 2, "the open file goes first, then the chips");
  list.setActiveOn(false);
  assert.equal(list.blocks().length, 1, "a switched-off open file is not sent");
  assert.deepEqual(list.labels(), ["app.ts, lines 2-4"]);
});

test("clearing the chips keeps the open-file toggle as the user set it", () => {
  const list = new AttachmentList();
  list.setActive({ kind: "file", uri: "file:///w/open.ts", name: "open.ts" });
  list.add(selection);
  list.clearChips();
  assert.equal(list.items.length, 0);
  assert.equal(list.activeOn, true);
  assert.equal(list.isEmpty(), false);
  list.setActive(null);
  assert.equal(list.isEmpty(), true);
});

test("a prompt is the typed text first, then the attachments; with no text it is only the attachments", () => {
  const list = new AttachmentList();
  list.add(selection);
  assert.deepEqual(promptBlocks("why?", list).map((block) => block.type), ["text", "resource"]);
  assert.deepEqual(promptBlocks("", list).map((block) => block.type), ["resource"]);
  assert.deepEqual(promptBlocks("hi", new AttachmentList()), [{ type: "text", text: "hi" }]);
});
