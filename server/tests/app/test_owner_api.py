"""Owner REST API: the auth matrix (401), tenant isolation (404), decisions (400/409) and the task
completing when the owner approves. Runs in-loop over httpx's ASGI transport against fake domain
collaborators (see tests/app/conftest.py)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from askonce_server.app.links import OwnerLinkSigner
from askonce_server.app.main import create_app
from askonce_server.clock import FixedClock
from askonce_server.config import DEV_ADMIN_TOKEN, Settings

pytestmark = pytest.mark.anyio

BAKERY = "thimbleberry-bakehouse"
SALON = "quillfern-salon"


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture
def inbox_token(app_signer: OwnerLinkSigner) -> str:
    return app_signer.sign(business_id=BAKERY, scope="inbox")


def assert_unauthorized(response: httpx.Response) -> None:
    assert response.status_code == 401, response.text
    assert response.json() == {"error": "Invalid or expired link", "code": "unauthorized"}
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"


# --------------------------------------------------------------------------- 401 matrix


async def test_missing_token_is_401(api: httpx.AsyncClient, seeded: Any) -> None:
    assert_unauthorized(await api.get(f"/owner/{BAKERY}/inbox"))
    assert_unauthorized(await api.get(f"/owner/{BAKERY}/requests/c_pending1"))
    assert_unauthorized(await api.post(f"/owner/{BAKERY}/requests/c_pending1/decision", json={"decision": "approve"}))


@pytest.mark.parametrize("header", ["Bearer", "Bearer ", "Basic abc", "Token x", "garbage"])
async def test_malformed_authorization_header_is_401(api: httpx.AsyncClient, inbox_token: str, header: str) -> None:
    # A malformed header never falls back to the query token.
    response = await api.get(f"/owner/{BAKERY}/inbox", params={"token": inbox_token}, headers={"authorization": header})
    assert_unauthorized(response)


async def test_garbage_token_is_401(api: httpx.AsyncClient) -> None:
    assert_unauthorized(await api.get(f"/owner/{BAKERY}/inbox", headers=bearer("not.a-token")))


async def test_expired_token_is_401(api: httpx.AsyncClient, app_signer: OwnerLinkSigner, clock: FixedClock) -> None:
    token = app_signer.sign(business_id=BAKERY, scope="inbox", ttl_s=60)
    assert (await api.get(f"/owner/{BAKERY}/inbox", headers=bearer(token))).status_code == 200
    clock.advance(timedelta(seconds=60))
    assert_unauthorized(await api.get(f"/owner/{BAKERY}/inbox", headers=bearer(token)))


async def test_other_business_token_is_401(api: httpx.AsyncClient, app_signer: OwnerLinkSigner, seeded: Any) -> None:
    salon_token = app_signer.sign(business_id=SALON, scope="inbox")
    assert_unauthorized(await api.get(f"/owner/{BAKERY}/inbox", headers=bearer(salon_token)))
    assert_unauthorized(await api.get(f"/owner/{BAKERY}/requests/c_pending1", headers=bearer(salon_token)))


async def test_token_signed_with_another_key_is_401(api: httpx.AsyncClient, clock: FixedClock) -> None:
    forger = OwnerLinkSigner([b"attacker-key-0123456789abcdef0123456789"], clock=clock, default_ttl_s=60,
                             public_base_url="http://x")
    assert_unauthorized(await api.get(f"/owner/{BAKERY}/inbox", headers=bearer(forger.sign(business_id=BAKERY,
                                                                                         scope="inbox"))))


async def test_decide_token_scope(api: httpx.AsyncClient, app_signer: OwnerLinkSigner, seeded: Any) -> None:
    token = app_signer.sign(business_id=BAKERY, scope="decide", commitment_id="c_pending1")
    assert_unauthorized(await api.get(f"/owner/{BAKERY}/inbox", headers=bearer(token)))
    assert_unauthorized(await api.get(f"/owner/{BAKERY}/requests/c_question1", headers=bearer(token)))
    assert (await api.get(f"/owner/{BAKERY}/requests/c_pending1", headers=bearer(token))).status_code == 200
    response = await api.post(
        f"/owner/{BAKERY}/requests/c_pending1/decision", json={"decision": "approve"}, headers=bearer(token)
    )
    assert response.status_code == 200, response.text


async def test_auth_is_checked_before_validation(api: httpx.AsyncClient) -> None:
    assert_unauthorized(await api.get(f"/owner/{BAKERY}/inbox", params={"status": "bogus", "limit": "x"}))
    assert_unauthorized(await api.post(f"/owner/{BAKERY}/requests/c_pending1/decision", json={"decision": "maybe"}))


# --------------------------------------------------------------------------- token sources


async def test_token_in_query(api: httpx.AsyncClient, inbox_token: str, seeded: Any) -> None:
    assert (await api.get(f"/owner/{BAKERY}/inbox", params={"token": inbox_token})).status_code == 200


async def test_token_in_body(api: httpx.AsyncClient, inbox_token: str, seeded: Any) -> None:
    response = await api.post(
        f"/owner/{BAKERY}/requests/c_pending1/decision", json={"decision": "decline", "token": inbox_token}
    )
    assert response.status_code == 200, response.text
    assert response.json()["commitment"]["status"] == "declined"


# --------------------------------------------------------------------------- inbox and requests


async def test_inbox_lists_open_requests_of_this_business_only(
    api: httpx.AsyncClient, inbox_token: str, seeded: Any
) -> None:
    response = await api.get(f"/owner/{BAKERY}/inbox", headers=bearer(inbox_token))
    assert response.status_code == 200
    body = response.json()
    assert body["business_id"] == BAKERY and body["business_name"] == "Thimbleberry Bakehouse"
    assert [item["commitment"]["commitment_id"] for item in body["items"]] == ["c_question1", "c_pending1"]
    assert body["next_cursor"] is None
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_inbox_all_includes_settled_requests(api: httpx.AsyncClient, inbox_token: str, seeded: Any) -> None:
    response = await api.get(f"/owner/{BAKERY}/inbox", params={"status": "all"}, headers=bearer(inbox_token))
    ids = {item["commitment"]["commitment_id"] for item in response.json()["items"]}
    assert ids == {"c_question1", "c_pending1", "c_done1"}


@pytest.mark.parametrize("params", [{"status": "bogus"}, {"limit": "0"}, {"limit": "101"}, {"limit": "x"}])
async def test_inbox_rejects_bad_query(api: httpx.AsyncClient, inbox_token: str, params: dict[str, str]) -> None:
    response = await api.get(f"/owner/{BAKERY}/inbox", params=params, headers=bearer(inbox_token))
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


async def test_request_detail_offers_module_decisions(
    api: httpx.AsyncClient, inbox_token: str, seeded: Any, world: Any
) -> None:
    calls = world.owner.item_calls
    response = await api.get(f"/owner/{BAKERY}/requests/c_pending1", headers=bearer(inbox_token))
    assert response.status_code == 200
    assert world.owner.item_calls == calls + 1  # an open request is the domain's own inbox item
    item = response.json()
    assert item["commitment"]["commitment_id"] == "c_pending1"
    assert item["commitment"]["intake"]["customer_name"] == "Sam Lee"  # the owner needs the contact details
    assert item["task_status"] == "working"
    assert item["allowed_decisions"] == ["approve", "decline", "counter_offer", "request_info"]
    assert item["response_fields"] == [
        {"key": "final_price", "type": "money", "label": "Final price", "required": False}
    ]


async def test_owner_question_offers_answer(api: httpx.AsyncClient, inbox_token: str, seeded: Any) -> None:
    item = (await api.get(f"/owner/{BAKERY}/requests/c_question1", headers=bearer(inbox_token))).json()
    assert item["allowed_decisions"] == ["answer"]
    assert [field["key"] for field in item["response_fields"]] == ["answer"]


async def test_detail_matches_the_inbox_listing(api: httpx.AsyncClient, inbox_token: str, seeded: Any) -> None:
    listed = (await api.get(f"/owner/{BAKERY}/inbox", headers=bearer(inbox_token))).json()["items"]
    for item in listed:
        commitment_id = item["commitment"]["commitment_id"]
        detail = await api.get(f"/owner/{BAKERY}/requests/{commitment_id}", headers=bearer(inbox_token))
        assert detail.json() == item


async def test_settled_request_offers_nothing(
    api: httpx.AsyncClient, inbox_token: str, seeded: Any, world: Any
) -> None:
    calls = world.owner.item_calls
    item = (await api.get(f"/owner/{BAKERY}/requests/c_done1", headers=bearer(inbox_token))).json()
    assert item["allowed_decisions"] == [] and item["response_fields"] == [] and item["task_status"] is None
    assert world.owner.item_calls == calls  # nothing to look up for a settled request


async def test_unknown_request_is_404(api: httpx.AsyncClient, inbox_token: str, seeded: Any) -> None:
    response = await api.get(f"/owner/{BAKERY}/requests/c_nope", headers=bearer(inbox_token))
    assert response.status_code == 404 and response.json() == {"error": "Not found.", "code": "not_found"}


async def test_other_tenants_request_is_404(api: httpx.AsyncClient, inbox_token: str, seeded: Any) -> None:
    assert (await api.get(f"/owner/{BAKERY}/requests/c_salon1", headers=bearer(inbox_token))).status_code == 404
    response = await api.post(
        f"/owner/{BAKERY}/requests/c_salon1/decision", json={"decision": "answer", "note": "x"},
        headers=bearer(inbox_token),
    )
    assert response.status_code == 404


async def test_unknown_business_is_404(api: httpx.AsyncClient, app_signer: OwnerLinkSigner) -> None:
    token = app_signer.sign(business_id="ghost-shop", scope="inbox")
    assert (await api.get("/owner/ghost-shop/inbox", headers=bearer(token))).status_code == 404


async def test_malformed_ids_are_400(api: httpx.AsyncClient, inbox_token: str) -> None:
    assert (await api.get("/owner/Thimbleberry_Bakehouse!/inbox", headers=bearer(inbox_token))).status_code == 400
    response = await api.get(f"/owner/{BAKERY}/requests/{'c' * 80}", headers=bearer(inbox_token))
    assert response.status_code == 400


# --------------------------------------------------------------------------- decisions


async def test_approve_completes_the_task(
    api: httpx.AsyncClient, inbox_token: str, seeded: Any, world: Any
) -> None:
    response = await api.post(
        f"/owner/{BAKERY}/requests/c_pending1/decision",
        json={"decision": "approve", "note": "See you Sunday"},
        headers=bearer(inbox_token),
    )
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["commitment"]["status"] == "approved"
    assert item["commitment"]["decision"]["note"] == "See you Sunday"
    assert item["task_status"] == "completed" and item["allowed_decisions"] == []

    record = await world.deps.task_manager.get("t_pending1")
    assert record.status == "completed"
    assert record.result["structuredContent"] == {"status": "approved", "reference": "c_pending1"}
    (business_id, commitment_id, decision) = world.owner.decisions[-1]
    assert (business_id, commitment_id, decision.decision) == (BAKERY, "c_pending1", "approve")


async def test_decision_twice_is_409(api: httpx.AsyncClient, inbox_token: str, seeded: Any) -> None:
    url = f"/owner/{BAKERY}/requests/c_pending1/decision"
    assert (await api.post(url, json={"decision": "approve"}, headers=bearer(inbox_token))).status_code == 200
    response = await api.post(url, json={"decision": "decline"}, headers=bearer(inbox_token))
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_state"


async def test_decision_on_cancelled_task_is_409(
    api: httpx.AsyncClient, inbox_token: str, seeded: Any, world: Any
) -> None:
    await world.deps.task_manager.cancel("t_pending1")  # the customer cancelled the task
    response = await api.post(
        f"/owner/{BAKERY}/requests/c_pending1/decision", json={"decision": "approve"}, headers=bearer(inbox_token)
    )
    assert response.status_code == 409


async def test_decision_not_allowed_is_400(api: httpx.AsyncClient, inbox_token: str, seeded: Any) -> None:
    response = await api.post(
        f"/owner/{BAKERY}/requests/c_pending1/decision", json={"decision": "quote"}, headers=bearer(inbox_token)
    )
    assert response.status_code == 400
    assert response.json() == {"error": "decision quote is not allowed for this request", "code": "invalid_decision"}


@pytest.mark.parametrize(
    "body",
    [{"decision": "maybe"}, {}, {"decision": "approve", "note": "x" * 1001}, {"decision": "approve", "values": "x"}],
)
async def test_invalid_decision_body_is_400(
    api: httpx.AsyncClient, inbox_token: str, seeded: Any, body: dict[str, Any]
) -> None:
    response = await api.post(
        f"/owner/{BAKERY}/requests/c_pending1/decision", json=body, headers=bearer(inbox_token)
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == "invalid_request" and payload["details"]
    assert inbox_token not in response.text


async def test_malformed_json_is_400(api: httpx.AsyncClient, inbox_token: str, seeded: Any) -> None:
    response = await api.post(
        f"/owner/{BAKERY}/requests/c_pending1/decision",
        content=b"{not json",
        headers={**bearer(inbox_token), "content-type": "application/json"},
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------- admin link minting


async def test_admin_mints_an_inbox_link(api: httpx.AsyncClient, app_signer: OwnerLinkSigner) -> None:
    response = await api.post(f"/owner/{BAKERY}/links", headers=bearer(DEV_ADMIN_TOKEN), params={"ttl_s": 3600})
    assert response.status_code == 200, response.text
    body = response.json()
    url = urlsplit(body["inbox_url"])
    assert url.path == f"/owner/{BAKERY}/inbox"
    token = parse_qs(url.query)["token"][0]
    assert app_signer.verify(token, business_id=BAKERY).scope == "inbox"
    assert body["expires_at"] == "2026-10-14T17:00:00Z"
    assert (await api.get(f"/owner/{BAKERY}/inbox", params={"token": token})).status_code == 200


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer wrong"}, {"authorization": DEV_ADMIN_TOKEN}])
async def test_admin_route_needs_the_admin_token(api: httpx.AsyncClient, headers: dict[str, str]) -> None:
    assert_unauthorized(await api.post(f"/owner/{BAKERY}/links", headers=headers))


async def test_owner_link_token_is_not_an_admin_token(api: httpx.AsyncClient, inbox_token: str) -> None:
    assert_unauthorized(await api.post(f"/owner/{BAKERY}/links", headers=bearer(inbox_token)))


async def test_admin_link_for_unknown_business_is_404(api: httpx.AsyncClient) -> None:
    assert (await api.post("/owner/ghost-shop/links", headers=bearer(DEV_ADMIN_TOKEN))).status_code == 404


async def test_admin_ttl_is_bounded(api: httpx.AsyncClient) -> None:
    for ttl in ("1", str(31 * 24 * 3600)):
        response = await api.post(f"/owner/{BAKERY}/links", headers=bearer(DEV_ADMIN_TOKEN), params={"ttl_s": ttl})
        assert response.status_code == 400


def prod_settings(**overrides: Any) -> Settings:
    return Settings(
        env="prod",
        storage="dynamodb",
        request_state_keys=[SecretStr("prod-request-state-key-0123456789abcdef")],
        owner_link_keys=[SecretStr("prod-owner-link-key-0123456789abcdef0123")],
        id_key=SecretStr("prod-id-key-0123456789abcdef0123456789"),
        allowed_hosts=["testserver"],
        public_base_url="https://mcp.example.com",
        **overrides,
    )


async def prod_app(world_factory: Any, settings: Settings) -> FastAPI:
    return create_app(settings, deps=world_factory(settings).deps)


async def test_admin_route_absent_in_prod_without_a_real_token(world_factory: Any) -> None:
    app = await prod_app(world_factory, prod_settings())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(f"/owner/{BAKERY}/links", headers=bearer(DEV_ADMIN_TOKEN))
    assert response.status_code in (404, 405)


async def test_admin_route_enabled_in_prod_with_a_real_token(world_factory: Any) -> None:
    real = "prod-admin-token-0123456789abcdef"
    app = await prod_app(world_factory, prod_settings(admin_token=SecretStr(real)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        assert (await client.post(f"/owner/{BAKERY}/links", headers=bearer(DEV_ADMIN_TOKEN))).status_code == 401
        assert (await client.post(f"/owner/{BAKERY}/links", headers=bearer(real))).status_code == 200


async def test_round2_prod_safety_admin_route_off_when_a_public_host_is_configured_without_env_prod(
    world_factory: Any,
) -> None:
    """SEC-5 R1: env forgotten, memory storage, but ALLOWED_HOSTS names the public host: the published
    DEV admin token must not mint links there."""
    settings = Settings(
        env="dev",
        request_state_keys=[SecretStr("prod-request-state-key-0123456789abcdef")],
        owner_link_keys=[SecretStr("prod-owner-link-key-0123456789abcdef0123")],
        id_key=SecretStr("prod-id-key-0123456789abcdef0123456789"),
        allowed_hosts=["mcp.example.com"],
        cors_origins=["https://owner.example.com"],
        allowed_origins=["https://owner.example.com"],
    )
    assert settings.looks_deployed and not settings.admin_enabled
    app = await prod_app(world_factory, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.com") as client:
        response = await client.post(f"/owner/{BAKERY}/links", headers=bearer(DEV_ADMIN_TOKEN))
    assert response.status_code in (404, 405)


# --------------------------------------------------------------------------- fallback decision options


def test_fallback_decision_options(world: Any) -> None:
    from askonce_server.app.owner_api import decision_options
    from askonce_server.domain.services.owner import ANSWER_FIELD

    bakery = world.profiles._profiles[BAKERY]
    pending = world.commitment("c_f1")
    assert decision_options(bakery, pending) == (
        ["approve", "decline", "counter_offer", "request_info"],
        [{"key": "final_price", "type": "money", "label": "Final price", "required": False}],
    )
    waiting = world.commitment("c_f2", status="info_requested")
    assert decision_options(bakery, waiting)[0] == ["approve", "decline", "counter_offer"]  # one question at a time
    question = world.commitment("c_f3", module_id="owner", kind="owner_question")
    assert decision_options(bakery, question) == (["answer"], [dict(ANSWER_FIELD)])
    assert decision_options(bakery, world.commitment("c_f4", status="approved")) == ([], [])
    assert decision_options(bakery, world.commitment("c_f5", module_id="gone")) == ([], [])
