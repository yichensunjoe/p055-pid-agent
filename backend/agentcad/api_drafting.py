"""Deterministic drafting surfaces (M3).

Charter reference: §15 (port-aware routing, collision detection, annotation
placement, region relayout, manual lock, crossing vs junction), §21.3, §48.

Both routes here are **read routes**, and that is the point: the drafting engine is
preview-only, so it cannot become a second write path beside the one governed
transaction channel (Charter §7, P0-2). ``/drafting/preview`` returns a complete,
reproducible transaction; applying it stays with the human editor path
(``POST /api/v2/documents/{id}/transactions``) or the audited agent path, exactly like
any other edit. A drawing therefore cannot be changed by drafting without the same
permission, revision check and audit record as a manual edit.
"""

from __future__ import annotations

from time import perf_counter

from fastapi import APIRouter, HTTPException

from .diagnostics import DiagnosticLogger
from .drafting_engine import DraftingEngine
from .drafting_models import DraftingPreview, DraftingReport, DraftingRequest
from .service import (
    DocumentNotFoundError,
    DocumentService,
    InvalidOperationError,
    RevisionConflictError,
)


def create_drafting_router(
    service: DocumentService,
    diagnostics: DiagnosticLogger | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["P&ID-Agent deterministic drafting"])
    engine = DraftingEngine(service)

    def _scope(request: DraftingRequest) -> dict:
        return {
            "scope_kind": "region"
            if request.region is not None
            else "selection"
            if request.element_ids
            else "document",
            "region": request.region.model_dump(mode="json") if request.region else None,
            "scope_element_count": len(request.element_ids),
            "locked_element_count": len(request.locked_element_ids),
            "relayout": request.relayout,
            "reroute_connectors": request.reroute_connectors,
            "place_annotations": request.place_annotations,
            "bridge_crossings": request.bridge_crossings,
            "resolve_collisions": request.resolve_collisions,
            "target_score": request.policy.target_score,
        }

    @router.post(
        "/documents/{document_id}/drafting/report",
        response_model=DraftingReport,
    )
    def drafting_report(document_id: str, request: DraftingRequest | None = None) -> DraftingReport:
        """Measure one drawing state against the drafting and drawing rules.

        Read-only analysis: ports, crossings, junctions, collisions, findings, lock
        provenance and the deterministic pass/fail gate. Nothing is written and no
        transaction is proposed.
        """

        request = request or DraftingRequest()
        try:
            result = engine.report(document_id, request)
        except DocumentNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail=f"document not found: {document_id}"
            ) from exc
        except InvalidOperationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if diagnostics is not None:
            diagnostics.emit(
                "drafting.report.completed",
                document_id=document_id,
                revision=result.revision,
                scope_kind=result.scope_kind,
                score=result.score,
                gate_passed=result.gate.passed,
                finding_count=len(result.findings),
                blocker_count=len(result.gate.blockers),
                port_count=len(result.ports),
                crossing_count=len(result.crossings),
                junction_count=len(result.junctions),
                locked_element_ids=result.locks.locked_element_ids,
            )
        return result

    @router.post(
        "/documents/{document_id}/drafting/preview",
        response_model=DraftingPreview,
    )
    def preview_drafting(document_id: str, request: DraftingRequest) -> DraftingPreview:
        """Preview a deterministic drafting run without writing the document.

        Returns the exact transaction, the before/after metrics, the findings the run
        could not fix, and a reproducibility digest. The transaction is applied by the
        caller through the normal governed write channel.
        """

        started = perf_counter()
        if diagnostics is not None:
            diagnostics.emit(
                "drafting.preview.started",
                document_id=document_id,
                expected_revision=request.expected_revision,
                **_scope(request),
            )
        try:
            result = engine.preview(document_id, request)
        except DocumentNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail=f"document not found: {document_id}"
            ) from exc
        except RevisionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidOperationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if diagnostics is not None:
            diagnostics.emit(
                "drafting.preview.completed",
                document_id=document_id,
                revision=result.current_revision,
                duration_ms=round((perf_counter() - started) * 1000, 2),
                operation_count=result.reproducibility.operation_count,
                settled=result.settled,
                moved_element_ids=result.moved_element_ids,
                rerouted_connector_ids=result.rerouted_connector_ids,
                moved_annotation_ids=result.moved_annotation_ids,
                bridged_connector_ids=result.bridged_connector_ids,
                skipped_locked_element_ids=result.skipped_locked_element_ids,
                score_before=result.metrics.before.score,
                score_after=result.metrics.after.score,
                regressions=result.metrics.regressions,
                improvements=result.metrics.improvements,
                gate_passed=result.gate.passed,
                blocker_codes=sorted({finding.code for finding in result.gate.blockers}),
                transaction_digest=result.reproducibility.transaction_digest,
                warning_count=len(result.warnings),
            )
        return result

    return router


__all__ = ["create_drafting_router"]
