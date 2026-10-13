"""AskOnce exception hierarchy.

Tools translate these into grounded results or `ToolError`s (whose text reaches the model verbatim,
so messages here must never contain secrets or PII). The owner API maps them to HTTP status codes.
"""

from __future__ import annotations


class AskOnceError(Exception):
    """Base class. `code` is a stable machine-readable reason."""

    code: str = "error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class UnknownBusiness(AskOnceError):
    """No live profile with that id (also used for non-live profiles: indistinguishable on purpose)."""

    code = "unknown_business"


class UnknownModule(AskOnceError):
    code = "unknown_module"


class UnknownItem(AskOnceError):
    code = "unknown_item"


class ProfileInvalid(AskOnceError):
    """A profile failed schema validation or a loader invariant. `report` lists every violation."""

    code = "profile_invalid"

    def __init__(self, message: str, *, report: object | None = None) -> None:
        super().__init__(message)
        self.report = report


class ConcurrencyConflict(AskOnceError):
    """A compare-and-swap write lost a race (stale revision or version)."""

    code = "conflict"


class NotFound(AskOnceError):
    code = "not_found"


class InvalidState(AskOnceError):
    """A state-machine transition is not allowed from the current status."""

    code = "invalid_state"


class InvalidDecision(AskOnceError):
    """An owner decision is not allowed by the module's approval block, or its fields are invalid."""

    code = "invalid_decision"


class AuthError(AskOnceError):
    """Missing, invalid or expired owner link token."""

    code = "unauthorized"


class DisputedFact(AskOnceError):
    """A rule the answer depends on is disputed (an open contradiction/confirm question, L6).

    `fact_ref` names the withheld fact; tools turn this into a withheld answer with a hand-off.
    """

    code = "disputed_fact"

    def __init__(self, message: str, *, fact_ref: str, question_id: str | None = None) -> None:
        super().__init__(message)
        self.fact_ref = fact_ref
        self.question_id = question_id
