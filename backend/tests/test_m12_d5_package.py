"""M12-D5 hard locks: deterministic project package + read-only surface.

Gate-frozen scope (incl. D81-1..5): explicit caller-declared member pins
(CAS boundary — the server re-reads current and compares, never rebuilding
historical revisions), frozen MANIFEST/ZIP member order with order-drift
rejection, domain declarations in the links payload, one-consistent-state
snapshot with a final re-verification gate, duplicate-member and audit-count
hard-locks, GET-only zero-audit surface with stable error codes.
"""

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

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
from agentcad.service import DocumentService
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
    store = SQLiteDocumentStore(tmp_path / "d5.db")
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


def _current_pins(store: SQLiteDocumentStore) -> dict[str, int]:
    pins: dict[str, int] = {}
    for document_id, domain, _added_at in store.list_project_documents(DEFAULT_PROJECT):
        if domain == "cable":
            pins[document_id] = int(store.get_cable_envelope(document_id)[0])
        else:
            pins[document_id] = store.get(document_id).document.revision
    return pins


def _build(tmp_path: Path, pins: dict[str, int] | None = None) -> bytes:
    store, service, cable, _pid_id, _cable_id = _seed_members(tmp_path)
    return build_project_package(
        store=store,
        pid_service=service,
        cable_service=cable,
        project_id=DEFAULT_PROJECT,
        member_pins=pins if pins is not None else _current_pins(store),
        evaluation_as_of=AS_OF,
    )


def test_package_builds_verifies_deterministic_and_order_frozen(tmp_path: Path) -> None:
    store, service, cable, _pid, _cable = _seed_members(tmp_path)
    pins = _current_pins(store)
    first = build_project_package(
        store=store,
        pid_service=service,
        cable_service=cable,
        project_id=DEFAULT_PROJECT,
        member_pins=pins,
        evaluation_as_of=AS_OF,
    )
    # F77 byte parity: same pins + same evaluation_as_of + same effective
    # server profile over the SAME project state rebuilds byte-for-byte.
    second = build_project_package(
        store=store,
        pid_service=service,
        cable_service=cable,
        project_id=DEFAULT_PROJECT,
        member_pins=pins,
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
    # D81-2 frozen order: project.json -> links -> readiness -> pid -> cable.
    assert member_paths[0] == "project.json"
    assert member_paths[1] == "links/engineering_links.json"
    assert member_paths[2] == "readiness/project_readiness.json"
    assert all(path.startswith("domains/pid/") for path in member_paths[3:-1])
    assert member_paths[-1].startswith("domains/cable/")
    assert names[1:] == member_paths  # zip entry order == manifest row order

    links = json.loads(archive.read("links/engineering_links.json"))
    assert links[0]["source_domain"] == "cable"  # D81-3
    assert links[0]["target_domain"] == "pid"
    readiness = json.loads(archive.read("readiness/project_readiness.json"))
    assert readiness["state"] == "eligible"


def test_explicit_pins_cas_boundary(tmp_path: Path) -> None:
    store, service, cable, pid_id, _cable_id = _seed_members(tmp_path)
    pins = _current_pins(store)

    # a historical/stale pin value is refused, never rebuilt
    stale_pins = dict(pins)
    stale_pins[pid_id] = pins[pid_id] - 1 if pins[pid_id] > 0 else 99
    with pytest.raises(ProjectPackageError) as exc_info:
        build_project_package(
            store=store,
            pid_service=service,
            cable_service=cable,
            project_id=DEFAULT_PROJECT,
            member_pins=stale_pins,
            evaluation_as_of=AS_OF,
        )
    assert exc_info.value.code == "member_revision_not_current"

    # pins that are not exactly the member set are refused
    with pytest.raises(ProjectPackageError) as exc_info:
        build_project_package(
            store=store,
            pid_service=service,
            cable_service=cable,
            project_id=DEFAULT_PROJECT,
            member_pins={pid_id: pins[pid_id]},
            evaluation_as_of=AS_OF,
        )
    assert exc_info.value.code == "member_pins_mismatch"
    with pytest.raises(ProjectPackageError) as exc_info:
        build_project_package(
            store=store,
            pid_service=service,
            cable_service=cable,
            project_id=DEFAULT_PROJECT,
            member_pins={**pins, "doc_stranger": 0},
            evaluation_as_of=AS_OF,
        )
    assert exc_info.value.code == "member_pins_mismatch"


def test_package_refuses_stale_link_and_unknown_project(tmp_path: Path) -> None:
    store, service, cable, _pid_id, _cable_id = _seed_members(tmp_path)
    pins = _current_pins(store)
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
            member_pins=pins,
            evaluation_as_of=AS_OF,
        )
    assert exc_info.value.code == "link_pin_not_current"

    with pytest.raises(ProjectPackageError) as exc_info:
        build_project_package(
            store=store,
            pid_service=service,
            cable_service=cable,
            project_id="proj_nope",
            member_pins=pins,
            evaluation_as_of=AS_OF,
        )
    assert exc_info.value.code == "project_not_found"


def test_final_gate_rejects_state_drift_during_build(tmp_path: Path, monkeypatch) -> None:
    """D81-4: an active-link change between the pin gate and ZIP emission
    must produce a stable refusal, never an inconsistent package."""
    store, service, cable, _pid_id, _cable_id = _seed_members(tmp_path)
    pins = _current_pins(store)
    original = store.list_active_engineering_links
    calls = {"n": 0}

    def flaky(project_id: str):
        calls["n"] += 1
        rows = original(project_id)
        if calls["n"] >= 2:
            rows = [dict(row, pinned_source_revision=42) for row in rows]
        return rows

    monkeypatch.setattr(store, "list_active_engineering_links", flaky)
    with pytest.raises(ProjectPackageError) as exc_info:
        build_project_package(
            store=store,
            pid_service=service,
            cable_service=cable,
            project_id=DEFAULT_PROJECT,
            member_pins=pins,
            evaluation_as_of=AS_OF,
        )
    assert exc_info.value.code == "package_state_changed"


def _rewrite_zip(package: bytes, transform) -> bytes:
    archive = zipfile.ZipFile(io.BytesIO(package))
    entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_STORED) as target:
        for name, data in transform(entries):
            fixed = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            fixed.compress_type = zipfile.ZIP_STORED
            fixed.extra = b""
            target.writestr(fixed, data)
    return buffer.getvalue()


def test_verify_rejects_byte_tamper(tmp_path: Path) -> None:
    package = bytearray(_build(tmp_path))
    package[100] ^= 0xFF
    with pytest.raises(ProjectPackageError, match="tamper_detected"):
        verify_project_package(bytes(package))


def test_verify_rejects_reencoded_zip_metadata(tmp_path: Path) -> None:
    package = _build(tmp_path)
    archive = zipfile.ZipFile(io.BytesIO(package))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as rewritten:
        for info in archive.infolist():
            rewritten.writestr(info.filename, archive.read(info.filename))
    with pytest.raises(ProjectPackageError, match="tamper_detected"):
        verify_project_package(buffer.getvalue())


def test_verify_rejects_missing_extra_duplicate_and_order_drift(tmp_path: Path) -> None:
    package = _build(tmp_path)

    without_project = _rewrite_zip(package, lambda entries: [e for e in entries if e[0] != "project.json"])
    with pytest.raises(ProjectPackageError, match="member mismatch"):
        verify_project_package(without_project)

    def with_rogue(entries):
        return [*entries, ("rogue.txt", b"x")]
    with pytest.raises(ProjectPackageError, match="member mismatch"):
        verify_project_package(_rewrite_zip(package, with_rogue))

    def with_duplicate(entries):
        return [entries[1], *entries]  # duplicate project.json entry
    with pytest.raises(ProjectPackageError, match="duplicate"):
        verify_project_package(_rewrite_zip(package, with_duplicate))

    def reordered(entries):
        body = [entry for entry in entries if entry[0] != "MANIFEST.json"]
        return [entries[0], *reversed(body)]  # D81-2: same set, wrong order
    with pytest.raises(ProjectPackageError, match="member order drift"):
        verify_project_package(_rewrite_zip(package, reordered))


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

    pins = _current_pins(store)
    package = client.get(
        f"/api/v2/projects/{DEFAULT_PROJECT}/package.zip",
        params={"evaluation_as_of": AS_OF_ISO, "pins": json.dumps(pins)},
    )
    assert package.status_code == 200
    verify_project_package(package.content)

    # D81-5: package GET is zero-audit (the whole surface is read-only).
    audits_before = len(client.get("/api/v2/audit/records").json())
    for path in [
        f"/api/v2/projects/{DEFAULT_PROJECT}",
        f"/api/v2/projects/{DEFAULT_PROJECT}/links",
        f"/api/v2/projects/{DEFAULT_PROJECT}/readiness?evaluation_as_of={AS_OF_ISO.replace('+', '%2B')}",
        f"/api/v2/projects/{DEFAULT_PROJECT}/package.zip?evaluation_as_of={AS_OF_ISO.replace('+', '%2B')}&pins={quote(json.dumps(pins))}",
    ]:
        response = client.get(path)
        assert response.status_code == 200, path
    audits_after = len(client.get("/api/v2/audit/records").json())
    assert audits_after == audits_before

    direct = build_project_package(
        store=store,
        pid_service=DocumentService(store, SymbolRegistry()),
        cable_service=CableService(store),
        project_id=DEFAULT_PROJECT,
        member_pins=pins,
        evaluation_as_of=AS_OF,
    )
    assert package.content == direct  # HTTP body == builder bytes

    # stale pins / pins drift -> stable 409
    stale_pins = dict(pins)
    stale_pins[cable_id] = pins[cable_id] + 1
    stale = client.get(
        f"/api/v2/projects/{DEFAULT_PROJECT}/package.zip",
        params={"evaluation_as_of": AS_OF_ISO, "pins": json.dumps(stale_pins)},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "member_revision_not_current"

    bad_pins = client.get(
        f"/api/v2/projects/{DEFAULT_PROJECT}/package.zip",
        params={"evaluation_as_of": AS_OF_ISO, "pins": json.dumps({pid_id: 0})},
    )
    assert bad_pins.status_code == 409
    assert bad_pins.json()["detail"]["code"] == "member_pins_mismatch"

    invalid = client.get(
        f"/api/v2/projects/{DEFAULT_PROJECT}/package.zip",
        params={"evaluation_as_of": AS_OF_ISO, "pins": "not-json"},
    )
    assert invalid.status_code == 422

    missing = client.get(
        "/api/v2/projects/proj_nope/package.zip",
        params={"evaluation_as_of": AS_OF_ISO, "pins": "{}"},
    )
    assert missing.status_code == 404
