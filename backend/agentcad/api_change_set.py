"""M13-D5: change-set evidence / read surface + governed write path.

Frozen scope (M13-D5 CODE GO): GET-only, zero-audit exposure of the durable
change-set facts (status, intent, impacted, preview, result pins, evidence);
the write path exists only to route into the SEALED D3/D4 governed flow —
stage runs the D3 analyzer/previewer and persists via the D2 primitives;
apply drives the M10 runtime (session -> approval -> authorization -> D4
executor) with NO copied domain logic. Automatic operator approval is
available ONLY in local deployment mode; shared mode refuses it (a human
approval can never be self-served by an endpoint).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException

from .audit import AuditRecorder
from .audit_models import AuditRecordDraft
from .config import Settings
from .harness import (
    AgentSessionNotFoundError,
    HarnessError,
    ToolApprovalNotFoundError,
    ToolPermissionDeniedError,
)
from .harness_models import (
    ToolApprovalCreateRequest,
    ToolApprovalResolveRequest,
)
from .project_change_set import (
    ChangeSetError,
    ChangeSetImpactAnalyzer,
    ChangeSetIntent,
    ChangeSetPreviewer,
    canonical_change_set_intent,
    change_set_intent_hash,
    new_change_set_id,
)
from .project_change_set_runtime import (
    TOOL_APPLY_CHANGE_SET,
    ChangeSetExecutor,
    ProjectChangeAuditAdapter,
    ProjectChangeDomainAdapter,
    ProjectChangeToolRegistry,
)
from .runtime.harness import AgentHarnessRuntime
from .service import DocumentService
from .store import SQLiteDocumentStore
from .symbols import SymbolRegistry


def _parse_json_column(raw: object, fallback: object) -> object:
    if raw is None:
        return fallback
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return fallback


def create_change_set_router(
    store: SQLiteDocumentStore,
    pid_service: DocumentService,
    settings: Settings,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v2/projects/{project_id}/change-sets",
        tags=["P&ID-Agent change sets"],
    )
    symbols = SymbolRegistry()
    from .cable_service import CableService

    cable_service = CableService(store)
    recorder = AuditRecorder(store=store, symbols=symbols)
    analyzer = ChangeSetImpactAnalyzer(store, pid_service, cable_service)
    previewer = ChangeSetPreviewer(store, pid_service, cable_service)
    executor = ChangeSetExecutor(store, pid_service, cable_service, analyzer, previewer)
    runtime = AgentHarnessRuntime(
        store=store,
        registry=ProjectChangeToolRegistry(),
        adapter=ProjectChangeDomainAdapter(executor, store),
        audit=ProjectChangeAuditAdapter(recorder),
    )

    def _row_payload(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "change_set_id": str(row["change_set_id"]),
            "project_id": str(row["project_id"]),
            "status": str(row["status"]),
            "base_pins": _parse_json_column(row["base_pins"], {}),
            "intent": _parse_json_column(row["intent"], {}),
            "intent_hash": str(row["intent_hash"]),
            "impacted": _parse_json_column(row["impacted"], {}),
            "preview": _parse_json_column(row["preview"], {}),
            "result_pins": _parse_json_column(row["result_pins"], {}),
            "evidence": _parse_json_column(row["evidence"], {}),
            "session_id": row["session_id"],
            "approval_id": row["approval_id"],
            "tool_call_id": row["tool_call_id"],
            "created_at": str(row["created_at"]),
            "created_by": str(row["created_by"]),
            "updated_at": str(row["updated_at"]),
        }

    def _require_project(project_id: str) -> None:
        if store.get_project(project_id) is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "project_not_found",
                    "message": f"project {project_id!r} not found",
                },
            )

    def _map_error(exc: ChangeSetError) -> HTTPException:
        mapping = {
            "change_set_base_not_current": 409,
            "change_set_conflict": 409,
            "change_set_not_authorized": 409,
            "change_set_invalidates_link": 409,
            "change_set_intent_mismatch": 409,
            "change_set_revision_leak": 422,
            "impact_unknown_object": 422,
            "impact_unknown_kind": 422,
            "change_set_not_found": 404,
        }
        return HTTPException(
            status_code=mapping.get(exc.code, 500),
            detail={"code": exc.code, "message": str(exc)},
        )

    # ------------------------------------------------------------ read surface

    @router.get("")
    def list_change_sets(project_id: str) -> dict[str, Any]:
        _require_project(project_id)
        return {
            "project_id": project_id,
            "change_sets": [_row_payload(row) for row in store.list_change_sets(project_id)],
        }

    @router.get("/{change_set_id}")
    def change_set_detail(project_id: str, change_set_id: str) -> dict[str, Any]:
        _require_project(project_id)
        row = store.get_change_set(change_set_id)
        if row is None or str(row["project_id"]) != project_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "change_set_not_found",
                    "message": f"change set {change_set_id!r} not found",
                },
            )
        return _row_payload(row)

    # ------------------------------------------------------- governed writes

    @router.post("")
    def stage_change_set(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Stage a change set: D3 impact analysis + shadow preview, persisted
        via the D2 primitives. Fail-closed before any row exists."""
        _require_project(project_id)
        try:
            declared = dict(payload)
            declared["project_id"] = project_id
            intent = ChangeSetIntent.model_validate(declared)
            impact = analyzer.analyze(intent)
            preview = previewer.preview(intent, impact)
        except ChangeSetError as exc:
            raise _map_error(exc) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail={"code": "invalid_intent", "message": str(exc)}
            ) from exc
        change_set_id = new_change_set_id()
        store.insert_change_set(
            change_set_id=change_set_id,
            project_id=project_id,
            base_pins=json.dumps(intent.base_member_pins, sort_keys=True),
            intent=canonical_change_set_intent(intent),
            intent_hash=change_set_intent_hash(intent),
            impacted=json.dumps(impact.canonical(), sort_keys=True),
            preview=json.dumps(preview.canonical(), sort_keys=True),
            created_by="web-user",
        )
        # governance-plane audit fact: staging persists a change-set row only
        # and never moves an engineering revision or digest.
        recorder.store.record_audit_event(
            AuditRecordDraft(
                event_type="project_change_set.staged",
                actor="web-user",
                surface="rest",
                tool_name="stage_project_change_set",
                status="applied",
                project_id=project_id,
                intent_hash=change_set_intent_hash(intent),
                evidence={
                    "change_set_id": change_set_id,
                    "affected_documents": sorted(impact.canonical().get("affected_documents", [])),
                    "zero_engineering_writes": True,
                },
            )
        )
        return {
            "change_set_id": change_set_id,
            "status": "staged",
            "impacted": impact.canonical(),
            "preview": preview.canonical(),
        }

    def _get_staged_row(project_id: str, change_set_id: str) -> dict[str, Any]:
        row = store.get_change_set(change_set_id)
        if row is None or str(row["project_id"]) != project_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "change_set_not_found",
                    "message": f"change set {change_set_id!r} not found",
                },
            )
        if str(row["status"]) != "staged":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "change_set_not_staged",
                    "message": f"change set {change_set_id!r} is {row['status']!r}",
                },
            )
        return row

    def _apply_payload(row: dict[str, Any], change_set_id: str):
        intent = ChangeSetIntent.model_validate(_parse_json_column(row["intent"], {}))
        payload = {
            "change_set_id": change_set_id,
            "intent": json.loads(canonical_change_set_intent(intent)),
        }
        binding_doc = sorted(m.document_id for m in intent.mutations)[0]
        return intent, payload, binding_doc

    def _map_harness_error(exc: Exception) -> HTTPException:
        status = 409
        if isinstance(exc, (AgentSessionNotFoundError, ToolApprovalNotFoundError)):
            status = 404
        elif isinstance(exc, ToolPermissionDeniedError):
            status = 403
        return HTTPException(
            status_code=status,
            detail={"code": getattr(exc, "code", "harness_error"), "message": str(exc)},
        )

    @router.post("/{change_set_id}/approval-requests")
    def request_change_set_approval(project_id: str, change_set_id: str) -> dict[str, Any]:
        """D86-1: shared-mode humans need a project-change approval bound to
        the SEALED runtime. The approval carries the exact stored intent hash,
        so a human resolve of THIS approval is the only shared-mode gate into
        apply. Anonymous callers still hit the auth boundary first."""
        _require_project(project_id)
        row = _get_staged_row(project_id, change_set_id)
        _intent, payload, binding_doc = _apply_payload(row, change_set_id)
        actor = settings.operator_identity or "web-user"
        try:
            session = runtime.ensure_session(binding_doc, actor=actor)
            approval = runtime.request_approval(
                session.id,
                ToolApprovalCreateRequest(
                    tool_name=TOOL_APPLY_CHANGE_SET,
                    document_id=binding_doc,
                    intent=payload,
                    requested_by=actor,
                ),
            )
        except HarnessError as exc:
            raise _map_harness_error(exc) from exc
        return {
            "change_set_id": change_set_id,
            "session_id": session.id,
            "approval_id": approval.id,
            "status": approval.status,
        }

    @router.post("/{change_set_id}/apply")
    def apply_change_set(
        project_id: str, change_set_id: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Apply via the sealed D4 governed flow. Local deployment may
        auto-resolve the operator approval. Shared deployment consumes ONLY a
        human-resolved approval created by this router's approval-request
        endpoint (a human decision can never be self-served by an endpoint);
        the M10 authorize step re-validates the exact session/tool/intent
        binding before anything engineering can move."""
        _require_project(project_id)
        approval_id = (body or {}).get("approval_id")
        session_id = (body or {}).get("session_id")
        if settings.deployment_mode != "local" and (not approval_id or not session_id):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "approval_not_self_served",
                    "message": (
                        "shared deployments apply a change set only with a "
                        "human-resolved approval from POST "
                        ".../change-sets/{id}/approval-requests"
                    ),
                },
            )
        row = _get_staged_row(project_id, change_set_id)
        _intent, payload, binding_doc = _apply_payload(row, change_set_id)
        actor = settings.operator_identity or "local-operator"
        try:
            if settings.deployment_mode == "local":
                # the local operator's explicit decision, recorded in governance
                store.update_change_set_status(
                    change_set_id=change_set_id,
                    expected_status="staged",
                    new_status="approved",
                )
                session = runtime.ensure_session(binding_doc, actor=actor)
                approval = runtime.request_approval(
                    session.id,
                    ToolApprovalCreateRequest(
                        tool_name=TOOL_APPLY_CHANGE_SET,
                        document_id=binding_doc,
                        intent=payload,
                        requested_by=actor,
                    ),
                )
                runtime.resolve_approval(
                    approval.id,
                    ToolApprovalResolveRequest(approved=True, actor=actor),
                )
                authorized = runtime.authorize(
                    session_id=session.id,
                    tool_name=TOOL_APPLY_CHANGE_SET,
                    document_id=binding_doc,
                    intent=payload,
                    approval_id=approval.id,
                    base_revision=None,
                )
            else:
                # validate the human approval binding BEFORE the governance
                # transition; authorize is zero-engineering-write
                authorized = runtime.authorize(
                    session_id=str(session_id),
                    tool_name=TOOL_APPLY_CHANGE_SET,
                    document_id=binding_doc,
                    intent=payload,
                    approval_id=str(approval_id),
                    base_revision=None,
                )
                if not store.update_change_set_status(
                    change_set_id=change_set_id,
                    expected_status="staged",
                    new_status="approved",
                ):
                    raise ChangeSetError(
                        f"change set {change_set_id!r} left staged during approval validation",
                        code="change_set_conflict",
                    )
            outcome = runtime.apply_authorized(authorized, binding_doc, payload)
        except ChangeSetError as exc:
            raise _map_error(exc) from exc
        except HarnessError as exc:
            raise _map_harness_error(exc) from exc
        return {
            "change_set_id": change_set_id,
            "status": store.get_change_set(change_set_id)["status"],
            "result_pins": outcome.payload["result_pins"],
        }

    return router
