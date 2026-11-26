// The messages between the webview and the extension host. The host relays the text of each JSON line; it does
// not read the JSON-RPC inside (that is the webview's AcpClient). Pure, so it can be tested without vscode.

// webview -> host
export type WebviewToHost =
  | { type: "ready" }
  | { type: "send"; text: string };

// What the host answers for the agent (T20): the file and terminal methods of ACP v1. The agent takes these only
// when they are declared in initialize, so the webview's initialize carries them (see webview/js/main.js).
export interface ClientCapabilities {
  fs: { readTextFile: boolean; writeTextFile: boolean };
  terminal: boolean;
}

export const CLIENT_CAPABILITIES: ClientCapabilities = {
  fs: { readTextFile: true, writeTextFile: true },
  terminal: true,
};

// host -> webview
export type HostToWebview =
  | { type: "init"; cwd: string; clientCapabilities: ClientCapabilities }
  | { type: "message"; text: string }
  | { type: "closed"; reason: string }
  // A message could not be sent, but the agent is still running: the link stays open.
  | { type: "error"; reason: string };

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
  return null;
}
