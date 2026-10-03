"""M12-D5 hard locks: deterministic project package + read-only surface.

Gate-frozen scope: server-resolved current pins only (current exact
revision, no historical rebuild), stable errors and NO package on any
precondition failure, MANIFEST self-exclusion, fresh-process byte parity,
verify tamper rejection, GET-only zero-audit surface with stable error codes.
"""

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentcad.cable_models import CableSegment
from agentcad.cable_service import CableService
from agentcad.config import Settings
from agentcad.main import create_app
from agentcad.models import (
    AddElementOperation,
    CreateDocumentRequest,
    SymbolElement,
    TransactionRequest,
)
from agentcad.project_package import (
    ProjectPackageError,
    build_project_package,
    verify_project_package,
)
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry

DEFAULT_PROJECT = "proj_m12default"
AS_OF = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
AS_OF_ISO = "2026-10-03T12:00:00+00:00"


def _app(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "d5.db",
        cors_origins=["http://localhost:5173"],
        frontend_dist=tmp_path / "dist",
        deployment_mode="local",  # type: ignore[arg-type]
        operator_identity="李工",
    )
    return create_app(settings)


def _seed_members(tmp_path: Path):
    """pid (with element) + cable (with segment), both in the default project,
    plus one active link pinned at current revisions."""
    store = SQLiteDocumentStore(tmp_path / "d5.db")
    from agentcad.service import DocumentService

    service = DocumentService(store, SymbolRegistry())
    cable = CableService(store)
    pid = service.create_document(CreateDocumentRequest(name="pid", width=800, height=600))
    service.apply_transaction(
        pid.id,
        TransactionRequest(
            expected_revision=0,
            label="seed element",
            operations=[
                AddElementOperation(
                    element=SymbolElement(
                        id="PMP-101",
                        symbol_key="agitator",
                        position={"x": 100, "y": 100},
                        width=60,
                        height=40,
                        label="PMP-101",
                    )
                )
            ],
        ),
    )
    pid_revision = service.get_document(pid.id).revision
    view = cable.create_document("cable")
    updated = view.document.with_segment(
        CableSegment(id="SEG-1", from_node="MCC-1", to_node="PMP-101", gauge="4mm2")
    )
    connection = store._connect()  # noqa: SLF001
    try:
        from agentcad.cable_models import serialize_cable_payload

        connection.execute(
            "UPDATE cable_documents SET revision = ?, data_json = ?, updated_at = ? WHERE document_id = ?",
            (updated.revision, serialize_cable_payload(updated), datetime.now(UTC).isoformat(), view.document_id),
        )
        connection.execute(
            "INSERT INTO engineering_links ("
            " link_id, project_id, relation_type, source_domain, source_document_id,"
            " source_object_ref, source_endpoint, target_domain, target_document_id,"
            " target_object_ref, pinned_source_revision, pinned_target_revision,"
            " created_at, created_by"
            ") VALUES ('lnk_d5', ?, 'cable_endpoint_equipment', 'cable', ?, 'SEG-1',"
            " 'from', 'pid', ?, 'PMP-101', ?, ?, ?, 'tester')",
            (
                DEFAULT_PROJECT,
                view.document_id,
                pid.id,
                updated.revision,
                pid_revision,
                datetime.now(UTC).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    store.add_document_to_project(DEFAULT_PROJECT, pid.id, added_by="tester")
    store.add_document_to_project(DEFAULT_PROJECT, view.document_id, added_by="tester")
    return store, service, cable, pid.id, view.document_id


def _build(tmp_path: Path) -> bytes:
    store, service, cable, _pid_id, _cable_id = _seed_members(tmp_path)
    return build_project_package(
        store=store,
        pid_service=service,
        cable_service=cable,
        project_id=DEFAULT_PROJECT,
        evaluation_as_of=AS_OF,
    )


def test_package_builds_verifies_and_is_deterministic(tmp_path: Path) -> None:
    store, service, cable, _pid_id, _cable_id = _seed_members(tmp_path)
    first = build_project_package(
        store=store,
        pid_service=service,
        cable_service=cable,
        project_id=DEFAULT_PROJECT,
        evaluation_as_of=AS_OF,
    )
    # F77 byte parity: same pins + same evaluation_as_of + same effective
    # server profile over the SAME project state rebuilds byte-for-byte.
    second = build_project_package(
        store=store,
        pid_service=service,
        cable_service=cable,
        project_id=DEFAULT_PROJECT,
        evaluation_as_of=AS_OF,
    )
    assert first == second
    verify_project_package(first)

    archive = zipfile.ZipFile(io.BytesIO(first))
    names = archive.namelist()
    assert names[0] == "MANIFEST.json"
    manifest = json.loads(archive.read("MANIFEST.json"))
    member_paths = [member["path"] for member in manifest["members"]]
    assert "MANIFEST.json" not in member_paths  # self-exclusion
    assert "project.json" in member_paths
    assert "links/engineering_links.json" in member_paths
    assert "readiness/project_readiness.json" in member_paths
    assert any(path.startswith("domains/pid/") for path in member_paths)
    assert any(path.startswith("domains/cable/") for path in member_paths)
    readiness = json.loads(archive.read("readiness/project_readiness.json"))
    assert readiness["state"] == "eligible"


def test_package_refuses_stale_link_and_unknown_project(tmp_path: Path) -> None:
    store, service, cable, pid_id, cable_id = _seed_members(tmp_path)
    connection = store._connect()  # noqa: SLF001
    try:
        connection.execute(
            "UPDATE engineering_links SET pinned_source_revision = 99 WHERE link_id = 'lnk_d5'"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ProjectPackageError) as exc_info:
        build_project_package(
            store=store,
            pid_service=service,
            cable_service=cable,
            project_id=DEFAULT_PROJECT,
            evaluation_as_of=AS_OF,
        )
    assert exc_info.value.code == "link_pin_not_current"

    with pytest.raises(ProjectPackageError) as exc_info:
        build_project_package(
            store=store,
            pid_service=service,
            cable_service=cable,
            project_id="proj_nope",
            evaluation_as_of=AS_OF,
        )
    assert exc_info.value.code == "project_not_found"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data[:100] + bytes([data[100] ^ 0xFF]) + data[101:],  # flipped byte
    ],
)
def test_verify_rejects_byte_tamper(tmp_path: Path, mutate) -> None:
    package = _build(tmp_path)
    with pytest.raises(ProjectPackageError, match="tamper_detected"):
        verify_project_package(mutate(bytearray(package)))


def test_verify_rejects_reencoded_zip_metadata(tmp_path: Path) -> None:
    package = _build(tmp_path)
    archive = zipfile.ZipFile(io.BytesIO(package))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as rewritten:
        for info in archive.infolist():
            rewritten.writestr(info.filename, archive.read(info.filename))
    with pytest.raises(ProjectPackageError, match="tamper_detected"):
        verify_project_package(buffer.getvalue())


def test_verify_rejects_missing_and_extra_members(tmp_path: Path) -> None:
    package = _build(tmp_path)
    archive = zipfile.ZipFile(io.BytesIO(package))
    kept = [(i, archive.read(i.filename)) for i in archive.infolist() if i.filename != "project.json"]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_STORED) as missing:
        for info, data in kept:
            fixed = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            fixed.compress_type = zipfile.ZIP_STORED
            fixed.extra = b""
            missing.writestr(fixed, data)
    with pytest.raises(ProjectPackageError, match="member mismatch"):
        verify_project_package(buffer.getvalue())

    archive = zipfile.ZipFile(io.BytesIO(package))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_STORED) as extra:
        for info in archive.infolist():
            fixed = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            fixed.compress_type = zipfile.ZIP_STORED
            fixed.extra = b""
            extra.writestr(fixed, archive.read(info.filename))
        rogue = zipfile.ZipInfo("rogue.txt", date_time=(1980, 1, 1, 0, 0, 0))
        rogue.compress_type = zipfile.ZIP_STORED
        extra.writestr(rogue, b"x")
    with pytest.raises(ProjectPackageError, match="member mismatch"):
        verify_project_package(buffer.getvalue())


def test_api_surface_read_only_and_stable_errors(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = TestClient(app)
    store, _service, _cable, pid_id, cable_id = _seed_members(tmp_path)

    summary = client.get(f"/api/v2/projects/{DEFAULT_PROJECT}")
    assert summary.status_code == 200
    body = summary.json()
    assert {m["domain"] for m in body["members"]} == {"pid", "cable"}
    assert all(m["revision"] is not None for m in body["members"])

    links = client.get(f"/api/v2/projects/{DEFAULT_PROJECT}/links")
    assert links.status_code == 200
    assert [link["link_id"] for link in links.json()["links"]] == ["lnk_d5"]

    readiness = client.get(
        f"/api/v2/projects/{DEFAULT_PROJECT}/readiness",
        params={"evaluation_as_of": AS_OF_ISO},
    )
    assert readiness.status_code == 200
    assert readiness.json()["state"] == "eligible"

    assert client.get(f"/api/v2/projects/{DEFAULT_PROJECT}/readiness").status_code == 422
    naive = client.get(
        f"/api/v2/projects/{DEFAULT_PROJECT}/readiness",
        params={"evaluation_as_of": "2026-10-03T12:00:00"},
    )
    assert naive.status_code == 422

    package = client.get(
        f"/api/v2/projects/{DEFAULT_PROJECT}/package.zip",
        params={"evaluation_as_of": AS_OF_ISO},
    )
    assert package.status_code == 200
    verify_project_package(package.content)
    direct = build_project_package(
        store=store,
        pid_service=__import__("agentcad.service", fromlist=["DocumentService"]).DocumentService(
            store, __import__("agentcad.symbols", fromlist=["SymbolRegistry"]).SymbolRegistry()
        ),
        cable_service=CableService(store),
        project_id=DEFAULT_PROJECT,
        evaluation_as_of=AS_OF,
    )
    assert package.content == direct  # HTTP body == builder bytes

    stale = client.get(
        f"/api/v2/projects/{DEFAULT_PROJECT}/package.zip",
        params={"evaluation_as_of": AS_OF_ISO},
    )
    connection = store._connect()  # noqa: SLF001
    try:
        connection.execute(
            "UPDATE cable_documents SET revision = revision + 1 WHERE document_id = ?",
            (cable_id,),
        )
        connection.commit()
    finally:
        connection.close()
    stale = client.get(
        f"/api/v2/projects/{DEFAULT_PROJECT}/package.zip",
        params={"evaluation_as_of": AS_OF_ISO},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "link_pin_not_current"

    missing = client.get(
        "/api/v2/projects/proj_nope/package.zip",
        params={"evaluation_as_of": AS_OF_ISO},
    )
    assert missing.status_code == 404
