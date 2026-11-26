// The host side of the editor context (T21), pure: the selection attachment, the @file glob, the diagnostics reply,
// and the routing of the agent's _local-agent/diagnostics request.
import * as assert from "node:assert/strict";
import { test } from "node:test";
import {
  DIAGNOSTICS_PER_FILE,
  diagnosticItem,
  diagnosticsReply,
  fileAttachment,
  activeFileOf,
  mentionExclude,
  mentionGlob,
  MENTION_LIMIT,
  rankMentions,
  SELECTION_MAX_CHARS,
  selectionAttachment,
} from "../src/attachments";
import { clientRequestOf, DIAGNOSTICS_METHOD, isHostMethod } from "../src/clientRouter";

function range(startLine: number, startCharacter: number, endLine: number, endCharacter: number) {
  return {
    start: { line: startLine, character: startCharacter },
    end: { line: endLine, character: endCharacter },
  };
}

test("a selection attachment keeps its lines (1-based) and its text", () => {
  const item = selectionAttachment("file:///w/app.ts", "app.ts", 3, 5, "const x = 1;\n");
  assert.deepEqual(item, {
    kind: "selection",
    uri: "file:///w/app.ts",
    name: "app.ts",
    startLine: 3,
    endLine: 5,
    text: "const x = 1;\n",
  });
});

test("a selection whose end is before its start takes the start line as its end", () => {
  assert.equal(selectionAttachment("file:///w/a", "a", 9, 7, "x").endLine, 9);
});

test("selection text beyond the host limit is cut", () => {
  const long = "a".repeat(SELECTION_MAX_CHARS + 10);
  assert.equal(selectionAttachment("file:///w/a", "a", 1, 1, long).text.length, SELECTION_MAX_CHARS);
});

test("a file attachment is a uri and a name", () => {
  assert.deepEqual(fileAttachment("file:///w/b.md", "b.md"), { kind: "file", uri: "file:///w/b.md", name: "b.md" });
});

test("the @file glob keeps letters, digits, dot, dash, underscore and slash, and matches a substring without case", () => {
  assert.equal(mentionGlob("app"), "**/*[aA][pP][pP]*");
  assert.equal(mentionGlob("src/ap-p_1.ts"), "**/*[sS][rR][cC]/[aA][pP]-[pP]_1.[tT][sS]*");
  assert.equal(mentionGlob("a*b{c}[d]!"), "**/*[aA][bB][cC][dD]*");
  assert.equal(mentionGlob(""), "**/*");
  assert.equal(mentionGlob("***"), "**/*");
});

test("the search excludes node_modules and .git and returns at most 20 files", () => {
  assert.equal(MENTION_LIMIT, 20);
});

test("the exclude merges the enabled entries of files.exclude and search.exclude with node_modules and .git", () => {
  const glob = mentionExclude(
    { "**/*.log": true, "**/dist": true, "**/.venv": false, "": true, "**/x,y": true, "**/a{b}": true },
    { "**/coverage": true, "**/tmp": false },
    undefined,
  );
  assert.ok(glob.startsWith("{") && glob.endsWith("}"));
  const parts = glob.slice(1, -1).split(",");
  for (const expected of ["**/node_modules", "**/node_modules/**", "**/.git", "**/.git/**", "**/*.log", "**/dist",
    "**/coverage", "**/coverage/**"]) {
    assert.ok(parts.includes(expected), `${expected} is in the exclude`);
  }
  assert.ok(!parts.includes("**/.venv"), "a disabled entry is not excluded");
  assert.ok(!parts.includes("**/tmp"), "a disabled entry is not excluded");
  assert.ok(!glob.includes("x,y") && !glob.includes("a{b}"), "an entry with a brace or comma is left out");
});

test("the best @file matches come first: a file name that starts with the query, then one that contains it", () => {
  const files = [
    fileAttachment("file:///w/src/mainApp.ts", "src/mainApp.ts"),
    fileAttachment("file:///w/docs/readme.md", "docs/readme.md"),
    fileAttachment("file:///w/src/app.ts", "src/app.ts"),
    fileAttachment("file:///w/lib/APPEND.ts", "lib/APPEND.ts"),
  ];
  assert.deepEqual(rankMentions("App", files).map((f) => f.name), ["src/app.ts", "lib/APPEND.ts", "src/mainApp.ts", "docs/readme.md"]);
  assert.deepEqual(rankMentions("app", files, 2).map((f) => f.name), ["src/app.ts", "lib/APPEND.ts"]);
});

test("the open file follows a real file editor only; no editor or a non-file editor changes nothing", () => {
  const name = (uri: { toString(): string }) => uri.toString().split("/").pop() ?? "";
  const fileEditor = { document: { uri: { scheme: "file", toString: () => "file:///w/a.ts" } } };
  assert.deepEqual(activeFileOf(fileEditor, name), { kind: "file", uri: "file:///w/a.ts", name: "a.ts" });
  assert.equal(activeFileOf(undefined, name), undefined, "focus on the chat (no editor) keeps the last file");
  const untitled = { document: { uri: { scheme: "untitled", toString: () => "untitled:Untitled-1" } } };
  assert.equal(activeFileOf(untitled, name), undefined);
});

test("a diagnostic is 1-based, its severity a word, and its source kept when there is one", () => {
  assert.deepEqual(
    diagnosticItem({ severity: 0, message: "boom", source: "ts", range: range(2, 4, 2, 9) }),
    { severity: "error", startLine: 3, startColumn: 5, endLine: 3, endColumn: 10, message: "boom", source: "ts" },
  );
  const warning = diagnosticItem({ severity: 1, message: "w", range: range(0, 0, 0, 1) });
  assert.equal(warning.severity, "warning");
  assert.equal("source" in warning, false);
  assert.equal(diagnosticItem({ severity: 2, message: "i", range: range(0, 0, 0, 0) }).severity, "information");
  assert.equal(diagnosticItem({ severity: 3, message: "h", range: range(0, 0, 0, 0) }).severity, "hint");
  assert.equal(diagnosticItem({ severity: 99, message: "?", range: range(0, 0, 0, 0) }).severity, "error");
});

test("the diagnostics reply answers each requested file in order, from the lookup", () => {
  const seen: string[] = [];
  const reply = diagnosticsReply({ uris: ["file:///a", "file:///b"] }, (uri) => {
    seen.push(uri);
    return uri === "file:///a" ? [{ severity: 1, message: "m", range: range(0, 0, 0, 1) }] : [];
  });
  assert.deepEqual(seen, ["file:///a", "file:///b"]);
  assert.deepEqual(reply, {
    files: [
      { uri: "file:///a", items: [{ severity: "warning", startLine: 1, startColumn: 1, endLine: 1, endColumn: 2, message: "m" }] },
      { uri: "file:///b", items: [] },
    ],
  });
});

test("the diagnostics reply caps the items per file", () => {
  const many = Array.from({ length: DIAGNOSTICS_PER_FILE + 5 }, () => ({ severity: 0, message: "x", range: range(0, 0, 0, 0) }));
  const reply = diagnosticsReply({ uris: ["file:///a"] }, () => many) as { files: { items: unknown[] }[] };
  assert.equal(reply.files[0].items.length, DIAGNOSTICS_PER_FILE);
});

test("bad diagnostics params are invalid params", () => {
  for (const params of [{}, { uris: "file:///a" }, { uris: [1] }, null, []]) {
    assert.throws(() => diagnosticsReply(params, () => []), /Invalid params/);
  }
});

test("the diagnostics method is a host method, and its request is routed to the host", () => {
  assert.equal(DIAGNOSTICS_METHOD, "_local-agent/diagnostics");
  assert.equal(isHostMethod(DIAGNOSTICS_METHOD), true);
  assert.equal(isHostMethod("_local-agent/models"), false);
  const line = JSON.stringify({ jsonrpc: "2.0", id: "srv-4", method: DIAGNOSTICS_METHOD, params: { uris: [] } });
  assert.deepEqual(clientRequestOf(line), { id: "srv-4", method: DIAGNOSTICS_METHOD, params: { uris: [] } });
});
