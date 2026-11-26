// The transport: the only module that knows about WebSocket (or any other pipe).
//
// A link is an object with:
//   send(text)          sends one JSON-RPC message (a string, no trailing newline)
//   onMessage(text)     set by the client; called with each incoming message
//   onClose(reason)     set by the client; called once when the link ends
//
// To feed the same UI from another host (the VS Code webview, T19), write a link that does the same over
// postMessage and pass it to AcpClient. Nothing else in the UI changes.

export class LinkError extends Error {}

// The VS Code webview link (T19). The extension host starts `local-agent serve --stdio` and relays each JSON line:
//   webview -> host: {type: "ready"} once, then {type: "send", text} for each message
//   host -> webview: {type: "init", cwd} once the agent runs, {type: "message", text} for each line from it,
//                    {type: "closed", reason} when the agent is gone (before init it is a failed connect),
//                    {type: "error", reason} when one message could not be sent but the agent is still there
// An error goes to link.onError(text) if the client set it (the chat shows it), else to console.error. The link
// stays open.
// Resolves with {link, cwd} on init. `api` is acquireVsCodeApi(); `win` is the window (its message events).
export function connectHostLink(api, win = window) {
  return new Promise((resolve, reject) => {
    let ready = false;
    let closed = false;
    const link = {
      onMessage: null,
      onClose: null,
      onError: null,
      send(text) {
        if (closed) {
          throw new LinkError("The connection to local-agent is closed.");
        }
        api.postMessage({ type: "send", text });
      },
    };
    const end = (reason) => {
      if (closed) {
        return;
      }
      closed = true;
      win.removeEventListener("message", onHostMessage);
      if (!ready) {
        reject(new LinkError(reason));
      } else if (link.onClose) {
        link.onClose(reason);
      }
    };
    const onHostMessage = (event) => {
      const data = event.data;
      if (!data || typeof data !== "object" || closed) {
        return;
      }
      if (data.type === "init" && !ready) {
        ready = true;
        resolve({ link, cwd: String(data.cwd || "") });
      } else if (data.type === "message" && typeof data.text === "string" && link.onMessage) {
        link.onMessage(data.text);
      } else if (data.type === "closed") {
        end(typeof data.reason === "string" ? data.reason : "The connection was closed.");
      } else if (data.type === "error" && !closed) {
        const text = typeof data.reason === "string" ? data.reason : "A message could not be sent to local-agent.";
        if (link.onError) {
          link.onError(text);
        } else {
          console.error(text);
        }
      }
    };
    win.addEventListener("message", onHostMessage);
    api.postMessage({ type: "ready" });
  });
}

// Opens the agent WebSocket. Resolves with a link once the socket is open; rejects if it cannot open.
// A refused connection (wrong token, foreign origin) is closed by the server before accept, so the browser
// reports it as a failed connection with no reason; the caller shows the generic message.
export function connectWebSocket(url) {
  return new Promise((resolve, reject) => {
    let socket;
    try {
      socket = new WebSocket(url);
    } catch (error) {
      reject(new LinkError(`Cannot open the WebSocket: ${error.message}`));
      return;
    }
    const link = {
      onMessage: null,
      onClose: null,
      send(text) {
        if (socket.readyState !== WebSocket.OPEN) {
          throw new LinkError("The connection to local-agent is closed.");
        }
        socket.send(text);
      },
    };
    let opened = false;
    socket.onopen = () => {
      opened = true;
      resolve(link);
    };
    socket.onmessage = (event) => {
      if (typeof event.data === "string" && link.onMessage) {
        link.onMessage(event.data);
      }
    };
    socket.onerror = () => {
      // The close event follows and carries the outcome; nothing to do here.
    };
    socket.onclose = (event) => {
      if (!opened) {
        reject(new LinkError("local-agent refused the connection. The link needs the token; open the address that local-agent printed."));
        return;
      }
      if (link.onClose) {
        link.onClose(event.code === 1000 ? "The connection was closed." : `The connection was closed (code ${event.code}).`);
      }
    };
  });
}
