// Editor context in the composer (T21): the attachment list, the @file mention, and the ACP content blocks of a
// prompt. Pure (no DOM): the extension's unit tests run it with node, and the bundle's main.js and chat.js use it.
//
// A prompt is a list of ACP content blocks (see docs/protocol.md, "Prompt content"): the typed text, then one block
// per attachment.
//   selection -> an embedded text resource, uri "<file uri>#L<start>-L<end>" (1-based, inclusive)
//   file      -> a resource_link to the file (the agent reads it through the editor, or from disk inside the workspace)
// The active editor's file is an attachment too, but it is only sent while its toggle is on.

export const MENTION_DEBOUNCE_MS = 150;

// The @mention being typed before the caret, or null. A mention starts at the start of the text or after a space,
// tab or line break, and its query has no whitespace and no second "@". Returns {query, start, end}.
export function mentionAt(text, caret) {
  const before = text.slice(0, caret);
  const match = /(^|\s)@([^\s@]*)$/.exec(before);
  if (!match) {
    return null;
  }
  return { query: match[2], start: caret - match[2].length - 1, end: caret };
}

// The text without the mention, and where the caret goes.
export function removeMention(text, mention) {
  return { text: text.slice(0, mention.start) + text.slice(mention.end), caret: mention.start };
}

// The ACP content block for one attachment.
export function attachmentBlock(attachment) {
  if (attachment.kind === "selection") {
    return {
      type: "resource",
      resource: { uri: `${attachment.uri}#L${attachment.startLine}-L${attachment.endLine}`, text: attachment.text },
    };
  }
  return { type: "resource_link", uri: attachment.uri, name: attachment.name };
}

// The label of an attachment in the composer.
export function attachmentLabel(attachment) {
  if (attachment.kind === "selection") {
    const lines = attachment.startLine === attachment.endLine
      ? `line ${attachment.startLine}`
      : `lines ${attachment.startLine}-${attachment.endLine}`;
    return `${attachment.name}, ${lines}`;
  }
  return attachment.name;
}

// The attachments of the composer. Selections and @file picks are removable chips; the active file is one toggle.
export class AttachmentList {
  constructor() {
    this.items = [];          // { id, ...attachment }: sent with the next prompt, then cleared
    this.nextId = 1;
    this.active = null;       // the active editor's file, or null
    this.activeOn = true;     // the open-file toggle: on by default
  }

  add(attachment) {
    if (attachment.kind === "file" && this.items.some((item) => item.kind === "file" && item.uri === attachment.uri)) {
      return null; // a file picked twice is one attachment
    }
    const item = { ...attachment, id: this.nextId };
    this.nextId += 1;
    this.items.push(item);
    return item;
  }

  remove(id) {
    this.items = this.items.filter((item) => item.id !== id);
  }

  setActive(file) {
    this.active = file || null;
  }

  setActiveOn(on) {
    this.activeOn = Boolean(on);
  }

  // The content blocks this list adds to a prompt: the open file first (when on), then the chips.
  blocks() {
    const out = [];
    if (this.active && this.activeOn) {
      out.push(attachmentBlock(this.active));
    }
    for (const item of this.items) {
      out.push(attachmentBlock(item));
    }
    return out;
  }

  // Called after a prompt is sent: the chips are spent; the open-file toggle stays as the user set it.
  clearChips() {
    this.items = [];
  }

  // The labels of what this prompt attaches, for the chat's record of the message.
  labels() {
    const out = [];
    if (this.active && this.activeOn) {
      out.push(attachmentLabel(this.active));
    }
    for (const item of this.items) {
      out.push(attachmentLabel(item));
    }
    return out;
  }

  // Whether the user chose an attachment on purpose (a selection or an @file pick). The open-file toggle does not count:
  // an empty composer with only the toggle on sends nothing.
  hasChips() {
    return this.items.length > 0;
  }

  isEmpty() {
    return this.items.length === 0 && !(this.active && this.activeOn);
  }
}

// The content blocks of one prompt: the typed text (when there is some), then the attachments.
export function promptBlocks(text, attachments) {
  const blocks = [];
  if (text) {
    blocks.push({ type: "text", text });
  }
  return blocks.concat(attachments.blocks());
}
