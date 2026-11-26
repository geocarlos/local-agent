import * as assert from "node:assert/strict";
import { test } from "node:test";
import { sliceLines } from "../src/lineSlice";

test("no line or limit gives the whole text, unchanged", () => {
  assert.equal(sliceLines("a\nb\nc\n"), "a\nb\nc\n");
  assert.equal(sliceLines("a\nb\nc"), "a\nb\nc");
  assert.equal(sliceLines(""), "");
});

test("line starts at a 1-based line and limit counts lines", () => {
  assert.equal(sliceLines("a\nb\nc\n", 2, 1), "b\n");
  assert.equal(sliceLines("a\nb\nc\n", 1, 2), "a\nb\n");
  assert.equal(sliceLines("a\nb\nc\n", 2, null), "b\nc\n");
  assert.equal(sliceLines("a\nb\nc\n", null, 1), "a\n");
});

test("a final newline is kept only when the slice reaches the end and the text had one", () => {
  assert.equal(sliceLines("a\nb\nc", 3, null), "c");
  assert.equal(sliceLines("a\nb\nc", 2, 1), "b\n");
  assert.equal(sliceLines("a\nb\nc\n", 3, null), "c\n");
});

test("CRLF line endings stay with their lines", () => {
  assert.equal(sliceLines("one\r\ntwo\r\nthree\r\n", 2, 1), "two\r\n");
});

test("a start past the end, or a limit of 0, gives an empty text", () => {
  assert.equal(sliceLines("a\nb\n", 3, null), "");
  assert.equal(sliceLines("a\nb\n", 1, 0), "");
});

test("a limit that runs past the end is cut at the end", () => {
  assert.equal(sliceLines("a\nb", 2, 50), "b");
});
