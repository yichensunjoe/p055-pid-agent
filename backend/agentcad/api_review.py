"""M9-WS1: the review workflow HTTP surface.

Actor trust (Gate-frozen v1 local-operator mode): the server mints one operator
token per process. A loopback-only bootstrap endpoint hands it to the local UI;
every human decision carries it in ``X-Operator-Token``, is compared in constant
time, and only counts from a loopback peer — the token is human authority solely
on the local machine. The token never enters audit, logs or diagnostics. Requests
without a valid token act as ``agent`` — able to request approvals and comment,
never to resolve, reopen or decide. Shared deployments (or a local deployment
without an explicitly configured operator identity) fail closed: no bootstrap,
no human decisions.
"""

from __future__ import annotations

import hmac
import secrets
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .config import Settings
from .review_models import review_snapshot_digest
from .review_service import (
    Actor,
    ActorNotTrustedError,
    ApprovalNotFoundError,
    ReviewService,
    ReviewStateConflict,
    ReviewThreadNotFoundError,
    ReviewWorkflowError,
)
from .service import DocumentService
from .store import SQLiteDocumentStore
from .validation_profile import load_profile

_OPERATOR_TOKEN_HEADER = "X-Operator-Token"


class _ThreadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str
    element_id: str | None = None
    subject_text: str = ""
    expected_governance_seq: int


class _CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str
    expected_governance_seq: int


class _Resolve(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution_note: str
    expected_governance_seq: int


class _GovernanceSeq(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_governance_seq: int


class _Decide(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    expected_governance_seq: int


def _loopback(host: str | None) -> bool:
    # "testclient" is Starlette's in-process TestClient peer: it never opens a
    # socket, so admitting it cannot widen a real deployment's attack surface.
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


def create_review_router(
    service: DocumentService,
    store: SQLiteDocumentStore,
    settings: Settings,
) -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["P&ID-Agent review workflow"])
    operator_token = secrets.token_hex(32)
    reviews = ReviewService(store, service, readiness_profile=load_profile())

    def _operator_enabled() -> bool:
        return settings.deployment_mode == "local" and bool(settings.operator_identity)

    def _actor(request: Request) -> Actor:
        presented = request.headers.get(_OPERATOR_TOKEN_HEADER, "")
        # Round-2 Gate freeze: the token is human authority only on a loopback
        # peer. A valid token presented from any other peer degrades to agent —
        # it can comment and request, but resolve/reopen/decide still fail closed
        # with 403 actor_not_trusted.
        peer = request.client.host if request.client else None
        if (
            _operator_enabled()
            and _loopback(peer)
            and presented
            and hmac.compare_digest(presented.encode(), operator_token.encode())
        ):
            return Actor(identity=str(settings.operator_identity), kind="operator")
        return Actor(identity="agent", kind="agent")

    def _reject(exc: ReviewWorkflowError) -> HTTPException:
        status = 422
        if isinstance(exc, ReviewStateConflict):
            status = 409
        elif isinstance(exc, ActorNotTrustedError):
            status = 403
        elif isinstance(exc, (ReviewThreadNotFoundError, ApprovalNotFoundError)):
            status = 404
        return HTTPException(
            status_code=status,
            detail={"code": exc.code, "message": str(exc), **exc.details},
        )

    def _view(document_id: str) -> dict[str, Any]:
        state = reviews.get_state(document_id)
        document = service.get_document(document_id)
        elements = {element.id for element in document.elements}
        threads = [
            {
                **thread.model_dump(mode="json"),
                "derived_stale": thread.anchor_revision != document.revision,
                "derived_orphaned": thread.element_id is not None
                and thread.element_id not in elements,
            }
            for thread in state.threads
        ]
        approvals = []
        current_digest = review_snapshot_digest(state)
        for approval in state.approvals:
            liveness = "historical"
            if approval.status in {"requested", "approved"}:
                # Round-2 Gate freeze: derived liveness compares BOTH bindings —
                # the engineering revision and the review snapshot digest the
                # approval was requested against. Either one moving marks the
                # approval stale at read time even before a mutation persists it.
                if approval.engineering_revision != document.revision:
                    liveness = "stale"
                elif approval.review_snapshot_digest != current_digest:
                    liveness = "stale"
                else:
                    liveness = "live"
            approvals.append(
                {**approval.model_dump(mode="json"), "derived_liveness": liveness}
            )
        return {
            "document_id": document_id,
            "governance_seq": state.governance_seq,
            "review_snapshot_digest": reviews.snapshot_digest(document_id),
            "operator_enabled": _operator_enabled(),
            "threads": threads,
            "comments": [comment.model_dump(mode="json") for comment in state.comments],
            "approvals": approvals,
        }

    @router.post("/review/operator-session")
    def operator_session(request: Request) -> dict[str, Any]:
        """Loopback-only bootstrap: hand the local UI its operator token."""

        if not _loopback(request.client.host if request.client else None):
            raise HTTPException(status_code=403, detail={"code": "actor_not_trusted"})
        if not _operator_enabled():
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "actor_not_trusted",
                    "message": "human review operations require a local deployment "
                    "with PID_AGENT_OPERATOR_IDENTITY configured",
                },
            )
        return {"operator_token": operator_token, "identity": settings.operator_identity}

    @router.get("/documents/{document_id}/review")
    def review_state(document_id: str) -> dict[str, Any]:
        service.get_document(document_id)  # 404 for unknown documents
        return _view(document_id)

    @router.post("/documents/{document_id}/review/threads")
    def create_thread(document_id: str, payload: _ThreadCreate, request: Request) -> dict[str, Any]:
        service.get_document(document_id)
        try:
            reviews.create_thread(
                document_id=document_id,
                actor=_actor(request),
                body=payload.body,
                element_id=payload.element_id,
                subject_text=payload.subject_text,
                expected_governance_seq=payload.expected_governance_seq,
            )
        except ReviewWorkflowError as exc:
            raise _reject(exc) from exc
        return _view(document_id)

    @router.post("/documents/{document_id}/review/threads/{thread_id}/comments")
    def add_comment(document_id: str, thread_id: str, payload: _CommentCreate, request: Request) -> dict[str, Any]:
        try:
            reviews.add_comment(
                document_id=document_id,
                thread_id=thread_id,
                actor=_actor(request),
                body=payload.body,
                expected_governance_seq=payload.expected_governance_seq,
            )
        except ReviewWorkflowError as exc:
            raise _reject(exc) from exc
        return _view(document_id)

    @router.post("/documents/{document_id}/review/threads/{thread_id}/resolve")
    def resolve_thread(document_id: str, thread_id: str, payload: _Resolve, request: Request) -> dict[str, Any]:
        try:
            reviews.resolve_thread(
                document_id=document_id,
                thread_id=thread_id,
                actor=_actor(request),
                resolution_note=payload.resolution_note,
                expected_governance_seq=payload.expected_governance_seq,
            )
        except ReviewWorkflowError as exc:
            raise _reject(exc) from exc
        return _view(document_id)

    @router.post("/documents/{document_id}/review/threads/{thread_id}/reopen")
    def reopen_thread(document_id: str, thread_id: str, payload: _GovernanceSeq, request: Request) -> dict[str, Any]:
        try:
            reviews.reopen_thread(
                document_id=document_id,
                thread_id=thread_id,
                actor=_actor(request),
                expected_governance_seq=payload.expected_governance_seq,
            )
        except ReviewWorkflowError as exc:
            raise _reject(exc) from exc
        return _view(document_id)

    @router.post("/documents/{document_id}/approval/request")
    def request_approval(document_id: str, payload: _GovernanceSeq, request: Request) -> dict[str, Any]:
        service.get_document(document_id)
        try:
            reviews.request_approval(
                document_id=document_id,
                actor=_actor(request),
                expected_governance_seq=payload.expected_governance_seq,
            )
        except ReviewWorkflowError as exc:
            raise _reject(exc) from exc
        return _view(document_id)

    @router.post("/documents/{document_id}/approval/{approval_id}/decide")
    def decide_approval(document_id: str, approval_id: str, payload: _Decide, request: Request) -> dict[str, Any]:
        try:
            reviews.decide_approval(
                document_id=document_id,
                approval_id=approval_id,
                actor=_actor(request),
                decision=payload.decision,
                expected_governance_seq=payload.expected_governance_seq,
            )
        except ReviewWorkflowError as exc:
            raise _reject(exc) from exc
        return _view(document_id)

    return router
