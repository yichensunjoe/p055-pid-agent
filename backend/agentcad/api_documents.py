from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .audit import request_audit_context
from .audit_models import AuditContext
from .models import Document, HistoryEntry
from .service import DocumentService, context_label, revision_snapshot
from .store import StoreRevisionConflictError


class RenameDocumentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=0)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.strip().split())
        if not normalized:
            raise ValueError("document name cannot be empty")
        return normalized


class MoveDocumentFolderRequest(BaseModel):
    folder_id: str = Field(default="", max_length=120)
    expected_revision: int = Field(ge=0)


class CanvasGridRequest(BaseModel):
    grid_size: float = Field(ge=1, le=100)
    expected_revision: int = Field(ge=0)


def _require_current_document(
    service: DocumentService,
    document_id: str,
    expected_revision: int,
):
    stored = service.store.get(document_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"document not found: {document_id}")
    document = stored.document
    if document.revision != expected_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                f"expected revision {expected_revision}, "
                f"current revision is {document.revision}"
            ),
        )
    return stored, document


def _save_document_mutation(
    service: DocumentService,
    stored,
    document: Document,
    *,
    previous_revision: int,
    label: str,
    before: Document,
    context: AuditContext,
) -> Document:
    """Persist a document-level mutation (rename / folder move) with provenance.

    These endpoints used to write a revision and a history row with no semantic diff
    and no audit record, so a rename or folder move was invisible to review. They now
    go through the same provenance builder as every other write, and the revision,
    its diff and its audit record commit in one SQLite transaction.
    """
    document.revision += 1
    document.updated_at = datetime.now(UTC)
    bundle = service.audit.build_revision_provenance(
        before=before,
        after=document,
        operations=None,
        request=None,
        action="transaction",
        source="web",
        context=context,
    )
    try:
        service.store.save(
            stored,
            expected_revision=previous_revision,
            history=HistoryEntry(
                document_id=document.id,
                revision=document.revision,
                source="web",
                action="transaction",
                label=context_label(context, label),
                operation_count=1,
            ),
            history_details=bundle.history_details,
            audit=bundle.audit,
        )
    except StoreRevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return document


def create_documents_router(service: DocumentService) -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["P&ID-Agent documents"])

    @router.put("/documents/{document_id}/name", response_model=Document)
    def rename_document(document_id: str, request: RenameDocumentRequest):
        stored, document = _require_current_document(
            service, document_id, request.expected_revision
        )
        if document.name == request.name:
            return document

        previous_revision = document.revision
        before = revision_snapshot(document)
        stored.undo_stack.append(document.model_dump(mode="json"))
        if len(stored.undo_stack) > service.history_limit:
            stored.undo_stack = stored.undo_stack[-service.history_limit :]
        stored.redo_stack.clear()
        document.name = request.name
        return _save_document_mutation(
            service,
            stored,
            document,
            previous_revision=previous_revision,
            label=f"Rename document to {request.name}",
            before=before,
            context=request_audit_context(
                "rename_document",
                label=f"Rename document to {request.name}",
            ),
        )

    @router.put("/documents/{document_id}/folder", response_model=Document)
    def move_document_folder(document_id: str, request: MoveDocumentFolderRequest):
        stored, document = _require_current_document(
            service, document_id, request.expected_revision
        )
        current_folder = str(document.metadata.get("folder_id", ""))
        target_folder = request.folder_id.strip()
        if current_folder == target_folder:
            return document

        previous_revision = document.revision
        before = revision_snapshot(document)
        stored.undo_stack.append(document.model_dump(mode="json"))
        if len(stored.undo_stack) > service.history_limit:
            stored.undo_stack = stored.undo_stack[-service.history_limit :]
        stored.redo_stack.clear()
        next_metadata = dict(document.metadata)
        if target_folder:
            next_metadata["folder_id"] = target_folder
        else:
            next_metadata.pop("folder_id", None)
        document.metadata = next_metadata
        return _save_document_mutation(
            service,
            stored,
            document,
            previous_revision=previous_revision,
            label=f"Move document to folder {target_folder or 'root'}",
            before=before,
            context=request_audit_context(
                "move_document_folder",
                label=f"Move document to folder {target_folder or 'root'}",
            ),
        )

    @router.put("/documents/{document_id}/canvas-grid", response_model=Document)
    def project_canvas_grid(document_id: str, request: CanvasGridRequest):
        _, document = _require_current_document(
            service, document_id, request.expected_revision
        )
        if document.canvas.grid_size == request.grid_size:
            return document

        # Grid density is an editor interaction preference. Return a projected view
        # without changing the engineering document, history, or revision.
        projected = document.model_copy(deep=True)
        projected.canvas.grid_size = request.grid_size
        return projected

    return router
