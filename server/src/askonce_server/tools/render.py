"""Render a commitment as the final `CallToolResult` of its MCP task (`domain.ports.DecisionRenderer`).

Used when the owner decides (and on expiry), long after the original tools/call returned. The result is
a `CommitmentResult` (the output schema shared by every commitment tool), serialised exactly as the SDK
serialises tool results: `structured_content` = model dump, `content` = one TextContent with the JSON.

Speech per status (examples for Thimbleberry Bakehouse):
  approved        "Good news: Rosa confirmed your celebration cake for Saturday. The final price is $88."
  declined        "Sorry, Rosa can't take that order." (+ owner note if spoken-safe)
  counter_offer   "Rosa suggested a change: <note>. Want to place it that way?"
  suggested_time  "Rosa can't do that time, but suggests Sunday at 10 am."
  quoted          "Kettlewick sent a quote: $240 for the job, valid until Friday."
  answered        "Rosa says: <answer>."
  expired         "Rosa hasn't answered yet, so your request isn't confirmed."
                  ("Your request lapsed because Rosa's question wasn't answered in time." only while the
                  owner's request_info question is unanswered: `decision.answered_at` is None)
  pending_owner after the customer answered Rosa's question:
                  "I've passed your answer on to Rosa, who usually replies within 24 hours. ..."
A confirmed or approved booking also says its 8-digit booking code ("Your booking code is 0 4 9 7,
3 0 7 3."), always kept when the speech is shortened, and shows it on the card: it is what a voice
customer says back to `cancel_booking`. `with_code=False` leaves it out (an anonymous replay).
Owner free text is passed through `guard.sanitize_speak` and recorded as a fact with evidence
`urn:askonce:owner-answer:<commitment id>` so the certifier can attribute it.

The same module builds the results where nothing is stored (`not_confirmed`, `needs_info`,
`unavailable`, `not_offered`), so every commitment tool speaks with one voice.
"""

from __future__ import annotations

import re

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Any

import mcp_types as types
import pydantic_core

from askonce_server.domain.models import OPEN_STATUSES, Commitment, PriceQuote
from askonce_server.domain.pricing import price_for
from askonce_server.domain.availability import ONE_PERSON_EACH
from askonce_server.domain.planning import CommitPlan, PlanProblem, SlotOption
from askonce_server.ids import spoken_reference
from askonce_server.profile.model import JsonObj, Profile
from askonce_server.tools import grounding, speech
from askonce_server.tools.guard import guard_result, sanitize_speak
from askonce_server.tools.schemas import (
    Card,
    CardAction,
    CardLine,
    CommitmentResult,
    CommitmentView,
    Fact,
    PriceView,
    SlotView,
)

OWNER_ANSWER_URN = "urn:askonce:owner-answer:"
COMMITMENT_URN = "urn:askonce:commitment:"
"""Evidence of a value fixed in a stored commitment (the price the customer agreed to), cited when the
profile has since changed, so the number spoken is still backed by a fact."""
OWNER_TEXT_MAX = 140
"""Owner free text is capped so the status sentence around it still fits in `speak`."""

MAX_ALTERNATIVES = 3

BOOKING_CODE_LABEL = "Booking code"

STATUS_LABELS: dict[str, str] = {
    "confirmed": "Confirmed",
    "pending_owner": "Waiting for the owner",
    "approved": "Approved",
    "declined": "Declined",
    "counter_offer": "Change suggested",
    "suggested_time": "New time suggested",
    "quoted": "Quote received",
    "info_requested": "More info needed",
    "answered": "Answered",
    "cancelled": "Cancelled",
    "expired": "Expired",
    "not_confirmed": "Not sent",
    "needs_info": "Needs details",
    "unavailable": "Unavailable",
    "not_offered": "Not offered",
}

_NOT_OFFERED_CODES = frozenset({"unknown_item", "not_offered", "unknown_module"})
_NEEDS_INFO_CODES = frozenset(
    {"missing_choice", "invalid_choice", "invalid_intake", "unknown_visit_window", "party_size_out_of_range"}
)

_PROBLEM_SPEECH: dict[str, str] = {
    "closed": "{business} is closed then.",
    "outside_hours": "That's outside the hours for this.",
    "not_on_schedule": "That isn't one of the available start times.",
    "lead_time": "That's too soon. This needs more notice.",
    "too_far_ahead": "That's too far ahead to book.",
    "in_the_past": "That time has already passed.",
    "outside_service_area": "{business} doesn't serve that area.",
    "party_size_out_of_range": "That group size isn't available for this.",
    "unknown_visit_window": "That visit window isn't one of the options.",
    "too_many_attempts": "I can't send this exact request again. Please contact {business} directly.",
}

_NOT_CONFIRMED_SPEECH: dict[str, str] = {
    "book": "Okay, I haven't booked anything.",
    "cancel_booking": "Okay, your booking stays as it is.",
    "custom_order": "Okay, I haven't placed the order.",
    "quote_request": "Okay, I haven't sent the request.",
    "ask_owner": "Okay, I haven't sent the question.",
}


# ---------------------------------------------------------------------------- small lookups


def price_view(price: PriceQuote | JsonObj | None) -> PriceView | None:
    """`PriceView` from a domain `PriceQuote` or a profile `price` object."""
    if price is None:
        return None
    get = price.get if isinstance(price, Mapping) else lambda key: getattr(price, key, None)
    kind = get("kind")
    if kind not in ("fixed", "from", "range", "quote_only", "free"):
        return None
    return PriceView(
        kind=kind,
        currency=get("currency"),
        amount_cents=get("amount_cents"),
        max_cents=get("max_cents"),
        per=get("per"),
    )


def item_spoken_name(profile: Profile, item_ref: str | None) -> str | None:
    if not item_ref:
        return None
    try:
        _module_id, item = profile.item(item_ref)
    except Exception:  # noqa: BLE001 - UnknownItem: the item left the profile since the commitment
        return None
    return str(item.get("spoken_name") or item.get("name") or "") or None


def choice_spoken_names(profile: Profile, item_ref: str | None, choices: Mapping[str, str]) -> dict[str, str]:
    """Option group id -> choice spoken name, in the item's option order (unknown ids stay as given)."""
    if not choices:
        return {}
    item: JsonObj | None = None
    if item_ref:
        try:
            _module_id, item = profile.item(item_ref)
        except Exception:  # noqa: BLE001 - UnknownItem: render the raw ids
            item = None
    ordered = [str(g["id"]) for g in (item or {}).get("options", ()) if g.get("id") in choices]
    ordered += [group_id for group_id in choices if group_id not in ordered]
    out: dict[str, str] = {}
    for group_id in ordered:
        choice_id = choices[group_id]
        name = choice_id
        if item is not None:
            try:
                name = str(profile.option_choice(item, group_id, choice_id).get("spoken_name") or choice_id)
            except Exception:  # noqa: BLE001
                name = choice_id
        out[group_id] = name
    return out


def owner_module(profile: Profile, module_id: str | None) -> tuple[str | None, JsonObj]:
    try:
        return profile.owner_approval(module_id)
    except Exception:  # noqa: BLE001 - modules without an owner module still render
        return None, {}


def approver(profile: Profile, module_id: str | None = None) -> str:
    return profile.approver_spoken_name(module_id)


def reply_expectation(profile: Profile, module_id: str | None) -> str | None:
    """'usually replies within 24 hours' from the owner module's `expected_response_minutes`."""
    _owner_id, owner = owner_module(profile, module_id)
    minutes = owner.get("expected_response_minutes")
    if not isinstance(minutes, int) or minutes <= 0:
        return None
    return f"usually replies within {speech.duration(minutes)}"


def waiting_message(profile: Profile, module_id: str | None) -> str:
    """Task `statusMessage` while pending: 'Waiting for Rosa, who usually replies within 24 hours.'"""
    who = approver(profile, module_id)
    expectation = reply_expectation(profile, module_id)
    return f"Waiting for {who}, who {expectation}." if expectation else f"Waiting for {who}."


def _local(profile: Profile, value: datetime) -> datetime:
    return value.astimezone(profile.timezone)


def when_spoken(profile: Profile, *, start: datetime | None, day: date | None) -> str | None:
    """Absolute 'Saturday, October 17 at 10 am' or 'Saturday, October 17' (renders long after the call)."""
    if start is not None:
        return speech.when(_local(profile, start))
    if day is not None:
        return speech.day(day)
    return None


def slot_view(
    profile: Profile,
    slot: SlotOption,
    *,
    resource_names: Mapping[str, str] | None = None,
    today: date | None = None,
) -> SlotView:
    """A domain `SlotOption` as a `SlotView`. The spoken label is absolute unless `today` is given
    (live answers say 'tomorrow at 10 am'; results read later say 'Thursday, October 15 at 10 am')."""
    names = resource_names or {}
    if slot.kind == "time" and slot.window is not None:
        start = _local(profile, slot.window.start)
        end = _local(profile, slot.window.end)
        return SlotView(
            kind="time",
            start=start.isoformat(),
            end=end.isoformat(),
            label_spoken=speech.when(start, today=today),
            resource_id=slot.resource_id,
            resource_spoken_name=names.get(slot.resource_id or ""),
            remaining=slot.remaining,
        )
    day = slot.day or (slot.window.start.date() if slot.window else None)
    return SlotView(
        kind="day",
        date=day.isoformat() if day else None,
        label_spoken=speech.day(day, today=today) if day else "an open day",
        resource_id=slot.resource_id,
        resource_spoken_name=names.get(slot.resource_id or ""),
        remaining=slot.remaining,
    )


def spoken_slots(
    profile: Profile,
    slots: Sequence[SlotOption],
    *,
    today: date | None = None,
    resource_names: Mapping[str, str] | None = None,
) -> str:
    """Slots as one compact phrase, grouped by day and resource:
    'today at 11 am, 11:15 am and 11:30 am with June' / 'Saturday, Sunday and Tuesday'."""
    names = resource_names or {}
    groups: list[tuple[str, str | None, list[str]]] = []
    for slot in slots:
        times: list[str] = []
        if slot.kind == "time" and slot.window is not None:
            start = _local(profile, slot.window.start)
            day_label = speech.day(start.date(), today=today)
            times = [speech.clock_time(start)]
        else:
            day = slot.day or (slot.window.start.date() if slot.window else None)
            if day is None:
                continue
            day_label = speech.day(day, today=today)
        who = names.get(slot.resource_id or "")
        if groups and groups[-1][0] == day_label and groups[-1][1] == who and times:
            groups[-1][2].extend(times)
        else:
            groups.append((day_label, who, times))
    phrases = []
    for day_label, who, times in groups:
        phrase = f"{day_label} at {speech.join(times)}" if times else day_label
        phrases.append(f"{phrase} with {who}" if who else phrase)
    return speech.join(phrases)


def resource_names(profile: Profile, module_id: str | None) -> dict[str, str]:
    """Resource id -> spoken name for a booking module (stylists, tables...)."""
    if not module_id:
        return {}
    try:
        module = profile.module(module_id)
    except Exception:  # noqa: BLE001
        return {}
    return {str(r["id"]): str(r.get("spoken_name") or r["id"]) for r in module.get("resources", ()) if "id" in r}


def _owner_text(text: Any, *, for_speech: bool = True) -> str | None:
    """The owner's free text made speakable and capped; `for_speech` drops a final period so the
    sentence builder can embed it ("Rosa says: See you Sunday.")."""
    if not isinstance(text, str) or not text.strip():
        return None
    cleaned = sanitize_speak(text)
    return speech.truncate(cleaned.rstrip(".") if for_speech else cleaned, OWNER_TEXT_MAX)


def _owner_fact(commitment: Commitment, field: str, value: Any) -> Fact:
    return Fact(
        fact_ref=f"{commitment.module_id}/{commitment.commitment_id}#{field}",
        value=value,
        evidence_url=f"{OWNER_ANSWER_URN}{commitment.commitment_id}",
        owner_confirmed=True,
    )


def _response_fields(profile: Profile, module_id: str) -> list[JsonObj]:
    try:
        module = profile.module(module_id)
    except Exception:  # noqa: BLE001
        return []
    return list((module.get("approval") or {}).get("response_fields", ()))


def quote_view(profile: Profile, commitment: Commitment) -> PriceView | None:
    """The owner's quoted amount: the first money-type response field with a value (cents)."""
    if commitment.decision is None:
        return None
    values = commitment.decision.values
    for spec in _response_fields(profile, commitment.module_id):
        if spec.get("type") == "money" and isinstance(values.get(spec.get("key")), int):
            return PriceView(kind="fixed", currency=profile.currency, amount_cents=values[spec["key"]])
    return None


def _reply_choice_label(profile: Profile, commitment: Commitment) -> str | None:
    """Label of the owner's choice-type response (e.g. 'Free on-site estimate')."""
    if commitment.decision is None:
        return None
    values = commitment.decision.values
    for spec in _response_fields(profile, commitment.module_id):
        if spec.get("type") == "choice" and spec.get("key") in values:
            for option in spec.get("options", ()):
                if option.get("value") == values[spec["key"]]:
                    return str(option.get("label") or option["value"])
    return None


def _suggestion(profile: Profile, commitment: Commitment) -> str | None:
    if commitment.decision is None:
        return None
    values = commitment.decision.values
    raw_day = values.get("suggested_date")
    raw_time = values.get("suggested_time")
    try:
        day = date.fromisoformat(raw_day) if isinstance(raw_day, str) else None
        at = time.fromisoformat(raw_time) if isinstance(raw_time, str) else None
    except ValueError:
        return None
    if day and at:
        return f"{speech.day(day)} at {speech.clock_time(at)}"
    if day:
        return speech.day(day)
    if at:
        return speech.clock_time(at)
    return None


# ---------------------------------------------------------------------------- views


def commitment_view(profile: Profile, commitment: Commitment) -> CommitmentView:
    """Structured view of a commitment for results and cards (no PII except what the customer gave)."""
    decision = commitment.decision
    answer = None
    if commitment.kind == "owner_question" and decision is not None:
        answer = _owner_text(decision.values.get("answer"), for_speech=False) or _owner_text(decision.note, for_speech=False)
    start = _local(profile, commitment.window.start) if commitment.window else None
    return CommitmentView(
        reference=commitment.commitment_id,
        kind=commitment.kind,
        status=commitment.status,
        item_ref=commitment.item_ref,
        item_name=item_spoken_name(profile, commitment.item_ref),
        start=start.isoformat() if start else None,
        date=commitment.day.isoformat() if commitment.day else None,
        when_spoken=when_spoken(profile, start=commitment.window.start if commitment.window else None, day=commitment.day),
        party_size=commitment.party_size,
        choices=choice_spoken_names(profile, commitment.item_ref, commitment.choices),
        price=price_view(commitment.price),
        decision=decision.decision if decision else None,
        owner_note=_owner_text(decision.note, for_speech=False) if decision else None,
        quote=quote_view(profile, commitment),
        answer=answer,
        expires_at=commitment.expires_at.isoformat() if commitment.expires_at else None,
    )


def _what(profile: Profile, commitment: Commitment) -> str:
    """'men's cut on Saturday, October 17 at 10 am' / 'custom cake for Sunday, October 18' / 'request'."""
    item = item_spoken_name(profile, commitment.item_ref)
    when = when_spoken(profile, start=commitment.window.start if commitment.window else None, day=commitment.day)
    if commitment.kind == "owner_question":
        return "question"
    if commitment.kind == "quote_request":
        return f"quote request for {item}" if item else "quote request"
    if commitment.kind == "booking":
        return f"{item or 'booking'} on {when}" if when else (item or "booking")
    return f"{item or 'order'} for {when}" if when else (item or "order")


def _card(
    profile: Profile,
    *,
    kind: str,
    title: str,
    status: str,
    lines: Sequence[tuple[str, str | None]],
    facts: Sequence[Fact],
    actions: Sequence[CardAction] = (),
) -> Card:
    return Card(
        kind=kind,  # type: ignore[arg-type] - one of the Card kinds
        title=title,
        subtitle=profile.name,
        lines=[CardLine(label=label, value=value) for label, value in lines if value],
        badges=[STATUS_LABELS.get(status, status)],
        citations=grounding.citations_for(profile, facts),
        actions=list(actions),
    )


BOOKING_CODE_STATUSES = frozenset({"confirmed", "approved"})
"""A booking says its code once it is actually booked (not while the owner has yet to approve it)."""


def booking_code(commitment: Commitment) -> str | None:
    """The 8-digit code a voice customer can say back to cancel this booking (`ids.spoken_reference`),
    for a confirmed or approved booking; None otherwise."""
    if commitment.kind != "booking" or commitment.status not in BOOKING_CODE_STATUSES:
        return None
    return spoken_reference(commitment.commitment_id)


def _code_fact(commitment: Commitment, code: str) -> Fact:
    """The spoken code is a value fixed in the stored commitment (its id), cited like a fixed price."""
    return Fact(
        fact_ref=f"{commitment.module_id}/{commitment.commitment_id}#booking_code",
        value=code,
        evidence_url=f"{COMMITMENT_URN}{commitment.commitment_id}",
        owner_confirmed=False,
    )


def _commitment_speech(profile: Profile, commitment: Commitment, facts: list[Fact], *, code: str | None = None) -> str:
    """The status line. `code` (a booking code) is said last and survives any shortening (`tail`)."""
    who = approver(profile, commitment.module_id)
    what = _what(profile, commitment)
    decision = commitment.decision
    note = _owner_text(decision.note) if decision else None
    note_sentence = f"{who} says: {note}" if note else None
    if note:
        facts.append(_owner_fact(commitment, "owner_note", note))
    status = commitment.status
    price = speech.price(commitment.price) if commitment.price and commitment.price.kind != "from" else None
    code_sentence = f"Your booking code is {speech.digits(code)}" if code else None
    if status == "confirmed":
        if commitment.kind == "booking":
            return speech.sentences(f"You're booked: {what}", f"The price is {price}" if price else None, tail=code_sentence)
        return speech.sentences(f"Your {what} is confirmed", f"The total is {price}" if price else None)
    if status == "pending_owner":
        expectation = reply_expectation(profile, commitment.module_id)
        tail = f", who {expectation}" if expectation else ""
        if decision is not None and decision.decision == "request_info":
            # The customer answered the owner's question: the request is back with the owner.
            follow_up = "I'll let you know the quote" if commitment.kind == "quote_request" else f"Nothing is confirmed until {who} approves it"
            return speech.sentences(f"I've passed your answer on to {who}{tail}", follow_up)
        if commitment.kind == "owner_question":
            return speech.sentences(f"I've sent your question to {who}{tail}", "I'll let you know the answer")
        if commitment.kind == "quote_request":
            return speech.sentences(f"I've sent your quote request to {who}{tail}", "I'll let you know the quote")
        return speech.sentences(
            f"I've sent your request to {who}{tail}", f"Nothing is confirmed until {who} approves it"
        )
    if status == "approved":
        return speech.sentences(
            f"Good news: {who} confirmed your {what}",
            f"The total is {price}" if price else None,
            note_sentence,
            tail=code_sentence,
        )
    if status == "declined":
        return speech.sentences(f"Sorry, {who} can't take your {what}", note_sentence)
    if status == "counter_offer":
        change = note or "a change to your request"
        return speech.sentences(f"{who} suggested a change: {change}", "Want to go ahead that way?")
    if status == "suggested_time":
        suggestion = _suggestion(profile, commitment)
        if suggestion:
            facts.append(_owner_fact(commitment, "suggested_time", suggestion))
            return speech.sentences(f"{who} can't do that time, but suggests {suggestion}", note_sentence)
        return speech.sentences(f"{who} can't do that time", note_sentence)
    if status == "quoted":
        quote = quote_view(profile, commitment)
        label = _reply_choice_label(profile, commitment)
        parts: list[str | None] = []
        if quote is not None and quote.amount_cents is not None:
            facts.append(_owner_fact(commitment, "quote", quote.amount_cents))
            parts.append(f"{profile.spoken_name} sent a quote: {speech.money(quote.amount_cents, profile.currency)}")
        elif label:
            facts.append(_owner_fact(commitment, "reply", label))
            parts.append(f"{profile.spoken_name} replied: {label}")
        else:
            parts.append(f"{profile.spoken_name} replied to your quote request")
        return speech.sentences(*parts, note_sentence)
    if status == "info_requested":
        question = _owner_text(decision.question) if decision else None
        if question:
            facts.append(_owner_fact(commitment, "question", question))
            return speech.sentences(f"{who} needs a bit more information: {question}")
        return speech.sentences(f"{who} needs a bit more information")
    if status == "answered":
        answer = None
        if decision is not None:
            answer = _owner_text(decision.values.get("answer")) or note
        if answer:
            facts.append(_owner_fact(commitment, "answer", answer))
            return speech.sentences(f"{who} says: {answer}")
        return speech.sentences(f"{who} answered your question")
    if status == "cancelled":
        return speech.sentences(f"Your {what} is cancelled")
    if status == "expired":
        if decision is not None and decision.decision == "request_info" and decision.answered_at is None:
            return speech.sentences(f"Your request lapsed because {who}'s question wasn't answered in time")
        return speech.sentences(f"{who} hasn't answered yet, so your request isn't confirmed")
    return speech.sentences(f"Your {what} is {STATUS_LABELS.get(status, status).lower()}")


def _commitment_actions(profile: Profile, commitment: Commitment) -> list[CardAction]:
    if commitment.task_id and commitment.status in OPEN_STATUSES:
        return [CardAction(label="Check status", tool="get_task", arguments={"task_id": commitment.task_id})]
    if commitment.kind == "booking" and commitment.status in ("confirmed", "approved"):
        return [
            CardAction(
                label="Cancel booking",
                tool="cancel_booking",
                arguments={"business_id": profile.profile_id, "reference": commitment.commitment_id},
            )
        ]
    return []


def _price_still_published(profile: Profile, commitment: Commitment) -> bool:
    """True if the profile serving this result still prices the commitment exactly as stored (same
    revision, or a later one whose price facts give the same quote)."""
    if commitment.price is None:
        return False
    if profile.revision == commitment.profile_revision:
        return True
    if not commitment.item_ref:
        return False
    try:
        now = price_for(profile, commitment.item_ref, choices=commitment.choices, party_size=commitment.party_size)
    except Exception:  # noqa: BLE001 - the item left the profile: the stored price stands on its own
        return False
    if now is None:
        return False
    keys = ("kind", "currency", "amount_cents", "max_cents", "per")
    return all(getattr(now, key) == getattr(commitment.price, key) for key in keys)


def price_facts(profile: Profile, commitment: Commitment) -> list[Fact]:
    """Facts behind the price a commitment result speaks.

    The profile's price facts when they still give the stored price; otherwise (the bakery changed its
    prices after the order) one fact for the price fixed in the commitment, with evidence
    `urn:askonce:commitment:<id>`, so the number spoken never cites a profile value that differs."""
    if commitment.price is None:
        return []
    if _price_still_published(profile, commitment):
        current = grounding.facts_for(profile, list(commitment.price.fact_refs))
        if len(current) == len(set(commitment.price.fact_refs)):
            return current
    return [
        Fact(
            fact_ref=f"{commitment.module_id}/{commitment.commitment_id}#price",
            value=commitment.price.model_dump(mode="json", exclude={"lines", "fact_refs"}),
            evidence_url=f"{COMMITMENT_URN}{commitment.commitment_id}",
            owner_confirmed=False,
        )
    ]


def commitment_result(
    profile: Profile, commitment: Commitment, *, replayed: bool = False, with_code: bool = True
) -> CommitmentResult:
    """The grounded result for a stored commitment in any status (price facts: see `price_facts`).

    A confirmed or approved booking speaks its booking code and shows it on the card (`booking_code`).
    The code is a bearer capability like the reference (it cancels the booking), so a result for
    someone who may not be the customer (an anonymous replay) passes `with_code=False`."""
    facts: list[Fact] = []
    if commitment.item_ref:
        facts.extend(grounding.facts_for(profile, [f"{commitment.item_ref}#spoken_name"]))
    facts.extend(price_facts(profile, commitment))
    code = booking_code(commitment) if with_code else None
    if code:
        facts.append(_code_fact(commitment, code))
    speak = _commitment_speech(profile, commitment, facts, code=code)
    view = commitment_view(profile, commitment)
    facts = grounding.dedupe_facts(facts)
    title = speech.capitalize(view.item_name) if view.item_name else ("Your question" if commitment.kind == "owner_question" else "Your request")
    card = _card(
        profile,
        kind="task" if commitment.kind == "owner_question" else "commitment",
        title=title,
        status=commitment.status,
        lines=[
            ("When", view.when_spoken),
            *((speech.capitalize(k.replace("_", " ").replace("-", " ")), v) for k, v in view.choices.items()),
            ("Party size", str(view.party_size) if view.party_size else None),
            ("Price", speech.price(commitment.price) if commitment.price else None),
            ("Quote", speech.price(view.quote) if view.quote else None),
            ("Owner note", view.owner_note),
            ("Answer", view.answer),
            (BOOKING_CODE_LABEL, f"{code[:4]} {code[4:]}" if code else None),
            ("Reference", commitment.commitment_id),
        ],
        facts=facts,
        actions=_commitment_actions(profile, commitment),
    )
    return CommitmentResult(
        speak=speak,
        facts=facts,
        card=card,
        business_id=profile.profile_id,
        profile_revision=profile.revision,
        status=commitment.status,
        reference=commitment.commitment_id,
        task_id=commitment.task_id,
        commitment=view,
        replayed=replayed,
    )


# ---------------------------------------------------------------------------- nothing stored


def _alternatives(problems: Sequence[PlanProblem]) -> list[SlotOption]:
    """Up to `MAX_ALTERNATIVES` distinct alternative slots across the plan's problems."""
    out: list[SlotOption] = []
    for problem in problems:
        for slot in problem.alternatives:
            if slot not in out:
                out.append(slot)
            if len(out) >= MAX_ALTERNATIVES:
                return out
    return out


def _group_question(profile: Profile, item_ref: str | None, group_id: str | None) -> tuple[str, str]:
    """(spoken question, group name) for an item option group; generic text when unknown."""
    if item_ref and group_id:
        try:
            _module_id, item = profile.item(item_ref)
        except Exception:  # noqa: BLE001
            item = None
        for group in (item or {}).get("options", ()) if item else ():
            if group.get("id") == group_id:
                name = str(group.get("name") or group_id).lower()
                return str(group.get("spoken_question") or f"Which {name} would you like?"), name
    name = (group_id or "option").replace("_", " ").replace("-", " ")
    return f"Which {name} would you like?", name


def _focused_question(profile: Profile, plan: CommitPlan, group_id: str | None) -> tuple[str, list[str]] | None:
    """The question for an option group the item phrase narrowed (`plan.choice_focus`), offering only
    those choices, and the facts it states; None when the group was not narrowed."""
    focus = next((f for f in plan.choice_focus if f.group_id == group_id), None)
    if focus is None or not plan.item_ref or not focus.choice_ids:
        return None
    try:
        _module_id, item = profile.item(plan.item_ref)
    except Exception:  # noqa: BLE001
        return None
    group = next((g for g in item.get("options", ()) or () if g.get("id") == group_id), None)
    if group is None:
        return None
    by_id = {str(c["id"]): c for c in group.get("choices") or ()}
    refs = [f"{plan.item_ref}/{group_id}/{cid}#spoken_name" for cid in focus.choice_ids if cid in by_id]
    names = [str(by_id[cid].get("spoken_name") or by_id[cid].get("name") or cid) for cid in focus.choice_ids if cid in by_id]
    if not names:
        return None
    noun = str(group.get("name") or group_id).lower()
    label = focus.label or ""
    after = label.startswith(("without", "for ", "with "))
    described = f"{noun} {label}" if after else (f"{label} {noun}" if label else noun)
    if len(names) == 1:
        return f"Would you like {names[0]} for the {noun}? It's the only {described} I can offer.", refs
    return f"Which {described}: {speech.join(names, conjunction='or')}?", refs


_TOO_LONG = re.compile(r"must be at most (\d+) characters$")
_AT_MOST = re.compile(r"must be at most ([\d.]+)$")
_AT_LEAST = re.compile(r"must be at least ([\d.]+)$")


def _invalid_intake_speech(label: str, detail: str) -> str:
    """Why an intake answer was refused, in words the customer can act on. `detail` comes from
    `domain.intake` ("<label>: <reason>", reasons are fixed templates that never echo the input), so
    only recognised reasons are turned into speech; anything else gets the generic retry."""
    reason = detail.rsplit(": ", 1)[-1].strip()
    if match := _TOO_LONG.search(reason):
        return f"The {label} can be at most {match.group(1)} characters. Could you make it shorter?"
    if match := _AT_MOST.search(reason):
        return f"The {label} can be at most {match.group(1)}. Could you say it again?"
    if match := _AT_LEAST.search(reason):
        return f"The {label} has to be at least {match.group(1)}. Could you say it again?"
    if "phone number" in reason or "digit" in reason or "country code" in reason:
        return f"That doesn't sound like a full {label}. Could you say it again, with the area code?"
    if "email" in reason:
        return f"That doesn't sound like an email address. Could you say the {label} again?"
    if "date" in reason:
        return f"I didn't catch the {label}. Which day is it for?"
    return f"I couldn't use the {label}. Could you say it again?"


def _intake_label(profile: Profile, module_id: str, key: str | None) -> str:
    if key:
        try:
            for spec in profile.intake_fields(module_id):
                if spec.get("key") == key:
                    return str(spec.get("label") or key).lower()
        except Exception:  # noqa: BLE001
            pass
    return (key or "details").replace("_", " ")


def problem_result(
    profile: Profile, plan: CommitPlan, *, asked: str | None = None, today: date | None = None
) -> CommitmentResult:
    """`not_offered` / `needs_info` / `unavailable` for a plan that cannot be committed (nothing stored).

    `asked` is what the customer asked for (used in the hand-off question). `today` (business-local)
    makes alternative labels relative ("Tuesday at 10 am"); without it they are absolute.
    """
    problems = list(plan.problems)
    first = problems[0] if problems else None
    alternatives = _alternatives(problems)
    names = resource_names(profile, plan.module_id)
    views = [slot_view(profile, s, resource_names=names, today=today) for s in alternatives]
    facts = grounding.facts_for(profile, [ref for s in alternatives for ref in s.fact_refs])
    handoff = None
    missing_fields: list[str] = []
    business = profile.spoken_name
    lead: str
    if first is not None and first.code in _NOT_OFFERED_CODES:
        status = "not_offered"
        lead = f"{business} doesn't offer that through me."
        question = f"Do you offer {asked}?" if asked else "Can you help with my request?"
        if grounding.can_ask_owner(profile, plan.module_id):
            handoff = grounding.handoff(profile, reason="not_offered", question=question, module_id=plan.module_id)
    elif first is not None and first.code == "disputed_fact":
        status = "unavailable"
        lead = f"{approver(profile, plan.module_id)} is double-checking some details, so I can't do this yet."
        if grounding.can_ask_owner(profile, plan.module_id):
            handoff = grounding.handoff(
                profile,
                reason="disputed",
                question=asked or "Can you confirm the details for my request?",
                fact_ref=first.fact_ref,
                module_id=plan.module_id,
            )
    elif first is None or first.code in _NEEDS_INFO_CODES:
        status = "needs_info"
        choice_problems = [p for p in problems if p.code in ("missing_choice", "invalid_choice")]
        if choice_problems:
            # Name every open choice, then ask the first one's own spoken question (or, when the item
            # phrase narrowed that group, a question offering only the fitting choices).
            first_choice = choice_problems[0]
            question, name = _group_question(profile, plan.item_ref, first_choice.field)
            focused = _focused_question(profile, plan, first_choice.field)
            if focused is not None:
                question, refs = focused
                facts = grounding.dedupe_facts([*facts, *grounding.facts_for(profile, refs)])
            names_needed = [_group_question(profile, plan.item_ref, p.field)[1] for p in choice_problems]
            if plan.choice_notes:
                lead = " ".join([*plan.choice_notes, question])
                facts = grounding.dedupe_facts([*facts, *grounding.facts_for(profile, plan.choice_note_refs)])
            elif first_choice.code == "invalid_choice":
                lead = f"That {name} isn't one of the options. {question}"
            elif len(names_needed) > 1:
                lead = f"I still need the {speech.join(names_needed)}. {question}"
            else:
                lead = question
            missing_fields = [p.field for p in choice_problems if p.field]
        elif first is not None and first.code == "invalid_intake":
            lead = _invalid_intake_speech(_intake_label(profile, plan.module_id, first.field), first.detail)
            missing_fields = [first.field] if first.field else []
        elif first is not None and first.code == "party_size_out_of_range" and first.detail == ONE_PERSON_EACH:
            lead = "Each appointment is for one person, so I'll book them one at a time. Who's the first one for?"
            missing_fields = [first.field] if first.field else []
        elif first is not None:
            lead = _PROBLEM_SPEECH.get(first.code, "I need a bit more information.").format(business=business)
            missing_fields = [first.field] if first.field else []
        else:
            labels = [m.label.lower() for m in plan.missing]
            lead = f"I still need {speech.join(labels)}." if labels else "I need a bit more information."
        missing_fields += [m.key for m in plan.missing if m.key not in missing_fields]
    else:
        status = "unavailable"
        if first.code == "capacity_full":
            lead = "That day is fully booked." if plan.window is None else "That time is already taken."
        else:
            lead = _PROBLEM_SPEECH.get(first.code, "That isn't available.").format(business=business)
    alt_sentence = None
    if alternatives:
        noun = "open time" if alternatives[0].kind == "time" else "open day"
        phrase = spoken_slots(profile, alternatives, today=today, resource_names=names)
        alt_sentence = f"The nearest {noun}s are {phrase}" if len(alternatives) > 1 else f"The nearest {noun} is {phrase}"
    offer = grounding.offer_sentence(profile, plan.module_id) if handoff is not None else None
    speak = speech.sentences(lead, alt_sentence, tail=offer)
    card = _card(
        profile,
        kind="handoff" if handoff is not None else "commitment",
        title=STATUS_LABELS[status],
        status=status,
        lines=[(speech.capitalize(v.label_spoken), v.resource_spoken_name or "Open") for v in views],
        facts=facts,
        actions=[
            CardAction(
                label=f"Ask {handoff.approver_spoken_name}",
                tool="ask_owner",
                arguments={"business_id": profile.profile_id, "question": handoff.suggested_question},
            )
        ]
        if handoff is not None
        else [],
    )
    return CommitmentResult(
        speak=speak,
        facts=facts,
        card=card,
        handoff=handoff,
        business_id=profile.profile_id,
        profile_revision=profile.revision,
        status=status,  # type: ignore[arg-type]
        alternatives=views,
        missing_fields=missing_fields,
    )


def not_confirmed_result(profile: Profile, plan: CommitPlan, *, commitment: Commitment | None = None) -> CommitmentResult:
    """The customer said no (or declined/cancelled the form): nothing was written."""
    speak = speech.sentences(_NOT_CONFIRMED_SPEECH.get(plan.operation, "Okay, I haven't done anything."))
    return CommitmentResult(
        speak=speak,
        card=_card(profile, kind="commitment", title="Not sent", status="not_confirmed", lines=[], facts=[]),
        business_id=profile.profile_id,
        profile_revision=profile.revision,
        status="not_confirmed",
        reference=commitment.commitment_id if commitment else None,
        commitment=commitment_view(profile, commitment) if commitment else None,
    )


# ---------------------------------------------------------------------------- wire


def to_call_tool_result(result: CommitmentResult) -> types.CallToolResult:
    """Serialise like the SDK does for a structured tool result."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=pydantic_core.to_json(result, indent=2).decode())],
        structured_content=result.model_dump(mode="json", by_alias=True),
    )


class CommitmentRenderer:
    """`DecisionRenderer` implementation handed to the domain's OwnerService."""

    def render(self, profile: Profile, commitment: Commitment) -> types.CallToolResult:
        return to_call_tool_result(guard_result(profile, commitment_result(profile, commitment)))
