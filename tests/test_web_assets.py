"""The web UI's static files (T16) and their packaging: content types, the CSP and nosniff headers, the path rules,
the token on /info, and the asset folder in the PyInstaller spec.
"""

import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fakes import FakeOllama
from local_agent.config import Settings
from local_agent.frontends import web, webassets

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765
TOKEN = "static-test-token"


@pytest.fixture
def client(tmp_path):
    app = web.create_app(Settings(workspace=tmp_path), token=TOKEN, port=PORT, client=FakeOllama([]),
                         log=lambda line: None)
    with TestClient(app) as test_client:
        yield test_client


# ----- the folder and the path rules ----------------------------------------------------------------

def test_the_asset_folder_is_found_and_holds_the_ui():
    root = webassets.web_root()
    assert root.is_dir(), root
    assert root == ROOT / "src" / "local_agent" / "web"
    for name in ["index.html", "style.css", "js/main.js", "js/acp.js", "js/transport.js", "js/chat.js"]:
        assert (root / name).is_file(), name


def test_find_asset_serves_only_files_of_the_folder():
    assert webassets.find_asset("")  == webassets.find_asset("index.html")
    assert webassets.find_asset("js/main.js") is not None
    for name in ["../web.py", "js/../../web.py", "..", "js/..", "/etc/passwd", "js//main.js", "./index.html",
                 "js\\main.js", "C:/index.html", "C:index.html", "js", "missing.js", "index.py", "a\x00.js",
                 "index.html/", "__init__.py"]:
        assert webassets.find_asset(name) is None, name


def test_find_asset_refuses_a_link_out_of_the_folder(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    outside = tmp_path / "secret.js"
    outside.write_text("secret", encoding="utf-8")
    try:
        os.symlink(outside, root / "linked.js")
    except (OSError, NotImplementedError):
        pytest.skip("this system does not allow symbolic links here")
    assert webassets.find_asset("linked.js", root) is None


def test_content_types_are_explicit():
    assert webassets.content_type(Path("a.html")) == "text/html; charset=utf-8"
    assert webassets.content_type(Path("a.css")) == "text/css; charset=utf-8"
    assert webassets.content_type(Path("a.js")) == "text/javascript; charset=utf-8"
    assert webassets.content_type(Path("a.svg")) == "image/svg+xml"
    assert webassets.content_type(Path("a.bin")) == "application/octet-stream"


# ----- the HTTP routes ------------------------------------------------------------------------------

def test_the_page_and_its_files_are_served_with_their_types(client):
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert '<script type="module" src="/js/main.js">' in page.text
    assert TOKEN not in page.text, "the page holds no secret"
    assert client.get("/style.css").headers["content-type"].startswith("text/css")
    assert client.get("/js/main.js").headers["content-type"].startswith("text/javascript")


def test_the_page_has_no_inline_script(client):
    text = client.get("/").text
    assert not re.search(r"<script(?![^>]*\bsrc=)", text), "every script is an external file"
    assert "onclick=" not in text and "style=" not in text


def test_every_response_has_the_csp_and_nosniff(client):
    expected_connect = f"connect-src 'self' ws://127.0.0.1:{PORT} ws://localhost:{PORT}"
    for path in ["/", "/style.css", "/nope.html", "/info"]:
        response = client.get(path)
        csp = response.headers["content-security-policy"]
        assert "script-src 'self'" in csp, path
        assert "style-src 'self'" in csp, path
        assert expected_connect in csp, path
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp, path
        assert "default-src 'none'" in csp, path
        assert response.headers["x-content-type-options"] == "nosniff", path
        assert response.headers["referrer-policy"] == "no-referrer", path


@pytest.mark.parametrize("path", ["/nope.html", "/js", "/js/", "/index.py", "/docs", "/openapi.json",
                                  "/%2e%2e/web.py", "/js/%2e%2e/%2e%2e/web.py", "/..%5Cweb.py"])
def test_paths_outside_the_folder_or_unknown_are_404(client, path):
    response = client.get(path)
    assert response.status_code == 404, path
    assert "local-agent" not in response.text


def test_the_static_files_need_no_token_but_the_info_route_does(client):
    assert client.get("/style.css").status_code == 200
    assert client.get("/info").status_code == 403
    assert client.get("/info", headers={"Authorization": "Bearer wrong"}).status_code == 403


def test_info_takes_the_token_only_in_the_authorization_header(client, tmp_path):
    # A token in the query would be written to logs and browser history, so /info does not read it.
    assert client.get(f"/info?token={TOKEN}").status_code == 403
    response = client.get("/info", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"cwd": str(tmp_path), "protocolVersion": 1}


def test_the_websocket_still_needs_the_token(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as refusal:
        with client.websocket_connect("/ws"):
            pass
    assert refusal.value.code == web.WS_POLICY_VIOLATION


# ----- packaging ------------------------------------------------------------------------------------

def test_the_pyinstaller_spec_ships_the_web_folder():
    spec = (ROOT / "packaging" / "local-agent.spec").read_text(encoding="utf-8")
    assert re.search(r'datas\.append\(\(str\(ROOT / "src" / "local_agent" / "web"\),\s*"local_agent/web"\)\)', spec)
    assert "local_agent/web" in spec

