"""`profile.snapshots`: page snapshots for L3."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from askonce_server.profile.snapshots import (
    DirectorySnapshotProvider,
    NullSnapshotProvider,
    SnapshotProvider,
    html_renderings,
    html_to_text,
    normalize_for_quote,
    snapshot_texts,
)

PAGE = (
    "<!doctype html><html><head><title>Title text</title><style>p{color:red}</style></head>"
    "<body><h1>Thimbleberry Bakehouse&#39;s menu</h1><p>Email <a href='mailto:x'>hello@<wbr>thimbleberry.example</a>"
    " or call.</p><script>var secret = 'hidden';</script><ul><li>Bread</li><li>Cake&nbsp;&amp; pie</li></ul>"
    "<template><p>never shown</p></template></body></html>"
)


def _write_html(directory: Path, html: str) -> str:
    data = html.encode()
    sha = hashlib.sha256(data).hexdigest()
    (directory / f"{sha}.html").write_bytes(data)
    return sha


def test_normalize_for_quote() -> None:
    assert normalize_for_quote("  a\n\tb   c ") == "a b c"
    assert normalize_for_quote("café") == normalize_for_quote("café")


def test_html_renderings() -> None:
    rendered, nodes = html_renderings(PAGE)
    rendered_n, nodes_n = normalize_for_quote(rendered), normalize_for_quote(nodes)
    assert "Thimbleberry Bakehouse's menu" in rendered_n
    assert "hello@thimbleberry.example or call." in rendered_n  # inline tags join without a space
    assert "hello@ thimbleberry.example" in nodes_n  # text nodes joined by one space
    assert "Bread Cake & pie" in rendered_n  # block boundaries are whitespace; &nbsp; and &amp; decoded
    for text in (rendered_n, nodes_n):
        assert "Title text" not in text and "secret" not in text and "color:red" not in text
        assert "never shown" not in text
    assert html_to_text(PAGE) == rendered


def test_directory_provider_reads_html_and_verifies_the_hash(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    sha = _write_html(tmp_path, PAGE)
    provider = DirectorySnapshotProvider(tmp_path)
    assert isinstance(provider, SnapshotProvider)
    texts = provider.texts_for(sha)
    assert len(texts) == 2 and provider.text_for(sha) == texts[0]

    tampered = "b" * 64
    (tmp_path / f"{tampered}.html").write_text(PAGE)
    with caplog.at_level(logging.WARNING):
        assert provider.text_for(tampered) is None
    assert "does not match its hash" in caplog.text


def test_directory_provider_prefers_pre_extracted_text(tmp_path: Path) -> None:
    sha = _write_html(tmp_path, PAGE)
    (tmp_path / f"{sha}.txt").write_text("Pre-extracted text.", encoding="utf-8")
    assert DirectorySnapshotProvider(tmp_path).texts_for(sha) == ("Pre-extracted text.",)


@pytest.mark.parametrize("sha", ["0" * 64, "../../etc/passwd", "ABC", ""])
def test_directory_provider_missing_or_bad_names(tmp_path: Path, sha: str) -> None:
    provider = DirectorySnapshotProvider(tmp_path)
    assert provider.text_for(sha) is None and provider.texts_for(sha) == ()


def test_directory_provider_caches(tmp_path: Path) -> None:
    sha = _write_html(tmp_path, PAGE)
    provider = DirectorySnapshotProvider(tmp_path)
    first = provider.texts_for(sha)
    (tmp_path / f"{sha}.html").unlink()
    assert provider.texts_for(sha) == first


def test_snapshot_texts_works_with_any_provider() -> None:
    class Single:
        def text_for(self, content_sha256: str) -> str | None:
            return "only" if content_sha256 == "a" else None

    assert snapshot_texts(Single(), "a") == ("only",)
    assert snapshot_texts(Single(), "b") == ()
    assert snapshot_texts(NullSnapshotProvider(), "a") == ()
    assert NullSnapshotProvider().text_for("a") is None
