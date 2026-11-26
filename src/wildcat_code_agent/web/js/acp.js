// The ACP client: JSON-RPC 2.0 over a link (see transport.js). It knows the protocol, not the screen.
//
// Outgoing: request(method, params) returns a promise for the result, or rejects with an RpcError.
//           notify(method, params) sends a notification (no answer).
// Incoming: onUpdate(update, sessionId) gets each session/update notification.
//           onPermission(params) must return the result for session/request_permission (a promise is fine).
//           onClose(reason) runs once when the link ends; pending requests are rejected.

export class RpcError extends Error {
  constructor(code, message, data) {
    super(message);
    this.name = "RpcError";
    this.code = code;
    this.data = data;
  }
}

export class AcpClient {
  constructor(link) {
    this.link = link;
    this.nextId = 1;
    this.pending = new Map();
    this.onUpdate = () => {};
    this.onPermission = async () => ({ outcome: { outcome: "cancelled" } });
    this.onClose = () => {};
    this.closed = false;
    link.onMessage = (text) => this._receive(text);
    link.onClose = (reason) => this._close(reason);
  }

  request(method, params) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      try {
        this.link.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
      } catch (error) {
        this.pending.delete(id);
        reject(error);
      }
    });
  }

  notify(method, params) {
    this.link.send(JSON.stringify({ jsonrpc: "2.0", method, params }));
  }

  _receive(text) {
    let message;
    try {
      message = JSON.parse(text);
    } catch {
      console.warn("wildcat-code-agent sent a line that is not JSON; ignored");
      return;
    }
    if (!message || typeof message !== "object") {
      return;
    }
    if (typeof message.method === "string") {
      if (message.id !== undefined) {
        this._answer(message);
      } else if (message.method === "session/update" && message.params) {
        this.onUpdate(message.params.update, message.params.sessionId);
      }
      return;
    }
    const waiter = this.pending.get(message.id);
    if (!waiter) {
      return;
    }
    this.pending.delete(message.id);
    if (message.error) {
      waiter.reject(new RpcError(message.error.code, message.error.message, message.error.data));
    } else {
      waiter.resolve(message.result);
    }
  }

  // The agent asks the client something (session/request_permission). Any failure is an internal error answer.
  async _answer(message) {
    let reply;
    try {
      const handler = message.method === "session/request_permission" ? this.onPermission : null;
      if (!handler) {
        reply = { jsonrpc: "2.0", id: message.id, error: { code: -32601, message: `Method not found: ${message.method}` } };
      } else {
        reply = { jsonrpc: "2.0", id: message.id, result: await handler(message.params) };
      }
    } catch (error) {
      reply = { jsonrpc: "2.0", id: message.id, error: { code: -32603, message: String(error.message || error) } };
    }
    try {
      this.link.send(JSON.stringify(reply));
    } catch {
      // The link is gone; the close handler reports it.
    }
  }

  _close(reason) {
    if (this.closed) {
      return;
    }
    this.closed = true;
    const error = new RpcError(-32603, reason);
    for (const waiter of this.pending.values()) {
      waiter.reject(error);
    }
    this.pending.clear();
    this.onClose(reason);
  }
}
