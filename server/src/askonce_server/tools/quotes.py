"""Quote tools: check_service_area and request_quote (always a task answered by the owner)."""

import re
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
from askonce_server.domain.planning import CommitPlan
from askonce_server.domain.services.quote import service_area_locations
from askonce_server.errors import UnknownModule
from askonce_server.profile.model import Profile
from askonce_server.tools import grounding, speech
from askonce_server.tools.booking import AnyModuleArg, DetailsArg, ItemArg
from askonce_server.tools.catalog import commit_item
from askonce_server.tools.confirm import (
    ConfirmCommit,
    commit_flow,
    confirmation_for,
    intake_for,
    merge_intake,
)
from askonce_server.tools.context import anonymous_nonce, finish, get_deps, live_profile, principal, tool_span
from askonce_server.tools.discovery import BusinessId
from askonce_server.tools.schemas import Card, CardLine, CommitmentResult, ServiceAreaResult

_POSTAL_CODE = re.compile(r"^[A-Za-z0-9]{2,5}[ -]?[A-Za-z0-9]{2,5}$|^[A-Za-z0-9]{3,10}$")


def spoken_place(postal_code: str | None, locality: str | None) -> str:
    """How the reply names the customer's location without echoing free text as the business's words:
    a well-formed postal code as given, anything else 'that area' (SEC-6)."""
    if postal_code and _POSTAL_CODE.match(postal_code):
        return postal_code.upper()
    return "that area"


VisitDayArg = Annotated[str | None, Field(description="Preferred visit date, local YYYY-MM-DD.", max_length=10)]
VisitWindowArg = Annotated[
    str | None, Field(description="Preferred visit window id or name, e.g. 'weekday morning'.", max_length=64)
]


async def check_service_area(
    business_id: BusinessId,
    ctx: Context,
    postal_code: Annotated[str | None, Field(description="ZIP or postal code of the job.", max_length=16)] = None,
    locality: Annotated[str | None, Field(description="Town or neighbourhood of the job.", max_length=60)] = None,
    module_id: AnyModuleArg = None,
) -> ServiceAreaResult:
    """Whether the business serves a location (never asks for a full address)."""
    deps = get_deps(ctx)
    with tool_span("check_service_area", business_id):
        profile = await live_profile(deps, business_id)
        postal = (postal_code or "").strip() or None
        town = (locality or "").strip() or None
        if postal is None and town is None:
            result = ServiceAreaResult(
                speak=speech.sentences("What's the ZIP code or town where the work is?"),
                card=grounding.notice_card("Where is the job?"),
            )
            return finish(result, profile, outcome="needs_info")
        where = spoken_place(postal, town)
        try:
            check = deps.quotes.check_service_area(profile, postal_code=postal, locality=town, module_id=module_id)
        except UnknownModule:
            # A shop with no on-site jobs (a bakery, a salon) has no service area to check.
            result = grounding.unknown_answer(
                profile,
                question=f"Do you come to customers in {postal or town}?",
                reason="not_offered",
                lead=f"{profile.spoken_name} doesn't take on-site job requests through me, so there's no service area to check.",
                result_type=ServiceAreaResult,
            )
            return finish(result, profile, outcome="not_offered")
        stated = [grounding.state(profile, ref) for ref in check.fact_refs]
        disputed = next((s for s in stated if s.disputed), None)
        if disputed is not None:
            result = grounding.withheld_answer(
                profile,
                question=f"Do you serve {postal or town}?",
                fact_ref=disputed.text,
                question_id=disputed.statability.question_id,
                result_type=ServiceAreaResult,
                module_id=check.module_id,
            )
            return finish(result, profile, outcome="withheld", module_id=check.module_id)
        facts = grounding.dedupe_facts([s.fact for s in stated if s.fact is not None])
        if check.served is None and postal is None and _lists_postal_codes(profile, check.module_id):
            # "Denver", "Stapleton": a town the areas don't list, where the ZIP code decides.
            result = ServiceAreaResult(
                speak=speech.sentences(f"I can't tell from the name alone whether {profile.spoken_name} covers it. What's the ZIP code where the work is?"),
                card=grounding.notice_card("Which ZIP code?"),
                module_id=check.module_id,
            )
            return finish(result, profile, outcome="needs_info", module_id=check.module_id)
        if check.served is None:
            result = grounding.unknown_answer(
                profile,
                question=f"Do you serve {postal or town}?",
                lead=f"I can't tell from {profile.spoken_name}'s published service area whether they cover {where}.",
                module_id=check.module_id,
                result_type=ServiceAreaResult,
            )
            return finish(result, profile, outcome="handoff", module_id=check.module_id)
        matched = check.matched or where
        speak = (
            speech.sentences(f"Yes, {profile.spoken_name} serves {matched}")
            if check.served
            else speech.sentences(f"Sorry, {profile.spoken_name} doesn't serve {where}")
        )
        result = ServiceAreaResult(
            speak=speak,
            facts=facts,
            card=Card(
                kind="business",
                title=profile.name,
                subtitle="Service area",
                lines=[CardLine(label="Location", value=where), CardLine(label="Served", value="Yes" if check.served else "No")],
                badges=["In service area" if check.served else "Outside service area"],
                citations=grounding.citations_for(profile, facts),
            ),
            module_id=check.module_id,
            served=check.served,
        )
        return finish(result, profile, outcome="served" if check.served else "not_served", module_id=check.module_id)


def _lists_postal_codes(profile: Profile, module_id: str) -> bool:
    """Whether the module's service areas are (also) listed by postal code."""
    module = profile.module(module_id)
    return any((location.get("area") or {}).get("postal_codes") for location in service_area_locations(profile, module))


# ---------------------------------------------------------------------------- request_quote: resolver chain


async def _plan(
    business_id: str,
    item: str,
    date: str | None,
    visit_window: str | None,
    details: dict[str, Any] | None,
    module_id: str | None,
    ctx: Context,
) -> CommitPlan:
    deps = get_deps(ctx)
    profile = await live_profile(deps, business_id)
    return await deps.quotes.plan(
        profile,
        item=commit_item(profile, item, "quote_request"),
        intake=dict(details or {}),
        principal=principal(ctx),
        day=date,
        visit_window=visit_window,
        module_id=module_id,
        nonce=anonymous_nonce(ctx),
    )


async def quote_plan(
    business_id: str,
    item: str,
    date: str | None,
    visit_window: str | None,
    details: dict[str, Any] | None,
    module_id: str | None,
    ctx: Context,
) -> CommitPlan:
    """Round-independent plan from the tool arguments alone (decides which intake fields are missing)."""
    return await _plan(business_id, item, date, visit_window, details, module_id, ctx)


async def quote_intake(
    plan: Annotated[CommitPlan, Resolve(quote_plan)], ctx: Context
) -> Elicit[BaseModel] | AcceptedElicitation[BaseModel]:
    return await intake_for(ctx, plan)


async def quote_final_plan(
    business_id: str,
    item: str,
    date: str | None,
    visit_window: str | None,
    details: dict[str, Any] | None,
    module_id: str | None,
    intake: Annotated[ElicitationResult[BaseModel], Resolve(quote_intake)],
    ctx: Context,
) -> CommitPlan:
    """Re-plan with the intake answers merged into `details`."""
    return await _plan(business_id, item, date, visit_window, merge_intake(details, intake), module_id, ctx)


async def quote_confirmation(
    plan: Annotated[CommitPlan, Resolve(quote_final_plan)], ctx: Context
) -> Elicit[ConfirmCommit] | DeclinedElicitation:
    return await confirmation_for(ctx, plan, get_deps(ctx).quotes)


async def request_quote(
    business_id: BusinessId,
    item: ItemArg,
    ctx: Context,
    plan: Annotated[CommitPlan, Resolve(quote_final_plan)],
    confirmation: Annotated[ElicitationResult[ConfirmCommit], Resolve(quote_confirmation)],
    date: VisitDayArg = None,
    visit_window: VisitWindowArg = None,
    details: DetailsArg = None,
    module_id: AnyModuleArg = None,
) -> CommitmentResult:
    """Ask the business for a quote on a job. The assistant never estimates a price: the owner replies
    with a quote, which arrives as the result of a task the host can poll."""
    deps = get_deps(ctx)
    return await commit_flow(ctx, tool="request_quote", service=deps.quotes, plan=plan, confirmation=confirmation, asked=item)
