from __future__ import annotations

import json

import pytest

from askonce_onboarding.census import PageCache, aggregate, read_sites
from askonce_onboarding.site import Fetched, FetchError


def row(**kw):
    base = dict(valid=True, template="bakery", pages=8, items=10, policies=5, has_hours=True, contradictions=0,
                missing=1, confirm=0, injections=0, dropped_facts=2, extraction_errors=0, verdict="CONDITIONAL",
                cases_failed=0, failed_checks=[], priced_items=4, priced_items_cited_by_name=4)
    return {**base, **kw}


def test_aggregate_reports_rates_without_naming_sites():
    summary = aggregate([row(url="https://a.example"), row(url="https://b.example", has_hours=False, contradictions=1)],
                        [{"url": "https://c.example", "reason": "robots_or_fetch"}])
    assert summary["sites_onboarded"] == 2 and summary["sites_attempted"] == 3
    assert summary["share_missing_hours"] == 0.5 and summary["share_with_contradiction"] == 0.5
    assert summary["share_prices_cited_by_name_and_amount"] == 1.0
    assert "example" not in json.dumps(summary)


def test_page_cache_fetches_each_url_once_and_replays_offline(tmp_path):
    calls = []

    class Inner:
        def fetch(self, url):
            calls.append(url)
            if url.endswith("/gone"):
                raise FetchError(f"{url}: HTTP 404")
            return Fetched(url, "text/html", b"<html>hi</html>")

    cache = PageCache(Inner(), tmp_path)
    assert cache.fetch("https://x.example/").body == b"<html>hi</html>"
    assert cache.fetch("https://x.example/").body == b"<html>hi</html>"
    with pytest.raises(FetchError):
        cache.fetch("https://x.example/gone")
    assert calls == ["https://x.example/", "https://x.example/gone"]
    offline = PageCache(Inner(), tmp_path, offline=True)
    assert offline.fetch("https://x.example/").body == b"<html>hi</html>"
    with pytest.raises(FetchError):
        offline.fetch("https://x.example/never")


def test_read_sites(tmp_path):
    f = tmp_path / "s.txt"
    f.write_text("# comment\nexample.com\nhttps://b.example/ # note\n\n")
    assert read_sites(f) == ["https://example.com", "https://b.example/"]
