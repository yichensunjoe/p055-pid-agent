"""The domain-neutral agent harness runtime (M10-P2, Gate CODE GO).

Everything here is generic governance: session lifecycle, tool authorization
(permission policy, canonical-intent hashing, approval binding), tool-call
records and the audit/diagnostics emissions. All domain knowledge lives behind
the ports in :mod:`agentcad.runtime.ports`:

* ``DomainAdapter`` — document context, intent canonicalization, diff preview,
  approval/rejection evidence, and the atomic governed write (R10-4: the
  engineering mutation AND the harness close-out land in one storage
  transaction, on success and on failure);
* ``HarnessStorePort`` — session/approval/tool-call persistence;
* ``AuditPort`` — neutral audit-event recording (full field parity, P2-4);
* ``ToolRegistryPort`` — neutral tool lookup (P2-6/R10-6).

Isolation contract (P2-1): importing this module must not load ANY non-runtime
``agentcad.*`` module — locked by a subprocess assertion in
``test_m10_p1_runtime.py`` and the P2 parity suites.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .models import (
    AgentSession,
    AgentSessionAudit,
    AgentSessionCreateRequest,
    ToolApproval,
    ToolApprovalCreateRequest,
    ToolApprovalResolveRequest,
    ToolCallRecord,
)
from .ports import (
    AuditEvent,
    AuditPort,
    DomainAdapter,
    ExecutionOutcome,
    HarnessStorePort,
    ToolDefinitionView,
    ToolRegistryPort,
)


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


class DiagnosticsPort(Protocol):
    """Structural stand-in for the diagnostics logger (never imported here)."""

    def emit(self, event: str, **fields: Any) -> None: ...


@dataclass(frozen=True)
class AuthorizedToolCall:
    """One authorized call, ready for the domain adapter to execute."""

    definition: ToolDefinitionView
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
    """Generic pass-through. Domain-specific normalization (e.g. filling
    Pydantic defaults before hashing) is the DomainAdapter's job; the runtime
    approval identity only depends on this deterministic normalization."""
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


class AgentHarnessRuntime:
    """Domain-neutral harness core (P2). Behavior is byte-for-byte the
    pre-M10 semantics; only the domain edges now route through ports."""

    def __init__(
        self,
        *,
        store: HarnessStorePort,
        registry: ToolRegistryPort,
        adapter: DomainAdapter,
        audit: AuditPort,
        diagnostics: DiagnosticsPort | None = None,
        completion_guard: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.adapter = adapter
        self.audit_port = audit
        self.diagnostics = diagnostics
        # Domain-injected guard run before a session may complete (P2-5: the
        # P&ID synthesis completeness guard plugs in here; the runtime itself
        # knows nothing about synthesis).
        self._completion_guard = completion_guard

    # ------------------------------------------------------------------ sessions

    def create_session(self, request: AgentSessionCreateRequest) -> AgentSession:
        context = self.adapter.document_context(document_id=request.document_id)
        session = AgentSession(
            document_id=context.document_id,
            actor=request.actor,
            project_id=request.project_id,
            provider=request.provider,
            model=request.model,
            start_revision=context.revision,
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

    def complete_session(
        self,
        session_id: str,
        *,
        end_revision: int | None,
        status: str = "completed",
    ) -> AgentSession:
        if status == "completed" and self._completion_guard is not None:
            self._completion_guard(session_id)
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

    # ----------------------------------------------------------------- approvals

    def request_approval(
        self,
        session_id: str,
        request: ToolApprovalCreateRequest,
    ) -> ToolApproval:
        session = self.ensure_session(request.document_id, session_id=session_id)
        definition = self.registry.require(request.tool_name)
        canonical_intent = self.adapter.canonicalize_intent(request.tool_name, request.intent)
        intent_hash = tool_intent_hash(
            request.tool_name,
            request.document_id,
            canonical_intent,
        )
        diff_preview_hash = self.adapter.preview_diff_hash(
            document_id=request.document_id, intent=request.intent
        )
        approval = ToolApproval(
            session_id=session.id,
            tool_name=definition.name,
            document_id=request.document_id,
            intent_hash=intent_hash,
            requested_by=request.requested_by,
            reason=request.reason,
            diff_preview_hash=diff_preview_hash,
            evidence=self.adapter.approval_evidence(
                definition=definition,
                canonical_intent=canonical_intent,
                diff_preview_hash=diff_preview_hash,
            ),
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

    # ------------------------------------------------------------- authorization

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
        canonical_intent = self.adapter.canonicalize_intent(tool_name, intent)
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
            was written. Recording must never mask the original error, so a failure
            to persist the record is swallowed after the tool-call row already exists.
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

    # ------------------------------------------------------------------- writes

    def apply_authorized(
        self,
        authorized: AuthorizedToolCall,
        document_id: str,
        intent: Any,
        *,
        validation_evidence: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExecutionOutcome:
        """Execute one approved call through the domain's governed write path.

        The atomicity contract lives in the DomainAdapter (R10-4): the domain's
        governed write must land the engineering mutation AND the harness
        close-out (tool call completed, approval consumed, session completed)
        in one storage transaction — a failure records rejected/failed evidence
        and never leaves a revision without provenance.
        """
        definition = authorized.definition
        session = authorized.session
        approval = authorized.approval
        event = AuditEvent(
            event_type="revision.created",
            actor=session.actor,
            tool_name=definition.name,
            surface="mcp" if session.metadata.get("surface") == "mcp" else "rest",
            label=definition.description[:120],
            session_id=session.id,
            approval_id=approval.id if approval else None,
            tool_call_id=authorized.record.id,
            document_id=document_id,
            base_revision=authorized.record.base_revision,
            provider=session.provider,
            model=session.model,
            intent_hash=authorized.record.intent_hash,
            diff_preview_hash=self._approved_diff_preview_hash(authorized),
            validation_status="valid",
            validation_evidence=validation_evidence or {},
            metadata={
                **(metadata or {}),
                "permission": definition.permission,
                "risk": definition.risk,
                "project_id": session.project_id,
            },
        )
        closure = self.adapter.closure_request(authorized=authorized, intent=intent)
        return self.adapter.execute(
            authorized=authorized,
            audit_event=event,
            closure=closure,
            intent=intent,
        )

    # ------------------------------------------------------------------ records

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

    # ------------------------------------------------------------------ helpers

    def _approved_diff_preview_hash(self, authorized: AuthorizedToolCall) -> str:
        approval = authorized.approval
        if approval is None:
            return ""
        return approval.diff_preview_hash

    def _record_rejected_call(
        self,
        session: AgentSession,
        definition: ToolDefinitionView,
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
        # reviewable evidence, not only present in the harness tables. (P2-4: every
        # AuditContext field below is carried by the neutral AuditEvent verbatim.)
        self.audit_port.record(
            AuditEvent(
                event_type="permission.rejected",
                actor=session.actor,
                tool_name=definition.name,
                surface="mcp" if session.metadata.get("surface") == "mcp" else "rest",
                status="rejected",
                error_code=error_code,
                label=definition.description[:120],
                session_id=session.id,
                approval_id=approval_id,
                tool_call_id=record.id,
                document_id=document_id,
                base_revision=base_revision,
                provider=session.provider,
                model=session.model,
                intent_hash=intent_hash,
                metadata={"permission": definition.permission, "risk": definition.risk},
                evidence=self.adapter.rejection_evidence(
                    definition=definition,
                    intent=intent_hash,
                    error_code=error_code,
                ),
            )
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


__all__ = [
    "AgentHarnessRuntime",
    "AgentSessionNotFoundError",
    "AuthorizedToolCall",
    "HarnessError",
    "ToolApprovalNotFoundError",
    "ToolApprovalRejectedError",
    "ToolApprovalRequiredError",
    "ToolIntentMismatchError",
    "ToolPermissionDeniedError",
    "canonicalize_tool_intent",
    "tool_intent_hash",
]
