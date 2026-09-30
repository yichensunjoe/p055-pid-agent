"""M11-D4 hard locks: the read-only Cable HTTP surface + closeout evidence."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from agentcad.cable_models import CableSegment
from agentcad.cable_service import CableService
from agentcad.config import Settings
from agentcad.main import create_app
from agentcad.models import CreateDocumentRequest
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry


def _app(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "d4.db",
        cors_origins=["http://localhost:5173"],
        frontend_dist=tmp_path / "dist",
        deployment_mode="local",  # type: ignore[arg-type]
        operator_identity="李工",
    )
    return create_app(settings), settings


def _seed_cable_with_segment(store: SQLiteDocumentStore) -> tuple[str, str]:
    cable = CableService(store)
    doc = cable.create_document("seed-cable")
    view = cable.load(doc.document_id)
    updated = view.document.with_segment(
        CableSegment(id="S1", from_node="MCC-1", to_node="PMP-101", gauge="4mm2")
    )
    connection = store._connect()  # noqa: SLF001
    try:
        from datetime import UTC, datetime

        from agentcad.cable_models import serialize_cable_payload

        connection.execute(
            "UPDATE cable_documents SET revision = ?, data_json = ?, updated_at = ? WHERE document_id = ?",
            (updated.revision, serialize_cable_payload(updated), datetime.now(UTC).isoformat(), doc.document_id),
        )
        connection.commit()
    finally:
        connection.close()
    return doc.document_id, "S1"


def test_three_gets_read_only_and_projection(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    client = TestClient(app)
    # seed through a service sharing the app DB path (bootstrap data plane only)
    service = DocumentService(SQLiteDocumentStore(tmp_path / "d4.db"), SymbolRegistry())
    document_id, _ = _seed_cable_with_segment(service.store)
    service.create_document(CreateDocumentRequest(name="pid-doc", width=10, height=10))

    listing = client.get("/api/v2/cable/documents")
    assert listing.status_code == 200
    entries = listing.json()["documents"]
    assert [(e["document_id"], e["revision"]) for e in entries] == [(document_id, 1)]

    detail = client.get(f"/api/v2/cable/documents/{document_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["revision"] == 1 and body["schema"] == "pid-agent.cable-document/1"
    assert body["segments"][0]["id"] == "S1"
    assert body["readiness"]["state"] == "eligible"
    assert body["readiness"]["counts"]["blocker"] == 1
    assert body["readiness"]["result_hash"]

    # P&ID document must NOT appear in the cable listing (isolation)
    assert all(e["document_id"] != "pid-doc" for e in entries)


def test_export_byte_parity_and_error_mapping(tmp_path: Path) -> None:
    from agentcad.cable_export import export_cable_document

    app, _ = _app(tmp_path)
    client = TestClient(app)
    service = DocumentService(SQLiteDocumentStore(tmp_path / "d4.db"), SymbolRegistry())
    document_id, _ = _seed_cable_with_segment(service.store)

    response = client.get(f"/api/v2/cable/documents/{document_id}/export.zip?expected_revision=1")
    assert response.status_code == 200
    direct = export_cable_document(
        CableService(service.store), document_id, expected_revision=1
    )
    assert response.content == direct.zip_bytes  # byte-for-byte parity, no re-pack

    stale = client.get(f"/api/v2/cable/documents/{document_id}/export.zip?expected_revision=0")
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_revision"

    pid_doc = service.create_document(CreateDocumentRequest(name="p", width=10, height=10))
    cross = client.get(f"/api/v2/cable/documents/{pid_doc.id}/export.zip?expected_revision=0")
    assert cross.status_code == 404


def test_corrupt_payload_maps_to_data_integrity_500(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    client = TestClient(app)
    service = DocumentService(SQLiteDocumentStore(tmp_path / "d4.db"), SymbolRegistry())
    document_id, _ = _seed_cable_with_segment(service.store)
    connection = service.store._connect()  # noqa: SLF001
    try:
        connection.execute(
            "UPDATE cable_documents SET data_json = ? WHERE document_id = ?",
            ('{"schema": "pid-agent.cable-document/1", "segments": [{"id": "S1", "from_node": "A", "to_node": "A"}]}', document_id),
        )
        connection.commit()
    finally:
        connection.close()
    response = client.get(f"/api/v2/cable/documents/{document_id}")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "cable_data_integrity"
    export = client.get(f"/api/v2/cable/documents/{document_id}/export.zip?expected_revision=1")
    assert export.status_code == 500
    assert export.json()["detail"]["code"] == "cable_data_integrity"


def test_storage_fault_not_relabelled(tmp_path: Path, monkeypatch) -> None:
    app, _ = _app(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    service = DocumentService(SQLiteDocumentStore(tmp_path / "d4.db"), SymbolRegistry())
    document_id, _ = _seed_cable_with_segment(service.store)

    def boom(self, document_id):  # noqa: ARG001
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(SQLiteDocumentStore, "get_cable_envelope", boom)
    response = client.get(f"/api/v2/cable/documents/{document_id}")
    # FastAPI turns the unexpected storage error into a real 500; crucially it
    # is NOT relabelled as a 4xx or a data-integrity code.
    assert response.status_code == 500


def test_shared_mode_auth_boundary(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "shared.db",
        cors_origins=["http://localhost:5173"],
        frontend_dist=tmp_path / "dist",
        deployment_mode="shared",  # type: ignore[arg-type]
        api_token="deployment-token",
        operator_identity="李工",
    )
    app = create_app(settings)
    client = TestClient(app)
    # Cable GETs live behind the same request boundary as every other route:
    # unauthenticated requests are rejected, not answered anonymously.
    unauthenticated = client.get("/api/v2/cable/documents")
    assert unauthenticated.status_code in {401, 403}
    authenticated = client.get(
        "/api/v2/cable/documents",
        headers={"Authorization": "Bearer deployment-token"},
    )
    assert authenticated.status_code == 200
