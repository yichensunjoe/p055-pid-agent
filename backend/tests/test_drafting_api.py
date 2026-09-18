"""REST surface tests for the deterministic drafting engine (M3).

The interesting property is not "the route returns 200" but *where the write happens*:
drafting is preview-only, so ``/drafting/preview`` must return a transaction that the
ordinary governed transaction route applies. That is what keeps drafting inside the one
audited write path instead of becoming a second one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentcad.main import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PID_AGENT_DATABASE_PATH", str(tmp_path / "drafting-api.db"))
    app = create_app()
    with TestClient(app) as test_client:
        test_client.app_state = app.state  # type: ignore[attr-defined]
        yield test_client


def _seed_document(client: TestClient, *, name: str = "Drafting API") -> str:
    document_id = client.post("/api/v2/documents", json={"name": name}).json()["id"]
    response = client.post(
        f"/api/v2/documents/{document_id}/transactions",
        json={
            "expected_revision": 0,
            "label": "Seed drafting API fixture",
            "operations": [
                {
                    "op": "add_element",
                    "element": {
                        "id": "pump",
                        "type": "symbol",
                        "symbol_key": "centrifugal_pump",
                        "position": {"x": 100, "y": 300},
                        "width": 80,
                        "height": 70,
                        "label": "P-101",
                    },
                },
                {
                    "op": "add_element",
                    "element": {
                        "id": "valve",
                        "type": "symbol",
                        "symbol_key": "gate_valve",
                        "position": {"x": 150, "y": 320},
                        "width": 60,
                        "height": 50,
                        "label": "HV-101",
                    },
                },
                {
                    "op": "add_element",
                    "element": {
                        "id": "pipe",
                        "type": "connector",
                        "points": [
                            {"x": 148, "y": 300},
                            {"x": 178, "y": 300},
                            {"x": 178, "y": 600},
                            {"x": 40, "y": 600},
                            {"x": 40, "y": 345},
                            {"x": 150, "y": 345},
                        ],
                        "source": {
                            "element_id": "pump",
                            "port_id": "discharge",
                            "point": {"x": 148, "y": 300},
                        },
                        "target": {
                            "element_id": "valve",
                            "port_id": "in",
                            "point": {"x": 150, "y": 345},
                        },
                        "routing": "manual",
                        "process_tag": "PL-1001",
                        "medium": "process",
                    },
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    return document_id


def test_drafting_report_is_read_only_and_reports_the_gate(client: TestClient):
    document_id = _seed_document(client)
    before = client.get(f"/api/v2/documents/{document_id}").json()

    response = client.post(f"/api/v2/documents/{document_id}/drafting/report", json={})

    assert response.status_code == 200, response.text
    report = response.json()
    assert report["schema"] == "pid-agent.drafting-report"
    assert report["document_id"] == document_id
    assert report["scope_kind"] == "document"
    assert report["engine_version"] >= 1
    assert report["gate"]["checked_codes"]
    assert {(port["element_id"], port["port_id"]) for port in report["ports"]} >= {
        ("pump", "discharge"),
        ("valve", "in"),
    }
    assert client.get(f"/api/v2/documents/{document_id}").json() == before


def test_drafting_report_defaults_when_the_body_is_empty(client: TestClient):
    document_id = _seed_document(client)
    response = client.post(f"/api/v2/documents/{document_id}/drafting/report")
    assert response.status_code == 200, response.text
    assert response.json()["gate"]["score"] >= 0


def test_drafting_report_on_an_unknown_document_is_404(client: TestClient):
    response = client.post("/api/v2/documents/does_not_exist/drafting/report", json={})
    assert response.status_code == 404


def test_drafting_preview_returns_a_reproducible_transaction(client: TestClient):
    document_id = _seed_document(client)
    body = {"expected_revision": 1}

    first = client.post(f"/api/v2/documents/{document_id}/drafting/preview", json=body)
    second = client.post(f"/api/v2/documents/{document_id}/drafting/preview", json=body)

    assert first.status_code == 200, first.text
    preview = first.json()
    digest = preview["reproducibility"]["transaction_digest"]
    assert digest and digest == second.json()["reproducibility"]["transaction_digest"]
    assert preview["transaction"] is not None
    assert preview["metrics"]["before"]["score"] <= preview["metrics"]["after"]["score"]
    assert preview["metrics"]["regressions"] == []
    assert preview["gate"]["passed"] is True
    # The preview is a preview: the drawing is untouched until the transaction is applied.
    assert client.get(f"/api/v2/documents/{document_id}").json()["revision"] == 1


def test_drafting_preview_rejects_a_stale_revision(client: TestClient):
    document_id = _seed_document(client)
    response = client.post(
        f"/api/v2/documents/{document_id}/drafting/preview",
        json={"expected_revision": 42},
    )
    assert response.status_code == 409


def test_applying_a_drafting_preview_through_the_governed_channel(client: TestClient):
    """The write happens on the ordinary transaction route, with its own evidence."""

    document_id = _seed_document(client)
    preview = client.post(
        f"/api/v2/documents/{document_id}/drafting/preview",
        json={"expected_revision": 1},
    ).json()
    transaction = preview["transaction"]
    assert transaction is not None

    applied = client.post(
        f"/api/v2/documents/{document_id}/transactions",
        json={
            "expected_revision": transaction["expected_revision"],
            "label": "Deterministic drafting",
            "operations": transaction["operations"],
        },
    )

    assert applied.status_code == 200, applied.text
    document = applied.json()["document"]
    assert document["revision"] == 2
    pipe = next(element for element in document["elements"] if element["id"] == "pipe")
    assert pipe["source"]["element_id"] == "pump"
    assert pipe["source"]["port_id"] == "discharge"
    assert pipe["target"]["element_id"] == "valve"
    assert pipe["target"]["port_id"] in {"in", "out"}

    trail = client.get(f"/api/v2/audit/records?document_id={document_id}").json()
    assert any(record["result_revision"] == 2 for record in trail)
    assert any(record["label"] == "Deterministic drafting" for record in trail)

    settled = client.post(
        f"/api/v2/documents/{document_id}/drafting/preview",
        json={"expected_revision": 2},
    ).json()
    assert settled["settled"] is True
    assert settled["transaction"] is None


def test_a_legacy_document_with_an_unknown_symbol_does_not_break_the_read_routes(
    client: TestClient,
):
    """The exact real-data failure: a stored drawing referencing a lost symbol key.

    The service refuses to compute a port point for such an element (correctly), so both
    read routes have to degrade honestly instead of answering 500 — this was a real 500 on
    the project's own database before the guard existed.
    """

    from agentcad.models import (
        ConnectorElement,
        ConnectorEndpoint,
        Document,
        Point,
        SymbolElement,
    )
    from agentcad.store import StoredDocument

    document = Document(
        id="doc_legacy",
        name="Legacy drawing",
        elements=[
            SymbolElement(
                id="ghost",
                symbol_key="pressure_transmitter",
                position=Point(x=400, y=300),
                width=60,
                height=50,
                label="PT-101",
            ),
            SymbolElement(
                id="pump",
                symbol_key="centrifugal_pump",
                position=Point(x=100, y=300),
                width=80,
                height=70,
                label="P-101",
            ),
            ConnectorElement(
                id="pipe",
                points=[Point(x=160, y=300), Point(x=400, y=320)],
                source=ConnectorEndpoint(
                    element_id="pump", port_id="discharge", point=Point(x=160, y=300)
                ),
                target=ConnectorEndpoint(
                    element_id="ghost", port_id="in", point=Point(x=400, y=320)
                ),
                routing="manual",
                medium="process",
            ),
        ],
    )
    # Written straight to the store: the service validates symbol keys on every write path,
    # which is exactly why a legacy drawing can only be loaded, never re-created.
    client.app_state.service.store.save(  # type: ignore[attr-defined]
        StoredDocument(document=document, undo_stack=[], redo_stack=[])
    )

    report = client.post("/api/v2/documents/doc_legacy/drafting/report", json={})
    assert report.status_code == 200, report.text
    codes = {finding["code"] for finding in report.json()["findings"]}
    assert "DRAFT_SYMBOL_DEFINITION_MISSING" in codes
    assert "DRAFT_PORT_UNRESOLVED" in codes

    preview = client.post(
        "/api/v2/documents/doc_legacy/drafting/preview",
        json={"expected_revision": 0},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["metrics"]["regressions"] == []
    assert "ghost" not in body["moved_element_ids"]
    assert body["locked_element_ids"] == []
    # And it is still a preview: nothing was written by either route.
    assert client.get("/api/v2/documents/doc_legacy").json()["revision"] == 0


def test_drafting_scope_parameters_are_honoured(client: TestClient):
    document_id = _seed_document(client)
    preview = client.post(
        f"/api/v2/documents/{document_id}/drafting/preview",
        json={
            "expected_revision": 1,
            "relayout": False,
            "locked_element_ids": ["pump"],
            "policy": {"target_score": 90},
        },
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["locks"]["request_element_ids"] == ["pump"]
    assert "pump" not in body["moved_element_ids"]
    assert body["gate"]["target_score"] == 90
