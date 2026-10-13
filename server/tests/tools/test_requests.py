"""Alexa+ compatibility layer: check_request_status and cancel_request work in any MCP client.

The clients here are the ones Alexa+ resembles: 2026-07-28 or 2025-11-25 without the tasks extension,
and (for cancel_request's two-step confirmation) a client that declared no elicitation at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from support import ElicitationScript
from tools_support import BAKERY, BIG_CAKE, SALON, Stack, assert_grounded, structured

from mcp import Client
from askonce_server.domain.services.owner import OwnerDecisionInput
from askonce_server.ids import spoken_reference

pytestmark = pytest.mark.anyio

MENS_CUT = {"business_id": SALON, "item": "men's cut", "start": "2026-10-16T10:00"}
NOT_FOUND = "I couldn't find a request with that reference."


@asynccontextmanager
async def no_elicitation_client(stack: Stack, *, mode: str = "plain") -> AsyncIterator[Client]:
    """A client that declares neither the tasks extension nor elicitation (a plain voice host)."""
    kwargs: dict[str, Any] = {"cache": None}
    if mode == "legacy":
        kwargs["mode"] = "legacy"
    async with Client(stack.server, **kwargs) as client:
        yield client


class Principal:
    """Switches the caller's identity, as an authenticated deployment would see two customers."""

    def __init__(self) -> None:
        self.current = "anonymous"

    def __call__(self, _ctx: Any) -> str:
        return self.current


@pytest.fixture
def who(monkeypatch: pytest.MonkeyPatch) -> Iterator[Principal]:
    principal = Principal()
    monkeypatch.setattr("askonce_server.tools.context.principal_of", principal)
    monkeypatch.setattr("mcp_tasks.server.principal_of", principal)
    yield principal


async def _pending_cake(client: Client) -> dict[str, Any]:
    created = structured(await client.call_tool("place_custom_order", BIG_CAKE))
    assert created["status"] == "pending_owner" and created["task_id"]
    return created


# ---------------------------------------------------------------------------- check_request_status


@pytest.mark.parametrize("mode", ["plain", "legacy"])
async def test_status_by_task_id_follows_the_owner_decision(stack: Stack, elicit: ElicitationScript, mode: str) -> None:
    async with stack.client(elicit, mode=mode) as client:
        created = await _pending_cake(client)
        waiting = structured(await client.call_tool("check_request_status", {"request_id_or_code": created["task_id"]}))
        assert waiting["found"] is True and waiting["status"] == "pending_owner" and waiting["task_status"] == "working"
        assert waiting["reference"] == created["reference"] and waiting["task_id"] == created["task_id"]
        assert waiting["status_message"] == "Waiting for Rosa, who usually replies within 24 hours."
        same = structured(await client.call_tool("get_task", {"task_id": created["task_id"]}))
        assert waiting["speak"] == same["speak"]  # one flow: get_task's
        await stack.deps.owner.decide(BAKERY, created["reference"], OwnerDecisionInput(decision="approve"))
        done = structured(await client.call_tool("check_request_status", {"request_id_or_code": created["task_id"]}))
    assert done["status"] == "approved" and done["task_status"] == "completed"
    assert done["result"]["status"] == "approved" and done["speak"] == done["result"]["speak"]
    await assert_grounded(stack.deps, done)


async def test_status_by_reference_needs_the_business(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        created = await _pending_cake(client)
        by_ref = structured(
            await client.call_tool("check_request_status", {"request_id_or_code": created["reference"], "business_id": BAKERY})
        )
        missing = await client.call_tool("check_request_status", {"request_id_or_code": created["reference"]})
    assert by_ref["found"] is True and by_ref["task_id"] == created["task_id"] and by_ref["status"] == "pending_owner"
    assert missing.is_error is True and "business_id" in str(missing.content)


async def test_status_by_spoken_booking_code_in_a_later_session(stack: Stack, elicit: ElicitationScript) -> None:
    """A confirmed booking has no task: the status still answers, from the code the customer heard."""
    async with stack.client(elicit) as client:
        booked = structured(await client.call_tool("book", MENS_CUT))
    code = spoken_reference(booked["reference"])
    assert code is not None
    async with no_elicitation_client(stack, mode="legacy") as later:
        data = structured(
            await later.call_tool("check_request_status", {"request_id_or_code": f"{code[:4]} {code[4:]}", "business_id": SALON})
        )
    assert data["found"] is True and data["status"] == "confirmed" and data["task_id"] is None
    assert data["reference"] == booked["reference"] and data["task_status"] is None
    assert data["speak"].startswith("You're booked: men's cut on Friday, October 16 at 10 am.")
    await assert_grounded(stack.deps, data)


async def test_status_of_unknown_requests_is_one_answer(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        booked = structured(await client.call_tool("book", MENS_CUT))
        code = spoken_reference(booked["reference"])
        attempts = [
            {"request_id_or_code": "t_doesnotexist"},
            {"request_id_or_code": "my cake order"},
            {"request_id_or_code": "c_nope", "business_id": SALON},
            {"request_id_or_code": code, "business_id": BAKERY},  # right code, wrong business
        ]
        results = [structured(await client.call_tool("check_request_status", args)) for args in attempts]
    for args, result in zip(attempts, results, strict=True):
        assert result["found"] is False and result["speak"] == NOT_FOUND, args
        assert result["status"] is None and result["reference"] is None and result["task_id"] is None


async def test_status_never_shows_another_customers_request(stack: Stack, elicit: ElicitationScript, who: Principal) -> None:
    who.current = "alice"
    async with stack.client(elicit) as client:
        cake = await _pending_cake(client)
        booked = structured(await client.call_tool("book", MENS_CUT))
        code = spoken_reference(booked["reference"])
        asks = [
            {"request_id_or_code": cake["task_id"]},
            {"request_id_or_code": cake["reference"], "business_id": BAKERY},
            {"request_id_or_code": booked["reference"], "business_id": SALON},
            {"request_id_or_code": code, "business_id": SALON},
        ]
        for stranger in ("mallory", "anonymous"):
            who.current = stranger
            for args in asks:
                data = structured(await client.call_tool("check_request_status", args))
                assert data["found"] is False and data["speak"] == NOT_FOUND, (stranger, args)
                assert cake["reference"] not in str(data) and booked["reference"] not in str(data)
        who.current = "alice"
        mine = [structured(await client.call_tool("check_request_status", args)) for args in asks]
    assert all(m["found"] for m in mine)


# ---------------------------------------------------------------------------- cancel_request


async def test_cancel_pending_request_two_step_without_elicitation(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as setup:
        created = await _pending_cake(setup)
    args = {"request_id_or_code": created["task_id"]}
    async with no_elicitation_client(stack) as client:
        asked = structured(await client.call_tool("cancel_request", args))
        assert asked["status"] == "not_confirmed" and asked["confirm_token"]
        assert asked["speak"].startswith("Cancel your request for custom cake") and asked["speak"].endswith("Shall I cancel it?")
        assert (await stack.deps.task_manager.get(created["task_id"])).status == "working"  # nothing written yet

        forged = structured(await client.call_tool("cancel_request", {**args, "confirm_token": "k_" + "a" * 20}))
        assert forged["status"] == "not_confirmed" and forged["confirm_token"] == asked["confirm_token"]
        assert (await stack.deps.task_manager.get(created["task_id"])).status == "working"

        done = structured(await client.call_tool("cancel_request", {**args, "confirm_token": asked["confirm_token"]}))
        again = structured(await client.call_tool("cancel_request", {**args, "confirm_token": asked["confirm_token"]}))
        status = structured(await client.call_tool("check_request_status", args))
    assert done["status"] == "cancelled" and done["confirm_token"] is None and done["reference"] == created["reference"]
    assert (await stack.deps.task_manager.get(created["task_id"])).status == "cancelled"
    stored = await stack.deps.commitments.get(BAKERY, created["reference"])
    assert stored is not None and stored.status == "cancelled" and stored.holds_released
    assert again["status"] == "cancelled" and again["replayed"] is True
    assert status["status"] == "cancelled" and status["task_status"] == "cancelled"
    await assert_grounded(stack.deps, done)


async def test_cancel_pending_request_asks_an_elicitation_client(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        created = await _pending_cake(client)
        elicit.answers.append({"confirm": False})
        kept = structured(await client.call_tool("cancel_request", {"request_id_or_code": created["task_id"]}))
        assert kept["status"] == "not_confirmed" and kept["speak"] == "Okay, your request stays as it is."
        assert kept["confirm_token"] is None
        assert elicit.seen[-1][0].startswith("Cancel your request for custom cake") and elicit.seen[-1][1] == ["confirm"]
        done = structured(await client.call_tool("cancel_request", {"request_id_or_code": created["task_id"]}))
    assert done["status"] == "cancelled"
    assert (await stack.deps.task_manager.get(created["task_id"])).status == "cancelled"


async def test_cancel_confirmed_booking_by_code_uses_the_booking_flow(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        booked = structured(await client.call_tool("book", MENS_CUT))
    code = spoken_reference(booked["reference"])
    args = {"request_id_or_code": code, "business_id": SALON}
    async with no_elicitation_client(stack, mode="legacy") as later:
        asked = structured(await later.call_tool("cancel_request", args))
        assert asked["status"] == "not_confirmed" and asked["confirm_token"]
        assert asked["speak"].startswith("Cancel men's cut, Friday, October 16 at 10 am.")
        assert asked["speak"].endswith("Shall I cancel it?") and len(asked["speak"]) <= 240
        done = structured(await later.call_tool("cancel_request", {**args, "confirm_token": asked["confirm_token"]}))
    assert done["status"] == "cancelled" and done["reference"] == booked["reference"]
    stored = await stack.deps.commitments.get(SALON, booked["reference"])
    assert stored is not None and stored.status == "cancelled"


async def test_cancel_booking_with_elicitation_reads_back_first(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit, mode="legacy") as client:
        booked = structured(await client.call_tool("book", MENS_CUT))
        done = structured(
            await client.call_tool("cancel_request", {"request_id_or_code": booked["reference"], "business_id": SALON})
        )
    assert done["status"] == "cancelled"
    assert elicit.seen[-1][0].startswith("Cancel men's cut, Friday, October 16 at 10 am.")


async def test_cancel_a_decided_request_changes_nothing(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        created = await _pending_cake(client)
        await stack.deps.owner.decide(BAKERY, created["reference"], OwnerDecisionInput(decision="decline", note="Fully booked"))
        asked = len(elicit.seen)
        data = structured(await client.call_tool("cancel_request", {"request_id_or_code": created["task_id"]}))
    assert data["status"] == "declined" and data["confirm_token"] is None and len(elicit.seen) == asked
    assert data["speak"] == (
        "That request has already been declined, so I can't cancel it here. To change it, please contact Thimbleberry Bakehouse."
    )


async def test_cancel_never_touches_another_customers_request(stack: Stack, elicit: ElicitationScript, who: Principal) -> None:
    who.current = "alice"
    async with stack.client(elicit) as client:
        cake = await _pending_cake(client)
        booked = structured(await client.call_tool("book", MENS_CUT))
    async with no_elicitation_client(stack) as client:
        token = structured(await client.call_tool("cancel_request", {"request_id_or_code": cake["task_id"]}))["confirm_token"]
        assert token
        for stranger in ("mallory", "anonymous"):
            who.current = stranger
            for args in (
                {"request_id_or_code": cake["task_id"], "confirm_token": token},
                {"request_id_or_code": cake["reference"], "business_id": BAKERY, "confirm_token": token},
                {"request_id_or_code": spoken_reference(booked["reference"]), "business_id": SALON},
            ):
                data = structured(await client.call_tool("cancel_request", args))
                assert data["status"] == "unavailable" and data["speak"] == NOT_FOUND, (stranger, args)
                assert data["confirm_token"] is None
    assert (await stack.deps.task_manager.get(cake["task_id"])).status == "working"
    stored = await stack.deps.commitments.get(SALON, booked["reference"])
    assert stored is not None and stored.status == "confirmed"


async def test_a_confirm_token_is_bound_to_its_request(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as setup:
        first = await _pending_cake(setup)
        second = structured(await setup.call_tool("ask_owner", {"business_id": BAKERY, "question": "Do you sell gift cards?"}))
    async with no_elicitation_client(stack) as client:
        token = structured(await client.call_tool("cancel_request", {"request_id_or_code": first["task_id"]}))["confirm_token"]
        other = structured(await client.call_tool("cancel_request", {"request_id_or_code": second["task_id"], "confirm_token": token}))
    assert other["status"] == "not_confirmed" and other["confirm_token"] != token
    assert other["speak"] == "Cancel your question to Rosa? Rosa hasn't decided yet. Shall I cancel it?"
    assert (await stack.deps.task_manager.get(second["task_id"])).status == "working"
