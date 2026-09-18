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
    assert body["version"] == 2
    # Identity is a stable surrogate; the tag is a mutable attribute beside it.
    assert {record["tag_key"] for record in body["objects"]} == {
        "equipment:p-101",
        "equipment:p-102",
        "line:pl-1001",
    }
    assert all(record["engineering_id"] for record in body["objects"])
    assert all(record["identity_basis"] == "element" for record in body["objects"])
    assert body["counts"]["equipment"] == 2
    assert body["counts"]["lines"] == 1
    assert body["counts"]["signals"] == 0
    assert body["counts"]["process_edges"] == 1
    assert body["edges"][0]["edge_class"] == "process"
    assert body["edges"][0]["source_engineering_id"].startswith("eq_")

    missing = client.get("/api/v2/documents/doc_missing/engineering-graph")
    assert missing.status_code == 404


def test_read_routes_do_not_change_stored_state(client: TestClient) -> None:
    document_id = _seed_document(client)
    service = client.app_state.service  # type: ignore[attr-defined]
    audit_before = len(service.store.all_audit_records())

    client.get(f"/api/v2/documents/{document_id}/engineering-graph")
    client.get(
        f"/api/v2/documents/{document_id}/engineering-graph/trace",
        params={"ref": "equipment:p-101"},
    )
    client.get("/api/v2/project/engineering-graph")
    client.get("/api/v2/project/engineering-objects", params={"ref": "P-101"})

    assert service.get_document(document_id).revision == 1
    assert service.store.list_project_index() == []
    assert len(service.store.all_audit_records()) == audit_before


def test_find_route_returns_empty_before_the_index_exists(client: TestClient) -> None:
    """The lookup reads the cache, so it says nothing until something built it."""

    document_id = _seed_document(client)
    assert (
        client.get("/api/v2/project/engineering-objects", params={"ref": "P-101"}).json()
        == []
    )

    client.post("/api/v2/project/index/rebuild")
    matches = client.get(
        "/api/v2/project/engineering-objects", params={"ref": "P-101"}
    ).json()
    assert [match["document_id"] for match in matches] == [document_id]
    assert matches[0]["tag_key"] == "equipment:p-101"
    assert matches[0]["resolved_from"] == "P-101"

    graph = client.get(f"/api/v2/documents/{document_id}/engineering-graph").json()
    engineering_id = next(
        record["engineering_id"] for record in graph["objects"] if record["tag"] == "P-101"
    )
    by_identity = client.get(
        "/api/v2/project/engineering-objects", params={"ref": engineering_id}
    ).json()
    assert [match["engineering_id"] for match in by_identity] == [engineering_id]
    assert by_identity[0]["resolved_from"] == ""

    # A lookup is a read: it must not have created an index row or an audit fact.
    service = client.app_state.service  # type: ignore[attr-defined]
    assert len(
        [
            record
            for record in service.store.all_audit_records()
            if record.event_type == "engineering.index.rebuilt"
        ]
    ) == 1


def test_trace_route_walks_and_validates_input(client: TestClient) -> None:
    document_id = _seed_document(client)

    response = client.get(
        f"/api/v2/documents/{document_id}/engineering-graph/trace",
        params={"ref": "equipment:p-101", "direction": "downstream"},
    )
    assert response.status_code == 200
    body = response.json()
    assert [step["kind"] for step in body["steps"]] == ["equipment", "equipment"]
    assert [step["engineering_id"] for step in body["steps"]][0].startswith("eq_")
    # The caller used a tag; the answer says which identity it resolved to.
    assert body["resolved_from"] == "equipment:p-101"
    assert body["origin_engineering_id"] == body["steps"][0]["engineering_id"]
    assert len(body["traversed_pipeline_ids"]) == 1
    assert body["traversed_pipeline_ids"][0].startswith("ln_")
    assert body["edge_class"] == "process"
    assert body["truncated"] is False

    # A stable engineering id works as well as a tag.
    by_id = client.get(
        f"/api/v2/documents/{document_id}/engineering-graph/trace",
        params={"ref": body["origin_engineering_id"], "direction": "downstream"},
    )
    assert by_id.status_code == 200
    assert by_id.json()["resolved_from"] == ""
    assert by_id.json()["steps"] == body["steps"]

    unknown = client.get(
        f"/api/v2/documents/{document_id}/engineering-graph/trace",
        params={"ref": "equipment:nope"},
    )
    assert unknown.status_code == 404
    assert "unknown engineering object" in unknown.json()["detail"]

    invalid = client.get(
        f"/api/v2/documents/{document_id}/engineering-graph/trace",
        params={"ref": "equipment:p-101", "direction": "sideways"},
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
    assert record["evidence"]["builder_version"] == 2
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
    assert body["off_page_connections"] == []

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


def test_signal_objects_are_exposed_over_the_rest_graph(client: TestClient) -> None:
    """A signal drawn in a drawing is a first-class object an engineer can query."""

    document_id = _seed_document(client)
    service = client.app_state.service  # type: ignore[attr-defined]
    document = service.get_document(document_id)
    response = client.post(
        f"/api/v2/documents/{document_id}/transactions",
        json={
            "expected_revision": document.revision,
            "operations": [
                {
                    "op": "add_element",
                    "element": {
                        "id": "pi_a",
                        "type": "symbol",
                        "symbol_key": "pressure_indicator",
                        "position": {"x": 250, "y": 300},
                        "width": 60,
                        "height": 60,
                        "label": "PI-101",
                    },
                },
                {
                    "op": "add_element",
                    "element": {
                        "id": "sig_a",
                        "type": "connector",
                        "points": [{"x": 250, "y": 300}, {"x": 180, "y": 100}],
                        "process_tag": "SI-1001",
                        "medium": "electric signal",
                        "routing": "manual",
                        "source": {
                            "element_id": "pi_a",
                            "port_id": "process",
                            "point": {"x": 250, "y": 300},
                        },
                        "target": {
                            "element_id": "pump_a",
                            "port_id": "discharge",
                            "point": {"x": 180, "y": 100},
                        },
                    },
                },
            ],
        },
    )
    assert response.status_code == 200, response.text

    body = client.get(f"/api/v2/documents/{document_id}/engineering-graph").json()
    assert body["counts"]["signals"] == 1
    signal = next(record for record in body["objects"] if record["kind"] == "signal")
    assert signal["signal"]["signal_type"] == "electrical"
    assert signal["signal"]["classification"] == "declared_medium"
    assert signal["signal"]["provenance"]["basis"] == "medium"
    assert signal["engineering_id"] in body["signals"]
    assert signal["signal"]["source_engineering_id"].startswith("inst_")
    # The signal connector must not appear as process flow.
    assert all(
        edge["connector_id"] != "sig_a" or edge["edge_class"] == "signal"
        for edge in body["edges"]
    )

    traced = client.get(
        f"/api/v2/documents/{document_id}/engineering-graph/trace",
        params={"ref": signal["engineering_id"]},
    ).json()
    assert traced["edge_class"] == "signal"
    assert traced["traversed_pipeline_ids"] == []
