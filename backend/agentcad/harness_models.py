from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from .models import StrictModel, utc_now

AgentSessionStatus = Literal["active", "completed", "failed", "cancelled"]
ApprovalStatus = Literal["pending", "approved", "rejected", "consumed"]
ToolCallStatus = Literal["running", "completed", "rejected", "failed"]


def _harness_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class AgentSessionCreateRequest(StrictModel):
    document_id: str
    actor: str = Field(default="web-user", min_length=1, max_length=200)
    project_id: str | None = Field(default=None, max_length=200)
    provider: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentSession(StrictModel):
    id: str = Field(default_factory=lambda: _harness_id("session"))
    document_id: str
    actor: str
    project_id: str | None = None
    provider: str = ""
    model: str = ""
    start_revision: int = Field(ge=0)
    end_revision: int | None = Field(default=None, ge=0)
    status: AgentSessionStatus = "active"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolApprovalCreateRequest(StrictModel):
    tool_name: str
    document_id: str
    intent: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = Field(default="web-user", min_length=1, max_length=200)
    reason: str = Field(default="", max_length=2000)


class ToolApprovalResolveRequest(StrictModel):
    approved: bool
    actor: str = Field(default="web-user", min_length=1, max_length=200)
    note: str = Field(default="", max_length=2000)


class ToolApproval(StrictModel):
    id: str = Field(default_factory=lambda: _harness_id("approval"))
    session_id: str
    tool_name: str
    document_id: str
    intent_hash: str
    status: ApprovalStatus = "pending"
    requested_by: str
    resolved_by: str | None = None
    reason: str = ""
    note: str = ""
    # Engineering semantic diff hash of the reviewed intent (T0.5 evidence binding).
    diff_preview_hash: str = ""
    # Bounded, secret-free review evidence: diff counts, risk hints, target revisions.
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None
    consumed_at: datetime | None = None


class ToolCallRecord(StrictModel):
    id: str = Field(default_factory=lambda: _harness_id("call"))
    session_id: str
    tool_name: str
    document_id: str
    permission: Literal["allow", "ask", "deny"]
    risk: Literal["read", "draft_edit", "engineering_change", "critical_change", "release"]
    approval_id: str | None = None
    intent_hash: str
    base_revision: int | None = Field(default=None, ge=0)
    result_revision: int | None = Field(default=None, ge=0)
    status: ToolCallStatus = "running"
    error_code: str = ""
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentSessionAudit(StrictModel):
    session: AgentSession
    approvals: list[ToolApproval] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
