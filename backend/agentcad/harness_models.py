"""Compatibility shim (M10-P2): harness state models moved to agentcad.runtime.models.

Every name is re-exported from the runtime package so all historical
``from agentcad.harness_models import ...`` call sites keep working unchanged.
"""

from __future__ import annotations

from .runtime.models import (
    AgentSession,
    AgentSessionAudit,
    AgentSessionCreateRequest,
    AgentSessionStatus,
    ApprovalStatus,
    ToolApproval,
    ToolApprovalCreateRequest,
    ToolApprovalResolveRequest,
    ToolCallRecord,
    ToolCallStatus,
)
from .runtime.primitives import StrictModel, utc_now

__all__ = [
    "StrictModel",
    "utc_now",
    "AgentSession",
    "AgentSessionAudit",
    "AgentSessionCreateRequest",
    "AgentSessionStatus",
    "ApprovalStatus",
    "ToolApproval",
    "ToolApprovalCreateRequest",
    "ToolApprovalResolveRequest",
    "ToolCallRecord",
    "ToolCallStatus",
]
