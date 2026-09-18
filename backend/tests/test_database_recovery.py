from __future__ import annotations

import gc
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import warnings
import zipfile
from pathlib import Path

import pytest

from agentcad.database_recovery import (
    BACKUP_DATABASE_MEMBER,
    BACKUP_METADATA_MEMBER,
    CURRENT_SCHEMA_VERSION,
    BackupValidationError,
    DatabaseIntegrityError,
    DatabaseRecoveryError,
    DatabaseVersionError,
    RestoreInstanceMismatchError,
    UnsafeDatabaseTargetError,
    create_backup,
    database_info,
    inspect_backup,
    restore_backup,
)
from agentcad.models import CreateDocumentRequest
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry


def _service(path: Path) -> DocumentService:
    return DocumentService(SQLiteDocumentStore(path), SymbolRegistry())


def _sidecars(path: Path) -> list[Path]:
    return [Path(f"{path}-wal"), Path(f"{path}-shm")]


def _prepare_for_restore(path: Path) -> None:
    gc.collect()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    gc.collect()
    for sidecar in _sidecars(path):
        sidecar.unlink(missing_ok=True)


def test_new_database_has_version_and_persistent_instance_identity(tmp_path: Path):
    source = tmp_path / "source.db"
    store = SQLiteDocumentStore(source)
    first_id = store.database_instance_id

    moved = tmp_path / "moved.db"
    shutil.copyfile(source, moved)

    source_info = database_info(source)
    moved_info = database_info(moved)
    assert source_info.schema_version == CURRENT_SCHEMA_VERSION
    assert moved_info.schema_version == CURRENT_SCHEMA_VERSION
    assert source_info.instance_id == moved_info.instance_id == first_id
    assert len(first_id) == 32


def test_unversioned_legacy_database_is_migrated_without_losing_rows(tmp_path: Path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE documents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                revision INTEGER NOT NULL,
                data_json TEXT NOT NULL,
                undo_json TEXT NOT NULL,
                redo_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE document_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                action TEXT NOT NULL,
                label TEXT NOT NULL,
                operation_count INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents (
                id, name, revision, data_json, undo_json, redo_json, created_at, updated_at
            ) VALUES ('doc-legacy', 'Legacy', 0, '{}', '[]', '[]', '2026-01-01', '2026-01-01')
            """
        )

    SQLiteDocumentStore(database)

    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(document_history)")}
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        document_count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        metadata_count = connection.execute(
            "SELECT COUNT(*) FROM database_metadata WHERE singleton_id = 1"
        ).fetchone()[0]
    assert version == CURRENT_SCHEMA_VERSION
    assert "details_json" in columns
    assert {"agent_sessions", "agent_approvals", "agent_tool_calls"}.issubset(tables)
    assert document_count == 1
    assert metadata_count == 1


def test_v3_database_gains_audit_tables_and_a_genesis_chain(tmp_path: Path):
    """A pre-T0.5 (schema v3) database migrates in place and starts an empty chain."""

    from agentcad.audit import AuditRecorder
    from agentcad.store import SQLiteDocumentStore as _Store

    database = tmp_path / "v3.db"
    SQLiteDocumentStore(database)
    with sqlite3.connect(database) as connection:
        # A genuine v3 database has neither the audit chain (v4) nor the derived
        # project index (v5); claim exactly v3 so both migrations must run.
        connection.execute("DROP TABLE audit_records")
        connection.execute("DROP TABLE project_index")
        connection.execute("PRAGMA user_version=3")

    store = _Store(database)
    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert version == CURRENT_SCHEMA_VERSION
    assert {"audit_records", "project_index"}.issubset(tables)

    # Nothing is back-filled (the past cannot be attested retroactively), but the
    # chain is empty-and-valid and the next write starts it at ordinal 1.
    recorder = AuditRecorder(store=store, symbols=SymbolRegistry())
    verification = recorder.verify_chain()
    assert verification.ok is True
    assert verification.record_count == 0

    service = DocumentService(store, SymbolRegistry())
    document = service.create_document(CreateDocumentRequest(name="Migrated"))
    records = store.all_audit_records()
    assert [record.ordinal for record in records] == [1]
    assert records[0].document_id == document.id
    assert service.audit.verify_chain().ok is True


def test_v4_database_gains_the_derived_project_index(tmp_path: Path):
    """A pre-M2 (schema v4) database gains the derived index and can rebuild it."""

    from agentcad.project_index import ProjectIndexService

    database = tmp_path / "v4.db"
    SQLiteDocumentStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE project_index")
        connection.execute("PRAGMA user_version=4")

    store = SQLiteDocumentStore(database)
    service = DocumentService(store, SymbolRegistry())
    document = service.create_document(CreateDocumentRequest(name="Legacy drawing"))
    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        index_rows = connection.execute("SELECT COUNT(*) FROM project_index").fetchone()[0]
    assert version == CURRENT_SCHEMA_VERSION
    # The index is derived data: migration creates the cache empty instead of
    # guessing what the graphs used to be.
    assert index_rows == 0

    project_index = ProjectIndexService(store, SymbolRegistry())
    assert project_index.list_entries() == []
    report = project_index.rebuild_all()
    assert report.rebuilt == [document.id]
    assert report.removed == []
    assert store.get_project_index(document.id) is not None
    assert service.audit.verify_chain().ok is True


def test_backup_and_restore_preserve_the_audit_chain(tmp_path: Path):
    from agentcad.audit import request_audit_context
    from agentcad.models import TransactionRequest

    database = tmp_path / "source.db"
    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    document = service.create_document(CreateDocumentRequest(name="Audited"))
    service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=document.revision,
            label="Add pump",
            operations=[
                {
                    "op": "add_element",
                    "element": {
                        "type": "symbol",
                        "symbol_key": "centrifugal_pump",
                        "position": {"x": 10.0, "y": 20.0},
                        "width": 60.0,
                        "height": 40.0,
                    },
                }
            ],
        ),
        audit=request_audit_context("apply_web_transaction"),
    )
    before = service.audit.verify_chain()

    backup = tmp_path / "snapshot.pidbak"
    create_backup(database, backup)
    restored_path = tmp_path / "restored.db"
    restore_backup(
        backup,
        restored_path,
        expected_instance_id=database_info(database).instance_id,
    )

    restored = DocumentService(SQLiteDocumentStore(restored_path), SymbolRegistry())
    after = restored.audit.verify_chain()
    assert after.ok is True
    assert after.record_count == before.record_count
    assert [record.record_hash for record in restored.store.all_audit_records()] == [
        record.record_hash for record in service.store.all_audit_records()
    ]


def test_newer_database_version_is_rejected(tmp_path: Path):
    database = tmp_path / "future.db"
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION + 1}")

    with pytest.raises(DatabaseVersionError) as error:
        SQLiteDocumentStore(database)
    assert error.value.code == "unsupported_database_version"


def test_online_backup_captures_committed_state_while_source_connection_is_open(tmp_path: Path):
    database = tmp_path / "active.db"
    service = _service(database)
    document = service.create_document(CreateDocumentRequest(name="Before backup"))

    active_connection = sqlite3.connect(database)
    try:
        active_connection.execute("PRAGMA journal_mode=WAL")
        active_connection.execute(
            "UPDATE documents SET name = ? WHERE id = ?", ("Committed during activity", document.id)
        )
        active_connection.commit()

        backup = tmp_path / "active.pidbak"
        result = create_backup(database, backup)
    finally:
        active_connection.close()

    restored = tmp_path / "restored.db"
    restore_backup(backup, restored, expected_instance_id=result.metadata.instance_id)
    with sqlite3.connect(restored) as connection:
        name = connection.execute(
            "SELECT name FROM documents WHERE id = ?", (document.id,)
        ).fetchone()[0]
    assert name == "Committed during activity"


def test_backup_metadata_and_payload_are_verified(tmp_path: Path):
    database = tmp_path / "source.db"
    _service(database).create_document(CreateDocumentRequest(name="Verified"))
    backup = tmp_path / "source.pidbak"

    result = create_backup(database, backup)
    metadata = inspect_backup(backup, verify_database_payload=True)

    assert metadata == result.metadata
    assert metadata.format == "pid-agent.sqlite-backup"
    assert metadata.schema_version == CURRENT_SCHEMA_VERSION
    assert metadata.database_size_bytes > 0
    assert len(metadata.database_sha256) == 64
    with zipfile.ZipFile(backup) as archive:
        assert set(archive.namelist()) == {BACKUP_DATABASE_MEMBER, BACKUP_METADATA_MEMBER}


def test_backup_does_not_capture_provider_or_environment_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret = "sk-provider-secret-must-not-appear"
    monkeypatch.setenv("PID_AGENT_LLM_API_KEY", secret)
    monkeypatch.setenv("PID_AGENT_API_TOKEN", "service-token-must-not-appear")
    database = tmp_path / "source.db"
    _service(database).create_document(CreateDocumentRequest(name="No credentials"))
    backup = tmp_path / "source.pidbak"

    create_backup(database, backup)

    assert secret.encode() not in backup.read_bytes()
    assert b"service-token-must-not-appear" not in backup.read_bytes()


def test_tampered_backup_database_is_rejected(tmp_path: Path):
    database = tmp_path / "source.db"
    SQLiteDocumentStore(database)
    backup = tmp_path / "source.pidbak"
    create_backup(database, backup)

    tampered = tmp_path / "tampered.pidbak"
    with zipfile.ZipFile(backup) as source, zipfile.ZipFile(tampered, "w") as destination:
        metadata = source.read(BACKUP_METADATA_MEMBER)
        database_bytes = bytearray(source.read(BACKUP_DATABASE_MEMBER))
        database_bytes[-1] ^= 0xFF
        destination.writestr(BACKUP_METADATA_MEMBER, metadata)
        destination.writestr(BACKUP_DATABASE_MEMBER, database_bytes)

    with pytest.raises(BackupValidationError) as error:
        inspect_backup(tampered)
    assert "SHA-256" in str(error.value)


def test_duplicate_backup_member_is_rejected(tmp_path: Path):
    backup = tmp_path / "duplicate.pidbak"
    metadata = {
        "format": "pid-agent.sqlite-backup",
        "version": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "instance_id": "a" * 32,
        "database_sha256": "0" * 64,
        "database_size_bytes": 1,
        "source_database_name": "source.db",
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(backup, "w") as archive:
            archive.writestr(BACKUP_METADATA_MEMBER, json.dumps(metadata))
            archive.writestr(BACKUP_DATABASE_MEMBER, b"a")
            archive.writestr(BACKUP_DATABASE_MEMBER, b"b")

    with pytest.raises(BackupValidationError, match="duplicate"):
        inspect_backup(backup)


def test_wrong_instance_restore_is_rejected_without_touching_target(tmp_path: Path):
    source = tmp_path / "source.db"
    _service(source).create_document(CreateDocumentRequest(name="Source"))
    backup = tmp_path / "source.pidbak"
    create_backup(source, backup)

    target = tmp_path / "target.db"
    _service(target).create_document(CreateDocumentRequest(name="Target"))
    _prepare_for_restore(target)
    before = target.read_bytes()

    with pytest.raises(RestoreInstanceMismatchError) as error:
        restore_backup(backup, target)

    assert error.value.code == "restore_instance_mismatch"
    assert target.read_bytes() == before


def test_missing_target_requires_explicit_backup_instance_confirmation(tmp_path: Path):
    source = tmp_path / "source.db"
    SQLiteDocumentStore(source)
    backup = tmp_path / "source.pidbak"
    result = create_backup(source, backup)
    target = tmp_path / "missing.db"

    with pytest.raises(RestoreInstanceMismatchError) as error:
        restore_backup(backup, target)
    assert error.value.code == "restore_instance_confirmation_required"
    assert not target.exists()

    restore_backup(backup, target, expected_instance_id=result.metadata.instance_id)
    assert database_info(target).instance_id == result.metadata.instance_id


def test_corrupt_target_requires_explicit_backup_instance_confirmation(tmp_path: Path):
    source = tmp_path / "source.db"
    SQLiteDocumentStore(source)
    backup = tmp_path / "source.pidbak"
    result = create_backup(source, backup)
    target = tmp_path / "corrupt.db"
    target.write_bytes(b"not sqlite")

    with pytest.raises(RestoreInstanceMismatchError) as error:
        restore_backup(backup, target)
    assert error.value.code == "restore_instance_confirmation_required"
    assert target.read_bytes() == b"not sqlite"

    restore_backup(backup, target, expected_instance_id=result.metadata.instance_id)
    assert database_info(target).instance_id == result.metadata.instance_id


def test_intentional_instance_override_requires_matching_confirmation(tmp_path: Path):
    source = tmp_path / "source.db"
    SQLiteDocumentStore(source)
    backup = tmp_path / "source.pidbak"
    result = create_backup(source, backup)
    target = tmp_path / "target.db"
    SQLiteDocumentStore(target)

    with pytest.raises(RestoreInstanceMismatchError) as error:
        restore_backup(backup, target, allow_instance_mismatch=True)
    assert error.value.code == "restore_instance_confirmation_required"

    restore_backup(
        backup,
        target,
        expected_instance_id=result.metadata.instance_id,
        allow_instance_mismatch=True,
    )
    assert database_info(target).instance_id == result.metadata.instance_id


def test_restore_rolls_back_to_backup_state_for_same_instance(tmp_path: Path):
    database = tmp_path / "source.db"
    service = _service(database)
    original = service.create_document(CreateDocumentRequest(name="Original"))
    backup = tmp_path / "source.pidbak"
    create_backup(database, backup)
    service.create_document(CreateDocumentRequest(name="Later"))
    assert database_info(database).document_count == 2
    _prepare_for_restore(database)

    restore_backup(backup, database)

    restored = _service(database)
    assert [item.id for item in restored.list_documents()] == [original.id]


def test_atomic_replace_failure_leaves_original_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database = tmp_path / "source.db"
    service = _service(database)
    service.create_document(CreateDocumentRequest(name="Original"))
    backup = tmp_path / "source.pidbak"
    create_backup(database, backup)
    service.create_document(CreateDocumentRequest(name="Later"))
    _prepare_for_restore(database)
    before = database.read_bytes()

    def fail_replace(source: os.PathLike[str] | str, target: os.PathLike[str] | str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("agentcad.database_recovery.os.replace", fail_replace)
    with pytest.raises(DatabaseRecoveryError, match="simulated") as error:
        restore_backup(backup, database)
    assert error.value.code == "restore_replace_failed"
    assert database.read_bytes() == before
    assert not list(tmp_path.glob(".source.db.restore-*.sqlite3"))


def test_restore_rejects_symlink_and_sqlite_sidecar_targets(tmp_path: Path):
    source = tmp_path / "source.db"
    SQLiteDocumentStore(source)
    backup = tmp_path / "source.pidbak"
    result = create_backup(source, backup)

    real_target = tmp_path / "real.db"
    SQLiteDocumentStore(real_target)
    _prepare_for_restore(real_target)
    symlink_target = tmp_path / "link.db"
    try:
        symlink_target.symlink_to(real_target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(UnsafeDatabaseTargetError):
        restore_backup(
            backup,
            symlink_target,
            expected_instance_id=result.metadata.instance_id,
            allow_instance_mismatch=True,
        )

    for sidecar in _sidecars(real_target):
        sidecar.write_bytes(b"active")
        break
    with pytest.raises(UnsafeDatabaseTargetError) as error:
        restore_backup(
            backup,
            real_target,
            expected_instance_id=result.metadata.instance_id,
            allow_instance_mismatch=True,
        )
    assert error.value.code == "database_may_be_active"


def test_cli_info_backup_and_restore_end_to_end(tmp_path: Path):
    backend_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(backend_root)
    database = tmp_path / "cli.db"
    backup = tmp_path / "cli.pidbak"

    info_command = [
        sys.executable,
        "-m",
        "agentcad.cli",
        "db",
        "info",
        "--database",
        str(database),
    ]
    info_result = subprocess.run(
        info_command, env=environment, text=True, capture_output=True, check=False
    )
    assert info_result.returncode == 0, info_result.stderr
    info_payload = json.loads(info_result.stdout)
    instance_id = info_payload["database"]["instance_id"]

    backup_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentcad.cli",
            "db",
            "backup",
            "--database",
            str(database),
            "--output",
            str(backup),
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert backup_result.returncode == 0, backup_result.stderr
    assert json.loads(backup_result.stdout)["backup"]["metadata"]["instance_id"] == instance_id

    database.unlink()
    failed_restore = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentcad.cli",
            "db",
            "restore",
            "--database",
            str(database),
            "--input",
            str(backup),
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert failed_restore.returncode == 2
    assert json.loads(failed_restore.stderr)["error"]["code"] == (
        "restore_instance_confirmation_required"
    )

    restore_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentcad.cli",
            "db",
            "restore",
            "--database",
            str(database),
            "--input",
            str(backup),
            "--expect-instance-id",
            instance_id,
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert restore_result.returncode == 0, restore_result.stderr
    assert json.loads(restore_result.stdout)["restore"]["metadata"]["instance_id"] == instance_id


def test_invalid_sqlite_backup_payload_reports_integrity_error(tmp_path: Path):
    source = tmp_path / "source.db"
    SQLiteDocumentStore(source)
    backup = tmp_path / "source.pidbak"
    result = create_backup(source, backup)

    broken_database = tmp_path / "broken.db"
    broken_database.write_bytes(b"not a sqlite database")
    broken_bytes = broken_database.read_bytes()
    digest = __import__("hashlib").sha256(broken_bytes).hexdigest()
    metadata = result.metadata.__dict__ | {
        "database_sha256": digest,
        "database_size_bytes": len(broken_bytes),
    }
    corrupt_backup = tmp_path / "corrupt.pidbak"
    with zipfile.ZipFile(corrupt_backup, "w") as archive:
        archive.writestr(BACKUP_METADATA_MEMBER, json.dumps(metadata))
        archive.writestr(BACKUP_DATABASE_MEMBER, broken_bytes)

    target = tmp_path / "target.db"
    with pytest.raises(DatabaseIntegrityError):
        restore_backup(
            corrupt_backup,
            target,
            expected_instance_id=result.metadata.instance_id,
        )
    assert not target.exists()


def test_backup_refuses_existing_output_without_explicit_overwrite(tmp_path: Path):
    database = tmp_path / "source.db"
    SQLiteDocumentStore(database)
    backup = tmp_path / "source.pidbak"
    backup.write_bytes(b"keep me")

    with pytest.raises(UnsafeDatabaseTargetError) as error:
        create_backup(database, backup)
    assert error.value.code == "backup_output_exists"
    assert backup.read_bytes() == b"keep me"

    result = create_backup(database, backup, overwrite=True)
    assert inspect_backup(backup) == result.metadata


def test_restore_rejects_backup_path_as_database_target(tmp_path: Path):
    database = tmp_path / "source.db"
    SQLiteDocumentStore(database)
    backup = tmp_path / "source.pidbak"
    create_backup(database, backup)
    before = backup.read_bytes()

    with pytest.raises(UnsafeDatabaseTargetError):
        restore_backup(backup, backup)
    assert backup.read_bytes() == before


def test_malformed_backup_metadata_types_return_validation_error(tmp_path: Path):
    backup = tmp_path / "malformed.pidbak"
    metadata = {
        "format": "pid-agent.sqlite-backup",
        "version": 1,
        "created_at": "not-a-date",
        "schema_version": "2",
        "instance_id": "a" * 32,
        "database_sha256": "0" * 64,
        "database_size_bytes": 1,
        "source_database_name": "source.db",
    }
    with zipfile.ZipFile(backup, "w") as archive:
        archive.writestr(BACKUP_METADATA_MEMBER, json.dumps(metadata))
        archive.writestr(BACKUP_DATABASE_MEMBER, b"a")

    with pytest.raises(BackupValidationError):
        inspect_backup(backup)
