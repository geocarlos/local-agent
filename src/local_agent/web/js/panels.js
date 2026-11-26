// The side panels: the model picker, the session list (drawer) and the settings form. Like chat.js, every piece of
// text comes from the server, so it goes in with textContent, never innerHTML.

import { el } from "./chat.js";

// The labels of the editable settings in the form (the keys are the config.toml names; see config_write.py).
export const SETTING_KEYS = [
  "model", "host", "num_ctx", "max_tool_rounds", "max_output_chars",
  "command_timeout_s", "network_timeout_s", "tool_profile",
];
const INTEGER_KEYS = new Set(["num_ctx", "max_tool_rounds", "max_output_chars", "command_timeout_s", "network_timeout_s"]);

// Fill the model <select> from the model option of session/new or session/set_config_option.
export function renderModelOption(select, option) {
  if (!option) {
    select.replaceChildren();
    select.disabled = true;
    return;
  }
  const choices = Array.isArray(option.options) ? option.options : [];
  select.replaceChildren(...choices.map((choice) => {
    const node = el("option", "", choice.name || choice.value);
    node.value = choice.value;
    return node;
  }));
  select.value = option.currentValue;
  select.disabled = false;
}

// The session list: one button per saved chat, the active one marked. Titles are text nodes.
export function renderSessionList(list, items, activeId, onOpen) {
  list.replaceChildren();
  for (const item of items) {
    const button = el("button", "session-item", item.title || "Untitled chat");
    button.type = "button";
    button.dataset.sessionId = item.sessionId;
    button.classList.add("lockable");
    if (item.sessionId === activeId) {
      button.classList.add("active");
      button.setAttribute("aria-current", "true");
    }
    const meta = el("span", "session-meta", `${formatTime(item.updatedAt)} · ${folderName(item.cwd)}`);
    button.append(meta);
    button.addEventListener("click", () => onOpen(item));
    const li = el("li");
    li.append(button);
    list.append(li);
  }
}

export function formatTime(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}

// The last part of a folder path, for a short label (both separators).
export function folderName(path) {
  const parts = String(path || "").split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path || "");
}

// Put the values from settings/get into the form.
export function fillSettings(form, values, file) {
  form.elements.namedItem("model").value = values.model;
  for (const key of SETTING_KEYS) {
    if (key === "model") {
      continue;
    }
    const field = form.elements.namedItem(key);
    if (field) {
      field.value = String(values[key]);
    }
  }
  form.dataset.file = file;
}

// The values of the form, as the server takes them. Integers are sent as numbers; text is trimmed.
export function readSettings(form) {
  const values = {};
  for (const key of SETTING_KEYS) {
    const field = form.elements.namedItem(key);
    if (!field) {
      continue;
    }
    const raw = String(field.value).trim();
    values[key] = INTEGER_KEYS.has(key) ? Number(raw) : raw;
  }
  return values;
}
