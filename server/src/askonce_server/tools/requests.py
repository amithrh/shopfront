"""Alexa+ compatibility layer: check_request_status and cancel_request, two plain tools that work in any
MCP client.

Alexa+'s documented client sends a classic `initialize` and never mentions the MCP Tasks extension, so a
customer there cannot `tasks/get` or `tasks/cancel` the request a tool started. These tools are the same
flows as plain tool calls:

  * check_request_status = `get_task` (`owner.task_status_result`), plus requests that have no task
    (a booking confirmed on the spot) or whose task has expired;
  * cancel_request = `tasks/cancel` plus the extension's cancel hook (`OwnerService.on_task_cancelled`)
    for a request still waiting for the owner, and the `cancel_booking` flow (`BookingService.plan_cancel`
    and `cancel`) for a booking that is already confirmed or approved.

Both accept what the customer actually has (`locate`):
  * a task id (`t_...`): needs nothing else;
  * a request reference (`c_...`) or the 8-digit booking code a confirmed booking speaks: these are
    per business, so they need `business_id` too (`booking.booking_id` resolves the code).

Security is the existing rule: without auth every caller is the anonymous principal, so the id or the
code is the capability. A task must belong to the caller (`TaskManager.get(principal=...)`), a
commitment's `principal` must be the caller's, and every miss (unknown, expired, someone else's, not a
code) answers identically ("I couldn't find a request with that reference"). The booking code is the
same 25-bit capability `cancel_booking` already accepts and reads back, so it reveals nothing new; it
still only names the caller's own confirmed or approved bookings.

Confirmation. `cancel_request` reads back what it will cancel before it writes anything:
  * a client that declared form elicitation is asked through the usual MRTR resolver (`guarded_elicit`);
  * any other client (a voice host that cannot answer a form mid-call) gets the read-back as `speak`
    with a `confirm_token`, and calls again with that token once the customer says yes. The token is a
    keyed digest of (caller, request): stateless, unguessable, valid only for that caller and request,
    so the host cannot skip the read-back by inventing one.

`_meta.ui.invoking` / `invoked` (short "Checking your request..." strings) are NOT set: the MCP Apps
tool meta (`McpUiToolMeta`, ext-apps 2.0.3) defines only `resourceUri` and `visibility`, and the SDK's
`Apps.tool` owns `_meta["ui"]` and refuses extra keys there. See docs/friction-log.md.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Annotated, Any

import anyio
from pydantic import Field

from mcp.server.mcpserver import AcceptedElicitation, Context, DeclinedElicitation, Elicit, ElicitationResult, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp_tasks import ACTIVE, InvalidTransition, TaskNotFound, TaskRecord
from askonce_server.deps import AskOnceDeps
from askonce_server.domain.models import Commitment
from askonce_server.domain.services.owner import approver_name
from askonce_server.errors import InvalidState, NotFound
from askonce_server.ids import reference_prefix
from askonce_server.profile.model import Profile
from askonce_server.tools import grounding, render, speech
from askonce_server.tools.booking import booking_id
from askonce_server.tools.confirm import (
    SAFE_CONFIRMATION,
    SKIPPED_CONFIRMATION,
    ConfirmCommit,
    confirmation_question,
    confirmed,
    guarded_elicit,
    read_back_message,
)
from askonce_server.tools.context import finish, get_deps, live_profile, principal, tool_span
from askonce_server.tools.owner import task_status_result
from askonce_server.tools.schemas import CancelRequestResult, RequestStatusResult

logger = logging.getLogger("askonce.tools")

NOT_FOUND = "I couldn't find a request with that reference"
NEEDS_BUSINESS = (
    "A request reference or booking code belongs to one business: pass its business_id too "
    "(a task_id needs nothing else)."
)
CONFIRM_TOKEN_BYTES = 12
"""Digest bytes in a confirm token (96 bits, base32 -> 20 characters)."""

RequestArg = Annotated[
    str,
    Field(
        description=(
            "What the customer has: the task_id from an earlier result, the request reference, or the "
            "8-digit booking code read out when a booking was confirmed."
        ),
        max_length=64,
    ),
]
OptionalBusinessArg = Annotated[
    str | None,
    Field(description="business_id of the business, needed for a reference or booking code (not for a task_id).", max_length=80),
]
ConfirmTokenArg = Annotated[
    str | None,
    Field(
        description=(
            "confirm_token from an earlier cancel_request result. Pass it only after reading that result's "
            "speak to the customer and hearing yes."
        ),
        max_length=64,
    ),
]


# ---------------------------------------------------------------------------- locating a request


@dataclass(frozen=True)
class Located:
    """The caller's request: its commitment (when one is stored) and its live task (when it has one)."""

    commitment: Commitment | None
    task: TaskRecord | None

    @property
    def key(self) -> str:
        """The request's stable id (what a confirm token is bound to)."""
        if self.commitment is not None:
            return self.commitment.commitment_id
        assert self.task is not None
        return self.task.task_id

    @property
    def waiting(self) -> bool:
        """True while the owner has not decided: the task is still active."""
        return self.task is not None and self.task.status in ACTIVE


async def _live_task(deps: AskOnceDeps, task_id: str, caller: str) -> TaskRecord | None:
    try:
        return await deps.task_manager.get(task_id, principal=caller)
    except TaskNotFound:
        return None


async def _commitment_of_task(deps: AskOnceDeps, record: TaskRecord, caller: str) -> Commitment | None:
    """The commitment behind `record`: by its server-set labels (a consistent read), else the task index."""
    business_id, commitment_id = record.labels.get("business_id"), record.labels.get("commitment_id")
    found: Commitment | None = None
    if business_id and commitment_id:
        found = await deps.commitments.get(business_id, commitment_id)
    if found is None:
        found = await deps.commitments.get_by_task(record.task_id)
    if found is None or found.principal != caller or found.task_id != record.task_id:
        return None  # defence in depth: the task already proved the caller, the commitment must agree
    return found


async def locate(deps: AskOnceDeps, said: str, business_id: str | None, caller: str) -> Located | None:
    """The caller's request named by `said` (task id, reference or booking code), or None.

    Unknown, expired and foreign requests are all None. A reference or a code without `business_id`
    raises `ToolError(NEEDS_BUSINESS)` (an argument problem the host can fix)."""
    text = said.strip()
    if text.startswith("t_"):
        record = await _live_task(deps, text, caller)
        if record is None:
            return None
        return Located(commitment=await _commitment_of_task(deps, record, caller), task=record)
    if business_id is None:
        if text.startswith("c_") or reference_prefix(text) is not None:
            raise ToolError(NEEDS_BUSINESS)
        return None
    await live_profile(deps, business_id)  # unknown and non-live businesses: "Unknown business"
    commitment = await deps.commitments.get(business_id, await booking_id(deps, business_id, text, caller))
    if commitment is None or commitment.principal != caller:
        return None  # unknown or someone else's: indistinguishable on purpose
    record = await _live_task(deps, commitment.task_id, caller) if commitment.task_id else None
    return Located(commitment=commitment, task=record)


def _not_found(result_type: type[Any], **fields: Any) -> Any:
    return result_type(speak=speech.sentences(NOT_FOUND), card=grounding.notice_card("Request not found"), **fields)


# ---------------------------------------------------------------------------- check_request_status


async def check_request_status(
    request_id_or_code: RequestArg,
    ctx: Context,
    business_id: OptionalBusinessArg = None,
) -> RequestStatusResult:
    """Status of a request the customer made earlier (a booking, custom order, quote request or question
    for the owner), for any client: no MCP tasks support needed. Accepts the task_id, the request
    reference, or the 8-digit booking code (those two with business_id). Includes the owner's final
    reply once there is one; when the owner asked a question, pass the customer's answer to
    reply_to_owner."""
    deps = get_deps(ctx)
    caller = principal(ctx)
    with tool_span("check_request_status", business_id):
        found = await locate(deps, request_id_or_code, business_id, caller)
        if found is None:
            return finish(_not_found(RequestStatusResult, found=False), None, outcome="not_found")
        commitment = found.commitment
        if found.task is not None:
            status = await task_status_result(deps, found.task)
            result = RequestStatusResult.model_validate(
                {
                    **status.model_dump(),
                    "found": True,
                    "status": commitment.status if commitment is not None else None,
                    "reference": commitment.commitment_id if commitment is not None else None,
                }
            )
            return result
        assert commitment is not None  # `locate` returns a commitment or a task, never neither
        profile = await deps.profiles.get(commitment.business_id)
        if profile is None:
            return finish(_not_found(RequestStatusResult, found=False), None, outcome="not_found")
        # The caller named the request by its id or code, so it may hear the booking code again.
        shown = render.commitment_result(profile, commitment)
        result = RequestStatusResult(
            speak=shown.speak,
            facts=shown.facts,
            card=shown.card,
            found=True,
            status=commitment.status,
            reference=commitment.commitment_id,
            task_id=None,
            commitment=shown.commitment,
        )
        return finish(result, profile, outcome=commitment.status, commitment_id=commitment.commitment_id)


# ---------------------------------------------------------------------------- cancel_request


def make_confirm_token(deps: AskOnceDeps, caller: str, found: Located) -> str:
    """Stateless proof that this caller was read back the cancellation of this request."""
    return deps.ids.derive("k", f"cancel_request|{caller}|{found.key}", nbytes=CONFIRM_TOKEN_BYTES)


def _token_ok(deps: AskOnceDeps, caller: str, found: Located, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(str(token), make_confirm_token(deps, caller, found))


def can_elicit(ctx: Context[Any, Any]) -> bool:
    """True if the client declared form elicitation (the SDK's own test: bare `elicitation: {}` counts)."""
    try:
        caps = ctx.client_capabilities
    except Exception:  # noqa: BLE001 - no session: treat as a plain client
        return False
    elicitation = caps.elicitation if caps is not None else None
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


def _cancellable_booking(found: Located) -> bool:
    c = found.commitment
    return not found.waiting and c is not None and c.kind == "booking" and c.status in render.BOOKING_CODE_STATUSES


def _withdraw_message(profile: Profile, found: Located) -> str:
    """The read-back for withdrawing a request the owner has not decided yet."""
    commitment = found.commitment
    who = approver_name(profile, commitment.module_id if commitment is not None else None)
    if commitment is None or commitment.kind == "owner_question":
        what = f"your question to {who}"
    else:
        view = render.commitment_view(profile, commitment)
        what = f"your request for {view.item_name}" if view.item_name else "your request"
        if view.when_spoken:
            what = f"{what}, {view.when_spoken}"
    return f"Cancel {what}? {who} hasn't decided yet. Shall I cancel it?"


async def _read_back(deps: AskOnceDeps, profile: Profile, found: Located, caller: str) -> str | None:
    """What cancel_request reads back before cancelling `found`, or None when nothing is cancellable."""
    if found.waiting:
        return _withdraw_message(profile, found)
    if _cancellable_booking(found):
        assert found.commitment is not None
        plan = await deps.bookings.plan_cancel(profile, booking_id=found.commitment.commitment_id, principal=caller)
        return read_back_message(profile, plan) if plan.ok else None
    return None


async def _profile_of(deps: AskOnceDeps, found: Located) -> Profile | None:
    if found.commitment is not None:
        business_id: str | None = found.commitment.business_id
    else:
        business_id = found.task.labels.get("business_id") if found.task is not None else None
    if not business_id:
        return None
    try:
        return await live_profile(deps, business_id)
    except ToolError:
        return None


async def cancel_request_confirmation(
    request_id_or_code: str, ctx: Context, business_id: str | None = None, confirm_token: str | None = None
) -> Elicit[ConfirmCommit] | DeclinedElicitation | AcceptedElicitation[ConfirmCommit]:
    """MRTR step: a valid `confirm_token` is the customer's earlier yes; otherwise a client that can
    answer a form is asked the read-back; any other client gets it in the result (the body)."""
    deps = get_deps(ctx)
    caller = principal(ctx)
    found = await locate(deps, request_id_or_code, business_id, caller)
    if found is None:
        return SKIPPED_CONFIRMATION
    if _token_ok(deps, caller, found, confirm_token):
        return AcceptedElicitation(action="accept", data=ConfirmCommit(confirm=True))
    profile = await _profile_of(deps, found)
    if profile is None or not can_elicit(ctx):
        return SKIPPED_CONFIRMATION
    if _cancellable_booking(found):
        assert found.commitment is not None
        plan = await deps.bookings.plan_cancel(profile, booking_id=found.commitment.commitment_id, principal=caller)
        return confirmation_question(profile, plan)
    if found.waiting:
        asked = guarded_elicit(
            profile,
            _withdraw_message(profile, found),
            ConfirmCommit,
            ("Shall I cancel this request?", ConfirmCommit),
            (SAFE_CONFIRMATION, ConfirmCommit),
        )
        return asked if asked is not None else SKIPPED_CONFIRMATION
    return SKIPPED_CONFIRMATION


def _as_cancel_result(result: Any, **extra: Any) -> CancelRequestResult:
    return CancelRequestResult.model_validate({**result.model_dump(), **extra})


async def _withdraw_task(deps: AskOnceDeps, found: Located, caller: str) -> Located:
    """`tasks/cancel` + its hook, as the tasks extension runs them; the request as stored afterwards."""
    assert found.task is not None
    try:
        cancelled = await deps.task_manager.cancel(found.task.task_id, "Cancelled by the customer")
    except (InvalidTransition, TaskNotFound):
        cancelled = None  # the owner decided (or it expired) meanwhile: report what happened instead
    if cancelled is not None:
        with anyio.CancelScope(shield=True):
            try:
                await deps.owner.on_task_cancelled(cancelled)
            except Exception:  # noqa: BLE001 - the cancellation itself already happened; the inbox closes it later
                logger.exception("cancel_request: withdrawing the request failed")
    record = await _live_task(deps, found.task.task_id, caller)
    commitment = found.commitment
    if commitment is not None:
        commitment = await deps.commitments.get(commitment.business_id, commitment.commitment_id) or commitment
    return Located(commitment=commitment, task=record)


_SETTLED = {
    "confirmed": "already been confirmed",
    "approved": "already been approved",
    "declined": "already been declined",
    "expired": "expired",
}
"""How `_settled_result` says a request the owner (or time) already settled; other outcomes are replies."""


def _settled_result(profile: Profile, found: Located) -> CancelRequestResult:
    """Nothing to cancel any more: already cancelled (a retry), or decided, answered or expired."""
    commitment = found.commitment
    if commitment is None:  # a task without a stored request: say what happened to the task
        status = found.task.status if found.task is not None else "cancelled"
        result = CancelRequestResult(
            speak=speech.sentences("That request was cancelled" if status == "cancelled" else "That request is already finished"),
            card=grounding.notice_card("Your request"),
            status="cancelled" if status == "cancelled" else "unavailable",
            task_id=found.task.task_id if found.task is not None else None,
            replayed=status == "cancelled",
        )
        return finish(result, profile, outcome=status)
    shown = render.commitment_result(profile, commitment, replayed=commitment.status == "cancelled")
    if commitment.status != "cancelled":
        who = approver_name(profile, commitment.module_id)
        state = _SETTLED.get(commitment.status, f"already been answered by {who}")
        speak = speech.sentences(
            f"That request has {state}, so I can't cancel it here", f"To change it, please contact {profile.spoken_name}"
        )
        shown = shown.model_copy(update={"speak": speak})
    return finish(_as_cancel_result(shown), profile, outcome=f"settled_{commitment.status}", commitment_id=commitment.commitment_id)


async def cancel_request(
    request_id_or_code: RequestArg,
    ctx: Context,
    confirmation: Annotated[ElicitationResult[ConfirmCommit], Resolve(cancel_request_confirmation)],
    business_id: OptionalBusinessArg = None,
    confirm_token: ConfirmTokenArg = None,
) -> CancelRequestResult:
    """Cancel a request the customer made earlier, for any client: withdraws a request still waiting for
    the owner, or cancels a confirmed booking. Accepts the task_id, the request reference, or the 8-digit
    booking code (those two with business_id). Nothing is cancelled before the customer confirms: if the
    result has a confirm_token, read its speak (a yes/no question) to the customer and, only if they say
    yes, call cancel_request again with the same arguments plus that confirm_token."""
    deps = get_deps(ctx)
    caller = principal(ctx)
    with tool_span("cancel_request", business_id):
        found = await locate(deps, request_id_or_code, business_id, caller)
        profile = await _profile_of(deps, found) if found is not None else None
        if found is None or profile is None:
            return finish(_not_found(CancelRequestResult, status="unavailable"), None, outcome="not_found")
        if not found.waiting and not _cancellable_booking(found):
            return _settled_result(profile, found)
        if not confirmed(confirmation):
            question = await _read_back(deps, profile, found, caller)
            if question is None:  # e.g. a booking whose start has passed: say why, write nothing
                assert found.commitment is not None
                plan = await deps.bookings.plan_cancel(profile, booking_id=found.commitment.commitment_id, principal=caller)
                return finish(_as_cancel_result(render.problem_result(profile, plan)), profile, outcome="not_cancellable")
            if can_elicit(ctx):  # the customer was asked and said no (or dismissed the form)
                result = CancelRequestResult(
                    speak=speech.sentences("Okay, your request stays as it is"),
                    card=grounding.notice_card("Nothing cancelled"),
                    status="not_confirmed",
                    reference=found.commitment.commitment_id if found.commitment is not None else None,
                )
                return finish(result, profile, outcome="not_confirmed")
            parts = speech.split_sentences(question) or [question]
            result = CancelRequestResult(
                speak=speech.sentences(*parts[:-1], tail=parts[-1]),
                card=grounding.notice_card("Cancel this request?", [("Read-back", question)]),
                status="not_confirmed",
                reference=found.commitment.commitment_id if found.commitment is not None else None,
                confirm_token=make_confirm_token(deps, caller, found),
            )
            return finish(result, profile, outcome="needs_confirmation")
        if found.waiting:
            after = await _withdraw_task(deps, found, caller)
            if after.commitment is None:
                return _settled_result(profile, after)
            shown = render.commitment_result(profile, after.commitment)
            return finish(_as_cancel_result(shown), profile, outcome=after.commitment.status, commitment_id=after.commitment.commitment_id)
        assert found.commitment is not None
        try:
            cancelled = await deps.bookings.cancel(profile, booking_id=found.commitment.commitment_id, principal=caller)
        except (NotFound, InvalidState):
            return finish(_not_found(CancelRequestResult, status="unavailable"), profile, outcome="not_found")
        shown = render.commitment_result(profile, cancelled)
        return finish(_as_cancel_result(shown), profile, outcome=cancelled.status, commitment_id=cancelled.commitment_id)
