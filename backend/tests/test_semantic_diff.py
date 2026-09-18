from pathlib import Path

from fastapi.testclient import TestClient

from agentcad.config import Settings
from agentcad.main import create_app
from agentcad.semantic_diff import semantic_diff_from_history_details


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                database_path=tmp_path / "semantic-diff.db",
                cors_origins=["http://localhost:5173"],
                frontend_dist=tmp_path / "missing-dist",
            )
        )
    )


def test_semantic_diff_preview_is_engineering_readable_and_does_not_write(tmp_path: Path):
    client = make_client(tmp_path)
    document = client.post("/api/v2/documents", json={"name": "Semantic diff"}).json()

    proposed = {
        "expected_revision": 0,
        "label": "Add isolation valve and process line",
        "operations": [
            {
                "op": "add_element",
                "element": {
                    "id": "valve_101",
                    "type": "symbol",
                    "symbol_key": "ball_valve",
                    "position": {"x": 100, "y": 120},
                    "width": 60,
                    "height": 40,
                    "label": "XV-101",
                },
            },
            {
                "op": "add_element",
                "element": {
                    "id": "line_1002",
                    "type": "connector",
                    "points": [{"x": 160, "y": 140}, {"x": 420, "y": 140}],
                    "process_tag": "L-1002",
                    "medium": "process",
                    "nominal_diameter": "DN80",
                    "flow_direction": "forward",
                },
            },
        ],
    }

    preview = client.post(
        f"/api/v2/documents/{document['id']}/transactions/semantic-diff",
        json=proposed,
    )
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["schema"] == "pid-agent.semantic-diff"
    assert payload["base_revision"] == 0
    assert payload["result_revision"] == 1
    assert payload["change_count"] == 2
    by_id = {item["entity_id"]: item for item in payload["changes"]}
    assert by_id["valve_101"]["entity_kind"] == "valve"
    assert by_id["valve_101"]["display_name"] == "XV-101"
    assert by_id["valve_101"]["summary"] == "Added valve XV-101"
    assert by_id["valve_101"]["risk_hint"] == "engineering_change"
    assert by_id["line_1002"]["entity_kind"] == "pipeline"
    assert by_id["line_1002"]["display_name"] == "L-1002"
    assert by_id["line_1002"]["risk_hint"] == "engineering_change"

    unchanged = client.get(f"/api/v2/documents/{document['id']}").json()
    assert unchanged["revision"] == 0
    assert unchanged["elements"] == []


def test_semantic_diff_distinguishes_layout_from_engineering_property_change(tmp_path: Path):
    client = make_client(tmp_path)
    document = client.post("/api/v2/documents", json={"name": "Diff classifications"}).json()
    seeded = client.post(
        f"/api/v2/documents/{document['id']}/transactions",
        json={
            "expected_revision": 0,
            "operations": [
                {
                    "op": "add_element",
                    "element": {
                        "id": "valve_101",
                        "type": "symbol",
                        "symbol_key": "ball_valve",
                        "position": {"x": 100, "y": 100},
                        "width": 60,
                        "height": 40,
                        "label": "XV-101",
                    },
                },
                {
                    "op": "add_element",
                    "element": {
                        "id": "line_1002",
                        "type": "connector",
                        "points": [{"x": 160, "y": 120}, {"x": 400, "y": 120}],
                        "process_tag": "L-1002",
                        "nominal_diameter": "DN80",
                    },
                },
            ],
        },
    ).json()["document"]

    layout = client.post(
        f"/api/v2/documents/{document['id']}/transactions/semantic-diff",
        json={
            "expected_revision": seeded["revision"],
            "operations": [
                {
                    "op": "update_element",
                    "element_id": "valve_101",
                    "patch": {"position": {"x": 140, "y": 100}},
                }
            ],
        },
    ).json()
    assert layout["change_count"] == 1
    assert layout["changes"][0]["risk_hint"] == "draft_edit"
    assert "layout_changed" in layout["changes"][0]["change_types"]

    engineering = client.post(
        f"/api/v2/documents/{document['id']}/transactions/semantic-diff",
        json={
            "expected_revision": seeded["revision"],
            "operations": [
                {
                    "op": "update_element",
                    "element_id": "line_1002",
                    "patch": {
                        "nominal_diameter": "DN100",
                        "points": [
                            {"x": 160, "y": 120},
                            {"x": 240, "y": 120},
                            {"x": 240, "y": 160},
                            {"x": 400, "y": 160},
                        ],
                    },
                }
            ],
        },
    ).json()
    change = engineering["changes"][0]
    assert change["entity_kind"] == "pipeline"
    assert change["risk_hint"] == "engineering_change"
    assert "rerouted" in change["change_types"]
    assert "engineering_properties_changed" in change["change_types"]
    delta_fields = {item["field"] for item in change["field_deltas"]}
    assert {"nominal_diameter", "points"}.issubset(delta_fields)


def test_committed_revision_persists_and_serves_semantic_diff(tmp_path: Path):
    client = make_client(tmp_path)
    document = client.post("/api/v2/documents", json={"name": "Stored diff"}).json()

    result = client.post(
        f"/api/v2/documents/{document['id']}/transactions",
        json={
            "expected_revision": 0,
            "label": "Add V-201",
            "operations": [
                {
                    "op": "add_element",
                    "element": {
                        "id": "valve_201",
                        "type": "symbol",
                        "symbol_key": "gate_valve",
                        "position": {"x": 200, "y": 200},
                        "width": 60,
                        "height": 50,
                        "label": "XV-201",
                    },
                }
            ],
        },
    )
    assert result.status_code == 200
    assert result.json()["document"]["revision"] == 1

    revision_diff = client.get(
        f"/api/v2/documents/{document['id']}/history/1/semantic-diff"
    )
    assert revision_diff.status_code == 200
    payload = revision_diff.json()
    assert payload["result_revision"] == 1
    assert payload["changes"][0]["summary"] == "Added valve XV-201"

    history = client.get(f"/api/v2/documents/{document['id']}/history").json()
    stored = history[0]["details"]["semantic_diff"]
    assert stored["schema"] == "pid-agent.semantic-diff"
    assert stored["changes"][0]["entity_id"] == "valve_201"


def test_deleted_safety_relief_valve_gets_critical_risk_hint():
    report = semantic_diff_from_history_details(
        "doc_test",
        {
            "base_revision": 4,
            "result_revision": 5,
            "change_count": 1,
            "changes": [
                {
                    "entity_kind": "element",
                    "entity_id": "psv_101",
                    "change": "deleted",
                    "entity_type": "symbol",
                    "changed_fields": ["id", "type", "symbol_key", "label"],
                    "before": {
                        "id": "psv_101",
                        "type": "symbol",
                        "symbol_key": "safety_relief_valve",
                        "label": "PSV-101",
                    },
                    "after": None,
                }
            ],
            "diff_truncated": False,
        },
    )

    assert report.critical_change_count == 1
    assert report.changes[0].entity_kind == "valve"
    assert report.changes[0].risk_hint == "critical_change"
    assert report.changes[0].summary == "Deleted valve PSV-101"
