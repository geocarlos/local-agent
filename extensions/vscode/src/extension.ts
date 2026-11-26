// The VS Code side: a command that opens a webview panel. The panel shows the T16 web bundle (copied to webview/
// at build time). The extension host starts `local-agent serve --stdio` and relays its JSON lines to and from the
// webview; the bundle's AcpClient does the JSON-RPC, approvals included. The host also answers the agent's fs/* and
// terminal/* requests itself (T20, editorTools.ts), so the agent's file reads, writes and commands go to the editor.
// See README "VS Code extension".

import { randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import { DIAGNOSTICS_METHOD, answerClientRequest, clientRequestOf, ClientRequest } from "./clientRouter";
import { CLIENT_CAPABILITIES, parseWebviewMessage, HostToWebview } from "./messages";
import { diagnosticsReply, SelectionAttachment } from "./attachments";
import {
  activeFileAttachment,
  diagnosticsOf,
  findWorkspaceFiles,
  selectionFromEditor,
} from "./contextTools";
import { EditorTools } from "./editorTools";
import { GuardedSender } from "./guardedSender";
import { AgentProcess, serveArgv } from "./stdioAgent";
import { nonceFrom, renderWebviewPage } from "./webviewHtml";

const VIEW_TYPE = "localAgent.chat";

let panel: vscode.WebviewPanel | undefined;
let agent: AgentProcess | undefined;
let editorTools: EditorTools | undefined;

// The open panel's link to the webview (T21). Messages for it wait in `queued` until the webview says it is ready,
// so an attachment sent just after the panel opens is not lost.
interface ChatHost {
  post: GuardedSender<HostToWebview>;
  ready: boolean;
  queued: HostToWebview[];
}
let chat: ChatHost | undefined;

function sendToChat(message: HostToWebview): void {
  if (!chat) {
    return;
  }
  if (chat.ready) {
    chat.post.send(message);
  } else {
    chat.queued.push(message);
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("local-agent");
  context.subscriptions.push(output);
  context.subscriptions.push(
    vscode.commands.registerCommand("localAgent.openChat", () => openChat(context, output)),
    vscode.commands.registerCommand("localAgent.addSelection", () => addSelection(context, output)),
    vscode.window.onDidChangeActiveTextEditor(() => sendToChat({ type: "active", file: activeFileAttachment() })),
    { dispose: () => stopAgent() },
  );
}

// "Add Selection to Chat": the selection goes into the panel's composer as an attachment; the panel opens if needed.
// Returns the attachment built (the tests read it), or undefined when nothing is selected or no folder is open.
function addSelection(context: vscode.ExtensionContext, output: vscode.OutputChannel): SelectionAttachment | undefined {
  const attachment = selectionFromEditor();
  if (!attachment) {
    void vscode.window.showInformationMessage("Select some text in an editor first.");
    return undefined;
  }
  openChat(context, output);
  sendToChat({ type: "attach", attachment });
  return attachment;
}

export function deactivate(): void {
  stopAgent();
  editorTools?.dispose();
  editorTools = undefined;
}

function openChat(context: vscode.ExtensionContext, output: vscode.OutputChannel): void {
  if (panel) {
    panel.reveal(vscode.ViewColumn.Beside);
    return;
  }
  const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  if (!folder) {
    void vscode.window.showErrorMessage("local-agent works in a workspace folder. Open a folder first.");
    return;
  }
  const bundle = vscode.Uri.joinPath(context.extensionUri, "webview");
  const current = vscode.window.createWebviewPanel(VIEW_TYPE, "local-agent", vscode.ViewColumn.Beside, {
    enableScripts: true,
    retainContextWhenHidden: true,
    localResourceRoots: [bundle],
  });
  panel = current;
  current.iconPath = vscode.Uri.joinPath(context.extensionUri, "media", "tab-icon.png");
  current.webview.html = pageHtml(context, current.webview, bundle);
  const tools = new EditorTools();
  editorTools = tools;

  // Every message to the webview goes through here; after dispose it is dropped (see guardedSender.ts).
  const post = new GuardedSender<HostToWebview>((message) => {
    void current.webview.postMessage(message);
  });
  const host: ChatHost = { post, ready: false, queued: [] };
  chat = host;
  const startAgent = () => {
    if (agent?.running) {
      return;
    }
    stopAgent();
    const command = vscode.workspace.getConfiguration("localAgent").get<string[]>("command", ["local-agent"]);
    const relay: AgentProcess = new AgentProcess({
      onMessage: (text) => {
        const request = clientRequestOf(text);
        if (request) {
          void answerAgentRequest(request, relay, tools); // the agent's file or terminal request: the host answers
          return;
        }
        post.send({ type: "message", text });
      },
      onStderr: (text) => output.append(text),
      onExit: (reason) => {
        post.send({ type: "closed", reason });
        output.appendLine(reason);
        tools.releaseAll(); // the agent's commands end with it (their terminals stay, with their output)
      },
    });
    agent = relay;
    try {
      const { program, args } = serveArgv(command, folder);
      output.appendLine(`Starting: ${program} ${args.join(" ")}`);
      relay.start(program, args, folder);
    } catch (error) {
      post.send({ type: "closed", reason: errorText(error) });
    }
  };

  current.webview.onDidReceiveMessage((data: unknown) => {
    const message = parseWebviewMessage(data);
    if (!message) {
      return;
    }
    if (message.type === "ready") {
      startAgent();
      post.send({ type: "init", cwd: folder, clientCapabilities: CLIENT_CAPABILITIES });
      host.ready = true;
      for (const queued of host.queued.splice(0)) {
        post.send(queued);
      }
      post.send({ type: "active", file: activeFileAttachment() });
    } else if (message.type === "findFiles") {
      void findWorkspaceFiles(message.query).then((files) => post.send({ type: "files", id: message.id, files }));
    } else if (message.type === "send") {
      if (!agent?.running) {
        post.send({ type: "closed", reason: "The agent is not running." });
        return;
      }
      try {
        agent.send(message.text);
      } catch (error) {
        // The agent is still running, so the link stays up; the webview shows the error and the user can retry.
        post.send({ type: "error", reason: `Could not send to the agent: ${errorText(error)}` });
      }
    }
  });
  current.onDidDispose(() => {
    post.dispose();
    if (chat === host) {
      chat = undefined;
    }
    stopAgent();
    tools.dispose(); // ends the commands the agent started and closes their terminals
    if (editorTools === tools) {
      editorTools = undefined;
    }
    if (panel === current) {
      panel = undefined;
    }
  });
}

// Answers one agent request (fs/* or terminal/*) and sends the reply back on the same agent. If the agent has
// stopped meanwhile, nothing is waiting for the reply, so a failed send is dropped.
async function answerAgentRequest(request: ClientRequest, agentProcess: AgentProcess, tools: EditorTools): Promise<void> {
  const reply = await answerClientRequest(request, async (method, params) =>
    method === DIAGNOSTICS_METHOD ? diagnosticsReply(params, diagnosticsOf) : tools.handle(method, params),
  );
  try {
    agentProcess.send(reply);
  } catch {
    // The agent is gone; see the closed message from its exit.
  }
}

function stopAgent(): void {
  agent?.detach();
  agent?.stop();
  agent = undefined;
  editorTools?.releaseAll(); // the commands the stopped agent started are ended too
}

function pageHtml(context: vscode.ExtensionContext, webview: vscode.Webview, bundle: vscode.Uri): string {
  const indexPath = path.join(context.extensionPath, "webview", "index.html");
  let indexHtml: string;
  try {
    indexHtml = fs.readFileSync(indexPath, "utf8");
  } catch {
    return `<!doctype html><body>The web bundle is missing (${indexPath}). Run "npm run compile" in extensions/vscode.</body>`;
  }
  return renderWebviewPage(indexHtml, {
    nonce: nonceFrom(randomBytes(16)),
    cspSource: webview.cspSource,
    assetUri: (file) => webview.asWebviewUri(vscode.Uri.joinPath(bundle, ...file.split("/"))).toString(),
  });
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
