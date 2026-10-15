"""Ground every extracted fact in a verbatim quote from its page (the onboarding half of invariant L3).

Models paraphrase: they add a space where a tag boundary had none ("Friday7:00 am"), straighten curly
quotes, swap an en dash for a hyphen or change case. `QuoteIndex.locate` finds the quote while
ignoring exactly those differences and returns the page's own characters, so the stored quote always
occurs verbatim in the snapshot. A quote that cannot be found is rejected and its fact is dropped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from askonce_server.profile.snapshots import html_renderings, normalize_for_quote

from askonce_onboarding.injection import looks_like_injection

MAX_QUOTE = 500

_FOLD = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
        "…": ".",
    }
)


def _fold(ch: str) -> str:
    return ch.translate(_FOLD).lower()


class _Rendering:
    def __init__(self, text: str, quarantined: Iterable[str] = ()) -> None:
        self.text = normalize_for_quote(text)
        self.blocked: list[tuple[int, int]] = []
        for bad in quarantined:
            bad = normalize_for_quote(bad)
            start = self.text.find(bad) if bad else -1
            while start >= 0:
                self.blocked.append((start, start + len(bad)))
                start = self.text.find(bad, start + 1)
        keep: list[str] = []
        index: list[int] = []
        for i, ch in enumerate(self.text):
            if ch.isspace():
                continue
            folded = _fold(ch)
            keep.append(folded[0] if folded else ch)
            index.append(i)
        self.stripped = "".join(keep)
        self.index = index

    def find(self, quote: str) -> str | None:
        needle = "".join(_fold(c)[:1] for c in quote if not c.isspace())
        if len(needle) < 3:
            return None
        pos = self.stripped.find(needle)
        while pos >= 0:
            start, end = self.index[pos], self.index[pos + len(needle) - 1] + 1
            if not any(start < b_end and b_start < end for b_start, b_end in self.blocked):
                return self.text[start:end]
            pos = self.stripped.find(needle, pos + 1)
        return None


class QuoteIndex:
    """Both renderings of one page (see `askonce_server.profile.snapshots`).

    `quarantined` texts (injection canaries) can never be quoted: a quote that overlaps one is not found,
    so nothing an injection says can become a fact.
    """

    def __init__(self, html: str, quarantined: Iterable[str] = ()) -> None:
        quarantined = list(quarantined)
        self._renderings = [_Rendering(text, quarantined) for text in html_renderings(html)]

    def opening(self, limit: int = 100) -> str:
        """The first words of the page body: always quotable, for facts that only need an anchor."""
        text = self._renderings[-1].text[:limit]
        return text.rsplit(" ", 1)[0] if len(text) == limit and " " in text else text

    def locate(self, quote: str | None) -> str | None:
        """The verbatim page text for `quote`, or None if the page does not say it."""
        if not quote or not isinstance(quote, str):
            return None
        quote = normalize_for_quote(quote)
        if looks_like_injection(quote):
            return None
        candidates = [quote]
        # "first part ... second part": keep the longest part that is really on the page.
        if "..." in quote or "…" in quote:
            candidates += sorted(re.split(r"\s*(?:\.\.\.|…)\s*", quote), key=len, reverse=True)
        for candidate in candidates:
            for rendering in self._renderings:
                found = rendering.find(candidate)
                if found:
                    return _clip(found)
        return None


def _clip(text: str) -> str:
    if len(text) <= MAX_QUOTE:
        return text
    cut = text[:MAX_QUOTE]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    return cut[: stop + 1] if stop > 100 else cut.rstrip()
