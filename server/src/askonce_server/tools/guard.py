"""Last line of defence on every tool result (applied by `tools.server` to every tool's output).

1. L8 canaries: if any injection-canary quote of the business (normalised: NFC, casefolded,
   whitespace collapsed) appears in any string of the result, the result is replaced by a safe
   hand-off and the event is traced (`askonce.guard.canary`).
2. `speak` passes `schemas.check_spoken` (URLs/markup stripped, length capped at a sentence boundary)
   so a template bug degrades the wording instead of crashing the call.
3. Every `facts[].fact_ref` must parse; unparseable facts are dropped and traced.

The checks work on the JSON payload (`guard_payload`) so the same code guards a pydantic result
(`guard_result`, used by the task renderer) and a serialised `CallToolResult` (the middleware). A
replacement keeps every required field of the original, so it still validates against the tool's
output schema (which the client re-validates).

Profile text also reaches the customer outside tool results. Each path has its own check, all with the
same normalisation, always on decoded text (never on a JSON encoding, which escapes non-ASCII
characters and quotes):

  * MRTR / legacy elicitations: `tools.confirm.guarded_elicit` (message, and every string and property
    name of the form), the only place an elicitation is built; `OutputGuardMiddleware` is the backstop
    for 2026-07-28 `input_required` rounds (legacy `elicitation/create` never passes it);
  * task objects (`tools/call` handle, `tasks/get`): `TaskViewGuard`, the tasks extension's `present`
    hook, re-checks them against the business's current canaries.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, TypeVar

import mcp_types as types

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp_tasks import TaskRecord
from askonce_server import observability
from askonce_server.profile.factref import FACT_REF_PATTERN
from askonce_server.profile.model import Profile
from askonce_server.profile.store import ProfileStore
from askonce_server.tools import speech
from askonce_server.tools.schemas import SPEAK_MAX, Grounded, HandOff, check_spoken

G = TypeVar("G", bound=Grounded)

logger = logging.getLogger("askonce.guard")

WITHHELD = "(withheld)"
"""Replaces any string that contained a canary (keeps string-typed fields schema-valid)."""

FALLBACK_SPEAK = "Sorry, I can't answer that right now."

_SPACES = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _SPACES.sub(" ", unicodedata.normalize("NFC", text).casefold()).strip()


def _normalised_canaries(canaries: Iterable[str]) -> list[str]:
    return sorted({n for n in (_norm(c) for c in canaries if c) if n})


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def strings_and_keys(value: Any) -> Iterator[str]:
    """Every string in a JSON-like `value`, and every dict key. An elicitation form carries profile
    text in its property names (intake keys) as well as in its values (titles, descriptions, option
    consts and titles)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from strings_and_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings_and_keys(item)


def _hits(texts: Iterable[str], normalised: list[str]) -> bool:
    """True if any normalised canary is a substring of any normalised text (decoded text only)."""
    if not normalised:
        return False
    for text in texts:
        folded = _norm(text) if text else ""
        if folded and any(canary in folded for canary in normalised):
            return True
    return False


def _payload_has_canary(payload: Any, normalised: list[str]) -> bool:
    return _hits(_strings(payload), normalised)


def contains_canary(result: Grounded, canaries: list[str]) -> bool:
    """True if any normalised canary is a substring of any normalised string in `result`."""
    return _payload_has_canary(result.model_dump(mode="json"), _normalised_canaries(canaries))


def text_has_canary(profile: Profile, *texts: str) -> bool:
    """True if any of `texts` contains one of the business's injection canaries (normalised as in
    `guard_payload`). Used where profile text reaches the customer outside a tool result: MRTR /
    legacy elicitation messages and forms (`tools.confirm`), which the output middleware never sees.
    When it fires, the event is traced on the active span."""
    if not _hits(texts, _normalised_canaries(profile.injection_canaries())):
        return False
    observability.set_attributes(None, **{"askonce.guard.canary": True})
    observability.mark_error(None, "injection canary blocked in an elicitation")
    logger.warning("injection canary blocked in an elicitation", extra={"business_id": profile.profile_id})
    return True


def payload_has_canary(profile: Profile, *payloads: Any) -> bool:
    """`text_has_canary` over every string and every key of each JSON-like payload (a message, an
    elicitation form's JSON schema, an MRTR round's `inputRequests`). The payload is walked as decoded
    text: a `json.dumps` copy would escape curly quotes, accents and double quotes and hide a canary."""
    return text_has_canary(profile, *(text for payload in payloads for text in strings_and_keys(payload)))


def sanitize_speak(text: str) -> str:
    """Strip URLs, markup and newlines; cap at 240 chars on a sentence/word boundary; never empty."""
    cleaned = speech.clean(text or "")
    if not cleaned:
        return FALLBACK_SPEAK
    if len(cleaned) > SPEAK_MAX:
        kept = speech.sentences(*speech.split_sentences(cleaned), limit=SPEAK_MAX)
        cleaned = kept or speech.truncate(cleaned, SPEAK_MAX)
    try:
        return check_spoken(cleaned)
    except ValueError:
        return FALLBACK_SPEAK


def _scrub(value: Any, normalised: list[str]) -> Any:
    if isinstance(value, str):
        return WITHHELD if any(c in _norm(value) for c in normalised) else value
    if isinstance(value, dict):
        return {k: _scrub(v, normalised) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, normalised) for v in value]
    return value


def safe_speak(approver: str | None) -> str:
    if approver:
        return f"Sorry, I can't share that answer. Want me to ask {approver}?"
    return FALLBACK_SPEAK


def guard_payload(
    payload: dict[str, Any], canaries: Iterable[str], *, approver: str | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Apply checks 1-3 to a result's JSON payload. Returns `(payload, events)`; `events` is empty when
    nothing changed (the input object is then returned as is)."""
    events: list[str] = []
    out = payload
    normalised = _normalised_canaries(canaries)
    if _payload_has_canary(payload, normalised):
        events.append("canary")
        if approver and _hits([approver], normalised):
            approver = None  # the replacement must not bring the canary back through the approver's name
        out = _scrub(payload, normalised)
        out["speak"] = safe_speak(approver)
        out["facts"] = []
        out["card"] = None
        if approver:
            out["handoff"] = HandOff(
                reason="needs_owner",
                approver_spoken_name=approver,
                suggested_question="Can you answer my question?",
            ).model_dump(mode="json")
        else:
            out["handoff"] = None
    speak = out.get("speak")
    if isinstance(speak, str):
        try:
            check_spoken(speak)
        except ValueError:
            events.append("speak_sanitized")
            out = dict(out) if out is payload else out
            out["speak"] = sanitize_speak(speak)
    facts = out.get("facts")
    if isinstance(facts, list):
        kept = [f for f in facts if isinstance(f, dict) and FACT_REF_PATTERN.match(str(f.get("fact_ref", "")))]
        if len(kept) != len(facts):
            events.append("fact_ref_dropped")
            out = dict(out) if out is payload else out
            out["facts"] = kept
    return out, events


def _trace(events: list[str], business_ids: Iterable[str | None]) -> None:
    ids = ",".join(sorted({b for b in business_ids if b}))
    for event in events:
        with observability.span(f"askonce.guard.{event}", **{observability.ATTR_BUSINESS: ids}) as current:
            if event == "canary":
                observability.mark_error(current, "injection canary blocked")
        logger.warning("output guard: %s (business %s)", event, ids or "-")


def guard_result(profile: Profile | None, result: G) -> G:
    """Apply checks 1-3. Returns the (possibly replaced) result of the same type."""
    canaries = profile.injection_canaries() if profile is not None else []
    approver = profile.approver_spoken_name() if profile is not None else None
    payload, events = guard_payload(result.model_dump(mode="json"), canaries, approver=approver)
    if not events:
        return result
    _trace(events, [profile.profile_id if profile is not None else None])
    return type(result).model_validate(payload)


def _business_ids(structured: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    if isinstance(structured.get("business_id"), str):
        ids.append(structured["business_id"])
    for match in structured.get("businesses") or ():
        if isinstance(match, dict) and isinstance(match.get("business_id"), str):
            ids.append(match["business_id"])
    return list(dict.fromkeys(ids))


class OutputGuardMiddleware:
    """`ServerMiddleware` applying `guard_result` checks to every `tools/call` result.

    Runs at the context tier (outermost user middleware). There the `tools/call` result is normally
    already the wire dict (`content`, `structuredContent`, `isError`, `resultType`); a `CallToolResult`
    model is handled the same way. It loads the canaries of every business named in the result
    (`business_id`, `businesses[].business_id`) via `profiles.get`, and replaces `structuredContent` and
    the JSON text copy in `content` only when a check fails, keeping the output schema valid.

    Task handles (`resultType: "task"`) and error results pass through untouched: task objects are
    checked by `TaskViewGuard` (the tasks extension's `present` hook) and task results by the renderer
    when the owner decides. A 2026-07-28 MRTR round (`"input_required"`) is a backstop check: every
    elicitation is built by `tools.confirm.guarded_elicit`, which already refuses canaries, so a round
    whose `inputRequests` (every string and property name) still carry a canary of the called business
    (`arguments.business_id`) is a bug, and it becomes an error result instead of a question. Legacy
    `elicitation/create` requests never pass through here.

    Profile-store failures here fail open for the canary check (the loader already rejects profiles
    whose spoken fields contain a canary, L8); the speak and fact-ref checks always run.
    """

    def __init__(self, profiles: ProfileStore | None) -> None:
        self._profiles = profiles

    async def _canaries(self, business_ids: list[str]) -> tuple[list[str], str | None]:
        canaries: list[str] = []
        approver: str | None = None
        if self._profiles is None:
            return canaries, approver
        for business_id in business_ids:
            try:
                profile = await self._profiles.get(business_id)
            except Exception:  # noqa: BLE001 - see class docstring (fail open, logged)
                logger.exception("output guard could not load profile %s", business_id)
                continue
            if profile is None:
                continue
            canaries.extend(profile.injection_canaries())
            if approver is None and len(business_ids) == 1:
                approver = profile.approver_spoken_name()
        return canaries, approver

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        result = await call_next(ctx)
        if ctx.method != "tools/call":
            return result
        if isinstance(result, types.CallToolResult):
            if result.is_error or not isinstance(result.structured_content, dict):
                return result
            payload = await self._guard(result.structured_content)
            if payload is None:
                return result
            return result.model_copy(
                update={"structured_content": payload, "content": [types.TextContent(type="text", text=_json(payload))]}
            )
        if isinstance(result, dict):
            # The usual shape at this tier: the wire dict of a CallToolResult. Task handles
            # (`resultType: "task"`) pass through untouched; MRTR rounds get the backstop check.
            if result.get("resultType") == "input_required":
                return await self._guard_round(ctx, result)
            if result.get("resultType", "complete") != "complete" or result.get("isError"):
                return result
            structured = result.get("structuredContent")
            if not isinstance(structured, dict):
                return result
            payload = await self._guard(structured)
            if payload is None:
                return result
            return {**result, "structuredContent": payload, "content": [{"type": "text", "text": _json(payload)}]}
        return result

    async def _guard_round(self, ctx: ServerRequestContext[Any, Any], result: dict[str, Any]) -> dict[str, Any]:
        """The MRTR backstop (see the class docstring). Only `inputRequests` is checked: `requestState`
        is opaque, and a short canary could match random bytes in it."""
        params = getattr(ctx, "params", None)
        arguments = params.get("arguments") if isinstance(params, Mapping) else None
        business_id = arguments.get("business_id") if isinstance(arguments, Mapping) else None
        if not isinstance(business_id, str):
            return result
        canaries, _approver = await self._canaries([business_id])
        if not _hits(strings_and_keys(result.get("inputRequests")), _normalised_canaries(canaries)):
            return result
        _trace(["canary"], [business_id])
        refused: dict[str, Any] = {
            "resultType": "complete",
            "isError": True,
            "content": [{"type": "text", "text": FALLBACK_SPEAK}],
        }
        if "_meta" in result:
            refused["_meta"] = result["_meta"]
        return refused

    async def _guard(self, structured: dict[str, Any]) -> dict[str, Any] | None:
        """The guarded payload, or None when nothing had to change."""
        if "speak" not in structured:
            return None
        business_ids = _business_ids(structured)
        canaries, approver = await self._canaries(business_ids)
        payload, events = guard_payload(structured, canaries, approver=approver)
        if not events:
            return None
        _trace(events, business_ids)
        return payload


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------- task objects

SAFE_TASK_QUESTION = "The business has a question about your request."
"""Replaces the message of a task input request (the owner's relayed question) that carries a canary."""

SAFE_TASK_STATUS: dict[str, str] = {
    "working": "Waiting for the business.",
    "input_required": "The business has a question for you.",
    "completed": "Done.",
    "failed": "The request could not be completed.",
    "cancelled": "Cancelled.",
}
"""Replaces a task `statusMessage` that carries a canary (per status)."""

SAFE_TASK_ERROR = "The request could not be completed."

_ANNOTATIONS = frozenset({"title", "description"})


def _without_canary_annotations(schema: Any, normalised: list[str]) -> Any:
    """`schema` minus every `title`/`description` string that carries a canary. Annotations only: the
    stored form, which `tasks/update` validates answers against, is unaffected either way."""
    if isinstance(schema, dict):
        return {
            key: _without_canary_annotations(value, normalised)
            for key, value in schema.items()
            if not (key in _ANNOTATIONS and isinstance(value, str) and _hits([value], normalised))
        }
    if isinstance(schema, list):
        return [_without_canary_annotations(item, normalised) for item in schema]
    return schema


def _safe_input_request(request: dict[str, Any], normalised: list[str]) -> dict[str, Any]:
    """One wire-shaped input request: unchanged if clean, else with a constant `message` (and canary
    annotations dropped from its `requestedSchema`, which is otherwise kept so answers still validate)."""
    if not _hits(strings_and_keys(request), normalised):
        return request
    params = request.get("params")
    if not isinstance(params, dict):
        return request
    safe = dict(params)
    if "message" in safe:
        safe["message"] = SAFE_TASK_QUESTION
    if "requestedSchema" in safe:
        safe["requestedSchema"] = _without_canary_annotations(safe["requestedSchema"], normalised)
    return {**request, "params": safe}


class TaskViewGuard:
    """`TasksExtension(present=...)` hook: the L8 canary check on every task object a client is sent
    (the `tools/call` task handle and `tasks/get`), against the business's *current* canaries.

    Stored task text was checked when it was written (the owner's relayed question by
    `OwnerService.decide`, results by the renderer), against the canaries of that time. A canary added
    later (a new profile revision flags text already on a task) would otherwise still reach
    tasks-capable clients, while the same task through the `get_task` tool is scrubbed by
    `OutputGuardMiddleware`. The view is rebuilt per request and never stored:

      * a `statusMessage` carrying a canary -> `SAFE_TASK_STATUS[status]`;
      * an input request carrying one (any string or key) -> `SAFE_TASK_QUESTION` as its message; its
        `requestedSchema` is kept (minus canary-carrying annotations), so the client's answer still
        matches the stored form `tasks/update` checks;
      * a completed `result` -> `guard_payload`, exactly as the middleware guards a tool result;
      * an `error` carrying one -> `SAFE_TASK_ERROR`.

    The business is the record's `business_id` label (else its stored `business_id` argument).
    Profile-store failures fail open (logged), like the middleware.
    """

    def __init__(self, profiles: ProfileStore | None) -> None:
        self._profiles = profiles

    async def __call__(self, record: TaskRecord) -> TaskRecord:
        business_id = record.labels.get("business_id") or record.arguments.get("business_id")
        if self._profiles is None or not isinstance(business_id, str) or not business_id:
            return record
        try:
            profile = await self._profiles.get(business_id)
        except Exception:  # noqa: BLE001 - fail open, as the middleware does (logged)
            logger.exception("task view guard could not load profile %s", business_id)
            return record
        if profile is None:
            return record
        canaries = profile.injection_canaries()
        normalised = _normalised_canaries(canaries)
        changes: dict[str, Any] = {}
        events: list[str] = []
        if record.status_message and _hits([record.status_message], normalised):
            changes["status_message"] = SAFE_TASK_STATUS.get(record.status, SAFE_TASK_STATUS["working"])
        if record.input_requests and _hits(strings_and_keys(record.input_requests), normalised):
            changes["input_requests"] = {
                key: _safe_input_request(request, normalised) for key, request in record.input_requests.items()
            }
        if record.error and _hits(strings_and_keys(record.error), normalised):
            changes["error"] = {"code": record.error.get("code"), "message": SAFE_TASK_ERROR}
        if changes:
            events.append("canary")
        structured = (record.result or {}).get("structuredContent")
        if isinstance(structured, dict) and "speak" in structured:
            payload, found = guard_payload(structured, canaries, approver=profile.approver_spoken_name())
            if found:
                events += [e for e in found if e not in events]
                text = {"type": "text", "text": _json(payload)}
                changes["result"] = {**(record.result or {}), "structuredContent": payload, "content": [text]}
        if not events:
            return record
        _trace(events, [business_id])
        return dataclasses.replace(record, **changes)
