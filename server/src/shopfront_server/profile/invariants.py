"""Loader invariants L1-L11 (`x-invariants` in the schema): rules JSON Schema cannot express.

Each check is a pure function `(InvariantContext) -> list[Violation]`, registered in `CHECKS`. The loader
runs them in order after schema validation succeeds. Severity policy:

  * strict mode (prod, certifier): every violation is an error;
  * lenient mode (dev): L3, L7 and L11 violations are downgraded to warnings so illustrative example
    profiles still load; everything else stays an error;
  * L3 with no snapshot available is always a warning with `skipped=True` (never a silent pass).

Invariants (verbatim intent from the schema):
  L1 refs resolve (refs, fact refs, location ids, approval.module -> an owner_approval module,
     session items, transactional `items` name catalog items or categories, say_before_confirm entries).
  L2 ids unique (entity ids within a module, location ids, source urls).
  L3 evidence quote occurs verbatim (NFC + whitespace collapsed) in its source snapshot; every
     evidence url matches a `sources` entry.
  L4 units: every price currency == business.currency; business.timezone exists in tzdata.
  L5 nearest provenance: every statable fact has some enclosing provenance; x-* never statable.
  L6 statable rule (checked at answer time by `profile.statable`; the loader only checks that open
     contradiction targets resolve).
  L7 certification: certified_run is the last run, passed, cases_passed == cases_total, and
     profile_sha256 == sha256(JCS(profile without certification)); runs bind to the current revision.
  L8 canaries: no injection canary quote appears in any spoken/text field of the profile itself
     (the output-time half is enforced by `tools.guard`).
  L9 vocabulary: attribute keys are declared and values match type/enum; approval_rule.when and
     read_back keys name an intake field or a built-in.
  L10 contradictions: candidates have pairwise different values and evidence from different sources.
  L11 contact: every booking, custom_order and quote_request module has a phone or email intake field
     (without auth, the contact is what keeps two customers' identical requests apart).

Implementation notes (where the schema text leaves room):
  * Every walk skips `x-*` keys, and L1/L2/L4/L9/L11 skip `x-*` modules: extension data is opaque to the
    loader and never a statable fact (L3 and L8 still cover them: citations and canaries are hygiene).
  * L1 checks the targets of every question except open `contradiction`/`confirm` ones, which L6
    owns (an unresolvable disputed target would silently fail to withhold anything). A target's entity
    must exist; its field may be absent (a `missing` question targets a fact not stated yet), but every
    field name must exist in the schema at that position and attribute keys must be declared.
  * L1 checks run failure `fact_ref`s only for runs bound to the current revision (older runs are
    append-only history about older documents).
  * L5 checks every entity kind whose facts tools state (business, locations, items and their options
    and choices, knowledge entries, resources, sessions, visit windows, owner_approval settings).
    Category labels and booking-service settings are flow configuration, not stated facts.
  * L8 scans every string outside `sources` (where canaries are quarantined), `certification` and
    `x-*` keys, case-insensitively: evidence quotes reach cards as citations, question prompts reach
    owner elicitations, so they count as output too.
"""

from __future__ import annotations

import itertools
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from shopfront_server.canonical import canonical_bytes
from shopfront_server.profile.factref import FactRef, FactRefError, explain_unresolvable, locate
from shopfront_server.profile.model import TRANSACTIONAL_TYPES, has_contact_field, is_extension_module
from shopfront_server.profile.snapshots import SnapshotProvider, normalize_for_quote, snapshot_texts

InvariantId = Literal["schema", "L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9", "L10", "L11"]
Severity = Literal["error", "warning"]

READ_BACK_BUILTINS: frozenset[str] = frozenset(
    {"item", "choices", "when", "price", "party_size", "visit_window", "order_total_cents"}
)
APPROVAL_RULE_BUILTINS: frozenset[str] = READ_BACK_BUILTINS | {"lead_minutes"}
"""The schema lists built-ins twice (approval_rule.when and x-invariants L9); accept the union."""


class Violation(BaseModel):
    """One broken rule. `where` is a fact ref or JSON pointer locating the problem."""

    invariant: InvariantId
    severity: Severity
    where: str
    message: str
    skipped: bool = False
    """True when the check could not run (e.g. L3 without a snapshot)."""


class LoadReport(BaseModel):
    """Everything the loader found for one profile document."""

    profile_id: str | None = None
    revision: int | None = None
    sha256: str | None = None
    violations: list[Violation] = Field(default_factory=list)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self, limit: int = 5) -> str:
        """One line per error (at most `limit`), for logs and exception messages."""
        lines = [f"[{v.invariant}] {v.where or '/'}: {v.message}" for v in self.errors[:limit]]
        if len(self.errors) > limit:
            lines.append(f"... and {len(self.errors) - limit} more error(s)")
        return "; ".join(lines) if lines else "no errors"


@dataclass(frozen=True)
class InvariantContext:
    raw: Mapping[str, Any]
    sha256: str
    snapshots: SnapshotProvider | None
    strict: bool


InvariantCheck = Callable[[InvariantContext], list[Violation]]


# --------------------------------------------------------------------------- traversal helpers

Path_ = tuple[str | int, ...]


def pointer(path: Path_) -> str:
    """RFC 6901 JSON pointer for a path inside the document."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


def _error(invariant: InvariantId, where: str, message: str) -> Violation:
    return Violation(invariant=invariant, severity="error", where=where, message=message)


def _is_ext_key(key: str | int) -> bool:
    return isinstance(key, str) and key.startswith("x-")


def _walk(node: Any, path: Path_ = (), skip: Callable[[Path_], bool] | None = None) -> Iterator[tuple[Path_, Any]]:
    """Yield `(path, value)` for every node, depth first, skipping `x-*` keys and `skip(path)` subtrees."""
    if skip is not None and skip(path):
        return
    yield path, node
    if isinstance(node, Mapping):
        for key, value in node.items():
            if not _is_ext_key(key):
                yield from _walk(value, (*path, key), skip)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, (*path, index), skip)


def _dicts(value: Any) -> list[tuple[int, Mapping[str, Any]]]:
    """`(index, object)` for the object elements of a list."""
    if not isinstance(value, list):
        return []
    return [(i, v) for i, v in enumerate(value) if isinstance(v, Mapping)]


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, Mapping) else None


def _modules(raw: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    modules = raw.get("modules")
    if not isinstance(modules, Mapping):
        return []
    return [(mid, m) for mid, m in modules.items() if isinstance(m, Mapping)]


def _core_modules(raw: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """Modules other than `x-*` extensions (whose content is opaque to the loader)."""
    return [(mid, m) for mid, m in _modules(raw) if not is_extension_module(mid, m)]


def _skip_extension_modules(path: Path_, raw: Mapping[str, Any]) -> bool:
    if len(path) != 2 or path[0] != "modules":
        return False
    module = _get(raw.get("modules"), str(path[1]))
    return isinstance(module, Mapping) and is_extension_module(str(path[1]), module)


def _locations(raw: Mapping[str, Any]) -> list[tuple[int, Mapping[str, Any]]]:
    return _dicts(_get(raw.get("business"), "locations"))


def _location_ids(raw: Mapping[str, Any]) -> set[str]:
    return {str(loc.get("id")) for _, loc in _locations(raw)}


def _catalog_index(raw: Mapping[str, Any]) -> tuple[set[str], set[str], dict[str, Mapping[str, Any]]]:
    """(item refs, category refs, item ref -> item) over catalog modules."""
    items: dict[str, Mapping[str, Any]] = {}
    categories: set[str] = set()
    for module_id, module in _modules(raw):
        if module.get("type") != "catalog":
            continue
        for _, item in _dicts(module.get("items")):
            items[f"{module_id}/{item.get('id')}"] = item
        for _, category in _dicts(module.get("categories")):
            categories.add(f"{module_id}/{category.get('id')}")
    return set(items), categories, items


def _ids(value: Any) -> list[tuple[int, str]]:
    """`(index, id)` for the list elements that are objects with a string `id`."""
    return [(i, v["id"]) for i, v in _dicts(value) if isinstance(v.get("id"), str)]


def _entity_ids(module: Mapping[str, Any]) -> list[tuple[Path_, str]]:
    """Every addressable entity id of a module with its relative path (fact-ref lookup lists)."""
    out: list[tuple[Path_, str]] = []
    for key in ("items", "entries", "resources", "visit_windows", "categories"):
        out.extend(((key, i, "id"), entity_id) for i, entity_id in _ids(module.get(key)))
    sessions = _get(module.get("slotting"), "sessions")
    out.extend((("slotting", "sessions", i, "id"), entity_id) for i, entity_id in _ids(sessions))
    return out


def _intake_keys(module: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(f.get("key")): f for _, f in _dicts(module.get("intake_fields"))}


def _questions(raw: Mapping[str, Any]) -> list[tuple[int, Mapping[str, Any]]]:
    return _dicts(raw.get("open_questions"))


def _is_disputing_open(question: Mapping[str, Any]) -> bool:
    return question.get("status") == "open" and question.get("kind") in ("contradiction", "confirm")


# --------------------------------------------------------------------------- L1 refs


def _fact_ref_problem(raw: Mapping[str, Any], text: Any) -> str | None:
    try:
        ref = FactRef.parse(text)
    except FactRefError as exc:
        return str(exc)
    return explain_unresolvable(raw, ref)


def _entity_ref_problem(raw: Mapping[str, Any], ref: Any) -> str | None:
    """A `$defs/ref` (`<module>` or `<module>/<entity>`) resolves to a module or any of its entities."""
    if not isinstance(ref, str):
        return "not a string"
    module_id, _, entity_id = ref.partition("/")
    modules = dict(_modules(raw))
    module = modules.get(module_id)
    if module is None:
        return f"unknown module {module_id!r}"
    if entity_id and entity_id not in {eid for _, eid in _entity_ids(module)}:
        return f"unknown entity {entity_id!r} in module {module_id!r}"
    return None


def check_l1_refs(ctx: InvariantContext) -> list[Violation]:
    raw = ctx.raw
    out: list[Violation] = []
    item_refs, category_refs, _ = _catalog_index(raw)
    location_ids = _location_ids(raw)
    modules = dict(_modules(raw))

    def bad(path: Path_, message: str) -> None:
        out.append(_error("L1", pointer(path), message))

    for module_id, module in _core_modules(raw):
        base: Path_ = ("modules", module_id)
        kind = module.get("type")
        # catalog: category_ids name categories of the same catalog
        if kind == "catalog":
            local_categories = {str(c.get("id")) for _, c in _dicts(module.get("categories"))}
            for i, item in _dicts(module.get("items")):
                for j, category_id in enumerate(item.get("category_ids") or ()):
                    if category_id not in local_categories:
                        bad(
                            (*base, "items", i, "category_ids", j), f"unknown category {category_id!r} in {module_id!r}"
                        )
        # knowledge: applies_to refs
        for i, entry in _dicts(module.get("entries")):
            for j, ref in enumerate(entry.get("applies_to") or ()):
                if problem := _entity_ref_problem(raw, ref):
                    bad((*base, "entries", i, "applies_to", j), f"applies_to {ref!r}: {problem}")
        # booking: services, resources, sessions
        resource_ids = {str(r.get("id")) for _, r in _dicts(module.get("resources"))}
        for i, service in _dicts(module.get("services")):
            if service.get("item") not in item_refs:
                bad((*base, "services", i, "item"), f"service item {service.get('item')!r} is not a catalog item")
            for j, resource_id in enumerate(service.get("resource_ids") or ()):
                if resource_id not in resource_ids:
                    bad((*base, "services", i, "resource_ids", j), f"unknown resource {resource_id!r}")
        for i, session in _dicts(_get(module.get("slotting"), "sessions")):
            spath: Path_ = (*base, "slotting", "sessions", i)
            if session.get("item") not in item_refs:
                bad((*spath, "item"), f"session item {session.get('item')!r} is not a catalog item")
            if "resource_id" in session and session["resource_id"] not in resource_ids:
                bad((*spath, "resource_id"), f"unknown resource {session['resource_id']!r}")
            if "location_id" in session and session["location_id"] not in location_ids:
                bad((*spath, "location_id"), f"unknown location {session['location_id']!r}")
        # custom_order / quote_request: items name catalog items or categories
        if kind in ("custom_order", "quote_request"):
            for i, ref in enumerate(module.get("items") or ()):
                if ref not in item_refs and ref not in category_refs:
                    bad((*base, "items", i), f"{ref!r} is neither a catalog item nor a catalog category")
        # fulfillment locations
        for i, location_id in enumerate(_get(module.get("fulfillment"), "location_ids") or ()):
            if location_id not in location_ids:
                bad((*base, "fulfillment", "location_ids", i), f"unknown location {location_id!r}")
        # approval.module -> owner_approval
        approval = module.get("approval")
        if isinstance(approval, Mapping) and "module" in approval:
            target = modules.get(str(approval["module"]))
            if target is None:
                bad((*base, "approval", "module"), f"unknown module {approval['module']!r}")
            elif target.get("type") != "owner_approval":
                bad((*base, "approval", "module"), f"{approval['module']!r} is not an owner_approval module")
        # say_before_confirm -> knowledge entries
        for i, ref in enumerate(_get(module.get("confirmation"), "say_before_confirm") or ()):
            entry_module_id, _, entry_id = str(ref).partition("/")
            entry_module = modules.get(entry_module_id)
            entries = {str(e.get("id")) for _, e in _dicts(_get(entry_module, "entries"))}
            if _get(entry_module, "type") != "knowledge" or entry_id not in entries:
                bad((*base, "confirmation", "say_before_confirm", i), f"{ref!r} is not a knowledge entry")

    # location areas measured from another location
    for i, location in _locations(raw):
        origin = _get(_get(location.get("area"), "radius"), "from_location")
        if origin is not None and origin not in location_ids:
            bad(("business", "locations", i, "area", "radius", "from_location"), f"unknown location {origin!r}")

    # question targets (open contradiction/confirm targets belong to L6)
    question_ids = {str(q.get("id")) for _, q in _questions(raw)}
    for i, question in _questions(raw):
        if _is_disputing_open(question):
            continue
        for j, target in enumerate(question.get("targets") or ()):
            if problem := _fact_ref_problem(raw, target):
                bad(("open_questions", i, "targets", j), f"target {target!r}: {problem}")

    # owner answers cite a question
    for source_id, source in (raw.get("sources") or {}).items():
        if isinstance(source, Mapping) and "question_id" in source and source["question_id"] not in question_ids:
            bad(("sources", source_id, "question_id"), f"unknown question {source['question_id']!r}")

    # run failures about the current revision
    certification = raw.get("certification")
    for i, run in _dicts(_get(certification, "runs")):
        if run.get("profile_revision") != raw.get("revision"):
            continue
        for j, failure in _dicts(run.get("failures")):
            if "fact_ref" in failure and (problem := _fact_ref_problem(raw, failure["fact_ref"])):
                bad(("certification", "runs", i, "failures", j, "fact_ref"), f"{failure['fact_ref']!r}: {problem}")
    return out


# --------------------------------------------------------------------------- L2 ids


def _duplicates(values: list[tuple[Path_, str]]) -> list[tuple[Path_, str]]:
    counts = Counter(v for _, v in values)
    seen: set[str] = set()
    out: list[tuple[Path_, str]] = []
    for path, value in values:
        if counts[value] > 1:
            if value in seen:
                out.append((path, value))
            seen.add(value)
    return out


def check_l2_ids(ctx: InvariantContext) -> list[Violation]:
    raw = ctx.raw
    groups: list[tuple[str, list[tuple[Path_, str]]]] = []
    groups.append(
        ("location id", [(("business", "locations", i, "id"), str(loc.get("id"))) for i, loc in _locations(raw)])
    )
    for module_id, module in _core_modules(raw):
        base: Path_ = ("modules", module_id)
        groups.append((f"entity id in module {module_id!r}", [((*base, *p), v) for p, v in _entity_ids(module)]))
        for i, item in _dicts(module.get("items")):
            ipath: Path_ = (*base, "items", i)
            groups.append(
                (
                    f"option group id in {module_id}/{item.get('id')}",
                    [((*ipath, "options", g, "id"), str(group.get("id"))) for g, group in _dicts(item.get("options"))],
                )
            )
            for g, group in _dicts(item.get("options")):
                groups.append(
                    (
                        f"choice id in {module_id}/{item.get('id')}/{group.get('id')}",
                        [
                            ((*ipath, "options", g, "choices", c, "id"), str(ch.get("id")))
                            for c, ch in _dicts(group.get("choices"))
                        ],
                    )
                )
        groups.append(
            (
                f"intake field key in module {module_id!r}",
                [
                    ((*base, "intake_fields", i, "key"), str(f.get("key")))
                    for i, f in _dicts(module.get("intake_fields"))
                ],
            )
        )
        response_fields = _get(module.get("approval"), "response_fields")
        groups.append(
            (
                f"response field key in module {module_id!r}",
                [
                    ((*base, "approval", "response_fields", i, "key"), str(f.get("key")))
                    for i, f in _dicts(response_fields)
                ],
            )
        )
    groups.append(("question id", [(("open_questions", i, "id"), str(q.get("id"))) for i, q in _questions(raw)]))
    groups.append(
        (
            "run id",
            [
                (("certification", "runs", i, "id"), str(r.get("id")))
                for i, r in _dicts(_get(raw.get("certification"), "runs"))
            ],
        )
    )
    sources = raw.get("sources") if isinstance(raw.get("sources"), Mapping) else {}
    groups.append(
        (
            "source url",
            [(("sources", sid, "url"), str(s.get("url"))) for sid, s in sources.items() if isinstance(s, Mapping)],
        )
    )

    out: list[Violation] = []
    for label, values in groups:
        for path, value in _duplicates(values):
            out.append(_error("L2", pointer(path), f"duplicate {label}: {value!r}"))
    return out


# --------------------------------------------------------------------------- L3 evidence


def _skip_free_form(path: Path_) -> bool:
    """Question candidate values and answers are free-form (`value: true`): never walk into them."""
    if len(path) < 3 or path[0] != "open_questions":
        return False
    return path[2] == "answer" or (len(path) >= 5 and path[2] == "candidates" and path[4] == "value")


def iter_evidence(raw: Mapping[str, Any]) -> Iterator[tuple[Path_, Mapping[str, Any]]]:
    """Every `evidence` object (provenance evidence and question candidate evidence)."""
    for path, node in _walk(raw, skip=_skip_free_form):
        if not path or not isinstance(node, Mapping):
            continue
        parent = path[-1] if isinstance(path[-1], str) else (path[-2] if len(path) > 1 else None)
        if parent == "evidence" and "url" in node and "quote" in node:
            yield path, node


def check_l3_evidence(ctx: InvariantContext) -> list[Violation]:
    raw = ctx.raw
    sources = raw.get("sources") if isinstance(raw.get("sources"), Mapping) else {}
    by_url = {str(s.get("url")): (sid, s) for sid, s in sources.items() if isinstance(s, Mapping)}
    texts: dict[str, tuple[str, ...]] = {}
    skipped: dict[str, int] = {}
    out: list[Violation] = []
    for path, evidence in iter_evidence(raw):
        url, quote = str(evidence.get("url")), str(evidence.get("quote"))
        found = by_url.get(url)
        if found is None:
            out.append(_error("L3", pointer((*path, "url")), f"evidence url {url!r} matches no source"))
            continue
        source_id, source = found
        if url.startswith("urn:shopfront:owner-"):
            continue  # owner words: the source registry entry is the record, there is no page snapshot
        sha = source.get("content_sha256")
        if source_id not in texts:
            found_texts = (
                snapshot_texts(ctx.snapshots, sha) if ctx.snapshots is not None and isinstance(sha, str) else ()
            )
            texts[source_id] = tuple(normalize_for_quote(t) for t in found_texts)
        renderings = texts[source_id]
        if not renderings:
            skipped[source_id] = skipped.get(source_id, 0) + 1
            continue
        needle = normalize_for_quote(quote)
        if not any(needle in text for text in renderings):
            out.append(_error("L3", pointer((*path, "quote")), f"quote not found in snapshot of {url}: {quote[:80]!r}"))
    for source_id, count in skipped.items():
        reason = "no snapshot provider" if ctx.snapshots is None else "snapshot unavailable"
        out.append(
            Violation(
                invariant="L3",
                severity="warning",
                where=pointer(("sources", source_id)),
                message=f"{count} quote(s) unchecked: {reason} for {sources[source_id].get('url')}",
                skipped=True,
            )
        )
    return out


# --------------------------------------------------------------------------- L4 units

PRICE_KEYS: frozenset[str] = frozenset({"price", "price_delta", "fee"})


def check_l4_units(ctx: InvariantContext) -> list[Violation]:
    raw = ctx.raw
    business = raw.get("business") if isinstance(raw.get("business"), Mapping) else {}
    currency = business.get("currency")
    out: list[Violation] = []
    def skip(path: Path_) -> bool:
        return _skip_free_form(path) or _skip_extension_modules(path, raw)

    for path, node in _walk(raw, skip=skip):
        if path and path[-1] in PRICE_KEYS and isinstance(node, Mapping) and "currency" in node:
            if node["currency"] != currency:
                out.append(
                    _error(
                        "L4",
                        pointer((*path, "currency")),
                        f"{node['currency']} differs from business currency {currency}",
                    )
                )
    timezone = business.get("timezone")
    try:
        ZoneInfo(str(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        out.append(_error("L4", "/business/timezone", f"timezone {timezone!r} is not in tzdata"))
    return out


# --------------------------------------------------------------------------- L5 provenance


def check_l5_provenance(ctx: InvariantContext) -> list[Violation]:
    raw = ctx.raw
    probes: list[FactRef] = [FactRef("business", (), "name")]
    probes += [FactRef("locations", (str(loc.get("id")),), "id") for _, loc in _locations(raw)]
    for module_id, module in _modules(raw):
        kind = module.get("type")
        if not isinstance(kind, str) or kind.startswith("x-") or module_id.startswith("x-"):
            continue  # extension modules are never statable: nothing to cover
        if kind == "owner_approval":
            probes.append(FactRef(module_id, (), "type"))
        for _, item in _dicts(module.get("items")):
            probes.append(FactRef(module_id, (str(item.get("id")),), "id"))
            for _, group in _dicts(item.get("options")):
                for _, choice in _dicts(group.get("choices")):
                    probes.append(
                        FactRef(module_id, (str(item.get("id")), str(group.get("id")), str(choice.get("id"))), "id")
                    )
        for key in ("entries", "resources", "visit_windows"):
            probes += [FactRef(module_id, (str(e.get("id")),), "id") for _, e in _dicts(module.get(key))]
        probes += [
            FactRef(module_id, (str(s.get("id")),), "id") for _, s in _dicts(_get(module.get("slotting"), "sessions"))
        ]

    out: list[Violation] = []
    for probe in probes:
        located = locate(raw, probe, include_disabled=True)
        if isinstance(located, str):
            continue  # L1/L2 report unresolvable or shadowed ids
        if not any(isinstance(obj.get("provenance"), Mapping) for obj in located.chain):
            out.append(_error("L5", probe.entity, f"{located.kind} {probe.entity!r} has no enclosing provenance"))
    return out


# --------------------------------------------------------------------------- L6 disputed targets


def check_l6_statable(ctx: InvariantContext) -> list[Violation]:
    out: list[Violation] = []
    for i, question in _questions(ctx.raw):
        if not _is_disputing_open(question):
            continue
        for j, target in enumerate(question.get("targets") or ()):
            if problem := _fact_ref_problem(ctx.raw, target):
                out.append(
                    _error(
                        "L6",
                        pointer(("open_questions", i, "targets", j)),
                        f"open {question.get('kind')} {question.get('id')!r} cannot withhold {target!r}: {problem}",
                    )
                )
    return out


# --------------------------------------------------------------------------- L7 certification


def check_l7_certification(ctx: InvariantContext) -> list[Violation]:
    raw = ctx.raw
    certification = raw.get("certification") if isinstance(raw.get("certification"), Mapping) else {}
    if certification.get("status") not in ("certified", "live"):
        return []
    runs = _dicts(certification.get("runs"))
    certified_run = certification.get("certified_run")
    if not runs:
        return [_error("L7", "/certification/runs", "certified or live without any run")]
    index, last = runs[-1]
    where = pointer(("certification", "runs", index))
    out: list[Violation] = []
    if last.get("id") != certified_run:
        out.append(
            _error(
                "L7",
                "/certification/certified_run",
                f"certified_run {certified_run!r} is not the last run {last.get('id')!r}",
            )
        )
        return out
    if last.get("result") != "passed":
        out.append(_error("L7", where, f"certified run {certified_run!r} did not pass"))
    total, passed = last.get("cases_total"), last.get("cases_passed")
    if not isinstance(total, int) or total < 1:
        out.append(_error("L7", where, f"certified run {certified_run!r} ran no cases"))
    elif passed != total:
        out.append(_error("L7", where, f"certified run {certified_run!r} passed {passed} of {total} cases"))
    if last.get("profile_sha256") != ctx.sha256:
        out.append(
            _error(
                "L7",
                f"{where}/profile_sha256",
                "certified run is bound to a different profile sha256 (profile edited since)",
            )
        )
    if last.get("profile_revision") != raw.get("revision"):
        out.append(
            _error(
                "L7",
                f"{where}/profile_revision",
                f"certified run is bound to revision {last.get('profile_revision')}, profile is {raw.get('revision')}",
            )
        )
    return out


# --------------------------------------------------------------------------- L8 canaries


def _skip_not_output(path: Path_) -> bool:
    return bool(path) and path[0] in ("sources", "certification", "$schema")


def _canary_form(text: str) -> str:
    return normalize_for_quote(text).casefold()


def check_l8_canaries(ctx: InvariantContext) -> list[Violation]:
    raw = ctx.raw
    sources = raw.get("sources") if isinstance(raw.get("sources"), Mapping) else {}
    canaries = [
        _canary_form(str(c.get("quote")))
        for s in sources.values()
        if isinstance(s, Mapping)
        for _, c in _dicts(s.get("injection_canaries"))
        if c.get("quote")
    ]
    canaries = [c for c in canaries if c]
    if not canaries:
        return []
    out: list[Violation] = []
    for path, node in _walk(raw, skip=_skip_not_output):
        if isinstance(node, str):
            text = _canary_form(node)
            if any(canary in text for canary in canaries):
                out.append(_error("L8", pointer(path), "contains an injection canary quote"))
    return out


# --------------------------------------------------------------------------- L9 vocabulary


def _value_matches(definition: Mapping[str, Any], value: Any) -> bool:
    kind = definition.get("type")
    enum = definition.get("enum") or ()
    if kind == "text":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "enum":
        return isinstance(value, str) and value in enum
    if kind == "multi_enum":
        return isinstance(value, list) and all(isinstance(v, str) and v in enum for v in value)
    return False


def _attribute_containers(raw: Mapping[str, Any]) -> Iterator[tuple[Path_, Mapping[str, Any]]]:
    """`(path, attributes)` for item, choice and resource `attributes` and knowledge entry `facts`."""
    for module_id, module in _core_modules(raw):
        base: Path_ = ("modules", module_id)
        for i, item in _dicts(module.get("items")):
            if isinstance(item.get("attributes"), Mapping):
                yield (*base, "items", i, "attributes"), item["attributes"]
            for g, group in _dicts(item.get("options")):
                for c, choice in _dicts(group.get("choices")):
                    if isinstance(choice.get("attributes"), Mapping):
                        yield (*base, "items", i, "options", g, "choices", c, "attributes"), choice["attributes"]
        for i, resource in _dicts(module.get("resources")):
            if isinstance(resource.get("attributes"), Mapping):
                yield (*base, "resources", i, "attributes"), resource["attributes"]
        for i, entry in _dicts(module.get("entries")):
            if isinstance(entry.get("facts"), Mapping):
                yield (*base, "entries", i, "facts"), entry["facts"]


ORDERING_OPS: frozenset[str] = frozenset({">", ">=", "<", "<="})
NUMERIC_FIELD_TYPES: frozenset[str] = frozenset({"integer", "number", "money"})


def _choice_ref_resolves(raw: Mapping[str, Any], ref: Any) -> bool:
    """`<catalog>/<item>/<group>/<choice>` names an existing option choice."""
    if not isinstance(ref, str) or ref.count("/") != 3:
        return False
    module_id, item_id, group_id, choice_id = ref.split("/")
    module = dict(_modules(raw)).get(module_id)
    for _, item in _dicts(_get(module, "items")):
        if item.get("id") == item_id:
            for _, group in _dicts(item.get("options")):
                if group.get("id") == group_id:
                    return any(c.get("id") == choice_id for _, c in _dicts(group.get("choices")))
    return False


def _rule_problems(
    raw: Mapping[str, Any], rule: Mapping[str, Any], intake: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    when, op, value = rule.get("when"), rule.get("op"), rule.get("value")
    if when not in intake and when not in APPROVAL_RULE_BUILTINS:
        return [f"approval rule key {when!r} is neither an intake field nor a built-in"]
    values = value if isinstance(value, list) else [value]
    problems: list[str] = []
    if when == "choices" and op in ("in", "=="):
        problems += [f"choice {v!r} does not resolve" for v in values if not _choice_ref_resolves(raw, v)]
    elif when == "item" and op in ("in", "=="):
        item_refs, _, _ = _catalog_index(raw)
        problems += [f"item {v!r} is not a catalog item" for v in values if v not in item_refs]
    elif when in intake:
        field = intake[str(when)]
        field_type = field.get("type")
        if (
            op in ORDERING_OPS
            and field_type in NUMERIC_FIELD_TYPES
            and not (isinstance(value, (int, float)) and not isinstance(value, bool))
        ):
            problems.append(f"rule on numeric field {when!r} compares with a non-number {value!r}")
        if field_type in ("choice", "multi_choice") and op in ("in", "=="):
            options = {str(o.get("value")) for _, o in _dicts(field.get("options"))}
            problems += [f"{v!r} is not an option of {when!r}" for v in values if v not in options]
    return problems


def check_l9_vocabulary(ctx: InvariantContext) -> list[Violation]:
    raw = ctx.raw
    vocabulary = _get(raw.get("vocabulary"), "attributes")
    declared: Mapping[str, Any] = vocabulary if isinstance(vocabulary, Mapping) else {}
    out: list[Violation] = []
    for path, attributes in _attribute_containers(raw):
        for key, value in attributes.items():
            definition = declared.get(key)
            if not isinstance(definition, Mapping):
                out.append(
                    _error("L9", pointer((*path, key)), f"attribute {key!r} is not declared in vocabulary.attributes")
                )
            elif not _value_matches(definition, value):
                out.append(
                    _error("L9", pointer((*path, key)), f"{value!r} does not match {key!r} ({definition.get('type')})")
                )
    for module_id, module in _core_modules(raw):
        base: Path_ = ("modules", module_id)
        intake = _intake_keys(module)
        for i, key in enumerate(_get(module.get("confirmation"), "read_back") or ()):
            if key not in intake and key not in READ_BACK_BUILTINS:
                out.append(
                    _error(
                        "L9",
                        pointer((*base, "confirmation", "read_back", i)),
                        f"read_back {key!r} is neither an intake field nor a built-in",
                    )
                )
        for i, rule in _dicts(_get(module.get("approval"), "rules")):
            for problem in _rule_problems(raw, rule, intake):
                out.append(_error("L9", pointer((*base, "approval", "rules", i)), problem))
    return out


# --------------------------------------------------------------------------- L10 contradictions


def check_l10_contradictions(ctx: InvariantContext) -> list[Violation]:
    out: list[Violation] = []
    for i, question in _questions(ctx.raw):
        if question.get("kind") != "contradiction":
            continue
        candidates = _dicts(question.get("candidates"))
        for (a, first), (b, second) in itertools.combinations(candidates, 2):
            where = pointer(("open_questions", i, "candidates", b))
            if canonical_bytes(first.get("value")) == canonical_bytes(second.get("value")):
                out.append(
                    _error("L10", where, f"candidates {a} and {b} of {question.get('id')!r} have the same value")
                )
            if _get(first.get("evidence"), "url") == _get(second.get("evidence"), "url"):
                out.append(
                    _error("L10", where, f"candidates {a} and {b} of {question.get('id')!r} cite the same source")
                )
    return out


# --------------------------------------------------------------------------- L11 contact


def check_l11_contact(ctx: InvariantContext) -> list[Violation]:
    """Every booking, custom_order and quote_request module collects a phone or email intake field.

    Without auth every caller is the same 'anonymous' principal, so the request details are all that
    tell two customers apart in the idempotency key: a module with no contact field would merge two
    strangers' identical requests into one (and the owner could not reach either). The runtime half
    (`domain.intake.missing_contact`) requires a value for one of the fields on every request."""
    out: list[Violation] = []
    for module_id, module in _core_modules(ctx.raw):
        if module.get("type") not in TRANSACTIONAL_TYPES:
            continue
        if not has_contact_field([field for _, field in _dicts(module.get("intake_fields"))]):
            out.append(
                _error(
                    "L11",
                    pointer(("modules", module_id, "intake_fields")),
                    f"{module.get('type')} module {module_id!r} has no phone or email intake field",
                )
            )
    return out


CHECKS: dict[InvariantId, InvariantCheck] = {
    "L1": check_l1_refs,
    "L2": check_l2_ids,
    "L3": check_l3_evidence,
    "L4": check_l4_units,
    "L5": check_l5_provenance,
    "L6": check_l6_statable,
    "L7": check_l7_certification,
    "L8": check_l8_canaries,
    "L9": check_l9_vocabulary,
    "L10": check_l10_contradictions,
    "L11": check_l11_contact,
}

LENIENT_DOWNGRADES: frozenset[InvariantId] = frozenset({"L3", "L7", "L11"})
"""Invariants reported as warnings (not errors) when `strict` is False."""


def run_all(ctx: InvariantContext) -> list[Violation]:
    """Run every check in order, applying the lenient downgrade policy.

    A check that crashes is reported as an error of that invariant (one bad document never takes the
    loader down, and a crash is never a pass).
    """
    out: list[Violation] = []
    for invariant, check in CHECKS.items():
        try:
            violations = check(ctx)
        except Exception as exc:  # noqa: BLE001 - reported as a violation, never swallowed
            violations = [_error(invariant, "", f"check failed: {type(exc).__name__}: {exc}")]
        for violation in violations:
            if not ctx.strict and violation.invariant in LENIENT_DOWNGRADES and violation.severity == "error":
                violation = violation.model_copy(update={"severity": "warning"})
            out.append(violation)
    return out
