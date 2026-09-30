"""M11-D2: Cable DomainAdapter (seven ports, production) + CableAuditAdapter.

Never wraps PidDomainAdapter. The atomic governed write delegates to
store.commit_cable_write; failures route through the two-transaction
semantics frozen in the D2 design (success rollback -> failure closeout,
level-2 honesty).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from .audit import AuditRecorder
from .audit_models import AuditRecordDraft
from .cable_models import AddCableSegmentIntent
from .cable_service import CableService
from .runtime.ports import (
    AuditEvent,
    AuditRecordRef,
    ClosureRequest,
    DocumentContext,
    ExecutionOutcome,
    ToolDefinitionView,
)

TOOL_ADD_CABLE_SEGMENT = "add_cable_segment"


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CableToolRegistry:
    """Cable-specific tool registry (M11-D2 §7): never extends the P&ID
    catalogue; the runtime only sees the neutral ToolDefinitionView."""

    _VIEW = ToolDefinitionView(
        name=TOOL_ADD_CABLE_SEGMENT,
        description="Add one cable segment to a cable schematic (M11 production slice)",
        permission="ask",
        risk="engineering_change",
        audit_event="tool.cable.add_segment",
    )

    def require(self, name: str) -> ToolDefinitionView:
        if name != self._VIEW.name:
            raise KeyError(f"unknown cable tool: {name}")
        return self._VIEW


class CableAuditAdapter:
    """AuditPort for the Cable plane: execute-time rejections (approval missing
    / rejected / four-tuple mismatch / permission deny) recorded by the runtime
    before any domain write — same field-parity semantics as PidAuditAdapter."""

    def __init__(self, recorder: AuditRecorder) -> None:
        self._recorder = recorder

    def record(self, event: AuditEvent) -> AuditRecordRef:
        from .audit import request_audit_context

        context = request_audit_context(
            event.tool_name or event.event_type,
            actor=event.actor,
            surface=event.surface,
            label=event.label,
            session_id=event.session_id,
            approval_id=event.approval_id,
            tool_call_id=event.tool_call_id,
            provider=event.provider,
            model=event.model,
            intent_hash=event.intent_hash,
            metadata=event.metadata,
        )
        record = self._recorder.record_rejection(
            context,
            event_type=event.event_type,
            error_code=event.error_code,
            document_id=event.document_id,
            base_revision=event.base_revision,
            status=event.status,
            evidence=event.evidence,
        )
        return AuditRecordRef(
            record_id=record.record_id, ordinal=record.ordinal, record_hash=record.record_hash
        )


class CableDomainAdapter:
    def __init__(self, service: CableService) -> None:
        self.service = service

    # ------------------------------------------------------------------ reads

    def document_context(self, *, document_id: str) -> DocumentContext:
        return self.service.document_context(document_id=document_id)

    def canonicalize_intent(self, tool_name: str, intent: Any) -> Any:
        if tool_name != TOOL_ADD_CABLE_SEGMENT or not isinstance(intent, dict):
            return intent
        return AddCableSegmentIntent.model_validate(intent).model_dump(mode="json")

    def preview_diff_hash(self, *, document_id: str, intent: Any) -> str:
        try:
            canonical = self.canonicalize_intent(TOOL_ADD_CABLE_SEGMENT, intent)
            return hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()
        except Exception:  # pragma: no cover - preview must never block an approval
            return ""

    def approval_evidence(
        self,
        *,
        definition: ToolDefinitionView,
        canonical_intent: Any,
        diff_preview_hash: str,
    ) -> dict[str, Any]:
        intent = AddCableSegmentIntent.model_validate(canonical_intent)
        return {
            "tool": definition.name,
            "cable_document_id": "",
            "expected_revision": intent.expected_revision,
            "cable_segment_id": intent.segment.id,
            "diff_preview_hash": diff_preview_hash,
        }

    def rejection_evidence(
        self,
        *,
        definition: ToolDefinitionView,
        intent: Any,
        error_code: str,
    ) -> dict[str, Any]:
        return {
            "tool_permission": definition.permission,
            "cable_document_id": "",
            "authorized": False,
        }

    def closure_request(self, *, authorized: Any, intent: Any) -> ClosureRequest:
        return ClosureRequest(
            tool_call_id=authorized.record.id,
            consume_approval=authorized.approval is not None,
            close_session=True,
            metadata={"applied_segment_count": 1},
        )

    # --------------------------------------------------------------- execution

    def execute(
        self,
        *,
        authorized: Any,
        audit_event: AuditEvent,
        closure: ClosureRequest,
        intent: Any,
    ) -> ExecutionOutcome:
        document_id = audit_event.document_id
        try:
            request = AddCableSegmentIntent.model_validate(intent)
            if authorized.record.base_revision != request.expected_revision:
                raise ValueError(
                    "approval bound base_revision "
                    f"{authorized.record.base_revision} but intent expects "
                    f"{request.expected_revision}"
                )
            view = self.service.load(document_id)
            updated = view.document.with_segment(request.segment)
            success_audit = AuditRecordDraft(
                event_type="revision.created",
                actor=authorized.session.actor,
                surface="rest",
                tool_name=authorized.definition.name,
                status="applied",
                document_id=document_id,
                session_id=authorized.session.id,
                approval_id=authorized.approval.id if authorized.approval else None,
                tool_call_id=authorized.record.id,
                base_revision=request.expected_revision,
                result_revision=updated.revision,
                label=f"Added cable segment {request.segment.id}",
                evidence={
                    "cable_document_id": document_id,
                    "cable_segment_id": request.segment.id,
                },
            )
            self.service.store.commit_cable_write(
                document_id=document_id,
                expected_revision=request.expected_revision,
                data_json=self.service.persist(
                    __import__("agentcad.cable_service", fromlist=["CableDocumentView"]).CableDocumentView(
                        document_id=view.document_id, document=updated
                    )
                ),
                new_revision=updated.revision,
                audit=success_audit,
                tool_call=authorized.record.model_copy(
                    update={
                        "status": "completed",
                        "result_revision": updated.revision,
                        "completed_at": datetime.now(UTC),
                    }
                ),
                approval=authorized.approval.model_copy(
                    update={"status": "consumed", "consumed_at": datetime.now(UTC)}
                )
                if authorized.approval is not None
                else None,
                session=authorized.session.model_copy(
                    update={"status": "completed", "end_revision": updated.revision}
                ),
            )
            return ExecutionOutcome(
                document_id=document_id,
                base_revision=request.expected_revision,
                result_revision=updated.revision,
                payload={"cable_document_id": document_id, "segment_id": request.segment.id},
            )
        except Exception as exc:
            error_code = getattr(exc, "code", type(exc).__name__)
            self._failure_closeout(authorized, str(error_code))
            raise

    def _failure_closeout(self, authorized: Any, error_code: str) -> None:
        now = datetime.now(UTC)
        audit = AuditRecordDraft(
            event_type="revision.created",
            actor=authorized.session.actor,
            surface="rest",
            tool_name=authorized.definition.name,
            status="rejected",
            error_code=error_code,
            document_id=authorized.record.document_id,
            session_id=authorized.session.id,
            approval_id=authorized.approval.id if authorized.approval else None,
            tool_call_id=authorized.record.id,
            base_revision=authorized.record.base_revision,
            label=f"Cable write denied: {error_code}",
            evidence={"cable_document_id": authorized.record.document_id},
        )
        self.service.store.cable_failure_closeout(
            tool_call=authorized.record.model_copy(
                update={"status": "failed", "error_code": error_code, "completed_at": now}
            ),
            session=authorized.session.model_copy(
                update={"status": "failed", "updated_at": now}
            ),
            audit=audit,
        )
