"""Network tool: web_fetch (network tier). Off unless `network_enabled` is set in the config; always asks.

It sends one GET per page, with no cookies and no credentials, and a User-Agent of
`local-agent/<version>`. Redirects are followed by hand, one hop at a time, so each
target is checked for http or https before it is requested. A redirect from a public
host to a local or private address is refused (the model can ask again, and the user
is then asked). The whole fetch has one deadline (`network_timeout_s`). The download stops
at MAX_FETCH_BYTES. HTML is turned into text with the standard library only.

Loopback and localhost targets never use a proxy from the environment. Tests pass an
httpx.MockTransport through `new_client`, so no test reaches the network.
"""

import codecs
import html.parser
import importlib.metadata
import ipaddress
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Annotated

import httpx

from local_agent.core.events import Preview
from local_agent.permissions import Risk
from local_agent.tools.output import truncate
from local_agent.tools.registry import ToolContext, tool

MAX_FETCH_BYTES = 2_000_000  # decoded bytes read from the body; more is not read
MAX_REDIRECTS = 5
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
ALLOWED_SCHEMES = ("http", "https")
HTML_TYPES = ("text/html", "application/xhtml+xml")
TEXT_XML_JSON_TYPES = ("application/json", "application/xml", "application/javascript", "application/ld+json")
# Elements whose text is not shown, and elements that start a new line in the output.
SKIPPED_ELEMENTS = frozenset({"script", "style", "noscript", "template"})
BLOCK_ELEMENTS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "footer", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "title",
    "tr", "ul",
})
ITEM_ELEMENTS = frozenset({"li", "dt", "dd", "tr"})
# Hosts that always go direct, whatever HTTP_PROXY and the like say (httpx mount patterns).
LOOPBACK_PATTERNS = ("localhost", "*.localhost", "127.0.0.1", "[::1]")
PRIVATE_NOTE = "Note: this is a local or private network address."


class FetchError(Exception):
    """The page could not be fetched (a refused redirect, too many redirects, the deadline). The text is shown to the model."""


def _now() -> float:
    """The clock for the fetch deadline. Tests replace it with a fake clock."""
    return time.monotonic()


def _user_agent() -> str:
    try:
        version = importlib.metadata.version("local-agent")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"
    return f"local-agent/{version}"


def new_client(timeout_s: int, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """The HTTP client for one fetch: no redirects (followed by _fetch), no cookies kept between hops.

    Without a transport, loopback hosts are mounted on a direct transport so an environment proxy
    is not used for them. With a transport (tests), every request goes to it and no environment
    proxy applies.
    """
    headers = {"User-Agent": _user_agent(), "Accept": "text/html, text/plain;q=0.9, */*;q=0.5"}
    if transport is not None:
        return httpx.Client(transport=transport, trust_env=False, timeout=timeout_s,
                            follow_redirects=False, headers=headers)
    direct = httpx.HTTPTransport()
    mounts = {f"all://{host}": direct for host in LOOPBACK_PATTERNS}
    return httpx.Client(mounts=mounts, timeout=timeout_s, follow_redirects=False, headers=headers)


def is_private_host(host: str) -> bool:
    """True for a host that names this machine or a local network.

    That is localhost (and *.localhost), a name with no dot (an intranet name), and an IP literal
    (bracketed IPv6 too) that is loopback, private, link-local, reserved, unspecified or multicast.
    An IPv4-mapped IPv6 address is judged by its IPv4 address. Names are not resolved in DNS.
    """
    name = host.strip().lower().rstrip(".")
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if not name:
        return False
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return "." not in name
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(address.is_loopback or address.is_private or address.is_link_local
                or address.is_reserved or address.is_unspecified or address.is_multicast)


def _host_of(url: str) -> str:
    """The host name of a URL as urlsplit reads it (brackets removed from IPv6), or "" if there is none."""
    try:
        return urllib.parse.urlsplit(url.strip()).hostname or ""
    except ValueError:
        return ""


def url_problem(url: str) -> str | None:
    """Why this URL cannot be fetched, or None: it must be http or https, name a host, and carry no username or password."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        parts.port  # raises ValueError for a bad port
    except ValueError as e:
        return f"Error: not a valid URL ({e})."
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        shown = parts.scheme or "none"
        return (f"Error: only http and https URLs can be fetched; this URL has scheme '{shown}'. "
                "Give the full URL, starting with http:// or https://.")
    if not parts.hostname:
        return "Error: the URL has no host name."
    if parts.username is not None or parts.password is not None:
        return "Error: URLs with a username or password are not accepted."
    return None


@dataclass(frozen=True)
class _Fetched:
    status: int
    url: str            # the final URL, after redirects
    content_type: str   # the Content-Type header as sent, or ""
    body: bytes
    truncated: bool     # True if the body was longer than MAX_FETCH_BYTES


@dataclass(frozen=True)
class _Deadline:
    """The limit for the whole fetch: `seconds` long, ending at the clock time `at`."""
    seconds: int
    at: float

    def check(self, url: str) -> None:
        if _now() >= self.at:
            raise FetchError(f"the fetch of {url} took longer than {self.seconds} s and was stopped.")


def _read_capped(response: httpx.Response, deadline: _Deadline, url: str) -> tuple[bytes, bool]:
    """Read the decoded body up to MAX_FETCH_BYTES, then stop reading. Returns (bytes, truncated).

    The deadline is checked for each chunk, so a server that sends slowly cannot hold the fetch.
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        deadline.check(url)
        room = MAX_FETCH_BYTES - total
        if len(chunk) > room:
            chunks.append(chunk[:room])
            return b"".join(chunks), True
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), False


def _fetch(url: str, timeout_s: int) -> _Fetched:
    """GET the URL, following up to MAX_REDIRECTS redirects, each target checked for http or https.

    A redirect from a public host to a local or private address is refused. The whole fetch,
    redirects included, must finish within `timeout_s` seconds.
    """
    redirects = 0
    deadline = _Deadline(timeout_s, _now() + timeout_s)
    origin_is_private = is_private_host(_host_of(url))
    with new_client(timeout_s) as client:
        current = url
        while True:
            deadline.check(current)
            with client.stream("GET", current) as response:
                client.cookies.clear()  # nothing a server sets is sent back, not even to the next hop
                location = response.headers.get("location")
                if response.status_code in REDIRECT_STATUSES and location:
                    redirects += 1
                    if redirects > MAX_REDIRECTS:
                        raise FetchError(f"more than {MAX_REDIRECTS} redirects; stopped at {current}.")
                    current = urllib.parse.urljoin(current, location)
                    problem = url_problem(current)
                    if problem is not None:
                        raise FetchError(f"the redirect target is refused: {problem.removeprefix('Error: ')}")
                    if not origin_is_private and is_private_host(_host_of(current)):
                        raise FetchError(
                            f"the redirect target {current} is a local or private network address, and this "
                            "fetch started at a public address, so it was not followed. To fetch that address, "
                            "call web_fetch with its URL; the user will be asked first.")
                    continue
                body, truncated = _read_capped(response, deadline, current)
                return _Fetched(
                    status=response.status_code,
                    url=current,
                    content_type=response.headers.get("content-type", ""),
                    body=body,
                    truncated=truncated,
                )


class _TextExtractor(html.parser.HTMLParser):
    """Collects the readable text of an HTML page. Link targets follow their text in brackets."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base = base_url
        self._parts: list[str] = []
        self._skipping = 0
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIPPED_ELEMENTS:
            self._skipping += 1
        elif tag in BLOCK_ELEMENTS:
            self._parts.append("\n")
        elif tag == "a" and not self._skipping:  # a link inside a skipped element is not shown, so its target is not kept
            self._href = self._link_target(dict(attrs).get("href"))

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_ELEMENTS:
            self._skipping = max(0, self._skipping - 1)
        elif tag in BLOCK_ELEMENTS and tag not in ITEM_ELEMENTS:  # list items start their own line; no blank between them
            self._parts.append("\n")
        elif tag == "a" and self._href is not None:
            self._parts.append(f" ({self._href})")
            self._href = None

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._parts.append(re.sub(r"\s+", " ", data))

    def _link_target(self, href: str | None) -> str | None:
        """The absolute http(s) URL of a link, or None for fragments, scripts and mail links."""
        if not href or href.lstrip().startswith("#"):
            return None
        target = urllib.parse.urljoin(self._base, href.strip())
        return target if urllib.parse.urlsplit(target).scheme in ALLOWED_SCHEMES else None

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._parts).split("\n")]
        out: list[str] = []
        for line in lines:
            if line:
                out.append(line)
            elif out and out[-1] != "":
                out.append("")  # at most one blank line in a row
        return "\n".join(out).strip()


def html_to_text(page: str, base_url: str = "") -> str:
    """Readable text of an HTML document: script, style and template content dropped, block elements on their own lines,
    and each link's target (made absolute against base_url) written after its text."""
    extractor = _TextExtractor(base_url)
    extractor.feed(page)
    extractor.close()
    return extractor.text()


def _split_content_type(value: str) -> tuple[str, str]:
    """(media type in lower case, charset parameter or ""). Both are "" when the header is absent."""
    mime, *params = value.split(";")
    charset = ""
    for param in params:
        name, _, val = param.partition("=")
        if name.strip().lower() == "charset":
            charset = val.strip().strip('"')
    return mime.strip().lower(), charset


def _is_text_type(mime: str) -> bool:
    return mime.startswith("text/") or mime in TEXT_XML_JSON_TYPES or mime.endswith(("+json", "+xml"))


def _decode(body: bytes, charset: str) -> str:
    """The body as text. A charset the server declared and Python knows is used. Otherwise UTF-8, and
    if that fails, cp1252 (what browsers use for old Western pages). Bytes cp1252 leaves undefined become U+FFFD."""
    try:
        encoding = codecs.lookup(charset).name if charset else None
    except LookupError:
        encoding = None
    if encoding is not None:
        return body.decode(encoding, errors="replace")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")


def _render(fetched: _Fetched, max_chars: int) -> str:
    """The text the model sees: status, final URL and content type, then the body (or a note that it is not text)."""
    mime, charset = _split_content_type(fetched.content_type)
    header = f"Status: {fetched.status}\nFinal URL: {fetched.url}\nContent type: {mime or 'not sent'}"
    if (mime and not _is_text_type(mime)) or (not mime and b"\x00" in fetched.body):
        body = f"Not text: the content type is {mime or 'unknown'}, so the body is not shown ({len(fetched.body)} bytes)."
    else:
        text = _decode(fetched.body, charset)
        body = html_to_text(text, fetched.url) if mime in HTML_TYPES else text
        body = truncate(body, max_chars)
    if fetched.truncated:
        body += f"\n[download stopped after {MAX_FETCH_BYTES} bytes; the rest of the page was not read]"
    return f"{header}\n\n{body}"


def _fetch_title(url: str) -> str:
    suffix = ", local/private address" if is_private_host(_host_of(url)) else ""
    return f"Fetch {url.strip()} (GET{suffix})"


def _fetch_preview(url: str) -> Preview:
    text = f"GET {url.strip()}"
    if is_private_host(_host_of(url)):
        text += f"\n{PRIVATE_NOTE}"
    return Preview(text=text)


@tool(
    risk=Risk.NETWORK,
    denied_message="User denied web_fetch.",
    approval_title=_fetch_title,
    preview=_fetch_preview,
    precheck=url_problem,
    allow_always=False,
)
def web_fetch(
    url: Annotated[str, "Full http or https URL to fetch."],
    *,
    context: ToolContext,
) -> str:
    """Fetch a web page (GET) and return its status and text. HTML is converted to readable text."""
    problem = url_problem(url)
    if problem is not None:
        return problem
    target = url.strip()
    try:
        fetched = _fetch(target, context.network_timeout_s)
    except FetchError as e:
        return f"Error: {e}"
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        return f"Error fetching {target}: {type(e).__name__}: {e}"
    return _render(fetched, context.max_output_chars)
