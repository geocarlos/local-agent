"""The web bundle inside a VS Code webview (T19), in Microsoft Edge: the same bundle as the browser, but with
`acquireVsCodeApi` (a shim here) instead of a WebSocket. The shim relays exactly what the extension host does:
- webview -> host: {type: "ready"} starts the agent and gets {type: "init", cwd} back;
  {type: "send", text} writes one line to the agent's stdin;
- host -> webview: each line the agent writes to stdout comes back as {type: "message", text}.

The agent is a real `local-agent serve --stdio` process with the scripted model (tests/fake_ollama_http.py).
The TypeScript side of the host (the spawn and the framing) is tested by `npm test` in extensions/vscode.

The page is served the way the extension serves it (extensions/vscode/src/webviewHtml.ts): the same
Content-Security-Policy meta, a per-load nonce on every script, and root-relative links turned into the webview's
origin. So a script or style the policy refuses shows up as a console error, and the test checks for it.

The tests skip when Edge or Playwright is missing, under the same rules as tests/test_web_browser.py.
"""

import functools
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fake_ollama_http import start as start_fake_ollama
from test_web_browser import ORIGINAL, EDITED, TIMEOUT_MS, edge  # noqa: F401 (the Edge fixture, shared)

playwright_api = pytest.importorskip("playwright.sync_api")

BUNDLE = Path(__file__).resolve().parents[1] / "src" / "local_agent" / "web"
PAGE_URL = "https://webview.test/index.html"
CSP_SOURCE = "https://webview.test"  # stands in for webview.cspSource
SHIM = """
window.acquireVsCodeApi = () => ({ postMessage: (message) => window.hostPost(message) });
"""


@pytest.fixture
def agent(tmp_path):
    """A real `serve --stdio` process with the scripted model. Yields its pipes and workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text(ORIGINAL, encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")  # the user's own config is never read
    fake, fake_url = start_fake_ollama()
    proc = subprocess.Popen(
        [sys.executable, "-m", "local_agent", "serve", "--stdio", "--no-plugins",
         "--host", fake_url, "--config", str(config), "--cwd", str(workspace)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        yield {"proc": proc, "workspace": workspace}
    finally:
        if proc.poll() is None:
            proc.stdin.close()  # EOF: the server finishes and exits
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()  # our own process, by its handle
                proc.wait(timeout=30)
        proc.stdout.close()
        fake.shutdown()
        fake.server_close()


class Host:
    """The extension host's part, played in the test: it relays the agent's lines into the page and the page's
    messages into the agent. Playwright's sync API runs on this thread only, so the reader thread only queues."""

    def __init__(self, page, proc, workspace):
        self.page = page
        self.proc = proc
        self.workspace = workspace
        self.outbox: "queue.Queue[dict]" = queue.Queue()  # host -> page messages, delivered by pump()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.sent: list[str] = []

    def _read(self):
        for raw in self.proc.stdout:
            text = raw.decode("utf-8", "replace").rstrip("\n")
            self.outbox.put({"type": "message", "text": text})

    def to_page(self, message):
        self.page.evaluate("(data) => window.dispatchEvent(new MessageEvent('message', { data }))", message)

    def from_page(self, message):  # exposed to the page as window.hostPost; runs inside a Playwright call
        kind = message.get("type")
        if kind == "ready":
            self.outbox.put({"type": "init", "cwd": str(self.workspace)})
        elif kind == "send":
            self.sent.append(message["text"])
            self.proc.stdin.write((message["text"] + "\n").encode("utf-8"))
            self.proc.stdin.flush()

    def pump(self):
        while True:
            try:
                message = self.outbox.get_nowait()
            except queue.Empty:
                return
            self.to_page(message)

    def wait_until(self, check, what, timeout_s=TIMEOUT_MS / 1000):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.pump()
            if check():
                return
            self.page.wait_for_timeout(50)
        raise AssertionError(f"timed out waiting for {what}")


def webview_csp(nonce):
    """The policy webviewHtml.ts builds (contentSecurityPolicy), with the test's origin as the webview source."""
    return "; ".join([
        "default-src 'none'",
        f"img-src {CSP_SOURCE} data:",
        f"style-src {CSP_SOURCE}",
        f"font-src {CSP_SOURCE}",
        f"script-src 'nonce-{nonce}' {CSP_SOURCE}",
        "connect-src 'none'",
    ])


def render_page(index_html, nonce):
    """The page renderWebviewPage makes: links to the origin, a nonce on each script, the CSP meta after charset."""
    with_uris = re.sub(r'(href|src)="/([^"]*)"', lambda m: f'{m.group(1)}="{CSP_SOURCE}/{m.group(2)}"', index_html)
    with_nonces = re.sub(r"<script(\s)", lambda m: f'<script nonce="{nonce}"{m.group(1)}', with_uris)
    meta = f'<meta http-equiv="Content-Security-Policy" content="{webview_csp(nonce)}">'
    return re.sub(r'(<meta charset="utf-8">)', lambda m: f"{m.group(1)}\n  {meta}", with_nonces, count=1, flags=re.I)


def open_bundle(edge_browser, agent):
    page = edge_browser.new_page()
    errors: list[str] = []
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    nonce = secrets.token_hex(16)  # per load, as the extension makes it
    page.route("https://webview.test/**", functools.partial(serve_bundle, nonce=nonce))
    host = Host(page, agent["proc"], agent["workspace"])
    page.expose_function("hostPost", host.from_page)
    page.add_init_script(SHIM)
    page.goto(PAGE_URL)
    host.wait_until(lambda: page.locator("#status").inner_text() == "Connected", "the page to connect")
    # The policy is in the page, so the CSP error check below is a real check: the page works under it.
    assert page.locator('meta[http-equiv="Content-Security-Policy"]').count() == 1
    return page, host, errors


def serve_bundle(route, nonce):
    name = route.request.url.split("webview.test/", 1)[1].split("?")[0] or "index.html"
    path = BUNDLE / name
    if not path.is_file() or ".." in name:
        route.fulfill(status=404, body="not found")
        return
    if name == "index.html":
        html = render_page(path.read_text(encoding="utf-8"), nonce)
        route.fulfill(status=200, body=html, content_type="text/html")
        return
    content_type = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
    route.fulfill(path=str(path), content_type=content_type.get(path.suffix, "application/octet-stream"))


def test_the_bundle_chats_through_the_host_link(edge, agent):
    page, host, errors = open_bundle(edge, agent)
    try:
        assert page.locator("#workspace").inner_text() == str(agent["workspace"])
        page.fill("#prompt", "Say hello")
        page.click("#send")
        host.wait_until(lambda: "Hello from the scripted model." in page.locator("#chat").inner_text(),
                        "the streamed reply")
        host.wait_until(lambda: page.locator("#send").is_enabled(), "the composer to be ready again")
    finally:
        page.close()
    assert any('"session/prompt"' in text for text in host.sent), host.sent
    assert not [e for e in errors if "Content Security Policy" in e], errors


def test_an_approved_core_side_write_is_answered_through_the_host_link(edge, agent):
    page, host, errors = open_bundle(edge, agent)
    try:
        page.fill("#prompt", "EDIT hello.txt")
        page.click("#send")
        dialog = page.locator("#approval")
        host.wait_until(lambda: dialog.is_visible(), "the approval dialog")
        assert "hello.txt" in page.locator("#approval-title").inner_text()
        assert "Allow once" in page.locator("#approval-options button").all_inner_texts()

        page.locator("#approval-options button", has_text="Allow once").click()
        host.wait_until(lambda: dialog.is_hidden(), "the dialog to close")
        host.wait_until(lambda: page.locator(".tool.status-completed").count() == 1, "the tool card to complete")
        host.wait_until(lambda: "The edit is done" in page.locator("#chat").inner_text(), "the final reply")
    finally:
        page.close()
    assert (agent["workspace"] / "hello.txt").read_text(encoding="utf-8") == EDITED
    assert not [e for e in errors if "Content Security Policy" in e], errors
