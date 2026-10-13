"""Domain records: commitments (bookings, custom orders, quote requests, owner questions) and their parts.

A *commitment* is anything a customer asked the business to do through AskOnce. All four kinds share
one record type and one store, so idempotency, capacity holds, owner approval and the owner inbox work
identically for every vertical. Kind-specific inputs live in typed fields where the engine needs them
(`window`, `day`, `party_size`, `choices`) and in `intake` (the module's intake field values).

Status machine (`ALLOWED_TRANSITIONS`):

    (new) -> confirmed            booking / order that needs no owner approval
    (new) -> pending_owner        approval required, every quote request, every owner question
    pending_owner -> approved | declined | counter_offer | suggested_time | quoted | answered
                   | info_requested | expired | cancelled
    info_requested -> pending_owner (customer answered) | approved | declined | counter_offer
                   | suggested_time | quoted | expired | cancelled
    confirmed | approved -> cancelled
    everything else is terminal

Capacity holds are taken at commit time (a pending request occupies its slot) and released atomically
with any transition into a status in `RELEASES_HOLDS`. A commitment in a terminal status
(`SETTLED_STATUSES`) is never replayed: an identical request is made afresh.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

CommitmentKind = Literal["booking", "custom_order", "quote_request", "owner_question"]

CommitmentStatus = Literal[
    "pending_owner",
    "confirmed",
    "approved",
    "counter_offer",
    "suggested_time",
    "quoted",
    "info_requested",
    "answered",
    "declined",
    "cancelled",
    "expired",
]

DecisionKind = Literal["approve", "decline", "counter_offer", "quote", "suggest_time", "request_info", "answer"]
"""Owner decisions. The first six come from the schema's `approval.decisions`; `answer` is for owner questions."""

DECISION_TO_STATUS: dict[DecisionKind, CommitmentStatus] = {
    "approve": "approved",
    "decline": "declined",
    "counter_offer": "counter_offer",
    "quote": "quoted",
    "suggest_time": "suggested_time",
    "request_info": "info_requested",
    "answer": "answered",
}

_OWNER_OUTCOMES: frozenset[CommitmentStatus] = frozenset(
    {"approved", "declined", "counter_offer", "suggested_time", "quoted", "answered", "expired", "cancelled"}
)

ALLOWED_TRANSITIONS: dict[CommitmentStatus, frozenset[CommitmentStatus]] = {
    "pending_owner": _OWNER_OUTCOMES | {"info_requested"},
    "info_requested": (_OWNER_OUTCOMES - {"answered"}) | {"pending_owner"},
    "confirmed": frozenset({"cancelled"}),
    "approved": frozenset({"cancelled"}),
    "counter_offer": frozenset(),
    "suggested_time": frozenset(),
    "quoted": frozenset(),
    "answered": frozenset(),
    "declined": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
}

OPEN_STATUSES: frozenset[CommitmentStatus] = frozenset({"pending_owner", "info_requested"})
"""Waiting on the owner: listed in the owner inbox, subject to expiry."""

ALL_STATUSES: frozenset[CommitmentStatus] = frozenset(get_args(CommitmentStatus))
"""Every commitment status (the owner inbox's `status=all` view)."""

RELEASES_HOLDS: frozenset[CommitmentStatus] = frozenset(
    {"declined", "cancelled", "expired", "counter_offer", "suggested_time"}
)
"""Entering one of these frees the commitment's capacity holds."""

SETTLED_STATUSES: frozenset[CommitmentStatus] = frozenset(
    status for status, targets in ALLOWED_TRANSITIONS.items() if not targets
)
"""Terminal statuses (no outgoing transition): `RELEASES_HOLDS` plus `quoted` and `answered`.

The request behind a settled commitment is over, so an identical request is a *new* request (a new
generation, `idempotency.generation_key`), never a replay of the old outcome. Only live commitments
(open, confirmed or approved) are replayed."""


def can_transition(current: CommitmentStatus, target: CommitmentStatus) -> bool:
    """True if `current -> target` is allowed."""
    return target in ALLOWED_TRANSITIONS[current]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PriceLine(_Model):
    """One component of a computed price, traceable to the profile fact it came from."""

    label: str
    amount_cents: int
    fact_ref: str


class PriceQuote(_Model):
    """A price the assistant may state, with its parts. Never an estimate: `quote_only` has no amount."""

    kind: Literal["fixed", "from", "range", "quote_only", "free"]
    currency: str
    amount_cents: int | None = None
    max_cents: int | None = None
    per: str | None = None
    lines: tuple[PriceLine, ...] = ()
    fact_refs: tuple[str, ...] = ()


class TimeWindow(_Model):
    """A local time span in the business timezone (both ends timezone-aware)."""

    start: AwareDatetime
    end: AwareDatetime


class CapacityHold(_Model):
    """`units` of capacity on counter `key`, which may never exceed `limit`."""

    key: str
    units: int = Field(ge=1)
    limit: int = Field(ge=1)


class ApprovalCheck(_Model):
    """Outcome of evaluating a module's `approval` block against a planned request."""

    required: bool
    owner_module_id: str | None = None
    reasons_spoken: tuple[str, ...] = ()
    """`reason_spoken` of each matched rule (or a generic line for `mode: always`)."""
    matched_rule_keys: tuple[str, ...] = ()
    fact_refs: tuple[str, ...] = ()


class OwnerDecision(_Model):
    """What the owner decided on the owner page."""

    decision: DecisionKind
    values: dict[str, JsonValue] = Field(default_factory=dict)
    """Validated `approval.response_fields` values (money already in cents)."""
    note: str | None = None
    question: str | None = None
    """For `request_info`: the question relayed to the customer through the task."""
    decided_at: AwareDatetime
    answered_at: AwareDatetime | None = None
    """For `request_info`: when the customer's reply to that question was folded into the commitment
    (None while it is unanswered). Additive: older records simply lack it."""


class Commitment(_Model):
    """A booking, custom order, quote request or owner question (see module docstring)."""

    commitment_id: str
    business_id: str
    module_id: str
    kind: CommitmentKind
    status: CommitmentStatus
    principal: str
    idempotency_key: str
    profile_revision: int

    item_ref: str | None = None
    choices: dict[str, str] = Field(default_factory=dict)
    """Option group id -> choice id."""
    window: TimeWindow | None = None
    """Booked slot (booking) or requested visit window on `day` (quote)."""
    day: date | None = None
    """Pickup/fulfilment date (custom order) or preferred date (quote), local to the business."""
    party_size: int | None = None
    visit_window_id: str | None = None
    resource_id: str | None = None
    """Booking: the resource (stylist, table, instructor) the slot was booked with. Additive (v1.1)."""
    intake: dict[str, JsonValue] = Field(default_factory=dict)
    """Normalised intake values keyed by intake field key. Contains PII: never log, never trace."""
    price: PriceQuote | None = None
    approval: ApprovalCheck = ApprovalCheck(required=False)
    holds: tuple[CapacityHold, ...] = ()
    holds_released: bool = False

    question: str | None = None
    """Owner questions: the customer's question as relayed to the owner."""
    fact_ref: str | None = None
    """Owner questions: the unknown or disputed fact that prompted it, if any."""

    task_id: str | None = None
    decision: OwnerDecision | None = None
    customer_input: dict[str, JsonValue] | None = None
    """Answers the customer gave through the task's input_required round (request_info)."""

    created_at: AwareDatetime
    updated_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    """When an open commitment expires per the owner_approval module (`expires_after_minutes`)."""
    version: int = 0


class IdempotencyRecord(_Model):
    """Marks that idempotency `key` produced `commitment_id`. Written in the same atomic unit as the commitment."""

    key: str
    business_id: str
    commitment_id: str
    created_at: datetime
    expires_at: datetime
