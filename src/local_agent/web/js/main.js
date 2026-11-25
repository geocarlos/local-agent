// Start-up of the web UI: read the token from the fragment, check it with /info, open the WebSocket, run
// initialize and session/new in the server's workspace, then wire the composer to session/prompt.
//
// The token is in the URL fragment (`/#token=...`). A browser never sends a fragment to the server, so it is
// not in any request line or log. The page reads it once, removes it from the address bar, and keeps it in
// memory only (no storage). Reloading the page therefore needs the printed link again.

import { AcpClient } from "./acp.js";
import { ChatView } from "./chat.js";
import { connectWebSocket } from "./transport.js";

const PROTOCOL_VERSION = 1;

export function tokenFromFragment(hash) {
  return new URLSearchParams(hash.replace(/^#/, "")).get("token") || "";
}

function socketUrl(token) {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${location.host}/ws?token=${encodeURIComponent(token)}`;
}

async function fetchInfo(token) {
  const response = await fetch("/info", {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error("local-agent did not accept the token. Open the address that local-agent printed.");
  }
  return response.json();
}

async function start(view) {
  const token = tokenFromFragment(location.hash);
  if (location.hash) {
    history.replaceState(null, "", location.pathname + location.search); // the token leaves the address bar
  }
  if (!token) {
    view.setStatus("No token", "bad");
    view.showNotice("This page needs the token. Open the link that local-agent printed (it ends in #token=...).");
    return;
  }

  let info;
  try {
    info = await fetchInfo(token);
    if (info.protocolVersion !== PROTOCOL_VERSION) {
      throw new Error(`local-agent speaks protocol ${info.protocolVersion}; this page needs ${PROTOCOL_VERSION}.`);
    }
  } catch (error) {
    view.setStatus("Not connected", "bad");
    view.showNotice(error.message);
    return;
  }

  let client;
  try {
    const link = await connectWebSocket(socketUrl(token));
    client = new AcpClient(link);
  } catch (error) {
    view.setStatus("Not connected", "bad");
    view.showNotice(error.message);
    return;
  }
  client.onUpdate = (update) => view.update(update);
  client.onPermission = (params) => view.askPermission(params);
  client.onClose = (reason) => view.disconnected(reason);

  let sessionId;
  try {
    const init = await client.request("initialize", { protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} });
    if (init.protocolVersion !== PROTOCOL_VERSION) {
      throw new Error(`The agent answered protocol ${init.protocolVersion}; this page needs ${PROTOCOL_VERSION}.`);
    }
    ({ sessionId } = await client.request("session/new", { cwd: info.cwd, mcpServers: [] }));
  } catch (error) {
    view.setStatus("Not connected", "bad");
    view.showNotice(error.message);
    return;
  }
  view.ready(info.cwd);
  wireComposer(view, client, sessionId);
}

function wireComposer(view, client, sessionId) {
  const form = view.doc.getElementById("composer");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = view.prompt.value.trim();
    if (!text || view.send.disabled) {
      return;
    }
    view.prompt.value = "";
    view.showNotice("");
    view.addUser(text);
    view.setBusy(true);
    try {
      const result = await client.request("session/prompt", {
        sessionId,
        prompt: [{ type: "text", text }],
      });
      view.turnEnded(result);
    } catch (error) {
      view.turnFailed(error);
    } finally {
      view.prompt.focus();
    }
  });
  view.prompt.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  view.cancel.addEventListener("click", () => {
    view.closeApproval();
    try {
      client.notify("session/cancel", { sessionId });
    } catch {
      // The connection is gone; the disconnected message already says so.
    }
  });
}

const view = new ChatView(document);
start(view);
