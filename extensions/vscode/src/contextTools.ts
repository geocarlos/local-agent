// The vscode side of the editor context (T21): the @file search, the diagnostics of a file, and the active file and
// selection as attachments. The pure parts (glob, attachments, reply shapes) are in attachments.ts.

import * as vscode from "vscode";
import {
  DiagnosticInput,
  FileAttachment,
  fileAttachment,
  mentionExclude,
  mentionGlob,
  MENTION_SEARCH_LIMIT,
  rankMentions,
  selectionAttachment,
  SelectionAttachment,
} from "./attachments";

// Workspace files whose name contains the query, best matches first, at most MENTION_LIMIT. The exclude merges the
// user's enabled files.exclude and search.exclude with node_modules and .git. A failed search gives no files.
export async function findWorkspaceFiles(query: string): Promise<FileAttachment[]> {
  try {
    const exclude = mentionExclude(
      vscode.workspace.getConfiguration("files").get<Record<string, unknown>>("exclude"),
      vscode.workspace.getConfiguration("search").get<Record<string, unknown>>("exclude"),
    );
    const uris = await vscode.workspace.findFiles(mentionGlob(query), exclude, MENTION_SEARCH_LIMIT);
    const files = uris.map((uri) => fileAttachment(uri.toString(), vscode.workspace.asRelativePath(uri, false)));
    return rankMentions(query, files);
  } catch {
    return [];
  }
}

// The diagnostics of one file (every severity), read from the editor's language services.
export function diagnosticsOf(uri: string): DiagnosticInput[] {
  let target: vscode.Uri;
  try {
    target = vscode.Uri.parse(uri).with({ fragment: "" });
  } catch {
    return [];
  }
  return vscode.languages.getDiagnostics(target).map((item) => ({
    severity: item.severity,
    message: item.message,
    source: item.source,
    range: item.range,
  }));
}

// The file of the active editor as an attachment, or null when there is no editor or it is not a file on disk.
export function activeFileAttachment(): FileAttachment | null {
  const document = vscode.window.activeTextEditor?.document;
  if (!document || document.uri.scheme !== "file") {
    return null;
  }
  return fileAttachment(document.uri.toString(), vscode.workspace.asRelativePath(document.uri, false));
}

// The active editor's selection as an attachment, or null when nothing is selected.
export function selectionFromEditor(): SelectionAttachment | null {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.selection.isEmpty) {
    return null;
  }
  const selection = editor.selection;
  const document = editor.document;
  return selectionAttachment(
    document.uri.toString(),
    vscode.workspace.asRelativePath(document.uri, false),
    selection.start.line + 1,
    selection.end.line + 1,
    document.getText(selection),
  );
}
