"""Tool output models: the grounded-answer contract.

Every tool returns a model derived from `Grounded`, so every result's `structuredContent` carries:

  speak    <= 240 chars of plain speech (no URLs, no markup, no newlines). The ONLY text a voice host
           reads aloud. Built from templates over `facts`; never free text from a model or a website.
  facts    every profile fact the speech relies on: `{fact_ref, value, evidence_url, owner_confirmed}`.
           The certifier checks deterministically that each number/claim in `speak` equals a fact
           value, and that each fact is statable (L6) in the profile revision that served it.
  card     optional display payload for MCP Apps (`ui://`) and text hosts; citations live here, never
           in `speak`.
  handoff  set when the answer is unknown or withheld: "I don't know. Want me to ask Rosa?" with the
           `ask_owner` arguments to use. Unknown facts are never guessed.

This module is a frozen contract shared by the tools layer, the renderer used for task results, the
reference host and the certifier. Changing a field is a cross-package change.

Schema portability: every subschema of every tool's `inputSchema` and `outputSchema` says what it
accepts (a `type`, `enum`, `const`, `$ref` or `anyOf`), because some MCP clients refuse the empty
schema `{}` / `true` that pydantic emits for `Any` and `JsonValue`. Free-form JSON is `JsonData`, an
explicit recursive union of the JSON types; `tests/tools/test_schema_portability.py` enforces this.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, Strict, StrictBool, StrictFloat, StrictInt, StrictStr

SPEAK_MAX = 240
_FORBIDDEN_SPOKEN = re.compile(r"(https?:|www\.|[<>{}\[\]|*#_~`\\\n])", re.IGNORECASE)


def check_spoken(text: str) -> str:
    """Validator for spoken text: same rule as the profile schema's `$defs.spoken`."""
    if not text or len(text) > SPEAK_MAX:
        raise ValueError(f"spoken text must be 1-{SPEAK_MAX} characters")
    if _FORBIDDEN_SPOKEN.search(text):
        raise ValueError("spoken text may not contain URLs, markup or newlines")
    return text


SpokenText = Annotated[str, AfterValidator(check_spoken)]

type JsonData = (
    StrictStr
    | StrictInt
    | StrictFloat
    | StrictBool
    | Annotated[list[JsonData], Strict()]
    | Annotated[dict[str, JsonData], Strict()]
    | None
)
"""Any JSON value, spelled out (`$defs.JsonData`: an `anyOf` of the seven JSON types). Use it instead
of `Any` / `pydantic.JsonValue`, whose JSON Schema is the untyped `{}` (see the module docstring).
Members are strict, so it validates exactly like `JsonValue`: no tuples, sets, bytes or non-string
keys, and `True` stays a bool rather than becoming 1."""


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------- grounding primitives


class Fact(_Out):
    """One profile fact a result relies on."""

    fact_ref: str = Field(description="Stable fact address, e.g. menu/celebration-cake#price.")
    value: JsonData = Field(description="The fact's value exactly as in the profile (prices in cents).")
    evidence_url: str | None = Field(
        default=None, description="Where it came from: a web page or urn:askonce:owner-answer:<id>."
    )
    owner_confirmed: bool = False


class Citation(_Out):
    url: str
    title: str | None = None
    quote: str | None = Field(default=None, max_length=300)


class CardLine(_Out):
    label: str
    value: str


class CardInput(_Out):
    """Free text an action needs from the user before it can be sent (e.g. the answer to the owner's
    question). The host collects it and calls the tool with `{**arguments, key: text}`."""

    key: str = Field(description="Tool argument the text is sent as.")
    label: str
    max_length: int | None = Field(default=None, ge=1)


class CardAction(_Out):
    """A follow-up the host may offer as a button (calls a tool with these arguments, plus the text
    collected for `input` when set)."""

    label: str
    tool: str
    arguments: dict[str, JsonData] = Field(default_factory=dict)
    input: CardInput | None = Field(default=None, exclude_if=lambda value: value is None)
    """Omitted from the payload when unset, so actions without input keep their wire shape."""


class Card(_Out):
    """Display-only payload rendered by the `ui://askonce/*` cards or as text by other hosts."""

    kind: Literal["business", "items", "item", "policy", "slots", "commitment", "task", "handoff", "notice"]
    title: str
    subtitle: str | None = None
    lines: list[CardLine] = Field(default_factory=list)
    badges: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    actions: list[CardAction] = Field(default_factory=list)


class HandOff(_Out):
    """Offer to ask the owner instead of guessing."""

    reason: Literal["unknown", "disputed", "not_offered", "needs_owner"]
    approver_spoken_name: str
    suggested_question: str = Field(max_length=300)
    fact_ref: str | None = None
    tool: Literal["ask_owner"] = "ask_owner"


class Grounded(_Out):
    """Base of every tool result (see module docstring)."""

    speak: SpokenText = Field(description="Short reply to read aloud. No URLs.")
    facts: list[Fact] = Field(default_factory=list)
    card: Card | None = None
    handoff: HandOff | None = None
    business_id: str | None = None
    profile_revision: int | None = Field(default=None, description="Profile revision the facts come from.")
    trace_id: str | None = Field(default=None, description="OpenTelemetry trace id (32 hex) for this call.")


# ---------------------------------------------------------------------------- shared views


class PriceView(_Out):
    kind: Literal["fixed", "from", "range", "quote_only", "free"]
    currency: str | None = None
    amount_cents: int | None = None
    max_cents: int | None = None
    per: str | None = None


class SlotView(_Out):
    kind: Literal["time", "day"]
    start: str | None = Field(default=None, description="Local ISO-8601 start with offset (time slots).")
    end: str | None = None
    date: str | None = Field(default=None, description="Local date (day slots).")
    label_spoken: str
    resource_id: str | None = None
    resource_spoken_name: str | None = None
    remaining: int | None = None


# ---------------------------------------------------------------------------- discovery


class BusinessMatch(_Out):
    business_id: str
    name: str
    spoken_name: str
    summary_spoken: str | None = None
    categories: list[str]
    locality: str | None = None
    score: float
    matched_on: list[str] = Field(default_factory=list, description="Why it matched: category, item, dietary, locality.")


class FindBusinessResult(Grounded):
    businesses: list[BusinessMatch] = Field(default_factory=list)


class HoursLine(_Out):
    days: list[str]
    open: str
    close: str


class BusinessInfoResult(Grounded):
    name: str | None = None
    open_now: bool | None = Field(default=None, description="None when hours are unknown or disputed.")
    hours: list[HoursLine] | None = Field(default=None, description="None when unknown or withheld (disputed).")
    address: str | None = None
    phone: str | None = None


# ---------------------------------------------------------------------------- catalog & knowledge


class ItemSummary(_Out):
    item_ref: str
    name: str
    spoken_name: str
    price: PriceView | None = None
    availability: str | None = None
    category_ids: list[str] = Field(default_factory=list)


class CatalogResult(Grounded):
    items: list[ItemSummary] = Field(default_factory=list)
    total: int = 0


def _unset(value: object) -> bool:
    return value is None


class OptionChoiceView(_Out):
    choice_id: str
    spoken_name: str
    price: PriceView | None = None
    price_delta: PriceView | None = None
    allergens: list[str] | None = Field(
        default=None, exclude_if=_unset, description="Owner-confirmed allergens of this choice (matched options only)."
    )
    dietary: list[str] | None = Field(
        default=None, exclude_if=_unset, description="Owner-confirmed dietary tags of this choice (matched options only)."
    )
    serves: int | None = Field(default=None, exclude_if=_unset, description="How many it serves (matched options only).")
    """The three fields above are omitted from the payload when unset, so `options` keeps its wire shape."""


class OptionGroupView(_Out):
    group_id: str
    name: str
    required: bool = False
    choices: list[OptionChoiceView] = Field(default_factory=list)


class ItemDetailResult(Grounded):
    item: ItemSummary | None = None
    description_spoken: str | None = None
    duration_minutes: int | None = None
    options: list[OptionGroupView] = Field(default_factory=list)
    allergens: list[str] | None = Field(default=None, description="Owner-confirmed; None = unknown.")
    may_contain: list[str] | None = None
    dietary: list[str] | None = Field(default=None, description="Owner-confirmed; None = unknown.")
    facility_may_contain: list[str] | None = None
    offered_via: list[str] = Field(default_factory=list, description="Module ids that sell/book this item.")
    matched_options: list[OptionGroupView] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
        description=(
            "Option choices the customer's words point at: named ('10 inch'), or meeting a dietary need "
            "('gluten-free') or a head count ('for 30'). Omitted when there are none."
        ),
    )


class PolicyResult(Grounded):
    entry_ref: str | None = None
    topic: str | None = None


# ---------------------------------------------------------------------------- transactions


class AvailabilityResult(Grounded):
    module_id: str | None = None
    item_ref: str | None = None
    slots: list[SlotView] = Field(default_factory=list)


class ServiceAreaResult(Grounded):
    module_id: str | None = None
    served: bool | None = Field(default=None, description="None = cannot tell; the owner can confirm.")


CommitmentOutcome = Literal[
    # stored commitment states
    "confirmed",
    "pending_owner",
    "approved",
    "declined",
    "counter_offer",
    "suggested_time",
    "quoted",
    "info_requested",
    "answered",
    "cancelled",
    "expired",
    # nothing stored
    "not_confirmed",
    "needs_info",
    "unavailable",
    "not_offered",
]


class CommitmentView(_Out):
    reference: str = Field(description="Booking/order/request reference (the commitment id).")
    kind: Literal["booking", "custom_order", "quote_request", "owner_question"]
    status: str
    item_ref: str | None = None
    item_name: str | None = None
    start: str | None = None
    date: str | None = None
    when_spoken: str | None = None
    party_size: int | None = None
    choices: dict[str, str] = Field(default_factory=dict)
    price: PriceView | None = None
    decision: str | None = None
    owner_note: str | None = None
    quote: PriceView | None = None
    answer: str | None = Field(default=None, description="Owner's answer (owner questions).")
    expires_at: str | None = None


class CommitmentResult(Grounded):
    """Output of book, cancel_booking, place_custom_order, request_quote and ask_owner.

    One schema for every path (synchronous answer, sync fallback while pending, and the final task
    result rendered after the owner decides), so task results validate against the tool's outputSchema.
    """

    status: CommitmentOutcome
    reference: str | None = None
    task_id: str | None = Field(
        default=None, description="Owner-approval task. Clients without the tasks extension poll get_task."
    )
    commitment: CommitmentView | None = None
    alternatives: list[SlotView] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    replayed: bool = Field(default=False, description="True if this was an idempotent retry of an earlier call.")


class TaskStatusResult(Grounded):
    task_id: str
    task_status: Literal["working", "input_required", "completed", "failed", "cancelled"]
    status_message: str | None = None
    commitment: CommitmentView | None = None
    result: dict[str, JsonData] | None = Field(default=None, description="Final structured result once completed.")


class RequestStatusResult(Grounded):
    """Output of check_request_status: one request's status for any client (no tasks extension needed).

    `status` is the request's own status (e.g. `pending_owner`, `approved`, `confirmed`, `cancelled`);
    `task_status` and `result` are present while the request has an owner task (as in get_task)."""

    found: bool = Field(description="False when no request of yours matches what was given.")
    status: str | None = Field(default=None, description="The request's own status, e.g. pending_owner, approved, confirmed.")
    reference: str | None = None
    task_id: str | None = None
    task_status: Literal["working", "input_required", "completed", "failed", "cancelled"] | None = None
    status_message: str | None = None
    commitment: CommitmentView | None = None
    result: dict[str, JsonData] | None = Field(default=None, description="Final structured result once completed.")


class CancelRequestResult(CommitmentResult):
    """Output of cancel_request: a `CommitmentResult`, plus the token that confirms the cancellation for
    clients that cannot answer a confirmation form."""

    confirm_token: str | None = Field(
        default=None,
        description=(
            "Present when nothing was cancelled yet: read speak to the customer and, only if they say yes, "
            "call cancel_request again with the same arguments plus this confirm_token."
        ),
    )
