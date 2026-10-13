"""Owner hand-off tools: ask_owner (unknown facts become a task), get_task (polling fallback) and
reply_to_owner (answering the owner's question without the tasks extension).

ask_owner needs no confirmation: the customer already accepted the hand-off offer ("Want me to ask
Rosa?"). It always becomes a task. get_task serves clients without the tasks extension: it returns the
task status, the commitment behind it and, once completed, the same `CommitmentResult` the task would
have delivered. When the owner asked a question (`input_required`), reply_to_owner delivers the
customer's answer exactly as `tasks/update` would, so the sync fallback (the primary contract for
voice hosts) is never a dead end. The reply is folded into the request at once
(`OwnerService.absorb_customer_answers`), and get_task (read-only) renders replies that are on the
task but not yet stored the same way, so neither repeats a question the customer already answered.
The task card's Answer action declares a text `input`, so an MCP Apps card collects the answer before
it calls reply_to_owner. Unknown, expired and foreign task ids are indistinguishable
("Unknown or expired task").
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp_tasks import InvalidInput, InvalidTransition, TaskNotFound, TaskRecord
from shopfront_server.deps import ShopfrontDeps
from shopfront_server.domain.intake import clean_text
from shopfront_server.domain.models import OPEN_STATUSES
from shopfront_server.domain.services.owner import MAX_ANSWER_LENGTH, approver_name, with_customer_answers
from shopfront_server.profile.factref import FACT_REF_PATTERN
from shopfront_server.tools import grounding, render, speech
from shopfront_server.tools.confirm import commit_flow
from shopfront_server.tools.context import anonymous_nonce, finish, get_deps, live_profile, principal, tool_span
from shopfront_server.tools.discovery import BusinessId
from shopfront_server.tools.schemas import (
    Card,
    CardAction,
    CardInput,
    CardLine,
    CommitmentResult,
    Fact,
    TaskStatusResult,
)

logger = logging.getLogger("shopfront.tools")

UNKNOWN_TASK = "Unknown or expired task"

_TASK_SPEECH = {
    "working": "Still waiting for an answer.",
    "input_required": "The business needs a bit more information.",
    "completed": "The request is finished.",
    "failed": "Sorry, something went wrong with that request.",
    "cancelled": "That request was cancelled.",
}


async def ask_owner(
    business_id: BusinessId,
    question: Annotated[str, Field(description="The customer's question for the owner, in plain words.", max_length=300)],
    ctx: Context,
    fact_ref: Annotated[
        str | None, Field(description="The fact_ref from a hand-off offer, if the question came from one.", max_length=200)
    ] = None,
) -> CommitmentResult:
    """Ask the business owner something the assistant could not answer. Use after the customer accepts a
    hand-off offer. The answer arrives later as the result of a task (or via get_task)."""
    deps = get_deps(ctx)
    profile = await live_profile(deps, business_id)
    text = " ".join(question.split())
    if not text:
        raise ToolError("question must not be empty")
    ref = fact_ref if fact_ref and FACT_REF_PATTERN.match(fact_ref) else None
    # Unauthenticated callers all share one principal: a per-call nonce keeps strangers' identical
    # questions apart (a retry then asks twice, which is harmless; merging them leaks the answer).
    plan = await deps.owner.plan_question(
        profile, question=text, principal=principal(ctx), fact_ref=ref, nonce=anonymous_nonce(ctx)
    )
    return await commit_flow(ctx, tool="ask_owner", service=deps.owner, plan=plan, confirmation=None, asked=text)


async def get_task(
    task_id: Annotated[str, Field(description="task_id from an earlier result.", max_length=64)],
    ctx: Context,
) -> TaskStatusResult:
    """Status of an owner-approval task, for hosts that cannot poll MCP tasks directly. Includes the
    final result (approved, declined, quote, answer...) once the owner has replied. When the owner
    asked a question, pass the customer's answer to reply_to_owner."""
    deps = get_deps(ctx)
    with tool_span("get_task", task_id=task_id):
        record = await _load_task(deps, task_id, principal(ctx))
        return await _status_result(deps, record)


async def reply_to_owner(
    task_id: Annotated[str, Field(description="task_id of the request whose owner asked a question.", max_length=64)],
    answer: Annotated[
        str, Field(description="The customer's answer to the owner's question, in plain words.", max_length=MAX_ANSWER_LENGTH)
    ],
    ctx: Context,
) -> TaskStatusResult:
    """Answer the business owner's question about a pending request (when get_task reports that the owner
    needs more information). The answer goes to the owner, and the request goes back to waiting for them."""
    deps = get_deps(ctx)
    caller = principal(ctx)
    with tool_span("reply_to_owner", task_id=task_id):
        record = await _load_task(deps, task_id, caller)
        text = clean_text(answer)
        if not text:
            raise ToolError("answer must not be empty")
        pending = record.input_requests or {}
        if record.status != "input_required" or not pending:
            raise ToolError("There is no question waiting for an answer on that request.")
        responses = {key: {"action": "accept", "content": {"answer": text}} for key in pending}
        try:
            updated = await deps.task_manager.submit_input(task_id, caller, responses)
        except TaskNotFound:
            raise ToolError(UNKNOWN_TASK) from None
        except InvalidInput as exc:
            raise ToolError(f"That answer could not be sent: {exc}") from None
        except InvalidTransition:
            updated = await _load_task(deps, task_id, caller)  # the owner decided meanwhile: show that
        else:
            await _absorb(deps, updated)
        return await _status_result(deps, updated, replied=updated.status != "input_required")


async def _absorb(deps: ShopfrontDeps, record: TaskRecord) -> None:
    """Store the reply on the request now (the owner's question stops showing, and the reply survives
    an expiry before the owner looks). The reply itself is already delivered, so a failure here is
    only logged: the inbox, `decide` and expiry fold it again."""
    try:
        await deps.owner.absorb_customer_answers(record)
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("could not fold the customer's reply into the request")


async def _load_task(deps: ShopfrontDeps, task_id: str, caller: str) -> TaskRecord:
    try:
        return await deps.task_manager.get(task_id, principal=caller)
    except TaskNotFound:
        raise ToolError(UNKNOWN_TASK) from None


async def _status_result(deps: ShopfrontDeps, record: TaskRecord, *, replied: bool = False) -> TaskStatusResult:
    """The `TaskStatusResult` for a task record (shared by get_task and reply_to_owner)."""
    task_id = record.task_id
    commitment = await deps.commitments.get_by_task(task_id)
    if commitment is not None:
        # A reply already on the task but not stored on the request yet (tasks/update, or an index
        # read that lags the fold) is shown as stored: the question is answered, the owner has it.
        commitment = with_customer_answers(commitment, record, deps.clock.now()) or commitment
    profile = await deps.profiles.get(commitment.business_id) if commitment is not None else None
    final: dict[str, Any] | None = None
    if record.status == "completed" and record.result:
        structured = record.result.get("structuredContent")
        final = dict(structured) if isinstance(structured, dict) else None
    view = render.commitment_view(profile, commitment) if commitment is not None and profile is not None else None
    # A task that ended without the owner (customer cancel, failure) must not be described by a
    # commitment that still looks open: say what happened to the task instead.
    stale = record.status in ("cancelled", "failed") and commitment is not None and commitment.status in OPEN_STATUSES
    who = approver_name(profile, commitment.module_id) if profile is not None and commitment is not None else None
    if final is not None and isinstance(final.get("speak"), str):
        speak = final["speak"]
    elif replied and who:
        speak = speech.sentences(f"Thanks, I've passed that on to {who}", "I'll let you know what they decide")
    elif commitment is not None and profile is not None and not stale:
        speak = render.commitment_result(profile, commitment).speak
        if record.status == "input_required":
            speak = speech.sentences(speak, "Tell me your answer and I'll pass it on")
    else:
        speak = speech.sentences(record.status_message or _TASK_SPEECH.get(record.status, "Still working on it."))
    facts: list[Fact] = []
    if final is not None:
        facts = [Fact.model_validate(f) for f in final.get("facts") or () if isinstance(f, dict)]
    lines = [CardLine(label="Status", value=render.STATUS_LABELS.get(view.status, view.status) if view else record.status)]
    if record.status_message:
        lines.append(CardLine(label="Update", value=record.status_message))
    if view is not None and view.when_spoken:
        lines.append(CardLine(label="When", value=view.when_spoken))
    actions: list[CardAction] = []
    if record.status == "input_required":
        actions.append(
            CardAction(
                label="Answer",
                tool="reply_to_owner",
                arguments={"task_id": task_id},
                input=CardInput(key="answer", label="Your answer", max_length=MAX_ANSWER_LENGTH),
            )
        )
    if record.status in ("working", "input_required"):
        actions.append(CardAction(label="Check again", tool="get_task", arguments={"task_id": task_id}))
    result = TaskStatusResult(
        speak=speak,
        facts=facts,
        card=Card(
            kind="task",
            title=speech.capitalize(view.item_name or "Your request") if view else "Your request",
            subtitle=profile.name if profile is not None else None,
            lines=lines,
            badges=[record.status.replace("_", " ")],
            citations=grounding.citations_for(profile, facts) if profile is not None else [],
            actions=actions,
        ),
        task_id=task_id,
        task_status=record.status,
        status_message=record.status_message,
        commitment=view,
        result=final,
    )
    return finish(result, profile, outcome=record.status, task_id=task_id)
