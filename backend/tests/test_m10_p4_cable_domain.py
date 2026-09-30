"""M10-P4 (Gate CODE GO): the named minimal second domain — cable schematic.

Proof object: a real (if tiny) second engineering-drawing domain running the
SAME runtime core end-to-end, with its own domain model, its own in-memory
repository, its own invariants and its own adapter — zero P&ID imports, zero
persistence, schema v13 untouched. Tests-only: no production file changes.

Frozen locks (Gate P4 CODE GO):
* CableSegmentRequest(id, from_node, to_node, gauge) on runtime primitives only;
* CableRepository is a pure dict holding revision + segments-by-id — executing
  add_cable_segment leaves a READABLE segment and bumps revision 0 -> 1;
* two domain invariants: from_node != to_node, segment id unique;
* CableDomainAdapter implements all seven ports independently (never wrapping
  PidDomainAdapter); evidence carries cable segment identity;
* subprocess isolation asserted before AND after the whole workflow.
"""

from __future__ import annotations

import json
import subprocess
import sys

PROBE = r'''
import hashlib
import json
import sys

pre = sorted(
    m for m in sys.modules
    if m.startswith("agentcad.") and not m.startswith("agentcad.runtime")
)
assert not pre, f"import-time leak: {pre}"

# ---- runtime core + neutral ports only (no P&ID module is imported) ----
from agentcad.runtime.harness import (
    AgentHarnessRuntime,
    ToolApprovalRequiredError,
)
from agentcad.runtime.models import (
    ToolApprovalCreateRequest,
    ToolApprovalResolveRequest,
)
from agentcad.runtime.ports import (
    AuditEvent,
    AuditRecordRef,
    ClosureRequest,
    DocumentContext,
    ExecutionOutcome,
    ToolDefinitionView,
)
from agentcad.runtime.primitives import StrictModel


# ---- the cable domain's own minimal model (runtime primitives only) ----
class CableSegmentRequest(StrictModel):
    id: str
    from_node: str
    to_node: str
    gauge: str = "2.5mm2"


class CableSegment(StrictModel):
    id: str
    from_node: str
    to_node: str
    gauge: str


class CableRepository:
    """Pure in-memory dict-backed store (Gate-frozen: no persistence)."""

    def __init__(self):
        self.revision = 0
        self.segments: dict[str, CableSegment] = {}


class CableDomainAdapter:
    """Independent seven-port DomainAdapter for the cable schematic domain."""

    def __init__(self, repository: CableRepository, store, audit):
        self.repository = repository
        self._store = store
        self._audit = audit
        self.executes = 0

    def document_context(self, *, document_id: str) -> DocumentContext:
        return DocumentContext(document_id=document_id, revision=self.repository.revision)

    def canonicalize_intent(self, tool_name: str, intent):
        # Domain invariant 1: from_node != to_node, enforced at intent time.
        request = CableSegmentRequest.model_validate(intent)
        if request.from_node == request.to_node:
            raise ValueError("cable segment requires from_node != to_node")
        return {"cable_segment": request.model_dump(mode="json")}

    def preview_diff_hash(self, *, document_id: str, intent) -> str:
        canonical = self.canonicalize_intent("add_cable_segment", intent)
        encoded = json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def approval_evidence(self, *, definition, canonical_intent, diff_preview_hash):
        segment = (canonical_intent or {}).get("cable_segment", {})
        return {
            "tool": definition.name,
            "cable_segment_id": segment.get("id", ""),
            "cable_route": f"{segment.get('from_node', '')}->{segment.get('to_node', '')}",
            "diff_preview_hash": diff_preview_hash,
        }

    def rejection_evidence(self, *, definition, intent, error_code):
        segment_id = ""
        if isinstance(intent, dict):
            segment_id = str(intent.get("id", ""))
        return {
            "tool_permission": definition.permission,
            "cable_segment_id": segment_id,
            "authorized": False,
        }

    def closure_request(self, *, authorized, intent) -> ClosureRequest:
        return ClosureRequest(
            tool_call_id=authorized.record.id,
            consume_approval=authorized.approval is not None,
            close_session=True,
            metadata={"cable_segment_id": CableSegmentRequest.model_validate(intent).id},
        )

    def _fail_closeout(self, authorized, error_code: str) -> None:
        """Failure provenance contract (same semantics the P&ID domain freezes):
        the failed tool call, the untouched approval and the failed session close
        out together with exactly one rejected audit fact — no half-state."""
        failed = authorized.record.model_copy(
            update={"status": "failed", "error_code": error_code}
        )
        self._store.update_tool_call(failed)
        session = self._store.get_agent_session(authorized.session.id)
        self._store.update_agent_session(
            session.model_copy(update={"status": "failed"})
        )
        self._audit.record(
            AuditEvent(
                event_type="revision.created",
                actor=authorized.session.actor,
                tool_name=authorized.definition.name,
                status="rejected",
                error_code=error_code,
                document_id=authorized.record.document_id,
                base_revision=authorized.record.base_revision,
                evidence={"cable_segment_id": authorized.record.metadata.get("cable_segment_id", "")},
            )
        )

    def execute(self, *, authorized, audit_event, closure, intent) -> ExecutionOutcome:
        self.executes += 1
        request = CableSegmentRequest.model_validate(intent)
        # Domain invariant 2: segment id unique within the repository.
        if request.id in self.repository.segments:
            error_code = "duplicate_cable_segment"
            self._fail_closeout(authorized, error_code)
            raise ValueError(f"duplicate cable segment id: {request.id}")
        revision = self.repository.revision + 1
        self.repository.revision = revision
        self.repository.segments[request.id] = CableSegment(
            id=request.id,
            from_node=request.from_node,
            to_node=request.to_node,
            gauge=request.gauge,
        )
        completed = authorized.record.model_copy(
            update={"status": "completed", "result_revision": revision}
        )
        self._store.update_tool_call(completed)
        if authorized.approval is not None:
            self._store.update_tool_approval(
                authorized.approval.model_copy(update={"status": "consumed"})
            )
        session = self._store.get_agent_session(authorized.session.id)
        self._store.update_agent_session(
            session.model_copy(update={"status": "completed", "end_revision": revision})
        )
        self._audit.record(
            AuditEvent(
                event_type="revision.created",
                actor=authorized.session.actor,
                tool_name=authorized.definition.name,
                status="applied",
                document_id=audit_event.document_id,
                base_revision=audit_event.base_revision,
                result_revision=revision,
                evidence={"cable_segment_id": request.id},
            )
        )
        return ExecutionOutcome(
            document_id=audit_event.document_id,
            base_revision=audit_event.base_revision,
            result_revision=revision,
            payload={"cable_segment_id": request.id},
        )


class FakeStore:
    def __init__(self):
        self.sessions = {}
        self.approvals = {}
        self.calls = {}

    def create_agent_session(self, session):
        self.sessions[session.id] = session

    def get_agent_session(self, session_id):
        return self.sessions.get(session_id)

    def update_agent_session(self, session):
        self.sessions[session.id] = session

    def create_tool_approval(self, approval):
        self.approvals[approval.id] = approval

    def get_tool_approval(self, approval_id):
        return self.approvals.get(approval_id)

    def update_tool_approval(self, approval):
        self.approvals[approval.id] = approval

    def list_tool_approvals(self, session_id):
        return [a for a in self.approvals.values() if a.session_id == session_id]

    def create_tool_call(self, record):
        self.calls[record.id] = record

    def get_tool_call(self, tool_call_id):
        return self.calls.get(tool_call_id)

    def update_tool_call(self, record):
        self.calls[record.id] = record

    def list_tool_calls(self, session_id):
        return [c for c in self.calls.values() if c.session_id == session_id]


class FakeAudit:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)
        ordinal = len(self.events)
        return AuditRecordRef(
            record_id=f"cable_audit_{ordinal}",
            ordinal=ordinal,
            record_hash=hashlib.sha256(str(ordinal).encode()).hexdigest(),
        )


class CableRegistry:
    VIEW = ToolDefinitionView(
        name="add_cable_segment",
        description="Add one cable segment to a cable schematic (M10 proof slice)",
        permission="ask",
        risk="engineering_change",
        audit_event="tool.cable.add_segment",
    )

    def require(self, name):
        if name != self.VIEW.name:
            raise KeyError(name)
        return self.VIEW


repository = CableRepository()
store = FakeStore()
audit = FakeAudit()
runtime = AgentHarnessRuntime(
    store=store,
    registry=CableRegistry(),
    adapter=CableDomainAdapter(repository, store, audit),
    audit=audit,
)

SEGMENT = {"id": "CBL-001", "from_node": "MCC-1", "to_node": "PMP-101", "gauge": "4mm2"}

# ---- refusal path: ask without approval ----
try:
    runtime.authorize(
        session_id="",
        tool_name="add_cable_segment",
        document_id="cable_doc",
        intent=SEGMENT,
        base_revision=0,
    )
    raise SystemExit("expected ToolApprovalRequiredError")
except ToolApprovalRequiredError:
    pass
assert len([c for c in store.calls.values() if c.status == "rejected"]) == 1
assert len([e for e in audit.events if e.event_type == "permission.rejected"]) == 1
assert runtime.adapter.executes == 0
assert repository.revision == 0 and repository.segments == {}

# ---- success chain: five closures + readable segment + revision 0 -> 1 ----
session = runtime.ensure_session("cable_doc", actor="cable-engineer")
approval = runtime.request_approval(
    session.id,
    ToolApprovalCreateRequest(
        tool_name="add_cable_segment", document_id="cable_doc", intent=SEGMENT
    ),
)
# Approval evidence carries the cable segment identity (Gate lock).
assert approval.evidence.get("cable_segment_id") == "CBL-001"
runtime.resolve_approval(
    approval.id, ToolApprovalResolveRequest(approved=True, actor="operator")
)
authorized = runtime.authorize(
    session_id=session.id,
    tool_name="add_cable_segment",
    document_id="cable_doc",
    intent=SEGMENT,
    approval_id=approval.id,
    base_revision=0,
)
outcome = runtime.apply_authorized(authorized, "cable_doc", SEGMENT)
assert runtime.adapter.executes == 1
assert outcome.result_revision == 1 and outcome.base_revision == 0
segment = repository.segments.get("CBL-001")
assert segment is not None and segment.from_node == "MCC-1" and segment.to_node == "PMP-101"
assert segment.gauge == "4mm2"
call = store.get_tool_call(authorized.record.id)
assert call.status == "completed" and call.result_revision == 1
assert store.get_tool_approval(approval.id).status == "consumed"
closed = store.get_agent_session(session.id)
assert closed.status == "completed" and closed.end_revision == 1
applied = [e for e in audit.events if e.event_type == "revision.created" and e.status == "applied"]
assert len(applied) == 1 and applied[0].evidence.get("cable_segment_id") == "CBL-001"

# ---- domain invariants (Gate lock) ----
try:
    runtime.adapter.canonicalize_intent(
        "add_cable_segment", {"id": "CBL-X", "from_node": "A", "to_node": "A"}
    )
    raise SystemExit("expected from_node != to_node invariant")
except ValueError:
    pass
fresh_session = runtime.ensure_session("cable_doc", actor="cable-engineer")
fresh_approval = runtime.request_approval(
    fresh_session.id,
    ToolApprovalCreateRequest(
        tool_name="add_cable_segment", document_id="cable_doc", intent=SEGMENT
    ),
)
runtime.resolve_approval(
    fresh_approval.id, ToolApprovalResolveRequest(approved=True, actor="operator")
)
fresh_authorized = runtime.authorize(
    session_id=fresh_session.id,
    tool_name="add_cable_segment",
    document_id="cable_doc",
    intent=SEGMENT,
    approval_id=fresh_approval.id,
    base_revision=1,
)
try:
    runtime.apply_authorized(fresh_authorized, "cable_doc", SEGMENT)
    raise SystemExit("expected duplicate id invariant")
except ValueError:
    pass
assert repository.revision == 1 and len(repository.segments) == 1
# R72-3: the failure closed provenance exactly like the P&ID domain — no
# running/approved/active half-state, exactly one rejected failure audit.
fresh_call = store.get_tool_call(fresh_authorized.record.id)
assert fresh_call.status == "failed" and fresh_call.error_code == "duplicate_cable_segment"
assert store.get_tool_approval(fresh_approval.id).status == "approved"
assert store.get_agent_session(fresh_session.id).status == "failed"
fresh_failures = [
    e for e in audit.events
    if e.event_type == "revision.created" and e.status == "rejected"
    and e.error_code == "duplicate_cable_segment"
]
assert len(fresh_failures) == 1
assert len([e for e in audit.events if e.status == "applied"]) == 1

post = sorted(
    m for m in sys.modules
    if m.startswith("agentcad.") and not m.startswith("agentcad.runtime")
)
assert not post, f"post-workflow leak: {post}"
print(json.dumps({
    "repository_revision": repository.revision,
    "segments": sorted(repository.segments),
    "audit_events": len(audit.events),
    "verdict": "cable-domain-ok",
}))
'''


def test_cable_domain_probe() -> None:
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["verdict"] == "cable-domain-ok"
    assert payload["repository_revision"] == 1
    assert payload["segments"] == ["CBL-001"]
    assert payload["audit_events"] == 3  # permission.rejected + applied + duplicate failure
