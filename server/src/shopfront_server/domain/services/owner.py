"""Owner-facing flows: 'ask the owner' questions, decisions on pending requests, and expiry.

Decision flow (`decide`): load the commitment (must belong to `business_id`), check the decision is in the
module's `approval.decisions` (or `answer` for owner questions), validate `values` against
`approval.response_fields` (money -> cents), CAS-transition the commitment, then drive its MCP task:

  approve/decline/counter_offer/quote/suggest_time/answer -> TaskManager.complete(task, renderer.render(...))
  request_info -> TaskManager.require_input(task, {"owner_question_<n>": ElicitRequest(form: answer)})

The renderer builds a result that validates against the originating tool's output schema. If the task is
already terminal (the customer cancelled it), the decision is rejected with `errors.InvalidState`. A
customer's `tasks/cancel` reaches `on_task_cancelled` (the tasks extension's cancel hook), which cancels
the still-open commitment at once, releasing its holds, so the slot or day is freed even if the owner
never looks; the inbox and `decide` also close such a commitment if the hook could not.

Customer cancel vs. owner decision: `decide` stores the decision before it completes the task, so a
`tasks/cancel` can land in between. The cancel wins: a cancelled task proves the decision was never
delivered, so the hook (or `decide` itself, when completing the task fails because it was cancelled)
withdraws an `approved` commitment too (`_withdraw`: primary-key re-read, CAS, bounded retry).

A commitment whose task record is gone (its TTL passed) can still be decided, except `request_info`
(there is nobody left to ask): the status is stored and holds are released, only the task is skipped.

A booking or custom order whose service time has passed (`service_deadline`: the slot's start, the end
of the order's day) can no longer be approved, counter-offered or asked about (`_NEEDS_THE_DATE`); it
can still be declined or answered with another time. The inbox leaves those decisions out too.

Customer replies to `request_info` arrive on the task (through `tasks/update` or the `reply_to_owner`
tool) and are folded into the commitment (`customer_input`, info_requested -> pending_owner,
`decision.answered_at`) as soon as they arrive (`absorb_customer_answers`: the reply tool and the tasks
extension's input hook), and again, as a fallback, by the inbox, `decide` and expiry. Expiry therefore
keeps the reply, and an expired request whose question was answered is not described as unanswered.
`get_task` renders the same fold without storing it (`with_customer_answers`).

Field rules for decisions:
  * required `response_fields` are enforced for every decision except `decline` and `request_info`;
  * `counter_offer`, `suggest_time` and `quote` need at least one value or a note;
  * `request_info` needs `question` (or a note), relayed to the customer as an elicitation
    (`owner_question_request`, built before anything is stored so the canary check covers the text
    actually sent: "Rosa asks: <question>" and the task's status message);
  * `answer` (owner questions only) needs `values.answer` or a note; it is stored as `values.answer`.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Any

import mcp_types as types
from pydantic import BaseModel, Field, JsonValue

from mcp_tasks import InvalidTransition, TaskManager, TaskNotFound, TaskRecord
from shopfront_server.domain.idempotency import idempotency_key
from shopfront_server.domain.intake import clean_text, field_specs, normalize_value
from shopfront_server.domain.models import (
    DECISION_TO_STATUS,
    OPEN_STATUSES,
    ApprovalCheck,
    Commitment,
    CommitmentStatus,
    DecisionKind,
    OwnerDecision,
    can_transition,
)
from shopfront_server.domain.planning import CommitPlan, PlanProblem
from shopfront_server.domain.ports import DecisionRenderer
from shopfront_server.domain.services.base import (
    TASK_TTL_SLACK,
    CommitmentServiceBase,
    DomainContext,
    dedupe,
    keep_waiting_horizon,
    log,
    owner_module_for,
    owner_module_of,
    service_deadline,
    whole_ms,
)
from shopfront_server.errors import (
    ConcurrencyConflict,
    InvalidDecision,
    InvalidState,
    NotFound,
    UnknownModule,
)
from shopfront_server.observability import id_digest
from shopfront_server.profile.factref import FACT_REF_PATTERN
from shopfront_server.profile.model import JsonObj, Profile
from shopfront_server.profile.store import ProfileStore

MAX_QUESTION_LENGTH = 300
MAX_ANSWER_LENGTH = 1000
OWNER_QUESTION_KEY = "owner_question_{n}"

ANSWER_FIELD: dict[str, JsonValue] = {
    "key": "answer",
    "label": "Your answer",
    "type": "long_text",
    "required": True,
    "max_length": MAX_ANSWER_LENGTH,
}
"""The response field of an owner question (they have no `approval` block)."""

_NEEDS_CONTENT: frozenset[str] = frozenset({"counter_offer", "suggest_time", "quote"})
_SKIP_REQUIRED: frozenset[str] = frozenset({"decline", "request_info"})

_WITHDRAWABLE: frozenset[CommitmentStatus] = OPEN_STATUSES | {"approved"}
"""Statuses a customer's task cancel withdraws (-> cancelled, holds released).

`approved` is included because the cancel only ever reaches the commitment while its task was still
active: a delivered approval completes the task, after which `tasks/cancel` is refused. An `approved`
commitment behind a cancelled task is therefore a decision the customer never received (the cancel
landed between `decide`'s commitment write and its task completion), and the customer's cancel wins.
`confirmed` (no task), `quoted`/`answered` (no capacity) and the statuses that already released their
holds are left alone."""

_NEEDS_THE_DATE: frozenset[DecisionKind] = frozenset({"approve", "counter_offer", "request_info"})
"""Decisions that keep a request on its original date: refused once that date has passed
(`service_deadline`). Declining it, or suggesting another time, stays possible."""

_WITHDRAW_ATTEMPTS = 3
_FOLD_ATTEMPTS = 3


class OwnerDecisionInput(BaseModel):
    """What the owner page submits."""

    decision: DecisionKind
    values: dict[str, JsonValue] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=1000)
    question: str | None = Field(default=None, max_length=300)


class InboxItem(BaseModel):
    """A commitment plus the live task state, for the owner page."""

    commitment: Commitment
    task_status: str | None = None
    allowed_decisions: list[DecisionKind] = []
    response_fields: list[dict[str, JsonValue]] = []


def approver_name(profile: Profile, module_id: str | None) -> str:
    """`approver.spoken_name` of the owner module responsible for `module_id` (business name fallback)."""
    owner = owner_module_for(profile, module_id)
    name = ((owner[1] if owner else {}).get("approver") or {}).get("spoken_name")
    return str(name or profile.spoken_name)


def owner_question_request(profile: Profile, module_id: str | None, question: str) -> tuple[types.ElicitRequest, str]:
    """The task input request relaying the owner's `request_info` question to the customer ("Rosa
    asks: <question>", one free-text `answer`), and the task's status message while it waits."""
    approver = approver_name(profile, module_id)
    request = types.ElicitRequest(
        params=types.ElicitRequestFormParams(
            message=f"{approver} asks: {question}",
            requested_schema={
                "type": "object",
                "properties": {"answer": {"type": "string", "title": "Your answer", "maxLength": MAX_ANSWER_LENGTH}},
                "required": ["answer"],
            },
        )
    )
    return request, f"{approver} has a question for you"


def _texts(value: Any) -> list[str]:
    """Every string in a JSON-like value (what a relayed request actually carries to the customer)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _texts(item)]
    if isinstance(value, list | tuple):
        return [text for item in value for text in _texts(item)]
    return []


def _plain_object(value: Mapping[str, Any]) -> dict[str, JsonValue]:
    """A read-only profile JSON object as a plain dict."""
    return {str(k): _plain(v) for k, v in value.items()}


def _plain(value: Any) -> JsonValue:
    """A read-only profile JSON view as plain dicts/lists (for API models)."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value  # type: ignore[no-any-return]  # a JSON scalar


def with_customer_answers(commitment: Commitment, task: TaskRecord | None, now: datetime) -> Commitment | None:
    """`commitment` with the customer's new replies to the owner's question folded in, or None if
    there are none. Pure: nothing is stored.

    Applies to an `info_requested` commitment whose task is back to `working` (its question was
    answered or declined, through `tasks/update` or `reply_to_owner`): the replies go into
    `customer_input`, the status becomes `pending_owner` and `decision.answered_at` is set. Only
    replies whose keys are not in `customer_input` yet count, so a task snapshot that still carries an
    earlier round's replies (the owner has just asked again and the new question has not reached the
    task yet) never marks the new question as answered."""
    if commitment.status != "info_requested" or task is None or task.status != "working":
        return None
    known = commitment.customer_input or {}
    fresh = {key: response for key, response in task.input_responses.items() if key not in known}
    if not fresh:
        return None
    answers: dict[str, JsonValue] = {}
    for key, response in sorted(fresh.items()):
        action = response.get("action")
        answers[key] = _plain(response.get("content")) if action == "accept" else {"action": action}
    decision = commitment.decision.model_copy(update={"answered_at": now}) if commitment.decision is not None else None
    return commitment.model_copy(
        update={"status": "pending_owner", "customer_input": {**known, **answers}, "decision": decision, "updated_at": now}
    )


class OwnerService(CommitmentServiceBase):
    def __init__(
        self, ctx: DomainContext, *, tasks: TaskManager, renderer: DecisionRenderer, profiles: ProfileStore
    ) -> None:
        super().__init__(ctx)
        self.tasks = tasks
        self.renderer = renderer
        self.profiles = profiles

    # ---- ask the owner ----------------------------------------------------------------------

    async def plan_question(
        self,
        profile: Profile,
        *,
        question: str,
        principal: str,
        fact_ref: str | None = None,
        nonce: str | None = None,
    ) -> CommitPlan:
        """Plan an `owner_question` commitment (module = the owner_approval module). Problems if the
        profile's owner_approval module has `answers_unknown_questions: false`.

        The question is whitespace-normalised (so retries hash identically) and must be 1-300
        characters. A malformed `fact_ref` is dropped rather than failing the hand-off. `nonce` makes
        the request unique: without auth every customer is 'anonymous', and two customers asking the
        same common question must not share one question (nor its personal answer).
        """
        text = clean_text(question or "")
        ref = fact_ref if fact_ref and FACT_REF_PATTERN.match(fact_ref) else None
        owner = owner_module_for(profile, None)
        module_id = owner[0] if owner else ""
        problems: list[PlanProblem] = []
        if owner is None:
            problems.append(PlanProblem(code="unknown_module", detail="this business takes no questions", field="question"))
        elif owner[1].get("answers_unknown_questions") is False:
            problems.append(
                PlanProblem(
                    code="not_offered",
                    detail="the owner does not take questions this way",
                    field="question",
                    fact_ref=f"{module_id}#answers_unknown_questions",
                )
            )
        if not text:
            problems.append(PlanProblem(code="invalid_intake", detail="the question is empty", field="question"))
        elif len(text) > MAX_QUESTION_LENGTH:
            problems.append(
                PlanProblem(code="invalid_intake", detail=f"keep the question under {MAX_QUESTION_LENGTH} characters", field="question")
            )
        key = idempotency_key(
            business_id=profile.profile_id,
            module_id=module_id,
            operation="ask_owner",
            principal=principal,
            payload={"question": text, "fact_ref": ref, **({"nonce": nonce} if nonce else {})},
        )
        refs = [f"{module_id}#approver.spoken_name", f"{module_id}#expected_response_minutes"] if owner else []
        plan = CommitPlan(
            business_id=profile.profile_id,
            module_id=module_id,
            kind="owner_question",
            operation="ask_owner",
            principal=principal,
            profile_revision=profile.revision,
            idempotency_key=key,
            question=text or None,
            fact_ref=ref,
            approval=ApprovalCheck(required=True, owner_module_id=module_id or None, fact_refs=tuple(refs[:1])),
            problems=tuple(problems),
            fact_refs=dedupe(refs),
        )
        return await self.settle_replay(plan)

    # ---- owner inbox ------------------------------------------------------------------------

    async def inbox(
        self,
        business_id: str,
        *,
        statuses: Collection[CommitmentStatus] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[InboxItem], str | None]:
        """Pending (default: OPEN_STATUSES) requests for the owner page, newest first. Also folds any
        customer answers from the task's `input_responses` into `customer_input` (info_requested ->
        pending_owner)."""
        wanted = set(statuses) if statuses is not None else set(OPEN_STATUSES)
        commitments, next_cursor = await self.ctx.commitments.list_for_business(
            business_id, statuses=wanted, limit=limit, cursor=cursor
        )
        profile = await self.profiles.get(business_id)
        return [await self._inbox_item(profile, commitment) for commitment in commitments], next_cursor

    async def item(self, business_id: str, commitment_id: str) -> InboxItem | None:
        """One request exactly as `inbox` would list it (customer answers folded in); None if unknown."""
        commitment = await self.ctx.commitments.get(business_id, commitment_id)
        if commitment is None:
            return None
        return await self._inbox_item(await self.profiles.get(business_id), commitment)

    async def _inbox_item(self, profile: Profile | None, commitment: Commitment) -> InboxItem:
        task = await self._task(commitment)
        if task is not None and (
            (task.terminal and commitment.status in OPEN_STATUSES)
            or (task.status == "cancelled" and commitment.status in _WITHDRAWABLE)
        ):
            # The customer gave up (or the task ended otherwise): nothing can be decided any more. An
            # approval behind a cancelled task never reached the customer, so it is withdrawn as well.
            commitment = await self._close_after_customer_cancel(commitment, task)
            return InboxItem(commitment=commitment, task_status=task.status)
        commitment = await self._fold_answers(commitment, task)
        allowed = list(self._allowed(profile, commitment)) if profile else []
        if commitment.task_id is not None and task is None:
            allowed = [d for d in allowed if d != "request_info"]  # nobody left to ask
        if profile is not None and self._date_passed(profile, commitment):
            allowed = [d for d in allowed if d not in _NEEDS_THE_DATE]
        return InboxItem(
            commitment=commitment,
            task_status=task.status if task else None,
            allowed_decisions=allowed,
            response_fields=[_plain_object(f) for f in self._response_fields(profile, commitment)] if profile else [],
        )

    async def on_task_cancelled(self, task: TaskRecord) -> None:
        """Cancel hook for the tasks extension: a customer's `tasks/cancel` withdraws the request.

        Runs only after the task really moved to `cancelled`. The commitment behind it (still open, or
        `approved` by a decision that was never delivered) is cancelled, which releases its capacity
        holds at once: the owner need not act and the slot is not blocked until expiry.

        The commitment is found from the task's server-set labels (`business_id`, `commitment_id`) and
        falls back to the task index only for tasks created without them. The index (a DynamoDB GSI)
        is eventually consistent, so it is used just to learn the ids: `_withdraw` re-reads the
        commitment by primary key (a consistent read) before every write."""
        if task.status != "cancelled":
            return  # only a task that really was cancelled proves no decision reached the customer
        key = await self._commitment_key(task)
        if key is None:
            log.info("cancelled task has no commitment", extra={"task_id": id_digest(task.task_id)})
            return
        await self._withdraw(*key, task.task_id)

    async def absorb_customer_answers(self, task: TaskRecord) -> Commitment | None:
        """Fold the customer's replies on `task` into its commitment now (`with_customer_answers`).

        Called right after a reply was delivered (`reply_to_owner`, and `on_task_input` for
        `tasks/update`), so the stored request stops showing the owner's question at once and keeps
        the reply even if it expires before the owner looks. The commitment is found as in
        `on_task_cancelled` (labels, else the task index) and re-read by primary key, the task is
        re-read too. Returns the commitment as stored afterwards (None if the task has none)."""
        key = await self._commitment_key(task)
        current = await self.ctx.commitments.get(*key) if key is not None else None
        if current is None or current.task_id != task.task_id:
            return None
        return await self._fold_answers(current, await self._task(current))

    async def on_task_input(self, task: TaskRecord) -> None:
        """Input hook for the tasks extension: runs after a client's `tasks/update` was accepted."""
        await self.absorb_customer_answers(task)

    async def _commitment_key(self, task: TaskRecord) -> tuple[str, str] | None:
        """(business_id, commitment_id) of the commitment behind `task`: from its server-set labels,
        else (tasks created without them) from the eventually consistent task index."""
        business_id = task.labels.get("business_id")
        commitment_id = task.labels.get("commitment_id")
        if business_id and commitment_id:
            return business_id, commitment_id
        found = await self.ctx.commitments.get_by_task(task.task_id)
        return (found.business_id, found.commitment_id) if found is not None else None

    # ---- decisions ----------------------------------------------------------------------------

    async def decide(self, business_id: str, commitment_id: str, decision: OwnerDecisionInput) -> Commitment:
        """Apply an owner decision (see module docstring). Raises errors.NotFound / InvalidDecision / InvalidState."""
        commitment = await self.ctx.commitments.get(business_id, commitment_id)
        if commitment is None:
            raise NotFound("unknown request")
        profile = await self.profiles.get(business_id)
        if profile is None:
            raise NotFound("unknown business")
        allowed = self._allowed(profile, commitment)
        if decision.decision not in allowed:
            raise InvalidDecision(f"{decision.decision!r} is not allowed here (allowed: {', '.join(allowed) or 'none'})")
        values = self._validate_values(profile, commitment, decision)
        note = clean_text(decision.note) if decision.note else None
        question = clean_text(decision.question or decision.note or "") if decision.decision == "request_info" else None
        if decision.decision == "request_info" and not question:
            raise InvalidDecision("ask the customer a question")
        texts: list[str | None] = [note, question, *(str(v) for v in values.values())]
        relay: tuple[types.ElicitRequest, str] | None = None
        if decision.decision == "request_info" and question:
            # Checked as sent (the composed message spans the approver's name and the question), and
            # before the write: a refused question stores nothing.
            relay = owner_question_request(profile, commitment.module_id, question)
            texts += [*_texts(relay[0].model_dump(by_alias=True, mode="json", exclude_none=True)), relay[1]]
        self._reject_canaries(profile, texts)

        task = await self._task(commitment)
        if commitment.task_id is not None:
            if task is None and decision.decision == "request_info":
                raise InvalidState("the customer can no longer be reached; approve or decline instead")
            if task is not None and task.terminal:
                await self._close_after_customer_cancel(commitment, task)
                raise InvalidState(f"the customer's request is already {task.status}")
        commitment = await self._fold_answers(commitment, task)
        target: CommitmentStatus = DECISION_TO_STATUS[decision.decision]
        if not can_transition(commitment.status, target):
            raise InvalidState(f"a {commitment.status} request cannot become {target}")
        if decision.decision in _NEEDS_THE_DATE and self._date_passed(profile, commitment):
            raise InvalidState("this request's date has passed; decline it or suggest another time")

        now = self.ctx.clock.now()
        recorded = OwnerDecision(
            decision=decision.decision, values=values, note=note, question=question, decided_at=now
        )
        updated = await self.ctx.commitments.update(
            commitment.model_copy(
                update={
                    "status": target,
                    "decision": recorded,
                    "updated_at": now,
                    "expires_at": commitment.expires_at if target in OPEN_STATUSES else None,
                }
            ),
            expected_version=commitment.version,
        )
        if updated.task_id is not None and task is not None:
            await self._drive_task(profile, updated, task, relay)
        return updated

    # ---- expiry -------------------------------------------------------------------------------

    async def expire_due(self, *, now: datetime | None = None, limit: int = 100) -> int:
        """Apply `on_expiry` to open commitments past `expires_at`; returns how many were handled.

        notify_customer -> status expired, task completed with an 'owner has not answered' result;
        auto_decline    -> status declined (decision recorded as automatic), task completed;
        keep_waiting    -> the request stays open until its `keep_waiting_horizon` (its service time,
                           at most `keep_waiting_max` after it was made), then ends as notify_customer.
                           Until then its task's TTL is extended to cover the horizon first, and only
                           then is `expires_at` moved to the horizon: if extending the task fails, the
                           deadline is left alone and the next sweep retries. A request whose task the
                           customer already cancelled is withdrawn instead (holds released).
        Safe on several replicas: each transition is a CAS; a lost race is skipped. A record that fails
        any other way is logged and left as it was, so the next sweep tries it again.
        """
        moment = now or self.ctx.clock.now()
        handled = 0
        for due in await self.ctx.commitments.list_expiring(before=moment, limit=limit):
            try:
                if await self._expire_one(due, moment):
                    handled += 1
            except (ConcurrencyConflict, InvalidState, NotFound):
                continue  # another replica (or the owner) got there first
            except Exception:  # noqa: BLE001 - one bad record must not stop the sweep
                log.exception(
                    "expiry failed", extra={"business_id": due.business_id, "commitment_id": id_digest(due.commitment_id)}
                )
        return handled

    async def _expire_one(self, due: Commitment, now: datetime) -> bool:
        current = await self.ctx.commitments.get(due.business_id, due.commitment_id)
        if current is None or current.status not in OPEN_STATUSES or current.expires_at is None or current.expires_at > now:
            return False
        # A reply the customer gave that nobody folded yet (the owner never opened the inbox) is kept,
        # and the request then expires as unanswered by the owner, not by the customer.
        current = await self._fold_answers(current, await self._task(current))
        if current.status not in OPEN_STATUSES or current.expires_at is None or current.expires_at > now:
            return False
        profile = await self.profiles.get(current.business_id)
        owner = owner_module_of(profile, current) if profile is not None else None
        policy = (owner[1] if owner else {}).get("on_expiry", "notify_customer")
        if policy == "keep_waiting" and profile is not None:
            horizon = keep_waiting_horizon(profile, current, since=current.created_at, max_wait=self.ctx.keep_waiting_max)
            if now < horizon:
                # The task first (a failure propagates and leaves `expires_at` due, so the next sweep
                # retries), then the deadline: the sweep comes back at the horizon.
                if not await self._keep_task_until(current, horizon + TASK_TTL_SLACK):
                    return False  # the customer had cancelled it: withdrawn instead
                await self.ctx.commitments.update(
                    current.model_copy(update={"expires_at": horizon, "updated_at": now}), expected_version=current.version
                )
                return False
            # Waited as long as it may (its date has passed, or `keep_waiting_max`): end it as notify_customer.
        if policy == "auto_decline":
            changes: dict[str, Any] = {
                "status": "declined",
                "decision": OwnerDecision(decision="decline", values={"automatic": True}, decided_at=now),
            }
        else:
            changes = {"status": "expired"}
        updated = await self.ctx.commitments.update(
            current.model_copy(update={**changes, "updated_at": now, "expires_at": None}),
            expected_version=current.version,
        )
        if updated.task_id is not None and profile is not None:
            try:
                await self._complete_task(profile, updated)
            except (TaskNotFound, InvalidTransition):
                log.info("expired request had no active task", extra={"commitment_id": id_digest(updated.commitment_id)})
        return True

    async def _keep_task_until(self, commitment: Commitment, until: datetime) -> bool:
        """Make the commitment's task stay addressable until `until` (keep_waiting). Returns False when
        the customer had already cancelled the task: the request is withdrawn instead (holds released).

        The TTL (counted from the task's `createdAt`) is only ever extended, and always to a finite
        value, so a decided request's task is still garbage-collected. A task that is gone, or that
        ended any other way, needs nothing. Any other failure (the task store throttling, say)
        propagates, so the caller leaves the commitment's deadline due and the next sweep retries."""
        task = await self._task(commitment)
        if task is not None and task.status == "cancelled":
            await self._close_after_customer_cancel(commitment, task)
            return False
        if task is None or task.terminal:
            return True
        expires = task.expires_at()
        if expires is not None and expires >= until:
            return True
        try:
            await self.tasks.set_ttl(task.task_id, whole_ms(until - task.created_at))
        except TaskNotFound:
            return True
        except InvalidTransition:
            # It ended meanwhile; a customer's cancel withdraws the request here as well.
            ended = await self._task(commitment)
            if ended is not None and ended.status == "cancelled":
                await self._close_after_customer_cancel(commitment, ended)
                return False
        return True

    # ---- internals ----------------------------------------------------------------------------

    def _date_passed(self, profile: Profile, commitment: Commitment) -> bool:
        """True once the service the request is for is due (`service_deadline`): it can no longer be
        approved for that date."""
        deadline = service_deadline(profile, commitment)
        return deadline is not None and self.ctx.clock.now() >= deadline

    def _allowed(self, profile: Profile | None, commitment: Commitment) -> tuple[DecisionKind, ...]:
        if commitment.kind == "owner_question":
            return ("answer",)
        if profile is None:
            return ()
        approval = self._approval(profile, commitment)
        return tuple(approval.get("decisions", ()) or ())

    def _approval(self, profile: Profile, commitment: Commitment) -> JsonObj:
        try:
            return profile.module(commitment.module_id).get("approval") or {}
        except UnknownModule:
            return {}

    def _response_fields(self, profile: Profile | None, commitment: Commitment) -> Sequence[JsonObj]:
        if commitment.kind == "owner_question":
            return [ANSWER_FIELD]
        if profile is None:
            return []
        return list(self._approval(profile, commitment).get("response_fields", ()) or ())

    def _validate_values(
        self, profile: Profile, commitment: Commitment, decision: OwnerDecisionInput
    ) -> dict[str, JsonValue]:
        specs = {spec.key: spec for spec in field_specs(self._response_fields(profile, commitment))}
        supplied = dict(decision.values)
        if decision.decision == "answer" and not supplied.get("answer") and decision.note:
            supplied["answer"] = decision.note
        unknown = sorted(set(supplied) - set(specs))
        if unknown:
            raise InvalidDecision(f"unknown fields: {', '.join(unknown)}")
        values: dict[str, JsonValue] = {}
        for key, spec in specs.items():
            raw = supplied.get(key)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                if spec.required and decision.decision not in _SKIP_REQUIRED:
                    raise InvalidDecision(f"{spec.label} is required")
                continue
            try:
                values[key] = normalize_value(spec, raw, locale=profile.locale)
            except ValueError as exc:
                raise InvalidDecision(f"{spec.label}: {exc}") from None
        if decision.decision in _NEEDS_CONTENT and not values and not (decision.note and decision.note.strip()):
            raise InvalidDecision(f"add details or a note for {decision.decision}")
        return values

    def _reject_canaries(self, profile: Profile, texts: Sequence[str | None]) -> None:
        """L8: planted injection text must never reach a customer, even through the owner page."""
        canaries = [clean_text(c).casefold() for c in profile.injection_canaries() if c and c.strip()]
        for text in texts:
            folded = clean_text(text).casefold() if text else ""
            if folded and any(canary in folded for canary in canaries):
                raise InvalidDecision("that text cannot be sent to the customer")

    async def _task(self, commitment: Commitment) -> TaskRecord | None:
        if commitment.task_id is None:
            return None
        try:
            return await self.tasks.get(commitment.task_id)
        except TaskNotFound:
            return None

    async def _fold_answers(self, commitment: Commitment, task: TaskRecord | None) -> Commitment:
        """Store the customer's replies to `request_info` (`with_customer_answers`). A concurrent change
        is re-read, with its task, and folded again (bounded); returns the commitment as last seen."""
        current = commitment
        for _ in range(_FOLD_ATTEMPTS):
            folded = with_customer_answers(current, task, self.ctx.clock.now())
            if folded is None:
                return current
            try:
                return await self.ctx.commitments.update(folded, expected_version=current.version)
            except (ConcurrencyConflict, InvalidState):
                reloaded = await self.ctx.commitments.get(current.business_id, current.commitment_id)
                if reloaded is None:
                    return current
                current = reloaded
                task = await self._task(current)
        return current

    async def _close_after_customer_cancel(self, commitment: Commitment, task: TaskRecord) -> Commitment:
        """The customer cancelled the task: withdraw the commitment too, freeing its holds. Returns the
        commitment as stored afterwards (unchanged if the task ended any other way)."""
        if task.status != "cancelled" or commitment.task_id != task.task_id:
            return commitment
        withdrawn = await self._withdraw(commitment.business_id, commitment.commitment_id, task.task_id)
        return withdrawn or commitment

    async def _withdraw(
        self, business_id: str, commitment_id: str, task_id: str, *, attempts: int = _WITHDRAW_ATTEMPTS
    ) -> Commitment | None:
        """Cancel the commitment behind the customer-cancelled task `task_id`, releasing its holds.

        Callers guarantee the task is `cancelled`. Each attempt re-reads the commitment by primary key
        (consistent in DynamoDB, unlike the task index) and CASes it to `cancelled` if it still belongs to
        that task and is in `_WITHDRAWABLE`; a concurrent change (an owner decision, folded answers) is
        re-read and retried. Returns the commitment as last stored, or None if it is unknown."""
        current: Commitment | None = None
        for _ in range(attempts):
            current = await self.ctx.commitments.get(business_id, commitment_id)
            if current is None or current.task_id != task_id or current.status not in _WITHDRAWABLE:
                return current
            try:
                return await self.ctx.commitments.update(
                    current.model_copy(
                        update={"status": "cancelled", "updated_at": self.ctx.clock.now(), "expires_at": None}
                    ),
                    expected_version=current.version,
                )
            except ConcurrencyConflict:
                continue  # changed under us: re-read and try again
            except (InvalidState, NotFound):
                return await self.ctx.commitments.get(business_id, commitment_id)
        log.warning(
            "could not withdraw the commitment of a cancelled task",
            extra={"commitment_id": id_digest(commitment_id), "task_id": id_digest(task_id), "attempts": attempts},
        )
        return current

    async def _complete_task(self, profile: Profile, commitment: Commitment) -> None:
        """Complete the commitment's task with the rendered result. The decision is already stored, so a
        rendering failure fails the task (the customer stops waiting) instead of leaving it working."""
        assert commitment.task_id is not None
        try:
            result = self.renderer.render(profile, commitment)
        except Exception:  # noqa: BLE001 - logged with the traceback; the customer must not wait forever
            log.exception("rendering the decision failed", extra={"commitment_id": id_digest(commitment.commitment_id)})
            await self.tasks.fail(commitment.task_id, "The request could not be completed. Please ask again.")
            return
        await self.tasks.complete(commitment.task_id, result)

    async def _drive_task(
        self, profile: Profile, updated: Commitment, task: TaskRecord, relay: tuple[types.ElicitRequest, str] | None
    ) -> None:
        """Deliver the stored decision: `relay` (the checked `owner_question_request`) for
        `request_info`, else the rendered result."""
        assert updated.task_id is not None
        try:
            if updated.status == "info_requested":
                assert relay is not None
                request, status_message = relay
                number = len(task.input_responses) + len(task.input_requests or {}) + 1
                await self.tasks.require_input(
                    updated.task_id, {OWNER_QUESTION_KEY.format(n=number): request}, message=status_message
                )
            else:
                await self._complete_task(profile, updated)
        except (TaskNotFound, InvalidTransition) as exc:
            now_task = await self._task(updated)
            if now_task is not None and now_task.status == "cancelled":
                # The customer's tasks/cancel landed between storing the decision and delivering it: the
                # decision never reached them, so their cancel wins. Withdraw it here too, in case the
                # cancel hook did not run (another replica died in between) or lost its race with us.
                await self._withdraw(updated.business_id, updated.commitment_id, updated.task_id)
                raise InvalidState("the customer's request is already cancelled") from exc
            log.warning(
                "decision stored but the task could not be updated",
                extra={"commitment_id": id_digest(updated.commitment_id), "task_id": id_digest(updated.task_id)},
            )
            raise InvalidState("the customer's request is no longer active") from exc
