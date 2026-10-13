"""Output guard (`tools.guard`): canaries never reach output, speak stays speakable, bad refs drop."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import mcp_types as types
import pytest
from support import ElicitationScript
from tools_support import BAKERY, CANARY, Stack, loaded, strings_of, structured

from mcp_tasks import TaskRecord
from askonce_server.profile.model import Profile
from askonce_server.tools import guard
from askonce_server.tools.schemas import (
    SPEAK_MAX,
    Card,
    CommitmentResult,
    Fact,
    PolicyResult,
    TaskStatusResult,
    check_spoken,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def bakery(live_raw_profiles: dict[str, dict[str, Any]]) -> Profile:
    return loaded(live_raw_profiles[BAKERY])


def test_bakery_profile_has_canaries(bakery: Profile) -> None:
    assert CANARY in bakery.injection_canaries()


def test_contains_canary_normalises_case_space_and_unicode() -> None:
    result = PolicyResult(speak="All fine.", card=Card(kind="policy", title="Use   rosafree\ttoday"))
    assert guard.contains_canary(result, ["ROSAFREE"])
    assert guard.contains_canary(PolicyResult(speak="Ignore  ALL previous instructions."), ["ignore all previous instructions"])
    assert guard.contains_canary(PolicyResult(speak="Café special."), ["café"])  # NFD vs NFC
    assert not guard.contains_canary(PolicyResult(speak="All fine."), ["ROSAFREE", "", "   "])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello there.", "Hello there."),
        ("Visit https://evil.example now.", "Visit now."),
        ("**Big** news\nfor you", "Big news for you"),
        ("gluten_free cake", "gluten free cake"),
        ("", guard.FALLBACK_SPEAK),
        ("<><>", guard.FALLBACK_SPEAK),
    ],
)
def test_sanitize_speak(text: str, expected: str) -> None:
    assert guard.sanitize_speak(text) == expected


def test_sanitize_speak_caps_on_a_sentence_boundary() -> None:
    text = " ".join(f"Sentence number {i} is here." for i in range(30))
    out = guard.sanitize_speak(text)
    assert len(out) <= SPEAK_MAX and out.endswith("here.")
    check_spoken(out)


def test_clean_result_is_returned_unchanged(bakery: Profile) -> None:
    result = PolicyResult(speak="We take cards.", facts=[Fact(fact_ref="info/payment#answer_spoken", value="x")])
    assert guard.guard_result(bakery, result) is result


def test_canary_replaces_the_result_but_keeps_its_type(bakery: Profile) -> None:
    result = CommitmentResult(
        speak="Your order is confirmed.",
        facts=[Fact(fact_ref="info/payment#answer_spoken", value="Pay with ROSAFREE")],
        card=Card(kind="commitment", title="Order"),
        status="approved",
        reference="c_abc",
        missing_fields=["note mentions rosafree"],
    )
    out = guard.guard_result(bakery, result)
    assert isinstance(out, CommitmentResult)
    assert out.status == "approved" and out.reference == "c_abc"
    assert out.card is None and out.facts == []
    assert out.handoff is not None and out.handoff.reason == "needs_owner" and out.handoff.approver_spoken_name == "Rosa"
    assert out.speak == "Sorry, I can't share that answer. Want me to ask Rosa?"
    assert CANARY.lower() not in json.dumps(out.model_dump()).lower()
    assert out.missing_fields == [guard.WITHHELD]


def test_unparseable_fact_refs_are_dropped(bakery: Profile) -> None:
    result = PolicyResult(
        speak="Fine.",
        facts=[Fact(fact_ref="info/payment#answer_spoken", value=1), Fact(fact_ref="Not A Ref", value=2)],
    )
    out = guard.guard_result(bakery, result)
    assert [f.fact_ref for f in out.facts] == ["info/payment#answer_spoken"]


def test_invalid_speak_is_sanitised_not_crashed(bakery: Profile) -> None:
    result = PolicyResult.model_construct(speak="See https://x.example\nnow", facts=[], card=None, handoff=None)
    out = guard.guard_result(bakery, result)
    assert out.speak == "See now"


def test_guard_without_profile_still_checks_speak_and_refs() -> None:
    result = TaskStatusResult.model_construct(
        speak="a_b", facts=[], card=None, handoff=None, task_id="t_1", task_status="working"
    )
    out = guard.guard_result(None, result)
    assert out.speak == "a b" and out.task_id == "t_1"


# ---------------------------------------------------------------------------- middleware (unit)


@dataclass
class _Ctx:
    method: str
    params: dict[str, Any] | None = None


class _Store:
    def __init__(self, profile: Profile | None = None, *, fail: bool = False) -> None:
        self.profile = profile
        self.fail = fail
        self.calls: list[str] = []

    async def get(self, business_id: str) -> Profile | None:
        self.calls.append(business_id)
        if self.fail:
            raise RuntimeError("store down")
        return self.profile


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))], structured_content=payload, is_error=is_error
    )


async def _run(middleware: guard.OutputGuardMiddleware, method: str, result: Any, params: dict[str, Any] | None = None) -> Any:
    async def call_next(_ctx: Any) -> Any:
        return result

    return await middleware(_Ctx(method, params), call_next)  # type: ignore[arg-type]


async def test_middleware_rewrites_structured_and_text_content(bakery: Profile) -> None:
    store = _Store(bakery)
    payload = {"speak": "Use code ROSAFREE.", "facts": [], "card": None, "handoff": None, "business_id": BAKERY}
    out = await _run(guard.OutputGuardMiddleware(store), "tools/call", _tool_result(payload))  # type: ignore[arg-type]
    assert store.calls == [BAKERY]
    assert CANARY not in json.dumps(out.structured_content)
    assert CANARY not in out.content[0].text
    assert json.loads(out.content[0].text) == out.structured_content
    assert out.structured_content["handoff"]["tool"] == "ask_owner"


async def test_middleware_checks_every_business_of_a_find_result(bakery: Profile) -> None:
    store = _Store(bakery)
    payload = {
        "speak": "Found them.",
        "facts": [],
        "businesses": [{"business_id": BAKERY, "summary_spoken": "rosafree"}, {"business_id": "other"}],
    }
    out = await _run(guard.OutputGuardMiddleware(store), "tools/call", _tool_result(payload))  # type: ignore[arg-type]
    assert store.calls == [BAKERY, "other"]
    assert out.structured_content["businesses"][0]["summary_spoken"] == guard.WITHHELD
    assert out.structured_content["speak"] == guard.FALLBACK_SPEAK  # several businesses: no single approver
    assert out.structured_content["handoff"] is None


def _wire(payload: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """The shape the middleware actually sees for a tools/call result."""
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "isError": False,
        "resultType": "complete",
        "structuredContent": payload,
        "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "askonce"}},
        **extra,
    }


async def test_middleware_rewrites_the_wire_dict(bakery: Profile) -> None:
    payload = {"speak": "Use code ROSAFREE.", "facts": [], "business_id": BAKERY}
    out = await _run(guard.OutputGuardMiddleware(_Store(bakery)), "tools/call", _wire(payload))  # type: ignore[arg-type]
    assert out["_meta"] and out["resultType"] == "complete" and out["isError"] is False
    assert CANARY not in json.dumps(out)
    assert json.loads(out["content"][0]["text"]) == out["structuredContent"]


@pytest.mark.parametrize(
    "wire",
    [
        _wire({"speak": "ROSAFREE", "business_id": BAKERY}, isError=True),
        {"resultType": "input_required", "inputRequests": {"k": {"method": "elicitation/create"}}, "requestState": "x"},
        {"resultType": "task", "taskId": "t_1", "status": "working", "statusMessage": "ROSAFREE"},
        _wire({"no_speak": "ROSAFREE", "business_id": BAKERY}),
    ],
)
async def test_middleware_leaves_other_wire_shapes_alone(bakery: Profile, wire: dict[str, Any]) -> None:
    out = await _run(guard.OutputGuardMiddleware(_Store(bakery)), "tools/call", wire)  # type: ignore[arg-type]
    assert out is wire


@pytest.mark.parametrize(
    "result",
    [
        {"resultType": "task", "taskId": "t_1", "status": "working"},  # task handle dict
        _tool_result({"speak": "ROSAFREE"}, is_error=True),
        types.CallToolResult(content=[types.TextContent(type="text", text="ROSAFREE")]),  # no structured content
    ],
)
async def test_middleware_passes_through_what_it_must_not_touch(bakery: Profile, result: Any) -> None:
    out = await _run(guard.OutputGuardMiddleware(_Store(bakery)), "tools/call", result)  # type: ignore[arg-type]
    assert out is result


async def test_middleware_ignores_other_methods(bakery: Profile) -> None:
    result = _tool_result({"speak": "ROSAFREE", "business_id": BAKERY})
    out = await _run(guard.OutputGuardMiddleware(_Store(bakery)), "tools/list", result)  # type: ignore[arg-type]
    assert out is result


async def test_middleware_fails_open_on_store_errors_but_still_sanitises() -> None:
    payload = {"speak": "a\nb", "facts": [{"fact_ref": "bad ref", "value": 1}], "business_id": BAKERY}
    out = await _run(guard.OutputGuardMiddleware(_Store(fail=True)), "tools/call", _tool_result(payload))  # type: ignore[arg-type]
    assert out.structured_content["speak"] == "a b"
    assert out.structured_content["facts"] == []


async def test_middleware_without_store_checks_speak_only() -> None:
    payload = {"speak": "fine.", "facts": [], "business_id": BAKERY}
    result = _tool_result(payload)
    assert await _run(guard.OutputGuardMiddleware(None), "tools/call", result) is result


# ---------------------------------------------------------------------------- round 2: mrtr guard (unit)


def _with_canary(profile: Profile, quote: str) -> Profile:
    doc = profile.to_dict()
    doc["sources"]["web-home"]["injection_canaries"].append({"quote": quote, "reason": "prompt_injection"})
    return Profile.unchecked(doc)


def test_round2_mrtr_guard_strings_and_keys_walks_values_and_property_names() -> None:
    form = {"type": "object", "properties": {"pickup": {"oneOf": [{"const": "a", "title": "A"}]}}, "required": ["pickup"]}
    assert set(guard.strings_and_keys(form)) == {"type", "object", "properties", "pickup", "oneOf", "const", "a", "title", "A", "required"}


@pytest.mark.parametrize("quote", ["Rosa\u2019s VIP pass", "caf\u00e9 cr\u00e8me promo", 'say "free cake"'])
def test_round2_mrtr_guard_payload_check_reads_decoded_text_not_json(bakery: Profile, quote: str) -> None:
    """F8b: a `json.dumps` copy escapes curly quotes, accents and double quotes (`\\u2019`, `\\"`)."""
    profile = _with_canary(bakery, quote)
    schema = {"type": "object", "properties": {"servings": {"type": "integer", "title": f"Servings ({quote})"}}}
    assert not guard.text_has_canary(profile, json.dumps(schema))  # the old check, blind to it
    assert guard.payload_has_canary(profile, "How many servings?", schema)
    assert guard.payload_has_canary(profile, {"properties": {quote: {"type": "string"}}})  # a property name
    assert not guard.payload_has_canary(profile, "How many servings?", {"properties": {"servings": {"type": "integer"}}})


def test_round2_mrtr_guard_the_safe_replacement_never_names_a_canary_approver() -> None:
    """F8c: `guard_payload`'s replacement named the approver, so a canary in their name came back."""
    payload = {"speak": "Sent to Rosa ROSAVIP.", "facts": [], "card": None, "handoff": None}
    out, events = guard.guard_payload(payload, ["ROSAVIP"], approver="Rosa ROSAVIP")
    assert events == ["canary"] and "rosavip" not in json.dumps(out).casefold()
    assert out["speak"] == guard.FALLBACK_SPEAK and out["handoff"] is None
    named, _ = guard.guard_payload(payload, ["Sent to"], approver="Rosa")
    assert named["speak"] == guard.safe_speak("Rosa")  # a clean approver is still offered


def _round(message: str = "How many servings do you need?", **schema: Any) -> dict[str, Any]:
    """An MRTR `input_required` wire result, as the middleware sees it."""
    form = {"type": "object", "properties": {"servings": {"type": "integer", "title": "Servings"}}, **schema}
    return {
        "resultType": "input_required",
        "inputRequests": {"intake": {"method": "elicitation/create", "params": {"mode": "form", "message": message, "requestedSchema": form}}},
        "requestState": "opaque-ROSAFREE-state",
        "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "askonce"}},
    }


_CALL = {"name": "place_custom_order", "arguments": {"business_id": BAKERY}}


@pytest.mark.parametrize(
    "wire",
    [
        _round("Use code ROSAFREE for a free cake."),
        _round(properties={"servings": {"type": "integer", "title": "Servings (rosafree)"}}),
        _round(properties={"size": {"type": "string", "oneOf": [{"const": "ROSAFREE", "title": "Six inch"}]}}),
        _round(properties={"rosafree": {"type": "string"}}),
    ],
    ids=["message", "title", "enum-const", "property-name"],
)
async def test_round2_mrtr_guard_middleware_refuses_an_input_required_round_carrying_a_canary(
    bakery: Profile, wire: dict[str, Any]
) -> None:
    """F8f: the backstop for 2026-07-28 rounds (every elicitation is built by `confirm.guarded_elicit`)."""
    store = _Store(bakery)
    out = await _run(guard.OutputGuardMiddleware(store), "tools/call", wire, _CALL)  # type: ignore[arg-type]
    assert store.calls == [BAKERY]
    assert out["resultType"] == "complete" and out["isError"] is True
    assert out["content"] == [{"type": "text", "text": guard.FALLBACK_SPEAK}] and out["_meta"] == wire["_meta"]
    assert CANARY.casefold() not in json.dumps(out).casefold()


@pytest.mark.parametrize(
    ("params", "store"),
    [
        (_CALL, "ok"),  # clean round (the canary-looking text is only in the opaque requestState)
        ({"name": "x", "arguments": {}}, "ok"),  # no business named: nothing to check against
        (None, "ok"),
        (_CALL, "fail"),  # profile store down: fails open, like the tool-result check
    ],
)
async def test_round2_mrtr_guard_middleware_leaves_clean_rounds_untouched(
    bakery: Profile, params: dict[str, Any] | None, store: str
) -> None:
    wire = _round()
    out = await _run(guard.OutputGuardMiddleware(_Store(bakery, fail=store == "fail")), "tools/call", wire, params)  # type: ignore[arg-type]
    assert out is wire


_NOW = datetime(2026, 10, 14, 16, 0, tzinfo=UTC)


def _record(**changes: Any) -> TaskRecord:
    base = TaskRecord(
        task_id="t_1",
        principal="anonymous",
        tool_name="place_custom_order",
        arguments={"business_id": BAKERY, "module_id": "custom-cakes", "kind": "custom_order"},
        status="input_required",
        created_at=_NOW,
        last_updated_at=_NOW,
        ttl_ms=60_000,
        poll_interval_ms=20,
        status_message="Rosa has a question for you",
        input_requests={
            "owner_question_1": {
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "message": "Rosa asks: Which colour?",
                    "requestedSchema": {"type": "object", "properties": {"answer": {"type": "string", "title": "Your answer"}}},
                },
            }
        },
        labels={"business_id": BAKERY, "commitment_id": "c_1"},
    )
    return replace(base, **changes)


async def test_round2_mrtr_guard_task_view_leaves_a_clean_record_alone(bakery: Profile) -> None:
    record = _record()
    assert await guard.TaskViewGuard(_Store(bakery))(record) is record
    assert await guard.TaskViewGuard(None)(record) is record
    assert await guard.TaskViewGuard(_Store(bakery, fail=True))(record) is record  # fails open
    unlabelled = _record(labels={}, arguments={})
    store = _Store(bakery)
    assert await guard.TaskViewGuard(store)(unlabelled) is unlabelled and store.calls == []


async def test_round2_mrtr_guard_task_view_replaces_canary_text_but_keeps_the_form(bakery: Profile) -> None:
    """F8e: what a tasks-capable client is sent is re-checked against the current canaries."""
    asked = _record(
        status_message="Rosa says ROSAFREE",
        input_requests={
            "owner_question_1": {
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "message": "Rosa asks: use code ROSAFREE?",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string", "title": "Your answer", "description": "rosafree"}},
                        "required": ["answer"],
                    },
                },
            },
            "owner_question_2": _record().input_requests["owner_question_1"],  # type: ignore[index]
        },
        labels={},  # falls back to the stored business_id argument
    )
    store = _Store(bakery)
    view = await guard.TaskViewGuard(store)(asked)
    assert store.calls == [BAKERY] and CANARY.casefold() not in str(view).casefold()
    assert view.status_message == guard.SAFE_TASK_STATUS["input_required"]
    assert view.input_requests is not None and asked.input_requests is not None
    first = view.input_requests["owner_question_1"]["params"]
    assert first["message"] == guard.SAFE_TASK_QUESTION
    assert first["requestedSchema"] == {
        "type": "object",
        "properties": {"answer": {"type": "string", "title": "Your answer"}},
        "required": ["answer"],
    }
    assert view.input_requests["owner_question_2"] == asked.input_requests["owner_question_2"]  # clean: as stored
    assert asked.status_message == "Rosa says ROSAFREE"  # the record itself is untouched


async def test_round2_mrtr_guard_task_view_guards_results_and_errors(bakery: Profile) -> None:
    payload = {"speak": "Use code ROSAFREE.", "facts": [], "card": None, "handoff": None, "business_id": BAKERY, "status": "approved"}
    done = _record(
        status="completed",
        status_message=None,
        input_requests=None,
        result={"content": [{"type": "text", "text": json.dumps(payload)}], "structuredContent": payload, "isError": False},
    )
    view = await guard.TaskViewGuard(_Store(bakery))(done)
    assert view.result is not None and CANARY not in json.dumps(view.result)
    assert view.result["structuredContent"]["speak"] == guard.safe_speak("Rosa")
    assert view.result["structuredContent"]["status"] == "approved" and view.result["isError"] is False
    assert json.loads(view.result["content"][0]["text"]) == view.result["structuredContent"]
    failed = _record(status="failed", status_message="ROSAFREE", input_requests=None, error={"code": -32603, "message": "ROSAFREE", "data": "x"})
    view = await guard.TaskViewGuard(_Store(bakery))(failed)
    assert view.error == {"code": -32603, "message": guard.SAFE_TASK_ERROR}
    assert view.status_message == guard.SAFE_TASK_STATUS["failed"]


# ---------------------------------------------------------------------------- end to end


async def test_injected_canary_never_reaches_any_output(canary_stack: Stack) -> None:
    elicit = ElicitationScript()
    async with canary_stack.client(elicit) as client:
        calls = [
            ("ask_policy", {"business_id": BAKERY, "question": "is there parking?"}),
            ("find_item", {"business_id": BAKERY, "item": "sourdough", "aspects": ["description", "price"]}),
            ("get_catalog", {"business_id": BAKERY, "category": "breads"}),
        ]
        for name, args in calls:
            result = await client.call_tool(name, args)
            data = structured(result)
            text = " ".join(c.text for c in result.content if isinstance(c, types.TextContent))
            everything = " ".join([*strings_of(data), text]).casefold()
            assert CANARY.casefold() not in everything, name
            check_spoken(data["speak"])
    replaced = structured(await _call(canary_stack, "ask_policy", {"business_id": BAKERY, "question": "is there parking?"}))
    assert replaced["speak"] == "Sorry, I can't share that answer. Want me to ask Rosa?"
    assert replaced["handoff"]["reason"] == "needs_owner" and replaced["facts"] == []


async def _call(stack: Stack, name: str, args: dict[str, Any]) -> Any:
    async with stack.client(ElicitationScript()) as client:
        return await client.call_tool(name, args)
