"""M11-D4: the minimal read-only Cable HTTP surface (Gate-frozen, three GETs).

Read-only by contract: no side effect, no runtime approval, no audit event.
Error mapping is frozen: 404 cross-domain / 409 stale / framework 422 /
500 data-integrity (a corrupt payload in storage is a server data failure,
never relabelled as a client 422) / 500 with the real error for storage
faults. The export endpoint streams the D3 artifact bytes untouched.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from .cable_export import CableExportError, export_cable_document
from .cable_service import CableDocumentNotFoundError, CableService
from .cable_validation import CableValidationError, assess_cable_document
from .store import SQLiteDocumentStore


def create_cable_router(store: SQLiteDocumentStore) -> APIRouter:
    router = APIRouter(prefix="/api/v2/cable", tags=["P&ID-Agent cable"])
    service = CableService(store)

    def _load(document_id: str):
        try:
            return service.load(document_id)
        except CableDocumentNotFoundError:
            raise HTTPException(
                status_code=404,
                detail={"code": "cable_document_not_found", "message": f"cable document {document_id!r} not found"},
            ) from None
        except Exception as exc:
            import pydantic

            if isinstance(exc, pydantic.ValidationError):
                # a corrupt payload in storage is a server data failure,
                # never relabelled as a client error
                raise HTTPException(
                    status_code=500,
                    detail={"code": "cable_data_integrity", "message": "stored cable payload is corrupt"},
                ) from exc
            raise  # storage faults keep their truth (real 500 via FastAPI)

    @router.get("/documents")
    def list_documents() -> list[dict]:
        """Frozen shape: [{document_id, name, revision, readiness_state}].
        A corrupt stored payload fails closed as 500 data-integrity."""
        entries = []
        for document_id, _revision, data_json in store.list_cable_documents():
            try:
                view = _load(document_id)
                readiness = assess_cable_document(service, document_id)
            except HTTPException:
                raise
            entries.append(
                {
                    "document_id": document_id,
                    "name": view.document.name,
                    "revision": readiness.revision,
                    "readiness_state": readiness.state,
                }
            )
        return entries

    @router.get("/documents/{document_id}")
    def document_detail(document_id: str) -> dict:
        view = _load(document_id)
        try:
            readiness = assess_cable_document(service, document_id)
        except CableValidationError as exc:
            if exc.code == "invalid_cable_payload":
                raise HTTPException(
                    status_code=500,
                    detail={"code": "cable_data_integrity", "message": "stored cable payload is corrupt"},
                ) from exc
            raise
        return {
            "document_id": view.document_id,
            "revision": readiness.revision,
            "schema": view.document.schema,
            "name": view.document.name,
            "segments": [segment.model_dump(mode="json") for segment in view.document.segments],
            "readiness": {
                "state": readiness.state,
                "counts": readiness.counts,
                "reasons": list(readiness.reasons),
                "result_hash": readiness.result_hash,
                "profile_id": readiness.profile_id,
                "profile_version": readiness.profile_version,
                "profile_fingerprint": readiness.profile_fingerprint,
            },
        }

    @router.get("/documents/{document_id}/export.zip")
    def export_document(
        document_id: str,
        expected_revision: int = Query(..., ge=0),
    ) -> Response:
        try:
            artifact = export_cable_document(
                service, document_id, expected_revision=expected_revision
            )
        except CableExportError as exc:
            if exc.code == "cable_document_not_found":
                raise HTTPException(
                    status_code=404,
                    detail={"code": "cable_document_not_found", "message": str(exc)},
                ) from exc
            if exc.code == "stale_revision":
                raise HTTPException(
                    status_code=409,
                    detail={"code": "stale_revision", "message": str(exc)},
                ) from exc
            if exc.code == "invalid_cable_payload":
                raise HTTPException(
                    status_code=500,
                    detail={"code": "cable_data_integrity", "message": "stored cable payload is corrupt"},
                ) from exc
            raise HTTPException(status_code=500, detail={"code": exc.code, "message": str(exc)}) from exc
        return Response(
            artifact.zip_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{document_id}-r{expected_revision}.zip"'
            },
        )

    return router
