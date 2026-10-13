"""Fact references: parse `<scope>[/<id>...]#<field>[.<subfield>]` and resolve them against a profile.

Scope is `business`, `locations` or a module id. Resolution rules (stable ids, never list positions):

  business#name                                   -> business.name
  locations/main#hours                            -> location "main" .hours
  menu#facility_may_contain                       -> module "menu" .facility_may_contain
  menu/celebration-cake#price                     -> catalog item .price
  menu/celebration-cake/size/10in#price           -> item -> option group "size" -> choice "10in" .price
  info/deposit#facts.deposit_percent              -> knowledge entry .facts.deposit_percent
  custom-cakes#availability.capacity_per_day      -> module .availability.capacity_per_day
  appointments/stylist-ana#attributes.level       -> booking resource (also slotting session, visit window)

Within a module the entity id is looked up in, in order: `items` (catalog items), `entries`,
`resources`, `slotting.sessions`, `visit_windows`, `categories`. A catalog item's next two path
segments are an option group id and a choice id.

Provenance follows L5 (nearest enclosing `provenance`): the field's own value if it is an object with
`provenance` (hours, allergens, dietary, availability, fulfillment, facility_may_contain), else the
entity's, else the module's, else (for business/locations) the business's. "The entity's" walks up the
entity chain: a choice is covered by its item, a slotting session by `slotting.provenance`.

Disabled modules (`enabled: false`) resolve as absent at answer time; the loader's L1 check still
treats their refs as valid.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from shopfront_server import errors
from shopfront_server.profile.model import JsonObj, Profile, entity_dicts, is_enabled, is_extension_module

FACT_REF_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9_-]{0,63}(/[a-z0-9][a-z0-9_-]{0,63}){0,3}#[a-z][a-z0-9_]{0,39}(\.[a-z][a-z0-9_]{0,39}){0,3}$"
)
"""Same pattern as `$defs.fact_ref` in the schema (always applied with `fullmatch`)."""

RESERVED_SCOPES: frozenset[str] = frozenset({"business", "locations"})


class FactRefError(ValueError):
    """Malformed fact ref (does not match the schema pattern)."""


@dataclass(frozen=True, order=True)
class FactRef:
    """A parsed fact ref. `str(ref)` round-trips to the canonical text."""

    scope: str
    path: tuple[str, ...]
    field: str
    subfields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.path, tuple) or not isinstance(self.subfields, tuple):
            raise FactRefError("FactRef.path and FactRef.subfields must be tuples")
        if not FACT_REF_PATTERN.fullmatch(self._text()):
            raise FactRefError(f"Malformed fact ref {self._text()!r}")

    @classmethod
    def parse(cls, text: str) -> FactRef:
        """Parse and validate. Raises `FactRefError`."""
        if not isinstance(text, str) or not FACT_REF_PATTERN.fullmatch(text):
            raise FactRefError(f"Malformed fact ref {text!r}")
        entity, _, fields = text.partition("#")
        scope, *path = entity.split("/")
        first, *subfields = fields.split(".")
        return cls(scope, tuple(path), first, tuple(subfields))

    def _text(self) -> str:
        return f"{'/'.join((self.scope, *self.path))}#{'.'.join((self.field, *self.subfields))}"

    def __str__(self) -> str:
        return self._text()

    @property
    def entity(self) -> str:
        """The part before `#`, e.g. `menu/celebration-cake`."""
        return "/".join((self.scope, *self.path))

    @property
    def field_path(self) -> tuple[str, ...]:
        """`(field, *subfields)`."""
        return (self.field, *self.subfields)

    def child(self, *subfields: str) -> FactRef:
        """Same entity, deeper field path (`hours` -> `hours.weekly`). Raises `FactRefError` if too deep."""
        return FactRef(self.scope, self.path, self.field, (*self.subfields, *subfields))

    def overlaps(self, other: FactRef) -> bool:
        """True if one ref addresses the other or something inside it.

        Same entity and one field path is a prefix of the other (`#hours` vs `#hours.weekly`), or the
        same field on an entity nested under the other's entity (`menu/cake#allergens` vs
        `menu/cake/sponge/gf#allergens` overlap; a contradiction on an item's price withholds choice
        prices too). Used by the statable rule (L6).
        """
        if self.scope != other.scope:
            return False
        if not _is_prefix(self.field_path, other.field_path) and not _is_prefix(other.field_path, self.field_path):
            return False
        return _is_prefix(self.path, other.path) or _is_prefix(other.path, self.path)


def _is_prefix(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    return len(short) <= len(long) and long[: len(short)] == short


@dataclass(frozen=True)
class Evidence:
    url: str
    quote: str

    @property
    def is_owner(self) -> bool:
        """True for `urn:shopfront:owner-(answer|edit):<id>` evidence."""
        return self.url.startswith("urn:shopfront:owner-")


@dataclass(frozen=True)
class ResolvedFact:
    """The outcome of resolving a fact ref. Never raised for absent facts: `present` says so."""

    ref: FactRef
    present: bool
    value: Any = None
    provenance: JsonObj | None = None
    owner_confirmed: bool = False
    evidence: tuple[Evidence, ...] = ()
    in_extension: bool = False
    """True if the value sits under an `x-*` key or `x-*` module (never statable, L5)."""
    entity: JsonObj | None = None
    notes: tuple[str, ...] = field(default=())

    @property
    def evidence_url(self) -> str | None:
        """The citation shown on cards: the first web page evidence, else the first owner URN."""
        for item in self.evidence:
            if item.url.startswith(("http://", "https://")):
                return item.url
        for item in self.evidence:
            if item.is_owner:
                return item.url
        return None


# --------------------------------------------------------------------------- entity location

EntityKind = Literal[
    "business",
    "location",
    "module",
    "item",
    "option_group",
    "choice",
    "entry",
    "resource",
    "session",
    "visit_window",
    "category",
]


@dataclass(frozen=True)
class Located:
    """Where a fact ref's entity lives in the raw document."""

    kind: EntityKind
    entity: JsonObj
    chain: tuple[JsonObj, ...]
    """Enclosing objects that may carry provenance, outermost first, ending with `entity`."""
    module_id: str | None = None
    module_type: str | None = None
    in_extension: bool = False


def locate(raw: Mapping[str, Any], ref: FactRef, *, include_disabled: bool = False) -> Located | str:
    """Find the entity a fact ref addresses, or return why it cannot be found.

    `include_disabled=True` (used by the loader's L1 check) also searches disabled modules.
    """
    business = raw.get("business")
    if not isinstance(business, Mapping):
        return "profile has no business"
    if ref.scope == "business":
        if ref.path:
            return "business has no nested entities"
        return Located("business", business, (business,))
    if ref.scope == "locations":
        if len(ref.path) != 1:
            return "locations refs take exactly one location id"
        for location in entity_dicts(business.get("locations")):
            if location["id"] == ref.path[0]:
                return Located("location", location, (business, location))
        return f"unknown location {ref.path[0]!r}"

    modules = raw.get("modules")
    module = modules.get(ref.scope) if isinstance(modules, Mapping) else None
    if not isinstance(module, Mapping):
        return f"unknown module {ref.scope!r}"
    if not include_disabled and not is_enabled(module):
        return f"module {ref.scope!r} is disabled"
    module_type = module.get("type") if isinstance(module.get("type"), str) else None
    common = {
        "module_id": ref.scope,
        "module_type": module_type,
        "in_extension": is_extension_module(ref.scope, module),
    }
    if not ref.path:
        return Located("module", module, (module,), **common)

    entity_id, rest = ref.path[0], ref.path[1:]
    for item in entity_dicts(module.get("items")):
        if item["id"] == entity_id:
            return _locate_in_item(module, item, rest, common)
    slotting = module.get("slotting") if isinstance(module.get("slotting"), Mapping) else None
    searches: list[tuple[EntityKind, Any, tuple[JsonObj, ...]]] = [
        ("entry", module.get("entries"), (module,)),
        ("resource", module.get("resources"), (module,)),
        ("session", slotting.get("sessions") if slotting else None, (module, slotting) if slotting else (module,)),
        ("visit_window", module.get("visit_windows"), (module,)),
        ("category", module.get("categories"), (module,)),
    ]
    for kind, entities, parents in searches:
        for entity in entity_dicts(entities):
            if entity["id"] == entity_id:
                if rest:
                    return f"{kind} {entity_id!r} has no nested entities"
                return Located(kind, entity, (*parents, entity), **common)
    return f"unknown entity {entity_id!r} in module {ref.scope!r}"


def _locate_in_item(module: JsonObj, item: JsonObj, rest: tuple[str, ...], common: dict[str, Any]) -> Located | str:
    if not rest:
        return Located("item", item, (module, item), **common)
    for group in entity_dicts(item.get("options")):
        if group["id"] == rest[0]:
            if len(rest) == 1:
                return Located("option_group", group, (module, item, group), **common)
            for choice in entity_dicts(group.get("choices")):
                if choice["id"] == rest[1]:
                    if len(rest) > 2:
                        return f"choice {rest[1]!r} has no nested entities"
                    return Located("choice", choice, (module, item, group, choice), **common)
            return f"unknown choice {rest[1]!r} in option group {rest[0]!r} of item {item['id']!r}"
    return f"unknown option group {rest[0]!r} in item {item['id']!r}"


_MISSING = object()


def _walk(entity: JsonObj, field_path: tuple[str, ...]) -> tuple[Any, list[JsonObj], int]:
    """Follow `field_path` from `entity`.

    Returns `(value or _MISSING, objects passed through including a final object value, steps taken)`.
    """
    current: Any = entity
    passed: list[JsonObj] = []
    for depth, name in enumerate(field_path):
        if not isinstance(current, Mapping) or name not in current:
            return _MISSING, passed, depth
        current = current[name]
        if isinstance(current, Mapping):
            passed.append(current)
    return current, passed, len(field_path)


def _nearest_provenance(objects: list[JsonObj]) -> JsonObj | None:
    for obj in reversed(objects):
        provenance = obj.get("provenance")
        if isinstance(provenance, Mapping):
            return provenance
    return None


def _evidence(provenance: JsonObj | None) -> tuple[Evidence, ...]:
    if provenance is None:
        return ()
    return tuple(
        Evidence(url=str(e.get("url", "")), quote=str(e.get("quote", "")))
        for e in provenance.get("evidence") or ()
        if isinstance(e, Mapping)
    )


def resolve(profile: Profile, ref: FactRef | str) -> ResolvedFact:
    """Resolve `ref` against `profile` following the rules in the module docstring.

    Unknown scope/entity/field -> `ResolvedFact(present=False)`. Malformed text -> `FactRefError`.
    """
    fact_ref = ref if isinstance(ref, FactRef) else FactRef.parse(ref)
    located = locate(profile.raw, fact_ref)
    if isinstance(located, str):
        return ResolvedFact(ref=fact_ref, present=False, notes=(located,))
    value, passed, depth = _walk(located.entity, fact_ref.field_path)
    if value is _MISSING:
        missing = ".".join(fact_ref.field_path[: depth + 1])
        return ResolvedFact(
            ref=fact_ref,
            present=False,
            entity=located.entity,
            in_extension=located.in_extension,
            notes=(f"{located.kind} {fact_ref.entity!r} has no {missing!r}",),
        )
    provenance = _nearest_provenance([*located.chain, *passed])
    return ResolvedFact(
        ref=fact_ref,
        present=True,
        value=value,
        provenance=provenance,
        owner_confirmed=bool(provenance.get("owner_confirmed")) if provenance else False,
        evidence=_evidence(provenance),
        in_extension=located.in_extension,
        entity=located.entity,
    )


def entity_ref(profile: Profile, module_id: str, *ids: str) -> str:
    """Build `<module>/<id>/...` after checking each id exists. Raises `errors.NotFound`."""
    try:
        probe = FactRef(module_id, tuple(ids), "id")
    except FactRefError as exc:
        raise errors.NotFound(f"Invalid entity ref {'/'.join((module_id, *ids))!r}") from exc
    if module_id in RESERVED_SCOPES:
        raise errors.NotFound(f"{module_id!r} is not a module")
    located = locate(profile.raw, probe)
    if isinstance(located, str):
        raise errors.NotFound(located)
    return probe.entity


# --------------------------------------------------------------------------- field-name knowledge (L1)

_MODULE_COMMON = frozenset({"type", "title", "enabled", "provenance"})
_TRANSACTIONAL = _MODULE_COMMON | {"intake_fields", "confirmation", "availability", "fulfillment", "approval"}
MODULE_FIELDS: dict[str, frozenset[str]] = {
    "catalog": _MODULE_COMMON | {"categories", "items", "facility_may_contain"},
    "knowledge": _MODULE_COMMON | {"entries"},
    "booking": _TRANSACTIONAL | {"services", "resources", "slotting"},
    "custom_order": _TRANSACTIONAL | {"items"},
    "quote_request": _TRANSACTIONAL | {"items", "visit_windows"},
    "owner_approval": _MODULE_COMMON
    | {
        "approver",
        "channel",
        "expected_response_minutes",
        "expires_after_minutes",
        "on_expiry",
        "answers_unknown_questions",
    },
}
"""Addressable fields of each module type (mirrors the schema's `*_module` definitions)."""

ENTITY_FIELDS: dict[str, frozenset[str]] = {
    "business": frozenset(
        {
            "name",
            "spoken_name",
            "synonyms",
            "summary_spoken",
            "categories",
            "keywords",
            "locale",
            "timezone",
            "currency",
            "website",
            "contact",
            "locations",
            "provenance",
        }
    ),
    "location": frozenset({"id", "kind", "name", "spoken_area", "address", "area", "hours", "provenance"}),
    "item": frozenset(
        {
            "id",
            "name",
            "spoken_name",
            "synonyms",
            "description_spoken",
            "category_ids",
            "price",
            "duration_minutes",
            "options",
            "attributes",
            "allergens",
            "dietary",
            "availability",
            "provenance",
        }
    ),
    "option_group": frozenset({"id", "name", "spoken_question", "required", "max_selections", "choices"}),
    "choice": frozenset(
        {
            "id",
            "name",
            "spoken_name",
            "synonyms",
            "price",
            "price_delta",
            "duration_minutes",
            "attributes",
            "allergens",
            "dietary",
        }
    ),
    "entry": frozenset(
        {"id", "topic", "question_variants", "answer_spoken", "answer_card", "facts", "applies_to", "provenance"}
    ),
    "resource": frozenset({"id", "kind", "spoken_name", "attributes", "provenance"}),
    "session": frozenset({"id", "item", "days", "start", "capacity", "resource_id", "location_id"}),
    "visit_window": frozenset({"id", "spoken_name", "start", "end"}),
    "category": frozenset({"id", "name", "spoken_name"}),
}
"""Addressable fields of each entity kind (mirrors the schema)."""

_PRICE_FIELDS = frozenset({"kind", "amount_cents", "max_cents", "currency", "per"})
OBJECT_FIELDS: dict[str, frozenset[str]] = {
    "hours": frozenset({"weekly", "closures", "note_spoken", "provenance"}),
    "address": frozenset({"line1", "line2", "locality", "region", "postal_code", "country"}),
    "area": frozenset({"postal_codes", "localities", "radius"}),
    "radius": frozenset({"distance", "unit", "from_location"}),
    "contact": frozenset({"phone", "email"}),
    "price": _PRICE_FIELDS,
    "price_delta": _PRICE_FIELDS,
    "fee": _PRICE_FIELDS,
    "allergens": frozenset({"contains", "may_contain", "provenance"}),
    "dietary": frozenset({"tags", "provenance"}),
    "availability": frozenset(
        {
            "status",
            "days",
            "note_spoken",
            "weekly",
            "closures",
            "capacity_per_slot",
            "capacity_per_day",
            "lead_time_minutes",
            "max_advance_minutes",
            "provenance",
        }
    ),
    "fulfillment": frozenset({"modes", "location_ids", "fee", "provenance"}),
    "facility_may_contain": frozenset({"allergens", "provenance"}),
    "approver": frozenset({"spoken_name", "role"}),
    "slotting": frozenset({"mode", "step_minutes", "sessions", "provenance"}),
    "approval": frozenset({"module", "mode", "rules", "decisions", "response_fields"}),
    "confirmation": frozenset({"before_commit", "idempotency", "read_back", "say_before_confirm"}),
    "provenance": frozenset({"owner_confirmed", "evidence"}),
}
"""Sub-fields of the schema's typed objects, keyed by the field name that holds them."""

ATTRIBUTE_CONTAINERS: frozenset[str] = frozenset({"attributes", "facts"})
"""Fields whose keys come from `vocabulary.attributes` (item/choice/resource attributes, entry facts)."""


def _allowed_names(located: Located, parents: tuple[str, ...], vocabulary: frozenset[str]) -> frozenset[str] | None:
    """Names that may follow `parents` on this entity, or None when anything goes (x-* modules)."""
    if located.in_extension:
        return None
    if not parents:
        if located.kind == "module":
            return MODULE_FIELDS.get(located.module_type or "", frozenset())
        return ENTITY_FIELDS[located.kind]
    parent = parents[-1]
    if parent in ATTRIBUTE_CONTAINERS and len(parents) == 1:
        return vocabulary
    return OBJECT_FIELDS.get(parent, frozenset())


def explain_unresolvable(raw: Mapping[str, Any], ref: FactRef) -> str | None:
    """Why `ref` does not resolve in `raw` (L1), or None if it does.

    The entity must exist (disabled modules included). The field path may be absent (a `missing` or
    `confirm` question targets a fact the profile does not state yet), but every name in it must be one
    the schema defines at that position, and `attributes` / `facts` keys must be declared vocabulary.
    """
    located = locate(raw, ref, include_disabled=True)
    if isinstance(located, str):
        return located
    vocabulary_obj = raw.get("vocabulary") if isinstance(raw.get("vocabulary"), Mapping) else {}
    attributes = vocabulary_obj.get("attributes") if isinstance(vocabulary_obj, Mapping) else None
    vocabulary = frozenset(attributes) if isinstance(attributes, Mapping) else frozenset()

    current: Any = located.entity
    for depth, name in enumerate(ref.field_path):
        allowed = _allowed_names(located, ref.field_path[:depth], vocabulary)
        if isinstance(current, Mapping) and name in current:
            current = current[name]
            continue
        if current is not _MISSING and not isinstance(current, Mapping):
            return f"cannot address {name!r} inside a {type(current).__name__} ({'.'.join(ref.field_path[:depth])})"
        if allowed is not None and name not in allowed:
            return f"{located.kind} {ref.entity!r} has no field {'.'.join(ref.field_path[: depth + 1])!r}"
        current = _MISSING
    return None
