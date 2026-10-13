"""`profile.directory.build_directory_entry`: the discovery summary holds only statable facts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from askonce_server.profile.directory import DirectoryEntry, build_directory_entry
from askonce_server.profile.loader import LoadedProfile


def _entry(doc: dict[str, Any], load_doc: Callable[..., LoadedProfile]) -> DirectoryEntry:
    return build_directory_entry(load_doc(doc).profile)


def test_gold_bakery_entry(gold_raw: Callable[[str], dict[str, Any]], load_doc: Callable[..., LoadedProfile]) -> None:
    entry = _entry(gold_raw("thimbleberry-bakehouse"), load_doc)
    assert entry.business_id == "thimbleberry-bakehouse" and entry.revision >= 1
    assert "gluten_free" in entry.dietary_tags and "vegan" in entry.dietary_tags
    assert entry.dietary_tags == sorted(set(entry.dietary_tags))
    assert "bakery" in entry.categories
    assert "custom cake" in entry.item_terms or "custom cakes" in entry.item_terms
    assert entry.localities == ["portland"] and entry.postal_codes == ["97202"]
    assert entry.module_types == ["catalog", "custom_order", "knowledge", "owner_approval"]
    for terms in (entry.item_terms, entry.keywords, entry.synonyms, entry.localities):
        assert all(term == term.lower() for term in terms)
        assert len(terms) == len(set(terms))


def test_example_entry(example_raw: dict[str, Any], load_doc: Callable[..., LoadedProfile]) -> None:
    entry = _entry(example_raw, load_doc)
    assert entry.name == "Thimbleberry Bakehouse" and entry.spoken_name == "Thimbleberry Bakehouse"
    assert entry.summary_spoken is not None and entry.summary_spoken.startswith("A neighborhood bakery")
    assert entry.dietary_tags == ["gluten_free", "vegan"]  # gf-vanilla choice + sourdough item
    assert entry.localities == ["springfield", "chatham"]  # storefront address + service area
    assert entry.postal_codes == ["62701", "62702", "62703", "62704", "62629"]
    assert entry.synonyms == ["thimbleberry", "thimbleberry bakery", "the bakery on elm"]
    assert {"celebration cake", "birthday cake", "cinnamon bun"} <= set(entry.item_terms)
    assert entry.module_types == ["booking", "catalog", "custom_order", "knowledge", "owner_approval", "quote_request"]


@pytest.mark.parametrize("name", ["quillfern-salon", "kettlewick-home"])
def test_other_gold_entries(
    name: str, gold_raw: Callable[[str], dict[str, Any]], load_doc: Callable[..., LoadedProfile]
) -> None:
    entry = _entry(gold_raw(name), load_doc)
    assert entry.business_id == name and entry.categories and entry.item_terms and entry.localities
    assert DirectoryEntry.model_validate_json(entry.model_dump_json()) == entry


def _open_contradiction(doc: dict[str, Any], targets: list[str]) -> None:
    source = next(s["url"] for s in doc["sources"].values() if s["kind"] == "web_page")
    other = [s["url"] for s in doc["sources"].values() if s["kind"] == "web_page"][1]
    doc["open_questions"].append(
        {
            "id": "q-test",
            "kind": "contradiction",
            "targets": targets,
            "prompt": "Which is right?",
            "why": "Two pages disagree.",
            "candidates": [
                {"value": "a", "label": "A", "evidence": {"url": source, "quote": "a"}},
                {"value": "b", "label": "B", "evidence": {"url": other, "quote": "b"}},
            ],
            "blocking": False,
            "status": "open",
        }
    )


def test_disputed_facts_are_left_out(example_raw: dict[str, Any], load_doc: Callable[..., LoadedProfile]) -> None:
    _open_contradiction(
        example_raw,
        ["business#keywords", "locations/main#address", "menu/sourdough#dietary", "menu/cinnamon-roll#synonyms"],
    )
    entry = _entry(example_raw, load_doc)
    assert entry.keywords == []
    assert entry.localities == ["springfield", "chatham"]  # service area still lists Springfield
    assert entry.postal_codes == ["62701", "62702", "62703", "62704", "62629"]
    assert entry.dietary_tags == ["gluten_free"]  # vegan came only from the disputed sourdough fact
    assert "cinnamon bun" not in entry.item_terms and "cinnamon roll" in entry.item_terms


def test_disabled_and_extension_modules_are_not_offered(
    example_raw: dict[str, Any], load_doc: Callable[..., LoadedProfile]
) -> None:
    example_raw["modules"]["catering"]["enabled"] = False
    example_raw["modules"]["waitlist"] = {"type": "x-waitlist"}
    entry = _entry(example_raw, load_doc)
    assert "quote_request" not in entry.module_types and "x-waitlist" not in entry.module_types
