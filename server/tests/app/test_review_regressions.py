"""Regression tests for confirmed review findings in the app layer.

SEC-4: owner link tokens and capability ids never reach logs or spans in the clear.
SEC-9: the owner decision endpoint authenticates before reading a body, and never reads a big one.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from shopfront_server.app import run
from shopfront_server.app.links import OwnerLinkSigner
from shopfront_server.app.owner_api import MAX_BODY_BYTES
from shopfront_server.observability import RedactSecretsFilter, id_digest, redact_secrets, uvicorn_log_config
from shopfront_server.tools.context import finish

pytestmark = pytest.mark.anyio

BAKERY = "thimbleberry-bakehouse"
DECISION = f"/owner/{BAKERY}/requests/c_pending1/decision"


# --------------------------------------------------------------------------- SEC-4


def _access_record(path: str) -> logging.LogRecord:
    """A record shaped exactly like uvicorn's access log line (`h11_impl` / `httptools_impl`)."""
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:5000", "GET", path, "1.1", 200), None
    )


def test_access_log_lines_never_carry_owner_tokens(app_signer: OwnerLinkSigner) -> None:
    url = app_signer.inbox_url(BAKERY)
    token = url.split("token=", 1)[1]
    record = _access_record(url.split("testserver", 1)[-1])
    assert RedactSecretsFilter().filter(record) is True
    line = record.getMessage()
    assert token not in line and "token=[redacted]" in line and "/owner/thimbleberry-bakehouse/inbox" in line
    assert redact_secrets("Authorization: Bearer abc.def") == "Authorization: Bearer [redacted]"


def test_uvicorn_runs_with_the_redacting_log_config(monkeypatch: pytest.MonkeyPatch) -> None:
    config = uvicorn_log_config()
    assert "redact_secrets" in config["handlers"]["access"]["filters"]
    assert "redact_secrets" in config["handlers"]["default"]["filters"]
    seen: dict[str, Any] = {}
    for name in list(os.environ):
        if name.startswith("SHOPFRONT_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(run, "create_app", lambda settings: object())
    monkeypatch.setattr(run, "configure_logging", lambda level: None)
    monkeypatch.setattr(run.uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))
    run.main()
    assert seen["log_config"] == config


def test_capability_ids_are_digested_in_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    from shopfront_server import observability
    from shopfront_server.tools.schemas import CommitmentResult

    recorded: dict[str, Any] = {}
    monkeypatch.setattr(observability, "set_attributes", lambda span, **attrs: recorded.update(attrs))
    finish(CommitmentResult(speak="Done.", status="confirmed"), None, outcome="ok", commitment_id="c_secret", task_id="t_secret")
    assert recorded["shopfront.commitment_id"] == id_digest("c_secret")
    assert recorded["shopfront.task_id"] == id_digest("t_secret")
    assert "c_secret" not in str(recorded) and "t_secret" not in str(recorded)


# --------------------------------------------------------------------------- SEC-9


@pytest.fixture
def inbox_token(app_signer: OwnerLinkSigner) -> str:
    return app_signer.sign(business_id=BAKERY, scope="inbox")


@pytest.fixture
async def raw_api(owner_app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as client:
        yield client


async def test_a_large_declared_body_is_refused_before_anything_runs(raw_api: httpx.AsyncClient, seeded: Any) -> None:
    big = b'{"decision": "approve", "note": "' + b"x" * (MAX_BODY_BYTES + 1) + b'"}'
    response = await raw_api.post(DECISION, content=big, headers={"content-type": "application/json"})
    assert response.status_code == 413 and response.json()["code"] == "too_large"


async def test_a_large_streamed_body_is_cut_off_while_reading(
    raw_api: httpx.AsyncClient, seeded: Any, inbox_token: str
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"decision": "approve", "note": "'
        for _ in range(64):
            yield b"x" * 1024  # 64 KB without a Content-Length
        yield b'"}'

    # No token in the header or query: the body is read (capped) only to look for one.
    response = await raw_api.post(DECISION, content=chunks(), headers={"content-type": "application/json"})
    assert response.status_code == 413


async def test_auth_comes_before_body_parsing(raw_api: httpx.AsyncClient, seeded: Any, inbox_token: str) -> None:
    # Malformed JSON without a token is a plain 401: no parser details before authentication.
    response = await raw_api.post(DECISION, content=b'{"decision": ', headers={"content-type": "application/json"})
    assert response.status_code == 401 and "details" not in response.json()
    # A bad header token is refused before the body is even read.
    response = await raw_api.post(DECISION, content=b"not json", headers={"authorization": "Bearer nope"})
    assert response.status_code == 401
    # Authenticated, a malformed body is a 400; a valid one is decided.
    auth = {"authorization": f"Bearer {inbox_token}", "content-type": "application/json"}
    assert (await raw_api.post(DECISION, content=b'{"decision": ', headers=auth)).status_code == 400
    response = await raw_api.post(DECISION, json={"decision": "approve"}, headers={"authorization": f"Bearer {inbox_token}"})
    assert response.status_code == 200, response.text
