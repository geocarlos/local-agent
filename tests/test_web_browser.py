"""The web UI in a real browser (T16): Microsoft Edge, driven by Playwright, against a real `serve --web` process.

The model is scripted (tests/fake_ollama_http.py). Three cases:
- an approved edit: a streamed reply, an approval dialog with the diff, allow once, the file changed on disk,
  the tool card completed;
- a rejected edit: the dialog, reject, the file unchanged, the card failed;
- a server that dies mid-turn: the page says Disconnected, the controls stay off, and no second message appears.

The test starts every server it uses and stops it by its own process handle.

Edge missing: the tests skip, when Playwright is installed and the error says the msedge channel is not found.
Any other launch error fails the tests, and so does a missing Edge when the CI environment variable is set
(CI on windows-latest has Edge, so a skip there would hide a real failure).
"""

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from fake_ollama_http import start as start_fake_ollama

playwright_api = pytest.importorskip("playwright.sync_api")

TIMEOUT_MS = 20_000
BANNER = re.compile(r"Open this link in a browser: (http://127\.0\.0\.1:\d+/#token=\S+)")
ORIGINAL = "Hello world\n"
EDITED = "Hello there\n"


def edge_not_installed(message: str) -> bool:
    """True when Playwright says the msedge channel is not installed (and nothing else went wrong)."""
    return re.search(r"msedge", message) is not None and re.search(
        r"not found|doesn't exist|executable", message, re.IGNORECASE) is not None


@pytest.fixture(scope="module")
def edge():
    """One Edge browser for the module."""
    playwright = playwright_api.sync_playwright().start()
    try:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
    except Exception as error:
        playwright.stop()
        if os.environ.get("CI") or not edge_not_installed(str(error)):
            raise
        pytest.skip(f"Microsoft Edge is not installed: {str(error).splitlines()[0][:200]}")
    yield browser
    browser.close()
    playwright.stop()


@pytest.fixture
def served(tmp_path):
    """A real `serve --web` process with the scripted model. Yields its page URL, workspace and process handle."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text(ORIGINAL, encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")  # the user's own config is never read
    fake, fake_url = start_fake_ollama()
    proc = subprocess.Popen(
        [sys.executable, "-m", "wildcat_code_agent", "serve", "--web", "--port", "0", "--no-plugins",
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
        yield {"url": found["url"], "workspace": workspace, "proc": proc, "config": config,
               "sessions": Path(os.environ["WILDCAT_CODE_AGENT_DATA_DIR"]) / "sessions"}
    finally:
        proc.kill()  # our own process, by its handle
        proc.wait(timeout=30)
        reader.join(30)
        proc.stdout.close()
        proc.stderr.close()
        fake.shutdown()
        fake.server_close()


def open_connected(edge, served):
    page = edge.new_page()
    errors: list[str] = []
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.goto(served["url"])
    playwright_api.expect(page.locator("#status")).to_have_text("Connected", timeout=TIMEOUT_MS)
    return page, errors


def ask_for_edit(page):
    page.fill("#prompt", "EDIT hello.txt")
    page.click("#send")
    dialog = page.locator("#approval")
    playwright_api.expect(dialog).to_be_visible(timeout=TIMEOUT_MS)
    return dialog


def test_a_chat_with_an_approved_edit_works_in_edge(edge, served):
    page, console_errors = open_connected(edge, served)
    try:
        assert "token" not in page.url, "the token is removed from the address bar"

        # A plain turn: the reply is streamed into one agent message.
        page.fill("#prompt", "Say hello")
        page.click("#send")
        playwright_api.expect(page.locator("#chat .msg.agent .text")).to_have_text(
            "Hello from the scripted model.", timeout=TIMEOUT_MS)
        playwright_api.expect(page.locator("#send")).to_be_enabled(timeout=TIMEOUT_MS)

        # An edit: the approval dialog shows the diff, with the removed and the added line.
        dialog = ask_for_edit(page)
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


def test_a_rejected_edit_leaves_the_file_unchanged(edge, served):
    page, console_errors = open_connected(edge, served)
    try:
        dialog = ask_for_edit(page)
        page.locator("#approval-options button", has_text="Reject").click()
        playwright_api.expect(dialog).to_be_hidden(timeout=TIMEOUT_MS)

        playwright_api.expect(page.locator(".tool.status-failed")).to_have_count(1, timeout=TIMEOUT_MS)
        playwright_api.expect(page.locator(".tool-output").first).to_contain_text("denied")
        playwright_api.expect(page.locator("#chat")).to_contain_text("I did not edit", timeout=TIMEOUT_MS)
        playwright_api.expect(page.locator("#send")).to_be_enabled(timeout=TIMEOUT_MS)
    finally:
        page.close()

    assert (served["workspace"] / "hello.txt").read_text(encoding="utf-8") == ORIGINAL
    assert not [e for e in console_errors if "Content Security Policy" in e], console_errors


def test_a_server_that_dies_mid_turn_leaves_the_page_disconnected(edge, served):
    page, _ = open_connected(edge, served)
    try:
        dialog = ask_for_edit(page)  # the turn is running: it waits for the answer
        served["proc"].kill()        # the process this test started, by its handle
        served["proc"].wait(timeout=30)

        playwright_api.expect(page.locator("#status")).to_have_text("Disconnected", timeout=TIMEOUT_MS)
        playwright_api.expect(dialog).to_be_hidden(timeout=TIMEOUT_MS)
        playwright_api.expect(page.locator("#notice")).to_be_visible(timeout=TIMEOUT_MS)
        assert page.locator("#send").is_disabled()
        assert page.locator("#prompt").is_disabled()
        assert page.locator("#cancel").is_disabled()
        # The disconnect is said once: no error bubble for the failed turn, and the controls stay off.
        assert page.locator(".msg.error").count() == 0
        page.wait_for_timeout(500)
        assert page.locator("#status").inner_text() == "Disconnected"
        assert page.locator("#send").is_disabled()
    finally:
        page.close()


def test_only_a_missing_msedge_channel_counts_as_not_installed():
    missing = r"Chromium distribution 'msedge' is not found at C:\Program Files\Microsoft\Edge\msedge.exe"
    assert edge_not_installed(missing)
    assert not edge_not_installed("Browser closed unexpectedly while starting msedge")
    assert not edge_not_installed("Target page, context or browser has been closed")


def test_a_saved_chat_reopens_after_a_reload_and_continues(edge, served):
    page, console_errors = open_connected(edge, served)
    try:
        page.fill("#prompt", "Say hello")
        page.click("#send")
        playwright_api.expect(page.locator("#chat .msg.agent .text")).to_have_text(
            "Hello from the scripted model.", timeout=TIMEOUT_MS)
        playwright_api.expect(page.locator("#send")).to_be_enabled(timeout=TIMEOUT_MS)
    finally:
        page.close()

    # A new page (a reload): the chat is gone from the screen, but the session is in the list.
    page, more_errors = open_connected(edge, served)
    try:
        assert page.locator("#chat .msg").count() == 0
        item = page.locator(".session-item", has_text="Say hello")
        playwright_api.expect(item).to_have_count(1, timeout=TIMEOUT_MS)
        item.click()
        playwright_api.expect(page.locator("#chat .msg.user .text")).to_have_text("Say hello", timeout=TIMEOUT_MS)
        playwright_api.expect(page.locator("#chat .msg.agent .text")).to_have_text(
            "Hello from the scripted model.", timeout=TIMEOUT_MS)
        playwright_api.expect(page.locator(".session-item.active")).to_have_count(1)

        # The reopened chat continues: the new message is added to the same saved session.
        page.fill("#prompt", "Say hello again")
        page.click("#send")
        playwright_api.expect(page.locator("#chat .msg.user .text")).to_have_count(2, timeout=TIMEOUT_MS)
        playwright_api.expect(page.locator("#send")).to_be_enabled(timeout=TIMEOUT_MS)
        saved = list(served["sessions"].glob("sess-*.json"))
        assert len(saved) == 1
        assert '"Say hello again"' in saved[0].read_text(encoding="utf-8")
    finally:
        page.close()
    assert not [e for e in more_errors + console_errors if "Content Security Policy" in e]


def test_the_model_picker_and_the_settings_round_trip(edge, served):
    page, _ = open_connected(edge, served)
    try:
        model = page.locator("#model")
        playwright_api.expect(model).to_be_enabled(timeout=TIMEOUT_MS)
        values = model.locator("option").all_inner_texts()
        assert "qwen2.5-coder:7b" in values and "qwen3-vl:8b" in values
        model.select_option("qwen2.5-coder:7b")
        playwright_api.expect(model).to_have_value("qwen2.5-coder:7b", timeout=TIMEOUT_MS)

        page.click("#settings-open")
        dialog = page.locator("#settings")
        playwright_api.expect(dialog).to_be_visible(timeout=TIMEOUT_MS)
        page.fill("#set-num_ctx", "4096")
        page.click("#settings-save")
        playwright_api.expect(page.locator("#settings-message")).to_contain_text("Saved", timeout=TIMEOUT_MS)
        assert "num_ctx = 4096" in served["config"].read_text(encoding="utf-8")
        page.click("#settings-close")
        playwright_api.expect(dialog).to_be_hidden(timeout=TIMEOUT_MS)
    finally:
        page.close()

    page, _ = open_connected(edge, served)
    try:
        page.click("#settings-open")
        playwright_api.expect(page.locator("#set-num_ctx")).to_have_value("4096", timeout=TIMEOUT_MS)
        page.click("#settings-close")
    finally:
        page.close()


def test_the_page_works_at_phone_width(edge, served):
    page = edge.new_page(viewport={"width": 390, "height": 800})
    try:
        page.goto(served["url"])
        playwright_api.expect(page.locator("#status")).to_have_text("Connected", timeout=TIMEOUT_MS)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        drawer = page.locator("#drawer")
        expect = playwright_api.expect
        expect(drawer).to_be_hidden()
        page.click("#sessions-toggle")
        expect(drawer).to_be_visible(timeout=TIMEOUT_MS)
        page.click("#new-chat")
        expect(drawer).to_be_hidden(timeout=TIMEOUT_MS)  # the new chat opens; the list closes on a choice only
    finally:
        page.close()
