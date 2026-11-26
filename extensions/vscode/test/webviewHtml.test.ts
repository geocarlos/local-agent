import * as assert from "node:assert/strict";
import { test } from "node:test";
import { contentSecurityPolicy, nonceFrom, renderWebviewPage } from "../src/webviewHtml";

const INDEX = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Wildcat Code Agent</title>
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <script type="module" src="/js/main.js"></script>
</body>
</html>`;

const CSP = "vscode-webview://abc";

function page(): string {
  return renderWebviewPage(INDEX, {
    nonce: "N0nce",
    cspSource: CSP,
    assetUri: (path) => `https://file.example/${path}`,
  });
}

test("root-relative links and the module script become webview URIs", () => {
  const html = page();
  assert.ok(html.includes('href="https://file.example/favicon.svg"'));
  assert.ok(html.includes('href="https://file.example/style.css"'));
  assert.ok(html.includes('src="https://file.example/js/main.js"'));
  assert.ok(!html.includes('href="/'));
  assert.ok(!html.includes('src="/'));
});

test("the script carries the nonce, and the policy allows only that nonce and the webview source", () => {
  const html = page();
  assert.ok(html.includes('<script nonce="N0nce" type="module" src='));
  assert.ok(html.includes(`content="${contentSecurityPolicy("N0nce", CSP)}"`));
  const csp = contentSecurityPolicy("N0nce", CSP);
  assert.ok(csp.includes("default-src 'none'"));
  assert.ok(csp.includes("script-src 'nonce-N0nce' vscode-webview://abc"));
  assert.ok(!csp.includes("unsafe-inline"));
  assert.ok(!csp.includes("unsafe-eval"));
  assert.ok(csp.includes("connect-src 'none'"));
});

test("the CSP meta sits in the head, right after the charset", () => {
  const html = page();
  const charset = html.indexOf('<meta charset="utf-8">');
  const meta = html.indexOf('http-equiv="Content-Security-Policy"');
  const title = html.indexOf("<title>");
  assert.ok(charset >= 0 && meta > charset && meta < title);
});

test("nonceFrom makes base64-safe text from random bytes", () => {
  const nonce = nonceFrom(Uint8Array.from([255, 254, 253, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]));
  assert.match(nonce, /^[A-Za-z0-9]+$/);
  assert.ok(nonce.length > 0);
});
