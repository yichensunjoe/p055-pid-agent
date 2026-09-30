"""P&ID domain adapter + audit adapter + registry port (M10-P2).

This module owns EVERY P&ID-specific seam the harness used to hard-code:
intent canonicalization for the four governed tools, semantic diff preview,
approval evidence, the governed write through DocumentService.apply_transaction
with its atomic provenance close-out, and the audit-context conversion. The
generic runtime (agentcad.runtime.harness) depends only on the neutral ports.
"""

from __future__ import annotations

from typing import Any

from .agent_semantic_models import SemanticTransaction
from .audit import AuditRecorder, request_audit_context
from .audit_models import AuditContext, ProvenanceState
from .drafting_models import DraftingRequest
from .layout_models import AutoLayoutRequest
from .m7_synthesis_models import (
    SynthesisProposalEvidence,  # noqa: F401  (re-exported for the facade)
)
from .models import TransactionRequest
from .runtime.ports import (
    AuditEvent,
    AuditRecordRef,
    ClosureRequest,
    DocumentContext,
    ExecutionOutcome,
    ToolDefinitionView,
)
from .service import (
    DocumentNotFoundError,
    DocumentService,
    InvalidOperationError,
    RevisionConflictError,
)
from .tool_registry import ToolDefinition, ToolRegistry


def canonicalize_pid_intent(tool_name: str, intent: Any) -> Any:
    """Pre-M10 semantics, moved verbatim from harness.canonicalize_tool_intent.

    Approval identity is semantic, not dependent on whether a caller omitted fields
    that Pydantic later fills with deterministic defaults.
    """
    if not isinstance(intent, dict):
        return intent
    if tool_name == "apply_compiled_agent_transaction":
        transaction = TransactionRequest.model_validate(intent.get("transaction", {}))
        return {"transaction": transaction.model_dump(mode="json")}
    if tool_name == "apply_agent_transaction":
        transaction = SemanticTransaction.model_validate(intent.get("transaction", {}))
        return {"transaction": transaction.model_dump(mode="json")}
    if tool_name == "apply_auto_layout":
        options = AutoLayoutRequest.model_validate(intent.get("options", {}))
        return {"options": options.model_dump(mode="json")}
    if tool_name == "apply_deterministic_drafting":
        request = DraftingRequest.model_validate(intent.get("request", {}))
        return {"request": request.model_dump(mode="json")}
    return intent


class PidToolRegistryPort:
    """R10-6: neutral governance view over the existing P&ID tool catalogue.

    The catalogue itself is neither moved nor retyped (P1/P2 forbidden list);
    this port only projects the fields the runtime core is allowed to see.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def require(self, name: str) -> ToolDefinitionView:
        definition: ToolDefinition = self._registry.require(name)
        return ToolDefinitionView(
            name=definition.name,
            description=definition.description,
            permission=definition.permission,
            risk=definition.risk,
            audit_event=definition.audit_event,
        )


class PidAuditAdapter:
    """P2-4: converts the neutral AuditEvent into the native audit context.

    Field parity is contractual — every AuditContext binding the pre-M10
    rejection path produced must be reproduced here (actor/surface/label/
    session/approval/tool-call IDs/provider/model/intent_hash plus the
    permission+risk metadata), and hash formation / chain schema / event
    semantics stay inside AuditRecorder.
    """

    def __init__(self, recorder: AuditRecorder) -> None:
        self._recorder = recorder

    def record(self, event: AuditEvent) -> AuditRecordRef:
        context = self._context(event)
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
            record_id=record.record_id,
            ordinal=record.ordinal,
            record_hash=record.record_hash,
        )

    def _context(self, event: AuditEvent) -> AuditContext:
        # First positional is the TOOL name (request_audit_context's contract),
        # exactly as the pre-M10 _tool_audit_context passed definition.name.
        return request_audit_context(
            event.tool_name,
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


class PidDomainAdapter:
    """The P&ID implementation of every DomainAdapter port (R10-1 closure)."""

    def __init__(self, service: DocumentService) -> None:
        self.service = service

    # ---------------------------------------------------------------- context

    def document_context(self, *, document_id: str) -> DocumentContext:
        document = self.service.get_document(document_id)
        return DocumentContext(document_id=document.id, revision=document.revision)

    def canonicalize_intent(self, tool_name: str, intent: Any) -> Any:
        return canonicalize_pid_intent(tool_name, intent)

    def preview_diff_hash(self, *, document_id: str, intent: Any) -> str:
        """Best-effort semantic diff hash for the reviewed intent (never raises)."""
        try:
            transaction = TransactionRequest.model_validate(intent.get("transaction", {}))
            document = self.service.get_document(document_id)
            return self.service.audit.diff_hash_for_transaction(
                before=document, request=transaction
            )
        except Exception:  # pragma: no cover - preview must never block an approval request
            return ""

    def approval_evidence(
        self,
        *,
        definition: ToolDefinitionView,
        canonical_intent: Any,
        diff_preview_hash: str,
    ) -> dict[str, Any]:
        """Bounded, secret-free review metadata attached to an approval request."""
        operations: list[str] = []
        touched: list[str] = []
        if isinstance(canonical_intent, dict):
            transaction = canonical_intent.get("transaction")
            if isinstance(transaction, dict):
                raw_operations = transaction.get("operations")
                if isinstance(raw_operations, list):
                    for operation in raw_operations[:100]:
                        if isinstance(operation, dict) and isinstance(operation.get("op"), str):
                            operations.append(str(operation["op"]))
                            element_id = operation.get("element_id")
                            if isinstance(element_id, str):
                                touched.append(element_id)
        return {
            "tool": definition.name,
            "tool_risk": definition.risk,
            "tool_permission": definition.permission,
            "operation_types": operations,
            "touched_element_ids": touched[:100],
            "diff_preview_hash": diff_preview_hash,
        }

    def rejection_evidence(
        self,
        *,
        definition: ToolDefinitionView,
        intent: Any,
        error_code: str,
    ) -> dict[str, Any]:
        # Pre-M10 semantics: the rejection evidence is this fixed, domain-neutral
        # shape (the intent itself is never embedded in the denial record).
        return {
            "tool_permission": definition.permission,
            "tool_risk": definition.risk,
            "intent_hash": intent,
            "authorized": False,
        }

    def closure_request(self, *, authorized: Any, intent: Any) -> ClosureRequest:
        transaction = TransactionRequest.model_validate(intent)
        return ClosureRequest(
            tool_call_id=authorized.record.id,
            consume_approval=authorized.approval is not None,
            close_session=True,
            metadata={
                "applied_operations": len(transaction.operations),
                "transaction_label": transaction.label,
            },
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
        """The single governed write path, moved verbatim from harness.apply_authorized.

        R10-4 postcondition: the document revision, its audit record and the
        harness close-out (tool call completed, approval consumed, session
        completed) commit in one SQLite transaction. A failure records
        rejected/failed evidence and never leaves a revision without provenance.
        """
        transaction = TransactionRequest.model_validate(intent)
        context = self._context(audit_event, transaction)
        state = ProvenanceState(
            tool_call_id=closure.tool_call_id,
            consume_approval=closure.consume_approval,
            close_session=closure.close_session,
            tool_call_metadata=closure.metadata,
        )
        try:
            result = self.service.apply_transaction(
                audit_event.document_id,
                transaction,
                source="llm" if context.surface == "rest" else "mcp",
                audit=context,
                state=state,
            )
        except (InvalidOperationError, RevisionConflictError, DocumentNotFoundError) as exc:
            error_code = getattr(exc, "code", type(exc).__name__)
            self.service.audit.record_failure(
                context=context,
                event_type="revision.created",
                error_code=str(error_code),
                document_id=audit_event.document_id,
                base_revision=transaction.expected_revision,
                status="rejected",
                evidence={
                    "rejected_operation_count": len(transaction.operations),
                    "transaction_label": transaction.label,
                },
                state=state,
            )
            raise
        return ExecutionOutcome(
            document_id=result.document.id,
            base_revision=audit_event.base_revision,
            result_revision=result.document.revision,
            payload=result,
        )

    @staticmethod
    def _context(event: AuditEvent, transaction: TransactionRequest) -> AuditContext:
        # Rebuild the exact pre-M10 AuditContext from the neutral carrier:
        # tool name first (request_audit_context's positional), and the label
        # precedence of the old apply_authorized (transaction.label wins).
        surface = "mcp" if event.surface == "mcp" else "rest"
        return request_audit_context(
            event.tool_name,
            actor=event.actor,
            surface=surface,
            label=transaction.label or event.label,
            session_id=event.session_id,
            approval_id=event.approval_id,
            tool_call_id=event.tool_call_id,
            provider=event.provider,
            model=event.model,
            intent_hash=event.intent_hash,
            diff_preview_hash=event.diff_preview_hash,
            validation_status="valid",
            validation_evidence=event.validation_evidence,
            metadata=event.metadata,
        )
