// Builds the webview page from the copied bundle's index.html: the bundle's root-relative links become webview
// URIs, every script gets the per-load nonce, and a Content-Security-Policy meta is added. Pure: the caller passes
// the URI maker (webview.asWebviewUri) and the csp source (webview.cspSource).

export interface PageOptions {
  nonce: string;
  cspSource: string;
  // Maps a bundle path such as "style.css" or "js/main.js" to a URI the webview can load.
  assetUri: (path: string) => string;
}

export function contentSecurityPolicy(nonce: string, cspSource: string): string {
  return [
    "default-src 'none'",
    `img-src ${cspSource} data:`,
    `style-src ${cspSource}`,
    `font-src ${cspSource}`,
    `script-src 'nonce-${nonce}' ${cspSource}`,
    "connect-src 'none'",
  ].join("; ");
}

export function renderWebviewPage(indexHtml: string, options: PageOptions): string {
  const { nonce, cspSource, assetUri } = options;
  const csp = `<meta http-equiv="Content-Security-Policy" content="${contentSecurityPolicy(nonce, cspSource)}">`;
  const withUris = indexHtml.replace(/(href|src)="\/([^"]*)"/g, (_match, attr: string, path: string) => `${attr}="${assetUri(path)}"`);
  const withNonces = withUris.replace(/<script(\s)/g, `<script nonce="${nonce}"$1`);
  return withNonces.replace(/(<meta charset="utf-8">)/i, `$1\n  ${csp}`);
}

// A nonce from random bytes: base64 without the characters that would need escaping.
export function nonceFrom(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64").replace(/[^A-Za-z0-9]/g, "");
}
