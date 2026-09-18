from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentcad.config import Settings
from agentcad.harness import (
    AgentHarnessService,
    ToolApprovalRequiredError,
    ToolIntentMismatchError,
)
from agentcad.harness_models import (
    AgentSessionCreateRequest,
    ToolApprovalCreateRequest,
    ToolApprovalResolveRequest,
)
from agentcad.main import create_app
from agentcad.models import CreateDocumentRequest
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry
from agentcad.tool_registry import get_default_tool_registry


def _harness(tmp_path: Path) -> tuple[DocumentService, AgentHarnessService]:
    store = SQLiteDocumentStore(tmp_path / "harness.db")
    service = DocumentService(store, SymbolRegistry())
    harness = AgentHarnessService(service, store, get_default_tool_registry())
    return service, harness


def _transaction_intent(revision: int = 0) -> dict:
    return {
        "transaction": {
            "operations": [{"op": "clear_document"}],
            "expected_revision": revision,
            "label": "Harness test",
            "source": None,
        }
    }


def test_engineering_change_requires_exact_one_time_approval(tmp_path: Path):
    service, harness = _harness(tmp_path)
    document = service.create_document(CreateDocumentRequest(name="Approval"))
    session = harness.create_session(
        AgentSessionCreateRequest(document_id=document.id, actor="engineer")
    )
    intent = _transaction_intent()

    with pytest.raises(ToolApprovalRequiredError):
        harness.authorize(
            session_id=session.id,
            tool_name="apply_compiled_agent_transaction",
            document_id=document.id,
            intent=intent,
            base_revision=0,
        )

    approval = harness.request_approval(
        session.id,
        ToolApprovalCreateRequest(
            tool_name="apply_compiled_agent_transaction",
            document_id=document.id,
            intent=intent,
            requested_by="engineer",
            reason="Apply reviewed plan",
        ),
    )
    approval = harness.resolve_approval(
        approval.id,
        ToolApprovalResolveRequest(
            approved=True,
            actor="engineer",
            note="Reviewed against preview",
        ),
    )
    assert approval.status == "approved"

    with pytest.raises(ToolIntentMismatchError):
        harness.authorize(
            session_id=session.id,
            tool_name="apply_compiled_agent_transaction",
            document_id=document.id,
            intent=_transaction_intent(revision=1),
            approval_id=approval.id,
            base_revision=1,
        )

    authorized = harness.authorize(
        session_id=session.id,
        tool_name="apply_compiled_agent_transaction",
        document_id=document.id,
        intent=intent,
        approval_id=approval.id,
        base_revision=0,
    )
    harness.complete_tool_call(authorized, result_revision=1)
    harness.complete_session(session.id, end_revision=1)

    audit = harness.audit(session.id)
    assert audit.session.status == "completed"
    assert audit.session.start_revision == 0
    assert audit.session.end_revision == 1
    assert audit.approvals[0].status == "consumed"
    assert audit.approvals[0].resolved_by == "engineer"
    assert [call.status for call in audit.tool_calls] == ["rejected", "completed"]
    assert audit.tool_calls[0].error_code == "tool_approval_required"

    with pytest.raises(ToolApprovalRequiredError):
        harness.authorize(
            session_id=session.id,
            tool_name="apply_compiled_agent_transaction",
            document_id=document.id,
            intent=intent,
            approval_id=approval.id,
            base_revision=0,
        )


def test_allow_tool_does_not_require_approval(tmp_path: Path):
    service, harness = _harness(tmp_path)
    document = service.create_document(CreateDocumentRequest(name="Layout"))
    session = harness.create_session(
        AgentSessionCreateRequest(document_id=document.id, actor="agent")
    )
    intent = {"options": {"expected_revision": 0}}

    authorized = harness.authorize(
        session_id=session.id,
        tool_name="apply_auto_layout",
        document_id=document.id,
        intent=intent,
        base_revision=0,
    )

    assert authorized.definition.permission == "allow"
    assert authorized.approval is None
    harness.complete_tool_call(authorized, result_revision=0)
    audit = harness.audit(session.id)
    assert audit.tool_calls[0].status == "completed"


def test_rest_agent_apply_is_blocked_until_exact_approval(tmp_path: Path):
    app = create_app(
        Settings(
            database_path=tmp_path / "api-harness.db",
            cors_origins=["http://localhost:5173"],
            frontend_dist=tmp_path / "missing-dist",
        )
    )
    client = TestClient(app)
    document = client.post("/api/v2/documents", json={"name": "Harness API"}).json()
    transaction = {
        "operations": [{"op": "clear_document"}],
        "expected_revision": 0,
        "label": "Approved clear",
        "source": None,
    }

    session_response = client.post(
        "/api/v2/agent/sessions",
        json={"document_id": document["id"], "actor": "engineer"},
    )
    assert session_response.status_code == 201
    session = session_response.json()

    blocked = client.post(
        f"/api/v2/documents/{document['id']}/agent/apply",
        params={"session_id": session["id"]},
        json=transaction,
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"] == "tool_approval_required"
    assert client.get(f"/api/v2/documents/{document['id']}").json()["revision"] == 0

    approval_response = client.post(
        f"/api/v2/agent/sessions/{session['id']}/approvals",
        json={
            "tool_name": "apply_compiled_agent_transaction",
            "document_id": document["id"],
            "intent": {"transaction": transaction},
            "requested_by": "engineer",
            "reason": "Reviewed transaction",
        },
    )
    assert approval_response.status_code == 201
    approval = approval_response.json()

    resolved = client.post(
        f"/api/v2/agent/approvals/{approval['id']}/resolve",
        json={"approved": True, "actor": "engineer", "note": "Approved after review"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "approved"

    applied = client.post(
        f"/api/v2/documents/{document['id']}/agent/apply",
        params={"session_id": session["id"], "approval_id": approval["id"]},
        json=transaction,
    )
    assert applied.status_code == 200
    assert applied.json()["document"]["revision"] == 1

    audit = client.get(f"/api/v2/agent/sessions/{session['id']}/audit")
    assert audit.status_code == 200
    payload = audit.json()
    assert payload["session"]["status"] == "completed"
    assert payload["session"]["end_revision"] == 1
    assert payload["approvals"][0]["status"] == "consumed"
    assert [item["status"] for item in payload["tool_calls"]] == ["rejected", "completed"]

    replay = client.post(
        f"/api/v2/documents/{document['id']}/agent/apply",
        params={"session_id": session["id"], "approval_id": approval["id"]},
        json={**transaction, "expected_revision": 1},
    )
    assert replay.status_code == 409
    assert client.get(f"/api/v2/documents/{document['id']}").json()["revision"] == 1


def test_harness_session_rejects_missing_document(tmp_path: Path):
    app = create_app(
        Settings(
            database_path=tmp_path / "missing-doc.db",
            cors_origins=[],
            frontend_dist=tmp_path / "missing-dist",
        )
    )
    client = TestClient(app)

    response = client.post(
        "/api/v2/agent/sessions",
        json={"document_id": "doc-does-not-exist", "actor": "engineer"},
    )

    assert response.status_code == 404
