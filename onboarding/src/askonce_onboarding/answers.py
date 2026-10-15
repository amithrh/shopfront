"""Apply the owner's answers to onboarding questions: the next profile revision, ready to re-certify.

Each answer is recorded on its question (`status: answered`), gets an `owner_answer` source
(`urn:askonce:owner-answer:<question id>`), and is written into the facts the question targets, with
the owner's own words as evidence and `owner_confirmed: true`:

  contradiction  hours (one weekday), a policy fact, a service-area zone or an item price take the
                 chosen value; a policy's spoken answer becomes the owner's words
  missing        owner reply time, custom-order capacity / notice, or a new knowledge entry
  confirm        allergen/dietary facts and safety statements become owner-confirmed (only on "yes")

Any edit bumps the revision and resets certification to draft (invariant L7); the runs stay as history.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from askonce_onboarding.assemble import DAY_NAMES, DAYS, as_int, slug, snake, spoken

OWNER_URN = "urn:askonce:owner-answer:"
JsonObj = dict[str, Any]


class AnswerError(ValueError):
    pass


def apply_answers(doc: JsonObj, answers: dict[str, JsonObj], *, answered_at: str) -> JsonObj:
    """`answers` maps question id -> {"value": ..., "raw_text": "the owner's words"}."""
    out = copy.deepcopy(doc)
    by_id = {q["id"]: q for q in out.get("open_questions", [])}
    for qid, answer in answers.items():
        q = by_id.get(qid)
        if q is None:
            raise AnswerError(f"no question {qid!r}")
        if q["status"] != "open":
            continue
        raw = str(answer.get("raw_text") or answer.get("value"))
        value = answer.get("value")
        evidence = {"url": f"{OWNER_URN}{qid}", "quote": raw[:500]}
        Applier(out, q, value, raw, evidence).apply()
        q["status"] = "answered"
        q["answer"] = {"value": value, "raw_text": raw[:1000], "answered_at": answered_at}
        out["sources"][f"owner-{qid}"[:64]] = {"kind": "owner_answer", "url": evidence["url"], "question_id": qid,
                                               "retrieved_at": answered_at}
    out["revision"] = int(doc["revision"]) + 1
    out["updated_at"] = answered_at
    runs = list((doc.get("certification") or {}).get("runs", []))
    out["certification"] = {"status": "draft", "runs": runs}
    return out


def confirm(prov: JsonObj | None, evidence: JsonObj) -> JsonObj:
    ev = [e for e in (prov or {}).get("evidence", []) if e != evidence][:9]
    return {"owner_confirmed": True, "evidence": [*ev, evidence]}


class Applier:
    def __init__(self, doc: JsonObj, q: JsonObj, value: Any, raw: str, evidence: JsonObj) -> None:
        self.doc, self.q, self.value, self.raw, self.ev = doc, q, value, raw, evidence

    def apply(self) -> None:
        kind = self.q["kind"]
        if kind == "confirm":
            if self.value is True:
                self.confirm()
            return
        for target in self.q["targets"]:
            if self.apply_target(target):
                return
        raise AnswerError(f"don't know how to apply an answer to {self.q['targets']}")

    # ---------------------------------------------------------------- targets

    def apply_target(self, target: str) -> bool:
        entity, _, path = target.partition("#")
        parts = entity.split("/")
        if parts[0] == "locations" and path == "hours.weekly":
            return self.hours(parts[1])
        if parts[0] == "locations" and path.startswith("area."):
            return self.zone()
        if entity == "info" and path == "entries":
            return self.new_entry()
        module = self.doc["modules"].get(parts[0])
        if module is None:
            return False
        if module["type"] == "owner_approval" and path == "expected_response_minutes":
            minutes = as_int(self.value, 1)
            if minutes is None:
                raise AnswerError("reply time must be a whole number of minutes")
            module["expected_response_minutes"] = minutes
            module["expires_after_minutes"] = max(module.get("expires_after_minutes", 0), minutes * 2)
            module["provenance"] = confirm(module.get("provenance"), self.ev)
            return True
        if module["type"] in ("custom_order", "booking", "quote_request") and path.startswith("availability."):
            field = path.split(".", 1)[1]
            number = as_int(self.value, 0)
            if number is None:
                raise AnswerError(f"{field} needs a whole number")
            if field == "lead_time_minutes" and self.q.get("answer_field", {}).get("key", "").endswith("_hours"):
                number *= 60
            availability = module.setdefault("availability", {})
            availability[field] = number if field != "capacity_per_day" else max(number, 1)
            availability["provenance"] = confirm(availability.get("provenance"), self.ev)
            return True
        if module["type"] == "knowledge" and len(parts) == 2:
            entry = next((e for e in module["entries"] if e["id"] == parts[1]), None)
            if entry is None:
                return False
            if path.startswith("facts."):
                entry.setdefault("facts", {})[path.split(".", 1)[1]] = self.value
            said = spoken(self.raw)
            if said:
                entry["answer_spoken"] = said
            entry["provenance"] = confirm(entry.get("provenance"), self.ev)
            return True
        if module["type"] == "catalog" and len(parts) == 2 and path == "price":
            item = next((i for i in module["items"] if i["id"] == parts[1]), None)
            cents = _cents(self.value)
            if item is None or cents is None:
                return False
            item["price"] = {"kind": "fixed", "amount_cents": cents, "currency": self.doc["business"]["currency"]}
            item["provenance"] = confirm(item.get("provenance"), self.ev)
            return True
        return False

    def hours(self, loc_id: str) -> bool:
        loc = next((l for l in self.doc["business"]["locations"] if l["id"] == loc_id), None)
        if loc is None or "hours" not in loc:
            return False
        day = next((d for d, name in DAY_NAMES.items() if f"q-{name.lower()}-" in self.q["id"]), None)
        if day is None:
            raise AnswerError("cannot tell which day an hours answer is for")
        week: dict[str, tuple[str, str]] = {}
        for line in loc["hours"]["weekly"]:
            for d in line["days"]:
                week[d] = (line["open"], line["close"])
        value = str(self.value)
        if re.fullmatch(r"\d\d:\d\d-\d\d:\d\d", value):
            week[day] = (value[:5], value[6:])
        elif re.fullmatch(r"\d\d:\d\d", value) and day in week:
            week[day] = (week[day][0], value)
        else:
            raise AnswerError(f"unexpected hours value {value!r}")
        groups: dict[tuple[str, str], list[str]] = {}
        for d in DAYS:
            if d in week:
                groups.setdefault(week[d], []).append(d)
        loc["hours"]["weekly"] = [{"days": days, "open": o, "close": c} for (o, c), days in groups.items()]
        loc["hours"]["provenance"] = confirm(loc["hours"].get("provenance"), self.ev)
        return True

    def zone(self) -> bool:
        place = self.q["id"].removeprefix("q-zone-")
        locations = self.doc["business"]["locations"]
        key = None
        for loc in locations:
            area = loc.get("area") or {}
            for k in ("postal_codes", "localities"):
                for p in list(area.get(k, [])):
                    if slug(p) == place:
                        key, original = k, p
                        area[k].remove(p)
                        if not area[k]:
                            del area[k]
        if key is None:
            return False
        target_id = {"core": "core-area", "extended": "extended-area"}.get(str(self.value))
        if target_id:
            loc = next((l for l in locations if l["id"] == target_id), None)
            if loc is not None:
                loc.setdefault("area", {}).setdefault(key, []).append(original)
                loc["provenance"] = confirm(loc.get("provenance"), self.ev)
        # A service area left with no codes or places cannot stand (schema: area minProperties 1).
        for loc in [l for l in locations if l["kind"] == "service_area" and not l.get("area")]:
            locations.remove(loc)
            for module in self.doc["modules"].values():
                ful = module.get("fulfillment")
                if ful and loc["id"] in ful.get("location_ids", []):
                    ful["location_ids"].remove(loc["id"])
        return True

    def new_entry(self) -> bool:
        module = next((m for m in self.doc["modules"].values() if m["type"] == "knowledge"), None)
        said = spoken(self.raw)
        if module is None or not said:
            return False
        topic = snake(self.q.get("answer_field", {}).get("key") or "owner_answer")
        entry_id = slug(topic)
        n = 2
        while any(e["id"] == entry_id for e in module["entries"]):
            entry_id, n = f"{slug(topic)}-{n}", n + 1
        module["entries"].append({"id": entry_id, "topic": topic, "question_variants": [self.q["prompt"][:120]],
                                  "answer_spoken": said, "provenance": {"owner_confirmed": True, "evidence": [self.ev]}})
        self.q["targets"] = [f"{_module_id(self.doc, module)}/{entry_id}#answer_spoken"]
        return True

    def confirm(self) -> None:
        if self.q["id"] == "q-allergens" or any(t.endswith(("#allergens", "#dietary")) for t in self.q["targets"]):
            for cand in self.q.get("candidates", []):
                for ref, value in (cand.get("value") or {}).items():
                    entity, _, field = ref.partition("#")
                    module_id, _, item_id = entity.partition("/")
                    item = next((i for i in self.doc["modules"][module_id]["items"] if i["id"] == item_id), None)
                    if item is None or field not in ("allergens", "dietary"):
                        continue
                    prov = {"owner_confirmed": True, "evidence": [cand["evidence"], self.ev]}
                    item[field] = {**value, "provenance": prov}
            return
        for target in self.q["targets"]:
            entity, _, _ = target.partition("#")
            module_id, _, entry_id = entity.partition("/")
            module = self.doc["modules"].get(module_id) or {}
            for entry in module.get("entries", []):
                if entry["id"] == entry_id:
                    entry["provenance"] = confirm(entry.get("provenance"), self.ev)


def _module_id(doc: JsonObj, module: JsonObj) -> str:
    return next(k for k, v in doc["modules"].items() if v is module)


def _cents(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    m = re.search(r"\$?(\d[\d,]*)(?:\.(\d{2}))?", str(value))
    return int(m.group(1).replace(",", "")) * 100 + int(m.group(2) or 0) if m else None
