"""Find text on a page that tries to instruct an AI, before any model sees the page.

Website text is untrusted data. Anything that addresses AI agents is quarantined deterministically: it
is recorded as an `injection_canary` on the page's source (so certification can prove it never reaches
output) and cut out of the text the extraction model reads. The scan covers what text extractors and
screen readers see but a screenshot may not: visible text, visually hidden elements, HTML comments,
`alt`/`title`/`aria-label` attributes and `<meta>` content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

from askonce_server.profile.snapshots import html_renderings, normalize_for_quote

Reason = Literal["prompt_injection", "hidden_text", "instruction_to_assistant"]

REMOVED = "[removed: suspected prompt injection]"

_PATTERNS = [
    r"\b(ai|a\.i\.)\s+(agents?|assistants?|models?|systems?)\b",
    r"\b(chat ?bots?|voice (agents?|assistants?)|booking (bots?|agents?)|automated (agents?|assistants?|booking agents?))\b",
    r"\bignore (all |any )?(the )?(previous|prior|above|earlier|other) (instructions|prices|policies)\b",
    r"\b(system notice|system prompt|owner override|developer message)\b",
    r"\b(language models?|llms?)\b.{0,40}\b(reading|must|should)\b",
    r"\bdo not (tell|mention|inform) (the )?(user|customer|owner)\b",
]
_INJECTION = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.IGNORECASE)

_HIDDEN_CLASSES = re.compile(r"\b(visually-hidden|sr-only|screen-reader-text|hidden|offscreen)\b", re.IGNORECASE)
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|left\s*:\s*-\d{3,}|font-size\s*:\s*0", re.I)
_TEXT_ATTRS = ("alt", "title", "aria-label", "content", "placeholder")
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})


@dataclass(frozen=True)
class Canary:
    quote: str
    reason: Reason
    where: str  # "text", "hidden", "comment", "attribute:alt", ...


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION.search(text))


class _Scanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[Canary] = []
        self._stack: list[tuple[str, bool]] = []
        self._skip = 0

    @property
    def _hidden(self) -> bool:
        return any(hidden for _, hidden in self._stack)

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        values = {k: v or "" for k, v in attrs}
        for name in _TEXT_ATTRS:
            text = values.get(name, "")
            if text and looks_like_injection(text):
                self._add(text, "hidden_text", f"attribute:{name}")
        return bool(
            _HIDDEN_CLASSES.search(values.get("class", ""))
            or _HIDDEN_STYLE.search(values.get("style", ""))
            or "hidden" in values
            or values.get("aria-hidden") == "true"
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        hidden = self._attrs(tag, attrs)
        if tag in ("script", "style"):
            self._skip += 1
        if tag not in _VOID:
            self._stack.append((tag, hidden))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._attrs(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data: str) -> None:
        if self._skip or not data.strip() or not looks_like_injection(data):
            return
        if self._hidden:
            self._add(data, "hidden_text", "hidden")
        else:
            self._add(data, "prompt_injection", "text")

    def handle_comment(self, data: str) -> None:
        if looks_like_injection(data):
            self._add(data, "hidden_text", "comment")

    def _add(self, text: str, reason: Reason, where: str) -> None:
        quote = normalize_for_quote(text)[:1000]
        if quote and all(c.quote != quote for c in self.found):
            self.found.append(Canary(quote=quote, reason=reason, where=where))


def find_canaries(html: str) -> list[Canary]:
    scanner = _Scanner()
    scanner.feed(html)
    scanner.close()
    return scanner.found


def model_text(html: str, canaries: list[Canary]) -> str:
    """The page text the extraction model reads: the rendered body text, injections cut out."""
    rendered, _ = html_renderings(html)
    text = normalize_for_quote(rendered)
    for canary in canaries:
        text = text.replace(canary.quote, REMOVED)
    return text
