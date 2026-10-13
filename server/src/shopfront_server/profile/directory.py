"""Directory entries: the small, search-ready summary of a live profile used by `find_business`.

Stored separately from the full profile (DynamoDB: `pk=DIRECTORY`, one item per live business) so
discovery never loads every profile document.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, Field

from shopfront_server.profile.factref import FactRef, resolve
from shopfront_server.profile.model import Profile, entity_dicts, is_enabled, is_extension_module
from shopfront_server.profile.statable import statability


class DirectoryEntry(BaseModel):
    """Everything discovery may match on, all taken from statable profile facts."""

    business_id: str
    revision: int
    name: str
    spoken_name: str
    summary_spoken: str | None = None
    categories: list[str]
    keywords: list[str] = Field(default_factory=list)
    synonyms: list[str] = Field(default_factory=list)
    """Business synonyms ('thimbleberry', 'the bakery on elm')."""
    item_terms: list[str] = Field(default_factory=list)
    """Lowercased item names, spoken names and synonyms across catalog modules."""
    dietary_tags: list[str] = Field(default_factory=list)
    """Owner-confirmed dietary tags offered on any item or choice (e.g. gluten_free)."""
    localities: list[str] = Field(default_factory=list)
    """Address localities and service-area localities (lowercased)."""
    postal_codes: list[str] = Field(default_factory=list)
    module_types: list[str] = Field(default_factory=list)
    """Which capabilities the business offers (booking, custom_order, quote_request...)."""


def _statable_value(profile: Profile, ref: FactRef) -> Any:
    """The value of a statable fact, else None (absent, disputed, unprovenanced or extension)."""
    fact = resolve(profile, ref)
    return fact.value if statability(profile, fact).statable else None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _unique_lower(values: Iterable[str]) -> list[str]:
    """Lowercased, stripped, de-duplicated, in first-seen order."""
    seen: dict[str, None] = {}
    for value in values:
        cleaned = " ".join(value.split()).lower()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def build_directory_entry(profile: Profile) -> DirectoryEntry:
    """Summarise a profile. Only statable facts are included (disputed ones are skipped).

    `name` and `spoken_name` are the business's identity and always present; every other field holds
    only statable facts: item terms from statable item names, dietary tags from owner-confirmed item
    and choice dietary facts, localities and postal codes from statable storefront/mobile addresses and
    service-area areas.
    """

    def business(field: str) -> Any:
        return _statable_value(profile, FactRef("business", (), field))

    summary = business("summary_spoken")
    item_terms: list[str] = []
    dietary: list[str] = []
    for module_id, item in profile.catalog_items():
        item_path = (str(item["id"]),)
        for field in ("name", "spoken_name", "synonyms"):
            item_terms += _strings(_statable_value(profile, FactRef(module_id, item_path, field)))
        dietary += _confirmed_tags(profile, FactRef(module_id, item_path, "dietary"))
        for group in entity_dicts(item.get("options")):
            for choice in entity_dicts(group.get("choices")):
                choice_ref = FactRef(module_id, (*item_path, str(group["id"]), str(choice["id"])), "dietary")
                dietary += _confirmed_tags(profile, choice_ref)

    localities: list[str] = []
    postal_codes: list[str] = []
    for location in profile.locations():
        location_path = (str(location["id"]),)
        if location.get("kind") in ("storefront", "mobile"):
            address = _statable_value(profile, FactRef("locations", location_path, "address"))
            if isinstance(address, dict):
                localities += _strings(address.get("locality"))
                postal_codes += _strings(address.get("postal_code"))
        elif location.get("kind") == "service_area":
            area = _statable_value(profile, FactRef("locations", location_path, "area"))
            if isinstance(area, dict):
                localities += _strings(area.get("localities"))
                postal_codes += _strings(area.get("postal_codes"))

    module_types = sorted(
        {
            str(module.get("type"))
            for module_id, module in profile.modules().items()
            if is_enabled(module) and not is_extension_module(module_id, module)
        }
    )
    return DirectoryEntry(
        business_id=profile.profile_id,
        revision=profile.revision,
        name=profile.name,
        spoken_name=profile.spoken_name,
        summary_spoken=summary if isinstance(summary, str) else None,
        categories=_unique_lower(_strings(business("categories"))),
        keywords=_unique_lower(_strings(business("keywords"))),
        synonyms=_unique_lower(_strings(business("synonyms"))),
        item_terms=_unique_lower(item_terms),
        dietary_tags=sorted(set(_unique_lower(dietary))),
        localities=_unique_lower(localities),
        postal_codes=list(dict.fromkeys(code.strip() for code in postal_codes if code.strip())),
        module_types=module_types,
    )


def _confirmed_tags(profile: Profile, ref: FactRef) -> list[str]:
    """Dietary tags of a statable, owner-confirmed `dietary` fact."""
    fact = resolve(profile, ref)
    if not statability(profile, fact).statable or not fact.owner_confirmed or not isinstance(fact.value, dict):
        return []
    return _strings(fact.value.get("tags"))
