// The VS Code side: a command that opens a webview panel. The panel shows the T16 web bundle (copied to webview/
// at build time). The extension host starts `local-agent serve --stdio` and relays its JSON lines to and from the
// webview; the bundle's AcpClient does the JSON-RPC, approvals included. See README "VS Code extension".

import { randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import { parseWebviewMessage, HostToWebview } from "./messages";
import { GuardedSender } from "./guardedSender";
import { AgentProcess, serveArgv } from "./stdioAgent";
import { nonceFrom, renderWebviewPage } from "./webviewHtml";

const VIEW_TYPE = "localAgent.chat";

let panel: vscode.WebviewPanel | undefined;
let agent: AgentProcess | undefined;

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("local-agent");
  context.subscriptions.push(output);
  context.subscriptions.push(
    vscode.commands.registerCommand("localAgent.openChat", () => openChat(context, output)),
    { dispose: () => stopAgent() },
  );
}

export function deactivate(): void {
  stopAgent();
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
  current.webview.html = pageHtml(context, current.webview, bundle);

  // Every message to the webview goes through here; after dispose it is dropped (see guardedSender.ts).
  const post = new GuardedSender<HostToWebview>((message) => {
    void current.webview.postMessage(message);
  });
  const startAgent = () => {
    if (agent?.running) {
      return;
    }
    stopAgent();
    const command = vscode.workspace.getConfiguration("localAgent").get<string[]>("command", ["local-agent"]);
    const relay = new AgentProcess({
      onMessage: (text) => post.send({ type: "message", text }),
      onStderr: (text) => output.append(text),
      onExit: (reason) => {
        post.send({ type: "closed", reason });
        output.appendLine(reason);
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
      post.send({ type: "init", cwd: folder });
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
    stopAgent();
    if (panel === current) {
      panel = undefined;
    }
  });
}

function stopAgent(): void {
  agent?.detach();
  agent?.stop();
  agent = undefined;
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
