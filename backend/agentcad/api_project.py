"""M12-D5: read-only project inspection surface (Gate CODE GO).

Frozen scope: GET-only, zero audit. Project summary, active cross-domain
links, project readiness (evaluation_as_of is a REQUIRED, timezone-aware
semantic input), and the deterministic package download. Build preconditions
surface as stable errors: 404 project_not_found, 409 link_pin_not_current /
member_revision_not_current / empty_project, 422 invalid evaluation_as_of.
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Response

from .cable_service import CableService
from .project_package import (
    ProjectPackageError,
    build_project_package,
)
from .project_readiness import (
    ProjectReadinessError,
    ProjectReadinessService,
)
from .service import DocumentService
from .store import SQLiteDocumentStore


def _parse_as_of(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_evaluation_as_of", "message": "not an ISO-8601 timestamp"},
        ) from None
    if parsed.tzinfo is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_evaluation_as_of",
                "message": "evaluation_as_of must be timezone-aware",
            },
        )
    return parsed


def _package_error(exc: ProjectPackageError) -> HTTPException:
    mapping = {
        "project_not_found": 404,
        "link_pin_not_current": 409,
        "member_revision_not_current": 409,
        "member_pins_mismatch": 409,
        "package_state_changed": 409,
        "empty_project": 409,
        "invalid_input": 422,
    }
    status = mapping.get(exc.code, 500)
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


def create_project_router(
    store: SQLiteDocumentStore,
    pid_service: DocumentService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v2/projects", tags=["P&ID-Agent project"])

    def _readiness() -> ProjectReadinessService:
        return ProjectReadinessService(store, pid_service, CableService(store))

    def _require_project(project_id: str) -> str:
        project = store.get_project(project_id)
        if project is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "project_not_found", "message": f"project {project_id!r} not found"},
            )
        return project[1]

    @router.get("/{project_id}")
    def project_summary(project_id: str) -> dict:
        name = _require_project(project_id)
        members = []
        for document_id, domain, added_at in store.list_project_documents(project_id):
            if domain == "cable":
                envelope = store.get_cable_envelope(document_id)
                revision = None if envelope is None else int(envelope[0])
            else:
                stored = store.get(document_id)
                revision = None if stored is None else stored.document.revision
            members.append(
                {"document_id": document_id, "domain": domain, "revision": revision, "added_at": added_at}
            )
        return {"project_id": project_id, "name": name, "members": members}

    @router.get("/{project_id}/links")
    def project_links(project_id: str) -> dict:
        _require_project(project_id)
        links = [
            {
                "link_id": str(row["link_id"]),
                "relation_type": str(row["relation_type"]),
                "source_document_id": str(row["source_document_id"]),
                "source_object_ref": str(row["source_object_ref"]),
                "source_endpoint": str(row["source_endpoint"]),
                "target_document_id": str(row["target_document_id"]),
                "target_object_ref": str(row["target_object_ref"]),
                "pinned_source_revision": int(row["pinned_source_revision"]),
                "pinned_target_revision": int(row["pinned_target_revision"]),
            }
            for row in store.list_active_engineering_links(project_id)
        ]
        return {"project_id": project_id, "links": links}

    @router.get("/{project_id}/readiness")
    def project_readiness(project_id: str, evaluation_as_of: str = Query(...)) -> dict:
        _require_project(project_id)
        as_of = _parse_as_of(evaluation_as_of)
        try:
            readiness = _readiness().assess(project_id=project_id, evaluation_as_of=as_of)
        except ProjectReadinessError as exc:
            raise HTTPException(
                status_code=404, detail={"code": exc.code, "message": str(exc)}
            ) from exc
        return {
            "project_id": readiness.project_id,
            "evaluation_as_of": readiness.evaluation_as_of.isoformat(),
            "state": readiness.state,
            "issues": [
                {"code": issue.code, "severity": issue.severity, "link_id": issue.link_id}
                for issue in readiness.issues
            ],
            "members": [
                {
                    "document_id": snapshot.document_id,
                    "domain": snapshot.domain,
                    "state": snapshot.state,
                    "readiness_hash": snapshot.readiness_hash,
                    "revision": snapshot.revision,
                }
                for snapshot in readiness.members
            ],
            "result_hash": readiness.result_hash,
            "profile_id": readiness.profile_id,
            "profile_version": readiness.profile_version,
            "profile_fingerprint": readiness.profile_fingerprint,
        }

    @router.get("/{project_id}/package.zip")
    def project_package(
        project_id: str,
        evaluation_as_of: str = Query(...),
        pins: str = Query(..., description="JSON object {document_id: pinned_revision}"),
    ) -> Response:
        _require_project(project_id)
        as_of = _parse_as_of(evaluation_as_of)
        try:
            parsed_pins = json.loads(pins)
            if not isinstance(parsed_pins, dict) or not all(
                isinstance(key, str) and isinstance(value, int)
                for key, value in parsed_pins.items()
            ):
                raise ValueError("pins must be a JSON object of document_id -> int")
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_pins", "message": str(exc)},
            ) from exc
        try:
            package = build_project_package(
                store=store,
                pid_service=pid_service,
                cable_service=CableService(store),
                project_id=project_id,
                member_pins=parsed_pins,
                evaluation_as_of=as_of,
            )
        except ProjectPackageError as exc:
            raise _package_error(exc) from exc
        return Response(
            package,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{project_id}-package.zip"'
            },
        )

    return router
