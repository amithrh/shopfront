"""Booking tools: check_availability, book, cancel_booking.

`book` and `cancel_booking` use the MRTR resolver chain documented in `tools.confirm`:
plan -> (intake form if fields are missing) -> re-plan -> confirmation -> body commits.
Resolvers are side-effect free; the only write happens in the tool body after an accepted confirmation.

Tool body outline (`book`):
  1. plan has problems            -> CommitmentResult(status unavailable/not_offered/needs_info, alternatives)
  2. not `confirmed(confirmation)`-> CommitmentResult(status="not_confirmed")  (decline/cancel/"no")
  3. if plan.approval.required:
         record = await deps.tasks.ensure_task(task_id=deps.ids.task_id(key), refresh_ttl=True, ...)
  4. outcome = await deps.bookings.commit(profile, plan, task_id=record.task_id if approval else None)
     capacity race lost -> the unadopted task is left for an identical retry (TTL refreshed on reuse)
     or expiry; return unavailable with alternatives
  5. pending_owner -> deps.tasks.adopt(record) (handle for opted-in clients, else sync result with task_id);
     never for an anonymous replay, including a `Duplicate` from a concurrent identical request (redacted)
  6. return render.commitment_result(profile, outcome.commitment, replayed=outcome.replayed)
Steps 1-6 are `confirm.commit_flow`, shared with orders, quotes and ask_owner.

`cancel_booking` takes the booking's reference or the 8-digit booking code a confirmed or approved
booking speaks (`booking_id`), because a voice customer can only say the code back.
"""

from datetime import date
from typing import Annotated, Any

from pydantic import BaseModel, Field

from mcp.server.mcpserver import (
    AcceptedElicitation,
    Context,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp_tasks import InvalidTransition, TaskNotFound
from shopfront_server.domain.availability import MAX_SEARCH_DAYS
from shopfront_server.domain.planning import CommitPlan, SlotOption
from shopfront_server.errors import DisputedFact
from shopfront_server.ids import reference_prefix
from shopfront_server.profile.model import Profile
from shopfront_server.tools import grounding, render, speech
from shopfront_server.tools.catalog import TRANSACTIONAL, commit_item, resolve_item
from shopfront_server.tools.confirm import (
    SKIPPED_CONFIRMATION,
    ConfirmCommit,
    commit_flow,
    confirmation_for,
    confirmation_question,
    confirmed,
    intake_for,
    merge_intake,
)
from shopfront_server.tools.context import anonymous_nonce, finish, get_deps, live_profile, principal, tool_span
from shopfront_server.tools.discovery import BusinessId
from shopfront_server.tools.schemas import AvailabilityResult, Card, CardAction, CardLine, CommitmentResult

ItemArg = Annotated[str, Field(description="Service or item as the customer said it, or an item ref.", max_length=120)]
StartArg = Annotated[
    str,
    Field(description="Requested start, local ISO-8601 like 2026-10-17T10:00 (business timezone).", max_length=40),
]
PartySizeArg = Annotated[int | None, Field(description="Number of people (default 1).", ge=1, le=100)]
DetailsArg = Annotated[
    dict[str, Any] | None,
    Field(
        description=(
            "Answers to the business's questions keyed by field key (e.g. customer_name). After a "
            "needs_info result, answer with the keys listed in missing_fields."
        )
    ),
]
BookingDetailsArg = Annotated[
    dict[str, Any] | None,
    Field(
        description=(
            "Answers to the booking questions keyed by field key (e.g. customer_name), including option "
            "choices keyed by option group id (e.g. {'length': 'shoulder length or shorter'}). After a "
            "needs_info result, answer with the keys listed in missing_fields."
        )
    ),
]
BookingChoicesArg = Annotated[
    dict[str, str] | None,
    Field(description="Option choices keyed by option group, e.g. {'length': 'shoulder length or shorter'}."),
]
ModuleArg = Annotated[str | None, Field(description="Booking module id when a business has several.", max_length=64)]
AnyModuleArg = Annotated[
    str | None, Field(description="Module id when the business has several (rarely needed).", max_length=64)
]
ResourceArg = Annotated[str | None, Field(description="Preferred stylist/table/instructor, if any.", max_length=64)]

SPOKEN_SLOTS = 3


def parse_day(value: str | None, name: str) -> date | None:
    """`YYYY-MM-DD` or None; anything else is the caller's mistake (ToolError the model can correct)."""
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ToolError(f"{name} must be a local date like 2026-10-17") from None


def check_range(first: date | None, last: date | None, today: date) -> None:
    """Refuse ranges the engine would only clamp: `date_to` before the start, or a span longer than
    `MAX_SEARCH_DAYS` (ToolError, so the model asks for a shorter range instead of a silent cut)."""
    if last is None:
        return
    start = first or today
    if last < start:
        raise ToolError("date_to must not be before date_from")
    if (last - start).days >= MAX_SEARCH_DAYS:
        raise ToolError(f"Ask for at most {MAX_SEARCH_DAYS} days at a time (date_from to date_to)")


async def check_availability(
    business_id: BusinessId,
    item: ItemArg,
    ctx: Context,
    date_from: Annotated[str | None, Field(description="First local date YYYY-MM-DD (default today).", max_length=10)] = None,
    date_to: Annotated[
        str | None,
        Field(description=f"Last local date YYYY-MM-DD (default two weeks out; at most {MAX_SEARCH_DAYS} days).", max_length=10),
    ] = None,
    party_size: PartySizeArg = None,
    module_id: ModuleArg = None,
    resource: ResourceArg = None,
    limit: Annotated[int, Field(ge=1, le=12)] = 6,
) -> AvailabilityResult:
    """Open times (appointments, classes) or open days (custom orders, quote visits) for an item,
    computed from the business's published rules and current bookings."""
    deps = get_deps(ctx)
    with tool_span("check_availability", business_id):
        profile = await live_profile(deps, business_id)
        first, last = parse_day(date_from, "date_from"), parse_day(date_to, "date_to")
        check_range(first, last, deps.clock.now().astimezone(profile.timezone).date())
        lookup = resolve_item(profile, item, module_type=TRANSACTIONAL)
        if lookup.found is None:
            if lookup.candidates:
                names = [c.spoken_name for c in lookup.candidates]
                result = AvailabilityResult(
                    speak=speech.sentences(f"Did you mean {speech.join(names, conjunction='or')}?"),
                    facts=grounding.facts_for(profile, [f"{c.ref}#spoken_name" for c in lookup.candidates]),
                )
                return finish(result, profile, outcome="ambiguous")
            result = grounding.unknown_answer(
                profile,
                question=f"Do you offer {item}?",
                reason="not_offered",
                lead=f"I don't see that at {profile.spoken_name}.",
                result_type=AvailabilityResult,
            )
            return finish(result, profile, outcome="not_offered")
        entry = lookup.found
        modules = profile.transactional_modules_for(entry.ref)
        if module_id is not None:
            modules = [m for m in modules if m == module_id]
        if not modules:
            result = grounding.unknown_answer(
                profile,
                question=f"Can I book or order the {entry.spoken_name}?",
                reason="not_offered",
                lead=f"I can't book or order the {entry.spoken_name} through {profile.spoken_name}.",
                result_type=AvailabilityResult,
                item_ref=entry.ref,
            )
            return finish(result, profile, outcome="not_offered")
        chosen = modules[0]
        kind = profile.module_type(chosen)
        try:
            slots = await _open_slots(deps, profile, kind, entry.ref, chosen, first, last, party_size, resource, limit)
        except DisputedFact as disputed:
            # A rule the slots depend on (hours, closures, notice...) is being double-checked (L6).
            result = grounding.withheld_answer(
                profile,
                question=f"Do you have any openings for the {entry.spoken_name}?",
                fact_ref=disputed.fact_ref,
                question_id=disputed.question_id,
                result_type=AvailabilityResult,
                module_id=chosen,
                item_ref=entry.ref,
            )
            return finish(result, profile, outcome="withheld", module_id=chosen)
        today = deps.clock.now().astimezone(profile.timezone).date()
        names = render.resource_names(profile, chosen)
        views = [render.slot_view(profile, s, resource_names=names, today=today) for s in slots[:limit]]
        facts = grounding.facts_for(profile, [f"{entry.ref}#spoken_name", *(r for s in slots for r in s.fact_refs)])
        spoken = entry.spoken_name
        handoff = None
        if not views:
            handoff = (
                grounding.handoff(
                    profile, reason="needs_owner", question=f"Do you have any openings for the {spoken}?", module_id=chosen
                )
                if grounding.can_ask_owner(profile, chosen)
                else None
            )
            speak = speech.sentences(
                f"I couldn't find an open {'time' if kind == 'booking' else 'day'} for the {spoken} in that range",
                tail=grounding.offer_sentence(profile, chosen) if handoff else None,
            )
        else:
            phrase = render.spoken_slots(profile, slots[:SPOKEN_SLOTS], today=today, resource_names=names)
            noun = "open time" if views[0].kind == "time" else "open day"
            if len(views) == 1:
                speak = speech.sentences(f"The next {noun} for the {spoken} is {phrase}")
            else:
                speak = speech.sentences(f"The next {noun}s for the {spoken} are {phrase}")
        action_tool = "book" if kind == "booking" else ("place_custom_order" if kind == "custom_order" else "request_quote")
        card = Card(
            kind="slots",
            title=entry.name,
            subtitle=profile.name,
            lines=[CardLine(label=speech.capitalize(v.label_spoken), value=v.resource_spoken_name or "Open") for v in views],
            citations=grounding.citations_for(profile, facts),
            actions=[
                CardAction(
                    label=speech.capitalize(v.label_spoken),
                    tool=action_tool,
                    arguments=_slot_arguments(business_id, entry.ref, chosen, v.start, v.date, v.resource_id, action_tool),
                )
                for v in views[:SPOKEN_SLOTS]
            ],
        )
        result = AvailabilityResult(
            speak=speak, facts=facts, card=card, handoff=handoff, module_id=chosen, item_ref=entry.ref, slots=views
        )
        return finish(result, profile, outcome="ok" if views else "none_open", module_id=chosen)


async def _open_slots(
    deps: Any,
    profile: Profile,
    kind: str,
    item_ref: str,
    module_id: str,
    first: date | None,
    last: date | None,
    party_size: int | None,
    resource: str | None,
    limit: int,
) -> list[SlotOption]:
    """Open times (booking) or open days (custom orders, quote visits) from the module's service."""
    if kind == "booking":
        slots: list[SlotOption] = await deps.bookings.open_slots(
            profile,
            item=item_ref,
            module_id=module_id,
            date_from=first,
            date_to=last,
            party_size=party_size or 1,
            resource_id=resource,
            limit=limit,
        )
        return slots
    service = deps.orders if kind == "custom_order" else deps.quotes  # quote_request: visit days
    days: list[SlotOption] = await service.open_days(
        profile, item=item_ref, module_id=module_id, date_from=first, date_to=last, limit=limit
    )
    return days


def _slot_arguments(
    business_id: str, item_ref: str, module_id: str, start: str | None, day: str | None, resource: str | None, tool: str
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"business_id": business_id, "item": item_ref, "module_id": module_id}
    if tool == "book" and start:
        arguments["start"] = start[:16]  # local ISO without offset, as `book` expects
        if resource:
            arguments["resource"] = resource
    elif day:
        arguments["date"] = day
    return arguments


# ---------------------------------------------------------------------------- book: resolver chain


async def book_plan(
    business_id: str,
    item: str,
    start: str,
    party_size: int | None,
    details: dict[str, Any] | None,
    choices: dict[str, str] | None,
    module_id: str | None,
    resource: str | None,
    ctx: Context,
) -> CommitPlan:
    """Round-independent plan from the tool arguments alone (decides which intake fields are missing)."""
    deps = get_deps(ctx)
    profile = await live_profile(deps, business_id)
    return await deps.bookings.plan(
        profile,
        item=commit_item(profile, item, "booking"),
        start=start,
        party_size=party_size,
        intake=dict(details or {}),
        principal=principal(ctx),
        module_id=module_id,
        resource_id=resource,
        choices=dict(choices or {}),
        nonce=anonymous_nonce(ctx),
    )


async def book_intake(
    plan: Annotated[CommitPlan, Resolve(book_plan)], ctx: Context
) -> Elicit[BaseModel] | AcceptedElicitation[BaseModel]:
    return await intake_for(ctx, plan)


async def book_final_plan(
    business_id: str,
    item: str,
    start: str,
    party_size: int | None,
    details: dict[str, Any] | None,
    choices: dict[str, str] | None,
    module_id: str | None,
    resource: str | None,
    intake: Annotated[ElicitationResult[BaseModel], Resolve(book_intake)],
    ctx: Context,
) -> CommitPlan:
    """Re-plan with the intake answers merged into `details`."""
    deps = get_deps(ctx)
    profile = await live_profile(deps, business_id)
    return await deps.bookings.plan(
        profile,
        item=commit_item(profile, item, "booking"),
        start=start,
        party_size=party_size,
        intake=merge_intake(details, intake),
        principal=principal(ctx),
        module_id=module_id,
        resource_id=resource,
        choices=dict(choices or {}),
        nonce=anonymous_nonce(ctx),
    )


async def book_confirmation(
    plan: Annotated[CommitPlan, Resolve(book_final_plan)], ctx: Context
) -> Elicit[ConfirmCommit] | DeclinedElicitation:
    return await confirmation_for(ctx, plan, get_deps(ctx).bookings)


async def book(
    business_id: BusinessId,
    item: ItemArg,
    start: StartArg,
    ctx: Context,
    plan: Annotated[CommitPlan, Resolve(book_final_plan)],
    confirmation: Annotated[ElicitationResult[ConfirmCommit], Resolve(book_confirmation)],
    party_size: PartySizeArg = None,
    details: BookingDetailsArg = None,
    choices: BookingChoicesArg = None,
    module_id: ModuleArg = None,
    resource: ResourceArg = None,
) -> CommitmentResult:
    """Book an appointment, table or class. The customer is always asked to confirm the read-back before
    anything is booked; retries never double-book. If the business reviews bookings, the result is a
    pending request the owner approves (a task the host can poll)."""
    deps = get_deps(ctx)
    return await commit_flow(ctx, tool="book", service=deps.bookings, plan=plan, confirmation=confirmation, asked=item)


# ---------------------------------------------------------------------------- cancel_booking

async def booking_id(deps: Any, business_id: str, reference: str, caller: str) -> str:
    """The booking id `reference` names: itself, or the caller's booking whose spoken 8-digit code it is.

    A voice customer only ever hears the code (`ids.spoken_reference`), never the 28-character
    reference. The code is the id's first 25 bits, so it is looked up by key prefix within this
    business, among the caller's bookings whose results speak it (`render.BOOKING_CODE_STATUSES`) or
    that are already cancelled (so a retry replays); a live one wins over a cancelled one. Anything
    else (not a code, no match, several matches) is returned unchanged and falls through to the usual
    exact lookup, so it answers exactly like an unknown reference."""
    prefix = reference_prefix(reference)
    if prefix is None:
        return reference
    found = await deps.commitments.find_by_reference_prefix(business_id, prefix)
    mine = [c for c in found if c.kind == "booking" and c.principal == caller]
    live = [c for c in mine if c.status in render.BOOKING_CODE_STATUSES]
    gone = [c for c in mine if c.status == "cancelled"]
    for candidates in (live, gone):
        if len(candidates) == 1:
            return str(candidates[0].commitment_id)
        if candidates:
            break  # ambiguous: never guess between two bookings
    return reference


async def cancel_plan(business_id: str, reference: str, ctx: Context) -> CommitPlan:
    deps = get_deps(ctx)
    profile = await live_profile(deps, business_id)
    caller = principal(ctx)
    found = await booking_id(deps, business_id, reference, caller)
    return await deps.bookings.plan_cancel(profile, booking_id=found, principal=caller)


async def cancel_confirmation(
    plan: Annotated[CommitPlan, Resolve(cancel_plan)], reference: str, ctx: Context
) -> Elicit[ConfirmCommit] | DeclinedElicitation:
    """Read back what will be cancelled; skipped for a booking that is already cancelled (a retry)."""
    deps = get_deps(ctx)
    profile = await live_profile(deps, plan.business_id)
    found = await booking_id(deps, plan.business_id, reference, principal(ctx))
    existing = await deps.commitments.get(plan.business_id, found)
    if existing is not None and existing.status == "cancelled":
        return SKIPPED_CONFIRMATION
    return confirmation_question(profile, plan)


async def cancel_booking(
    business_id: BusinessId,
    reference: Annotated[
        str,
        Field(description="Booking reference, or the 8-digit booking code read out when it was booked.", max_length=64),
    ],
    ctx: Context,
    plan: Annotated[CommitPlan, Resolve(cancel_plan)],
    confirmation: Annotated[ElicitationResult[ConfirmCommit], Resolve(cancel_confirmation)],
) -> CommitmentResult:
    """Cancel a booking after reading back what will be cancelled and the cancellation policy. The
    customer may give the 8-digit booking code they were told when it was booked instead of the
    reference."""
    deps = get_deps(ctx)
    with tool_span("cancel_booking", business_id, module_id=plan.module_id):
        profile = await live_profile(deps, business_id)
        reference = await booking_id(deps, business_id, reference, principal(ctx))
        existing = await deps.commitments.get(business_id, reference)
        if existing is None or existing.kind != "booking" or existing.principal != principal(ctx):
            # Unknown, not a booking, or someone else's: indistinguishable on purpose.
            result = CommitmentResult(
                speak=speech.sentences("I couldn't find a booking with that reference"),
                card=grounding.notice_card("Booking not found"),
                status="unavailable",
            )
            return finish(result, profile, outcome="not_found")
        if existing.status == "cancelled":
            result = render.commitment_result(profile, existing, replayed=True)
            return finish(result, profile, outcome="replayed", commitment_id=existing.commitment_id, idempotent_replay=True)
        if not plan.ok:
            result = render.problem_result(profile, plan)
            return finish(result, profile, outcome=result.status, commitment_id=existing.commitment_id)
        if not confirmed(confirmation):
            result = render.not_confirmed_result(profile, plan, commitment=existing)
            return finish(result, profile, outcome="not_confirmed", commitment_id=existing.commitment_id)
        cancelled = await deps.bookings.cancel(profile, booking_id=reference, principal=principal(ctx))
        if cancelled.task_id:
            try:  # a booking still waiting for the owner: stop the owner task too
                await deps.task_manager.cancel(cancelled.task_id, "Cancelled by the customer")
            except (InvalidTransition, TaskNotFound):
                pass
        result = render.commitment_result(profile, cancelled)
        return finish(result, profile, outcome=cancelled.status, commitment_id=cancelled.commitment_id)
