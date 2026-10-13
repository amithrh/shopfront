"""Plans: the validated, priced, approval-checked description of a commitment before it is made.

Every transactional tool works in two phases:

  1. `plan` (pure, deterministic, no writes): resolve the item and module, normalise intake, check the
     slot/day against the availability rules and current capacity, compute the price, evaluate the
     approval rules, list missing intake fields, build the read-back. Runs on every MRTR round, so it
     must be deterministic for the same arguments (the confirmation message is derived from it).
  2. `commit` (one atomic write): only after the customer accepted the confirmation elicitation.

Plans carry structured values and fact refs only. Speech is produced by the tools layer.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from askonce_server.domain.models import (
    ApprovalCheck,
    CapacityHold,
    Commitment,
    CommitmentKind,
    PriceQuote,
    TimeWindow,
)

Operation = Literal["book", "cancel_booking", "custom_order", "quote_request", "ask_owner"]

ProblemCode = Literal[
    "unknown_module",
    "unknown_item",
    "not_offered",
    "invalid_choice",
    "missing_choice",
    "invalid_intake",
    "party_size_out_of_range",
    "closed",
    "outside_hours",
    "not_on_schedule",
    "lead_time",
    "too_far_ahead",
    "capacity_full",
    "outside_service_area",
    "unknown_visit_window",
    "disputed_fact",
    "in_the_past",
    "too_many_attempts",
]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IntakeFieldSpec(_Model):
    """One intake field as the tools layer needs it to build an elicitation form."""

    key: str
    label: str
    type: Literal[
        "text", "long_text", "integer", "number", "boolean", "choice", "multi_choice", "date", "time", "email", "phone", "money"
    ]
    required: bool = False
    spoken_prompt: str | None = None
    options: tuple[tuple[str, str], ...] = ()
    """(value, label) pairs for choice / multi_choice."""
    min: float | None = None
    max: float | None = None
    max_length: int | None = None
    pii: bool = False


class SlotOption(_Model):
    """An open slot (booking) or day (custom order / quote) offered to the customer."""

    kind: Literal["time", "day"]
    window: TimeWindow | None = None
    day: date | None = None
    session_id: str | None = None
    resource_id: str | None = None
    remaining: int | None = None
    """Capacity left (units). Informational; the assistant does not have to say it."""
    fact_refs: tuple[str, ...] = ()


class PlanProblem(_Model):
    """Why a plan cannot be committed as asked. `detail` is for logs and cards, never spoken verbatim."""

    code: ProblemCode
    detail: str
    field: str | None = None
    fact_ref: str | None = None
    alternatives: tuple[SlotOption, ...] = ()


class ReadBackItem(_Model):
    """One `confirmation.read_back` entry with its structured value (the tools layer speaks it)."""

    key: str
    value: JsonValue
    fact_refs: tuple[str, ...] = ()


class CommitPlan(_Model):
    """Everything needed to confirm and then commit one request."""

    business_id: str
    module_id: str
    kind: CommitmentKind
    operation: Operation
    principal: str
    profile_revision: int
    idempotency_key: str

    item_ref: str | None = None
    choices: dict[str, str] = Field(default_factory=dict)
    window: TimeWindow | None = None
    day: date | None = None
    party_size: int | None = None
    visit_window_id: str | None = None
    resource_id: str | None = None
    """Booking: the resource assigned to the slot (requested, or the first free eligible one). Additive (v1.1)."""
    resource_requested: bool = False
    """Booking: True when the customer named the resource; otherwise a lost race may reassign it."""
    intake: dict[str, JsonValue] = Field(default_factory=dict)
    question: str | None = None
    fact_ref: str | None = None

    missing: tuple[IntakeFieldSpec, ...] = ()
    """Required intake fields the customer has not supplied (asked by elicitation before confirming)."""
    price: PriceQuote | None = None
    approval: ApprovalCheck = ApprovalCheck(required=False)
    holds: tuple[CapacityHold, ...] = ()
    read_back: tuple[ReadBackItem, ...] = ()
    say_before_confirm: tuple[str, ...] = ()
    """Knowledge entry refs whose `answer_spoken` is read before 'Shall I ...?'."""
    problems: tuple[PlanProblem, ...] = ()
    fact_refs: tuple[str, ...] = ()
    """Every profile fact the plan relied on (grounding for the confirmation and the result)."""

    @property
    def ok(self) -> bool:
        """No problems and nothing missing: ready to confirm."""
        return not self.problems and not self.missing


class CommitOutcome(_Model):
    """Result of `commit`: the stored commitment, whether it was a replay, or why nothing was written."""

    commitment: Commitment | None
    replayed: bool = False
    problem: PlanProblem | None = None
