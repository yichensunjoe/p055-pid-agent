"""Runtime contract ports (M10-P1, Gate-frozen) — the harness/domain seam.

These protocols are the entire surface the runtime core (moved in P2) may use
to talk to a domain or to persistence/audit. They intentionally reference no
P&ID type at runtime: model types enter only under ``TYPE_CHECKING`` and every
payload the runtime passes around is either a neutral dataclass defined here
or an opaque ``Any`` bound by hash, never inspected.

Gate-frozen rules (M10-P1 hard locks):
* this module must not import models.py / service.py / store.py / audit.py /
  audit_models.py / tool_registry.py or any P&ID/M6/M7/review/release module —
  enforced by a subprocess isolation test (R10-8 corrected logic);
* ``HarnessStorePort`` mirrors the EXISTING SQLiteDocumentStore method names
  and semantics one-for-one (R10-7) — no parallel API is invented;
* ``ToolRegistryPort`` is the neutral governance view of a tool (R10-6) so the
  runtime's authorize() never imports the P&ID catalogue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:  # static only — never imported at runtime (isolation lock)
    from agentcad.harness_models import (
        AgentSession,
        ToolApproval,
        ToolCallRecord,
    )

AuditSurface = Literal["rest", "mcp", "cli", "internal"]
AuditStatus = Literal["applied", "rejected", "failed"]
# R69-1: the runtime's neutral ToolRisk keeps the EXACT existing governance
# value set — the P&ID tool registry and ToolCallRecord already use these five
# strings, and P2 writes definition.risk into existing governance rows. Any
# remapping here would distort the frozen contract.
ToolRisk = Literal["read", "draft_edit", "engineering_change", "critical_change", "release"]
ToolPermission = Literal["allow", "ask", "deny"]


@dataclass(frozen=True)
class DocumentContext:
    """What the runtime needs to know about a document: it exists, and where."""

    document_id: str
    revision: int


@dataclass(frozen=True)
class ClosureRequest:
    """Neutral provenance close-out for one governed write (R10-4 postcondition).

    The domain adapter must land the engineering mutation AND this close-out in
    one storage transaction: on success all five closures appear (revision,
    audit record, tool-call closed, approval consumed, session completed); on
    failure neither direction may half-close.
    """

    tool_call_id: str
    consume_approval: bool
    close_session: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionOutcome:
    document_id: str
    base_revision: int
    result_revision: int


@dataclass(frozen=True)
class AuditRecordRef:
    record_id: str
    ordinal: int
    record_hash: str


@dataclass(frozen=True)
class ToolDefinitionView:
    """Neutral governance view of one tool (R10-6)."""

    name: str
    description: str
    permission: ToolPermission
    risk: ToolRisk
    audit_event: str


class ToolRegistryPort(Protocol):
    """Neutral tool lookup; the P&ID catalogue implements it from P2 on."""

    def require(self, name: str) -> ToolDefinitionView: ...


class HarnessStorePort(Protocol):
    """Session / approval / tool-call persistence (R10-7: exact existing names)."""

    def create_agent_session(self, session: AgentSession) -> None: ...

    def get_agent_session(self, session_id: str) -> AgentSession | None: ...

    def update_agent_session(self, session: AgentSession) -> None: ...

    def create_tool_approval(self, approval: ToolApproval) -> None: ...

    def get_tool_approval(self, approval_id: str) -> ToolApproval | None: ...

    def update_tool_approval(self, approval: ToolApproval) -> None: ...

    def list_tool_approvals(self, session_id: str) -> list[ToolApproval]: ...

    def create_tool_call(self, record: ToolCallRecord) -> None: ...

    def get_tool_call(self, tool_call_id: str) -> ToolCallRecord | None: ...

    def update_tool_call(self, record: ToolCallRecord) -> None: ...

    def list_tool_calls(self, session_id: str) -> list[ToolCallRecord]: ...


class AuditPort(Protocol):
    """Neutral audit event recording; hash-chain specifics stay in the impl."""

    def record(
        self,
        *,
        event_type: str,
        actor: str,
        surface: AuditSurface = "internal",
        status: AuditStatus = "applied",
        error_code: str = "",
        evidence: dict[str, Any] | None = None,
        session_id: str | None = None,
        approval_id: str | None = None,
        tool_call_id: str | None = None,
        document_id: str | None = None,
        base_revision: int | None = None,
    ) -> AuditRecordRef: ...


class DomainAdapter(Protocol):
    """Everything the runtime delegates to a drawing domain (R10-1 closure).

    The runtime never inspects ``intent``; it canonicalises through the domain,
    binds the hash, and delegates execution. Atomicity of the provenance
    close-out is the adapter's contractual postcondition (R10-4).
    """

    def document_context(self, *, document_id: str) -> DocumentContext: ...

    def canonicalize_intent(self, tool_name: str, intent: Any) -> Any: ...

    def preview_diff_hash(self, *, document_id: str, intent: Any) -> str: ...

    def approval_evidence(
        self,
        *,
        definition: ToolDefinitionView,
        canonical_intent: Any,
        diff_preview_hash: str,
    ) -> dict[str, Any]: ...

    def rejection_evidence(
        self,
        *,
        definition: ToolDefinitionView,
        intent: Any,
        error_code: str,
    ) -> dict[str, Any]: ...

    def closure_request(self, *, authorized: Any, intent: Any) -> ClosureRequest: ...

    def execute(
        self,
        *,
        authorized: Any,
        audit_context: dict[str, Any],
        closure: ClosureRequest,
        intent: Any,
    ) -> ExecutionOutcome: ...


__all__ = [
    "AuditPort",
    "AuditRecordRef",
    "ClosureRequest",
    "DocumentContext",
    "DomainAdapter",
    "ExecutionOutcome",
    "HarnessStorePort",
    "ToolDefinitionView",
    "ToolRegistryPort",
]
