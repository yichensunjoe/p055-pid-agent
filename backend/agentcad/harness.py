"""Agent harness — P&ID compatibility facade over the M10 runtime core (P2).

Every historical import path keeps working unchanged (P2-2):

* all error classes and ``AuthorizedToolCall`` come from
  :mod:`agentcad.runtime.harness` (same objects);
* ``canonicalize_tool_intent`` / ``tool_intent_hash`` keep their exact
  pre-M10 semantics — the P&ID canonicalization special cases live in
  :class:`PidDomainAdapter`, applied before the generic hash;
* ``AgentHarnessService`` keeps its constructor signature
  ``(service, store, registry=None, diagnostics=None)`` so main.py and every
  API module are untouched.

P&ID-specific state that must stay P&ID (P2-5): the synthesis proposal
evidence accessors and the session-completion completeness guard live HERE,
not in the runtime. The generic runtime only knows an optional
``completion_guard`` callback.
"""

from __future__ import annotations

from typing import Any

from .m7_synthesis_models import SynthesisProposalEvidence
from .pid_domain_adapter import (
    PidAuditAdapter,
    PidDomainAdapter,
    PidToolRegistryPort,
)
from .runtime.harness import (
    AgentHarnessRuntime,
    AgentSessionNotFoundError,
    AuthorizedToolCall,
    HarnessError,
    ToolApprovalNotFoundError,
    ToolApprovalRejectedError,
    ToolApprovalRequiredError,
    ToolIntentMismatchError,
    ToolPermissionDeniedError,
    tool_intent_hash,
)
from .runtime.models import (
    AgentSession,
    AgentSessionAudit,
    AgentSessionCreateRequest,
    ToolApproval,
    ToolApprovalCreateRequest,
    ToolApprovalResolveRequest,
    ToolCallRecord,
)
from .runtime.ports import ExecutionOutcome
from .service import DocumentService
from .store import SQLiteDocumentStore
from .tool_registry import ToolRegistry, get_default_tool_registry


def canonicalize_tool_intent(tool_name: str, intent: Any) -> Any:
    """Pre-M10 entry point, semantics preserved: generic normalization plus the
    P&ID per-tool canonicalization (Pydantic default filling) for the four
    governed tools, pass-through for everything else."""
    from .pid_domain_adapter import canonicalize_pid_intent

    return canonicalize_pid_intent(tool_name, intent)


class AgentHarnessService:
    """Composition root for the P&ID deployment of the runtime harness.

    Construction signature is unchanged since before M10; the P&ID adapters
    are built internally and injected into the generic runtime.
    """

    def __init__(
        self,
        service: DocumentService,
        store: SQLiteDocumentStore,
        registry: ToolRegistry | None = None,
        diagnostics: Any = None,
    ) -> None:
        pid_registry = registry if registry is not None else get_default_tool_registry()
        self.service = service
        self.store = store
        self.registry = pid_registry
        self.diagnostics = diagnostics
        self._pid_adapter = PidDomainAdapter(service)
        self._runtime = AgentHarnessRuntime(
            store=store,
            registry=PidToolRegistryPort(pid_registry),
            adapter=self._pid_adapter,
            audit=PidAuditAdapter(service.audit),
            diagnostics=diagnostics,
            completion_guard=self.assert_session_may_complete,
        )

    # ---------------------------------------------------------------- proxy API
    # Behavioral parity is the contract: these forward to the runtime core.

    def create_session(self, request: AgentSessionCreateRequest) -> AgentSession:
        return self._runtime.create_session(request)

    def transit_session(
        self,
        session_id: str,
        *,
        status: str,
        end_revision: int | None = None,
    ) -> AgentSession:
        return self._runtime.transit_session(session_id, status=status, end_revision=end_revision)

    def get_session(self, session_id: str) -> AgentSession:
        return self._runtime.get_session(session_id)

    def ensure_session(
        self,
        document_id: str,
        *,
        session_id: str | None = None,
        actor: str = "system",
        provider: str = "",
        model: str = "",
    ) -> AgentSession:
        return self._runtime.ensure_session(
            document_id, session_id=session_id, actor=actor, provider=provider, model=model
        )

    def complete_session(
        self,
        session_id: str,
        *,
        end_revision: int | None,
        status: str = "completed",
    ) -> AgentSession:
        return self._runtime.complete_session(session_id, end_revision=end_revision, status=status)

    def request_approval(
        self,
        session_id: str,
        request: ToolApprovalCreateRequest,
    ) -> ToolApproval:
        return self._runtime.request_approval(session_id, request)

    def resolve_approval(
        self,
        approval_id: str,
        request: ToolApprovalResolveRequest,
    ) -> ToolApproval:
        return self._runtime.resolve_approval(approval_id, request)

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
        return self._runtime.authorize(
            session_id=session_id,
            tool_name=tool_name,
            document_id=document_id,
            intent=intent,
            approval_id=approval_id,
            base_revision=base_revision,
            metadata=metadata,
        )

    def apply_authorized(
        self,
        authorized: AuthorizedToolCall,
        document_id: str,
        transaction: Any,
        *,
        intent: Any | None = None,
        metadata: dict[str, Any] | None = None,
        validation_evidence: dict[str, Any] | None = None,
    ) -> Any:
        """Pre-M10 signature and return value preserved (P2-3): the runtime
        returns a neutral ExecutionOutcome whose opaque payload IS the
        historical TransactionResult; this facade unwraps it verbatim."""
        outcome: ExecutionOutcome = self._runtime.apply_authorized(
            authorized,
            document_id,
            transaction,
            metadata=metadata,
            validation_evidence=validation_evidence,
        )
        return outcome.payload

    def complete_tool_call(
        self,
        authorized: AuthorizedToolCall,
        *,
        result_revision: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ToolCallRecord:
        return self._runtime.complete_tool_call(
            authorized, result_revision=result_revision, metadata=metadata
        )

    def fail_tool_call(
        self,
        authorized: AuthorizedToolCall,
        *,
        error_code: str,
    ) -> ToolCallRecord:
        return self._runtime.fail_tool_call(authorized, error_code=error_code)

    def audit(self, session_id: str) -> AgentSessionAudit:
        return self._runtime.audit(session_id)

    # ------------------------------------------------- P&ID-specific (P2-5)
    # Synthesis proposal evidence stays a P&ID facade concern; the generic
    # runtime never sees it. The completion guard is injected into the runtime
    # and runs on every completed-session path.

    def record_synthesis_proposal_evidence(
        self, evidence: SynthesisProposalEvidence
    ) -> SynthesisProposalEvidence:
        """Append one proposal attempt. Append-only: a replan adds a row, never overwrites."""
        self.store.append_synthesis_proposal_evidence(evidence)
        if self.diagnostics is not None:
            self.diagnostics.emit(
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


__all__ = [
    "AgentHarnessService",
    "AgentSession",
    "AgentSessionAudit",
    "AgentSessionCreateRequest",
    "AgentSessionNotFoundError",
    "AuthorizedToolCall",
    "HarnessError",
    "ToolApproval",
    "ToolApprovalCreateRequest",
    "ToolApprovalNotFoundError",
    "ToolApprovalRejectedError",
    "ToolApprovalRequiredError",
    "ToolApprovalResolveRequest",
    "ToolCallRecord",
    "ToolIntentMismatchError",
    "ToolPermissionDeniedError",
    "canonicalize_tool_intent",
    "tool_intent_hash",
]
