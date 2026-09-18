from __future__ import annotations

import json
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import ValidationError

from .diagnostics import DiagnosticLogger
from .history_diff import build_history_details
from .llm import OpenAICompatiblePlanner, PlannerError
from .models import (
    AgentGenerateRequest,
    AgentGenerateResult,
    CreateDocumentRequest,
    Document,
    HistorySource,
    ProviderConfig,
    TransactionRequest,
    TransactionResult,
)
from .project_io import (
    ImportConflictPolicy,
    ImportResult,
    ProjectIOError,
    ProjectSettings,
)
from .provider_discovery import discover_provider_models
from .service import (
    DocumentNotFoundError,
    DocumentService,
    InvalidOperationError,
    RevisionConflictError,
)
from .svg import render_png, render_svg
from .tool_registry import get_default_tool_registry


def _record_revision_details(
    service: DocumentService,
    before: Document,
    after: Document,
    request: TransactionRequest | None,
    *,
    action: str,
    source: HistorySource,
    diagnostics: DiagnosticLogger | None,
) -> dict[str, Any]:
    details = build_history_details(
        before,
        after,
        request.operations if request else None,
        action=action,
    )
    persisted = service.store.update_history_details(after.id, after.revision, details)
    if diagnostics is not None:
        diagnostics.emit(
            "document.revision.created",
            document_id=after.id,
            base_revision=before.revision,
            revision=after.revision,
            source=source,
            action=action,
            label=request.label if request else action.title(),
            operation_count=len(request.operations) if request else 1,
            affected_element_ids=details["affected_element_ids"],
            added_element_ids=details["added_element_ids"],
            updated_element_ids=details["updated_element_ids"],
            deleted_element_ids=details["deleted_element_ids"],
            history_details_persisted=persisted,
        )
    return details


def _apply_transaction_with_details(
    service: DocumentService,
    document_id: str,
    request: TransactionRequest,
    *,
    source: HistorySource,
    diagnostics: DiagnosticLogger | None,
) -> TransactionResult:
    before = service.get_document(document_id)
    result = service.apply_transaction(document_id, request, source=source)
    _record_revision_details(
        service,
        before,
        result.document,
        request,
        action="transaction",
        source=source,
        diagnostics=diagnostics,
    )
    return result


def _validate_transaction(
    service: DocumentService,
    document_id: str,
    request: TransactionRequest,
) -> dict[str, Any]:
    current = service.get_document(document_id)
    if request.expected_revision is not None and request.expected_revision != current.revision:
        raise RevisionConflictError(
            f"expected revision {request.expected_revision}, current revision is {current.revision}"
        )

    working = Document.model_validate(current.model_dump(mode="python"))
    for index, operation in enumerate(request.operations):
        try:
            service._apply_operation(working, operation)
        except InvalidOperationError as exc:
            raise InvalidOperationError(
                f"operations[{index}] ({operation.op}): {exc}"
            ) from exc

    working.revision = current.revision + 1
    try:
        working = Document.model_validate(working.model_dump(mode="python"))
    except ValidationError as exc:
        raise InvalidOperationError(f"resulting document is invalid: {exc}") from exc
    details = build_history_details(
        current,
        working,
        request.operations,
        action="preview",
    )
    return {
        "valid": True,
        "document_id": document_id,
        "current_revision": current.revision,
        "next_revision": current.revision + 1,
        "operation_count": len(request.operations),
        "resulting_element_count": len(working.elements),
        "affected_element_ids": details["affected_element_ids"],
        "added_element_ids": details["added_element_ids"],
        "updated_element_ids": details["updated_element_ids"],
        "deleted_element_ids": details["deleted_element_ids"],
        "change_count": details["change_count"],
    }


def create_v2_router(
    service: DocumentService,
    planner: OpenAICompatiblePlanner,
    diagnostics: DiagnosticLogger | None = None,
    version: str = "unknown",
) -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["P&ID-Agent v2"])

    @router.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "service": "P&ID-Agent", "api_version": "v2"}

    @router.get("/agent/tools")
    def agent_tools() -> dict[str, Any]:
        """Return the canonical machine-readable AgentCAD/P&ID tool catalog."""
        return get_default_tool_registry().catalog()

    @router.get("/agent/runtime-config")
    def agent_runtime_config() -> dict[str, float | None]:
        return {
            "default_timeout_seconds": (
                min(120.0, planner.max_timeout_seconds)
                if planner.max_timeout_seconds is not None
                else None
            ),
            "max_timeout_seconds": planner.max_timeout_seconds,
        }

    @router.get("/documents")
    def list_documents():
        return service.list_documents()

    @router.post("/documents", response_model=Document, status_code=status.HTTP_201_CREATED)
    def create_document(request: CreateDocumentRequest):
        document = service.create_document(request, source="web")
        if diagnostics is not None:
            diagnostics.emit(
                "document.created",
                document_id=document.id,
                revision=document.revision,
                name=document.name,
                width=document.canvas.width,
                height=document.canvas.height,
                source="web",
            )
        return document

    @router.get("/documents/{document_id}", response_model=Document)
    def get_document(document_id: str):
        return _call(service.get_document, document_id)

    @router.get("/documents/{document_id}/status")
    def document_status(document_id: str):
        document = _call(service.get_document, document_id)
        return {
            "id": document.id,
            "revision": document.revision,
            "updated_at": document.updated_at,
        }

    @router.get("/documents/{document_id}/history")
    def document_history(document_id: str, limit: int = 100):
        _call(service.get_document, document_id)
        return service.store.list_history_detailed(document_id, limit)

    @router.get("/diagnostics/export")
    def export_diagnostics(document_id: str | None = None, limit: int = 500):
        safe_limit = max(1, min(limit, 5000))
        payload: dict[str, Any] = {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "service": {"name": "P&ID-Agent", "version": version},
            "database": {
                "path": str(service.store.database_path.expanduser().resolve()),
                "instance_id": service.store.database_instance_id,
            },
            "documents": [
                {
                    "id": item.id,
                    "revision": item.revision,
                    "updated_at": item.updated_at,
                }
                for item in service.list_documents()
            ],
            "diagnostics": {
                "log": diagnostics.info() if diagnostics is not None else None,
                "events": diagnostics.recent(safe_limit) if diagnostics is not None else [],
            },
            "privacy": {
                "api_keys_recorded": False,
                "authorization_headers_recorded": False,
                "full_prompts_recorded": False,
                "full_context_recorded": False,
                "project_content_recorded": False,
                "upload_bodies_recorded": False,
            },
        }
        if document_id:
            document = _call(service.get_document, document_id)
            payload["document"] = {
                "id": document.id,
                "revision": document.revision,
                "element_count": len(document.elements),
                "layer_count": len(document.layers),
                "system_count": len(document.systems),
            }
        body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        suffix = f"-{document_id}" if document_id else ""
        return Response(
            body,
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="pid-agent-diagnostics{suffix}.json"'
                )
            },
        )

    @router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_document(
        document_id: str,
        expected_revision: Annotated[int, Query(ge=0)],
    ):
        before = _call(service.get_document, document_id)
        _call(service.delete_document, document_id, expected_revision)
        if diagnostics is not None:
            diagnostics.emit(
                "document.deleted",
                document_id=document_id,
                revision=before.revision,
                element_count=len(before.elements),
                source="web",
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/documents/{document_id}/transactions", response_model=TransactionResult)
    def apply_transaction(document_id: str, request: TransactionRequest):
        return _call(
            _apply_transaction_with_details,
            service,
            document_id,
            request,
            source="web",
            diagnostics=diagnostics,
        )

    @router.post("/documents/{document_id}/transactions/validate")
    def validate_transaction(document_id: str, request: TransactionRequest):
        return _call(_validate_transaction, service, document_id, request)

    @router.post("/documents/{document_id}/undo", response_model=Document)
    def undo(
        document_id: str,
        expected_revision: Annotated[int | None, Query(ge=0)] = None,
    ):
        before = _call(service.get_document, document_id)
        updated = _call(
            service.undo,
            document_id,
            expected_revision=expected_revision,
            source="web",
        )
        if updated.revision != before.revision:
            _record_revision_details(
                service,
                before,
                updated,
                None,
                action="undo",
                source="web",
                diagnostics=diagnostics,
            )
        return updated

    @router.post("/documents/{document_id}/redo", response_model=Document)
    def redo(
        document_id: str,
        expected_revision: Annotated[int | None, Query(ge=0)] = None,
    ):
        before = _call(service.get_document, document_id)
        updated = _call(
            service.redo,
            document_id,
            expected_revision=expected_revision,
            source="web",
        )
        if updated.revision != before.revision:
            _record_revision_details(
                service,
                before,
                updated,
                None,
                action="redo",
                source="web",
                diagnostics=diagnostics,
            )
        return updated

    @router.get("/documents/{document_id}/scene-summary")
    def scene_summary(document_id: str):
        return _call(service.scene_summary, document_id)

    @router.get("/documents/{document_id}/export.svg")
    def export_svg(document_id: str):
        document = _call(service.get_document, document_id)
        return Response(
            render_svg(document, service.symbols),
            media_type="image/svg+xml",
            headers={"Content-Disposition": f'attachment; filename="{document.id}.svg"'},
        )

    @router.get("/documents/{document_id}/export.json")
    def export_json(document_id: str):
        document = _call(service.get_document, document_id)
        return Response(
            document.model_dump_json(indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{document.id}.json"'},
        )

    @router.get("/documents/{document_id}/export-v1.json")
    def export_versioned_json(document_id: str):
        envelope = _call(service.export_document_envelope, document_id)
        return Response(
            envelope.model_dump_json(indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{envelope.document.id}.pid.json"'
            },
        )

    @router.post(
        "/imports/document",
        response_model=ImportResult,
        status_code=status.HTTP_201_CREATED,
    )
    def import_document(
        payload: dict[str, Any],
        conflict_policy: ImportConflictPolicy = "regenerate",
    ):
        try:
            result = service.import_document_payload(payload, conflict_policy=conflict_policy)
        except ProjectIOError as exc:
            raise HTTPException(
                status_code=409 if exc.code == "document_id_conflict" else 422,
                detail={"error": exc.code, "message": str(exc), "retryable": False},
            ) from exc
        if diagnostics is not None:
            diagnostics.emit(
                "document.import.completed",
                document_ids=[item.id for item in result.documents],
                document_id_map=result.document_id_map,
                source="web",
            )
        return result

    @router.get("/project/settings", response_model=ProjectSettings)
    def get_project_settings():
        return service.get_project_settings()

    @router.put("/project/settings", response_model=ProjectSettings)
    def update_project_settings(settings: ProjectSettings):
        return service.update_project_settings(settings)

    @router.get("/project/export.json")
    def export_project_package():
        try:
            package = service.export_project_package()
        except ProjectIOError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": exc.code, "message": str(exc), "retryable": False},
            ) from exc
        return Response(
            package.model_dump_json(indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="pid-agent-project.pid.json"'},
        )

    @router.post(
        "/imports/project-package",
        response_model=ImportResult,
        status_code=status.HTTP_201_CREATED,
    )
    def import_project_package(
        payload: dict[str, Any],
        conflict_policy: ImportConflictPolicy = "regenerate",
    ):
        try:
            result = service.import_project_payload(payload, conflict_policy=conflict_policy)
        except ProjectIOError as exc:
            raise HTTPException(
                status_code=409 if exc.code == "document_id_conflict" else 422,
                detail={"error": exc.code, "message": str(exc), "retryable": False},
            ) from exc
        if diagnostics is not None:
            diagnostics.emit(
                "project.import.completed",
                document_ids=[item.id for item in result.documents],
                document_id_map=result.document_id_map,
                project_name=result.project.name if result.project else None,
                source="web",
            )
        return result

    @router.get("/documents/{document_id}/export.png")
    def export_png(document_id: str, scale: float = 1.0):
        if not 0.1 <= scale <= 8:
            raise HTTPException(status_code=422, detail="scale must be between 0.1 and 8")
        document = _call(service.get_document, document_id)
        try:
            payload = render_png(
                document,
                service.symbols,
                output_width=int(document.canvas.width * scale),
                output_height=int(document.canvas.height * scale),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "png_render_failed",
                    "message": "PNG export failed",
                    "retryable": True,
                },
            ) from exc
        return Response(
            payload,
            media_type="image/png",
            headers={"Content-Disposition": f'attachment; filename="{document.id}.png"'},
        )

    @router.get("/symbols")
    def list_symbols():
        return service.symbols.list()

    @router.post("/symbols/reload")
    def reload_symbols():
        service.symbols.reload()
        return {"count": len(service.symbols.list())}

    @router.post("/agent/provider/models")
    def list_provider_models(request: ProviderConfig):
        started = perf_counter()
        if diagnostics is not None:
            diagnostics.emit(
                "llm.provider_models.started",
                base_url=request.base_url,
                timeout_seconds=request.timeout_seconds,
                api_key_present=bool(request.api_key),
            )
        try:
            result = discover_provider_models(
                request,
                provider_policy=planner.provider_policy,
                max_response_bytes=planner.max_response_bytes,
                max_timeout_seconds=planner.max_timeout_seconds,
            )
        except PlannerError as exc:
            if diagnostics is not None:
                diagnostics.emit(
                    "llm.provider_models.failed",
                    base_url=request.base_url,
                    duration_ms=round((perf_counter() - started) * 1000, 2),
                    error_code=exc.code,
                    provider_status=exc.provider_status,
                    error=exc,
                )
            raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
        if diagnostics is not None:
            diagnostics.emit(
                "llm.provider_models.completed",
                base_url=result.get("base_url"),
                duration_ms=round((perf_counter() - started) * 1000, 2),
                model_count=result.get("count"),
            )
        return result

    @router.post("/agent/provider/test")
    def test_provider(request: ProviderConfig):
        started = perf_counter()
        if diagnostics is not None:
            diagnostics.emit(
                "llm.provider_test.started",
                base_url=request.base_url,
                model=request.model,
                timeout_seconds=request.timeout_seconds,
                api_key_present=bool(request.api_key),
            )
        try:
            result = planner.test_provider(request)
        except PlannerError as exc:
            if diagnostics is not None:
                diagnostics.emit(
                    "llm.provider_test.failed",
                    base_url=request.base_url,
                    model=request.model,
                    duration_ms=round((perf_counter() - started) * 1000, 2),
                    error_code=exc.code,
                    provider_status=exc.provider_status,
                    error=exc,
                )
            raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
        if diagnostics is not None:
            diagnostics.emit(
                "llm.provider_test.completed",
                base_url=result.get("base_url"),
                model=result.get("model"),
                duration_ms=round((perf_counter() - started) * 1000, 2),
                method=result.get("method"),
                model_available=result.get("model_available"),
            )
        return result

    @router.get("/agent/tool-schema")
    def agent_tool_schema():
        return {
            "name": "apply_pid_agent_transaction",
            "description": (
                "Apply an atomic, validated set of drawing operations to one P&ID-Agent document. "
                "Use scene-summary first when modifying an existing drawing."
            ),
            "input_schema": TransactionRequest.model_json_schema(),
        }

    @router.post(
        "/documents/{document_id}/agent/generate",
        response_model=AgentGenerateResult,
    )
    def agent_generate(document_id: str, request: AgentGenerateRequest):
        started = perf_counter()
        before = _call(service.get_document, document_id)
        if diagnostics is not None:
            diagnostics.emit(
                "llm.plan.started",
                document_id=document_id,
                revision=before.revision,
                dry_run=request.dry_run,
                prompt_chars=len(request.prompt),
                context_chars=len(request.context),
                base_url=request.provider.base_url if request.provider else None,
                model=request.provider.model if request.provider else None,
                timeout_seconds=(
                    request.provider.timeout_seconds if request.provider else None
                ),
                api_key_present=bool(request.provider and request.provider.api_key),
            )
        try:
            plan = planner.plan(document_id, request)
            if request.dry_run:
                validation = _validate_transaction(service, document_id, plan.transaction)
                if diagnostics is not None:
                    diagnostics.emit(
                        "llm.plan.completed",
                        document_id=document_id,
                        revision=before.revision,
                        dry_run=True,
                        duration_ms=round((perf_counter() - started) * 1000, 2),
                        transaction_label=plan.transaction.label,
                        operation_count=len(plan.transaction.operations),
                        affected_element_ids=validation["affected_element_ids"],
                    )
                return AgentGenerateResult(plan=plan)
            result = _apply_transaction_with_details(
                service,
                document_id,
                plan.transaction,
                source="llm",
                diagnostics=diagnostics,
            )
            if diagnostics is not None:
                diagnostics.emit(
                    "llm.plan.completed",
                    document_id=document_id,
                    revision=result.document.revision,
                    dry_run=False,
                    duration_ms=round((perf_counter() - started) * 1000, 2),
                    transaction_label=plan.transaction.label,
                    operation_count=len(plan.transaction.operations),
                )
            return AgentGenerateResult(plan=plan, document=result.document)
        except PlannerError as exc:
            if diagnostics is not None:
                diagnostics.emit(
                    "llm.plan.failed",
                    document_id=document_id,
                    revision=before.revision,
                    duration_ms=round((perf_counter() - started) * 1000, 2),
                    error_code=exc.code,
                    provider_status=exc.provider_status,
                    error=exc,
                )
            raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
        except (DocumentNotFoundError, InvalidOperationError, RevisionConflictError) as exc:
            if diagnostics is not None:
                diagnostics.emit(
                    "llm.plan.rejected",
                    document_id=document_id,
                    revision=before.revision,
                    duration_ms=round((perf_counter() - started) * 1000, 2),
                    error=exc,
                )
            return _raise_service_error(exc)

    @router.post(
        "/documents/{document_id}/agent/apply",
        response_model=TransactionResult,
    )
    def apply_agent_plan(document_id: str, request: TransactionRequest):
        return _call(
            _apply_transaction_with_details,
            service,
            document_id,
            request,
            source="llm",
            diagnostics=diagnostics,
        )

    return router


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (DocumentNotFoundError, InvalidOperationError, RevisionConflictError) as exc:
        return _raise_service_error(exc)


def _raise_service_error(exc: Exception):
    if isinstance(exc, DocumentNotFoundError):
        raise HTTPException(status_code=404, detail=f"document not found: {exc.args[0]}") from exc
    if isinstance(exc, RevisionConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc
