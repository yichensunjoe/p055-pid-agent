from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .agent_semantic_models import SemanticTransaction
from .audit import request_audit_context
from .audit_models import AuditContext, ProvenanceState
from .diagnostics import DiagnosticLogger
from .drafting_models import DraftingRequest
from .harness_models import (
    AgentSession,
    AgentSessionAudit,
    AgentSessionCreateRequest,
    ToolApproval,
    ToolApprovalCreateRequest,
    ToolApprovalResolveRequest,
    ToolCallRecord,
)
from .layout_models import AutoLayoutRequest
from .m7_synthesis_models import SynthesisProposalEvidence
from .models import TransactionRequest, TransactionResult
from .service import (
    DocumentNotFoundError,
    DocumentService,
    InvalidOperationError,
    RevisionConflictError,
)
from .store import SQLiteDocumentStore
from .tool_registry import ToolDefinition, ToolRegistry, get_default_tool_registry


class HarnessError(RuntimeError):
    code = "harness_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class AgentSessionNotFoundError(HarnessError):
    code = "agent_session_not_found"


class ToolApprovalNotFoundError(HarnessError):
    code = "tool_approval_not_found"


class ToolApprovalRequiredError(HarnessError):
    code = "tool_approval_required"


class ToolApprovalRejectedError(HarnessError):
    code = "tool_approval_rejected"


class ToolPermissionDeniedError(HarnessError):
    code = "tool_permission_denied"


class ToolIntentMismatchError(HarnessError):
    code = "tool_intent_mismatch"


@dataclass(frozen=True)
class AuthorizedToolCall:
    definition: ToolDefinition
    session: AgentSession
    record: ToolCallRecord
    approval: ToolApproval | None


def _normalize_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _normalize_json(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    return str(value)


def canonicalize_tool_intent(tool_name: str, intent: Any) -> Any:
    """Normalize implemented tool payloads before hashing approvals.

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


def tool_intent_hash(tool_name: str, document_id: str, intent: Any) -> str:
    payload = {
        "tool_name": tool_name,
        "document_id": document_id,
        "intent": _normalize_json(intent),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AgentHarnessService:
    def __init__(
        self,
        service: DocumentService,
        store: SQLiteDocumentStore,
        registry: ToolRegistry | None = None,
        diagnostics: DiagnosticLogger | None = None,
    ):
        self.service = service
        self.store = store
        self.registry = registry or get_default_tool_registry()
        self.diagnostics = diagnostics

    # ------------------------------------------------------------------ governed writes

    def apply_authorized(
        self,
        authorized: AuthorizedToolCall,
        document_id: str,
        transaction: TransactionRequest,
        *,
        intent: Any | None = None,
        metadata: dict[str, Any] | None = None,
        validation_evidence: dict[str, Any] | None = None,
    ) -> TransactionResult:
        """Execute one approved tool call through the single governed write path.

        The document revision, its semantic diff, its audit record and the harness
        close-out (tool call completed, approval consumed, session completed) commit
        in one SQLite transaction. A failure records rejected/failed evidence and
        never leaves a revision without provenance.
        """
        session = authorized.session
        context: AuditContext = request_audit_context(
            authorized.definition.name,
            actor=session.actor,
            surface="rest" if session.metadata.get("surface") != "mcp" else "mcp",
            label=transaction.label or authorized.definition.description[:120],
            session_id=session.id,
            approval_id=authorized.record.approval_id,
            tool_call_id=authorized.record.id,
            provider=session.provider,
            model=session.model,
            intent_hash=authorized.record.intent_hash,
            diff_preview_hash=self._approved_diff_preview_hash(authorized),
            validation_status="valid",
            validation_evidence=validation_evidence or {},
            metadata={
                **(metadata or {}),
                "permission": authorized.definition.permission,
                "risk": authorized.definition.risk,
                "project_id": session.project_id,
            },
        )
        state = ProvenanceState(
            tool_call_id=authorized.record.id,
            consume_approval=authorized.approval is not None,
            close_session=True,
            tool_call_metadata={
                "applied_operations": len(transaction.operations),
                "transaction_label": transaction.label,
            },
        )
        try:
            return self.service.apply_transaction(
                document_id,
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
                document_id=document_id,
                base_revision=transaction.expected_revision,
                status="rejected",
                evidence={
                    "rejected_operation_count": len(transaction.operations),
                    "transaction_label": transaction.label,
                },
                state=state,
            )
            raise

    def _approved_diff_preview_hash(self, authorized: AuthorizedToolCall) -> str:
        approval = authorized.approval
        if approval is None:
            return ""
        return approval.diff_preview_hash

    def preview_intent_diff_hash(
        self,
        document_id: str,
        intent: dict[str, Any],
    ) -> str:
        """Best-effort semantic diff hash for the reviewed intent (never raises)."""
        try:
            transaction = TransactionRequest.model_validate(intent.get("transaction", {}))
            document = self.service.get_document(document_id)
            return self.service.audit.diff_hash_for_transaction(
                before=document, request=transaction
            )
        except Exception:  # pragma: no cover - preview must never block an approval request
            return ""

    def create_session(self, request: AgentSessionCreateRequest) -> AgentSession:
        document = self.service.get_document(request.document_id)
        session = AgentSession(
            document_id=document.id,
            actor=request.actor,
            project_id=request.project_id,
            provider=request.provider,
            model=request.model,
            start_revision=document.revision,
            metadata=request.metadata,
        )
        self.store.create_agent_session(session)
        self._emit(
            "agent.session.created",
            session_id=session.id,
            document_id=session.document_id,
            actor=session.actor,
            provider=session.provider,
            model=session.model,
            start_revision=session.start_revision,
        )
        return session

    def transit_session(
        self,
        session_id: str,
        *,
        status: str,
        end_revision: int | None = None,
    ) -> AgentSession:
        """Move a session to a terminal non-success state (cancel/fail).

        Session lifecycle lives in ``agent_sessions`` plus diagnostics; the audit chain
        records the revision attempts, so this intentionally does not append a
        synthetic revision-created fact.
        """
        if status not in {"cancelled", "failed"}:
            raise HarnessError(f"unsupported session transition: {status}", code="invalid_transition")
        session = self.get_session(session_id)
        if session.status != "active":
            raise HarnessError(
                f"agent session {session.id} is {session.status}",
                code="agent_session_not_active",
            )
        updated = session.model_copy(
            update={
                "status": status,
                "end_revision": end_revision if end_revision is not None else session.end_revision,
                "updated_at": datetime.now(UTC),
            }
        )
        self.store.update_agent_session(updated)
        self._emit(
            "agent.session.completed",
            session_id=updated.id,
            document_id=updated.document_id,
            start_revision=updated.start_revision,
            end_revision=updated.end_revision,
            status=updated.status,
        )
        return updated

    def get_session(self, session_id: str) -> AgentSession:
        session = self.store.get_agent_session(session_id)
        if session is None:
            raise AgentSessionNotFoundError(f"agent session not found: {session_id}")
        return session

    def ensure_session(
        self,
        document_id: str,
        *,
        session_id: str | None = None,
        actor: str = "system",
        provider: str = "",
        model: str = "",
    ) -> AgentSession:
        if session_id:
            session = self.get_session(session_id)
            if session.document_id != document_id:
                raise ToolIntentMismatchError(
                    f"session {session.id} belongs to document {session.document_id}, not {document_id}"
                )
            if session.status != "active":
                raise HarnessError(
                    f"agent session {session.id} is {session.status}",
                    code="agent_session_not_active",
                )
            return session
        return self.create_session(
            AgentSessionCreateRequest(
                document_id=document_id,
                actor=actor,
                provider=provider,
                model=model,
            )
        )

    def record_synthesis_proposal_evidence(
        self, evidence: SynthesisProposalEvidence
    ) -> SynthesisProposalEvidence:
        """Append one proposal attempt. Append-only: a replan adds a row, never overwrites."""

        self.store.append_synthesis_proposal_evidence(evidence)
        self._emit(
            "synthesis.proposal.recorded",
            session_id=evidence.session_id,
            document_id=evidence.document_id,
            proposal_evidence_id=evidence.proposal_evidence_id,
            proposal_attempt_index=evidence.proposal_attempt_index,
            validity=evidence.validity,
            completeness=evidence.completeness,
            proposed_operation_count=evidence.proposed_operation_count,
            accepted_operation_count=evidence.accepted_operation_count,
            rejected_operation_count=evidence.rejected_operation_count,
        )
        return evidence

    def latest_synthesis_proposal_evidence(
        self, session_id: str
    ) -> SynthesisProposalEvidence | None:
        return self.store.latest_synthesis_proposal_evidence(session_id)

    def assert_session_may_complete(self, session_id: str) -> None:
        """Refuse to mark a session completed while its latest proposal is not whole.

        The UI showing a completeness counts is not sufficient: a partially compiled plan
        must not be recordable as a completed session by any caller. A session with no
        synthesis proposal at all is unaffected, because most sessions do not synthesise.
        """

        evidence = self.store.latest_synthesis_proposal_evidence(session_id)
        if evidence is None:
            return
        if evidence.is_success_candidate():
            return
        raise HarnessError(
            f"agent session {session_id} may not be completed: its latest synthesis proposal "
            f"is {evidence.validity}/{evidence.completeness} "
            f"({evidence.accepted_operation_count} accepted, "
            f"{evidence.rejected_operation_count} rejected of "
            f"{evidence.proposed_operation_count})",
            code="synthesis_proposal_incomplete",
        )

    def complete_session(
        self,
        session_id: str,
        *,
        end_revision: int | None,
        status: str = "completed",
    ) -> AgentSession:
        if status == "completed":
            self.assert_session_may_complete(session_id)
        session = self.get_session(session_id)
        updated = session.model_copy(
            update={
                "status": status,
                "end_revision": end_revision,
                "updated_at": datetime.now(UTC),
            }
        )
        self.store.update_agent_session(updated)
        self._emit(
            "agent.session.completed",
            session_id=updated.id,
            document_id=updated.document_id,
            start_revision=updated.start_revision,
            end_revision=updated.end_revision,
            status=updated.status,
        )
        return updated

    def request_approval(
        self,
        session_id: str,
        request: ToolApprovalCreateRequest,
    ) -> ToolApproval:
        session = self.ensure_session(request.document_id, session_id=session_id)
        definition = self.registry.require(request.tool_name)
        canonical_intent = canonicalize_tool_intent(request.tool_name, request.intent)
        intent_hash = tool_intent_hash(
            request.tool_name,
            request.document_id,
            canonical_intent,
        )
        diff_preview_hash = self.preview_intent_diff_hash(request.document_id, request.intent)
        approval = ToolApproval(
            session_id=session.id,
            tool_name=definition.name,
            document_id=request.document_id,
            intent_hash=intent_hash,
            requested_by=request.requested_by,
            reason=request.reason,
            diff_preview_hash=diff_preview_hash,
            evidence=self._approval_evidence(definition, canonical_intent, diff_preview_hash),
        )
        self.store.create_tool_approval(approval)
        self._emit(
            "agent.approval.requested",
            approval_id=approval.id,
            session_id=session.id,
            document_id=request.document_id,
            tool_name=definition.name,
            permission=definition.permission,
            risk=definition.risk,
            requested_by=request.requested_by,
            intent_hash=intent_hash,
        )
        return approval

    def resolve_approval(
        self,
        approval_id: str,
        request: ToolApprovalResolveRequest,
    ) -> ToolApproval:
        approval = self.store.get_tool_approval(approval_id)
        if approval is None:
            raise ToolApprovalNotFoundError(f"tool approval not found: {approval_id}")
        if approval.status != "pending":
            raise HarnessError(
                f"approval {approval.id} is already {approval.status}",
                code="tool_approval_already_resolved",
            )
        now = datetime.now(UTC)
        updated = approval.model_copy(
            update={
                "status": "approved" if request.approved else "rejected",
                "resolved_by": request.actor,
                "note": request.note,
                "resolved_at": now,
            }
        )
        self.store.update_tool_approval(updated)
        self._emit(
            "agent.approval.resolved",
            approval_id=updated.id,
            session_id=updated.session_id,
            document_id=updated.document_id,
            tool_name=updated.tool_name,
            status=updated.status,
            resolved_by=updated.resolved_by,
        )
        return updated

    def authorize(
        self,
        *,
        session_id: str,
        tool_name: str,
        document_id: str,
        intent: Any,
        approval_id: str | None = None,
        base_revision: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuthorizedToolCall:
        definition = self.registry.require(tool_name)
        session = self.ensure_session(document_id, session_id=session_id)
        canonical_intent = canonicalize_tool_intent(tool_name, intent)
        intent_hash = tool_intent_hash(tool_name, document_id, canonical_intent)
        approval: ToolApproval | None = None

        if definition.permission == "deny":
            self._record_rejected_call(
                session,
                definition,
                document_id,
                intent_hash,
                approval_id,
                base_revision,
                "tool_permission_denied",
                metadata,
            )
            raise ToolPermissionDeniedError(f"tool is denied by policy: {tool_name}")

        def deny(error_code: str, error: Exception) -> None:
            """Record the refusal, then raise. Every refusal path is reviewable.

            A refusal is evidence too: a reviewer must be able to answer "who tried
            what, against which revision, and why was it refused" even when nothing
            was written. Recording must never mask the original error, so a failure to
            persist the record is swallowed after the tool-call row already exists.
            """
            self._record_rejected_call(
                session,
                definition,
                document_id,
                intent_hash,
                approval_id,
                base_revision,
                error_code,
                metadata,
            )
            raise error

        if definition.permission == "ask":
            if not approval_id:
                deny(
                    "tool_approval_required",
                    ToolApprovalRequiredError(
                        f"tool requires explicit approval before execution: {tool_name}"
                    ),
                )
            approval = self.store.get_tool_approval(approval_id)
            if approval is None:
                deny(
                    "tool_approval_not_found",
                    ToolApprovalNotFoundError(f"tool approval not found: {approval_id}"),
                )
            if (
                approval.session_id != session.id
                or approval.tool_name != tool_name
                or approval.document_id != document_id
                or approval.intent_hash != intent_hash
            ):
                deny(
                    "tool_intent_mismatch",
                    ToolIntentMismatchError(
                        "approval does not match the exact session/tool/document/intent"
                    ),
                )
            if approval.status == "rejected":
                deny(
                    "tool_approval_rejected",
                    ToolApprovalRejectedError(f"approval was rejected: {approval_id}"),
                )
            if approval.status != "approved":
                deny(
                    "tool_approval_consumed",
                    ToolApprovalRequiredError(
                        f"approval is not approved or was already consumed: {approval_id}"
                    ),
                )

        record = ToolCallRecord(
            session_id=session.id,
            tool_name=definition.name,
            document_id=document_id,
            permission=definition.permission,
            risk=definition.risk,
            approval_id=approval.id if approval else None,
            intent_hash=intent_hash,
            base_revision=base_revision,
            metadata=metadata or {},
        )
        self.store.create_tool_call(record)
        self._emit(
            definition.audit_event + ".started",
            tool_call_id=record.id,
            session_id=session.id,
            approval_id=record.approval_id,
            document_id=document_id,
            tool_name=definition.name,
            permission=definition.permission,
            risk=definition.risk,
            intent_hash=intent_hash,
            base_revision=base_revision,
        )
        return AuthorizedToolCall(
            definition=definition,
            session=session,
            record=record,
            approval=approval,
        )

    def complete_tool_call(
        self,
        authorized: AuthorizedToolCall,
        *,
        result_revision: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ToolCallRecord:
        completed = authorized.record.model_copy(
            update={
                "status": "completed",
                "result_revision": result_revision,
                "completed_at": datetime.now(UTC),
                "metadata": {**authorized.record.metadata, **(metadata or {})},
            }
        )
        self.store.update_tool_call(completed)
        if authorized.approval is not None:
            consumed = authorized.approval.model_copy(
                update={
                    "status": "consumed",
                    "consumed_at": datetime.now(UTC),
                }
            )
            self.store.update_tool_approval(consumed)
        self._emit(
            authorized.definition.audit_event + ".completed",
            tool_call_id=completed.id,
            session_id=completed.session_id,
            approval_id=completed.approval_id,
            document_id=completed.document_id,
            tool_name=completed.tool_name,
            base_revision=completed.base_revision,
            result_revision=completed.result_revision,
        )
        return completed

    def fail_tool_call(
        self,
        authorized: AuthorizedToolCall,
        *,
        error_code: str,
    ) -> ToolCallRecord:
        failed = authorized.record.model_copy(
            update={
                "status": "failed",
                "error_code": error_code,
                "completed_at": datetime.now(UTC),
            }
        )
        self.store.update_tool_call(failed)
        self._emit(
            authorized.definition.audit_event + ".failed",
            tool_call_id=failed.id,
            session_id=failed.session_id,
            approval_id=failed.approval_id,
            document_id=failed.document_id,
            tool_name=failed.tool_name,
            error_code=error_code,
        )
        return failed

    def audit(self, session_id: str) -> AgentSessionAudit:
        return AgentSessionAudit(
            session=self.get_session(session_id),
            approvals=self.store.list_tool_approvals(session_id),
            tool_calls=self.store.list_tool_calls(session_id),
        )

    @staticmethod
    def _approval_evidence(
        definition: ToolDefinition,
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

    def _tool_audit_context(
        self,
        *,
        session: AgentSession,
        definition: ToolDefinition,
        approval_id: str | None = None,
        tool_call_id: str | None = None,
        intent_hash: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AuditContext:
        return request_audit_context(
            definition.name,
            actor=session.actor,
            surface="mcp" if session.metadata.get("surface") == "mcp" else "rest",
            label=definition.description[:120],
            session_id=session.id,
            approval_id=approval_id,
            tool_call_id=tool_call_id,
            provider=session.provider,
            model=session.model,
            intent_hash=intent_hash,
            metadata={
                **(metadata or {}),
                "permission": definition.permission,
                "risk": definition.risk,
            },
        )

    def _record_rejected_call(
        self,
        session: AgentSession,
        definition: ToolDefinition,
        document_id: str,
        intent_hash: str,
        approval_id: str | None,
        base_revision: int | None,
        error_code: str,
        metadata: dict[str, Any] | None,
    ) -> None:
        now = datetime.now(UTC)
        record = ToolCallRecord(
            session_id=session.id,
            tool_name=definition.name,
            document_id=document_id,
            permission=definition.permission,
            risk=definition.risk,
            approval_id=approval_id,
            intent_hash=intent_hash,
            base_revision=base_revision,
            status="rejected",
            error_code=error_code,
            started_at=now,
            completed_at=now,
            metadata=metadata or {},
        )
        self.store.create_tool_call(record)
        # Chain the denial itself: "who tried what, and why it was refused" must be
        # reviewable evidence, not only present in the harness tables.
        self.service.audit.record_rejection(
            self._tool_audit_context(
                session=session,
                definition=definition,
                approval_id=approval_id,
                tool_call_id=record.id,
                intent_hash=intent_hash,
                metadata=metadata,
            ),
            event_type="permission.rejected",
            error_code=error_code,
            document_id=document_id,
            base_revision=base_revision,
            evidence={
                "tool_permission": definition.permission,
                "tool_risk": definition.risk,
                "intent_hash": intent_hash,
                "authorized": False,
            },
        )
        self._emit(
            definition.audit_event + ".rejected",
            tool_call_id=record.id,
            session_id=session.id,
            approval_id=approval_id,
            document_id=document_id,
            tool_name=definition.name,
            error_code=error_code,
        )

    def _emit(self, event: str, **fields: Any) -> None:
        if self.diagnostics is not None:
            self.diagnostics.emit(event, **fields)
