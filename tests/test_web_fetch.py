"""web_fetch: approval every time, URL refusals, size and redirect caps, text conversion.

No test reaches the network. The `site` fixture routes every request through an
httpx.MockTransport (the transport seam of tools/web_tools.new_client), and records them.
"""

import http.server
import threading

import httpx
import pytest
from fakes import text_reply, tool_reply

from wildcat_code_agent import tools
from wildcat_code_agent.config import Settings
from wildcat_code_agent.core import loop
from wildcat_code_agent.core.events import Decision, ToolResult
from wildcat_code_agent.permissions import SessionApprovals
from wildcat_code_agent.tools import web_tools
from wildcat_code_agent.tools.registry import ToolContext


class Site:
    """Scripted web server: add(url, ...) sets a reply; every request is recorded."""

    def __init__(self) -> None:
        self.replies: dict[str, httpx.Response] = {}
        self.requests: list[httpx.Request] = []
        self.timeouts: list[int] = []

    def add(self, url: str, *, status: int = 200, body: bytes | str = b"", content_type: str | None = "text/html",
            headers: dict[str, str] | None = None) -> None:
        raw = body.encode("utf-8") if isinstance(body, str) else body
        head = dict(headers or {})
        if content_type is not None:
            head["content-type"] = content_type
        self.replies[url] = httpx.Response(status, headers=head, content=raw)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.get(str(request.url))
        if reply is None:
            return httpx.Response(404, content=b"no such page", headers={"content-type": "text/plain"})
        return httpx.Response(reply.status_code, headers=reply.headers, content=reply.content)


@pytest.fixture
def site(monkeypatch) -> Site:
    scripted = Site()
    real_new_client = web_tools.new_client

    def client_on_the_scripted_site(timeout_s: int, transport=None) -> httpx.Client:
        scripted.timeouts.append(timeout_s)
        return real_new_client(timeout_s, transport=httpx.MockTransport(scripted.handler))

    monkeypatch.setattr(web_tools, "new_client", client_on_the_scripted_site)
    return scripted


class RecordingApprover:
    def __init__(self, decision: Decision):
        self.decision = decision
        self.requests = []

    def decide(self, request):
        self.requests.append(request)
        return self.decision


def fetch(url: str, *, max_output_chars: int = 8000, network_timeout_s: int = 20) -> str:
    context = ToolContext(max_output_chars=max_output_chars, network_timeout_s=network_timeout_s)
    return tools.TOOLS["web_fetch"].call({"url": url}, context)


def run_fetch(fake_ollama, url: str, *, approver=None, session=None, settings: Settings | None = None):
    """One turn in which the model calls web_fetch(url) and then answers."""
    client = fake_ollama([tool_reply(("web_fetch", {"url": url})), text_reply("done")])
    settings = settings or Settings(tool_profile="all", network_enabled=True)
    events: list = []
    messages = loop.new_messages()
    messages.append({"role": "user", "content": "fetch it"})
    loop.run_turn(messages, client=client, settings=settings, emit=events.append, approver=approver,
                  session=session)
    result = next(e for e in events if isinstance(e, ToolResult))
    return result, events


# --- approval -------------------------------------------------------------------------------

def test_every_fetch_asks_and_no_allow_always_is_offered(fake_ollama, site, workspace):
    site.add("https://example.com/a", body="one")
    site.add("https://example.com/b", body="two")
    session = SessionApprovals()
    approver = RecordingApprover(Decision.ALLOW_ALWAYS)
    client = fake_ollama([
        tool_reply(("web_fetch", {"url": "https://example.com/a"})),
        tool_reply(("web_fetch", {"url": "https://example.com/b"})),
        text_reply("done"),
    ])
    messages = loop.new_messages() + [{"role": "user", "content": "go"}]

    loop.run_turn(messages, client=client, settings=Settings(tool_profile="all", network_enabled=True),
                  approver=approver, session=session)

    assert len(approver.requests) == 2  # asked for each call, even after "allow always"
    for request in approver.requests:
        assert request.tool_name == "web_fetch"
        assert request.risk == "network"
        assert request.options == ("allow_once", "reject_once")
        assert "https://example.com/" in request.title
        assert request.title.endswith("(GET)")
        assert request.preview.text.startswith("GET https://example.com/")
    assert not session.allows("web_fetch", workspace)
    assert site.requests and len(site.requests) == 2


def test_a_refused_fetch_does_not_reach_the_site(fake_ollama, site):
    site.add("https://example.com/", body="secret page")

    result, _ = run_fetch(fake_ollama, "https://example.com/", approver=RecordingApprover(Decision.REJECT_ONCE))

    assert result.content == "User denied web_fetch."
    assert result.status == "failed"
    assert site.requests == []


def test_an_allowed_fetch_runs_and_returns_the_page(fake_ollama, site):
    site.add("https://example.com/", body="<p>hello there</p>")

    result, _ = run_fetch(fake_ollama, "https://example.com/", approver=RecordingApprover(Decision.ALLOW_ONCE))

    assert result.status == "completed"
    assert "hello there" in result.content
    assert "Final URL: https://example.com/" in result.content


# --- URL refusals (no question is asked) ------------------------------------------------------

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/file.txt",
    "javascript:alert(1)",
    "example.com/page",          # no scheme
    "//example.com/page",        # no scheme either
    "https://",                  # no host
    "https://user:pass@example.com/",
])
def test_a_refused_url_is_answered_without_asking(fake_ollama, site, url):
    approver = RecordingApprover(Decision.ALLOW_ONCE)

    result, _ = run_fetch(fake_ollama, url, approver=approver)

    assert approver.requests == []
    assert result.status == "failed"
    assert result.content.startswith("Error: ")
    assert site.requests == []


def test_scheme_error_names_the_scheme_and_what_is_allowed():
    message = fetch("file:///C:/secret.txt")

    assert message.startswith("Error: only http and https URLs can be fetched")
    assert "'file'" in message


def test_http_urls_are_accepted(site):
    site.add("http://example.com/plain", body="plain text", content_type="text/plain")

    assert "plain text" in fetch("http://example.com/plain")


# --- size and redirect caps --------------------------------------------------------------------

def test_the_download_stops_at_the_size_cap(site, monkeypatch):
    monkeypatch.setattr(web_tools, "MAX_FETCH_BYTES", 100)
    site.add("https://example.com/big", body="x" * 1000, content_type="text/plain")

    result = fetch("https://example.com/big")

    assert "x" * 100 in result
    assert "x" * 101 not in result
    assert "[download stopped after 100 bytes" in result


def test_the_body_is_not_read_past_the_cap(monkeypatch):
    monkeypatch.setattr(web_tools, "MAX_FETCH_BYTES", 2500)
    pulled: list[int] = []

    def chunks():
        for _ in range(1000):
            pulled.append(1)
            yield b"y" * 1000

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=chunks())

    real_new_client = web_tools.new_client
    monkeypatch.setattr(web_tools, "new_client",
                        lambda timeout_s: real_new_client(timeout_s, transport=httpx.MockTransport(handler)))

    fetch("https://example.com/endless")

    assert len(pulled) <= 4  # 2500 bytes need three 1000-byte chunks; the stream is not drained


def test_five_redirects_are_followed(site):
    for i in range(5):
        site.add(f"https://example.com/r{i}", status=302, headers={"location": f"/r{i + 1}"}, content_type=None)
    site.add("https://example.com/r5", body="arrived", content_type="text/plain")

    result = fetch("https://example.com/r0")

    assert "Status: 200" in result
    assert "Final URL: https://example.com/r5" in result
    assert "arrived" in result


def test_a_sixth_redirect_is_refused(site):
    for i in range(6):
        site.add(f"https://example.com/r{i}", status=301, headers={"location": f"/r{i + 1}"}, content_type=None)
    site.add("https://example.com/r6", body="never reached", content_type="text/plain")

    result = fetch("https://example.com/r0")

    assert result.startswith("Error: more than 5 redirects")
    assert "never reached" not in result
    assert "https://example.com/r6" not in [str(r.url) for r in site.requests]


def test_a_redirect_to_a_non_http_scheme_is_refused(site):
    site.add("https://example.com/start", status=302, headers={"location": "file:///etc/passwd"},
             content_type=None)

    result = fetch("https://example.com/start")

    assert result.startswith("Error: the redirect target is refused")
    assert "file" in result
    assert [str(r.url) for r in site.requests] == ["https://example.com/start"]


def test_cookies_set_by_one_hop_are_not_sent_on_the_next(site):
    site.add("https://example.com/a", status=302, headers={"location": "/b", "set-cookie": "session=abc"},
             content_type=None)
    site.add("https://example.com/b", body="ok", content_type="text/plain")

    fetch("https://example.com/a")

    assert len(site.requests) == 2
    assert "cookie" not in site.requests[1].headers


def test_the_request_is_a_plain_get_with_a_generic_user_agent_and_no_credentials(site):
    site.add("https://example.com/", body="ok", content_type="text/plain")

    fetch("https://example.com/")

    request = site.requests[0]
    assert request.method == "GET"
    assert request.headers["user-agent"].startswith("wildcat-code-agent/")
    assert "authorization" not in request.headers
    assert "cookie" not in request.headers


def test_the_network_timeout_comes_from_the_context(site):
    site.add("https://example.com/", body="ok", content_type="text/plain")

    fetch("https://example.com/", network_timeout_s=7)

    assert site.timeouts == [7]


def test_a_transport_error_is_reported_as_text(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused by the test")

    real_new_client = web_tools.new_client
    monkeypatch.setattr(web_tools, "new_client",
                        lambda timeout_s: real_new_client(timeout_s, transport=httpx.MockTransport(handler)))

    result = fetch("https://example.com/")

    assert result == "Error fetching https://example.com/: ConnectError: refused by the test"


# --- content types and text conversion ---------------------------------------------------------

def test_a_404_shows_its_status_and_body(site):
    site.add("https://example.com/missing", status=404, body="gone", content_type="text/plain")

    result = fetch("https://example.com/missing")

    assert result.startswith("Status: 404\nFinal URL: https://example.com/missing\nContent type: text/plain")
    assert result.endswith("gone")


def test_plain_text_and_json_are_returned_as_they_are(site):
    site.add("https://example.com/data.json", body='{"a": 1}', content_type="application/json; charset=utf-8")

    assert fetch("https://example.com/data.json").endswith('{"a": 1}')


def test_a_binary_type_is_not_shown(site):
    site.add("https://example.com/pic.png", body=b"\x89PNG\r\n\x1a\n\x00\x00", content_type="image/png")

    result = fetch("https://example.com/pic.png")

    assert "Content type: image/png" in result
    assert "Not text: the content type is image/png, so the body is not shown (10 bytes)." in result
    assert "PNG" not in result


def test_the_charset_from_the_header_decodes_the_body(site):
    site.add("https://example.com/latin", body="café".encode("latin-1"),
             content_type="text/plain; charset=iso-8859-1")

    assert fetch("https://example.com/latin").endswith("café")


def test_long_output_is_truncated_with_the_shared_marker(site):
    site.add("https://example.com/long", body="#" * 500, content_type="text/plain")

    result = fetch("https://example.com/long", max_output_chars=100)

    assert "...[truncated 400 chars]" in result
    assert result.count("#") == 100


PAGE = """<!doctype html>
<html><head><title>Ex &amp; Co</title>
<style>p { color: red }</style>
<script>var secret = "do not show";</script></head>
<body>
<nav><a href="/home">Home</a> <a href="#top">Top</a> <a href="javascript:void(0)">Nothing</a></nav>
<h1>Welcome</h1>
<p>First   paragraph with <a href="https://other.example/x">a link</a>.</p>
<p>Second <b>bold</b> line.<br>After break</p>
<noscript>enable scripts</noscript>
<ul><li>one</li><li>two</li></ul>
</body></html>"""


def test_html_becomes_readable_text_without_scripts_or_styles():
    text = web_tools.html_to_text(PAGE, "https://example.com/docs/page.html")

    assert "do not show" not in text
    assert "color: red" not in text
    assert "enable scripts" not in text
    assert "Ex & Co" in text
    assert "Welcome" in text
    assert "First paragraph with a link (https://other.example/x)." in text
    assert "Second bold line." in text
    assert "After break" in text
    assert "one\ntwo" in text
    assert "Home (https://example.com/home)" in text  # relative link made absolute
    assert "Top" in text and "Top (" not in text       # fragment-only link: no target written
    assert "Nothing (" not in text                      # javascript: link: no target written


def test_html_text_has_no_runs_of_blank_lines():
    text = web_tools.html_to_text("<div>a</div>" + "<div></div>" * 10 + "<div>b</div>")

    assert text == "a\n\nb"


def test_an_html_page_is_converted_in_the_tool_output(site):
    site.add("https://example.com/page", body=PAGE)

    result = fetch("https://example.com/page")

    assert "<h1>" not in result
    assert "Welcome" in result
    assert "do not show" not in result


def test_a_link_inside_a_skipped_element_leaves_no_target():
    assert web_tools.html_to_text('<template><a href="/leak">y</a></template>ok', "https://example.com/") == "ok"
    assert web_tools.html_to_text('<script><a href="/leak">y</a></script>ok', "https://example.com/") == "ok"


# --- charset ----------------------------------------------------------------------------------------

def test_a_body_that_is_not_utf8_and_names_no_charset_is_read_as_cp1252(site):
    site.add("https://example.com/old", body="café €".encode("cp1252"), content_type="text/plain")

    assert fetch("https://example.com/old").endswith("café €")


def test_a_bogus_charset_falls_back_to_the_same_rule(site):
    site.add("https://example.com/old", body="café".encode("cp1252"),
             content_type="text/plain; charset=x-no-such-charset")

    assert fetch("https://example.com/old").endswith("café")


def test_utf8_without_a_charset_is_still_read_as_utf8(site):
    site.add("https://example.com/new", body="café".encode("utf-8"), content_type="text/plain")

    assert fetch("https://example.com/new").endswith("café")


def test_bytes_cp1252_leaves_undefined_become_replacement_characters(site):
    site.add("https://example.com/odd", body=b"a\x81b", content_type="text/plain")

    assert fetch("https://example.com/odd").endswith("a�b")


# --- private addresses: redirects and the approval ----------------------------------------------

@pytest.mark.parametrize("host", [
    "localhost", "LOCALHOST", "localhost.", "api.localhost",
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.0.10", "169.254.169.254", "0.0.0.0", "224.0.0.1",
    "::1", "[::1]", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
    "intranet", "printer",
])
def test_local_and_private_hosts_are_recognised(host):
    assert web_tools.is_private_host(host)


@pytest.mark.parametrize("host", [
    "example.com", "localhost-not.example.com", "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8",
])
def test_public_hosts_are_not_private(host):
    assert not web_tools.is_private_host(host)


@pytest.mark.parametrize("target", [
    "http://127.0.0.1:8080/admin",
    "http://169.254.169.254/latest/meta-data/",
    "http://localhost/",
    "http://[::1]/",
    "http://intranet/wiki",
])
def test_a_public_page_cannot_redirect_to_a_private_address(site, target):
    site.add("https://example.com/start", status=302, headers={"location": target}, content_type=None)

    result = fetch("https://example.com/start")

    assert result.startswith(f"Error: the redirect target {target} is a local or private network address")
    assert "call web_fetch with its URL" in result
    assert [str(r.url) for r in site.requests] == ["https://example.com/start"]


def test_a_private_page_may_redirect_to_another_private_address(site):
    site.add("http://127.0.0.1:8000/a", status=302, headers={"location": "http://192.168.1.5/b"}, content_type=None)
    site.add("http://192.168.1.5/b", body="lan page", content_type="text/plain")

    assert "lan page" in fetch("http://127.0.0.1:8000/a")


def test_a_public_page_may_redirect_to_another_public_host(site):
    site.add("https://example.com/a", status=302, headers={"location": "https://example.org/b"}, content_type=None)
    site.add("https://example.org/b", body="other site", content_type="text/plain")

    assert "other site" in fetch("https://example.com/a")


def test_a_fetch_of_a_private_address_is_labelled_in_the_approval(fake_ollama, site):
    site.add("http://127.0.0.1:11434/api/tags", body="{}", content_type="application/json")
    approver = RecordingApprover(Decision.ALLOW_ONCE)

    result, _ = run_fetch(fake_ollama, "http://127.0.0.1:11434/api/tags", approver=approver)

    request = approver.requests[0]
    assert request.title == "Fetch http://127.0.0.1:11434/api/tags (GET, local/private address)"
    assert request.preview.text == ("GET http://127.0.0.1:11434/api/tags\n"
                                    "Note: this is a local or private network address.")
    assert result.status == "completed"


def test_a_public_fetch_has_no_private_label():
    assert web_tools._fetch_title("https://example.com/") == "Fetch https://example.com/ (GET)"
    assert web_tools._fetch_preview("https://example.com/").text == "GET https://example.com/"


# --- the whole-fetch deadline ------------------------------------------------------------------------

def _fake_clock(monkeypatch, step: float) -> None:
    """Each read of the fetch clock moves it on by `step` seconds, so the deadline passes after a few reads."""
    now = {"t": 0.0}

    def tick() -> float:
        now["t"] += step
        return now["t"]

    monkeypatch.setattr(web_tools, "_now", tick)


def test_a_slow_drip_is_stopped_at_the_whole_fetch_deadline(monkeypatch):
    _fake_clock(monkeypatch, 1.0)
    pulled: list[int] = []

    def drip():
        while True:
            pulled.append(1)
            yield b"x"

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=drip())

    real_new_client = web_tools.new_client
    monkeypatch.setattr(web_tools, "new_client",
                        lambda timeout_s: real_new_client(timeout_s, transport=httpx.MockTransport(handler)))

    result = fetch("https://example.com/drip", network_timeout_s=3)

    assert result == "Error: the fetch of https://example.com/drip took longer than 3 s and was stopped."
    assert 1 <= len(pulled) <= 3  # the stream is not read on after the deadline


def test_the_deadline_also_covers_the_redirect_chain(site, monkeypatch):
    _fake_clock(monkeypatch, 1.0)
    for i in range(5):
        site.add(f"https://example.com/r{i}", status=302, headers={"location": f"/r{i + 1}"}, content_type=None)
    site.add("https://example.com/r5", body="arrived", content_type="text/plain")

    result = fetch("https://example.com/r0", network_timeout_s=3)

    assert result.startswith("Error: the fetch of https://example.com/r")
    assert "took longer than 3 s" in result
    assert "arrived" not in result
    assert len(site.requests) < 5


# --- proxies from the environment -----------------------------------------------------------------

@pytest.fixture
def make_server():
    """Starts a plain HTTP server on 127.0.0.1 that answers every GET with `body`. Returns (port, paths it got)."""
    servers: list[http.server.ThreadingHTTPServer] = []

    def start(body: bytes):
        hits: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1], hits

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def http_proxy_from_env(monkeypatch):
    """Sets a proxy in the environment for the test (the upper and lower case names, and no NO_PROXY)."""
    for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy", "http_proxy"):
        monkeypatch.delenv(name, raising=False)

    def set_proxy(port: int) -> None:
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{port}")

    return set_proxy


def test_a_loopback_target_is_reached_directly_even_with_an_http_proxy_set(make_server, http_proxy_from_env):
    local_port, local_hits = make_server(b"local page")
    proxy_port, proxy_hits = make_server(b"from the proxy")
    http_proxy_from_env(proxy_port)

    result = fetch(f"http://127.0.0.1:{local_port}/page")

    assert "local page" in result
    assert local_hits == ["/page"]
    assert proxy_hits == []


def test_a_localhost_target_is_reached_directly_even_with_an_http_proxy_set(make_server, http_proxy_from_env):
    local_port, local_hits = make_server(b"local page")
    proxy_port, proxy_hits = make_server(b"from the proxy")
    http_proxy_from_env(proxy_port)

    result = fetch(f"http://localhost:{local_port}/page")

    assert "local page" in result
    assert local_hits == ["/page"]
    assert proxy_hits == []


def test_a_public_target_still_goes_through_the_proxy(make_server, http_proxy_from_env):
    # The control for the two tests above: the proxy really is in use for other hosts (it answers locally, so
    # no request leaves the machine).
    proxy_port, proxy_hits = make_server(b"from the proxy")
    http_proxy_from_env(proxy_port)

    result = fetch("http://example.com/page")

    assert "from the proxy" in result
    assert proxy_hits == ["http://example.com/page"]
