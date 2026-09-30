"""M10-P2 hard locks (Gate P2 CODE GO): parity, atomicity, compatibility.

* intent-hash parity: the four governed tools plus pass-through hash exactly as
  pre-M10 (golden values captured from main@5150758), and an intent that omits
  Pydantic-filled defaults hashes identically to the explicit one;
* rejection-audit parity (P2-4): the permission.rejected audit record carries
  every pre-M10 field (event type/status/actor/surface/tool/session/approval/
  tool-call/provider/model/intent hash/label/permission+risk evidence/error code);
* five-closure success (R10-4): revision + audit + tool-call completed +
  approval consumed + session completed, all from one governed write;
* mid-commit failure (P2-6): revision unchanged, tool_call failed (no running
  residue), approval still approved (not consumed), session failed, exactly one
  revision.created failure record with status rejected and the original error
  code, and no compensating second audit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

GOLDEN_HASHES = {
    "apply_auto_layout": "038aabdbed69691a74bf2358db7391937b2ffcddd0f4fa7e5ae898084cf18b24",
    "apply_compiled_agent_transaction": "f3c982a8e76da87517f248790891abc924c9e4b6128af039cc45574132000a9b",
    "apply_deterministic_drafting": "18cb3f46e00a40d86ea2ebadd1a3c80499272a4bfbef9317f6257b68c063cf55",
    "passthrough_tool": "f849c2a121debb52f5a55b5dd6666c64aa8f9ac52dbee2bc669c8cb2afbc45ec",
}

GOLDEN_TRANSACTION = {
    "expected_revision": 0,
    "label": "golden-tx",
    "operations": [
        {
            "op": "add_element",
            "element": {
                "type": "text",
                "id": "t_golden",
                "position": {"x": 10, "y": 20},
                "text": "golden",
            },
        }
    ],
}

GOLDEN_REJECTION_PROJECTION = {
    "base_revision": 0,
    "actor": "system",
    "error_code": "tool_approval_required",
    "event_type": "permission.rejected",
    "evidence_authorized": False,
    "evidence_keys": [
        "attribution",
        "authorized",
        "intent_hash",
        "metadata",
        "tool_permission",
        "tool_risk",
        "validation",
    ],
    "evidence_permission": "ask",
    "evidence_risk": "engineering_change",
    "has_session_id": True,
    "has_tool_call_id": True,
    "label": "Apply an already compiled and validated low-level Agent transaction "
    "through the atomic DocumentService write boundary.",
    "metadata_keys": ["permission", "risk"],
    "model": "",
    "provider": "",
    "status": "rejected",
    "surface": "rest",
    "tool_name": "apply_compiled_agent_transaction",
}


def _service(tmp_path: Path):
    from agentcad.service import DocumentService
    from agentcad.store import SQLiteDocumentStore
    from agentcad.symbols import SymbolRegistry

    return DocumentService(SQLiteDocumentStore(tmp_path / "p2.db"), SymbolRegistry())


def _seed(service, tmp_path: Path) -> str:
    from agentcad.models import CreateDocumentRequest

    return service.create_document(CreateDocumentRequest(name="p2", width=100, height=100)).id


def test_intent_hash_golden_parity_and_default_invariance() -> None:
    from agentcad.harness import canonicalize_tool_intent, tool_intent_hash

    cases = {
        "apply_compiled_agent_transaction": {"transaction": GOLDEN_TRANSACTION},
        "apply_auto_layout": {"options": {}},
        "apply_deterministic_drafting": {"request": {}},
        "passthrough_tool": {"anything": {"b": 1, "a": [True, None, 2]}},
    }
    for name, intent in cases.items():
        canonical = canonicalize_tool_intent(name, intent)
        assert tool_intent_hash(name, "doc_golden", canonical) == GOLDEN_HASHES[name], name
    # Omitted Pydantic defaults must not drift the approval identity.
    canonical_full = canonicalize_tool_intent(
        "apply_compiled_agent_transaction", {"transaction": GOLDEN_TRANSACTION}
    )
    explicit = {
        "expected_revision": 0,
        "label": "golden-tx",
        "operations": GOLDEN_TRANSACTION["operations"],
    }
    canonical_explicit = canonicalize_tool_intent(
        "apply_compiled_agent_transaction",
        {"transaction": explicit, "dry_run": False, "source": "web"},
    )
    assert tool_intent_hash("t", "d", canonical_full) == tool_intent_hash(
        "t", "d", canonical_explicit
    )


def test_rejection_audit_parity(tmp_path: Path) -> None:
    from agentcad.harness import AgentHarnessService

    service = _service(tmp_path)
    document_id = _seed(service, tmp_path)
    harness = AgentHarnessService(service, service.store)
    with pytest.raises(Exception) as excinfo:  # ToolApprovalRequiredError
        harness.authorize(
            session_id="",
            tool_name="apply_compiled_agent_transaction",
            document_id=document_id,
            intent={"transaction": GOLDEN_TRANSACTION},
            base_revision=0,
        )
    assert getattr(excinfo.value, "code", "") == "tool_approval_required"

    records = [
        record
        for record in service.store.all_audit_records()
        if record.event_type == "permission.rejected"
    ]
    assert len(records) == 1
    record = records[0]
    projection = {
        "base_revision": record.base_revision,
        "actor": record.actor,
        "error_code": record.error_code,
        "event_type": record.event_type,
        "evidence_authorized": record.evidence.get("authorized"),
        "evidence_keys": sorted(record.evidence.keys()),
        "evidence_permission": record.evidence.get("tool_permission"),
        "evidence_risk": record.evidence.get("tool_risk"),
        "has_session_id": bool(record.session_id),
        "has_tool_call_id": bool(record.tool_call_id),
        "label": record.label,
        "metadata_keys": sorted(record.evidence.get("metadata", {}).keys()),
        "model": record.model,
        "provider": record.provider,
        "status": record.status,
        "surface": record.surface,
        "tool_name": record.tool_name,
    }
    assert projection == GOLDEN_REJECTION_PROJECTION


def _approved_call(harness, service, document_id: str):
    from agentcad.harness_models import ToolApprovalCreateRequest, ToolApprovalResolveRequest

    session = harness.ensure_session(document_id, actor="engineer")
    approval = harness.request_approval(
        session.id,
        ToolApprovalCreateRequest(
            tool_name="apply_compiled_agent_transaction",
            document_id=document_id,
            intent={"transaction": GOLDEN_TRANSACTION},
            requested_by="engineer",
        ),
    )
    harness.resolve_approval(
        approval.id, ToolApprovalResolveRequest(approved=True, actor="operator")
    )
    authorized = harness.authorize(
        session_id=session.id,
        tool_name="apply_compiled_agent_transaction",
        document_id=document_id,
        intent={"transaction": GOLDEN_TRANSACTION},
        approval_id=approval.id,
        base_revision=0,
    )
    return session, approval, authorized


def test_five_closure_success_postcondition(tmp_path: Path) -> None:
    from agentcad.harness import AgentHarnessService

    service = _service(tmp_path)
    document_id = _seed(service, tmp_path)
    harness = AgentHarnessService(service, service.store)
    session, approval, authorized = _approved_call(harness, service, document_id)

    result = harness.apply_authorized(authorized, document_id, GOLDEN_TRANSACTION)
    # P2-3: the facade still returns the historical TransactionResult.
    assert result.document.revision == 1
    assert result.applied_operations == 1

    store = service.store
    call = store.get_tool_call(authorized.record.id)
    assert call.status == "completed" and call.result_revision == 1
    stored_approval = store.get_tool_approval(approval.id)
    assert stored_approval.status == "consumed"
    stored_session = store.get_agent_session(session.id)
    assert stored_session.status == "completed" and stored_session.end_revision == 1
    audit_records = [
        record
        for record in store.all_audit_records()
        if record.event_type == "revision.created" and record.tool_call_id == call.id
    ]
    assert len(audit_records) == 1 and audit_records[0].status == "applied"


def test_mid_commit_failure_locks_existing_states(tmp_path: Path, monkeypatch) -> None:
    from agentcad.harness import AgentHarnessService
    from agentcad.service import RevisionConflictError

    service = _service(tmp_path)
    document_id = _seed(service, tmp_path)
    harness = AgentHarnessService(service, service.store)
    session, approval, authorized = _approved_call(harness, service, document_id)

    def racing_apply(document, transaction, **kwargs):
        raise RevisionConflictError("revision moved while committing")

    monkeypatch.setattr(service, "apply_transaction", racing_apply)
    with pytest.raises(RevisionConflictError):
        harness.apply_authorized(authorized, document_id, GOLDEN_TRANSACTION)

    store = service.store
    # P2-6: the existing failure states, not redefined ones.
    assert service.get_document(document_id).revision == 0
    call = store.get_tool_call(authorized.record.id)
    assert call.status == "failed" and call.error_code
    stored_approval = store.get_tool_approval(approval.id)
    assert stored_approval.status == "approved"
    stored_session = store.get_agent_session(session.id)
    assert stored_session.status == "failed"
    failures = [
        record
        for record in store.all_audit_records()
        if record.event_type == "revision.created" and record.tool_call_id == call.id
    ]
    assert len(failures) == 1
    assert failures[0].status == "rejected"
    # Parity with pre-M10: RevisionConflictError carries no .code, so the
    # recorded error_code is the class name (existing behavior, not redefined).
    assert failures[0].error_code == "RevisionConflictError"
