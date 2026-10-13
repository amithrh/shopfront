"""Owner REST API (JSON) for the phone-friendly owner page. All routes need a signed link token.

Routes (token in `Authorization: Bearer <token>`, `?token=` or, for the decision POST, a JSON body field
`token`; responses never include other tenants):

  GET  /owner/{business_id}/inbox?status=open|all&cursor=&limit=   -> InboxResponse
       pending requests with allowed decisions and response fields (from the module's approval block)
  GET  /owner/{business_id}/requests/{commitment_id}        -> InboxItem
       an open request exactly as the inbox lists it (customer answers folded in); a settled one with
       nothing left to decide
  POST /owner/{business_id}/requests/{commitment_id}/decision  body: OwnerDecisionInput (+ optional token)
       -> InboxItem; 400 invalid decision/fields, 404 unknown, 409 not pending (task cancelled/expired)
  POST /owner/{business_id}/links  (admin bearer token; not registered in prod without a real token)
       -> {"inbox_url": ..., "expires_at": ...}   mints an inbox link for the demo

Error mapping (`install_error_handlers`, body `{"error", "code"}`): errors.AuthError -> 401 (generic
body, `WWW-Authenticate: Bearer`), NotFound / UnknownBusiness -> 404, InvalidDecision and malformed
requests -> 400, a body over `MAX_BODY_BYTES` -> 413, InvalidState / ConcurrencyConflict (and a task
that is gone or already terminal) -> 409. The link token is checked before any query or body field is
validated, so an unauthenticated caller learns nothing about the request beyond "401". The decision
POST reads its body itself (FastAPI would buffer and parse any size before running the auth
dependency): a header or query token is verified before a single body byte is read, and the body is
read up to `MAX_BODY_BYTES` only (`read_capped_body`). Tenant isolation: the token's business must equal the
path's business, a decide token is bound to one commitment, and every store read is scoped by the
path's business (the domain service re-checks ownership on decisions).

Every response carries `Cache-Control: no-store` and `Referrer-Policy: no-referrer` (bodies contain the
customer's contact details; URLs may carry the token).

This module deliberately avoids `from __future__ import annotations`: FastAPI evaluates the annotations
of the route and dependency closures defined inside `build_owner_router`.
"""

import hmac
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp_tasks import InvalidTransition, TaskNotFound
from pydantic import BaseModel, Field, JsonValue, ValidationError

from askonce_server.app.links import AUTH_FAILED, MAX_TOKEN_LENGTH, LinkClaims, OwnerLinkSigner
from askonce_server.clock import utc_iso
from askonce_server.deps import AskOnceDeps
from askonce_server.domain.models import (
    ALL_STATUSES,
    DECISION_TO_STATUS,
    OPEN_STATUSES,
    Commitment,
    DecisionKind,
    can_transition,
)
from askonce_server.domain.services.owner import ANSWER_FIELD, InboxItem, OwnerDecisionInput
from askonce_server.errors import (
    AuthError,
    ConcurrencyConflict,
    InvalidDecision,
    InvalidState,
    NotFound,
    UnknownBusiness,
    UnknownModule,
)
from askonce_server.observability import ATTR_BUSINESS, ATTR_COMMITMENT, aspan, id_digest
from askonce_server.profile.model import Profile

logger = logging.getLogger("askonce.owner_api")

ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
"""The schema's `$defs/id` pattern (business ids); commitment ids (`c_<base32>`) match it too."""

MIN_LINK_TTL_S = 60
MAX_LINK_TTL_S = 30 * 24 * 3600
MAX_INBOX_PAGE = 100

MAX_BODY_BYTES = 16 * 1024
"""Largest owner API request body (a decision with a note is well under 2 KB)."""

SECURITY_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

BusinessIdPath = Annotated[str, Path(pattern=ID_PATTERN, description="Business (profile) id.")]
CommitmentIdPath = Annotated[str, Path(pattern=ID_PATTERN, description="Commitment (request) id.")]
AuthorizationHeader = Annotated[str | None, Header(description="`Bearer <owner link token>`.")]
TokenQuery = Annotated[
    str | None, Query(max_length=MAX_TOKEN_LENGTH, description="Owner link token (alternative to the header).")
]


class InboxResponse(BaseModel):
    business_id: str
    business_name: str
    items: list[InboxItem]
    next_cursor: str | None = None


class DecisionBody(OwnerDecisionInput):
    """`OwnerDecisionInput` plus an optional `token`, for clients that cannot set headers."""

    token: str | None = Field(default=None, max_length=MAX_TOKEN_LENGTH)


class LinkResponse(BaseModel):
    inbox_url: str
    expires_at: str


# --------------------------------------------------------------------------- errors


class BodyTooLarge(Exception):
    """A request body over `MAX_BODY_BYTES` (413)."""


class MalformedBody(Exception):
    """A decision body that is not a JSON object (400, after authentication)."""


@dataclass(frozen=True)
class _ErrorSpec:
    status: int
    code: str
    message: str | None
    """Fixed message, or None to use the exception's own (domain messages carry no secrets or PII)."""


_NO_LONGER_PENDING = "This request is no longer waiting for a decision."

_ERROR_SPECS: tuple[tuple[type[Exception], _ErrorSpec], ...] = (
    (AuthError, _ErrorSpec(401, "unauthorized", AUTH_FAILED)),
    (NotFound, _ErrorSpec(404, "not_found", "Not found.")),
    (UnknownBusiness, _ErrorSpec(404, "not_found", "Not found.")),
    (InvalidDecision, _ErrorSpec(400, "invalid_decision", None)),
    (InvalidState, _ErrorSpec(409, "invalid_state", _NO_LONGER_PENDING)),
    (ConcurrencyConflict, _ErrorSpec(409, "conflict", _NO_LONGER_PENDING)),
    (TaskNotFound, _ErrorSpec(409, "invalid_state", _NO_LONGER_PENDING)),
    (InvalidTransition, _ErrorSpec(409, "invalid_state", _NO_LONGER_PENDING)),
    (BodyTooLarge, _ErrorSpec(413, "too_large", "Request body too large.")),
    (MalformedBody, _ErrorSpec(400, "invalid_request", "Invalid request.")),
)


def error_response(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    """The owner API's error shape: `{"error": <message>, "code": <code>, ...}` plus security headers."""
    headers = dict(SECURITY_HEADERS)
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse({"error": message, "code": code, **extra}, status_code=status, headers=headers)


async def _handle_known_error(_request: Request, exc: Exception) -> JSONResponse:
    for exc_type, spec in _ERROR_SPECS:
        if isinstance(exc, exc_type):
            return error_response(spec.status, spec.code, spec.message or str(exc) or spec.code)
    raise exc  # pragma: no cover - handlers are only registered for the types above


async def _handle_validation_error(_request: Request, exc: Exception) -> JSONResponse:
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    # Locations and messages only: inputs are never echoed (they may contain a token or PII).
    details = [{"loc": [str(part) for part in e.get("loc", ())], "msg": str(e.get("msg", ""))} for e in errors]
    return error_response(400, "invalid_request", "Invalid request.", details=details)


def install_error_handlers(app: FastAPI) -> None:
    """Map domain and auth errors to the owner API's JSON errors, and request validation to 400."""
    for exc_type, _spec in _ERROR_SPECS:
        app.add_exception_handler(exc_type, _handle_known_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)


# --------------------------------------------------------------------------- helpers


def _plain(value: Any) -> Any:
    """Deep copy of a read-only profile view into plain JSON types."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _approval_block(profile: Profile, module_id: str) -> Mapping[str, Any]:
    try:
        module = profile.module(module_id)
    except (UnknownModule, LookupError):
        return {}
    approval = module.get("approval")
    return approval if isinstance(approval, Mapping) else {}


def decision_options(profile: Profile, commitment: Commitment) -> tuple[list[DecisionKind], list[dict[str, JsonValue]]]:
    """What the owner may decide on `commitment` now, and the fields they fill in (fallback view).

    Used for requests the open inbox does not list: settled requests offer nothing; an open request
    offers the module's `approval.decisions` (owner questions: `answer` with the domain's answer
    field) that the status machine allows from its current status.
    """
    if commitment.status not in OPEN_STATUSES:
        return [], []
    candidates: list[DecisionKind]
    fields: list[dict[str, JsonValue]] = []
    if commitment.kind == "owner_question":
        candidates = ["answer"]
        fields = [dict(ANSWER_FIELD)]
    else:
        approval = _approval_block(profile, commitment.module_id)
        candidates = [d for d in approval.get("decisions", ()) if d in DECISION_TO_STATUS]
        fields = [_plain(f) for f in approval.get("response_fields", ()) if isinstance(f, Mapping)]
    allowed = [d for d in candidates if can_transition(commitment.status, DECISION_TO_STATUS[d])]
    return allowed, fields


def _header_or_query_token(authorization: str | None, token: str | None) -> str | None:
    """`Authorization: Bearer` first, then `?token=`; None when neither is given. A malformed header is
    a 401 (never a fallback to another source)."""
    if authorization is not None:
        scheme, _, value = authorization.strip().partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            raise AuthError(AUTH_FAILED)
        return value.strip()
    return token or None


async def read_capped_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    """The request body, refusing (`BodyTooLarge`) as soon as it is known to exceed `limit` bytes:
    by `Content-Length` before reading, or while streaming a body sent without one."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise BodyTooLarge
        except ValueError:
            raise MalformedBody from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _json_object(raw: bytes) -> dict[str, Any] | None:
    """`raw` parsed as a JSON object, else None."""
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _security_headers(response: Response) -> None:
    response.headers.update(SECURITY_HEADERS)


# --------------------------------------------------------------------------- router


def build_owner_router(deps: AskOnceDeps, signer: OwnerLinkSigner) -> APIRouter:
    """The `/owner` router (see module docstring). Call `install_error_handlers(app)` on the app too."""
    router = APIRouter(prefix="/owner", tags=["owner"], dependencies=[Depends(_security_headers)])
    settings = deps.settings

    async def owner_claims(
        request: Request,
        business_id: BusinessIdPath,
        authorization: AuthorizationHeader = None,
        token: TokenQuery = None,
    ) -> LinkClaims:
        """Authenticate the owner link for the path's business (and commitment, if any)."""
        raw = _header_or_query_token(authorization, token)
        if raw is None:
            raise AuthError(AUTH_FAILED)
        return signer.verify(raw, business_id=business_id, commitment_id=request.path_params.get("commitment_id"))

    Claims = Annotated[LinkClaims, Depends(owner_claims)]

    async def load_profile(business_id: str) -> Profile:
        profile = await deps.profiles.get(business_id)
        if profile is None:
            raise NotFound("Not found.")
        return profile

    async def task_status(task_id: str | None) -> str | None:
        if task_id is None:
            return None
        try:
            return (await deps.task_manager.get(task_id)).status
        except TaskNotFound:
            return None

    async def inbox_item(profile: Profile, commitment: Commitment) -> InboxItem:
        """Open requests: exactly what the inbox shows. Settled ones: the record, nothing to decide."""
        if commitment.status in OPEN_STATUSES:
            item = await deps.owner.item(commitment.business_id, commitment.commitment_id)
            if item is not None:
                return item
        allowed, fields = decision_options(profile, commitment)
        return InboxItem(
            commitment=commitment,
            task_status=await task_status(commitment.task_id),
            allowed_decisions=allowed,
            response_fields=fields,
        )

    @router.get("/{business_id}/inbox", response_model=InboxResponse, summary="Pending requests")
    async def get_inbox(
        business_id: BusinessIdPath,
        _claims: Claims,
        status: Annotated[Literal["open", "all"], Query()] = "open",
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_INBOX_PAGE)] = 50,
    ) -> InboxResponse:
        async with aspan("askonce.owner_api.inbox", **{ATTR_BUSINESS: business_id}):
            profile = await load_profile(business_id)
            # explicit sets: OwnerService.inbox treats `statuses=None` as "open only"
            statuses = ALL_STATUSES if status == "all" else OPEN_STATUSES
            items, next_cursor = await deps.owner.inbox(business_id, statuses=statuses, limit=limit, cursor=cursor)
        return InboxResponse(
            business_id=business_id, business_name=profile.name, items=items, next_cursor=next_cursor
        )

    @router.get("/{business_id}/requests/{commitment_id}", response_model=InboxItem, summary="One request")
    async def get_request(business_id: BusinessIdPath, commitment_id: CommitmentIdPath, _claims: Claims) -> InboxItem:
        attributes = {ATTR_BUSINESS: business_id, ATTR_COMMITMENT: id_digest(commitment_id)}
        async with aspan("askonce.owner_api.request", **attributes):
            profile = await load_profile(business_id)
            commitment = await deps.commitments.get(business_id, commitment_id)
            if commitment is None:
                raise NotFound("Not found.")
            return await inbox_item(profile, commitment)

    async def decision_body(
        request: Request, business_id: str, commitment_id: str, authorization: str | None, token: str | None
    ) -> DecisionBody:
        """Authenticate, then read and validate the decision body (see module docstring).

        With a header or query token nothing is read before the token verifies. Only a client that
        cannot set either sends the token in the (size-capped) body; a body without a usable token is a
        401 whatever else is wrong with it."""
        raw_token = _header_or_query_token(authorization, token)
        if raw_token is not None:
            signer.verify(raw_token, business_id=business_id, commitment_id=commitment_id)
            payload = _json_object(await read_capped_body(request))
            if payload is None:
                raise MalformedBody
        else:
            payload = _json_object(await read_capped_body(request))
            body_token = (payload or {}).get("token")
            if not isinstance(body_token, str) or not body_token or len(body_token) > MAX_TOKEN_LENGTH:
                raise AuthError(AUTH_FAILED)
            signer.verify(body_token, business_id=business_id, commitment_id=commitment_id)
        try:
            return DecisionBody.model_validate(payload)
        except ValidationError as exc:
            raise RequestValidationError(
                [{**error, "loc": ("body", *error.get("loc", ()))} for error in exc.errors(include_input=False)]
            ) from None

    @router.post(
        "/{business_id}/requests/{commitment_id}/decision",
        response_model=InboxItem,
        summary="Decide a request",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": DecisionBody.model_json_schema()}},
            }
        },
    )
    async def post_decision(
        business_id: BusinessIdPath,
        commitment_id: CommitmentIdPath,
        request: Request,
        authorization: AuthorizationHeader = None,
        token: TokenQuery = None,
    ) -> InboxItem:
        body = await decision_body(request, business_id, commitment_id, authorization, token)
        attributes = {ATTR_BUSINESS: business_id, ATTR_COMMITMENT: id_digest(commitment_id), "askonce.decision": body.decision}
        async with aspan("askonce.owner_api.decide", **attributes):
            profile = await load_profile(business_id)
            decision = OwnerDecisionInput.model_validate(body.model_dump(exclude={"token"}))
            commitment = await deps.owner.decide(business_id, commitment_id, decision)
            logger.info(
                "owner decision: business=%s commitment=%s decision=%s status=%s",
                business_id,
                id_digest(commitment_id),
                decision.decision,
                commitment.status,
            )
            return await inbox_item(profile, commitment)

    if settings.admin_enabled:

        @router.post("/{business_id}/links", response_model=LinkResponse, summary="Mint an inbox link (admin)")
        async def mint_link(
            business_id: BusinessIdPath,
            authorization: AuthorizationHeader = None,
            ttl_s: Annotated[int | None, Query(ge=MIN_LINK_TTL_S, le=MAX_LINK_TTL_S)] = None,
        ) -> LinkResponse:
            expected = settings.admin_token.get_secret_value().encode()
            scheme, _, given = (authorization or "").strip().partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip().encode(), expected):
                raise AuthError(AUTH_FAILED)
            await load_profile(business_id)
            ttl = ttl_s or settings.owner_link_ttl_s
            url = signer.inbox_url(business_id, ttl_s=ttl)
            logger.info("admin minted an inbox link: business=%s ttl_s=%d", business_id, ttl)
            return LinkResponse(inbox_url=url, expires_at=utc_iso(deps.clock.now() + timedelta(seconds=ttl)))

    return router
