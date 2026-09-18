"""REST surface tests for the engineering semantic graph (M2).

Checks three things the charter cares about: an engineer can ask engineering
questions over HTTP, *read* routes cannot change stored state, and the one mutating
route (index rebuild) is audited and leaves the evidence chain valid.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentcad.main import create_app
from agentcad.models import (
    AddElementOperation,
    Point,
    SymbolElement,
    TransactionRequest,
)


@pytest.fixture()
def client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PID_AGENT_DATABASE_PATH", str(tmp_path / "api.db"))
    app = create_app()
    with TestClient(app) as test_client:
        test_client.app_state = app.state  # type: ignore[attr-defined]
        yield test_client


def _seed_document(client: TestClient, *, name: str = "IR API") -> str:
    document = client.post("/api/v2/documents", json={"name": name}).json()
    document_id = document["id"]
    response = client.post(
        f"/api/v2/documents/{document_id}/transactions",
        json={
            "expected_revision": 0,
            "operations": [
                {
                    "op": "add_element",
                    "element": {
                        "id": "pump_a",
                        "type": "symbol",
                        "symbol_key": "centrifugal_pump",
                        "position": {"x": 100, "y": 100},
                        "width": 80,
                        "height": 70,
                        "label": "P-101",
                    },
                },
                {
                    "op": "add_element",
                    "element": {
                        "id": "pump_b",
                        "type": "symbol",
                        "symbol_key": "centrifugal_pump",
                        "position": {"x": 400, "y": 100},
                        "width": 80,
                        "height": 70,
                        "label": "P-102",
                    },
                },
                {
                    "op": "add_element",
                    "element": {
                        "id": "line_a",
                        "type": "connector",
                        "points": [{"x": 180, "y": 100}, {"x": 400, "y": 100}],
                        "process_tag": "PL-1001",
                        "medium": "water",
                        "nominal_diameter": "DN50",
                        "flow_direction": "forward",
                        "routing": "manual",
                        "source": {
                            "element_id": "pump_a",
                            "port_id": "discharge",
                            "point": {"x": 180, "y": 100},
                        },
                        "target": {
                            "element_id": "pump_b",
                            "port_id": "suction",
                            "point": {"x": 400, "y": 100},
                        },
                    },
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    return document_id


def test_engineering_graph_endpoint_returns_objects_and_findings(client: TestClient) -> None:
    document_id = _seed_document(client)
    response = client.get(f"/api/v2/documents/{document_id}/engineering-graph")

    assert response.status_code == 200
    assert response.headers["X-PID-Agent-Graph-Revision"] == "1"
    assert response.headers["X-PID-Agent-Graph-Objects"] == "3"
    assert response.headers["X-PID-Agent-Graph-Hash"]
    body = response.json()
    assert body["schema"] == "pid-agent.engineering-graph"
    assert {record["object_id"] for record in body["objects"]} == {
        "equipment:p-101",
        "equipment:p-102",
        "line:pl-1001",
    }
    assert body["counts"]["equipment"] == 2
    assert body["counts"]["lines"] == 1
    assert body["edges"][0]["source_object_id"] == "equipment:p-101"

    missing = client.get("/api/v2/documents/doc_missing/engineering-graph")
    assert missing.status_code == 404


def test_read_routes_do_not_change_stored_state(client: TestClient) -> None:
    document_id = _seed_document(client)
    service = client.app_state.service  # type: ignore[attr-defined]
    audit_before = len(service.store.all_audit_records())

    client.get(f"/api/v2/documents/{document_id}/engineering-graph")
    client.get(
        f"/api/v2/documents/{document_id}/engineering-graph/trace",
        params={"object_id": "equipment:p-101"},
    )
    client.get("/api/v2/project/engineering-graph")

    assert service.get_document(document_id).revision == 1
    assert service.store.list_project_index() == []
    assert len(service.store.all_audit_records()) == audit_before


def test_trace_route_walks_and_validates_input(client: TestClient) -> None:
    document_id = _seed_document(client)

    response = client.get(
        f"/api/v2/documents/{document_id}/engineering-graph/trace",
        params={"object_id": "equipment:p-101", "direction": "downstream"},
    )
    assert response.status_code == 200
    body = response.json()
    assert [step["object_id"] for step in body["steps"]] == [
        "equipment:p-101",
        "equipment:p-102",
    ]
    assert body["traversed_pipeline_ids"] == ["line:pl-1001"]
    assert body["truncated"] is False

    unknown = client.get(
        f"/api/v2/documents/{document_id}/engineering-graph/trace",
        params={"object_id": "equipment:nope"},
    )
    assert unknown.status_code == 404
    assert "unknown engineering object" in unknown.json()["detail"]

    invalid = client.get(
        f"/api/v2/documents/{document_id}/engineering-graph/trace",
        params={"object_id": "equipment:p-101", "direction": "sideways"},
    )
    assert invalid.status_code == 422


def test_project_index_rebuild_is_audited_and_chain_stays_valid(client: TestClient) -> None:
    document_id = _seed_document(client)
    assert client.get(f"/api/v2/documents/{document_id}/project-index").status_code == 404

    rebuilt = client.post("/api/v2/project/index/rebuild")
    assert rebuilt.status_code == 200
    report = rebuilt.json()
    assert report["rebuilt"] == [document_id]
    assert report["stale_after"] == []

    entry = client.get(f"/api/v2/documents/{document_id}/project-index")
    assert entry.status_code == 200
    body = entry.json()
    assert body["staleness"] == "verified_fresh"
    assert body["counts"]["equipment"] == 2

    records = client.get(
        "/api/v2/audit/records", params={"event_type": "engineering.index.rebuilt"}
    ).json()
    assert len(records) == 1
    record = records[0]
    assert record["actor"] == "web-user"
    assert record["surface"] == "rest"
    assert record["tool_name"] == "rebuild_project_index"
    assert record["result_revision"] is None
    assert record["evidence"]["rebuilt_count"] == 1
    assert record["evidence"]["builder_version"] == 1
    assert record["evidence"]["attribution"]["request_id"]
    assert record["evidence"]["metadata"]["method"] == "POST"
    assert record["evidence"]["metadata"]["path"] == "/api/v2/project/index/rebuild"

    verification = client.get("/api/v2/audit/verify").json()
    assert verification["ok"] is True

    # Rebuilding again is idempotent and records a second, honest fact.
    again = client.post("/api/v2/project/index/rebuild", params={"force": True})
    assert again.status_code == 200
    assert again.json()["rebuilt"] == [document_id]
    assert client.get("/api/v2/audit/verify").json()["ok"] is True


def test_project_graph_route_reports_cheap_freshness(client: TestClient) -> None:
    document_id = _seed_document(client)
    client.post("/api/v2/project/index/rebuild")

    response = client.get("/api/v2/project/engineering-graph")
    assert response.status_code == 200
    body = response.json()
    assert body["freshness"] == "cheap"
    assert body["document_count"] == 1
    assert body["stale_document_ids"] == []
    assert body["totals"]["objects"] == 3
    assert body["cross_document_links"] == []

    # A new revision makes the cached row stale until it is rebuilt.
    service = client.app_state.service  # type: ignore[attr-defined]
    document = service.get_document(document_id)
    service.apply_transaction(
        document_id,
        TransactionRequest(
            expected_revision=document.revision,
            operations=[
                AddElementOperation(
                    element=SymbolElement(
                        id="pump_c",
                        symbol_key="centrifugal_pump",
                        position=Point(x=700, y=100),
                        width=80,
                        height=70,
                        label="P-103",
                    )
                )
            ],
        ),
        source="system",
    )

    stale = client.get("/api/v2/project/engineering-graph").json()
    assert stale["stale_document_ids"] == [document_id]
    assert [finding["code"] for finding in stale["findings"]] == ["IR_INDEX_STALE"]
