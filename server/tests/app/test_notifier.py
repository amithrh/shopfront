"""The owner_web notifier: a signed decision link per pending request, logged without PII."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from askonce_server.app.links import OwnerLinkSigner
from askonce_server.app.notifier import OutboxMessage, OwnerWebNotifier
from askonce_server.domain.ports import OwnerNotifier
from askonce_server.observability import id_digest

pytestmark = pytest.mark.anyio


async def test_notify_records_a_decision_link(world: Any, app_signer: OwnerLinkSigner) -> None:
    notifier = OwnerWebNotifier(app_signer)
    commitment = world.commitment("c_new1")
    await notifier.notify(object(), commitment)  # type: ignore[arg-type]  # the profile is not needed

    (message,) = notifier.outbox
    assert isinstance(message, OutboxMessage)
    assert (message.business_id, message.commitment_id, message.kind) == ("thimbleberry-bakehouse", "c_new1", "custom_order")
    url = urlsplit(message.link)
    assert url.path == "/owner/thimbleberry-bakehouse/requests/c_new1"
    token = parse_qs(url.query)["token"][0]
    claims = app_signer.verify(token, business_id="thimbleberry-bakehouse", commitment_id="c_new1")
    assert claims.scope == "decide"
    assert notifier.messages_for("thimbleberry-bakehouse") == [message]
    assert notifier.messages_for("quillfern-salon") == []


async def test_notify_logs_ids_only(world: Any, app_signer: OwnerLinkSigner, caplog: pytest.LogCaptureFixture) -> None:
    notifier = OwnerWebNotifier(app_signer)
    with caplog.at_level(logging.INFO, logger="askonce.notifier"):
        await notifier.notify(object(), world.commitment("c_new2"))  # type: ignore[arg-type]
    # The commitment id is a bearer capability without auth: only its digest is logged (SEC-4).
    assert f"business=thimbleberry-bakehouse commitment={id_digest('c_new2')} kind=custom_order" in caplog.text
    for secret_or_pii in ("Sam Lee", "+12175550100", "token=", notifier.outbox[0].link, "c_new2"):
        assert secret_or_pii not in caplog.text


async def test_outbox_is_bounded(world: Any, app_signer: OwnerLinkSigner) -> None:
    notifier = OwnerWebNotifier(app_signer, max_messages=2)
    for n in range(3):
        await notifier.notify(object(), world.commitment(f"c_b{n}"))  # type: ignore[arg-type]
    assert [m.commitment_id for m in notifier.outbox] == ["c_b1", "c_b2"]


def test_notifier_satisfies_the_domain_port(app_signer: OwnerLinkSigner) -> None:
    assert isinstance(OwnerWebNotifier(app_signer), OwnerNotifier)
