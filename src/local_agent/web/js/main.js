// Start-up of the web UI: read the token from the fragment, check it with /info, open the WebSocket, run
// initialize and session/new in the server's workspace, then wire the composer, the model picker, the session
// list and the settings form.
//
// The token is in the URL fragment (`/#token=...`). A browser never sends a fragment to the server, so it is
// not in any request line or log. The page reads it once, removes it from the address bar, and keeps it in
// memory only (no storage). Reloading the page therefore needs the printed link again.

import { AcpClient } from "./acp.js";
import { ChatView } from "./chat.js";
import { AttachmentList, MENTION_DEBOUNCE_MS, mentionAt, promptBlocks, removeMention } from "./attachments.js";
import { fillSettings, readSettings, renderModelOption, renderSessionList } from "./panels.js";
import { connectHostLink, connectWebSocket } from "./transport.js";

const PROTOCOL_VERSION = 1;
const LIST_PAGE = "session/list";
const MODEL_OPTION = "model";
const EXT_SETTINGS_GET = "_local-agent/settings/get";
const EXT_SETTINGS_SET = "_local-agent/settings/set";

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

// The browser (and pywebview) link: the T15 WebSocket, with the token. Resolves with {link, cwd}: no client
// capabilities, so the agent keeps its own file and shell tools (T20 advertises them only from the VS Code host).
async function openBrowserLink(token) {
  const info = await fetchInfo(token);
  if (info.protocolVersion !== PROTOCOL_VERSION) {
    throw new Error(`local-agent speaks protocol ${info.protocolVersion}; this page needs ${PROTOCOL_VERSION}.`);
  }
  const link = await connectWebSocket(socketUrl(token));
  return { link, cwd: info.cwd };
}

// The page's state: the connection, the open session and the panels. The screen is in ChatView.
class App {
  constructor(view, doc) {
    this.view = view;
    this.doc = doc;
    this.client = null;
    this.cwd = "";
    this.sessionId = null;
    this.currentModel = "";
    this.busy = false;
    this.dead = false;
    this.link = null;          // the host link (VS Code), or null in the browser: no editor context there
    this.attachments = new AttachmentList();
    this.mention = null;       // the @mention being typed, or null
    this.mentionTimer = null;
    this.mentionSearch = 0;    // the id of the last file search asked for
    this.nextCursor = null;
    this.items = [];
    this.canList = true;
    this.model = doc.getElementById("model");
    this.drawer = doc.getElementById("drawer");
    this.sessionsToggle = doc.getElementById("sessions-toggle");
    this.sessionList = doc.getElementById("session-list");
    this.sessionsEmpty = doc.getElementById("sessions-empty");
    this.sessionsMore = doc.getElementById("sessions-more");
    this.newChatButton = doc.getElementById("new-chat");
    this.settingsOpen = doc.getElementById("settings-open");
    this.settings = doc.getElementById("settings");
    this.settingsForm = doc.getElementById("settings-form");
    this.settingsFile = doc.getElementById("settings-file");
    this.settingsMessage = doc.getElementById("settings-message");
  }

  // Lock or unlock the controls that must not change while a turn runs or the link is gone.
  lock(locked) {
    const off = locked || this.dead;
    for (const node of this.doc.querySelectorAll(".lockable")) {
      node.disabled = off;
    }
    this.model.disabled = off || !this.model.options.length;
    this.settingsOpen.disabled = this.dead || !this.sessionId;
  }

  // `open` returns {link, cwd, clientCapabilities}: openBrowserLink (browser, pywebview) or connectHostLink (VS Code).
  async connect(open) {
    const { link, cwd, clientCapabilities } = await open();
    // A host-side send error (the link stays open) is shown in the chat, the same way a failed turn is.
    link.onError = (text) => this.view.addError(text);
    this.link = link;
    link.onHost = (message) => this.onHostMessage(message);
    this.client = new AcpClient(link);
    this.client.onUpdate = (update) => this.view.update(update);
    this.client.onPermission = (params) => this.view.askPermission(params);
    this.client.onClose = (reason) => {
      this.dead = true;
      this.view.disconnected(reason);
      this.lock(true);
    };
    const init = await this.client.request("initialize", {
      protocolVersion: PROTOCOL_VERSION,
      clientCapabilities: clientCapabilities || {},
    });
    if (init.protocolVersion !== PROTOCOL_VERSION) {
      throw new Error(`The agent answered protocol ${init.protocolVersion}; this page needs ${PROTOCOL_VERSION}.`);
    }
    this.canList = Boolean(init.agentCapabilities && init.agentCapabilities.sessionCapabilities &&
      init.agentCapabilities.sessionCapabilities.list);
    this.cwd = cwd;
    this.view.workspace.textContent = cwd;
    return this.client;
  }

  // Editor messages (T21): a selection to attach, the active file, and the answer to an @file search.
  onHostMessage(message) {
    if (message.type === "attach") {
      this.attachments.add(message.attachment);
      this.renderAttachments();
      this.view.prompt.focus();
    } else if (message.type === "active") {
      this.attachments.setActive(message.file);
      this.renderAttachments();
    } else if (message.type === "files" && message.id === this.mentionSearch && this.mention) {
      this.view.showMentions(message.files);
    }
  }

  renderAttachments() {
    this.view.renderAttachments(this.attachments, {
      onToggle: (on) => {
        this.attachments.setActiveOn(on);
      },
      onRemove: (id) => {
        this.attachments.remove(id);
        this.renderAttachments();
      },
    });
  }

  // The @file mention under the caret: after a short pause the host is asked for files matching it.
  updateMention() {
    if (!this.link || typeof this.link.findFiles !== "function") {
      return; // the browser has no editor to search
    }
    const view = this.view;
    this.mention = mentionAt(view.prompt.value, view.prompt.selectionStart);
    clearTimeout(this.mentionTimer);
    if (!this.mention) {
      view.hideMentions();
      return;
    }
    const query = this.mention.query;
    this.mentionTimer = setTimeout(() => {
      this.mentionSearch += 1;
      this.link.findFiles(this.mentionSearch, query);
    }, MENTION_DEBOUNCE_MS);
  }

  // The file picked from the @ list replaces the mention with an attachment.
  pickMention(file) {
    const view = this.view;
    const mention = mentionAt(view.prompt.value, view.prompt.selectionStart);
    if (!mention) {
      return;
    }
    const next = removeMention(view.prompt.value, mention);
    view.prompt.value = next.text;
    view.prompt.setSelectionRange(next.caret, next.caret);
    this.attachments.add(file);
    this.mention = null;
    view.hideMentions();
    this.renderAttachments();
    view.prompt.focus();
  }

  // Apply a session/new or session/load answer: the model picker and the warnings it carries.
  applySession(result) {
    const option = Array.isArray(result.configOptions)
      ? result.configOptions.find((item) => item.id === MODEL_OPTION) : null;
    renderModelOption(this.model, option);
    this.currentModel = option ? option.currentValue : "";
    const warnings = result._meta && Array.isArray(result._meta.warnings) ? result._meta.warnings : [];
    this.view.showNotice(warnings.join(" "));
  }

  async newChat() {
    if (this.busy || this.dead) {
      return;
    }
    const result = await this.client.request("session/new", { cwd: this.cwd, mcpServers: [] });
    this.sessionId = result.sessionId;
    this.view.reset();
    this.applySession(result);
    this.highlight();
    this.closeDrawer();
    this.lock(false);
    this.view.prompt.focus();
    await this.refreshSessions();
  }

  async openSession(item) {
    if (this.busy || this.dead) {
      return;
    }
    this.view.reset();
    this.view.showNotice(`Opening "${item.title || "Untitled chat"}"...`);
    this.lock(true);
    try {
      const result = await this.client.request("session/load", {
        sessionId: item.sessionId, cwd: item.cwd, mcpServers: [],
      });
      this.sessionId = item.sessionId;
      this.applySession(result);
      this.highlight();
      this.closeDrawer(); // on a phone the list covers the chat; choosing a chat closes it
    } catch (error) {
      this.view.showNotice("");
      this.view.addError(error.message || String(error));
    } finally {
      this.lock(false);
      this.view.prompt.focus();
    }
  }

  closeDrawer() {
    this.drawer.hidden = true;
    this.sessionsToggle.setAttribute("aria-expanded", "false");
  }

  highlight() {
    for (const button of this.sessionList.querySelectorAll(".session-item")) {
      const active = button.dataset.sessionId === this.sessionId;
      button.classList.toggle("active", active);
      if (active) {
        button.setAttribute("aria-current", "true");
      } else {
        button.removeAttribute("aria-current");
      }
    }
  }

  async refreshSessions(cursor) {
    if (!this.canList) {
      this.drawer.hidden = true;
      this.sessionsToggle.hidden = true;
      return;
    }
    const params = cursor ? { cursor } : {};
    const result = await this.client.request(LIST_PAGE, params);
    this.items = cursor ? this.items.concat(result.sessions) : result.sessions;
    this.nextCursor = result.nextCursor || null;
    this.renderList();
  }

  renderList() {
    renderSessionList(this.sessionList, this.items, this.sessionId, (item) => this.openSession(item));
    this.sessionsEmpty.hidden = this.items.length > 0;
    this.sessionsMore.hidden = !this.nextCursor;
    this.lock(this.busy);
  }

  async changeModel() {
    if (this.busy || this.dead || !this.sessionId) {
      return;
    }
    const value = this.model.value;
    try {
      const result = await this.client.request("session/set_config_option", {
        sessionId: this.sessionId, configId: MODEL_OPTION, value,
      });
      this.applySession(result); // the new choice, and the tools warning if the model has no tools
    } catch (error) {
      this.model.value = this.currentModel; // the session keeps the model it had
      this.view.addError(error.message || String(error));
    }
  }

  async openSettings() {
    this.settingsMessage.hidden = true;
    try {
      const result = await this.client.request(EXT_SETTINGS_GET, {});
      fillSettings(this.settingsForm, result.values, result.file);
      this.settingsFile.textContent = `File: ${result.file}${result.exists ? "" : " (not created yet)"}`;
      this.settings.showModal();
    } catch (error) {
      this.view.addError(error.message || String(error));
    }
  }

  async saveSettings(event) {
    event.preventDefault();
    this.settingsMessage.hidden = true;
    try {
      const result = await this.client.request(EXT_SETTINGS_SET, { values: readSettings(this.settingsForm) });
      fillSettings(this.settingsForm, result.values, result.file);
      this.settingsFile.textContent = `File: ${result.file}`;
      this.showSettingsMessage("Saved. The new settings take effect when local-agent starts again.", false);
    } catch (error) {
      this.showSettingsMessage(error.message || String(error), true);
    }
  }

  showSettingsMessage(text, isError) {
    this.settingsMessage.textContent = text;
    this.settingsMessage.className = isError ? "notice error-text" : "notice ok-text";
    this.settingsMessage.hidden = false;
  }

  wireComposer() {
    const form = this.doc.getElementById("composer");
    const view = this.view;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = view.prompt.value.trim();
      if ((!text && this.attachments.isEmpty()) || view.send.disabled) {
        return;
      }
      const blocks = promptBlocks(text, this.attachments);
      const attached = this.attachments.labels();
      view.prompt.value = "";
      view.hideMentions();
      this.mention = null;
      view.showNotice("");
      view.addUser(attached.length ? `${text}
[attached: ${attached.join(", ")}]`.trim() : text);
      this.attachments.clearChips();
      this.renderAttachments();
      this.busy = true;
      view.setBusy(true);
      this.lock(true);
      try {
        const result = await this.client.request("session/prompt", {
          sessionId: this.sessionId,
          prompt: blocks,
        });
        view.turnEnded(result);
      } catch (error) {
        view.turnFailed(error);
      } finally {
        this.busy = false;
        this.lock(false);
        if (!this.dead) {
          this.refreshSessions().catch(() => {});
        }
        view.prompt.focus();
      }
    });
    view.onPick = (file) => this.pickMention(file);
    view.prompt.addEventListener("input", () => this.updateMention());
    view.prompt.addEventListener("keydown", (event) => {
      if (view.mentionsOpen()) {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          view.moveMention(event.key === "ArrowDown" ? 1 : -1);
          return;
        }
        if (event.key === "Escape") {
          event.preventDefault();
          view.hideMentions();
          return;
        }
        if ((event.key === "Enter" && !event.shiftKey && !event.isComposing) || event.key === "Tab") {
          const chosen = view.chosenMention();
          if (chosen) {
            event.preventDefault();
            this.pickMention(chosen);
            return;
          }
        }
      }
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
    view.cancel.addEventListener("click", () => {
      view.closeApproval();
      try {
        this.client.notify("session/cancel", { sessionId: this.sessionId });
      } catch {
        // The connection is gone; the disconnected message already says so.
      }
    });
  }

  wirePanels() {
    this.model.addEventListener("change", () => this.changeModel());
    this.sessionsToggle.addEventListener("click", () => {
      this.drawer.hidden = !this.drawer.hidden;
      this.sessionsToggle.setAttribute("aria-expanded", String(!this.drawer.hidden));
    });
    this.newChatButton.addEventListener("click", () => {
      this.newChat().catch((error) => this.view.addError(error.message || String(error)));
    });
    this.sessionsMore.addEventListener("click", () => {
      this.refreshSessions(this.nextCursor).catch((error) => this.view.addError(error.message || String(error)));
    });
    this.settingsOpen.addEventListener("click", () => this.openSettings());
    this.doc.getElementById("settings-close").addEventListener("click", () => this.settings.close());
    this.settingsForm.addEventListener("submit", (event) => this.saveSettings(event));
  }

  async start(open) {
    try {
      await this.connect(open);
    } catch (error) {
      this.view.setStatus("Not connected", "bad");
      this.view.showNotice(error.message);
      return;
    }
    this.wirePanels();
    this.wireComposer();
    try {
      const result = await this.client.request("session/new", { cwd: this.cwd, mcpServers: [] });
      this.sessionId = result.sessionId;
      this.applySession(result);
    } catch (error) {
      this.view.setStatus("Not connected", "bad");
      this.view.showNotice(error.message);
      return;
    }
    this.view.ready(this.cwd);
    this.lock(false);
    this.refreshSessions().catch((error) => this.view.showNotice(error.message || String(error)));
  }
}

export function readToken(hash) {
  const token = tokenFromFragment(hash);
  if (hash) {
    history.replaceState(null, "", location.pathname + location.search); // the token leaves the address bar
  }
  return token;
}

const view = new ChatView(document);
const app = new App(view, document);
// In a VS Code webview acquireVsCodeApi exists and there is no token: the extension host is the link (T19).
const vscodeApi = typeof acquireVsCodeApi === "function" ? acquireVsCodeApi() : null;
if (vscodeApi) {
  app.start(() => connectHostLink(vscodeApi));
} else {
  const token = readToken(location.hash);
  if (!token) {
    view.setStatus("No token", "bad");
    view.showNotice("This page needs the token. Open the link that local-agent printed (it ends in #token=...).");
  } else {
    app.start(() => openBrowserLink(token));
  }
}
