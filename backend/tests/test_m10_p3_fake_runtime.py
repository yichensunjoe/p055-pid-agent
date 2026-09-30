"""M10-P3 hard locks (Gate P3 CODE GO): runtime reuse without any P&ID object.

* subprocess probe: recursively import the whole agentcad.runtime package,
  then run a FULL fake-domain workflow, asserting before AND after that no
  non-runtime agentcad module entered sys.modules (P3-1/P3-2/P3-7);
* the workflow itself (P3-4/P3-5/P3-6) runs on in-memory fakes only —
  FakeStore implements the complete 11-method HarnessStorePort (P3-3), the
  fake domain implements all seven DomainAdapter ports (never borrowing the
  P&ID adapter), and the success/refusal/mismatch chains assert the five
  closures, refusal-is-evidence and the approval four-tuple binding.
"""

from __future__ import annotations

import json
import pkgutil
import subprocess
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "agentcad"

PROBE = r'''
import hashlib
import importlib
import json
import pkgutil
import sys

import agentcad.runtime

# Recursively import every module of the runtime package.
runtime_pkg = agentcad.runtime
for module_info in pkgutil.walk_packages(runtime_pkg.__path__, runtime_pkg.__name__ + "."):
    importlib.import_module(module_info.name)


def leaked():
    return sorted(
        m
        for m in sys.modules
        if m.startswith("agentcad.") and not m.startswith("agentcad.runtime")
    )


pre = leaked()
assert not pre, f"import-time leak: {pre}"

# ---- in-memory fakes (no P&ID object is ever constructed) ----
from agentcad.runtime.harness import (
    AgentHarnessRuntime,
    ToolApprovalRequiredError,
    ToolIntentMismatchError,
    tool_intent_hash,
)
from agentcad.runtime.models import (
    AgentSession,
    ToolApproval,
    ToolApprovalCreateRequest,
    ToolApprovalResolveRequest,
    ToolCallRecord,
)
from agentcad.runtime.ports import (
    AuditEvent,
    AuditRecordRef,
    ClosureRequest,
    DocumentContext,
    ExecutionOutcome,
    ToolDefinitionView,
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
            record_id=f"audit_{ordinal}",
            ordinal=ordinal,
            record_hash=hashlib.sha256(str(ordinal).encode()).hexdigest(),
        )


class FakeDomain:
    """A second drawing domain: an in-memory revision counter per document."""

    def __init__(self, store, audit):
        self.revisions = {}
        self.executes = 0
        self._store = store
        self._audit = audit

    def document_context(self, *, document_id):
        return DocumentContext(document_id=document_id, revision=self.revisions.get(document_id, 0))

    def canonicalize_intent(self, tool_name, intent):
        return intent

    def preview_diff_hash(self, *, document_id, intent):
        return "fake-diff"

    def approval_evidence(self, *, definition, canonical_intent, diff_preview_hash):
        return {"tool": definition.name, "fake": True}

    def rejection_evidence(self, *, definition, intent, error_code):
        return {"tool_permission": definition.permission, "authorized": False}

    def closure_request(self, *, authorized, intent):
        return ClosureRequest(
            tool_call_id=authorized.record.id,
            consume_approval=authorized.approval is not None,
            close_session=True,
            metadata={"fake": True},
        )

    def execute(self, *, authorized, audit_event, closure, intent):
        # One atomic governed write for the fake domain: revision, audit fact and
        # the harness close-out land together (all in-memory, but inseparable).
        self.executes += 1
        document_id = audit_event.document_id
        revision = self.revisions.get(document_id, 0) + 1
        self.revisions[document_id] = revision
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
                document_id=document_id,
                base_revision=audit_event.base_revision,
                result_revision=revision,
            )
        )
        return ExecutionOutcome(
            document_id=document_id,
            base_revision=audit_event.base_revision,
            result_revision=revision,
            payload={"fake": True},
        )


class FakeRegistry:
    def __init__(self):
        self.view = ToolDefinitionView(
            name="fake_write",
            description="Fake second-domain governed write",
            permission="ask",
            risk="engineering_change",
            audit_event="tool.fake_write",
        )

    def require(self, name):
        if name != self.view.name:
            raise KeyError(name)
        return self.view


store = FakeStore()
audit = FakeAudit()
domain = FakeDomain(store, audit)
runtime = AgentHarnessRuntime(
    store=store,
    registry=FakeRegistry(),
    adapter=domain,
    audit=audit,
)

# ---- refusal path (P3-5): ask without approval ----
try:
    runtime.authorize(
        session_id="",
        tool_name="fake_write",
        document_id="doc_fake",
        intent={"op": "draw"},
        base_revision=0,
    )
    raise SystemExit("expected ToolApprovalRequiredError")
except ToolApprovalRequiredError:
    pass
rejected_calls = [c for c in store.calls.values() if c.status == "rejected"]
assert len(rejected_calls) == 1
assert len([e for e in audit.events if e.event_type == "permission.rejected"]) == 1
assert domain.executes == 0
assert domain.revisions.get("doc_fake", 0) == 0

# ---- approval binding mismatch (P3-6): approve intent A, authorize intent B ----
session = runtime.ensure_session("doc_fake", actor="engineer")
intent_a = {"op": "draw", "what": "A"}
approval = runtime.request_approval(
    session.id,
    ToolApprovalCreateRequest(
        tool_name="fake_write", document_id="doc_fake", intent=intent_a
    ),
)
runtime.resolve_approval(
    approval.id, ToolApprovalResolveRequest(approved=True, actor="operator")
)
intent_b = {"op": "draw", "what": "B"}
try:
    runtime.authorize(
        session_id=session.id,
        tool_name="fake_write",
        document_id="doc_fake",
        intent=intent_b,
        approval_id=approval.id,
        base_revision=0,
    )
    raise SystemExit("expected ToolIntentMismatchError")
except ToolIntentMismatchError:
    pass

# ---- success chain (P3-4): exact intent, full five closures ----
authorized = runtime.authorize(
    session_id=session.id,
    tool_name="fake_write",
    document_id="doc_fake",
    intent=intent_a,
    approval_id=approval.id,
    base_revision=0,
)
outcome = runtime.apply_authorized(authorized, "doc_fake", intent_a)
assert outcome.result_revision == 1 and outcome.base_revision == 0
assert domain.revisions["doc_fake"] == 1
call = store.get_tool_call(authorized.record.id)
assert call.status == "completed" and call.result_revision == 1
assert store.get_tool_approval(approval.id).status == "consumed"
closed = store.get_agent_session(session.id)
assert closed.status == "completed" and closed.end_revision == 1
applied = [e for e in audit.events if e.event_type == "revision.created" and e.status == "applied"]
assert len(applied) == 1

post = leaked()
assert not post, f"post-workflow leak: {post}"
print(json.dumps({
    "revisions": domain.revisions,
    "executes": domain.executes,
    "audit_events": len(audit.events),
    "rejected_calls": len(rejected_calls),
    "verdict": "fake-runtime-ok",
}))
'''


def test_runtime_fake_domain_probe() -> None:
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["verdict"] == "fake-runtime-ok"
    assert payload["revisions"] == {"doc_fake": 1}
    assert payload["executes"] == 1
    # Two refusal audits (no-approval + intent mismatch) and one applied fact.
    assert payload["audit_events"] == 3
    assert payload["rejected_calls"] == 1  # counted before the mismatch attempt


def test_runtime_package_modules_discovered() -> None:
    """Guard against a silently-empty runtime package in the probe."""
    import agentcad.runtime

    names = {
        module_info.name
        for module_info in pkgutil.walk_packages(
            agentcad.runtime.__path__, agentcad.runtime.__name__ + "."
        )
    }
    assert "agentcad.runtime.harness" in names
    assert "agentcad.runtime.ports" in names
    assert "agentcad.runtime.models" in names
    assert "agentcad.runtime.primitives" in names
