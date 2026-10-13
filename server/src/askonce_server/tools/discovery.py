"""Discovery tools: find_business (multi-tenant entry point) and get_business_info.

No `from __future__ import annotations` in tool modules: the SDK builds schemas from real annotations.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from askonce_server.errors import UnknownBusiness
from askonce_server.profile.model import JsonObj, Profile
from askonce_server.tools import grounding, search, speech
from askonce_server.tools.context import finish, get_deps, live_profile, tool_span
from askonce_server.tools.schemas import (
    BusinessInfoResult,
    BusinessMatch,
    Card,
    CardAction,
    CardLine,
    Fact,
    FindBusinessResult,
    HandOff,
    HoursLine,
)

BUSINESS_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
"""The schema's `$defs/id` pattern: anything else cannot name a profile, so it is refused up front."""

BusinessId = Annotated[
    str,
    Field(description="Business id from find_business, e.g. thimbleberry-bakehouse.", max_length=64, pattern=BUSINESS_ID_PATTERN),
]

Topic = Literal["summary", "hours", "location", "contact"]
DEFAULT_TOPICS: tuple[Topic, ...] = ("summary", "hours")


# ---------------------------------------------------------------------------- find_business


async def find_business(
    need: Annotated[
        str,
        Field(description="What the customer wants, in their words, e.g. 'gluten-free birthday cake for 30'.", max_length=300),
    ],
    ctx: Context,
    category: Annotated[
        str | None, Field(description="Optional kind of business: bakery, hair salon, plumber...", max_length=60)
    ] = None,
    locality: Annotated[
        str | None,
        Field(
            description="Optional town or ZIP code where the customer needs it, e.g. 'Portland' or '80211'. Omit when unknown.",
            max_length=60,
        ),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> FindBusinessResult:
    """Find certified local businesses that can help with a need. Returns business ids to use with the
    other tools. Only businesses that passed AskOnce certification are listed."""
    deps = get_deps(ctx)
    with tool_span("find_business"):
        entries = await deps.profiles.list_live()
        ranked = search.rank(entries, need=need, category=category, locality=locality, limit=limit)
        matches: list[BusinessMatch] = []
        facts: list[Fact] = []
        profiles: list[Profile] = []
        for match in ranked:
            try:
                profile = await deps.profiles.get_live(match.entry.business_id)
            except UnknownBusiness:
                continue  # went non-live since the directory was read
            profiles.append(profile)
            facts.extend(grounding.facts_for(profile, ["business#name", "business#categories"]))
            matches.append(
                BusinessMatch(
                    business_id=match.entry.business_id,
                    name=match.entry.name,
                    spoken_name=match.entry.spoken_name,
                    summary_spoken=match.entry.summary_spoken,
                    categories=list(match.entry.categories),
                    locality=_locality(profile),
                    score=match.score,
                    matched_on=match.matched_on,
                )
            )
        if not matches:
            elsewhere = bool(search.locality_wanted(locality)) and bool(
                search.rank(entries, need=need, category=category, limit=1)
            )
            # The customer's place is never echoed: "that area", not whatever the host passed.
            lead = (
                "I don't have a certified business for that in that area yet"
                if elsewhere
                else "I couldn't find a certified business for that yet"
            )
            result = FindBusinessResult(
                speak=speech.sentences(lead, None if elsewhere else "Try describing it another way"),
                card=grounding.notice_card("No matching businesses"),
            )
            return finish(result, None, outcome="not_in_area" if elsewhere else "no_match")
        top, top_profile = matches[0], profiles[0]
        summary = grounding.state(top_profile, "business#summary_spoken")
        if summary.fact is not None:
            facts.append(summary.fact)
        if len(matches) == 1:
            speak = speech.sentences(
                f"{top.spoken_name} can help", summary.value if summary.ok and isinstance(summary.value, str) else None
            )
        else:
            # Each business named with its own trade, so nobody hears one business's summary as
            # another's ("Kettlewick Home Repair, a plumber, and Quillfern Hair Studio, a hair salon").
            named = [_with_trade(m) for m in matches]
            speak = speech.sentences(f"I found {len(matches)} businesses that can help: {_list_with_appositives(named)}")
        card = Card(
            kind="business",
            title="Businesses that can help" if len(matches) > 1 else top.name,
            subtitle=None if len(matches) > 1 else top.summary_spoken,
            lines=[CardLine(label=m.name, value=m.summary_spoken or ", ".join(m.categories)) for m in matches]
            if len(matches) > 1
            else [CardLine(label="Area", value=m.locality) for m in matches if m.locality],
            badges=["Certified"],
            citations=grounding.citations_for(top_profile, facts[:2]),
            actions=[
                CardAction(label=f"About {m.name}", tool="get_business_info", arguments={"business_id": m.business_id})
                for m in matches[:3]
            ],
        )
        result = FindBusinessResult(speak=speak, facts=facts, card=card, businesses=matches)
        return finish(result, None, outcome="ok", fact_count=len(facts))


def _with_trade(match: BusinessMatch) -> str:
    """'Quillfern Hair Studio, a hair salon' (the first category), or the bare name."""
    if not match.categories:
        return match.spoken_name
    trade = match.categories[0].replace("_", " ")
    article = "an" if trade[:1].lower() in "aeiou" else "a"
    return f"{match.spoken_name}, {article} {trade}"


def _list_with_appositives(parts: list[str]) -> str:
    """Join phrases that contain commas: 'A, a plumber, and B, a hair salon' / 'A, a x; B, a y; and C'."""
    if len(parts) <= 2:
        return f"{parts[0]}, and {parts[1]}" if len(parts) == 2 and "," in parts[0] else speech.join(parts)
    return "; ".join(parts[:-1]) + f"; and {parts[-1]}"


def _locality(profile: Profile) -> str | None:
    location = profile.primary_location()
    address = (location or {}).get("address") or {}
    value = address.get("locality")
    return str(value) if value else None


# ---------------------------------------------------------------------------- hours arithmetic


def _minutes(hhmm: str) -> int:
    hour, minute = (int(p) for p in hhmm.split(":", 1))
    return hour * 60 + minute


def closure_on(closures: Any, day: date) -> JsonObj | None:
    """The closure covering `day` (`to` is inclusive; omitted = one day), if any."""
    for closure in closures or ():
        try:
            start = date.fromisoformat(str(closure["from"]))
            end = date.fromisoformat(str(closure.get("to") or closure["from"]))
        except (KeyError, ValueError):
            continue
        if start <= day <= end:
            return closure
    return None


def day_intervals(weekly: Any, closures: Any, day: date) -> list[tuple[str, str]]:
    """Open `(HH:MM, HH:MM)` spans on a local date (empty when closed)."""
    if closure_on(closures, day) is not None:
        return []
    return speech.intervals_by_day(weekly or ()).get(speech.WEEKDAYS[day.weekday()], [])


@dataclass(frozen=True)
class OpenStatus:
    open_now: bool
    closes_at: str | None
    next_open: tuple[date, str] | None


def open_status(weekly: Any, closures: Any, now_local: datetime) -> OpenStatus:
    """Whether a timetable is open at `now_local`, when it closes, and the next opening within a week."""
    today = now_local.date()
    minute = now_local.hour * 60 + now_local.minute
    for open_, close in day_intervals(weekly, closures, today):
        if _minutes(open_) <= minute < _minutes(close):
            return OpenStatus(True, close, None)
    for offset in range(0, 8):
        day = today + timedelta(days=offset)
        for open_, _close in day_intervals(weekly, closures, day):
            if offset > 0 or _minutes(open_) > minute:
                return OpenStatus(False, None, (day, open_))
    return OpenStatus(False, None, None)


def _hours_location(profile: Profile) -> JsonObj | None:
    primary = profile.primary_location()
    if primary is not None and primary.get("hours"):
        return primary
    for location in profile.locations():
        if location.get("hours"):
            return location
    return primary


def _address_text(address: Any) -> str | None:
    if not address:
        return None
    region = " ".join(str(p) for p in (address.get("region"), address.get("postal_code")) if p)
    parts = [address.get("line1"), address.get("line2"), address.get("locality"), region]
    return ", ".join(str(p) for p in parts if p)


# ---------------------------------------------------------------------------- get_business_info


@dataclass
class _Section:
    sentences: list[str]
    """What the topic must say (dropped only when nothing else fits, with an offer to go on)."""
    facts: list[Fact]
    lines: list[CardLine]
    extra: list[str] | None = None
    """Nice to have (the hours note): spoken only if there is room."""
    detail: list[str] | None = None
    """The full weekly hours: required when hours are all that was asked, otherwise spoken after the
    other requested topics (the phone number someone asked for comes before the whole timetable)."""


def _hours_section(
    profile: Profile, location: JsonObj | None, *, now_local: datetime, on_date: date | None
) -> tuple[_Section, list[HoursLine] | None, bool | None, HandOff | None, list[str]]:
    """(section, hours, open_now, handoff, badges) for the hours topic, honouring L6 withholding."""
    name = profile.spoken_name
    if location is None or "id" not in location:
        hand = grounding.handoff(profile, reason="unknown", question="What are your opening hours?")
        return _Section([f"I don't have {name}'s opening hours"], [], []), None, None, hand, []
    ref = f"locations/{location['id']}#hours.weekly"
    weekly = grounding.state(profile, ref)
    if not weekly.ok:
        reason = "disputed" if weekly.disputed else "unknown"
        hand = grounding.handoff(profile, reason=reason, question="What are your opening hours?", fact_ref=ref)
        sentence = (
            f"{profile.approver_spoken_name()} is double-checking the opening hours, so I can't say for sure yet"
            if weekly.disputed
            else f"I don't have {name}'s opening hours"
        )
        return _Section([sentence], [], []), None, None, hand, []
    facts = [weekly.fact] if weekly.fact else []
    closures_ref = f"locations/{location['id']}#hours.closures"
    closures_got = grounding.state(profile, closures_ref)
    closures = closures_got.value if closures_got.ok else []
    note = grounding.state(profile, f"locations/{location['id']}#hours.note_spoken")
    lines = [HoursLine(days=list(line["days"]), open=str(line["open"]), close=str(line["close"])) for line in weekly.value]
    today = now_local.date()
    if grounding.is_disputed(profile, closures_got):
        # Which days are closed is being double-checked (L6): the weekly hours stand, but whether the
        # business is open on a given day (or right now) is not said either way.
        when = "right now" if on_date is None or on_date == today else _on(speech.day(on_date, today=today))
        hand = grounding.handoff(
            profile, reason="disputed", question="Which days are you closed for holidays?", fact_ref=closures_ref
        )
        sentences = [
            f"{profile.approver_spoken_name()} is double-checking the holiday closures, so I can't say for sure "
            f"whether {name} is open {when}",
            f"Hours are {speech.hours(weekly.value)}",
        ]
        card_lines = [CardLine(label="Hours", value=speech.hours(weekly.value))]
        return _Section(sentences, facts, card_lines), lines, None, hand, []
    sentences: list[str] = []
    badges: list[str] = []
    open_now: bool | None = None
    if on_date is not None and on_date != today:
        spans = day_intervals(weekly.value, closures, on_date)
        closure = closure_on(closures, on_date)
        when = speech.day(on_date, today=today)
        if closure is not None and closures_got.fact:
            facts.append(closures_got.fact)
            reason = closure.get("reason_spoken")
            sentences.append(f"{name} is closed {_on(when)}" + (f" for {reason}" if reason else ""))
        elif spans:
            sentences.append(f"{speech.capitalize(_on(when))}, {name} is open {speech.join([speech.interval(o, c) for o, c in spans])}")
        else:
            sentences.append(f"{name} is closed {_on(when)}")
    else:
        status = open_status(weekly.value, closures, now_local)
        open_now = status.open_now
        if closure_on(closures, today) is not None and closures_got.fact:
            facts.append(closures_got.fact)
        badges.append("Open now" if status.open_now else "Closed now")
        if status.open_now and status.closes_at:
            sentences.append(f"{name} is open now, until {speech.hhmm(status.closes_at)}")
        elif status.next_open is not None:
            day, at = status.next_open
            when = "today" if day == today else speech.day(day, today=today)
            sentences.append(f"{name} is closed right now and opens {when} at {speech.hhmm(at)}")
        else:
            sentences.append(f"{name} is closed right now")
    detail = [f"Hours are {speech.hours(weekly.value)}"] if on_date is None or on_date == today else []
    extra: list[str] = []
    if note.ok and isinstance(note.value, str):
        extra.append(note.value)
        if note.fact:
            facts.append(note.fact)
    card_lines = [CardLine(label="Hours", value=speech.hours(weekly.value))]
    return _Section(sentences, facts, card_lines, extra, detail), lines, open_now, None, badges


def _on(when: str) -> str:
    return when if when in ("today", "tomorrow") else f"on {when}"


async def get_business_info(
    business_id: BusinessId,
    ctx: Context,
    topics: Annotated[
        list[Literal["summary", "hours", "location", "contact"]] | None,
        Field(description="What to include; default summary and hours."),
    ] = None,
    on_date: Annotated[
        str | None, Field(description="Local date YYYY-MM-DD to check opening hours for (default today).")
    ] = None,
) -> BusinessInfoResult:
    """Opening hours, location and contact details of a business, with sources. Disputed facts (the
    business is still confirming them) are withheld rather than guessed."""
    deps = get_deps(ctx)
    with tool_span("get_business_info", business_id):
        profile = await live_profile(deps, business_id)
        wanted = list(dict.fromkeys(topics or DEFAULT_TOPICS))
        day_asked: date | None = None
        if on_date:
            try:
                day_asked = date.fromisoformat(on_date)
            except ValueError:
                raise ToolError("on_date must be a date like 2026-10-17") from None
        now_local = deps.clock.now().astimezone(profile.timezone)
        location = _hours_location(profile)
        # Spoken in this order: hand-off sentences, what each requested topic must say (in the order
        # asked), then nice-to-haves (the hours note, the summary) while they fit.
        priority: list[str] = []
        required: list[tuple[str, str]] = []
        secondary: list[tuple[str, str]] = []
        optional: list[str] = []
        facts: list[Fact] = []
        lines: list[CardLine] = []
        badges: list[str] = []
        hours: list[HoursLine] | None = None
        open_now: bool | None = None
        handoff: HandOff | None = None
        address: str | None = None
        phone: str | None = None
        name_fact = grounding.state(profile, "business#name")
        if name_fact.fact:
            facts.append(name_fact.fact)
        summary_text: str | None = None
        for topic in wanted:
            if topic == "summary":
                summary = grounding.state(profile, "business#summary_spoken")
                if summary.ok and isinstance(summary.value, str):
                    summary_text = summary.value
                    facts.append(summary.fact)  # type: ignore[arg-type]
            elif topic == "hours":
                section, hours, open_now, hand, hour_badges = _hours_section(
                    profile, location, now_local=now_local, on_date=day_asked
                )
                if hand is not None:
                    priority.extend(section.sentences)
                else:
                    required.extend(("hours", sentence) for sentence in section.sentences)
                secondary.extend(("hours", sentence) for sentence in section.detail or [])
                optional.extend(section.extra or [])
                facts += section.facts
                lines += section.lines
                badges += hour_badges
                handoff = handoff or hand
            elif topic == "location":
                address, location_sentences, location_facts = _location_section(profile, location)
                required.extend(("location", sentence) for sentence in location_sentences)
                facts += location_facts
                if address:
                    lines.append(CardLine(label="Address", value=address))
            elif topic == "contact":
                got = grounding.state(profile, "business#contact.phone")
                if got.ok and isinstance(got.value, str):
                    phone = got.value
                    required.append(("contact", f"You can call {profile.spoken_name} at {speech.phone(phone)}"))
                    facts.append(got.fact)  # type: ignore[arg-type]
                    lines.append(CardLine(label="Phone", value=speech.phone(phone)))
                else:
                    required.append(("contact", f"I don't have a phone number for {profile.spoken_name}"))
        offer = grounding.offer_sentence(profile) if handoff is not None else None
        if handoff is not None and not grounding.can_ask_owner(profile):
            handoff = None
        facts = grounding.dedupe_facts(facts)
        if summary_text is not None:
            # The summary leads only when it is all that was asked; otherwise it never displaces a
            # requested topic (the phone number someone asked for must not be cut for the blurb).
            if required or priority or secondary:
                optional.append(summary_text)
            else:
                required.insert(0, ("summary", summary_text))
        if {kind for kind, _sentence in required} <= {"hours"}:
            required, secondary = required + secondary, []  # only hours asked: the timetable is the answer
        speak = fit_speech(priority, [*required, *secondary], optional, tail=offer)
        result = BusinessInfoResult(
            speak=speak or speech.sentences(f"Here is what I know about {profile.spoken_name}"),
            facts=facts,
            card=Card(
                kind="business",
                title=profile.name,
                subtitle=None,
                lines=lines,
                badges=badges,
                citations=grounding.citations_for(profile, facts),
            ),
            handoff=handoff,
            name=profile.name,
            open_now=open_now,
            hours=hours,
            address=address,
            phone=phone,
        )
        outcome = "ok" if handoff is None else ("withheld" if handoff.reason == "disputed" else "handoff")
        return finish(result, profile, outcome=outcome)


_MORE_OFFERS: dict[str, str] = {
    "hours": "Want the opening hours too?",
    "location": "Want the address too?",
    "contact": "Want the phone number too?",
    "summary": "Want to hear more about them?",
}


def fit_speech(
    priority: list[str], required: list[tuple[str, str]], optional: list[str], *, tail: str | None
) -> str:
    """`priority`, then every `required` sentence that fits, then `optional` ones that fit, then `tail`.

    Sentences that do not fit are skipped (never silently cut mid-list for what follows): a later,
    shorter one may still fit. When a required sentence had to be dropped and there is no hand-off
    offer, the reply ends by offering it ("Want the phone number too?") instead of losing it silently.
    """

    def assemble(end: str | None) -> tuple[str, list[str]]:
        budget = speech.SPEAK_LIMIT - (len(end) + 1 if end else 0)
        kept: list[str] = []
        dropped: list[str] = []
        for kind, sentence in [*(("priority", p) for p in priority), *required, *(("optional", o) for o in optional)]:
            candidate = speech.sentences(*kept, sentence, limit=10_000)
            if len(candidate) <= budget:
                kept.append(sentence)
            elif kind not in ("priority", "optional"):
                dropped.append(kind)
        return speech.sentences(*kept, tail=end), dropped

    spoken, dropped = assemble(tail)
    if dropped and tail is None:
        spoken, _again = assemble(_MORE_OFFERS.get(dropped[0], "Want me to go on?"))
    return spoken


def _location_section(profile: Profile, location: JsonObj | None) -> tuple[str | None, list[str], list[Fact]]:
    if location is None or "id" not in location:
        return None, [f"I don't have an address for {profile.spoken_name}"], []
    base = f"locations/{location['id']}"
    facts: list[Fact] = []
    address_got = grounding.state(profile, f"{base}#address")
    area_got = grounding.state(profile, f"{base}#spoken_area")
    address = _address_text(address_got.value) if address_got.ok else None
    sentences: list[str] = []
    if area_got.ok and isinstance(area_got.value, str):
        locality = (address_got.value or {}).get("locality") if address_got.ok else None
        sentences.append(f"{profile.spoken_name} is {area_got.value}" + (f" in {locality}" if locality and locality not in area_got.value else ""))
        facts.append(area_got.fact)  # type: ignore[arg-type]
    elif address_got.ok and address_got.value:
        sentences.append(f"{profile.spoken_name} is at {address_got.value.get('line1')} in {address_got.value.get('locality')}")
    else:
        sentences.append(f"I don't have an address for {profile.spoken_name}")
    if address_got.fact:
        facts.append(address_got.fact)
    return address, sentences, facts
