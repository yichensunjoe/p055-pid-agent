"""Audit / provenance read surfaces (Charter §19, §21.6 Reviewability).

Read-only by design: the chain is only ever appended through the engineering write
paths. Verification re-reads and recomputes the whole chain, so a reviewer does not
have to trust the stored hashes.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response

from .audit_models import AuditRecord, AuditVerification, RevisionEvidence
from .service import DocumentNotFoundError, DocumentService


def create_audit_router(service: DocumentService) -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["P&ID-Agent audit"])

    def _require_document(document_id: str):
        try:
            return service.get_document(document_id)
        except DocumentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"document not found: {document_id}") from exc

    @router.get("/audit/records", response_model=list[AuditRecord])
    def audit_records(
        document_id: str | None = None,
        event_type: str | None = None,
        actor: str | None = None,
        status: str | None = None,
        tool: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: Annotated[int, Query(ge=1, le=5000)] = 200,
    ):
        return service.audit.audit_trail(
            document_id=document_id,
            event_type=event_type,
            actor=actor,
            status=status,
            tool_name=tool,
            since=since,
            until=until,
            limit=limit,
        )

    @router.get("/audit/verify", response_model=AuditVerification)
    def audit_verify():
        return service.audit.verify_chain()

    @router.get("/audit/export")
    def audit_export(
        document_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
    ):
        package = service.audit.export_package(document_id=document_id, limit=limit)
        suffix = f"-{document_id}" if document_id else ""
        return Response(
            json.dumps(package, ensure_ascii=False, indent=2, default=str),
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="pid-agent-audit{suffix}.json"'
                ),
                "X-PID-Agent-Audit-Records": str(len(package["records"])),
                "X-PID-Agent-Audit-Chain-Ok": str(package["verification"]["ok"]).lower(),
            },
        )

    @router.get("/documents/{document_id}/audit", response_model=list[AuditRecord])
    def document_audit(
        document_id: str,
        limit: Annotated[int, Query(ge=1, le=5000)] = 200,
    ):
        _require_document(document_id)
        return service.audit.audit_trail(document_id=document_id, limit=limit)

    @router.get(
        "/documents/{document_id}/history/{revision}/evidence",
        response_model=RevisionEvidence,
    )
    def revision_evidence(document_id: str, revision: int):
        _require_document(document_id)
        if service.store.get_history_revision_detailed(document_id, revision) is None:
            raise HTTPException(
                status_code=404,
                detail=f"history revision not found: {document_id}@{revision}",
            )
        return service.audit.revision_evidence(document_id, revision)

    return router


__all__ = ["create_audit_router"]
