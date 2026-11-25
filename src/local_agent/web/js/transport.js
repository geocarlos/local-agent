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
