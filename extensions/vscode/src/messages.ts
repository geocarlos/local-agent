// The messages between the webview and the extension host. The host relays the text of each JSON line; it does
// not read the JSON-RPC inside (that is the webview's AcpClient). Pure, so it can be tested without vscode.

import type { Attachment, FileAttachment } from "./attachments";

// webview -> host
export type WebviewToHost =
  | { type: "ready" }
  | { type: "send"; text: string }
  // An @file search (T21): the host answers with {type: "files", id, files} for the same id.
  | { type: "findFiles"; id: number; query: string };

// What the host answers for the agent (T20): the file and terminal methods of ACP v1. The agent takes these only
// when they are declared in initialize, so the webview's initialize carries them (see webview/js/main.js).
// `_meta` is ACP's extension point: the host also declares the diagnostics answer (T21) there.
export interface ClientCapabilities {
  fs: { readTextFile: boolean; writeTextFile: boolean };
  terminal: boolean;
  _meta: { "wildcat-code-agent": { diagnostics: boolean } };
}

export const CLIENT_CAPABILITIES: ClientCapabilities = {
  fs: { readTextFile: true, writeTextFile: true },
  terminal: true,
  _meta: { "wildcat-code-agent": { diagnostics: true } },
};

// host -> webview
export type HostToWebview =
  | { type: "init"; cwd: string; clientCapabilities: ClientCapabilities }
  | { type: "message"; text: string }
  | { type: "closed"; reason: string }
  // A message could not be sent, but the agent is still running: the link stays open.
  | { type: "error"; reason: string }
  // A selection or other attachment to add to the composer (T21).
  | { type: "attach"; attachment: Attachment }
  // The active editor's file, or null when there is none: it is offered as an "open file" toggle (T21).
  | { type: "active"; file: FileAttachment | null }
  // The answer to a findFiles request.
  | { type: "files"; id: number; files: FileAttachment[] };

// Returns the message if `data` has a known shape, else null (anything else from the webview is ignored).
export function parseWebviewMessage(data: unknown): WebviewToHost | null {
  if (!data || typeof data !== "object") {
    return null;
  }
  const record = data as Record<string, unknown>;
  if (record.type === "ready") {
    return { type: "ready" };
  }
  if (record.type === "send" && typeof record.text === "string") {
    return { type: "send", text: record.text };
  }
  if (record.type === "findFiles" && typeof record.id === "number" && typeof record.query === "string") {
    return { type: "findFiles", id: record.id, query: record.query.slice(0, 200) };
  }
  return null;
}

// The error shown when the agent's program cannot be started because it is not found (ENOENT). The most common
// cause after a fresh install is a VS Code that was already open: Windows gives a program the PATH it started with,
// so the new install folder is not on VS Code's PATH until every VS Code window is closed and opened again.
export function notFoundText(program: string): string {
  return (
    `Wildcat Code Agent cannot find "${program}". If you installed the app after VS Code started, close every ` +
    "VS Code window and open it again (Reload Window is not enough). Otherwise install the app, or set the full " +
    "path to wildcat-code-agent.exe in the wildcatCodeAgent.command setting."
  );
}
