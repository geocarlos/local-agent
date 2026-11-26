// Editor context for the chat (T21), pure (no vscode import): the attachments the host builds from the editor and the
// workspace, the glob for an @file search, and the diagnostics the agent asks for. The vscode calls are in
// contextTools.ts and extension.ts; the webview builds the ACP content blocks (webview/js/attachments.js).

import { ClientMethodError, INVALID_PARAMS, recordParams } from "./clientRouter";

// The most text one selection may carry from the editor (the agent cuts it further to its own limit).
export const SELECTION_MAX_CHARS = 1_000_000;
// The most @file suggestions one search returns.
export const MENTION_LIMIT = 20;
// The most candidates a search asks the workspace for; rankMentions picks the best MENTION_LIMIT of them.
export const MENTION_SEARCH_LIMIT = 200;
// Folders a search never lists, whatever the user's settings say.
const BUILT_IN_EXCLUDES = ["**/node_modules", "**/.git"];
// The most diagnostics the host sends for one file (the agent shows fewer).
export const DIAGNOSTICS_PER_FILE = 200;

// A selection in the active editor: the text as the editor has it, with its lines (1-based, inclusive).
export interface SelectionAttachment {
  kind: "selection";
  uri: string;
  name: string;
  startLine: number;
  endLine: number;
  text: string;
}

// A whole file, by URI: an @file pick or the active editor's file (read by the agent through the editor).
export interface FileAttachment {
  kind: "file";
  uri: string;
  name: string;
}

export type Attachment = SelectionAttachment | FileAttachment;

// The selection as an attachment. `startLine` and `endLine` are 1-based and inclusive. Text beyond SELECTION_MAX_CHARS
// is cut here; the agent applies its own, smaller limit as well.
export function selectionAttachment(
  uri: string,
  name: string,
  startLine: number,
  endLine: number,
  text: string,
): SelectionAttachment {
  const kept = text.length > SELECTION_MAX_CHARS ? text.slice(0, SELECTION_MAX_CHARS) : text;
  return { kind: "selection", uri, name, startLine, endLine: Math.max(startLine, endLine), text: kept };
}

// The file as an attachment, by its URI and the name shown in the panel.
export function fileAttachment(uri: string, name: string): FileAttachment {
  return { kind: "file", uri, name };
}

// The query as the search uses it: only letters, digits, dot, dash, underscore and slash survive, so a character that
// means something in a glob is dropped.
export function mentionQuery(query: string): string {
  return query.replace(/[^A-Za-z0-9._\-/]/g, "");
}

// The glob for an @file search: the query as a substring of the file name, matched without regard to case (each
// letter becomes a character class, so the search does not depend on the platform's case rules).
export function mentionGlob(query: string): string {
  const safe = mentionQuery(query);
  if (safe === "") {
    return "**/*";
  }
  const folded = safe.replace(/[A-Za-z]/g, (ch) => `[${ch.toLowerCase()}${ch.toUpperCase()}]`);
  return `**/*${folded}*`;
}

// The exclude glob for an @file search: the enabled entries of the user's `files.exclude` and `search.exclude`, plus
// node_modules and .git. An entry also excludes its contents. Entries with a brace or a comma are left out: they
// cannot be joined into one glob safely (they then list more, which is the safe direction for a search).
export function mentionExclude(...maps: Array<Record<string, unknown> | null | undefined>): string {
  const patterns = new Set<string>();
  const add = (pattern: string): void => {
    patterns.add(pattern);
    patterns.add(`${pattern.replace(/\/+$/, "")}/**`);
  };
  BUILT_IN_EXCLUDES.forEach(add);
  for (const map of maps) {
    for (const [pattern, enabled] of Object.entries(map ?? {})) {
      const trimmed = pattern.trim();
      if (enabled === true && trimmed !== "" && !/[{},]/.test(trimmed)) {
        add(trimmed);
      }
    }
  }
  return `{${[...patterns].join(",")}}`;
}

// The candidates in the order the @ list shows them: names whose file name starts with the query, then names that
// contain it, then the rest. Case does not count, and the order the search gave is kept within each group.
export function rankMentions(query: string, files: FileAttachment[], limit = MENTION_LIMIT): FileAttachment[] {
  const q = mentionQuery(query).toLowerCase();
  const rank = (file: FileAttachment): number => {
    if (q === "") {
      return 1;
    }
    const base = (file.name.split(/[\\/]/).pop() ?? file.name).toLowerCase();
    if (base.startsWith(q)) {
      return 0;
    }
    return base.includes(q) ? 1 : 2;
  };
  return files
    .map((file, index) => ({ file, index, rank: rank(file) }))
    .sort((a, b) => a.rank - b.rank || a.index - b.index)
    .slice(0, limit)
    .map((entry) => entry.file);
}

// The file an active-editor change makes the open file. undefined means the change is ignored: no editor (the
// focus moved to the chat, which is not an editor, or to a panel), or an editor whose document is not a file on
// disk. The last file editor stays the open file in those cases. (The vscode TextEditor has this shape, with a Uri.)
export function activeFileOf<U extends { scheme: string; toString(): string }>(
  editor: { document: { uri: U } } | undefined,
  nameOf: (uri: U) => string,
): FileAttachment | undefined {
  if (!editor || editor.document.uri.scheme !== "file") {
    return undefined;
  }
  return fileAttachment(editor.document.uri.toString(), nameOf(editor.document.uri));
}

// Which severity words the agent sees (the vscode DiagnosticSeverity numbers: Error 0, Warning 1, Information 2,
// Hint 3).
const SEVERITY_WORDS = ["error", "warning", "information", "hint"] as const;

export interface DiagnosticInput {
  severity: number;
  message: string;
  source?: string;
  range: { start: { line: number; character: number }; end: { line: number; character: number } };
}

export interface DiagnosticItem {
  severity: (typeof SEVERITY_WORDS)[number];
  startLine: number;
  startColumn: number;
  endLine: number;
  endColumn: number;
  message: string;
  source?: string;
}

// One diagnostic as the agent reads it: 1-based lines and columns, the severity as a word. An unknown severity
// number is an error, so nothing is hidden.
export function diagnosticItem(input: DiagnosticInput): DiagnosticItem {
  const item: DiagnosticItem = {
    severity: SEVERITY_WORDS[input.severity] ?? "error",
    startLine: input.range.start.line + 1,
    startColumn: input.range.start.character + 1,
    endLine: input.range.end.line + 1,
    endColumn: input.range.end.character + 1,
    message: input.message,
  };
  if (input.source) {
    item.source = input.source;
  }
  return item;
}

// The answer to _wildcat-code-agent/diagnostics. `params` is {uris: string[]}; `lookup` gives a file's diagnostics. Each
// requested file is answered, in the order asked, with its items (at most DIAGNOSTICS_PER_FILE).
export function diagnosticsReply(params: unknown, lookup: (uri: string) => DiagnosticInput[]): unknown {
  const record = recordParams(params);
  const uris = record.uris;
  if (!Array.isArray(uris) || !uris.every((uri) => typeof uri === "string")) {
    throw new ClientMethodError(INVALID_PARAMS, "Invalid params: uris must be a list of strings.");
  }
  return {
    files: (uris as string[]).map((uri) => ({
      uri,
      items: lookup(uri).slice(0, DIAGNOSTICS_PER_FILE).map(diagnosticItem),
    })),
  };
}

