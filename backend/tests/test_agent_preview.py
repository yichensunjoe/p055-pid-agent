from pathlib import Path

from fastapi.testclient import TestClient

from agentcad.config import Settings
from agentcad.llm import OpenAICompatiblePlanner
from agentcad.main import create_app
from agentcad.models import (
    AddElementOperation,
    AgentPlan,
    TextElement,
    TransactionRequest,
    UpdateElementOperation,
)


def make_client(tmp_path: Path) -> TestClient:
    app = create_app(
        Settings(
            database_path=tmp_path / "preview.db",
            cors_origins=["http://localhost:5173"],
            frontend_dist=tmp_path / "missing-dist",
        )
    )
    return TestClient(app)


def approve_agent_transaction(
    client: TestClient,
    document_id: str,
    transaction: dict,
) -> tuple[str, str]:
    session = client.post(
        "/api/v2/agent/sessions",
        json={"document_id": document_id, "actor": "test-engineer"},
    ).json()
    approval = client.post(
        f"/api/v2/agent/sessions/{session['id']}/approvals",
        json={
            "tool_name": "apply_compiled_agent_transaction",
            "document_id": document_id,
            "intent": {"transaction": transaction},
            "requested_by": "test-engineer",
            "reason": "Test reviewed Agent transaction",
        },
    ).json()
    resolved = client.post(
        f"/api/v2/agent/approvals/{approval['id']}/resolve",
        json={"approved": True, "actor": "test-engineer", "note": "Approved in test"},
    )
    assert resolved.status_code == 200
    return session["id"], approval["id"]


def test_transaction_validation_does_not_write_and_locates_operation(tmp_path: Path):
    client = make_client(tmp_path)
    document = client.post("/api/v2/documents", json={"name": "Validate"}).json()

    valid = client.post(
        f"/api/v2/documents/{document['id']}/transactions/validate",
        json={
            "expected_revision": 0,
            "label": "Preview text",
            "operations": [
                {
                    "op": "add_element",
                    "element": {
                        "id": "preview_text",
                        "type": "text",
                        "position": {"x": 40, "y": 60},
                        "text": "preview only",
                    },
                }
            ],
        },
    )

    assert valid.status_code == 200
    assert valid.json()["valid"] is True
    assert valid.json()["current_revision"] == 0
    assert valid.json()["next_revision"] == 1
    assert valid.json()["affected_element_ids"] == ["preview_text"]

    unchanged = client.get(f"/api/v2/documents/{document['id']}").json()
    assert unchanged["revision"] == 0
    assert unchanged["elements"] == []

    invalid = client.post(
        f"/api/v2/documents/{document['id']}/transactions/validate",
        json={
            "expected_revision": 0,
            "operations": [
                {
                    "op": "update_element",
                    "element_id": "missing_element",
                    "patch": {"name": "invalid"},
                }
            ],
        },
    )

    assert invalid.status_code == 422
    assert "operations[0]" in invalid.json()["detail"]
    assert "missing_element" in invalid.json()["detail"]


def test_agent_dry_run_then_confirm_records_llm_history(tmp_path: Path, monkeypatch):
    def fake_plan(self, document_id, request):
        assert request.dry_run is True
        return AgentPlan(
            explanation="Add one reviewed text element",
            transaction=TransactionRequest(
                expected_revision=request.expected_revision,
                label="Agent preview confirmation test",
                operations=[
                    AddElementOperation(
                        element=TextElement(
                            id="agent_preview_text",
                            position={"x": 100, "y": 120},
                            text="confirmed",
                        )
                    )
                ],
            ),
        )

    monkeypatch.setattr(OpenAICompatiblePlanner, "plan", fake_plan)
    client = make_client(tmp_path)
    document = client.post("/api/v2/documents", json={"name": "Agent preview"}).json()

    preview = client.post(
        f"/api/v2/documents/{document['id']}/agent/generate",
        json={
            "prompt": "add reviewed text",
            "dry_run": True,
            "expected_revision": 0,
        },
    )

    assert preview.status_code == 200
    assert preview.json()["document"] is None
    transaction = preview.json()["plan"]["transaction"]

    still_unchanged = client.get(f"/api/v2/documents/{document['id']}").json()
    assert still_unchanged["revision"] == 0
    assert still_unchanged["elements"] == []

    session_id, approval_id = approve_agent_transaction(
        client, document["id"], transaction
    )
    applied = client.post(
        f"/api/v2/documents/{document['id']}/agent/apply",
        params={"session_id": session_id, "approval_id": approval_id},
        json=transaction,
    )

    assert applied.status_code == 200
    assert applied.json()["document"]["revision"] == 1
    assert applied.json()["document"]["elements"][0]["id"] == "agent_preview_text"

    history = client.get(f"/api/v2/documents/{document['id']}/history").json()
    assert history[0]["revision"] == 1
    assert history[0]["source"] == "llm"
    assert history[0]["label"] == "Agent preview confirmation test"


def test_agent_confirm_rejects_stale_preview(tmp_path: Path):
    client = make_client(tmp_path)
    document = client.post("/api/v2/documents", json={"name": "Conflict"}).json()

    client.post(
        f"/api/v2/documents/{document['id']}/transactions",
        json={
            "expected_revision": 0,
            "operations": [
                {
                    "op": "add_element",
                    "element": {
                        "id": "human_change",
                        "type": "text",
                        "position": {"x": 10, "y": 20},
                        "text": "human",
                    },
                }
            ],
        },
    )

    stale_transaction = TransactionRequest(
        expected_revision=0,
        label="Stale agent plan",
        operations=[
            UpdateElementOperation(
                element_id="human_change",
                patch={"text": "overwritten"},
            )
        ],
    ).model_dump(mode="json")
    session_id, approval_id = approve_agent_transaction(
        client, document["id"], stale_transaction
    )
    stale = client.post(
        f"/api/v2/documents/{document['id']}/agent/apply",
        params={"session_id": session_id, "approval_id": approval_id},
        json=stale_transaction,
    )

    assert stale.status_code == 409
    latest = client.get(f"/api/v2/documents/{document['id']}").json()
    assert latest["revision"] == 1
    assert latest["elements"][0]["text"] == "human"
