// The screen: the conversation, tool-call cards, the approval dialog, the status line and the composer.
//
// Every piece of text from the agent or the tools goes in with textContent (or a text node), never innerHTML,
// so a file's content or a model's reply cannot run as markup. There is no markdown rendering: the reply is
// shown as plain text, with its line breaks kept.

const APPROVE_KINDS = new Set(["allow_once", "allow_always"]);
const TOOL_STATUSES = new Set(["pending", "in_progress", "completed", "failed"]);
// The most output text a tool card keeps in the page. The end of a long output is kept, with a marker.
export const MAX_OUTPUT_CHARS = 20000;
// The end of a preview that the agent cut (cap_preview adds "...[N more chars]").
const TRUNCATED_PREVIEW = /\n\.\.\.\[\d+ more chars\]$/;

const STOP_LABELS = {
  end_turn: "Done.",
  cancelled: "Cancelled.",
  max_turn_requests: "Stopped: the tool round limit was reached.",
  max_tokens: "Stopped: the reply hit the length limit.",
  refusal: "The model refused to answer.",
};

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

// The class of one line of a unified diff, for colouring.
export function diffLineClass(line) {
  if (line.startsWith("+++") || line.startsWith("---")) {
    return "meta";
  }
  if (line.startsWith("@@")) {
    return "hunk";
  }
  if (line.startsWith("+")) {
    return "add";
  }
  if (line.startsWith("-")) {
    return "del";
  }
  return "ctx";
}

// The preview of an approval request: a diff (coloured by line), proposed content, or a command line.
export function previewElement(preview) {
  const wrap = el("div", "preview-body");
  if (!preview || typeof preview.text !== "string") {
    wrap.append(el("span", "label", "No preview for this call."));
    return wrap;
  }
  const path = preview.path ? ` for '${preview.path}'` : "";
  let label;
  if (preview.kind === "diff") {
    label = `Proposed changes${path}`;
  } else if (preview.kind === "command") {
    label = `Command (${preview.shell || "shell"})`;
  } else if (preview.kind === "text" && preview.path) {
    label = `Proposed content${path}`;
  } else {
    label = "Preview"; // a tool's arguments or a fetch: no file is named
  }
  wrap.append(el("span", "label", label));
  const block = el("pre", "code");
  if (preview.kind === "diff") {
    for (const line of preview.text.split("\n")) {
      block.append(el("span", `line ${diffLineClass(line)}`, line || " "));
    }
  } else {
    block.textContent = preview.text;
  }
  wrap.append(block);
  if (TRUNCATED_PREVIEW.test(preview.text)) {
    wrap.append(el("p", "truncated", "Preview truncated: the rest of it is not shown. Read the details before you allow this."));
  }
  return wrap;
}

export class ChatView {
  constructor(doc = document) {
    this.doc = doc;
    this.chat = doc.getElementById("chat");
    this.notice = doc.getElementById("notice");
    this.status = doc.getElementById("status");
    this.workspace = doc.getElementById("workspace");
    this.prompt = doc.getElementById("prompt");
    this.send = doc.getElementById("send");
    this.cancel = doc.getElementById("cancel");
    this.usage = doc.getElementById("usage");
    this.dialog = doc.getElementById("approval");
    this.approvalTitle = doc.getElementById("approval-title");
    this.approvalPreview = doc.getElementById("approval-preview");
    this.approvalOptions = doc.getElementById("approval-options");

    this.streams = new Map();   // messageId -> { el, body } for a run of text or thought
    this.tools = new Map();     // toolCallId -> { card, status, output }
    this.approval = null;       // the resolver of the open approval dialog
    this.context = null;        // { used, size } from the last usage_update
    this.lastTurnTokens = null; // completion tokens of the last turn
    this.dead = false;          // the link is gone: nothing can be sent, so the controls stay off

    this.dialog.addEventListener("cancel", (event) => {
      event.preventDefault(); // Esc refuses the call instead of closing the dialog silently
      this.finishApproval({ outcome: "cancelled" });
    });
  }

  setStatus(text, state = "") {
    this.status.textContent = text;
    this.status.className = `status ${state}`.trim();
  }

  showNotice(text) {
    this.notice.textContent = text;
    this.notice.hidden = !text;
  }

  ready(cwd) {
    this.workspace.textContent = cwd;
    this.setStatus("Connected", "ok");
    this.prompt.disabled = false;
    this.send.disabled = false;
    this.prompt.focus();
  }

  disconnected(reason) {
    this.dead = true;
    this.setStatus("Disconnected", "bad");
    this.showNotice(`${reason} Reopen the address that wildcat-code-agent printed, then try again.`);
    this.prompt.disabled = true;
    this.send.disabled = true;
    this.cancel.disabled = true;
    this.closeApproval();
  }

  // Empty the conversation before a saved session is replayed (or a new chat starts).
  reset() {
    this.chat.replaceChildren();
    this.streams.clear();
    this.tools.clear();
    this.context = null;
    this.lastTurnTokens = null;
    this._renderUsage();
    this.showNotice("");
  }

  setBusy(busy) {
    if (this.dead) {
      return; // a late answer after a disconnect must not bring the controls back
    }
    this.doc.body.classList.toggle("busy", busy); // CSS disables the .lockable controls while a turn runs
    this.send.disabled = busy;
    this.prompt.disabled = busy;
    this.cancel.disabled = !busy;
    this.setStatus(busy ? "Working..." : "Connected", busy ? "" : "ok");
  }

  addUser(text) {
    const node = el("div", "msg user");
    node.append(el("span", "label", "You"), el("div", "text", text));
    this._append(node);
  }

  addSystem(text) {
    this._append(el("div", "msg system", text));
  }

  addError(text) {
    const node = el("div", "msg error");
    node.append(el("span", "label", "Error"), el("div", "text", text));
    this._append(node);
  }

  // A session/update notification.
  update(update) {
    if (!update || typeof update !== "object") {
      return;
    }
    switch (update.sessionUpdate) {
      case "agent_message_chunk":
        this._streamText("message", update.messageId, update.content);
        break;
      case "agent_thought_chunk":
        this._streamText("thought", update.messageId, update.content);
        break;
      case "user_message_chunk": // a saved message replayed by session/load
        this._userText(update.messageId, update.content);
        break;
      case "tool_call":
        this._toolCall(update);
        break;
      case "tool_call_update":
        this._toolUpdate(update);
        break;
      case "usage_update":
        this.context = { used: update.used, size: update.size };
        this._renderUsage();
        break;
      default:
        break; // an update this UI does not show
    }
  }

  // A session/prompt answer: the turn is over.
  turnEnded(result) {
    if (this.dead) {
      return;
    }
    const reason = result && result.stopReason;
    this.addSystem(STOP_LABELS[reason] || `Turn ended (${reason}).`);
    const tokens = result && result._meta && result._meta.usage;
    this.lastTurnTokens = tokens ? tokens.completionTokens : null;
    this.addWarnings(result && result._meta && result._meta.warnings);
    this._renderUsage();
    this.setBusy(false);
  }

  // Warnings the agent sends with an answer (for example, the chat could not be saved). Shown as text.
  addWarnings(warnings) {
    for (const text of Array.isArray(warnings) ? warnings : []) {
      const node = el("div", "msg warning");
      node.append(el("span", "label", "Warning"), el("div", "text", String(text)));
      this._append(node);
    }
  }

  // After a disconnect the disconnect notice already says what happened, so there is no second message.
  turnFailed(error) {
    if (this.dead) {
      return;
    }
    this.addError(error && error.message ? error.message : String(error));
    this.addWarnings(error && error.data && error.data.warnings);
    this.setBusy(false);
  }

  // session/request_permission: show the dialog and resolve with the result the agent expects.
  askPermission(params) {
    this.closeApproval();
    const call = (params && params.toolCall) || {};
    this.approvalTitle.textContent = call.title || "Allow this tool call?";
    const meta = call._meta && call._meta.preview; // ACP's extension point, see docs/protocol.md
    this.approvalPreview.replaceChildren(previewElement(meta));
    this.approvalOptions.replaceChildren();
    const options = Array.isArray(params && params.options) ? params.options : [];
    const outcome = new Promise((resolve) => {
      this.approval = { resolve };
    });
    let focusTarget = null;
    for (const option of options) {
      const button = el("button", APPROVE_KINDS.has(option.kind) ? "primary" : "", option.name || option.optionId);
      button.type = "button";
      button.addEventListener("click", () => this.finishApproval({ outcome: "selected", optionId: option.optionId }));
      this.approvalOptions.append(button);
      if (option.kind === "reject_once" || focusTarget === null) {
        focusTarget = button; // the refusal gets the focus, so Enter does not approve by accident
      }
    }
    if (!this.dialog.open) {
      this.dialog.showModal();
    }
    if (focusTarget) {
      focusTarget.focus();
    }
    return outcome.then((answer) => ({ outcome: answer }));
  }

  finishApproval(outcome) {
    const pending = this.approval;
    if (!pending) {
      return;
    }
    this.approval = null;
    if (this.dialog.open) {
      this.dialog.close();
    }
    this.approvalOptions.replaceChildren();
    pending.resolve(outcome);
  }

  // The dialog closes without an answer when the turn is cancelled or the link goes away.
  closeApproval() {
    this.finishApproval({ outcome: "cancelled" });
  }

  _append(node) {
    this.chat.append(node);
    node.scrollIntoView({ block: "nearest" });
  }

  // A replayed user message: one bubble per messageId, the same look as a message sent now.
  _userText(messageId, content) {
    const text = content && typeof content.text === "string" ? content.text : "";
    const key = `user:${messageId}`;
    let body = this.streams.get(key);
    if (!body) {
      const node = el("div", "msg user");
      node.append(el("span", "label", "You"));
      body = el("div", "text");
      node.append(body);
      this.streams.set(key, body);
      this._append(node);
    }
    body.append(this.doc.createTextNode(text));
  }

  _streamText(kind, messageId, content) {
    const text = content && typeof content.text === "string" ? content.text : "";
    const key = `${kind}:${messageId}`;
    let stream = this.streams.get(key);
    if (!stream) {
      stream = {};
      if (kind === "thought") {
        const details = el("details", "msg thought");
        const summary = el("summary", "", "Thinking");
        stream.body = el("div", "text");
        details.append(summary, stream.body);
        stream.el = details;
      } else {
        stream.el = el("div", "msg agent");
        stream.el.append(el("span", "label", "wildcat-code-agent"));
        stream.body = el("div", "text");
        stream.el.append(stream.body);
      }
      this.streams.set(key, stream);
      this._append(stream.el);
    }
    stream.body.append(this.doc.createTextNode(text));
    stream.el.scrollIntoView({ block: "nearest" });
  }

  _toolCall(update) {
    const id = String(update.toolCallId);
    if (this.tools.has(id)) {
      return;
    }
    const card = el("article", "tool status-pending");
    const head = el("div", "tool-head");
    head.append(
      el("div", "tool-title", update.title || "tool"),
      el("span", "badge kind", update.kind || "other"),
      el("span", "badge state", "pending"),
    );
    const output = el("pre", "tool-output");
    card.append(head, output);
    this.tools.set(id, { card, state: head.querySelector(".state"), output, status: "pending", text: "" });
    this._append(card);
  }

  _toolUpdate(update) {
    const tool = this.tools.get(String(update.toolCallId));
    if (!tool) {
      return;
    }
    if (TOOL_STATUSES.has(update.status)) {
      tool.status = update.status;
      tool.card.className = `tool status-${update.status}`;
      tool.state.textContent = update.status === "in_progress" ? "running" : update.status;
    }
    let added = false;
    for (const item of Array.isArray(update.content) ? update.content : []) {
      if (item && item.type === "content" && item.content && typeof item.content.text === "string") {
        tool.text += item.content.text;
        added = true;
      }
    }
    if (added) {
      if (tool.text.length > MAX_OUTPUT_CHARS) {
        tool.text = "...[earlier output not shown]\n" + tool.text.slice(-MAX_OUTPUT_CHARS);
      }
      tool.output.textContent = tool.text;
    }
  }

  _renderUsage() {
    const parts = [];
    if (this.context) {
      parts.push(`Context ${this.context.used} of ${this.context.size} tokens`);
    }
    if (this.lastTurnTokens !== null && this.lastTurnTokens !== undefined) {
      parts.push(`last reply ${this.lastTurnTokens} tokens`);
    }
    this.usage.textContent = parts.join(" · ");
  }
}
