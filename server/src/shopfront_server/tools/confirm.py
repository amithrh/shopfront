"""MRTR confirmation and clarifying questions (Resolve + Elicit), shared by all transactional tools.

Protocol 2026-07-28 has no server-to-client requests, so `ctx.elicit()` fails there. Every question is
a `Resolve` resolver returning `Elicit(message, Model)`; the SDK turns it into an `InputRequiredResult`
round on 2026-07-28 and into a synchronous `elicitation/create` on legacy (stateful SSE) connections.

Verified pattern (scratch probe, mcp 2.2.0, both eras):

    async def x_plan(<tool args by name>, ctx: Context) -> CommitPlan             # plain value
    async def x_intake(plan: Annotated[CommitPlan, Resolve(x_plan)], ctx: Context)
        -> Elicit | AcceptedElicitation                # NO_INTAKE when nothing is missing
    async def x_final(<tool args>, intake: Annotated[ElicitationResult[BaseModel], Resolve(x_intake)],
                      ctx: Context) -> CommitPlan     # re-plan with the answers merged in
    async def x_confirm(plan: Annotated[CommitPlan, Resolve(x_final)], ctx: Context)
        -> Elicit | DeclinedElicitation                # SKIPPED when the plan is not ok

    tool(..., plan: Annotated[CommitPlan, Resolve(x_final)],
              confirmation: Annotated[ElicitationResult[ConfirmCommit], Resolve(x_confirm)])

Rules learned from the SDK (docs/research/mcp-server-core.md):
  * use the union form `ElicitationResult[...]`, NOT `ElicitationResult[...] | None` (the `| None`
    silently switches to the unwrapped form, where a decline aborts the tool with is_error);
  * skip a question by returning a plain `AcceptedElicitation`/`DeclinedElicitation` instance;
  * messages and schemas must be deterministic across rounds (no timestamps, no capacity counts),
    or the SDK discards the answer and asks again until the 10-round cap;
  * elicitation schemas must be flat: str/int/float/bool/date, `Literal[...]`, `list[Literal[...]]`;
  * resolvers re-run every round and must be side-effect free (no writes before the tool body).

Every question carries profile text straight to the customer, outside any tool result, so every
`Elicit` is built by `guarded_elicit` (pinned by tests/test_architecture.py): the L8 canary check on
the message and on the form exactly as sent, with generic fallbacks that are checked the same way.

A plain value returned by a resolver reaches its consumer wrapped once more as
`AcceptedElicitation(data=<value>)` (`mcp.server.mcpserver.resolve._accepted`), so `NO_INTAKE` and
`SKIPPED_CONFIRMATION` arrive nested; `outcome()` peels that wrapper and every helper here uses it.

The second half of this module is the tool body every transactional tool shares (`commit_flow`):
problems -> grounded unavailable/needs_info; not confirmed -> `not_confirmed` (no writes); otherwise
`ensure_task` (when the owner must decide) -> one atomic commit -> `adopt` -> `CommitmentResult`.
"""

from __future__ import annotations

import datetime as _dt
import functools
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, create_model

from mcp.server.elicitation import render_elicitation_schema
from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    Context,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
)
from mcp_tasks import ANONYMOUS_PRINCIPAL, TaskNotFound, TaskRecord
from shopfront_server.domain.models import SETTLED_STATUSES, Commitment
from shopfront_server.domain.planning import CommitOutcome, CommitPlan, IntakeFieldSpec, PlanProblem, ReadBackItem
from shopfront_server.domain.services.base import TASK_TTL_SLACK, owner_deadline, whole_ms
from shopfront_server.observability import id_digest
from shopfront_server.profile.model import JsonObj, Profile
from shopfront_server.tools import grounding, render, speech
from shopfront_server.tools.catalog import resolve_item, unmet_result
from shopfront_server.tools.guard import payload_has_canary
from shopfront_server.tools.context import finish, get_deps, live_profile, tool_span
from shopfront_server.tools.schemas import Card, CardAction, CardLine, CommitmentResult

logger = logging.getLogger("shopfront.tools")

ALWAYS_TASK_KINDS = frozenset({"quote_request", "owner_question"})
"""Kinds that always wait for the owner, whatever the approval rules say."""

TASK_TTL_SLACK_MS = whole_ms(TASK_TTL_SLACK)
"""A task outlives its commitment's last deadline by a day, so the expiry result can still be polled."""

READ_BACK_TEXT_MAX = 80

READ_BACK_BUDGET = 360
"""Hard cap on a spoken confirmation (about 25 seconds; most are under 300 characters). The summary, the
approval reason and the closing question always fit; `say_before_confirm` sentences that apply to this
request are added while there is room (ask_policy reads any policy in full)."""

_PRIORITY_READ_BACK = frozenset({"item", "choices", "when", "price", "party_size", "visit_window"})
"""Read-back keys kept when the summary itself is too long (the rest are dropped from the end)."""

_PARENTHETICAL = re.compile(r"\s*\([^)]*\)")
_AT_OR_ABOVE = re.compile(r"\b(or more|or over|and over|and up|over|above|at least|larger)\b")
_BELOW = re.compile(r"\b(under|less than|fewer than|below|smaller)\b")

SAFE_INTAKE_MESSAGE = "I need a few more details to continue."
SAFE_CONFIRMATION = "Shall I go ahead with this request?"
"""Used when profile text in an elicitation would carry an injection canary (L8)."""

M = TypeVar("M", bound=BaseModel)

class ConfirmCommit(BaseModel):
    """The confirmation form: one boolean. 'No' (or decline/cancel) means nothing is committed."""

    confirm: bool = Field(description="Yes to go ahead")


class _NoIntake(BaseModel):
    """Placeholder answer when no intake question was needed."""


NO_INTAKE: AcceptedElicitation[BaseModel] = AcceptedElicitation(action="accept", data=_NoIntake())
"""Return from an intake resolver to skip the question (plain value = already resolved)."""

SKIPPED_CONFIRMATION: DeclinedElicitation = DeclinedElicitation(action="decline")
"""Return from a confirmation resolver when the plan has problems (the body reports them instead)."""


def guarded_elicit(profile: Profile, message: str, model: type[M], *fallbacks: tuple[str, type[M]]) -> Elicit[M] | None:
    """`Elicit(message, model)`, or the first of `fallbacks` when that carries one of the business's
    injection canaries (L8); None when every candidate does (the caller then asks nothing).

    The only place an `Elicit` is built (tests/test_architecture.py). Each candidate is checked exactly
    as the client receives it, on both eras: the message plus every string and property name of the
    rendered form (`render_elicitation_schema`, what the SDK sends), as decoded text. Deterministic for
    the same profile, so the question is the same in every MRTR round."""
    for text, form in ((message, model), *fallbacks):
        if not payload_has_canary(profile, text, render_elicitation_schema(form)):
            return Elicit(text, form)
    return None


def outcome(answer: Any) -> Any:
    """Peel the SDK's plain-value wrapper: `Accepted(data=Accepted|Declined|Cancelled)` -> the inner one."""
    current = answer
    while isinstance(current, AcceptedElicitation) and isinstance(
        current.data, (AcceptedElicitation, DeclinedElicitation, CancelledElicitation)
    ):
        current = current.data
    return current


# ---------------------------------------------------------------------------- intake form


class _IntakeForm(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _choice_type(spec: IntakeFieldSpec) -> Any:
    values = tuple(v for v, _label in spec.options)
    one_of = [{"const": v, "title": label or v} for v, label in spec.options]
    choice = Literal[values]  # type: ignore[valid-type]
    if spec.type == "multi_choice":
        return Annotated[list[choice], WithJsonSchema({"type": "array", "items": {"anyOf": one_of}})]  # type: ignore[valid-type]
    return Annotated[choice, WithJsonSchema({"type": "string", "oneOf": one_of})]


def _field_type(spec: IntakeFieldSpec) -> tuple[Any, dict[str, Any]]:
    """(annotation, extra Field kwargs) for one spec; see `intake_form_model` for the mapping."""
    constraints: dict[str, Any] = {}
    if spec.type in ("text", "long_text", "time", "phone", "email"):
        if spec.max_length:
            constraints["max_length"] = spec.max_length
        if spec.type == "email":
            constraints["json_schema_extra"] = {"format": "email"}
        return str, constraints
    if spec.type in ("integer", "number", "money"):
        if spec.min is not None:
            constraints["ge"] = int(spec.min) if spec.type == "integer" else spec.min
        if spec.max is not None:
            constraints["le"] = int(spec.max) if spec.type == "integer" else spec.max
        return (int if spec.type == "integer" else float), constraints
    if spec.type == "boolean":
        return bool, constraints
    if spec.type == "date":
        return _dt.date, constraints
    if spec.type in ("choice", "multi_choice") and spec.options:
        return _choice_type(spec), constraints
    return str, constraints


def _safe_model_name(name: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z]+", " ", name).title().replace(" ", "")
    return cleaned if cleaned and cleaned[0].isalpha() else f"Form{cleaned}"


@functools.lru_cache(maxsize=256)
def _build_form(specs: tuple[IntakeFieldSpec, ...], model_name: str) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for index, spec in enumerate(specs):
        annotation, constraints = _field_type(spec)
        description = spec.spoken_prompt or spec.label
        if spec.required:
            fields[f"f{index}"] = (annotation, Field(alias=spec.key, title=spec.label, description=description, **constraints))
        else:
            fields[f"f{index}"] = (
                annotation | None,
                Field(default=None, alias=spec.key, title=spec.label, description=description, **constraints),
            )
    return create_model(  # type: ignore[call-overload, no-any-return]
        _safe_model_name(model_name),
        __base__=_IntakeForm,
        __doc__=None,
        **fields,
    )


def intake_form_model(specs: Sequence[IntakeFieldSpec], *, model_name: str) -> type[BaseModel]:
    """A flat pydantic model for the missing intake fields (deterministic for the same specs).

    Type mapping (see docs/design/profile-schema.md): text/long_text/time/phone/email -> str
    (+ max_length), integer -> int (+ge/le), number/money -> float, boolean -> bool,
    date -> datetime.date, choice -> Literal[values], multi_choice -> list[Literal[values]].
    Field descriptions come from `spoken_prompt` or `label`. Uses `pydantic.create_model`.

    Property names on the wire are the intake keys (field aliases; the Python names are `f0..fn`
    so no key can shadow a BaseModel attribute). Choices render as titled enums (`oneOf` const/title),
    so hosts show the profile's labels. Models are cached per `(specs, model_name)`.
    """
    return _build_form(tuple(specs), model_name)


def _item_name(profile: Profile, plan: CommitPlan) -> str | None:
    return render.item_spoken_name(profile, plan.item_ref)


def _intake_message(profile: Profile, plan: CommitPlan) -> str:
    missing = list(plan.missing)
    if len(missing) == 1 and missing[0].spoken_prompt:
        return missing[0].spoken_prompt
    item = _item_name(profile, plan)
    purpose = {
        "book": f"To book your {item}" if item else "To book this",
        "custom_order": f"To order your {item}" if item else "To place your order",
        "quote_request": f"To request a quote for {item}" if item else "To request your quote",
    }.get(plan.operation, "To continue")
    return f"{purpose}, I need a few details: {speech.join([m.label.lower() for m in missing])}."


def _plain_spec(spec: IntakeFieldSpec) -> IntakeFieldSpec:
    """`spec` without profile wording (key as label, option values as labels): the canary fallback."""
    title = spec.key.replace("_", " ")
    return spec.model_copy(update={"label": title, "spoken_prompt": None, "options": tuple((v, v) for v, _ in spec.options)})


def intake_question(profile: Profile, plan: CommitPlan) -> Elicit[BaseModel] | AcceptedElicitation[BaseModel]:
    """Ask for `plan.missing` in one form, or return `NO_INTAKE`. Skipped when the plan already has
    problems (no point asking for a phone number for a closed day).

    The message and the form carry profile text (prompts, labels, option labels, keys) straight to the
    customer, outside any tool result, so the L8 canary check runs here too (`guarded_elicit`): on a
    hit the question is asked with generic wording (`SAFE_INTAKE_MESSAGE`, keys as labels). If even
    that form carries a canary (in a key or an option value), nothing is asked: the plan keeps its
    missing fields and the tool answers `needs_info`, a tool result the output guard checks."""
    if plan.problems or not plan.missing:
        return NO_INTAKE
    specs = tuple(plan.missing)
    asked: Elicit[BaseModel] | None = guarded_elicit(
        profile,
        _intake_message(profile, plan),
        intake_form_model(specs, model_name=f"{plan.module_id} details"),
        (
            SAFE_INTAKE_MESSAGE,
            intake_form_model(tuple(_plain_spec(spec) for spec in specs), model_name=f"{plan.module_id} plain details"),
        ),
    )
    return asked if asked is not None else NO_INTAKE


_RETRY_PROBLEMS = frozenset({"capacity_full"})


async def _possible_retry(ctx: Context[Any, Any], plan: CommitPlan) -> bool:
    """True if the caller already holds a live commitment for this item and slot/day.

    Read-only (resolvers stay side-effect free). It decides whether the intake form must still be
    asked although the slot looks full: the form answers are part of the idempotency key, so only
    with them can a retry be recognised and replayed instead of reported as "already taken".
    """
    deps = get_deps(ctx)
    found, _cursor = await deps.commitments.list_for_business(plan.business_id, limit=100)
    for commitment in found:
        if (
            commitment.principal == plan.principal
            and commitment.module_id == plan.module_id
            and commitment.item_ref == plan.item_ref
            and commitment.status not in SETTLED_STATUSES
            and (
                (plan.window is not None and commitment.window == plan.window)
                or (plan.window is None and plan.day is not None and commitment.day == plan.day)
            )
        ):
            return True
    return False


async def intake_for(ctx: Context[Any, Any], plan: CommitPlan) -> Elicit[BaseModel] | AcceptedElicitation[BaseModel]:
    """The intake step every transactional resolver chain uses: `intake_question`, except that a
    plan whose only problem is a full slot still asks for the missing fields when this may be a retry
    of the caller's own earlier request (see `_possible_retry`); the body then replays it."""
    profile = await live_profile(get_deps(ctx), plan.business_id)
    if (
        plan.missing
        and plan.problems
        and all(p.code in _RETRY_PROBLEMS for p in plan.problems)
        and await _possible_retry(ctx, plan)
    ):
        return intake_question(profile, plan.model_copy(update={"problems": ()}))
    return intake_question(profile, plan)


def merge_intake(details: Mapping[str, Any] | None, answer: ElicitationResult[BaseModel]) -> dict[str, Any]:
    """Tool-supplied details overlaid with accepted form answers (dates as ISO strings)."""
    merged: dict[str, Any] = dict(details or {})
    answer = outcome(answer)
    if isinstance(answer, AcceptedElicitation) and isinstance(answer.data, BaseModel):
        if not isinstance(answer.data, _NoIntake):
            merged.update(answer.data.model_dump(mode="json", by_alias=True, exclude_none=True))
    return merged


# ---------------------------------------------------------------------------- read-back


def _intake_spec(profile: Profile, module_id: str, key: str) -> JsonObj | None:
    try:
        for spec in profile.intake_fields(module_id):
            if spec.get("key") == key:
                return spec
    except Exception:  # noqa: BLE001 - unknown module: fall back to generic wording
        return None
    return None


def _visit_window_name(profile: Profile, module_id: str, window_id: str) -> str:
    try:
        windows = profile.module(module_id).get("visit_windows", ())
    except Exception:  # noqa: BLE001
        windows = ()
    for window in windows:
        if window.get("id") == window_id:
            return str(window.get("spoken_name") or window_id)
    return window_id.replace("-", " ")


def _intake_phrase(profile: Profile, plan: CommitPlan, key: str, value: Any) -> str | None:
    spec = _intake_spec(profile, plan.module_id, key)
    label = str((spec or {}).get("label") or key.replace("_", " "))
    kind = (spec or {}).get("type", "text")
    if value is None or value == "" or value == []:
        return None
    if kind == "date":
        try:
            return f"for {speech.day(_dt.date.fromisoformat(str(value)))}"
        except ValueError:
            return f"{label.lower()} {value}"
    if kind == "time":
        try:
            return f"at {speech.hhmm(str(value))}"
        except ValueError:
            return f"{label.lower()} {value}"
    if kind in ("integer", "number"):
        if label.lower().startswith("number of "):
            return f"{value} {label[len('number of '):].lower()}"
        return f"{label.lower()} {value}"
    if kind == "money" and isinstance(value, int):
        return f"{label.lower()} {speech.money(value, profile.currency)}"
    if kind == "boolean":
        return f"{label.rstrip('?').lower()}: {'yes' if value else 'no'}"
    if kind in ("choice", "multi_choice"):
        labels = {str(o.get("value")): str(o.get("label") or o.get("value")) for o in (spec or {}).get("options", ())}
        values = value if isinstance(value, list) else [value]
        words = [_PARENTHETICAL.sub("", labels.get(str(v), str(v))).strip() or str(v) for v in values]
        if all(_is_plain_phrase(w) for w in words):
            return speech.join([_lower_first(w) for w in words])  # "weekday morning" says it all
        return f"{label.lower()}: {speech.join(words)}"  # "stylist: Wren Abernathy", "contact: Text"
    if kind == "phone":
        return f"{label.lower()} {speech.phone(str(value))}"
    text = " ".join(str(value).split()).strip().strip('"\u201c\u201d').rstrip(".!?")
    if " " not in text and len(text) <= 16:
        return f"{label.lower()} {text}"  # a code or a single word: "zip code 80211"
    text = speech.truncate(text, READ_BACK_TEXT_MAX)
    if label.split(" ", 1)[0].lower() in _PROMPT_WORDS:
        return _lower_first(text)  # "describe the job" is a prompt, not a name: read the answer alone
    return f"{label.lower()}: {text}"  # "message on the cake: Happy birthday Sam"


_PROMPT_WORDS = frozenset({"describe", "tell", "explain", "what", "how", "why", "which", "anything", "any", "list", "please"})
"""First words of field labels that are prompts ("Describe the job") rather than names of a value."""


def _is_plain_phrase(text: str) -> bool:
    """Several words, only the first capitalised ("Weekday morning", not "Wren Abernathy" or "Text")."""
    words = text.split()
    return len(words) > 1 and all(w[:1].islower() or not w[:1].isalpha() for w in words[1:])


def _lower_first(text: str) -> str:
    """'Weekday morning' -> 'weekday morning'; 'ZIP code' and 'I' stay as written."""
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def _read_back_phrase(profile: Profile, plan: CommitPlan, item: ReadBackItem) -> str | None:
    key = item.key
    if key == "item":
        return _item_name(profile, plan) or (str(item.value) if item.value else None)
    if key == "choices":
        names = render.choice_spoken_names(profile, plan.item_ref, plan.choices)
        return speech.join(list(names.values())) or None
    if key == "when":
        if plan.window is not None:
            return speech.when(plan.window.start.astimezone(profile.timezone))
        if plan.day is not None:
            return f"for {speech.day(plan.day)}"
        return None
    if key == "price":
        return speech.price(plan.price) if plan.price is not None else None
    if key == "party_size":
        size = plan.party_size or (item.value if isinstance(item.value, int) else None)
        if not size:
            return None
        return f"for {size} {'person' if size == 1 else 'people'}"
    if key == "visit_window":
        window_id = plan.visit_window_id or (str(item.value) if item.value else None)
        return _visit_window_name(profile, plan.module_id, window_id) if window_id else None
    value = plan.intake.get(key, item.value)
    return _intake_phrase(profile, plan, key, value)


def _closing_question(profile: Profile, plan: CommitPlan) -> str:
    if plan.approval.required or plan.kind in ALWAYS_TASK_KINDS:
        return f"Shall I send it to {render.approver(profile, plan.module_id)}?"
    return {
        "book": "Shall I book it?",
        "cancel_booking": "Shall I cancel it?",
        "custom_order": "Shall I place the order?",
    }.get(plan.operation, "Shall I go ahead?")


def _policy_sentences(profile: Profile, plan: CommitPlan) -> list[str]:
    """`answer_spoken` of each `say_before_confirm` entry that is statable (disputed ones, and entries
    whose safety facts are not owner-confirmed, are skipped)."""
    out: list[str] = []
    for ref in plan.say_before_confirm:
        try:
            entry = profile.knowledge_entry(ref)
            got, _facts = grounding.knowledge_answer(profile, ref, entry)
        except Exception:  # noqa: BLE001 - unknown entry or malformed ref: say nothing rather than guess
            continue
        if got.ok and isinstance(got.value, str) and got.value.strip():
            out.append(got.value.strip())
    return out


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _approval_thresholds(profile: Profile, plan: CommitPlan) -> set[str]:
    """The numeric `value`s of the module's approval rules ("25" for `servings >= 25`)."""
    try:
        rules = (profile.module(plan.module_id).get("approval") or {}).get("rules") or ()
    except Exception:  # noqa: BLE001 - unknown module: no thresholds
        return set()
    return {
        str(rule["value"]) for rule in rules if isinstance(rule.get("value"), int | float) and not isinstance(rule.get("value"), bool)
    }


def _applies(sentence: str, plan: CommitPlan, thresholds: set[str]) -> bool:
    """False for a policy sentence about the other side of an approval threshold: "Orders under 25
    servings need no deposit" is not read for a 30-serving order the owner must approve, and "For 25
    servings or more, a 50 percent deposit is due" is not read for a 10-serving one."""
    text = sentence.casefold()
    if not any(re.search(rf"\b{re.escape(value)}\b", text) for value in thresholds):
        return True
    above, below = bool(_AT_OR_ABOVE.search(text)), bool(_BELOW.search(text))
    if above == below:
        return True
    return plan.approval.required if above else not plan.approval.required


def _summary(plan: CommitPlan, phrases: list[tuple[str, str]], room: int) -> str:
    """The read-back phrases as one sentence, dropping non-priority phrases from the end if needed."""
    kept = list(phrases)
    while True:
        summary = ", ".join(p for _key, p in kept)
        text = speech.capitalize(f"cancel {summary}" if plan.operation == "cancel_booking" else summary) + "."
        droppable = [i for i, (key, _p) in enumerate(kept) if key not in _PRIORITY_READ_BACK]
        if len(text) <= room or not droppable:
            return text if summary else ""
        del kept[droppable[-1]]


def read_back_message(profile: Profile, plan: CommitPlan) -> str:
    """The spoken confirmation, within `READ_BACK_BUDGET` characters: the read-back summary, why the
    owner must approve (when they must), the first sentence of each `say_before_confirm` policy that
    fits, then the question.

    e.g. "Custom cake, ten inch round, vanilla bean, for Sunday, October 18, 30 servings, $120. Orders of
    25 servings or more need Rosa's approval, so I'll send your request to her. For 25 servings or
    more, a 50 percent deposit is due by card within 48 hours of Rosa's approval, and the rest at
    pickup. Shall I send it to Rosa?" Policy sentences about the other side of an approval threshold
    are skipped (`_applies`). The question names the approver when approval is required, else "Shall I
    book it?" / "Shall I place the order?". Intake values are read as phrases ("zip code 80211",
    "weekday morning", the customer's own description), never as quoted form labels. Deterministic for
    the same plan. Policy entries that are not statable (disputed or absent) are skipped.
    """
    phrases = [(item.key, p) for item in plan.read_back if (p := _read_back_phrase(profile, plan, item))]
    question = _closing_question(profile, plan)
    reason = ""
    if needs_owner(plan) and plan.approval.reasons_spoken:
        reason = _sentence((speech.split_sentences(plan.approval.reasons_spoken[0]) or [""])[0])
    fixed = len(question) + (len(reason) + 1 if reason else 0)
    summary = _summary(plan, phrases, max(READ_BACK_BUDGET - fixed - 1, 80))
    parts = [p for p in (summary, reason) if p]
    used = sum(len(p) + 1 for p in parts) + len(question)
    thresholds = _approval_thresholds(profile, plan)
    for policy in _policy_sentences(profile, plan):
        for sentence in speech.split_sentences(policy) or [policy]:
            sentence = _sentence(sentence)
            if _applies(sentence, plan, thresholds) and used + len(sentence) + 1 <= READ_BACK_BUDGET:
                parts.append(sentence)
                used += len(sentence) + 1
    parts.append(question)
    return " ".join(parts)


def confirmation_question(profile: Profile, plan: CommitPlan) -> Elicit[ConfirmCommit] | DeclinedElicitation:
    """`Elicit(read_back_message, ConfirmCommit)` for an ok plan, else `SKIPPED_CONFIRMATION`.

    The read-back embeds profile text (policy answers, names) outside any tool result, so the L8
    canary check runs here (`guarded_elicit`): on a hit a generic question naming the approver is
    asked, and if the approver's name carries the canary, the bare `SAFE_CONFIRMATION`. Should even
    that match (a canary inside this server's own wording), nothing is asked or committed."""
    if not plan.ok:
        return SKIPPED_CONFIRMATION
    asked = guarded_elicit(
        profile,
        read_back_message(profile, plan),
        ConfirmCommit,
        (f"{SAFE_CONFIRMATION} {_closing_question(profile, plan)}", ConfirmCommit),
        (SAFE_CONFIRMATION, ConfirmCommit),
    )
    return asked if asked is not None else SKIPPED_CONFIRMATION


async def confirmation_for(
    ctx: Context[Any, Any], plan: CommitPlan, service: CommitService
) -> Elicit[ConfirmCommit] | DeclinedElicitation:
    """The confirmation step every transactional resolver chain uses: `confirmation_question`, skipped
    when the plan replays an earlier identical request (it was confirmed then; `commit_flow` replays
    it without writing anything)."""
    profile = await live_profile(get_deps(ctx), plan.business_id)
    if plan.ok and await service.replay(plan) is not None:
        return SKIPPED_CONFIRMATION
    return confirmation_question(profile, plan)


def confirmed(answer: ElicitationResult[ConfirmCommit]) -> bool:
    """True only for an accepted form with `confirm=True`."""
    answer = outcome(answer)
    return isinstance(answer, AcceptedElicitation) and bool(getattr(answer.data, "confirm", False))


# ---------------------------------------------------------------------------- the shared tool body


class CommitService(Protocol):
    """What `commit_flow` needs from a domain service (`CommitmentServiceBase`)."""

    async def replay(self, plan: CommitPlan) -> CommitOutcome | None: ...

    async def with_generation(self, plan: CommitPlan) -> CommitPlan: ...

    async def commit(self, profile: Profile, plan: CommitPlan, *, task_id: str | None) -> CommitOutcome: ...


def needs_owner(plan: CommitPlan) -> bool:
    """True when the commitment waits for the owner (and therefore becomes a task)."""
    return plan.approval.required or plan.kind in ALWAYS_TASK_KINDS


def task_ttl_ms(
    profile: Profile, plan: CommitPlan, default_ms: int, *, now: _dt.datetime, keep_waiting_max: _dt.timedelta
) -> int:
    """How long (from `now`) the owner task of a request committed now must stay addressable: until the
    owner's last deadline (`owner_deadline`) plus `TASK_TTL_SLACK_MS`, so the final result can still be
    polled. That deadline is the commitment's expiry (`expires_after_minutes`); under `on_expiry:
    keep_waiting` it is the later `keep_waiting_horizon` (the request's service time, at most
    `keep_waiting_max` away), so the task is born long enough even if no expiry sweep ever runs. Always
    finite, so a decided request's task is garbage-collected. `default_ms` when the owner module sets
    no expiry."""
    deadline = owner_deadline(profile, plan, since=now, keep_waiting_max=keep_waiting_max)
    if deadline is None:
        return default_ms
    return whole_ms(deadline - now) + TASK_TTL_SLACK_MS


def task_arguments(plan: CommitPlan) -> dict[str, Any]:
    """What a task record keeps of the call: ids only. The raw tool arguments may hold personal data
    and unvalidated keys; the normalised request lives in the commitment."""
    return {"business_id": plan.business_id, "module_id": plan.module_id, "kind": plan.kind}


_CAPABILITY_LINES = frozenset({"Reference", render.BOOKING_CODE_LABEL})


def _redacted_replay(result: CommitmentResult, commitment_id: str) -> CommitmentResult:
    """A replay for an unauthenticated caller: status and speech only, no capabilities.

    Without auth every caller is 'anonymous', so anyone who repeats a request's details would
    otherwise receive its reference, booking code and task id (all bearer capabilities:
    cancel_booking, get_task, tasks/cancel). The code is also left out of the speech, by rendering
    the replay `with_code=False` (`_commitment_answer`); dropping its card line here is a backstop.

    Facts fixed in the commitment (an owner's note, a price the profile no longer publishes) name it in
    their `fact_ref` and evidence URN, and so do their card citations: there the id is replaced by its
    `observability.id_digest`, which still ties the fact to one commitment but cancels nothing."""
    alias = id_digest(commitment_id) or "redacted"

    def scrub(text: str | None) -> str | None:
        return text.replace(commitment_id, alias) if text and commitment_id else text

    facts = [fact.model_copy(update={"fact_ref": scrub(fact.fact_ref), "evidence_url": scrub(fact.evidence_url)}) for fact in result.facts]
    citations = [citation.model_copy(update={"url": scrub(citation.url)}) for citation in result.card.citations]
    lines = [line for line in result.card.lines if line.label not in _CAPABILITY_LINES]
    card = result.card.model_copy(update={"lines": lines, "actions": [], "citations": citations})
    return result.model_copy(
        update={"reference": None, "task_id": None, "card": card, "commitment": None, "facts": facts}
    )


def _withholds_capabilities(plan: CommitPlan, *, replayed: bool) -> bool:
    """True when this answer must not carry the commitment's task handle, task id or reference.

    Without auth every caller is 'anonymous', and the idempotency key is built from the request
    details alone, so a replay (sequential, or a concurrent duplicate whose commit lost to the first
    one) may well be a stranger who repeated someone else's request."""
    return replayed and plan.principal == ANONYMOUS_PRINCIPAL


async def _adopt(
    ctx: Context[Any, Any], plan: CommitPlan, task_id: str | None, record: TaskRecord | None, *, replayed: bool
) -> bool:
    """Answer this call with the commitment's task handle when the client can take one.

    Never for an unauthenticated replay (`_withholds_capabilities`): the handle carries the task id."""
    if not task_id or _withholds_capabilities(plan, replayed=replayed):
        return False
    deps = get_deps(ctx)
    if record is None or record.task_id != task_id:
        try:
            record = await deps.task_manager.get(task_id)
        except TaskNotFound:
            return False
    return deps.tasks.adopt(record)


async def _commitment_answer(
    ctx: Context[Any, Any],
    profile: Profile,
    plan: CommitPlan,
    commitment: Commitment,
    record: TaskRecord | None,
    *,
    replayed: bool,
) -> CommitmentResult:
    """The result for a live commitment, and its task handle for clients that can take one.

    The only place that hands out a commitment's capabilities: an unauthenticated replay gets
    neither the handle (`_adopt` refuses) nor the ids and booking code (`with_code=False`,
    `_redacted_replay`)."""
    await _adopt(ctx, plan, commitment.task_id, record, replayed=replayed)
    withhold = _withholds_capabilities(plan, replayed=replayed)
    result = render.commitment_result(profile, commitment, replayed=replayed, with_code=not withhold)
    if withhold:
        result = _redacted_replay(result, commitment.commitment_id)
    return result


async def commit_flow(
    ctx: Context[Any, Any],
    *,
    tool: str,
    service: CommitService,
    plan: CommitPlan,
    confirmation: ElicitationResult[ConfirmCommit] | None,
    asked: str | None = None,
) -> CommitmentResult:
    """The body of book / place_custom_order / request_quote / ask_owner (design section 7).

    1. an earlier identical request already committed -> replay it (its own hold may be what makes the
       slot look full); otherwise a plan with problems (or missing intake) -> grounded problem result;
    2. not confirmed (decline, cancel or `confirm=false`) -> `not_confirmed`, nothing written;
       `confirmation=None` means the tool needs no confirmation (ask_owner);
    3. owner approval needed -> `ensure_task` with the id derived from the idempotency key (a reused
       task gets its TTL refreshed, see `_ensure_task`);
    4. one atomic commit; a lost capacity race returns `unavailable` (the unadopted task is left to
       a retry or to expiry);
    5. `adopt` the task (handle for opted-in clients; everyone else gets this result with `task_id`),
       unless the commit was a `Duplicate` from an unauthenticated caller (see below).

    Replays only return *live* commitments (a settled request is made afresh as a new generation).
    An unauthenticated replay is never adopted and is redacted (`_commitment_answer`): without auth,
    repeating someone else's request details must not hand over their task handle, reference or task
    id. That covers both the sequential replay (step 1) and a concurrent duplicate whose replay check
    ran before the first request's commit landed (its commit then returns `Duplicate` in step 4).
    """
    deps = get_deps(ctx)
    with tool_span(tool, plan.business_id, module_id=plan.module_id):
        profile = await live_profile(deps, plan.business_id)
        today = deps.clock.now().astimezone(profile.timezone).date()
        attrs: dict[str, Any] = {"module_id": plan.module_id}
        replay = await service.replay(plan)
        if replay is not None and replay.commitment is not None:
            # An identical earlier request already committed (after its own confirmation) and is still
            # live: return it whatever capacity or the confirmation say now. Nothing new is written.
            commitment = replay.commitment
            result = await _commitment_answer(ctx, profile, plan, commitment, None, replayed=True)
            return finish(
                result,
                profile,
                outcome="replayed",
                commitment_id=commitment.commitment_id,
                task_id=commitment.task_id,
                idempotent_replay=True,
                **attrs,
            )
        if not plan.ok:
            ambiguous = ambiguous_item_result(profile, plan, tool=tool, asked=asked)
            if ambiguous is not None:
                return finish(ambiguous, profile, outcome="ambiguous", **attrs)
            result = render.problem_result(profile, plan, asked=asked, today=today)
            return finish(result, profile, outcome=result.status, **attrs)
        if confirmation is not None and not confirmed(confirmation):
            return finish(render.not_confirmed_result(profile, plan), profile, outcome="not_confirmed", **attrs)

        # A request whose earlier attempt was settled (cancelled, declined, quoted...) is made afresh.
        plan = await service.with_generation(plan)
        if not plan.ok:
            # Every attempt at this exact request was already settled (`too_many_attempts`): nothing is
            # written, and the last settled outcome is never replayed in its place.
            result = render.problem_result(profile, plan, asked=asked, today=today)
            return finish(result, profile, outcome=result.status, **attrs)
        record: TaskRecord | None = None
        if needs_owner(plan):
            record = await _ensure_task(ctx, profile, plan)
        committed = await service.commit(profile, plan, task_id=record.task_id if record else None)
        if committed.commitment is None:
            # Lost a capacity race. The task (if any) is left alone: no client was given it, and it is
            # keyed by this attempt, so an identical retry reuses it once the slot frees up; otherwise
            # it simply expires. Cancelling it would poison that retry.
            problem = committed.problem or PlanProblem(code="capacity_full", detail="commit refused")
            result = render.problem_result(
                profile, plan.model_copy(update={"problems": (problem,)}), asked=asked, today=today
            )
            return finish(result, profile, outcome=result.status, **attrs)

        commitment = committed.commitment
        # A `Duplicate` here is a concurrent identical request that committed first: for an anonymous
        # caller it is redacted and not adopted, exactly like the sequential replay above.
        result = await _commitment_answer(ctx, profile, plan, commitment, record, replayed=committed.replayed)
        return finish(
            result,
            profile,
            outcome="replayed" if committed.replayed else commitment.status,
            commitment_id=commitment.commitment_id,
            task_id=commitment.task_id,
            idempotent_replay=committed.replayed,
            **attrs,
        )


_MODULE_TYPE_OF_KIND = {"booking": "booking", "custom_order": "custom_order", "quote_request": "quote_request"}


def ambiguous_item_result(profile: Profile, plan: CommitPlan, *, tool: str, asked: str | None) -> CommitmentResult | None:
    """'Did you mean women's cut and style, men's cut or men's cut and beard trim?' for a plan whose item
    did not resolve because several items match what the customer said, or one only once a misspelt
    word is corrected (the same resolution
    `check_availability` and `find_item` use), else None. Nothing is stored; the card offers each
    candidate as a retry of the same tool. A dietary need no candidate is confirmed to meet ("a
    nut-free cake") is a `not_offered` hand-off instead."""
    module_type = _MODULE_TYPE_OF_KIND.get(plan.kind)
    if not asked or module_type is None or not plan.problems or plan.problems[0].code != "unknown_item":
        return None
    lookup = resolve_item(profile, asked, module_type=module_type)
    if lookup.unmet_dietary:
        # "a nut-free cake": the items exist, but none is confirmed to meet the need. Nothing is
        # ordered as the plain item; the owner can be asked instead.
        return unmet_result(profile, lookup, asked=asked, result_type=CommitmentResult, status="not_offered")
    if lookup.found is not None or not lookup.candidates:
        return None  # (one candidate: a misspelling the customer should confirm, "Did you mean water heater ...?")
    names = [c.spoken_name for c in lookup.candidates]
    facts = grounding.facts_for(profile, [f"{c.ref}#spoken_name" for c in lookup.candidates])
    return CommitmentResult(
        speak=speech.sentences(f"Did you mean {speech.join(names, conjunction='or')}?"),
        facts=facts,
        card=Card(
            kind="items",
            title="Which one?",
            subtitle=profile.name,
            lines=[CardLine(label=c.name, value=c.spoken_name) for c in lookup.candidates],
            actions=[
                CardAction(label=c.name, tool=tool, arguments={"business_id": profile.profile_id, "item": c.ref})
                for c in lookup.candidates
            ],
            citations=grounding.citations_for(profile, facts),
        ),
        business_id=profile.profile_id,
        profile_revision=profile.revision,
        status="needs_info",
        missing_fields=["item"],
    )


async def _ensure_task(ctx: Context[Any, Any], profile: Profile, plan: CommitPlan) -> TaskRecord:
    """The owner task for this attempt, keyed by its idempotency key (so a retry reuses it).

    A reused live record is one an earlier attempt left unadopted (its commit lost a capacity race, or
    it crashed before committing) or a concurrent identical call's. Its TTL is refreshed
    (`refresh_ttl`): this attempt's commitment gets its own expiry, counted from now, so a retry long
    after the lost race must not inherit a task that dies before the owner's deadline (the customer's
    handle, `get_task` and `request_info` would go with it). A TTL is only ever extended.

    A finished record under that id cannot carry a new commitment (its outcome is already final), so
    in that rare case a fresh random id is minted instead.

    The labels name the commitment this attempt creates (the same id `CommitmentServiceBase.commit`
    derives from the key), so the cancel hook finds it by primary key rather than through the eventually
    consistent task index. Labels are stored with the task but never sent to clients."""
    deps = get_deps(ctx)
    options: dict[str, Any] = {
        "status_message": render.waiting_message(profile, plan.module_id),
        "ttl_ms": task_ttl_ms(
            profile,
            plan,
            deps.settings.task_default_ttl_ms,
            now=deps.clock.now(),
            keep_waiting_max=_dt.timedelta(days=deps.settings.keep_waiting_max_days),
        ),
        "poll_interval_ms": deps.settings.task_poll_interval_ms,
        "labels": {
            "business_id": plan.business_id,
            "commitment_id": deps.ids.commitment_id(plan.idempotency_key),
            "kind": plan.kind,
            "module_id": plan.module_id,
        },
        "arguments": task_arguments(plan),
    }
    record = await deps.tasks.ensure_task(task_id=deps.ids.task_id(plan.idempotency_key), refresh_ttl=True, **options)
    if record.terminal:
        logger.warning("derived task id already finished; minting a fresh one", extra={"module_id": plan.module_id})
        record = await deps.tasks.ensure_task(**options)
    return record
