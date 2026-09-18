"""T0.5 audit / provenance tests.

These tests are deliberately adversarial about attribution: they assert that no
surface (REST v2, REST v1 legacy, MCP, Harness, documents router, CLI-adjacent
service calls) can write an engineering revision without a chained audit record,
that the chain detects tampering, and that the recorded actor/tool/session came
from the server rather than from the client payload.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentcad.api_documents import create_documents_router
from agentcad.api_v1 import create_v1_compat_router
from agentcad.audit import request_audit_context
from agentcad.main import create_app
from agentcad.models import CreateDocumentRequest, TransactionRequest
from agentcad.service import DocumentService, InvalidOperationError
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry


@pytest.fixture()
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PID_AGENT_DATABASE_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setenv("PID_AGENT_DIAGNOSTICS_PATH", str(tmp_path / "diagnostics.jsonl"))
    application = create_app()
    with TestClient(application) as client:
        yield client, application


def _service(tmp_path: Path, name: str = "audit.db") -> DocumentService:
    return DocumentService(SQLiteDocumentStore(tmp_path / name), SymbolRegistry())


def _sample_transaction(revision: int) -> TransactionRequest:
    from agentcad.models import Point, SymbolElement

    return TransactionRequest(
        expected_revision=revision,
        label="Add pump",
        operations=[
            {
                "op": "add_element",
                "element": SymbolElement(
                    symbol_key="centrifugal_pump",
                    position=Point(x=100.0, y=100.0),
                    width=60.0,
                    height=40.0,
                ).model_dump(mode="json"),
            }
        ],
    )


# --------------------------------------------------------------------------- unit


def test_revision_write_records_chained_audit_record(tmp_path: Path) -> None:
    service = _service(tmp_path)
    document = service.create_document(CreateDocumentRequest(name="Loop"))
    result = service.apply_transaction(
        document.id,
        _sample_transaction(document.revision),
        audit=request_audit_context(
            "apply_web_transaction",
            actor="web-user",
            label="Add pump",
            validation_status="valid",
        ),
    )

    record = service.store.get_audit_record_for_revision(
        document.id, result.document.revision
    )
    assert record is not None
    assert record.event_type == "revision.created"
    assert record.status == "applied"
    assert record.actor == "web-user"
    assert record.surface == "rest"
    assert record.base_revision == document.revision
    assert record.result_revision == result.document.revision
    assert record.diff_hash
    assert record.record_hash
    assert record.prev_hash != record.record_hash
    assert record.validation_status == "valid"
    assert record.evidence["element_count_before"] == 0
    assert record.evidence["element_count_after"] == 1
    assert service.audit.verify_chain().ok is True


def test_history_details_and_audit_land_in_same_transaction(tmp_path: Path) -> None:
    service = _service(tmp_path)
    document = service.create_document(CreateDocumentRequest(name="Loop"))
    result = service.apply_transaction(
        document.id,
        _sample_transaction(document.revision),
        audit=request_audit_context("apply_web_transaction"),
    )

    history = service.store.get_history_revision_detailed(
        document.id, result.document.revision
    )
    assert history is not None
    details = history["details"]
    assert "semantic_diff" in details
    record = service.store.get_audit_record_for_revision(
        document.id, result.document.revision
    )
    assert record is not None
    assert record.evidence["semantic_diff"]["schema"] == details["semantic_diff"]["schema"]


def test_failed_write_leaves_no_revision_and_records_rejection(tmp_path: Path) -> None:
    service = _service(tmp_path)
    document = service.create_document(CreateDocumentRequest(name="Loop"))
    before_count = len(service.store.all_audit_records())

    with pytest.raises(InvalidOperationError):
        service.apply_transaction(
            document.id,
            TransactionRequest(
                expected_revision=document.revision,
                operations=[{"op": "delete_element", "element_id": "missing-element"}],
            ),
            audit=request_audit_context("apply_web_transaction"),
        )

    assert service.get_document(document.id).revision == document.revision
    assert service.store.get_audit_record_for_revision(document.id, document.revision + 1) is None
    assert len(service.store.all_audit_records()) == before_count


def test_undo_and_redo_are_audited(tmp_path: Path) -> None:
    service = _service(tmp_path)
    document = service.create_document(CreateDocumentRequest(name="Loop"))
    service.apply_transaction(
        document.id,
        _sample_transaction(document.revision),
        audit=request_audit_context("apply_web_transaction"),
    )
    undone = service.undo(document.id, audit=request_audit_context("undo_document"))
    redone = service.redo(document.id, audit=request_audit_context("redo_document"))

    actions = [
        record.evidence.get("action")
        for record in service.audit.audit_trail(document_id=document.id, limit=50)
    ]
    assert "undo" in actions
    assert "redo" in actions
    assert undone.revision == redone.revision - 1
    assert service.audit.verify_chain().ok is True


def test_chain_detects_tampering(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    document = service.create_document(CreateDocumentRequest(name="Loop"))
    service.apply_transaction(
        document.id,
        _sample_transaction(document.revision),
        audit=request_audit_context("apply_web_transaction"),
    )
    assert service.audit.verify_chain().ok is True

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE audit_records SET actor = 'someone-else' WHERE ordinal = 2"
        )
        connection.commit()
    finally:
        connection.close()

    verification = service.audit.verify_chain()
    assert verification.ok is False
    assert verification.first_divergence is not None
    assert verification.first_divergence.reason == "hash_mismatch"
    assert verification.first_divergence.ordinal == 2


def test_chain_detects_deleted_middle_record(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    document = service.create_document(CreateDocumentRequest(name="Loop"))
    first = service.apply_transaction(
        document.id,
        _sample_transaction(document.revision),
        audit=request_audit_context("apply_web_transaction"),
    )
    service.apply_transaction(
        document.id,
        _sample_transaction(first.document.revision),
        audit=request_audit_context("apply_web_transaction"),
    )
    assert service.audit.verify_chain().ok is True

    connection = sqlite3.connect(database)
    try:
        connection.execute("DELETE FROM audit_records WHERE ordinal = 2")
        connection.commit()
    finally:
        connection.close()

    verification = service.audit.verify_chain()
    assert verification.ok is False
    assert verification.first_divergence is not None
    assert verification.first_divergence.reason == "ordinal_gap"
    assert verification.first_divergence.ordinal == 3


def test_chain_cannot_detect_truncated_tail_without_external_anchor(tmp_path: Path) -> None:
    """Documented limitation: dropping the newest records leaves a locally valid chain.

    This is exactly why ``export_package`` records the chain head hash and record
    count: verification of a *closed* package detects the truncation.
    """

    database = tmp_path / "audit.db"
    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    document = service.create_document(CreateDocumentRequest(name="Loop"))
    service.apply_transaction(
        document.id,
        _sample_transaction(document.revision),
        audit=request_audit_context("apply_web_transaction"),
    )
    package = service.audit.export_package(document_id=document.id, limit=100)

    connection = sqlite3.connect(database)
    try:
        connection.execute("DELETE FROM audit_records WHERE ordinal = 2")
        connection.commit()
    finally:
        connection.close()

    assert service.audit.verify_chain().ok is True  # local chain still self-consistent
    reopened_package = service.audit.export_package(document_id=document.id, limit=100)
    assert reopened_package["verification"]["record_count"] < package["verification"]["record_count"]
    assert package["chain_head_hash"] != reopened_package["chain_head_hash"]


def test_revision_evidence_recomputes_hashes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    document = service.create_document(CreateDocumentRequest(name="Loop"))
    result = service.apply_transaction(
        document.id,
        _sample_transaction(document.revision),
        audit=request_audit_context("apply_web_transaction", validation_status="valid"),
    )

    evidence = service.audit.revision_evidence(document.id, result.document.revision)
    assert evidence.audit_record is not None
    assert evidence.semantic_diff is not None
    assert evidence.diff_hash_matches is True
    assert evidence.chain_verified is True


def test_audit_evidence_is_bounded_identifiers_and_hashes(tmp_path: Path) -> None:
    """The audit record carries attribution, counts, ids and hashes, not document bodies."""

    service = _service(tmp_path)
    document = service.create_document(CreateDocumentRequest(name="Loop"))
    service.apply_transaction(
        document.id,
        _sample_transaction(document.revision),
        audit=request_audit_context("apply_web_transaction"),
    )
    record = service.store.get_audit_record_for_revision(document.id, document.revision + 1)
    assert record is not None
    serialized = json.dumps(record.evidence)
    # No element payload is copied wholesale into evidence.
    assert "centrifugal_pump" not in serialized
    assert "rotation" not in serialized
    assert set(record.evidence) >= {
        "attribution",
        "semantic_diff",
        "element_count_before",
        "element_count_after",
        "changed_entity_ids",
    }


# ---------------------------------------------------------------------- surfaces


def test_rest_transaction_is_attributed_to_rest_surface(app_client) -> None:
    client, application = app_client
    service = application.state.service
    document = service.create_document(CreateDocumentRequest(name="Loop"))

    response = client.post(
        f"/api/v2/documents/{document.id}/transactions",
        json=_sample_transaction(document.revision).model_dump(mode="json"),
    )
    assert response.status_code == 200, response.text

    record = service.store.get_audit_record_for_revision(document.id, document.revision + 1)
    assert record is not None
    assert record.surface == "rest"
    assert record.actor == "web-user"
    assert record.tool_name == "apply_web_transaction"
    # Server-derived correlation id, not anything the client sent.
    assert record.evidence["attribution"]["request_id"]


def test_rest_client_cannot_forge_attribution(app_client) -> None:
    client, application = app_client
    service = application.state.service
    document = service.create_document(CreateDocumentRequest(name="Loop"))

    payload = _sample_transaction(document.revision).model_dump(mode="json")
    payload["source"] = "mcp"
    payload["label"] = "forged"
    response = client.post(f"/api/v2/documents/{document.id}/transactions", json=payload)
    assert response.status_code == 200, response.text

    record = service.store.get_audit_record_for_revision(document.id, document.revision + 1)
    assert record is not None
    assert record.surface == "rest"
    assert record.actor == "web-user"


def test_legacy_v1_writes_are_attributed(app_client) -> None:
    client, application = app_client
    service = application.state.service

    response = client.post(
        "/api/v1/draw/line",
        json={"start": [0, 0], "end": [100, 100], "layer": "default"},
    )
    assert response.status_code == 200, response.text

    records = [
        record
        for record in service.audit.audit_trail(limit=50)
        if record.event_type == "revision.created"
    ]
    assert records, "legacy v1 write must produce an audit record"
    assert records[0].surface == "rest"
    assert records[0].actor == "legacy-v1-client"
    assert records[0].tool_name.startswith("legacy_v1.")
    assert service.audit.verify_chain().ok is True


def test_document_rename_is_audited(app_client) -> None:
    client, application = app_client
    service = application.state.service
    document = service.create_document(CreateDocumentRequest(name="Loop"))

    response = client.put(
        f"/api/v2/documents/{document.id}/name",
        json={"name": "Loop A", "expected_revision": document.revision},
    )
    assert response.status_code == 200, response.text

    record = service.store.get_audit_record_for_revision(document.id, document.revision + 1)
    assert record is not None
    assert record.tool_name == "rename_document"
    assert record.evidence["semantic_diff"]["schema"]


def test_document_folder_move_is_audited(app_client) -> None:
    client, application = app_client
    service = application.state.service
    document = service.create_document(CreateDocumentRequest(name="Loop"))

    response = client.put(
        f"/api/v2/documents/{document.id}/folder",
        json={"folder_id": "area-100", "expected_revision": document.revision},
    )
    assert response.status_code == 200, response.text

    record = service.store.get_audit_record_for_revision(document.id, document.revision + 1)
    assert record is not None
    assert record.tool_name == "move_document_folder"


def test_audit_read_endpoints(app_client) -> None:
    client, application = app_client
    service = application.state.service
    document = service.create_document(CreateDocumentRequest(name="Loop"))
    service.apply_transaction(
        document.id,
        _sample_transaction(document.revision),
        audit=request_audit_context("apply_web_transaction"),
    )

    verify = client.get("/api/v2/audit/verify")
    assert verify.status_code == 200
    assert verify.json()["ok"] is True

    trail = client.get("/api/v2/audit/records", params={"document_id": document.id})
    assert trail.status_code == 200
    assert {item["document_id"] for item in trail.json()} == {document.id}

    exported = client.get("/api/v2/audit/export", params={"document_id": document.id})
    assert exported.status_code == 200
    assert exported.headers["x-pid-agent-audit-chain-ok"] == "true"
    package = exported.json()
    assert package["records"]
    assert package["verification"]["ok"] is True

    evidence = client.get(f"/api/v2/documents/{document.id}/audit")
    assert evidence.status_code == 200
    assert len(evidence.json()) >= 1

    revision_evidence = client.get(
        f"/api/v2/documents/{document.id}/history/{document.revision + 1}/evidence"
    )
    assert revision_evidence.status_code == 200
    assert revision_evidence.json()["diff_hash_matches"] is True


def test_harness_approval_and_audit_commit_together(app_client) -> None:
    client, application = app_client
    service = application.state.service
    document = service.create_document(CreateDocumentRequest(name="Loop"))

    session = client.post(
        "/api/v2/agent/sessions",
        json={"document_id": document.id, "actor": "agent-runner", "provider": "openai", "model": "gpt-test"},
    )
    assert session.status_code in {200, 201}, session.text
    session_id = session.json()["id"]

    transaction = _sample_transaction(document.revision)
    approval = client.post(
        f"/api/v2/agent/sessions/{session_id}/approvals",
        json={
            "tool_name": "apply_compiled_agent_transaction",
            "document_id": document.id,
            "intent": {"transaction": transaction.model_dump(mode="json")},
        },
    )
    assert approval.status_code in {200, 201}, approval.text
    approval_id = approval.json()["id"]
    assert client.post(
        f"/api/v2/agent/approvals/{approval_id}/resolve",
        json={"approved": True, "actor": "engineer"},
    ).status_code == 200

    response = client.post(
        f"/api/v2/documents/{document.id}/agent/apply-v2",
        json={
            "session_id": session_id,
            "approval_id": approval_id,
            "plan_id": "plan-1",
            "transaction": transaction.model_dump(mode="json"),
        },
    )
    assert response.status_code == 200, response.text

    record = service.store.get_audit_record_for_revision(document.id, document.revision + 1)
    assert record is not None
    assert record.session_id == session_id
    assert record.approval_id == approval_id
    assert record.tool_call_id
    assert record.provider == "openai"
    assert record.model == "gpt-test"
    assert record.intent_hash
    assert record.surface == "rest"

    # Approval consumption and session closure happened in the same write.
    assert service.store.get_tool_approval(approval_id).status == "consumed"
    assert service.store.get_agent_session(session_id).status == "completed"
    assert service.store.get_agent_session(session_id).end_revision == document.revision + 1
    assert service.audit.verify_chain().ok is True


def test_rejected_approval_is_recorded_as_evidence(app_client) -> None:
    client, application = app_client
    service = application.state.service
    document = service.create_document(CreateDocumentRequest(name="Loop"))
    session_id = client.post(
        "/api/v2/agent/sessions",
        json={"document_id": document.id, "actor": "agent-runner"},
    ).json()["id"]
    transaction = _sample_transaction(document.revision)
    approval = client.post(
        f"/api/v2/agent/sessions/{session_id}/approvals",
        json={
            "tool_name": "apply_compiled_agent_transaction",
            "document_id": document.id,
            "intent": {"transaction": transaction.model_dump(mode="json")},
        },
    ).json()
    client.post(
        f"/api/v2/agent/approvals/{approval['id']}/resolve",
        json={"approved": False, "actor": "engineer"},
    )

    response = client.post(
        f"/api/v2/documents/{document.id}/agent/apply-v2",
        json={
            "session_id": session_id,
            "approval_id": approval["id"],
            "plan_id": "plan-1",
            "transaction": transaction.model_dump(mode="json"),
        },
    )
    assert response.status_code >= 400

    rejected = [
        record
        for record in service.store.all_audit_records()
        if record.status in {"rejected", "failed"}
    ]
    assert rejected, "a refused approval must leave reviewable evidence"
    assert service.get_document(document.id).revision == document.revision
    assert service.audit.verify_chain().ok is True


def test_intent_mismatch_is_recorded_and_does_not_write(app_client) -> None:
    client, application = app_client
    service = application.state.service
    document = service.create_document(CreateDocumentRequest(name="Loop"))
    session_id = client.post(
        "/api/v2/agent/sessions",
        json={"document_id": document.id, "actor": "agent-runner"},
    ).json()["id"]
    transaction = _sample_transaction(document.revision)
    approval = client.post(
        f"/api/v2/agent/sessions/{session_id}/approvals",
        json={
            "tool_name": "apply_compiled_agent_transaction",
            "document_id": document.id,
            "intent": {"transaction": transaction.model_dump(mode="json")},
        },
    ).json()

    other = _sample_transaction(document.revision).model_copy(update={"label": "different"})
    response = client.post(
        f"/api/v2/documents/{document.id}/agent/apply-v2",
        json={
            "session_id": session_id,
            "approval_id": approval["id"],
            "plan_id": "plan-1",
            "transaction": other.model_dump(mode="json"),
        },
    )
    assert response.status_code >= 400
    assert service.get_document(document.id).revision == document.revision
    assert any(
        record.status in {"rejected", "failed"} for record in service.store.all_audit_records()
    )


def test_diagnostics_event_is_derived_from_audit_record(app_client, tmp_path: Path) -> None:
    client, application = app_client
    service = application.state.service
    document = service.create_document(CreateDocumentRequest(name="Loop"))
    client.post(
        f"/api/v2/documents/{document.id}/transactions",
        json=_sample_transaction(document.revision).model_dump(mode="json"),
    )

    log_path = Path(application.state.diagnostics.path)
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    revision_events = [
        event for event in events if event.get("event") == "document.revision.created"
    ]
    assert revision_events, "every committed revision must appear in the diagnostics log"
    latest = revision_events[-1]
    assert latest["revision"] == document.revision + 1
    assert latest["semantic_diff_persisted"] is True
    assert latest["audit_record_id"]


# ------------------------------------------------------------------ store / engine


def test_audit_survives_store_reopen(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    document = service.create_document(CreateDocumentRequest(name="Loop"))
    service.apply_transaction(
        document.id,
        _sample_transaction(document.revision),
        audit=request_audit_context("apply_web_transaction"),
    )

    reopened = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    assert reopened.audit.verify_chain().ok is True
    assert len(reopened.store.all_audit_records()) == 2


def test_schema_version_covers_audit_tables(tmp_path: Path) -> None:
    from agentcad.database_recovery import database_info

    database = tmp_path / "audit.db"
    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    document = service.create_document(CreateDocumentRequest(name="Loop"))
    service.apply_transaction(
        document.id,
        _sample_transaction(document.revision),
        audit=request_audit_context("apply_web_transaction"),
    )

    info = database_info(database)
    assert info.current_schema_version == info.schema_version
    assert info.schema_version >= 4

    connection = sqlite3.connect(database)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()
    assert "audit_records" in tables
    assert service.audit.verify_chain().ok is True
    history = service.store.get_history_revision_detailed(document.id, document.revision + 1)
    assert history is not None and history["details"]["semantic_diff"]


def test_store_save_requires_explicit_audit_for_new_revisions(tmp_path: Path) -> None:
    """A revision written without a draft is still chained by the store, but the
    service is the only supported way to write: routers must not bypass it."""

    service = _service(tmp_path)
    api_v1 = create_v1_compat_router(service)
    documents_router = create_documents_router(service)
    assert api_v1 is not None and documents_router is not None

    document = service.create_document(CreateDocumentRequest(name="Empty"))
    records = service.audit.audit_trail(limit=10)
    assert records and all(record.record_hash for record in records)
    assert records[0].document_id == document.id
    assert service.audit.verify_chain().ok is True
