"""M6-2B-D4: the governed apply HTTP surface.

Frozen scope (D4 CODE GO): compile+persist a confirmed finding into a durable patch, read
patches and the apply ledger, and drive the sealed M10 flow for apply/revert. The route
names carry no forbidden token; every write route is declared in surface_contract.

Approval discipline (M13's): in local mode the operator's approval is created and resolved
by the operator's own deployment; in shared mode the apply/revert consumes ONLY a
human-resolved approval created by this router's approval-request endpoints — a human
decision can never be self-served by an endpoint (``approval_not_self_served``).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .audit import AuditRecorder
from .audit_models import AuditRecordDraft
from .config import Settings
from .harness import (
    AgentSessionNotFoundError,
    HarnessError,
    ToolApprovalNotFoundError,
    ToolPermissionDeniedError,
)
from .harness_models import ToolApprovalCreateRequest, ToolApprovalResolveRequest
from .m6_apply_runtime import (
    TOOL_APPLY_M6_FINDING,
    TOOL_REVERT_M6_APPLY,
    M6ApplyError,
    M6ApplyExecutor,
    M6AuditAdapter,
    M6DomainAdapter,
    M6ToolRegistry,
)
from .m6_candidate_core import (
    CompilationRefused,
    M6CandidateService,
    NotConfirmedError,
    patch_operations_payload,
)
from .runtime.harness import AgentHarnessRuntime
from .service import DocumentService
from .store import M6FilingError, SQLiteDocumentStore
from .symbols import SymbolRegistry


class _CompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1)


class _ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = ""
    approval_id: str = ""


def create_m6_apply_router(
    service: DocumentService,
    store: SQLiteDocumentStore,
    settings: Settings,
) -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["P&ID-Agent M6 apply"])
    symbols = SymbolRegistry()
    recorder = AuditRecorder(store=store, symbols=symbols)
    executor = M6ApplyExecutor(store, service, symbols)
    runtime = AgentHarnessRuntime(
        store=store,
        registry=M6ToolRegistry(),
        adapter=M6DomainAdapter(executor, store),
        audit=M6AuditAdapter(recorder),
    )
    candidates = M6CandidateService(store)

    def _map_error(exc: Exception) -> HTTPException:
        code = getattr(exc, "code", "m6_apply_error")
        status = 409
        if code in {"patch_not_found", "apply_record_not_found", "target_not_found"}:
            status = 404
        return HTTPException(status_code=status, detail={"code": code, "message": str(exc)})

    def _map_harness_error(exc: HarnessError) -> HTTPException:
        status = 409
        if isinstance(exc, (AgentSessionNotFoundError, ToolApprovalNotFoundError)):
            status = 404
        elif isinstance(exc, ToolPermissionDeniedError):
            status = 403
        return HTTPException(
            status_code=status,
            detail={"code": getattr(exc, "code", "harness_error"), "message": str(exc)},
        )

    def _patch_payload(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "patch_id": str(row["patch_id"]),
            "finding_ids": row["finding_ids_json"],
            "intent": str(row["intent"]),
            "policy_disposition": str(row["policy_disposition"]),
            "operations": row["operations_json"],
            "compiler": str(row["compiler_name"]),
            "compiler_version": str(row["compiler_version"]),
            "compiler_rules": str(row["compiler_rules"]),
            "baseline_revision": int(row["baseline_revision"]),
            "canonical_digest": str(row["canonical_digest"]),
            "source_document_id": str(row["source_document_id"]),
            "target_document_id": str(row["target_document_id"]),
            "created_at": str(row["created_at"]),
        }

    def _apply_payload(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "apply_id": str(row["apply_id"]),
            "patch_id": str(row["patch_id"]),
            "candidate_id": str(row["candidate_id"]),
            "source_document_id": str(row["source_document_id"]),
            "target_document_id": str(row["target_document_id"]),
            "base_revision": int(row["base_revision"]),
            "result_revision": row["result_revision"],
            "status": str(row["status"]),
            "tool_intent_hash": str(row["tool_intent_hash"]),
            "approval_id": str(row["approval_id"]),
            "audit_record_id": str(row["audit_record_id"]),
            "applied_at": str(row["applied_at"]),
            "reverted_at": str(row["reverted_at"]),
            "revert_apply_id": str(row["revert_apply_id"]),
        }

    def _require_target(document_id: str) -> None:
        service.get_document(document_id)  # 404 for unknown documents

    # ------------------------------------------------------------- patches

    @router.post("/documents/{document_id}/m6/patches")
    def compile_patch(document_id: str, payload: _CompileRequest) -> dict[str, Any]:
        """Compile a confirmed finding into a durable patch (idempotent by identity)."""

        _require_target(document_id)
        try:
            document = service.get_document(document_id)
            patch = candidates.compile_finding(
                payload.finding_id, document=document, registry=symbols
            )
            finding = store.get_confirmed_finding(payload.finding_id)
            candidate = (
                store.get_semantic_candidate(finding.candidate_id) if finding else None
            )
            if finding is None or candidate is None:
                raise M6ApplyError(
                    f"finding {payload.finding_id!r} has no candidate chain",
                    code="apply_identity_chain_mismatch",
                )
            if candidate.target_document_id != document_id:
                raise M6ApplyError(
                    f"the finding targets {candidate.target_document_id!r}, not {document_id!r}",
                    code="apply_identity_chain_mismatch",
                )
            store.insert_structured_patch(
                patch_id=patch.patch_id,
                finding_ids=patch.finding_ids,
                intent=patch.intent,
                policy_disposition=patch.policy_disposition,
                operations_json=_canonical_ops(patch),
                compiler_name=patch.compiler,
                compiler_version=patch.compiler_version,
                compiler_rules=patch.compiler_rules,
                baseline_revision=patch.baseline_revision,
                canonical_digest=patch.canonical_digest,
                source_document_id=candidate.artifact.source_document_id,
                target_document_id=document_id,
                # The store writes the audit only when the row is new; a recompile of the
                # same finding is not a new fact and audits nothing.
                audit=AuditRecordDraft(
                    event_type="m6.patch.compiled",
                    actor=settings.operator_identity or "web-user",
                    surface="rest",
                    tool_name="compile_m6_patch",
                    status="applied",
                    document_id=document_id,
                    label=f"Compile M6 patch {patch.patch_id}",
                    evidence={
                        "patch_id": patch.patch_id,
                        "finding_ids": patch.finding_ids,
                        "candidate_id": candidate.candidate_id,
                        "intent": patch.intent,
                        "canonical_digest": patch.canonical_digest,
                    },
                ),
            )
        except (M6ApplyError, M6FilingError) as exc:
            raise _map_error(exc) from exc
        except NotConfirmedError as exc:
            raise _map_error(exc) from exc
        except CompilationRefused as exc:
            raise _map_error(exc) from exc
        row = store.get_structured_patch(patch.patch_id)
        return _patch_payload(row)

    @router.get("/documents/{document_id}/m6/patches")
    def list_patches(document_id: str) -> dict[str, Any]:
        _require_target(document_id)
        rows = store.list_structured_patches(target_document_id=document_id)
        return {"document_id": document_id, "patches": [_patch_payload(row) for row in rows]}

    @router.get("/documents/{document_id}/m6/patches/{patch_id}")
    def patch_detail(document_id: str, patch_id: str) -> dict[str, Any]:
        _require_target(document_id)
        row = store.get_structured_patch(patch_id)
        if row is None or str(row["target_document_id"]) != document_id:
            raise HTTPException(
                status_code=404,
                detail={"code": "patch_not_found", "message": f"patch {patch_id!r} not found"},
            )
        return _patch_payload(row)

    @router.post("/documents/{document_id}/m6/patches/{patch_id}/approval-requests")
    def request_patch_approval(document_id: str, patch_id: str) -> dict[str, Any]:
        """Create the M10 approval bound to the sealed M6 runtime (D86-1 precedent)."""

        _require_target(document_id)
        row = store.get_structured_patch(patch_id)
        if row is None or str(row["target_document_id"]) != document_id:
            raise HTTPException(
                status_code=404,
                detail={"code": "patch_not_found", "message": f"patch {patch_id!r} not found"},
            )
        actor = settings.operator_identity or "web-user"
        try:
            session = runtime.ensure_session(document_id, actor=actor)
            approval = runtime.request_approval(
                session.id,
                ToolApprovalCreateRequest(
                    tool_name=TOOL_APPLY_M6_FINDING,
                    document_id=document_id,
                    intent={"patch_id": patch_id},
                    requested_by=actor,
                ),
            )
        except (HarnessError, M6ApplyError) as exc:
            raise _map_harness_error(exc) from exc
        return {
            "patch_id": patch_id,
            "session_id": session.id,
            "approval_id": approval.id,
            "status": approval.status,
        }

    @router.post("/documents/{document_id}/m6/patches/{patch_id}/apply")
    def apply_patch(
        document_id: str, patch_id: str, body: _ApplyRequest | None = None
    ) -> dict[str, Any]:
        """Apply via the sealed M10 flow: local auto-approves the operator; shared
        consumes only a human-resolved approval from the approval-requests route."""

        _require_target(document_id)
        row = store.get_structured_patch(patch_id)
        if row is None or str(row["target_document_id"]) != document_id:
            raise HTTPException(
                status_code=404,
                detail={"code": "patch_not_found", "message": f"patch {patch_id!r} not found"},
            )
        payload = {"patch_id": patch_id}
        actor = settings.operator_identity or "local-operator"
        try:
            if settings.deployment_mode == "local":
                session = runtime.ensure_session(document_id, actor=actor)
                approval = runtime.request_approval(
                    session.id,
                    ToolApprovalCreateRequest(
                        tool_name=TOOL_APPLY_M6_FINDING,
                        document_id=document_id,
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
                    tool_name=TOOL_APPLY_M6_FINDING,
                    document_id=document_id,
                    intent=payload,
                    approval_id=approval.id,
                )
            else:
                approval_id = (body or _ApplyRequest()).approval_id
                session_id = (body or _ApplyRequest()).session_id
                if not approval_id or not session_id:
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "approval_not_self_served",
                            "message": (
                                "shared deployments apply only with a human-resolved approval "
                                "from POST .../m6/patches/{id}/approval-requests"
                            ),
                        },
                    )
                authorized = runtime.authorize(
                    session_id=session_id,
                    tool_name=TOOL_APPLY_M6_FINDING,
                    document_id=document_id,
                    intent=payload,
                    approval_id=approval_id,
                )
            outcome = runtime.apply_authorized(authorized, document_id, payload)
        except (M6ApplyError, M6FilingError) as exc:
            raise _map_error(exc) from exc
        except HarnessError as exc:
            raise _map_harness_error(exc) from exc
        return {
            "patch_id": patch_id,
            "apply_id": outcome.payload["apply_id"],
            "result_revision": outcome.result_revision,
        }

    # ------------------------------------------------------------- apply ledger

    @router.get("/documents/{document_id}/m6/apply-records")
    def list_apply_records(document_id: str) -> dict[str, Any]:
        _require_target(document_id)
        rows = store.list_apply_records(target_document_id=document_id)
        return {
            "document_id": document_id,
            "apply_records": [_apply_payload(row) for row in rows],
        }

    @router.get("/documents/{document_id}/m6/apply-records/{apply_id}")
    def apply_record_detail(document_id: str, apply_id: str) -> dict[str, Any]:
        _require_target(document_id)
        row = store.get_apply_record(apply_id)
        if row is None or str(row["target_document_id"]) != document_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "apply_record_not_found",
                    "message": f"apply record {apply_id!r} not found",
                },
            )
        return _apply_payload(row)

    @router.post("/documents/{document_id}/m6/apply-records/{apply_id}/revert-approval-requests")
    def request_revert_approval(document_id: str, apply_id: str) -> dict[str, Any]:
        _require_target(document_id)
        row = store.get_apply_record(apply_id)
        if row is None or str(row["target_document_id"]) != document_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "apply_record_not_found",
                    "message": f"apply record {apply_id!r} not found",
                },
            )
        actor = settings.operator_identity or "web-user"
        try:
            session = runtime.ensure_session(document_id, actor=actor)
            approval = runtime.request_approval(
                session.id,
                ToolApprovalCreateRequest(
                    tool_name=TOOL_REVERT_M6_APPLY,
                    document_id=document_id,
                    intent={"apply_id": apply_id},
                    requested_by=actor,
                ),
            )
        except (HarnessError, M6ApplyError) as exc:
            raise _map_harness_error(exc) from exc
        return {
            "apply_id": apply_id,
            "session_id": session.id,
            "approval_id": approval.id,
            "status": approval.status,
        }

    @router.post("/documents/{document_id}/m6/apply-records/{apply_id}/revert")
    def revert_apply(
        document_id: str, apply_id: str, body: _ApplyRequest | None = None
    ) -> dict[str, Any]:
        """The compensation revert through the same sealed flow (UNDO = a governed
        compensating transaction, never a state rollback)."""

        _require_target(document_id)
        row = store.get_apply_record(apply_id)
        if row is None or str(row["target_document_id"]) != document_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "apply_record_not_found",
                    "message": f"apply record {apply_id!r} not found",
                },
            )
        payload = {"apply_id": apply_id}
        actor = settings.operator_identity or "local-operator"
        try:
            if settings.deployment_mode == "local":
                session = runtime.ensure_session(document_id, actor=actor)
                approval = runtime.request_approval(
                    session.id,
                    ToolApprovalCreateRequest(
                        tool_name=TOOL_REVERT_M6_APPLY,
                        document_id=document_id,
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
                    tool_name=TOOL_REVERT_M6_APPLY,
                    document_id=document_id,
                    intent=payload,
                    approval_id=approval.id,
                )
            else:
                approval_id_body = (body or _ApplyRequest()).approval_id
                session_id = (body or _ApplyRequest()).session_id
                if not approval_id_body or not session_id:
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "approval_not_self_served",
                            "message": (
                                "shared deployments revert only with a human-resolved approval "
                                "from POST .../m6/apply-records/{id}/revert-approval-requests"
                            ),
                        },
                    )
                authorized = runtime.authorize(
                    session_id=session_id,
                    tool_name=TOOL_REVERT_M6_APPLY,
                    document_id=document_id,
                    intent=payload,
                    approval_id=approval_id_body,
                )
            outcome = runtime.apply_authorized(authorized, document_id, payload)
        except (M6ApplyError, M6FilingError) as exc:
            raise _map_error(exc) from exc
        except HarnessError as exc:
            raise _map_harness_error(exc) from exc
        return {
            "apply_id": apply_id,
            "revert_apply_id": outcome.payload["revert_apply_id"],
            "result_revision": outcome.result_revision,
        }

    return router


def _canonical_ops(patch) -> str:
    return json.dumps(
        patch_operations_payload(patch), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


__all__ = ["create_m6_apply_router"]
