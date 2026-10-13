"""`Profile`: a read-only, typed view over one validated Business Profile document.

The raw document stays the source of truth (schema: `schema/business-profile.schema.json`, design:
`docs/design/profile-schema.md`). This class only adds lookups the tools and domain need, so nobody
walks the raw dict by hand. Everything returned is a read-only view of the raw JSON (`JsonObj`);
callers must not mutate it.

Immutability: the constructor deep-copies the input into `FrozenDict` / `FrozenList` containers (dict and
list subclasses whose mutating methods raise `TypeError`). They still behave as plain JSON for
`json.dumps`, pydantic (`JsonValue`, `Any`) and RFC 8785, so values can flow into tool results unchanged.
`copy.deepcopy` of a frozen value (and `thaw`) returns ordinary mutable dicts and lists.

Ref conventions (from the schema):
  * an entity ref is `<module id>/<entity id>`, e.g. `menu/celebration-cake`;
  * a fact ref is `<scope>[/<id>...]#<field>[.<subfield>]` (see `profile.factref`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import cached_property
from typing import Any, Literal, NoReturn, cast
from zoneinfo import ZoneInfo

from askonce_server import errors
from askonce_server.canonical import profile_sha256

JsonObj = Mapping[str, Any]
"""A read-only JSON object from the profile."""

ModuleType = Literal["catalog", "knowledge", "booking", "custom_order", "quote_request", "owner_approval"]
TRANSACTIONAL_TYPES: frozenset[str] = frozenset({"booking", "custom_order", "quote_request"})
CONTACT_FIELD_TYPES: frozenset[str] = frozenset({"phone", "email"})
"""Intake field types that reach the customer (always `pii`). A transactional module must collect one
(loader invariant L11) and every request must carry a value for one (`domain.intake.normalize_intake`):
without auth, the contact is what tells two customers' otherwise identical requests apart."""


def has_contact_field(fields: Sequence[JsonObj]) -> bool:
    """True if `intake_fields` include a phone or email field (`CONTACT_FIELD_TYPES`)."""
    return any(isinstance(f, Mapping) and f.get("type") in CONTACT_FIELD_TYPES for f in fields)
CertificationStatus = Literal["draft", "failed", "certified", "live"]

_EMPTY: Mapping[str, Any] = {}


# --------------------------------------------------------------------------- frozen JSON containers


def _immutable(self: object, *_args: object, **_kwargs: object) -> NoReturn:
    raise TypeError(f"{type(self).__name__} is read-only (profile data); copy it with thaw() first")


class FrozenDict(dict[str, Any]):
    """A `dict` whose mutating methods raise `TypeError`. JSON- and pydantic-compatible."""

    __slots__ = ()

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable  # type: ignore[assignment]
    clear = _immutable
    pop = _immutable  # type: ignore[assignment]
    popitem = _immutable
    setdefault = _immutable  # type: ignore[assignment]
    update = _immutable  # type: ignore[assignment]

    def __reduce__(self) -> tuple[type[FrozenDict], tuple[dict[str, Any]]]:
        return (FrozenDict, (dict(self),))

    def __copy__(self) -> dict[str, Any]:
        return dict(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], thaw(self))


class FrozenList(list[Any]):
    """A `list` whose mutating methods raise `TypeError`. JSON- and pydantic-compatible."""

    __slots__ = ()

    __setitem__ = _immutable  # type: ignore[assignment]
    __delitem__ = _immutable  # type: ignore[assignment]
    __iadd__ = _immutable  # type: ignore[assignment]
    __imul__ = _immutable  # type: ignore[assignment]
    append = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable  # type: ignore[assignment]
    remove = _immutable
    clear = _immutable
    sort = _immutable  # type: ignore[assignment]
    reverse = _immutable

    def __reduce__(self) -> tuple[type[FrozenList], tuple[list[Any]]]:
        return (FrozenList, (list(self),))

    def __copy__(self) -> list[Any]:
        return list(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> list[Any]:
        return cast(list[Any], thaw(self))


def freeze(value: Any) -> Any:
    """Deep copy of a JSON value into `FrozenDict` / `FrozenList` containers (scalars are shared)."""
    if isinstance(value, Mapping):
        return FrozenDict({str(k): freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return FrozenList(freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    """Deep copy of a (possibly frozen) JSON value into ordinary mutable dicts and lists."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(v) for v in value]
    return value


def is_extension_module(module_id: str, module: JsonObj) -> bool:
    """True for `x-*` module types (and `x-*` module ids): extensions whose facts are never statable."""
    module_type = module.get("type")
    return module_id.startswith("x-") or (isinstance(module_type, str) and module_type.startswith("x-"))


def is_enabled(module: JsonObj) -> bool:
    """`enabled` defaults to true (schema `module_common`)."""
    return module.get("enabled", True) is not False


def entity_dicts(value: Any) -> list[JsonObj]:
    """The id-bearing objects of a JSON list (skips ref strings, e.g. custom_order `items`)."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, Mapping) and isinstance(v.get("id"), str)]


# --------------------------------------------------------------------------- Profile


class Profile:
    """Read-only view of a profile that passed `profile.loader.load_profile`.

    Construct through the loader (or `Profile.unchecked` in tests that deliberately bypass validation).
    Instances are immutable and safe to share across requests and tasks.
    """

    def __init__(self, raw: Mapping[str, Any], *, sha256: str) -> None:
        """`raw` must already be schema-valid; `sha256` is `canonical.profile_sha256(raw)`."""
        if not isinstance(raw, Mapping):
            raise TypeError("a profile document must be a JSON object")
        self._raw: FrozenDict = freeze(raw)
        self._sha256 = sha256

    @classmethod
    def unchecked(cls, raw: Mapping[str, Any]) -> Profile:
        """Wrap `raw` without validation (computes the sha). Test helper only."""
        return cls(raw, sha256=profile_sha256(raw))

    def __repr__(self) -> str:
        return f"Profile({self.profile_id!r}, revision={self.revision}, status={self.status!r})"

    def to_dict(self) -> dict[str, Any]:
        """A mutable deep copy of the whole document (for editing and persistence)."""
        return cast(dict[str, Any], thaw(self._raw))

    # ---- identity & lifecycle ---------------------------------------------------------------

    @property
    def raw(self) -> JsonObj:
        """The whole document (read-only view)."""
        return self._raw

    @property
    def profile_id(self) -> str:
        """Tenant id == `business_id` in every tool."""
        return str(self._raw["profile_id"])

    @property
    def revision(self) -> int:
        return int(self._raw["revision"])

    @property
    def sha256(self) -> str:
        """sha256(JCS(profile without certification)), see `canonical.profile_sha256`."""
        return self._sha256

    @property
    def status(self) -> CertificationStatus:
        """`certification.status`."""
        certification = self._raw.get("certification") or _EMPTY
        return cast(CertificationStatus, certification.get("status", "draft"))

    @property
    def is_live(self) -> bool:
        """Only live profiles are discoverable or callable by customers."""
        return self.status == "live"

    # ---- business ------------------------------------------------------------------------------

    @property
    def business(self) -> JsonObj:
        return cast(JsonObj, self._raw.get("business") or FrozenDict())

    @property
    def name(self) -> str:
        """`business.name` (cards)."""
        return str(self.business["name"])

    @property
    def spoken_name(self) -> str:
        """`business.spoken_name` (speech)."""
        return str(self.business["spoken_name"])

    @cached_property
    def timezone(self) -> ZoneInfo:
        """`business.timezone` as a ZoneInfo (validated against tzdata by L4)."""
        return ZoneInfo(str(self.business["timezone"]))

    @property
    def currency(self) -> str:
        return str(self.business["currency"])

    @property
    def locale(self) -> str:
        return str(self.business["locale"])

    def locations(self) -> Sequence[JsonObj]:
        return cast(Sequence[JsonObj], self.business.get("locations") or FrozenList())

    def location(self, location_id: str) -> JsonObj:
        """Raises `errors.NotFound` for an unknown id."""
        found = self._locations_by_id.get(location_id)
        if found is None:
            raise errors.NotFound(f"Unknown location {location_id!r}")
        return found

    def primary_location(self) -> JsonObj | None:
        """First storefront/mobile location (whose hours are the default availability), if any."""
        for location in self.locations():
            if location.get("kind") in ("storefront", "mobile"):
                return location
        return None

    @cached_property
    def _locations_by_id(self) -> dict[str, JsonObj]:
        out: dict[str, JsonObj] = {}
        for location in self.locations():
            out.setdefault(str(location.get("id")), location)
        return out

    # ---- modules ---------------------------------------------------------------------------------

    def modules(self) -> Mapping[str, JsonObj]:
        """All modules keyed by id, including `x-*` extension modules (callers must skip those)."""
        return cast(Mapping[str, JsonObj], self._raw.get("modules") or FrozenDict())

    def module(self, module_id: str) -> JsonObj:
        """Raises `errors.UnknownModule` (also for disabled modules: `enabled: false`)."""
        module = self.modules().get(module_id)
        if module is None or not is_enabled(module):
            raise errors.UnknownModule(f"Unknown module {module_id!r}")
        return module

    def modules_of_type(self, module_type: ModuleType) -> list[tuple[str, JsonObj]]:
        """Enabled modules of one type, in document order."""
        return [
            (module_id, module)
            for module_id, module in self.modules().items()
            if module.get("type") == module_type and is_enabled(module) and not is_extension_module(module_id, module)
        ]

    def module_type(self, module_id: str) -> str:
        return str(self.module(module_id)["type"])

    # ---- catalog -----------------------------------------------------------------------------------

    def catalog_items(self) -> list[tuple[str, JsonObj]]:
        """Every `(catalog module id, item)` across catalog modules."""
        return [
            (module_id, item)
            for module_id, module in self.modules_of_type("catalog")
            for item in entity_dicts(module.get("items"))
        ]

    def item(self, ref: str) -> tuple[str, JsonObj]:
        """Resolve `<catalog module>/<item id>` (or a bare item id if unique) to `(module_id, item)`.

        Raises `errors.UnknownItem`.
        """
        if "/" in ref:
            module_id, _, item_id = ref.partition("/")
            found = self._items_by_ref.get((module_id, item_id))
            if found is None:
                raise errors.UnknownItem(f"Unknown item {ref!r}")
            return module_id, found
        matches = self._items_by_bare_id.get(ref, [])
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise errors.UnknownItem(f"Unknown item {ref!r}")
        raise errors.UnknownItem(f"Ambiguous item {ref!r}: use <catalog module>/<item id>")

    def item_ref(self, module_id: str, item: JsonObj) -> str:
        """`<module id>/<item id>`."""
        return f"{module_id}/{item['id']}"

    def option_choice(self, item: JsonObj, group_id: str, choice_id: str) -> JsonObj:
        """A choice of an item's option group. Raises `errors.UnknownItem` for unknown ids."""
        for group in entity_dicts(item.get("options")):
            if group["id"] == group_id:
                for choice in entity_dicts(group.get("choices")):
                    if choice["id"] == choice_id:
                        return choice
                raise errors.UnknownItem(f"Unknown choice {choice_id!r} in option {group_id!r}")
        raise errors.UnknownItem(f"Unknown option group {group_id!r}")

    def facility_may_contain(self, catalog_module_id: str) -> JsonObj | None:
        """The catalog's kitchen-wide cross-contact statement, if any."""
        return cast(JsonObj | None, self.module(catalog_module_id).get("facility_may_contain"))

    def item_categories(self, item_ref: str) -> list[str]:
        """Category refs (`<catalog module>/<category id>`) the item belongs to."""
        module_id, item = self.item(item_ref)
        return [f"{module_id}/{category_id}" for category_id in item.get("category_ids") or ()]

    @cached_property
    def _items_by_ref(self) -> dict[tuple[str, str], JsonObj]:
        out: dict[tuple[str, str], JsonObj] = {}
        for module_id, item in self.catalog_items():
            out.setdefault((module_id, str(item["id"])), item)
        return out

    @cached_property
    def _items_by_bare_id(self) -> dict[str, list[tuple[str, JsonObj]]]:
        out: dict[str, list[tuple[str, JsonObj]]] = {}
        for (module_id, item_id), item in self._items_by_ref.items():
            out.setdefault(item_id, []).append((module_id, item))
        return out

    # ---- knowledge -------------------------------------------------------------------------------------

    def knowledge_entries(self) -> list[tuple[str, JsonObj]]:
        """Every `(knowledge module id, entry)`."""
        return [
            (module_id, entry)
            for module_id, module in self.modules_of_type("knowledge")
            for entry in entity_dicts(module.get("entries"))
        ]

    def knowledge_entry(self, ref: str) -> JsonObj:
        """Resolve `<knowledge module>/<entry id>` (used by `confirmation.say_before_confirm`).

        Raises `errors.NotFound`.
        """
        module_id, _, entry_id = ref.partition("/")
        for entry_module_id, entry in self.knowledge_entries():
            if entry_module_id == module_id and entry["id"] == entry_id:
                return entry
        raise errors.NotFound(f"Unknown knowledge entry {ref!r}")

    # ---- transactional modules -------------------------------------------------------------------------

    def transactional_modules_for(self, item_ref: str, module_type: ModuleType | None = None) -> list[str]:
        """Ids of booking/custom_order/quote_request modules that accept `item_ref`.

        A module accepts an item if it lists the item ref, or a catalog category the item belongs to
        (custom_order/quote_request `items`), or a booking `services[].item` equal to the ref.
        Unknown items accept nothing (empty list). A bare unique item id is accepted too.
        """
        try:
            catalog_id, item = self.item(item_ref)
        except errors.UnknownItem:
            return []
        canonical = self.item_ref(catalog_id, item)
        accepted_refs = {canonical, *(f"{catalog_id}/{c}" for c in item.get("category_ids") or ())}
        wanted_types = {module_type} if module_type else set(TRANSACTIONAL_TYPES)
        out: list[str] = []
        for module_id, module in self.modules().items():
            kind = module.get("type")
            if kind not in wanted_types or kind not in TRANSACTIONAL_TYPES:
                continue
            if not is_enabled(module) or is_extension_module(module_id, module):
                continue
            if kind == "booking":
                if any(service.get("item") == canonical for service in module.get("services") or ()):
                    out.append(module_id)
            elif any(ref in accepted_refs for ref in module.get("items") or ()):
                out.append(module_id)
        return out

    def intake_fields(self, module_id: str) -> Sequence[JsonObj]:
        return cast(Sequence[JsonObj], self.module(module_id).get("intake_fields") or FrozenList())

    def pii_keys(self, module_id: str) -> frozenset[str]:
        """Intake keys marked `pii: true` (redacted from traces and logs)."""
        return frozenset(str(f["key"]) for f in self.intake_fields(module_id) if f.get("pii") is True)

    def owner_approval(self, module_id: str | None = None) -> tuple[str, JsonObj]:
        """The owner_approval module for `module_id`'s `approval.module`, or the first one if None.

        A `module_id` without an `approval` block (or naming an owner_approval module itself) falls back
        sensibly. Raises `errors.UnknownModule` if the profile has no owner_approval module.
        """
        if module_id is not None:
            module = self.module(module_id)
            if module.get("type") == "owner_approval":
                return module_id, module
            approval = module.get("approval")
            if isinstance(approval, Mapping) and isinstance(approval.get("module"), str):
                target_id = str(approval["module"])
                target = self.module(target_id)
                if target.get("type") != "owner_approval":
                    raise errors.UnknownModule(f"{target_id!r} is not an owner_approval module")
                return target_id, target
        approvers = self.modules_of_type("owner_approval")
        if not approvers:
            raise errors.UnknownModule("This business has no owner_approval module")
        return approvers[0]

    def approver_spoken_name(self, module_id: str | None = None) -> str:
        """`approver.spoken_name` ('Rosa'), used in hand-off offers. Falls back to the business spoken name."""
        try:
            _, module = self.owner_approval(module_id)
        except errors.UnknownModule:
            return self.spoken_name
        approver = module.get("approver") or _EMPTY
        name = approver.get("spoken_name")
        return str(name) if name else self.spoken_name

    # ---- provenance support ------------------------------------------------------------------------------

    def open_questions(self) -> list[JsonObj]:
        """Questions with `status: open`."""
        return [q for q in self._raw.get("open_questions") or () if q.get("status") == "open"]

    def source_by_url(self, url: str) -> tuple[str, JsonObj] | None:
        """`(source id, source)` whose `url` equals `url`."""
        for source_id, source in (self._raw.get("sources") or _EMPTY).items():
            if source.get("url") == url:
                return source_id, source
        return None

    def injection_canaries(self) -> list[str]:
        """Every `sources.*.injection_canaries[].quote` (never allowed in output, L8)."""
        return [
            str(canary["quote"])
            for source in (self._raw.get("sources") or _EMPTY).values()
            for canary in source.get("injection_canaries") or ()
            if canary.get("quote")
        ]

    def vocabulary_attribute(self, key: str) -> JsonObj | None:
        vocabulary = self._raw.get("vocabulary") or _EMPTY
        attributes = vocabulary.get("attributes") or _EMPTY
        return cast(JsonObj | None, attributes.get(key))
