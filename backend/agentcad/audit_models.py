"""Audit / provenance data model (Charter §19 Engineering Change Record, Priority 0 / T0.5).

The audit layer is additive: ``DocumentService`` and ``TransactionRequest`` stay the
engineering write authority. What changes is that the evidence for one engineering
change is committed in the *same* SQLite transaction as the document revision, so a
half-written revision can never exist without its provenance.

Only hashes, counts, identifiers and stable codes are stored here. Prompts, model
context, document bodies and credentials never enter an audit row; that matches the
redaction guarantees already documented for diagnostics in
``docs/shared-deployment-security.md``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .harness_models import AgentSession, ToolApproval, ToolCallRecord
from .models import StrictModel, utc_now

AUDIT_CHAIN_SCHEMA = "pid-agent.audit-chain"
AUDIT_CHAIN_VERSION = 1
AUDIT_HASH_ALGORITHM = "sha256"
GENESIS_HASH = "0" * 64

AuditEventType = Literal[
    "revision.created",
    "document.created",
    "document.deleted",
    "document.imported",
    "project.settings_updated",
    "engineering.identity.changed",
    "engineering.index.rebuilt",
    "validation.rejected",
    # M4: validation and release readiness are *reads*. They are recorded as read/tool
    # evidence so an invocation can be audited, and deliberately not as revision events:
    # running a validator must never look like an edit to the drawing.
    "validation.completed",
    "release.readiness.assessed",
    "permission.rejected",
    "provider.egress.blocked",
]

AuditStatus = Literal["applied", "rejected", "failed"]
AuditSurface = Literal["rest", "mcp", "cli", "internal"]
DiffBinding = Literal["matched", "mismatched", "not_recorded"]
ValidationStatus = Literal["valid", "invalid", "not_run"]


class AuditContext(StrictModel):
    """Server-derived attribution for one engineering action.

    ``actor`` is never taken from a client-supplied ``TransactionRequest.source``;
    adapters derive it from the surface they own (web editor, MCP tool, semantic
    agent session, legacy compatibility router, CLI, internal service use).
    """

    actor: str = Field(min_length=1, max_length=200)
    surface: AuditSurface = "internal"
    tool_name: str = Field(default="", max_length=200)
    session_id: str | None = None
    approval_id: str | None = None
    tool_call_id: str | None = None
    project_id: str | None = None
    provider: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=500)
    request_id: str = Field(default="", max_length=120)
    intent_hash: str = Field(default="", max_length=128)
    diff_preview_hash: str = Field(default="", max_length=128)
    validation_status: ValidationStatus = "not_run"
    validation_evidence: dict[str, Any] = Field(default_factory=dict)
    label: str = Field(default="", max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """Return the compact, secret-free attribution block embedded in evidence."""
        return {
            "actor": self.actor,
            "surface": self.surface,
            "tool_name": self.tool_name,
            "session_id": self.session_id,
            "approval_id": self.approval_id,
            "tool_call_id": self.tool_call_id,
            "provider": self.provider,
            "model": self.model,
            "request_id": self.request_id,
        }


class AuditRecordDraft(StrictModel):
    """One audit fact before the chain hash is assigned."""

    event_type: AuditEventType
    actor: str = Field(min_length=1, max_length=200)
    surface: AuditSurface
    tool_name: str = Field(default="", max_length=200)
    status: AuditStatus = "applied"
    document_id: str | None = Field(default=None, max_length=160)
    project_id: str | None = Field(default=None, max_length=200)
    base_revision: int | None = Field(default=None, ge=0)
    result_revision: int | None = Field(default=None, ge=0)
    session_id: str | None = None
    approval_id: str | None = None
    tool_call_id: str | None = None
    provider: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=500)
    label: str = Field(default="", max_length=500)
    intent_hash: str = Field(default="", max_length=128)
    diff_hash: str = Field(default="", max_length=128)
    diff_preview_hash: str = Field(default="", max_length=128)
    diff_binding: DiffBinding = "not_recorded"
    validation_status: ValidationStatus = "not_run"
    validation_hash: str = Field(default="", max_length=128)
    evidence: dict[str, Any] = Field(default_factory=dict)
    error_code: str = Field(default="", max_length=200)


class AuditRecord(StrictModel):
    """A persisted, hash-chained audit fact."""

    record_id: str
    ordinal: int = Field(ge=1)
    recorded_at: datetime = Field(default_factory=utc_now)
    event_type: AuditEventType
    actor: str
    surface: AuditSurface
    tool_name: str = ""
    status: AuditStatus = "applied"
    document_id: str | None = None
    project_id: str | None = None
    base_revision: int | None = Field(default=None, ge=0)
    result_revision: int | None = Field(default=None, ge=0)
    session_id: str | None = None
    approval_id: str | None = None
    tool_call_id: str | None = None
    provider: str = ""
    model: str = ""
    label: str = ""
    intent_hash: str = ""
    diff_hash: str = ""
    diff_preview_hash: str = ""
    diff_binding: DiffBinding = "not_recorded"
    validation_status: ValidationStatus = "not_run"
    validation_hash: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    error_code: str = ""
    prev_hash: str = GENESIS_HASH
    record_hash: str = ""
    chain_schema: str = AUDIT_CHAIN_SCHEMA
    chain_version: int = AUDIT_CHAIN_VERSION


class AuditDivergence(StrictModel):
    ordinal: int = Field(ge=1)
    record_id: str = ""
    reason: str
    expected_hash: str = ""
    actual_hash: str = ""


class AuditVerification(StrictModel):
    schema_name: Literal["pid-agent.audit-verification"] = Field(
        default="pid-agent.audit-verification", alias="schema"
    )
    version: Literal[1] = 1
    ok: bool
    hash_algorithm: str = AUDIT_HASH_ALGORITHM
    chain_schema: str = AUDIT_CHAIN_SCHEMA
    chain_version: int = AUDIT_CHAIN_VERSION
    record_count: int = Field(default=0, ge=0)
    verified_at: datetime = Field(default_factory=utc_now)
    first_divergence: AuditDivergence | None = None
    database_instance_id: str = ""


class RevisionEvidence(StrictModel):
    """Everything needed to review one engineering change without trusting the store."""

    schema_name: Literal["pid-agent.revision-evidence"] = Field(
        default="pid-agent.revision-evidence", alias="schema"
    )
    version: Literal[1] = 1
    document_id: str
    revision: int = Field(ge=0)
    audit_record: AuditRecord | None = None
    history_details: dict[str, Any] = Field(default_factory=dict)
    semantic_diff: dict[str, Any] | None = None
    session: AgentSession | None = None
    approval: ToolApproval | None = None
    tool_call: ToolCallRecord | None = None
    recomputed_diff_hash: str = ""
    recomputed_validation_hash: str = ""
    diff_hash_matches: bool | None = None
    validation_hash_matches: bool | None = None
    chain_verified: bool = False


class ProvenanceState(StrictModel):
    """Harness-side intent to close out provenance in the same write.

    Passing this to ``DocumentService.apply_transaction`` makes tool call
    completion, approval consumption and session closure happen inside the same
    SQLite transaction as the document revision, instead of being best-effort
    follow-up updates.
    """

    tool_call_id: str | None = None
    tool_call_metadata: dict[str, Any] = Field(default_factory=dict)
    consume_approval: bool = False
    close_session: bool = False


class ProvenanceBundle(StrictModel):
    """One atomic provenance write attached to a document mutation."""

    history_details: dict[str, Any] | None = None
    audit: AuditRecordDraft | None = None
    tool_call: ToolCallRecord | None = None
    approval: ToolApproval | None = None
    session: AgentSession | None = None
