"""The web UI in a real browser (T16): Microsoft Edge, driven by Playwright, against a real `serve --web` process.

The model is scripted (tests/fake_ollama_http.py). The test sends a plain prompt and sees its streamed reply,
then asks for an edit, sees the approval dialog with the diff, allows it once, and checks that the file changed
on disk and that the tool card completed.

Skipped when Playwright is not installed or Edge cannot be launched (CI on windows-latest has Edge).
"""

import re
import subprocess
import sys
import threading

import pytest

from fake_ollama_http import start as start_fake_ollama

playwright_api = pytest.importorskip("playwright.sync_api")

TIMEOUT_MS = 20_000
BANNER = re.compile(r"Open this link in a browser: (http://127\.0\.0\.1:\d+/#token=\S+)")
ORIGINAL = "Hello world\n"
EDITED = "Hello there\n"


@pytest.fixture(scope="module")
def edge():
    """One Edge browser for the module. Skips the tests when Edge cannot be started."""
    playwright = playwright_api.sync_playwright().start()
    try:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
    except Exception as error:  # Playwright raises a plain Error when the channel is missing
        playwright.stop()
        pytest.skip(f"Microsoft Edge is not available to Playwright: {str(error).splitlines()[0][:200]}")
    yield browser
    browser.close()
    playwright.stop()


@pytest.fixture
def served(tmp_path):
    """A real `serve --web` process with the scripted model, and the page URL it prints (with the token)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text(ORIGINAL, encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")  # the user's own config is never read
    fake, fake_url = start_fake_ollama()
    proc = subprocess.Popen(
        [sys.executable, "-m", "local_agent", "serve", "--web", "--port", "0", "--no-plugins",
         "--host", fake_url, "--config", str(config), "--cwd", str(workspace)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    found: dict = {}
    seen = threading.Event()
    lines: list[str] = []

    def read_stderr() -> None:
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            lines.append(line)
            match = BANNER.search(line)
            if match and not seen.is_set():
                found["url"] = match.group(1)
                seen.set()

    reader = threading.Thread(target=read_stderr, daemon=True)
    reader.start()
    try:
        if not seen.wait(60):
            raise AssertionError("the server did not print its page URL: " + "\n".join(lines))
        yield {"url": found["url"], "workspace": workspace}
    finally:
        proc.kill()  # our own process, by its handle
        proc.wait(timeout=30)
        reader.join(30)
        proc.stdout.close()
        proc.stderr.close()
        fake.shutdown()
        fake.server_close()


def test_a_chat_with_an_approved_edit_works_in_edge(edge, served):
    page = edge.new_page()
    console_errors: list[str] = []
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    try:
        page.goto(served["url"])
        status = page.locator("#status")
        playwright_api.expect(status).to_have_text("Connected", timeout=TIMEOUT_MS)
        assert "token" not in page.url, "the token is removed from the address bar"

        # A plain turn: the reply is streamed into one agent message.
        page.fill("#prompt", "Say hello")
        page.click("#send")
        playwright_api.expect(page.locator("#chat .msg.agent .text")).to_have_text(
            "Hello from the scripted model.", timeout=TIMEOUT_MS)
        playwright_api.expect(page.locator("#send")).to_be_enabled(timeout=TIMEOUT_MS)

        # An edit: the approval dialog shows the diff, with the removed and the added line.
        page.fill("#prompt", "EDIT hello.txt")
        page.click("#send")
        dialog = page.locator("#approval")
        playwright_api.expect(dialog).to_be_visible(timeout=TIMEOUT_MS)
        playwright_api.expect(page.locator("#approval-title")).to_contain_text("hello.txt")
        playwright_api.expect(page.locator("#approval-preview .line.del")).to_contain_text("world")
        playwright_api.expect(page.locator("#approval-preview .line.add")).to_contain_text("there")
        labels = page.locator("#approval-options button").all_inner_texts()
        assert "Allow once" in labels and "Reject" in labels

        page.locator("#approval-options button", has_text="Allow once").click()
        playwright_api.expect(dialog).to_be_hidden(timeout=TIMEOUT_MS)

        # The tool card completes and the final reply is streamed.
        playwright_api.expect(page.locator(".tool.status-completed")).to_have_count(1, timeout=TIMEOUT_MS)
        playwright_api.expect(page.locator(".tool-title").first).to_contain_text("edit_file")
        playwright_api.expect(page.locator("#chat")).to_contain_text("The edit is done", timeout=TIMEOUT_MS)
        playwright_api.expect(page.locator("#send")).to_be_enabled(timeout=TIMEOUT_MS)
    finally:
        page.close()

    assert (served["workspace"] / "hello.txt").read_text(encoding="utf-8") == EDITED
    assert not [e for e in console_errors if "Content Security Policy" in e], console_errors
