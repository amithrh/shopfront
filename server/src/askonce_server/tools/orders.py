"""Custom-order tool: place_custom_order (resolver chain as in `tools.booking`).

plan -> (intake form if required fields are missing) -> re-plan -> confirmation -> `confirm.commit_flow`.
Orders that match the owner's approval rules (large, special diets) become a pending request and a
task; others are confirmed straight away.
"""

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
from askonce_server.tools.booking import AnyModuleArg, DetailsArg, ItemArg
from askonce_server.tools import choices as item_choices
from askonce_server.tools.confirm import (
    ConfirmCommit,
    commit_flow,
    confirmation_for,
    intake_for,
    merge_intake,
)
from askonce_server.tools.context import anonymous_nonce, get_deps, live_profile, principal
from askonce_server.tools.discovery import BusinessId
from askonce_server.tools.schemas import CommitmentResult

ChoicesArg = Annotated[
    dict[str, str] | None,
    Field(description="Option choices keyed by option, e.g. {'size': '10 inch', 'sponge': 'gluten-free vanilla'}."),
]
DayArg = Annotated[str | None, Field(description="Pickup or delivery date, local YYYY-MM-DD.", max_length=10)]


async def _plan(
    business_id: str,
    item: str,
    choices: dict[str, str] | None,
    date: str | None,
    details: dict[str, Any] | None,
    module_id: str | None,
    ctx: Context,
) -> CommitPlan:
    deps = get_deps(ctx)
    profile = await live_profile(deps, business_id)
    # Choices named in the item phrase ("half sheet carrot cake") and dietary needs ("vegan cake").
    request = item_choices.request_for(profile, item, "custom_order", choices, details)
    plan = await deps.orders.plan(
        profile,
        item=request.item,
        choices=request.choices,
        day=date,
        intake=dict(details or {}),
        principal=principal(ctx),
        module_id=module_id,
        nonce=anonymous_nonce(ctx),
    )
    return item_choices.apply(plan, request)


async def order_plan(
    business_id: str,
    item: str,
    choices: dict[str, str] | None,
    date: str | None,
    details: dict[str, Any] | None,
    module_id: str | None,
    ctx: Context,
) -> CommitPlan:
    """Round-independent plan from the tool arguments alone (decides which intake fields are missing)."""
    return await _plan(business_id, item, choices, date, details, module_id, ctx)


async def order_intake(
    plan: Annotated[CommitPlan, Resolve(order_plan)], ctx: Context
) -> Elicit[BaseModel] | AcceptedElicitation[BaseModel]:
    return await intake_for(ctx, plan)


async def order_final_plan(
    business_id: str,
    item: str,
    choices: dict[str, str] | None,
    date: str | None,
    details: dict[str, Any] | None,
    module_id: str | None,
    intake: Annotated[ElicitationResult[BaseModel], Resolve(order_intake)],
    ctx: Context,
) -> CommitPlan:
    """Re-plan with the intake answers merged into `details`."""
    return await _plan(business_id, item, choices, date, merge_intake(details, intake), module_id, ctx)


async def order_confirmation(
    plan: Annotated[CommitPlan, Resolve(order_final_plan)], ctx: Context
) -> Elicit[ConfirmCommit] | DeclinedElicitation:
    return await confirmation_for(ctx, plan, get_deps(ctx).orders)


async def place_custom_order(
    business_id: BusinessId,
    item: ItemArg,
    ctx: Context,
    plan: Annotated[CommitPlan, Resolve(order_final_plan)],
    confirmation: Annotated[ElicitationResult[ConfirmCommit], Resolve(order_confirmation)],
    choices: ChoicesArg = None,
    date: DayArg = None,
    details: DetailsArg = None,
    module_id: AnyModuleArg = None,
) -> CommitmentResult:
    """Order a made-to-order item (e.g. a celebration cake). The customer confirms a read-back first.
    Orders matching the owner's rules (large, special diets) become a request the owner approves: the
    result is a task the host can poll, and the owner's decision arrives as the task result."""
    deps = get_deps(ctx)
    return await commit_flow(
        ctx, tool="place_custom_order", service=deps.orders, plan=plan, confirmation=confirmation, asked=item
    )
