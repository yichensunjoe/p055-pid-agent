"""M11-D3 hard locks: cable validator profile + deterministic export."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentcad.cable_export import (
    CableExportError,
    export_cable_document,
    verify_cable_artifact,
)
from agentcad.cable_service import CableService
from agentcad.cable_validation import (
    CableValidationError,
    assess,
    content_hash_of,
)
from agentcad.models import CreateDocumentRequest
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry


def _service(tmp_path: Path, name: str = "d3.db"):
    store = SQLiteDocumentStore(tmp_path / name)
    return store, DocumentService(store, SymbolRegistry()), CableService(store)


def _with_segment(cable: CableService, document_id: str, segment_id: str, gauge: str = "2.5mm2"):
    from agentcad.cable_models import CableSegment

    view = cable.load(document_id)
    updated = view.document.with_segment(
        CableSegment(id=segment_id, from_node="A", to_node="B", gauge=gauge)
    )
    return updated


def test_two_rules_pass_eligible(tmp_path: Path) -> None:
    _, _, cable = _service(tmp_path)
    doc = cable.create_document("ok")
    cable.store  # noqa: B018
    updated = _with_segment(cable, doc.document_id, "S1")
    # persist via store directly (D3 is read-only; use the envelope write primitive path)
    _persist(cable, doc.document_id, updated)
    readiness = assess(cable.load(doc.document_id))
    assert readiness.eligible
    assert {r.rule_id for r in readiness.rule_results} == {
        "cable.gauge_grammar",
        "cable.document_non_empty",
    }


def _persist(cable: CableService, document_id: str, document) -> None:
    from datetime import UTC, datetime

    from agentcad.cable_models import serialize_cable_payload

    connection = cable.store._connect()  # noqa: SLF001
    try:
        connection.execute(
            "UPDATE cable_documents SET revision = ?, data_json = ?, updated_at = ? "
            "WHERE document_id = ?",
            (
                document.revision,
                serialize_cable_payload(document),
                datetime.now(UTC).isoformat(),
                document_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_gauge_violations_block(tmp_path: Path) -> None:
    _, _, cable = _service(tmp_path)
    doc = cable.create_document("g")
    _persist(cable, doc.document_id, _with_segment(cable, doc.document_id, "S1", "2.5AWG12"))
    readiness = assess(cable.load(doc.document_id))
    assert not readiness.eligible
    gauge = next(r for r in readiness.rule_results if r.rule_id == "cable.gauge_grammar")
    assert not gauge.passed and "S1" in gauge.detail

    for bad in ("0mm2", "AWG99", "2.123456mm2"):
        doc2 = cable.create_document("g2")
        _persist(cable, doc2.document_id, _with_segment(cable, doc2.document_id, "S1", bad))
        result = assess(cable.load(doc2.document_id))
        assert not result.eligible, bad


def test_empty_document_warning_but_not_eligible(tmp_path: Path) -> None:
    _, _, cable = _service(tmp_path)
    doc = cable.create_document("empty")
    readiness = assess(cable.load(doc.document_id))
    warning = next(r for r in readiness.rule_results if r.rule_id == "cable.document_non_empty")
    assert warning.severity == "warning" and not warning.passed
    assert not readiness.eligible


def test_unknown_rule_and_profile_fail_closed(tmp_path: Path) -> None:
    _, _, cable = _service(tmp_path)
    doc = cable.create_document("u")
    with pytest.raises(CableValidationError) as excinfo:
        assess(cable.load(doc.document_id), profile_version=2)
    assert excinfo.value.code == "unsupported_profile_version"
    from agentcad.cable_validation import _assess_rules

    with pytest.raises(CableValidationError) as excinfo:
        _assess_rules(
            cable.load(doc.document_id),
            active=("cable.unknown_rule",),
            profile_version=1,
        )
    assert excinfo.value.code == "unknown_rule"


def test_hashes_deterministic_and_revision_bound(tmp_path: Path) -> None:
    _, _, cable = _service(tmp_path)
    doc = cable.create_document("h")
    first = assess(cable.load(doc.document_id))
    second = assess(cable.load(doc.document_id))
    assert first.result_hash == second.result_hash
    assert first.content_hash == content_hash_of(cable.load(doc.document_id).document, 0)
    _persist(cable, doc.document_id, _with_segment(cable, doc.document_id, "S1"))
    third = assess(cable.load(doc.document_id))
    assert third.result_hash != first.result_hash
    assert third.content_hash != first.content_hash


def test_export_members_manifest_and_tamper(tmp_path: Path) -> None:
    import zipfile
    from io import BytesIO

    _, _, cable = _service(tmp_path)
    doc = cable.create_document("e")
    artifact = export_cable_document(cable, doc.document_id, expected_revision=0)
    with zipfile.ZipFile(BytesIO(artifact.zip_bytes)) as archive:
        assert sorted(archive.namelist()) == ["MANIFEST.sha256", "cable-document.json"]
        manifest = archive.read("MANIFEST.sha256").decode("utf-8")
    assert len(manifest.splitlines()) == 1
    assert manifest.endswith("  cable-document.json\n")
    verify_cable_artifact(artifact.zip_bytes)

    tampered = bytearray(artifact.zip_bytes)
    tampered[-3] ^= 0xFF
    with pytest.raises(CableExportError):
        verify_cable_artifact(bytes(tampered))


def test_export_stale_revision_typed_error(tmp_path: Path) -> None:
    _, _, cable = _service(tmp_path)
    doc = cable.create_document("s")
    with pytest.raises(CableExportError) as excinfo:
        export_cable_document(cable, doc.document_id, expected_revision=7)
    assert excinfo.value.code == "stale_revision"


def test_cross_domain_fail_closed_and_pid_zero_change(tmp_path: Path) -> None:
    store, service, cable = _service(tmp_path)
    pid_doc = service.create_document(CreateDocumentRequest(name="p", width=10, height=10))
    before = service.get_document(pid_doc.id).model_dump()
    audits_before = len(store.all_audit_records())
    with pytest.raises(CableExportError) as excinfo:
        export_cable_document(cable, pid_doc.id, expected_revision=0)
    assert excinfo.value.code == "cable_document_not_found"
    from agentcad.cable_service import CableDocumentNotFoundError

    with pytest.raises(CableDocumentNotFoundError):
        assess(cable.load(pid_doc.id))
    assert service.get_document(pid_doc.id).model_dump() == before
    assert len(store.all_audit_records()) == audits_before


def test_payload_change_changes_artifact(tmp_path: Path) -> None:
    _, _, cable = _service(tmp_path)
    doc = cable.create_document("c")
    first = export_cable_document(cable, doc.document_id, expected_revision=0)
    _persist(cable, doc.document_id, _with_segment(cable, doc.document_id, "S1"))
    second = export_cable_document(cable, doc.document_id, expected_revision=1)
    assert first.artifact_sha256 != second.artifact_sha256


# ---- R75-3: ZIP metadata contract lock ----

def test_zip_metadata_contract(tmp_path: Path) -> None:
    import zipfile
    from io import BytesIO

    _, _, cable = _service(tmp_path)
    doc = cable.create_document("zip")
    artifact = export_cable_document(cable, doc.document_id, expected_revision=0)
    with zipfile.ZipFile(BytesIO(artifact.zip_bytes)) as archive:
        infos = {info.filename: info for info in archive.infolist()}
        assert archive.namelist() == ["cable-document.json", "MANIFEST.sha256"]  # insertion order
        for info in infos.values():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.create_system == 0
            assert info.external_attr == 0
            assert info.extra == b""
            assert not info.comment
            assert info.compress_type == zipfile.ZIP_STORED


# ---- R75-4: full-bytes cross-process reproducibility (no hardcoded paths) ----

def test_cross_process_full_bytes(tmp_path: Path) -> None:
    import os

    _, _, cable = _service(tmp_path)
    doc = cable.create_document("x")
    script = (
        "import sys\n"
        "from agentcad.cable_export import export_cable_document\n"
        "from agentcad.cable_service import CableService\n"
        "from agentcad.store import SQLiteDocumentStore\n"
        f"service = CableService(SQLiteDocumentStore(r'{tmp_path}/d3.db'))\n"
        f"artifact = export_cable_document(service, '{doc.document_id}', expected_revision=0)\n"
        "sys.stdout.buffer.write(artifact.zip_bytes)\n"
    )
    env = dict(os.environ)
    backend = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = backend + os.pathsep + env.get("PYTHONPATH", "")
    outputs = []
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            timeout=60,
            env=env,
        )
        assert result.returncode == 0, result.stderr.decode()
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    import hashlib

    assert hashlib.sha256(outputs[0]).hexdigest() == hashlib.sha256(outputs[1]).hexdigest()


# ---- R75-5: targeted member tamper ----

def test_targeted_member_tamper(tmp_path: Path) -> None:
    import zipfile
    from io import BytesIO

    _, _, cable = _service(tmp_path)
    doc = cable.create_document("t")
    artifact = export_cable_document(cable, doc.document_id, expected_revision=0)

    from agentcad.cable_export import _force_zero_external_attributes, _zipinfo

    def rebuild(modify) -> bytes:
        with zipfile.ZipFile(BytesIO(artifact.zip_bytes)) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members = modify(members)
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.comment = b""
            for name in ("cable-document.json", "MANIFEST.sha256"):
                archive.writestr(_zipinfo(name), members[name])
        return _force_zero_external_attributes(buffer.getvalue())

    def tamper_document(members):
        members["cable-document.json"] = members["cable-document.json"].replace(b'"name"', b'"nane"')
        return members

    def tamper_manifest(members):
        manifest = members["MANIFEST.sha256"]
        flip = b"1" if manifest[:1] == b"0" else b"0"
        members["MANIFEST.sha256"] = flip + manifest[1:]
        return members

    with pytest.raises(CableExportError) as excinfo:
        verify_cable_artifact(rebuild(tamper_document))
    assert excinfo.value.code == "artifact_corrupt"
    assert "document hash mismatch" in str(excinfo.value)
    with pytest.raises(CableExportError) as excinfo:
        verify_cable_artifact(rebuild(tamper_manifest))
    assert excinfo.value.code == "artifact_corrupt"
    assert "document hash mismatch" in str(excinfo.value)


# ---- R75-6: reverse cross-domain + P&ID zero-change during cable validate/export ----

def test_reverse_cross_domain_and_pid_zero_change(tmp_path: Path) -> None:
    store, service, cable = _service(tmp_path)
    pid_doc = service.create_document(CreateDocumentRequest(name="p", width=10, height=10))
    cable_doc = cable.create_document("c")
    from agentcad.cable_models import CableSegment

    view = cable.load(cable_doc.document_id)
    _persist(
        cable,
        cable_doc.document_id,
        view.document.with_segment(CableSegment(id="S1", from_node="A", to_node="B")),
    )
    # cable id -> P&ID plane fail-closed
    from agentcad.service import DocumentNotFoundError

    with pytest.raises(DocumentNotFoundError):
        service.get_document(cable_doc.document_id)
    # successful cable validate + export leaves the P&ID document untouched
    pid_before = service.get_document(pid_doc.id).model_dump()
    audits_before = len(store.all_audit_records())
    from agentcad.release_validator import assess_document_release_readiness
    from agentcad.validation_profile import load_profile

    with pytest.raises(DocumentNotFoundError):
        assess_document_release_readiness(
            service, cable_doc.document_id, load_profile()
        )
    readiness = assess(cable.load(cable_doc.document_id))
    assert readiness.state == "eligible"
    artifact = export_cable_document(cable, cable_doc.document_id, expected_revision=1)
    assert artifact.readiness.state == "eligible"
    assert service.get_document(pid_doc.id).model_dump() == pid_before
    assert len(store.all_audit_records()) == audits_before


# ---- R75-7: corrupt payload maps to typed error ----

def test_corrupt_payload_typed_error(tmp_path: Path) -> None:
    _, _, cable = _service(tmp_path)
    doc = cable.create_document("corrupt")
    connection = cable.store._connect()  # noqa: SLF001
    try:
        connection.execute(
            "UPDATE cable_documents SET data_json = ? WHERE document_id = ?",
            ('{"schema": "pid-agent.cable-document/1", "segments": [{"id": "S1", "from_node": "A", "to_node": "A"}], "name": 42}', doc.document_id),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(CableExportError) as excinfo:
        export_cable_document(cable, doc.document_id, expected_revision=0)
    assert excinfo.value.code == "invalid_cable_payload"
    from agentcad.cable_validation import assess_cable_document

    with pytest.raises(CableValidationError) as excinfo:
        assess_cable_document(cable, doc.document_id)
    assert excinfo.value.code == "invalid_cable_payload"


def test_storage_failure_not_relabelled(tmp_path: Path, monkeypatch) -> None:
    """R75-7: a SQLite/storage failure keeps its truth — never reported as an
    invalid payload."""
    _, _, cable = _service(tmp_path)
    doc = cable.create_document("db")

    def boom(document_id):
        import sqlite3

        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(cable.store, "get_cable_envelope", boom)
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        export_cable_document(cable, doc.document_id, expected_revision=0)
    from agentcad.cable_validation import assess_cable_document

    with pytest.raises(sqlite3.OperationalError):
        assess_cable_document(cable, doc.document_id)
