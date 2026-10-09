"""M6 Phase-2B D3: the review HTTP surface for semantic candidates.

Frozen scope (D3 CODE GO): two read routes and one decision route, nothing else. The reads
are pure (no audit, no write); the decision route is a ``governance_write`` — it never moves
an engineering revision — and every recorded decision carries its audit fact in the same
transaction. Reviewer identity is declared by the caller and recorded verbatim with
``identity_assurance="declared"``; a shared deployment's service token authenticates the
request and is never mapped to a person.

Routes are named ``/m6/candidates`` on purpose: the contract's forbidden-surface tokens are
narrowed word by word as slices register real surfaces (Gate Q2), and this slice registers
only what it serves — ``candidate``; the queue words stay forbidden.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from .config import Settings
from .m6_candidate_core import (
    CandidateNotFound,
    IllegalTransition,
    NotConfirmedError,
    TransitionEvidenceMissing,
)
from .m6_candidate_models import ConflictResolutionChoice
from .m6_review_service import M6ReviewService, ReviewSurfaceError
from .m6_source_adapter import CandidateIdentityConflict, SourceVerificationError
from .service import DocumentService
from .store import SQLiteDocumentStore
from .symbols import SymbolRegistry


class _DecisionRequest(BaseModel):
    """One review decision. ``reviewer_identity`` is the caller's *declared* identity."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["confirm", "reject", "recheck", "resolve_conflict", "reassign"]
    reviewer_identity: str
    reviewer_action: str = ""
    note: str = ""
    conflict_resolution: str = ""
    resolution_choice: ConflictResolutionChoice | None = None
    element_refs: list[str] | None = None
    equipment_tag: str = ""


def create_m6_review_router(
    service: DocumentService,
    store: SQLiteDocumentStore,
    settings: Settings,
) -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["P&ID-Agent M6 review"])
    reviews = M6ReviewService(
        store, SymbolRegistry(), deployment_mode=settings.deployment_mode
    )

    def _reject(exc: Exception) -> HTTPException:
        if isinstance(exc, ReviewSurfaceError):
            if exc.code in {"candidate_not_found", "source_unknown"}:
                status = 404
            elif exc.code in {"decision_state_moved", "decision_log_out_of_order"}:
                status = 409
            else:
                status = 422
            return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})
        if isinstance(exc, (SourceVerificationError, CandidateIdentityConflict)):
            return HTTPException(
                status_code=409, detail={"code": exc.code, "message": str(exc)}
            )
        if isinstance(exc, CandidateNotFound):
            return HTTPException(
                status_code=404, detail={"code": exc.code, "message": str(exc)}
            )
        if isinstance(exc, (IllegalTransition, NotConfirmedError)):
            return HTTPException(
                status_code=409, detail={"code": exc.code, "message": str(exc)}
            )
        if isinstance(exc, TransitionEvidenceMissing):
            return HTTPException(
                status_code=422, detail={"code": exc.code, "message": str(exc)}
            )
        raise exc

    def _require_source_document(document_id: str) -> None:
        """404 only when neither the document nor any candidate about it exists.

        A deleted source keeps its review queue readable (the evidence outlives the
        drawing by design); decisions on it still refuse through source verification.
        """

        if store.get(document_id) is not None:
            return
        if store.list_semantic_candidates(source_document_id=document_id):
            return
        raise HTTPException(
            status_code=404,
            detail={
                "code": "source_unknown",
                "message": f"no document and no review queue for {document_id!r}",
            },
        )

    @router.get("/documents/{document_id}/m6/candidates")
    def list_candidates(
        document_id: str,
        status: str | None = None,
        offset: int = 0,
        limit: int = 200,
    ) -> dict:
        _require_source_document(document_id)
        try:
            return reviews.list_candidates(
                document_id, status=status, offset=offset, limit=limit
            )
        except ReviewSurfaceError as exc:
            raise _reject(exc) from exc

    @router.get("/documents/{document_id}/m6/candidates/{candidate_id}")
    def candidate_detail(document_id: str, candidate_id: str) -> dict:
        _require_source_document(document_id)
        try:
            return reviews.candidate_detail(document_id, candidate_id)
        except ReviewSurfaceError as exc:
            raise _reject(exc) from exc

    @router.post("/documents/{document_id}/m6/candidates/{candidate_id}/decisions")
    def record_decision(
        document_id: str, candidate_id: str, payload: _DecisionRequest
    ) -> dict:
        _require_source_document(document_id)
        candidate = store.get_semantic_candidate(candidate_id)
        if candidate is None or candidate.artifact.source_document_id != document_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "candidate_not_found",
                    "message": f"unknown candidate {candidate_id!r} for source {document_id!r}",
                },
            )
        try:
            outcome = reviews.decide(
                candidate_id,
                action=payload.action,
                reviewer_identity=payload.reviewer_identity,
                reviewer_action=payload.reviewer_action,
                note=payload.note,
                conflict_resolution=payload.conflict_resolution,
                resolution_choice=payload.resolution_choice,
                element_refs=payload.element_refs,
                equipment_tag=payload.equipment_tag,
            )
        except (
            ReviewSurfaceError,
            SourceVerificationError,
            CandidateIdentityConflict,
            CandidateNotFound,
            IllegalTransition,
            NotConfirmedError,
            TransitionEvidenceMissing,
        ) as exc:
            raise _reject(exc) from exc
        return outcome.model_dump(mode="json")

    return router


__all__ = ["create_m6_review_router"]
