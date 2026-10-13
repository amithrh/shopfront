"""Page snapshots for invariant L3 (every evidence quote occurs verbatim in its source snapshot).

Snapshots are written by onboarding code, keyed by `content_sha256`. The server only reads them. When
no provider is configured (or a snapshot is missing), L3 is reported as a warning ("skipped"), never
silently passed: this is the pluggable, best-effort part of the loader.

Snapshot text of an HTML page (body only, `<script>`/`<style>`/`<template>` dropped, character
references decoded) is produced in two renderings, and an L3 quote must occur whole in one of them:

  * rendered: inline tags join their text without a space; block boundaries (`p`, `li`, `dd`, `div`,
    `br`...) read as whitespace. What a browser shows (the kettlewick gold profile's convention).
  * nodes: every text node joined by one space (the bakery and salon gold profiles' convention).

They differ only in whether a tag boundary is whitespace, so neither admits a word that is not on the
page. L3 compares NFC-normalised, whitespace-collapsed forms (`normalize_for_quote`).
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


@runtime_checkable
class SnapshotProvider(Protocol):
    """Returns the plain text of a page snapshot by its sha256, or None if unavailable."""

    def text_for(self, content_sha256: str) -> str | None: ...


def normalize_for_quote(text: str) -> str:
    """NFC, then collapse every whitespace run to one space and strip (the L3 comparison form)."""
    return " ".join(unicodedata.normalize("NFC", text).split())


BLOCK_TAGS: frozenset[str] = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "br",
        "button",
        "caption",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "img",
        "input",
        "label",
        "legend",
        "li",
        "main",
        "nav",
        "ol",
        "option",
        "p",
        "pre",
        "section",
        "select",
        "summary",
        "table",
        "tbody",
        "td",
        "textarea",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
"""Tags whose boundaries read as whitespace in the rendered form."""


class _BodyTextParser(HTMLParser):
    """Collects body text outside `<head>`, `<script>`, `<style>` and `<template>`, in both renderings."""

    _SKIPPED = frozenset({"head", "script", "style", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[str] = []
        self.rendered: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIPPED:
            self._skip_depth += 1
        elif tag in BLOCK_TAGS:
            self.rendered.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in BLOCK_TAGS:
            self.rendered.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in BLOCK_TAGS:
            self.rendered.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self.rendered.append(data)
        if data.strip():
            self.nodes.append(data.strip())


def html_renderings(html: str) -> tuple[str, str]:
    """`(rendered, nodes)` body text of an HTML page (see the module docstring)."""
    parser = _BodyTextParser()
    parser.feed(html)
    parser.close()
    return "".join(parser.rendered), " ".join(parser.nodes)


def html_to_text(html: str) -> str:
    """Body text of an HTML page as rendered (inline tags join, block boundaries are whitespace)."""
    return html_renderings(html)[0]


def snapshot_texts(provider: SnapshotProvider, content_sha256: str) -> tuple[str, ...]:
    """Every rendering a provider offers for one snapshot (empty if unavailable).

    Providers may implement `texts_for(sha) -> tuple[str, ...]` to offer several renderings;
    otherwise `text_for` is the only one.
    """
    texts_for = getattr(provider, "texts_for", None)
    if callable(texts_for):
        return tuple(texts_for(content_sha256))
    text = provider.text_for(content_sha256)
    return () if text is None else (text,)


class DirectorySnapshotProvider:
    """Reads `<dir>/<sha256>.txt` (pre-extracted text) or `<dir>/<sha256>.html` (tags stripped).

    Verifies that the file's own sha256 matches its name before trusting it (HTML snapshots are hashed
    as raw bytes, which is what onboarding records). A `.txt` file is onboarding's pre-extracted text of
    that page, so it cannot be hashed against the page: it is trusted as written. A mismatching `.html`
    file is logged and treated as missing (L3 then reports the source as skipped). Results are cached:
    a snapshot never changes under its hash.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._cache: dict[str, tuple[str, ...]] = {}

    def text_for(self, content_sha256: str) -> str | None:
        texts = self.texts_for(content_sha256)
        return texts[0] if texts else None

    def texts_for(self, content_sha256: str) -> tuple[str, ...]:
        """Every rendering of the snapshot: `(text,)` for `.txt`, `(rendered, nodes)` for `.html`."""
        if not _SHA256.fullmatch(content_sha256):
            return ()
        if content_sha256 not in self._cache:
            self._cache[content_sha256] = self._read(content_sha256)
        return self._cache[content_sha256]

    def _read(self, content_sha256: str) -> tuple[str, ...]:
        text_path = self._dir / f"{content_sha256}.txt"
        if text_path.is_file():
            return (text_path.read_text(encoding="utf-8"),)
        html_path = self._dir / f"{content_sha256}.html"
        if not html_path.is_file():
            return ()
        data = html_path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != content_sha256:
            logger.warning("Snapshot %s does not match its hash (sha256 %s); ignoring it", html_path.name, actual)
            return ()
        return html_renderings(data.decode("utf-8", errors="replace"))


class NullSnapshotProvider:
    """No snapshots: L3 is always reported as skipped."""

    def text_for(self, content_sha256: str) -> str | None:
        return None
