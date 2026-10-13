"""Owner notification for the `owner_web` channel (domain port `OwnerNotifier`).

No SMS (US 10DLC registration is out of scope): the owner page polls the inbox API. The notifier
records a signed decision link per pending request in a bounded in-memory outbox (for the demo and
tests) and logs it at INFO without PII (business id, commitment id, kind only). The link itself is a
bearer credential, so it is kept in the outbox but never logged.
"""

from __future__ import annotations

import logging
from collections import deque

from pydantic import BaseModel

from shopfront_server.app.links import OwnerLinkSigner
from shopfront_server.domain.models import Commitment
from shopfront_server.observability import ATTR_BUSINESS, ATTR_COMMITMENT, id_digest, span
from shopfront_server.profile.model import Profile

logger = logging.getLogger("shopfront.notifier")


class OutboxMessage(BaseModel):
    business_id: str
    commitment_id: str
    kind: str
    link: str


class OwnerWebNotifier:
    """`OwnerNotifier` for the owner_web channel: one signed decision link per pending request."""

    def __init__(self, signer: OwnerLinkSigner, *, max_messages: int = 500) -> None:
        self._signer = signer
        self.outbox: deque[OutboxMessage] = deque(maxlen=max_messages)

    async def notify(self, profile: Profile, commitment: Commitment) -> None:
        """Record a decision link for `commitment` (the owner page also lists it in the inbox)."""
        attributes = {ATTR_BUSINESS: commitment.business_id, ATTR_COMMITMENT: id_digest(commitment.commitment_id)}
        with span("shopfront.owner.notify", **attributes):
            link = self._signer.decision_url(commitment.business_id, commitment.commitment_id)
            self.outbox.append(
                OutboxMessage(
                    business_id=commitment.business_id,
                    commitment_id=commitment.commitment_id,
                    kind=commitment.kind,
                    link=link,
                )
            )
            logger.info(
                "owner notified: business=%s commitment=%s kind=%s",
                commitment.business_id,
                id_digest(commitment.commitment_id),
                commitment.kind,
            )

    def messages_for(self, business_id: str) -> list[OutboxMessage]:
        """Outbox messages for one business, oldest first (demo and tests)."""
        return [m for m in self.outbox if m.business_id == business_id]
