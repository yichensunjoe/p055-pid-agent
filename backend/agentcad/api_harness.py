from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .harness import (
    AgentHarnessService,
    AgentSessionNotFoundError,
    HarnessError,
    ToolApprovalNotFoundError,
    ToolApprovalRejectedError,
    ToolApprovalRequiredError,
    ToolIntentMismatchError,
    ToolPermissionDeniedError,
)
from .service import DocumentNotFoundError
from .harness_models import (
    AgentSession,
    AgentSessionAudit,
    AgentSessionCreateRequest,
    ToolApproval,
    ToolApprovalCreateRequest,
    ToolApprovalResolveRequest,
)


def _raise_harness_error(exc: Exception):
    if isinstance(
        exc,
        (AgentSessionNotFoundError, ToolApprovalNotFoundError, DocumentNotFoundError),
    ):
        raise HTTPException(
            status_code=404,
            detail={"error": exc.code, "message": str(exc), "retryable": False},
        ) from exc
    if isinstance(exc, ToolPermissionDeniedError):
        raise HTTPException(
            status_code=403,
            detail={"error": exc.code, "message": str(exc), "retryable": False},
        ) from exc
    if isinstance(
        exc,
        (ToolApprovalRequiredError, ToolApprovalRejectedError, ToolIntentMismatchError),
    ):
        raise HTTPException(
            status_code=409,
            detail={"error": exc.code, "message": str(exc), "retryable": False},
        ) from exc
    if isinstance(exc, HarnessError):
        raise HTTPException(
            status_code=409,
            detail={"error": exc.code, "message": str(exc), "retryable": False},
        ) from exc
    raise exc


def create_harness_router(harness: AgentHarnessService) -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["AgentCAD harness"])

    @router.post("/agent/sessions", response_model=AgentSession, status_code=201)
    def create_session(request: AgentSessionCreateRequest):
        try:
            return harness.create_session(request)
        except Exception as exc:
            return _raise_harness_error(exc)

    @router.get("/agent/sessions/{session_id}", response_model=AgentSession)
    def get_session(session_id: str):
        try:
            return harness.get_session(session_id)
        except Exception as exc:
            return _raise_harness_error(exc)

    @router.get("/agent/sessions/{session_id}/audit", response_model=AgentSessionAudit)
    def get_session_audit(session_id: str):
        try:
            return harness.audit(session_id)
        except Exception as exc:
            return _raise_harness_error(exc)

    @router.post(
        "/agent/sessions/{session_id}/approvals",
        response_model=ToolApproval,
        status_code=201,
    )
    def request_approval(session_id: str, request: ToolApprovalCreateRequest):
        try:
            return harness.request_approval(session_id, request)
        except Exception as exc:
            return _raise_harness_error(exc)

    @router.post("/agent/approvals/{approval_id}/resolve", response_model=ToolApproval)
    def resolve_approval(approval_id: str, request: ToolApprovalResolveRequest):
        try:
            return harness.resolve_approval(approval_id, request)
        except Exception as exc:
            return _raise_harness_error(exc)

    return router
