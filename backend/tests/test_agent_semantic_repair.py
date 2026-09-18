from pathlib import Path

from fastapi.testclient import TestClient

from agentcad.agent_semantic import SemanticTransactionCompiler
from agentcad.agent_semantic_models import (
    ConnectPortsOperation,
    ReconnectConnectorOperation,
    ReplaceSymbolOperation,
    SafeDeleteElementOperation,
    SemanticAgentPlan,
    SemanticTransaction,
)
from agentcad.config import Settings
from agentcad.main import create_app
from agentcad.models import (
    AddElementOperation,
    ConnectorElement,
    ConnectorEndpoint,
    CreateDocumentRequest,
    Point,
    SymbolElement,
    TransactionRequest,
)
from agentcad.semantic_planner import SemanticAgentPlanner
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry


def make_service(tmp_path: Path) -> DocumentService:
    return DocumentService(SQLiteDocumentStore(tmp_path / "semantic.db"), SymbolRegistry())


def seed_connected_valves(service: DocumentService):
    document = service.create_document(CreateDocumentRequest(name="Semantic repair"))
    source = SymbolElement(
        id="source_valve",
        symbol_key="ball_valve",
        position={"x": 100, "y": 200},
        width=60,
        height=40,
        label="V-101",
    )
    target = SymbolElement(
        id="target_valve",
        symbol_key="ball_valve",
        position={"x": 360, "y": 200},
        width=60,
        height=40,
        label="V-102",
    )
    connector = ConnectorElement(
        id="process_line",
        points=[Point(x=160, y=220), Point(x=360, y=220)],
        source=ConnectorEndpoint(
            element_id=source.id,
            port_id="out",
            point=Point(x=160, y=220),
        ),
        target=ConnectorEndpoint(
            element_id=target.id,
            port_id="in",
            point=Point(x=360, y=220),
        ),
        routing="orthogonal",
    )
    result = service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=0,
            label="Seed connected valves",
            operations=[
                AddElementOperation(element=source),
                AddElementOperation(element=target),
                AddElementOperation(element=connector),
            ],
        ),
    )
    return result.document


def test_replace_symbol_preserves_connector_identity_and_rebinds_ports(tmp_path: Path):
    service = make_service(tmp_path)
    document = seed_connected_valves(service)
    compiler = SemanticTransactionCompiler(service)

    missing_mapping = compiler.compile(
        document.id,
        SemanticTransaction(
            expected_revision=document.revision,
            operations=[
                ReplaceSymbolOperation(
                    element_id="source_valve",
                    symbol_key="heat_exchanger",
                )
            ],
        ),
    )
    assert missing_mapping.transaction is None
    assert missing_mapping.assessment.issues[0].code == "replacement_port_mapping_required"
    assert "tube_out" in missing_mapping.assessment.issues[0].available_values["new_port_ids"]

    compiled = compiler.compile(
        document.id,
        SemanticTransaction(
            expected_revision=document.revision,
            label="Replace valve with exchanger",
            operations=[
                ReplaceSymbolOperation(
                    element_id="source_valve",
                    symbol_key="heat_exchanger",
                    port_mapping={"out": "tube_out"},
                )
            ],
        ),
    )
    assert compiled.assessment.valid is True
    assert compiled.transaction is not None
    assert [operation.op for operation in compiled.transaction.operations] == [
        "delete_element",
        "add_element",
        "update_element",
    ]

    applied = service.apply_transaction(document.id, compiled.transaction)
    replacement = next(item for item in applied.document.elements if item.id == "source_valve")
    line = next(item for item in applied.document.elements if item.id == "process_line")
    assert replacement.type == "symbol"
    assert replacement.symbol_key == "heat_exchanger"
    assert replacement.label == "V-101"
    assert line.source.element_id == "source_valve"
    assert line.source.port_id == "tube_out"
    assert line.id == "process_line"


def test_connect_reconnect_and_connection_aware_delete_compile_sequentially(tmp_path: Path):
    service = make_service(tmp_path)
    document = service.create_document(CreateDocumentRequest(name="Connect ports"))
    compiler = SemanticTransactionCompiler(service)

    transaction = SemanticTransaction(
        expected_revision=0,
        operations=[
            AddElementOperation(
                element=SymbolElement(
                    id="left",
                    symbol_key="ball_valve",
                    position={"x": 100, "y": 100},
                    width=60,
                    height=40,
                )
            ),
            AddElementOperation(
                element=SymbolElement(
                    id="right",
                    symbol_key="gate_valve",
                    position={"x": 360, "y": 100},
                    width=60,
                    height=50,
                )
            ),
            ConnectPortsOperation(
                connector_id="line_1",
                source_element_id="left",
                source_port_id="out",
                target_element_id="right",
                target_port_id="in",
            ),
        ],
    )
    compiled = compiler.compile(document.id, transaction)
    assert compiled.assessment.valid is True
    assert compiled.transaction is not None
    applied = service.apply_transaction(document.id, compiled.transaction).document
    line = next(item for item in applied.elements if item.id == "line_1")
    assert line.source.element_id == "left"
    assert line.target.element_id == "right"

    reconnect = compiler.compile(
        document.id,
        SemanticTransaction(
            expected_revision=applied.revision,
            operations=[
                ReconnectConnectorOperation(
                    connector_id="line_1",
                    endpoint="target",
                    element_id="left",
                    port_id="in",
                    routing="orthogonal",
                )
            ],
        ),
    )
    assert reconnect.assessment.valid is True
    reconnected = service.apply_transaction(document.id, reconnect.transaction).document
    line = next(item for item in reconnected.elements if item.id == "line_1")
    assert line.target.element_id == "left"
    assert line.target.port_id == "in"

    rejected = compiler.compile(
        document.id,
        SemanticTransaction(
            expected_revision=reconnected.revision,
            operations=[SafeDeleteElementOperation(element_id="left")],
        ),
    )
    assert rejected.transaction is None
    assert rejected.assessment.issues[0].code == "element_has_connections"

    cascade = compiler.compile(
        document.id,
        SemanticTransaction(
            expected_revision=reconnected.revision,
            operations=[
                SafeDeleteElementOperation(
                    element_id="left",
                    connection_policy="delete_connectors",
                )
            ],
        ),
    )
    assert cascade.assessment.valid is True
    deleted = service.apply_transaction(document.id, cascade.transaction).document
    assert {item.id for item in deleted.elements} == {"right"}


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                database_path=tmp_path / "semantic-api.db",
                cors_origins=["http://localhost:5173"],
                frontend_dist=tmp_path / "missing-dist",
                diagnostics_path=tmp_path / "semantic-api.diagnostics.jsonl",
            )
        )
    )


def test_invalid_semantic_plan_can_be_replanned_without_writing(tmp_path: Path, monkeypatch):
    def fake_plan(self, document_id, request):
        return SemanticAgentPlan(
            explanation="Invalid replacement for repair test",
            transaction=SemanticTransaction(
                expected_revision=request.expected_revision,
                label="Invalid replacement",
                operations=[
                    ReplaceSymbolOperation(
                        element_id="source_valve",
                        symbol_key="heat_exchanger",
                    )
                ],
            ),
        )

    def fake_replan(self, document_id, request, failure):
        assert failure.valid is False
        assert failure.issues[0].code == "replacement_port_mapping_required"
        return SemanticAgentPlan(
            explanation="Add the required real port mapping",
            transaction=SemanticTransaction(
                expected_revision=request.expected_revision,
                label="Repaired replacement",
                operations=[
                    ReplaceSymbolOperation(
                        element_id="source_valve",
                        symbol_key="heat_exchanger",
                        port_mapping={"out": "tube_out"},
                    )
                ],
            ),
        )

    monkeypatch.setattr(SemanticAgentPlanner, "plan", fake_plan)
    monkeypatch.setattr(SemanticAgentPlanner, "replan", fake_replan)
    client = make_client(tmp_path)
    service = client.app.state.service
    seeded = seed_connected_valves(service)

    preview = client.post(
        f"/api/v2/documents/{seeded.id}/agent/plan-v2",
        json={
            "prompt": "replace the selected valve",
            "dry_run": True,
            "expected_revision": seeded.revision,
        },
    )
    assert preview.status_code == 200
    preview_payload = preview.json()
    assert preview_payload["compiled_plan"] is None
    assert preview_payload["assessment"]["valid"] is False
    assert preview_payload["assessment"]["issues"][0]["code"] == (
        "replacement_port_mapping_required"
    )
    unchanged = client.get(f"/api/v2/documents/{seeded.id}").json()
    assert unchanged["revision"] == seeded.revision

    repaired = client.post(
        f"/api/v2/documents/{seeded.id}/agent/replan",
        json={
            "session_id": preview_payload["session_id"],
            "prompt": "replace the selected valve",
            "expected_revision": seeded.revision,
            "failed_plan": preview_payload["plan"],
            "attempt": 1,
        },
    )
    assert repaired.status_code == 200
    repaired_payload = repaired.json()
    assert repaired_payload["assessment"]["valid"] is True
    assert repaired_payload["parent_plan_id"] == preview_payload["plan"]["plan_id"]
    assert repaired_payload["compiled_plan"] is not None

    compiled_transaction = repaired_payload["compiled_plan"]["transaction"]
    approval = client.post(
        f"/api/v2/agent/sessions/{repaired_payload['session_id']}/approvals",
        json={
            "tool_name": "apply_compiled_agent_transaction",
            "document_id": seeded.id,
            "intent": {"transaction": compiled_transaction},
            "requested_by": "test-engineer",
            "reason": "Apply repaired semantic plan",
        },
    ).json()
    resolved = client.post(
        f"/api/v2/agent/approvals/{approval['id']}/resolve",
        json={"approved": True, "actor": "test-engineer"},
    )
    assert resolved.status_code == 200

    applied = client.post(
        f"/api/v2/documents/{seeded.id}/agent/apply-v2",
        json={
            "session_id": repaired_payload["session_id"],
            "approval_id": approval["id"],
            "plan_id": repaired_payload["plan"]["plan_id"],
            "parent_plan_id": repaired_payload["parent_plan_id"],
            "attempt": repaired_payload["attempt"],
            "transaction": compiled_transaction,
        },
    )
    assert applied.status_code == 200
    assert applied.json()["document"]["revision"] == seeded.revision + 1
    history = client.get(f"/api/v2/documents/{seeded.id}/history").json()
    assert history[0]["source"] == "llm"
    assert history[0]["label"] == "Repaired replacement"


def test_analyze_endpoint_returns_structured_revision_repair(tmp_path: Path):
    client = make_client(tmp_path)
    document = client.post("/api/v2/documents", json={"name": "Analyze"}).json()
    analysis = client.post(
        f"/api/v2/documents/{document['id']}/transactions/analyze",
        json={
            "expected_revision": 99,
            "operations": [
                {
                    "op": "add_element",
                    "element": {
                        "id": "never_written",
                        "type": "text",
                        "position": {"x": 10, "y": 20},
                        "text": "stale",
                    },
                }
            ],
        },
    )
    assert analysis.status_code == 200
    payload = analysis.json()
    assert payload["valid"] is False
    assert payload["issues"][0]["code"] == "revision_conflict"
    assert "重新读取" in payload["issues"][0]["suggestions"][0]
