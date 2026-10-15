"""Fetch a small-business website: same-origin crawl from a start URL, one immutable snapshot per page.

A snapshot is the page's raw bytes; `content_sha256` is the sha256 of those bytes, which is what the
server's `DirectorySnapshotProvider` checks when it verifies evidence quotes (invariant L3).

Fetchers:
  * `HttpFetcher`: real HTTP(S) via httpx.
  * `DirectoryFetcher`: serves a host from a local folder. The three demo sites live in `sites/` and
    are published under fictional hosts (`thimbleberry.example.com`, ...), so onboarding them never
    touches the network but records the same URLs the gold profiles use.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import posixpath
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

MAX_PAGES = 15
USER_AGENT = "AskOnce-Onboarding/0.1 (read-only; hackathon research)"
MAX_PAGE_BYTES = 2_000_000

REPO_ROOT = Path(__file__).resolve().parents[3]
SITES_DIR = REPO_ROOT / "sites"

DEMO_SITES: Mapping[str, str] = {
    "thimbleberry.example.com": "thimbleberry-bakehouse",
    "kettlewick.example.com": "kettlewick-home",
    "quillfern.example": "quillfern-salon",
}
"""Fictional host -> folder under `sites/`."""

DEMO_URLS: tuple[str, ...] = (
    "https://thimbleberry.example.com/",
    "https://kettlewick.example.com/",
    "https://quillfern.example/",
)


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Fetched:
    url: str
    content_type: str
    body: bytes


class Fetcher(Protocol):
    def fetch(self, url: str) -> Fetched: ...


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    html: str
    raw: bytes
    retrieved_at: str
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sha256", hashlib.sha256(self.raw).hexdigest())

    @property
    def path_key(self) -> str:
        """Short stable name for the page (`index`, `menu`, `custom-cakes`)."""
        path = urlsplit(self.url).path.rstrip("/")
        name = posixpath.basename(path) or "index"
        name = re.sub(r"\.(html?|php|aspx?)$", "", name)
        return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "index"


class HttpFetcher:
    def __init__(self, timeout_s: float = 20.0) -> None:
        import httpx

        self._client = httpx.Client(
            timeout=timeout_s, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )

    def fetch(self, url: str) -> Fetched:
        response = self._client.get(url)
        if response.status_code != 200:
            raise FetchError(f"{url}: HTTP {response.status_code}")
        return Fetched(str(response.url), response.headers.get("content-type", ""), response.content[:MAX_PAGE_BYTES])


class PoliteFetcher:
    """Honours robots.txt for our user agent (and `*`) before fetching a page from a real site."""

    def __init__(self, inner: Fetcher | None = None) -> None:
        self._inner = inner or HttpFetcher()
        self._robots: dict[str, object] = {}

    def allowed(self, url: str) -> bool:
        from urllib.robotparser import RobotFileParser

        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(origin)
        if parser is None:
            parser = RobotFileParser()
            try:
                body = self._inner.fetch(origin + "/robots.txt").body.decode("utf-8", errors="replace")
                parser.parse(body.splitlines())
            except Exception:  # noqa: BLE001 - no robots.txt: everything is allowed
                parser.parse([])
            self._robots[origin] = parser
        return bool(parser.can_fetch(USER_AGENT, url))  # type: ignore[attr-defined]

    def fetch(self, url: str) -> Fetched:
        if not self.allowed(url):
            raise FetchError(f"{url}: disallowed by robots.txt")
        return self._inner.fetch(url)


class DirectoryFetcher:
    """Serves `https://<host>/<path>` from `<folder>/<path>` for each mapped host."""

    def __init__(self, hosts: Mapping[str, Path]) -> None:
        self._hosts = {host: Path(folder).resolve() for host, folder in hosts.items()}

    @classmethod
    def demo(cls) -> DirectoryFetcher:
        return cls({host: SITES_DIR / folder for host, folder in DEMO_SITES.items()})

    def handles(self, url: str) -> bool:
        return (urlsplit(url).hostname or "") in self._hosts

    def fetch(self, url: str) -> Fetched:
        parts = urlsplit(url)
        root = self._hosts.get(parts.hostname or "")
        if root is None:
            raise FetchError(f"{url}: host not served locally")
        rel = parts.path.lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        target = (root / rel).resolve()
        if root not in target.parents or not target.is_file():
            raise FetchError(f"{url}: not found")
        ctype = "text/html" if target.suffix in (".html", ".htm") else "application/octet-stream"
        return Fetched(url, ctype, target.read_bytes())


class RoutingFetcher:
    """Demo hosts from disk, everything else over HTTP."""

    def __init__(self, local: DirectoryFetcher, remote_factory: Callable[[], Fetcher] = HttpFetcher) -> None:
        self._local = local
        self._remote_factory = remote_factory
        self._remote: Fetcher | None = None

    def fetch(self, url: str) -> Fetched:
        if self._local.handles(url):
            return self._local.fetch(url)
        if self._remote is None:
            self._remote = self._remote_factory()
        return self._remote.fetch(url)


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def _bare(netloc: str) -> str:
    """`www.example.com` and `example.com` are one site."""
    return re.sub(r"^www\.", "", netloc.lower())


def _canonical(url: str) -> str:
    url, _ = urldefrag(url)
    parts = urlsplit(url)
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, parts.query, ""))


def _same_page(a: str, b: str) -> bool:
    def norm(u: str) -> str:
        parts = urlsplit(_canonical(u))
        path = re.sub(r"/index\.html?$", "/", parts.path).rstrip("/") or "/"
        return f"{_bare(parts.netloc)}{path}?{parts.query}"

    return norm(a) == norm(b)


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


_ASSET = re.compile(r"\.(jpe?g|png|gif|webp|svg|ico|pdf|zip|mp4|mp3|css|js|json|xml|txt|docx?|xlsx?)$", re.I)
_SKIP_PATH = re.compile(
    r"/(cart|checkout|basket|login|log-in|signin|sign-in|register|account|my-account|wp-admin|wp-login|feed|tag|"
    r"category|author|search|cdn-cgi|privacy|privacy-policy|terms|cookie|sitemap)(/|$|\.)|/blog/.+|/news/.+|"
    r"/product/.+|/products/.+|/shop/.+",
    re.I,
)
_USEFUL = re.compile(
    r"contact|visit|find-us|location|hour|about|menu|price|pricing|rates|service|treatment|faq|question|"
    r"polic|book|appoint|order|cake|deliver|area|team|staff|how-it-works",
    re.I,
)


def _page_like(path: str) -> bool:
    last = path.rsplit("/", 1)[-1]
    return not _ASSET.search(path) and ("." not in last or bool(re.search(r"\.(html?|php|aspx?)$", last, re.I)))


def _priority(url: str) -> tuple[int, int]:
    path = urlsplit(url).path
    return (0 if _USEFUL.search(path) else 1, path.count("/"))


def sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def crawl(
    start_url: str,
    fetcher: Fetcher,
    *,
    max_pages: int = MAX_PAGES,
    clock: Callable[[], str] = _now,
    delay_s: float = 0.0,
) -> list[Page]:
    """Breadth-first crawl of HTML pages on the start URL's origin. The home page comes first.

    `/` is recorded as `/index.html` when that is what the site links to, so one page never gets two
    sources (invariant L2: source urls are unique).
    """
    origin = urlsplit(start_url)
    queue = [_canonical(start_url)]
    seen: list[str] = []
    pages: list[Page] = []
    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if any(_same_page(url, s) for s in seen):
            continue
        seen.append(url)
        try:
            fetched = fetcher.fetch(url)
        except FetchError:
            if not pages:
                raise
            continue
        if "html" not in fetched.content_type:
            continue
        final = _canonical(fetched.url)
        if final != url:
            # Redirected (http -> https, bare -> www): record the page under the URL that served it.
            if any(_same_page(final, p.url) for p in pages):
                continue
            url = final
            seen.append(url)
        html = fetched.body.decode("utf-8", errors="replace")
        parser = _LinkParser()
        parser.feed(html)
        title = " ".join(parser.title.split())[:120] or url
        links = []
        for href in parser.links:
            absolute = _canonical(urljoin(url, href))
            parts = urlsplit(absolute)
            if parts.scheme not in ("http", "https") or _bare(parts.netloc) != _bare(origin.netloc):
                continue
            if not _page_like(parts.path) or _SKIP_PATH.search(parts.path):
                continue
            links.append(absolute)
        if not pages:
            # Name the home page the way the site's own nav does (`/index.html`), if it does.
            home_alias = next((link for link in links if _same_page(link, url) and link != url), None)
            if home_alias:
                url = home_alias
                seen.append(url)
        pages.append(Page(url=url, title=title, html=html, raw=fetched.body, retrieved_at=clock()))
        for link in links:
            if not any(_same_page(link, s) for s in seen + queue):
                queue.append(link)
        # Pages a customer-service profile needs come first (contact, hours, menu, prices, FAQ...).
        queue.sort(key=_priority)
        if delay_s:
            sleep(delay_s)
    return pages
