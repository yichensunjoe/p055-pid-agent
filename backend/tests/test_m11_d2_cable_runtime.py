"""M11-D2 hard locks (Gate CODE GO evidence list): Cable production wiring.

Machine evidence required by the Gate: revision-binding mismatch fail-closed,
Cable permission rejection audit, success/failure-closeout two-level fault
injection, bootstrap three-piece atomicity, five closures on success,
P&ID<->Cable zero pollution both ways, loaders fail-closed both ways,
global audit P->C->P->C ordinal continuity, cross-domain approval binding
non-reuse, and a 100-round interleave property test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcad.audit import AuditRecorder
from agentcad.cable_domain_adapter import (
    TOOL_ADD_CABLE_SEGMENT,
    CableAuditAdapter,
    CableDomainAdapter,
    CableToolRegistry,
)
from agentcad.cable_service import CableDocumentNotFoundError, CableService
from agentcad.harness_models import (
    ToolApprovalCreateRequest,
    ToolApprovalResolveRequest,
)
from agentcad.models import CreateDocumentRequest
from agentcad.runtime.harness import (
    AgentHarnessRuntime,
    ToolIntentMismatchError,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry

SEGMENT = {"id": "CBL-001", "from_node": "MCC-1", "to_node": "PMP-101", "gauge": "4mm2"}


def _composition(tmp_path: Path):
    store = SQLiteDocumentStore(tmp_path / "d2.db")
    service = DocumentService(store, SymbolRegistry())
    cable = CableService(store)
    audit_recorder = AuditRecorder(store=store, symbols=SymbolRegistry())
    runtime = AgentHarnessRuntime(
        store=store,
        registry=CableToolRegistry(),
        adapter=CableDomainAdapter(cable),
        audit=CableAuditAdapter(audit_recorder),
    )
    return store, service, cable, audit_recorder, runtime


def _intent(revision: int, segment: dict | None = None) -> dict:
    return {
        "expected_revision": revision,
        "segment": segment or dict(SEGMENT),
    }


def _approve(runtime, cable_doc_id: str, intent: dict):
    session = runtime.ensure_session(cable_doc_id, actor="cable-engineer")
    approval = runtime.request_approval(
        session.id,
        ToolApprovalCreateRequest(
            tool_name=TOOL_ADD_CABLE_SEGMENT,
            document_id=cable_doc_id,
            intent=intent,
            requested_by="cable-engineer",
        ),
    )
    runtime.resolve_approval(
        approval.id, ToolApprovalResolveRequest(approved=True, actor="operator")
    )
    return session, approval


def _authorized(runtime, session, approval, cable_doc_id: str, intent: dict, base: int):
    return runtime.authorize(
        session_id=session.id,
        tool_name=TOOL_ADD_CABLE_SEGMENT,
        document_id=cable_doc_id,
        intent=intent,
        approval_id=approval.id,
        base_revision=base,
    )


def test_success_write_five_closures(tmp_path: Path) -> None:
    store, _, cable, _, runtime = _composition(tmp_path)
    doc = cable.create_document("demo")
    session, approval = _approve(runtime, doc.document_id, _intent(0))
    authorized = _authorized(runtime, session, approval, doc.document_id, _intent(0), 0)

    outcome = runtime.apply_authorized(authorized, doc.document_id, _intent(0))

    assert outcome.result_revision == 1 and outcome.base_revision == 0
    loaded = cable.load(doc.document_id)
    assert loaded.document.revision == 1
    assert loaded.document.segments[0].id == "CBL-001"
    call = store.get_tool_call(authorized.record.id)
    assert call.status == "completed" and call.result_revision == 1
    assert store.get_tool_approval(approval.id).status == "consumed"
    closed = store.get_agent_session(session.id)
    assert closed.status == "completed" and closed.end_revision == 1
    audits = [
        record
        for record in store.all_audit_records()
        if record.event_type == "revision.created"
        and record.document_id == doc.document_id
        and record.status == "applied"
    ]
    assert len(audits) == 1
    assert audits[0].tool_call_id == call.id


def test_revision_binding_mismatch_fails_closed(tmp_path: Path) -> None:
    store, _, cable, _, runtime = _composition(tmp_path)
    doc = cable.create_document("demo")
    session, approval = _approve(runtime, doc.document_id, _intent(0))
    # The approval identity covers expected_revision via the canonical intent;
    # a caller that authorizes against a different base_revision is refused at
    # execute time by the frozen base-binding assertion (D2 §2A 五处同值链).
    authorized = _authorized(runtime, session, approval, doc.document_id, _intent(0), base=1)
    with pytest.raises(ValueError, match="approval bound base_revision"):
        runtime.apply_authorized(authorized, doc.document_id, _intent(0))
    assert cable.load(doc.document_id).document.revision == 0


def test_permission_rejection_audit_via_cable_audit_adapter(tmp_path: Path) -> None:
    store, _, cable, _, runtime = _composition(tmp_path)
    doc = cable.create_document("demo")
    with pytest.raises(Exception) as excinfo:  # ToolApprovalRequiredError
        runtime.authorize(
            session_id="",
            tool_name=TOOL_ADD_CABLE_SEGMENT,
            document_id=doc.document_id,
            intent=_intent(0),
            base_revision=0,
        )
    assert getattr(excinfo.value, "code", "") == "tool_approval_required"
    rejected = [
        record
        for record in store.all_audit_records()
        if record.event_type == "permission.rejected"
    ]
    assert len(rejected) == 1
    assert rejected[0].document_id == doc.document_id
    assert rejected[0].status == "rejected"


def test_failure_closeout_level_one(tmp_path: Path, monkeypatch) -> None:
    store, _, cable, _, runtime = _composition(tmp_path)
    doc = cable.create_document("demo")
    session, approval = _approve(runtime, doc.document_id, _intent(0))
    authorized = _authorized(runtime, session, approval, doc.document_id, _intent(0), 0)

    real = store.commit_cable_write

    def fail_audit_once(**kwargs):
        if kwargs["audit"].status == "applied":
            raise RuntimeError("injected success-audit failure")
        return real(**kwargs)

    monkeypatch.setattr(store, "commit_cable_write", fail_audit_once)
    with pytest.raises(RuntimeError, match="injected success-audit failure"):
        runtime.apply_authorized(authorized, doc.document_id, _intent(0))

    assert cable.load(doc.document_id).document.revision == 0  # success tx rolled back
    call = store.get_tool_call(authorized.record.id)
    assert call.status == "failed"
    assert store.get_tool_approval(approval.id).status == "approved"  # untouched
    assert store.get_agent_session(session.id).status == "failed"
    failures = [
        record
        for record in store.all_audit_records()
        if record.event_type == "revision.created"
        and record.status == "rejected"
        and record.tool_call_id == call.id
    ]
    assert len(failures) == 1


def test_failure_closeout_level_two_honesty(tmp_path: Path, monkeypatch) -> None:
    store, _, cable, _, runtime = _composition(tmp_path)
    doc = cable.create_document("demo")
    session, approval = _approve(runtime, doc.document_id, _intent(0))
    authorized = _authorized(runtime, session, approval, doc.document_id, _intent(0), 0)

    def always_fail(**kwargs):
        raise RuntimeError("audit storage down")

    monkeypatch.setattr(store, "commit_cable_write", always_fail)
    monkeypatch.setattr(store, "cable_failure_closeout", always_fail)
    with pytest.raises(RuntimeError, match="audit storage down"):
        runtime.apply_authorized(authorized, doc.document_id, _intent(0))

    assert cable.load(doc.document_id).document.revision == 0
    assert store.get_tool_call(authorized.record.id).status == "running"
    assert store.get_tool_approval(approval.id).status == "approved"
    assert store.get_agent_session(session.id).status == "active"


def test_bootstrap_three_piece_atomicity(tmp_path: Path) -> None:
    store, _, cable, _, _ = _composition(tmp_path)
    doc = cable.create_document("boot")
    with store._connect() as connection:  # noqa: SLF001
        registry = connection.execute(
            "SELECT domain FROM documents_registry WHERE document_id = ?",
            (doc.document_id,),
        ).fetchone()
        assert registry["domain"] == "cable"
        envelope = connection.execute(
            "SELECT revision, data_json FROM cable_documents WHERE document_id = ?",
            (doc.document_id,),
        ).fetchone()
        assert envelope["revision"] == 0
        created = [
            record
            for record in store.all_audit_records()
            if record.event_type == "document.created"
            and record.document_id == doc.document_id
        ]
        assert len(created) == 1 and created[0].status == "applied"


def test_loaders_fail_closed_both_ways(tmp_path: Path) -> None:
    store, service, cable, _, _ = _composition(tmp_path)
    pid_doc = service.create_document(CreateDocumentRequest(name="p", width=10, height=10))
    cable_doc = cable.create_document("c")
    with pytest.raises(CableDocumentNotFoundError):
        cable.load(pid_doc.id)  # P&ID id must not resolve on the cable plane
    from agentcad.service import DocumentNotFoundError

    with pytest.raises(DocumentNotFoundError):
        service.get_document(cable_doc.document_id)  # cable id not on P&ID plane


def test_cross_domain_pollution_and_approval_non_reuse(tmp_path: Path) -> None:
    store, service, cable, _, runtime = _composition(tmp_path)
    pid_doc = service.create_document(CreateDocumentRequest(name="p", width=10, height=10))
    cable_doc = cable.create_document("c")
    pid_before = service.get_document(pid_doc.id).model_copy()

    session, approval = _approve(runtime, cable_doc.document_id, _intent(0))
    authorized = _authorized(
        runtime, session, approval, cable_doc.document_id, _intent(0), 0
    )
    runtime.apply_authorized(authorized, cable_doc.document_id, _intent(0))

    # Cable write left the P&ID document untouched (revision + content).
    pid_after = service.get_document(pid_doc.id)
    assert pid_after.revision == pid_before.revision
    assert pid_after.model_dump() == pid_before.model_dump()
    # and the cable approval cannot authorize a P&ID document intent.
    with pytest.raises(ToolIntentMismatchError):
        runtime.authorize(
            session_id=session.id,
            tool_name=TOOL_ADD_CABLE_SEGMENT,
            document_id=pid_doc.id,
            intent=_intent(0),
            approval_id=approval.id,
            base_revision=0,
        )


def test_global_audit_interleaved_ordinals(tmp_path: Path) -> None:
    store, service, cable, recorder, runtime = _composition(tmp_path)
    service.create_document(CreateDocumentRequest(name="p", width=10, height=10))
    cable_doc = cable.create_document("c")
    session, approval = _approve(runtime, cable_doc.document_id, _intent(0))
    authorized = _authorized(
        runtime, session, approval, cable_doc.document_id, _intent(0), 0
    )
    runtime.apply_authorized(authorized, cable_doc.document_id, _intent(0))

    ordinals = [record.ordinal for record in store.all_audit_records()]
    assert ordinals == list(range(1, len(ordinals) + 1))
    assert recorder.verify_chain().ok
    domains = [
        record.document_id.startswith("cab_")
        for record in store.all_audit_records()
        if record.document_id
    ]
    assert any(domains) and not all(domains)  # P and C facts interleave one chain


def test_hundred_round_interleave_zero_pollution(tmp_path: Path) -> None:
    store, service, cable, _, runtime = _composition(tmp_path)
    pid_doc = service.create_document(CreateDocumentRequest(name="p", width=10, height=10))
    cable_doc = cable.create_document("c")
    from agentcad.models import AddLayerOperation, Layer, TransactionRequest

    for index in range(100):
        if index % 2 == 0:
            intent = {
                "expected_revision": index // 2,
                "segment": {
                    "id": f"CBL-{index:03d}",
                    "from_node": f"N{index}",
                    "to_node": f"M{index}",
                    "gauge": "1mm2",
                },
            }
            session, approval = _approve(runtime, cable_doc.document_id, intent)
            authorized = _authorized(
                runtime, session, approval, cable_doc.document_id, intent, index // 2
            )
            runtime.apply_authorized(authorized, cable_doc.document_id, intent)
        else:
            service.apply_transaction(
                pid_doc.id,
                TransactionRequest(
                    expected_revision=index // 2,
                    label=f"round {index}",
                    operations=[
                        AddLayerOperation(layer=Layer(id=f"l{index}", name=f"L{index}"))
                    ],
                ),
            )
    assert service.get_document(pid_doc.id).revision == 50
    assert cable.load(cable_doc.document_id).document.revision == 50
    assert len(cable.load(cable_doc.document_id).document.segments) == 50
