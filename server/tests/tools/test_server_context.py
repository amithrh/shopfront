"""Server assembly (`tools.server`) and per-call plumbing (`tools.context`)."""

from __future__ import annotations

from typing import Any

import mcp_types as types
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from support import ElicitationScript
from tools_support import BAKERY, BIG_CAKE, SMALL_CAKE, Stack, build_stack, loaded, structured

from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from askonce_server import observability
from askonce_server.domain.services.owner import OwnerDecisionInput
from askonce_server.errors import UnknownBusiness
from askonce_server.tools import context
from askonce_server.tools.guard import OutputGuardMiddleware
from askonce_server.tools.schemas import PolicyResult
from askonce_server.tools.server import INSTRUCTIONS, build_mcp_server

pytestmark = pytest.mark.anyio


def test_server_installs_the_output_guard(stack: Stack) -> None:
    assert any(isinstance(m, OutputGuardMiddleware) for m in stack.server.middleware)
    assert stack.server.name == "askonce"
    assert "find_business" in INSTRUCTIONS and "get_task" in INSTRUCTIONS


async def test_a_confirmation_round_can_land_on_another_replica(
    tmp_path: Any, live_raw_profiles: dict[str, dict[str, Any]]
) -> None:
    """Round 1 on replica A, round 2 on replica B: works because both share the request-state keys and
    the fixed server name, and all state lives in the (shared) stores, not in the process."""
    replica_a = build_stack(tmp_path, [loaded(d) for d in live_raw_profiles.values()])
    replica_b = build_mcp_server(replica_a.deps)
    script = ElicitationScript()
    async with Client(replica_a.server, elicitation_callback=script, cache=None) as client_a:
        first = await client_a.session.call_tool("place_custom_order", SMALL_CAKE, allow_input_required=True)
    assert isinstance(first, types.InputRequiredResult) and first.request_state
    [(key, request)] = first.input_requests.items()
    assert request.params.requested_schema["properties"] == {"confirm": {"description": "Yes to go ahead", "title": "Confirm", "type": "boolean"}}
    async with Client(replica_b, elicitation_callback=script, cache=None) as client_b:
        second = await client_b.session.call_tool(
            "place_custom_order",
            SMALL_CAKE,
            input_responses={key: types.ElicitResult(action="accept", content={"confirm": True})},
            request_state=first.request_state,
            allow_input_required=True,
        )
    assert isinstance(second, types.CallToolResult)
    assert structured(second)["status"] == "confirmed"


class _NonLiveStore:
    def __init__(self, profile: Any) -> None:
        self.profile = profile

    async def get_live(self, business_id: str) -> Any:
        if business_id != BAKERY:
            raise UnknownBusiness(business_id)
        return self.profile


class _Draft:
    is_live = False


async def test_live_profile_is_the_tenant_gate(stack: Stack) -> None:
    profile = await context.live_profile(stack.deps, BAKERY)
    assert profile.profile_id == BAKERY
    with pytest.raises(ToolError, match="Unknown business"):
        await context.live_profile(stack.deps, "nope")

    class Deps:
        profiles = _NonLiveStore(_Draft())

    with pytest.raises(ToolError, match="Unknown business"):
        await context.live_profile(Deps(), BAKERY)  # type: ignore[arg-type]


def _sdk_tracer() -> tuple[Any, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


async def test_finish_stamps_tenant_revision_and_trace(stack: Stack) -> None:
    profile = await stack.deps.profiles.get_live(BAKERY)
    tracer, exporter = _sdk_tracer()
    with tracer.start_as_current_span("tools/call ask_policy") as span:
        result = context.finish(
            PolicyResult(speak="We take cards."), profile, outcome="ok", module_id="info", commitment_id=None
        )
    assert result.business_id == BAKERY and result.profile_revision == profile.revision
    assert result.trace_id == format(span.get_span_context().trace_id, "032x")
    [finished] = exporter.get_finished_spans()
    attrs = dict(finished.attributes or {})
    assert attrs[observability.ATTR_OUTCOME] == "ok"
    assert attrs[observability.ATTR_BUSINESS] == BAKERY
    assert attrs[observability.ATTR_FACT_COUNT] == 0
    assert attrs[observability.ATTR_MODULE] == "info"
    assert observability.ATTR_COMMITMENT not in attrs  # None values are not recorded


def test_finish_without_a_span_or_profile() -> None:
    result = context.finish(PolicyResult(speak="Hi."), None, outcome="no_match")
    assert result.trace_id is None and result.business_id is None


# ---------------------------------------------------------------------------- round 2: task ids stay out of spans


def test_round2_tasks_list_tool_span_records_only_digests_of_capability_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without auth a task id guards tasks/get, tasks/update and tasks/cancel on its own, so a trace
    reader must not be able to recover one: `tool_span` applies the same digest rule as `finish`."""
    tracer, exporter = _sdk_tracer()
    monkeypatch.setattr(observability, "tracer", lambda: tracer)
    with context.tool_span("get_task", task_id="t_secret", commitment_id="c_secret", module_id="rosa", absent=None):
        pass
    [finished] = exporter.get_finished_spans()
    attrs = dict(finished.attributes or {})
    assert attrs[observability.ATTR_TOOL] == "get_task"
    assert attrs[observability.ATTR_TASK] == observability.id_digest("t_secret")
    assert attrs[observability.ATTR_COMMITMENT] == observability.id_digest("c_secret")
    assert attrs[observability.ATTR_MODULE] == "rosa"
    assert "askonce.absent" not in attrs
    assert not [k for k, v in attrs.items() if "_secret" in str(v) or not k.startswith("askonce.")]


async def test_round2_tasks_list_no_span_carries_a_raw_task_id_or_reference(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: placing an owner-approval order, polling it with get_task and answering the owner's
    question with reply_to_owner (both take the raw task id as an argument) leave only digests."""
    tracer, exporter = _sdk_tracer()
    monkeypatch.setattr(observability, "tracer", lambda: tracer)
    elicit = ElicitationScript()
    async with Client(stack.server, elicitation_callback=elicit, cache=None) as client:
        created = structured(await client.call_tool("place_custom_order", BIG_CAKE))
        task_id, reference = created["task_id"], created["reference"]
        structured(await client.call_tool("get_task", {"task_id": task_id}))
        await stack.deps.owner.decide(BAKERY, reference, OwnerDecisionInput(decision="request_info", question="Which colour?"))
        replied = structured(await client.call_tool("reply_to_owner", {"task_id": task_id, "answer": "Blue"}))
    assert task_id and reference and replied["task_status"] == "working"
    spans = {span.name: dict(span.attributes or {}) for span in exporter.get_finished_spans()}
    for name in ("askonce.tool.place_custom_order", "askonce.tool.get_task", "askonce.tool.reply_to_owner"):
        assert spans[name][observability.ATTR_TASK] == observability.id_digest(task_id), name
    assert spans["askonce.tool.place_custom_order"][observability.ATTR_COMMITMENT] == observability.id_digest(reference)
    leaked = [
        (name, key)
        for name, attrs in spans.items()
        for key, value in attrs.items()
        if task_id in str(value) or reference in str(value)
    ]
    assert leaked == []
