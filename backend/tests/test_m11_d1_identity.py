"""M11-D1 hard locks (Gate CODE GO evidence list): identity boundary + v14 migration.

Covers the frozen Merge-Gate evidence: full v13->v14 migration with row-level
equivalence, failure rollback to a usable v13, FK check empty, P&ID lifecycle
(create/import/delete + cascade + audit retention), cross-domain id collision
fail-closed, frozen cable envelope DDL, untouched audit chain, and the
backup -> restore rollback rehearsal.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentcad import database_recovery as recovery
from agentcad.database_recovery import CURRENT_SCHEMA_VERSION
from agentcad.models import CreateDocumentRequest
from agentcad.service import DocumentService
from agentcad.store import (
    SQLiteDocumentStore,
    StoreDocumentIdentityError,
)
from agentcad.symbols import SymbolRegistry


def _service(path: Path) -> DocumentService:
    return DocumentService(SQLiteDocumentStore(path), SymbolRegistry())


def _snapshot_tables(database: Path) -> dict[str, list[dict]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    snapshot: dict[str, list[dict]] = {}
    for table in ("agent_sessions", "agent_approvals", "agent_tool_calls"):
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        snapshot[table] = [dict(row) for row in rows]
    snapshot["documents"] = [dict(r) for r in connection.execute("SELECT * FROM documents")]
    connection.close()
    return snapshot


def _seed_v13_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a genuine v13 database with governance rows, then freeze it at v13."""
    monkeypatch.setattr(recovery, "CURRENT_SCHEMA_VERSION", 13)
    database = tmp_path / "seed-v13.db"
    service = _service(database)
    first = service.create_document(CreateDocumentRequest(name="one", width=100, height=100))
    second = service.create_document(CreateDocumentRequest(name="two", width=100, height=100))
    store = service.store
    runtime_models = __import__("agentcad.runtime.models", fromlist=["x"])
    session = runtime_models.AgentSession(
        document_id=first.id, actor="engineer", start_revision=0
    )
    store.create_agent_session(session)
    approval = runtime_models.ToolApproval(
        session_id=session.id,
        tool_name="apply_compiled_agent_transaction",
        document_id=first.id,
        intent_hash="h1",
        requested_by="engineer",
    )
    store.create_tool_approval(approval)
    store.create_tool_call(
        runtime_models.ToolCallRecord(
            session_id=session.id,
            tool_name="apply_compiled_agent_transaction",
            document_id=first.id,
            permission="ask",
            risk="engineering_change",
            approval_id=approval.id,
            intent_hash="h1",
        )
    )
    monkeypatch.undo()
    assert second.id
    return database


def test_v13_to_v14_full_row_equivalence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _seed_v13_database(tmp_path, monkeypatch)
    before = _snapshot_tables(database)

    service = _service(database)  # reopens: migrates v13 -> current (v15 via v14)
    store = service.store
    with store._connect() as connection:  # noqa: SLF001
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        assert version == CURRENT_SCHEMA_VERSION == 15
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        registry_rows = connection.execute(
            "SELECT domain, COUNT(*) AS n FROM documents_registry GROUP BY domain"
        ).fetchall()
        assert [(row["domain"], row["n"]) for row in registry_rows] == [("pid", 2)]
        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        for expected in (
            "idx_agent_sessions_document_created",
            "idx_agent_approvals_session_created",
            "idx_agent_tool_calls_session_started",
        ):
            assert expected in indexes
        cable_columns = [
            row["name"]
            for row in connection.execute("PRAGMA table_info(cable_documents)")
        ]
        assert cable_columns == [
            "document_id",
            "revision",
            "data_json",
            "created_at",
            "updated_at",
        ]
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='cable_segments'"
        ).fetchone()["n"] == 0

    after = _snapshot_tables(database)
    assert after == before  # full-row, full-column equivalence on every table


def test_migration_failure_rolls_back_to_usable_v13(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _seed_v13_database(tmp_path, monkeypatch)
    original = recovery._migration_14

    def exploding(connection: sqlite3.Connection) -> None:
        original(connection)
        raise RuntimeError("injected post-rebuild failure")

    monkeypatch.setitem(recovery._MIGRATIONS, 14, exploding)
    with pytest.raises(RuntimeError, match="injected post-rebuild failure"):
        _service(database)
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
    connection.close()
    # v13 stays fully usable and a retry succeeds once the injection is gone.
    monkeypatch.undo()
    service = _service(database)
    document = service.create_document(CreateDocumentRequest(name="after", width=10, height=10))
    assert service.get_document(document.id).id == document.id


def test_pid_lifecycle_registry_semantics(tmp_path: Path) -> None:
    service = _service(tmp_path / "life.db")
    store = service.store
    document = service.create_document(CreateDocumentRequest(name="life", width=10, height=10))
    with store._connect() as connection:  # noqa: SLF001
        rows = connection.execute(
            "SELECT domain FROM documents_registry WHERE document_id = ?", (document.id,)
        ).fetchall()
        assert [(row["domain"],) for row in rows] == [("pid",)]

    # revision update must not touch the registry (single identity row stays)
    from agentcad.models import AddLayerOperation, Layer, TransactionRequest

    service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=0,
            label="bump",
            operations=[AddLayerOperation(layer=Layer(id="l1", name="L"))],
        ),
    )
    with store._connect() as connection:  # noqa: SLF001
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM documents_registry WHERE document_id = ?",
            (document.id,),
        ).fetchone()["n"]
        assert count == 1

    # delete: registry identity goes with the document; audit rows survive.
    audits_before = len(store.all_audit_records())
    assert store.delete(document.id, expected_revision=1)
    with store._connect() as connection:  # noqa: SLF001
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM documents_registry WHERE document_id = ?",
            (document.id,),
        ).fetchone()["n"] == 0
    assert len(store.all_audit_records()) >= audits_before


def test_governance_cascade_matches_v13(tmp_path: Path) -> None:
    service = _service(tmp_path / "cascade.db")
    store = service.store
    runtime_models = __import__("agentcad.runtime.models", fromlist=["x"])
    document = service.create_document(CreateDocumentRequest(name="c", width=10, height=10))
    session = runtime_models.AgentSession(document_id=document.id, actor="a", start_revision=0)
    store.create_agent_session(session)
    approval = runtime_models.ToolApproval(
        session_id=session.id,
        tool_name="t",
        document_id=document.id,
        intent_hash="h",
        requested_by="a",
    )
    store.create_tool_approval(approval)
    store.create_tool_call(
        runtime_models.ToolCallRecord(
            session_id=session.id,
            tool_name="t",
            document_id=document.id,
            permission="ask",
            risk="read",
            approval_id=approval.id,
            intent_hash="h",
        )
    )
    audits_before = len(store.all_audit_records())
    store.delete(document.id, expected_revision=0)
    with store._connect() as connection:  # noqa: SLF001
        for table in ("agent_sessions", "agent_approvals", "agent_tool_calls"):
            remaining = connection.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE document_id = ?", (document.id,)
            ).fetchone()["n"]
            assert remaining == 0, table
    assert len(store.all_audit_records()) == audits_before


def test_cross_domain_id_collision_fails_closed(tmp_path: Path) -> None:
    service = _service(tmp_path / "collision.db")
    store = service.store
    with store._connect() as connection:  # noqa: SLF001
        connection.execute(
            "INSERT INTO documents_registry (document_id, domain, created_at) VALUES ('cab_x', 'cable', '2026-10-01T00:00:00')"
        )
        connection.commit()
    from agentcad.store import StoredDocument

    impostor = service.create_document(CreateDocumentRequest(name="i", width=10, height=10))
    cable_claimed = impostor.model_copy(update={"id": "cab_x"})
    with pytest.raises(StoreDocumentIdentityError):
        store.save(StoredDocument(document=cable_claimed, undo_stack=[], redo_stack=[]))


def test_cable_envelope_ddl_frozen(tmp_path: Path) -> None:
    service = _service(tmp_path / "envelope.db")
    with service.store._connect() as connection:  # noqa: SLF001
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='cable_documents'"
        ).fetchone()["sql"]
        for fragment in (
            "document_id TEXT PRIMARY KEY",
            "revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0)",
            "data_json TEXT NOT NULL DEFAULT '{}'",
            "REFERENCES documents_registry(document_id)",
            "ON DELETE CASCADE",
        ):
            assert fragment in sql, fragment


def test_audit_chain_unchanged_by_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentcad.audit import AuditRecorder

    database = _seed_v13_database(tmp_path, monkeypatch)
    recorder = AuditRecorder(store=SQLiteDocumentStore(database), symbols=SymbolRegistry())
    verification = recorder.verify_chain()
    assert verification.ok
    ordinals = [record.ordinal for record in recorder.store.all_audit_records()]
    assert ordinals == list(range(1, len(ordinals) + 1))


def test_backup_restore_rollback_rehearsal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentcad.database_recovery import create_backup, inspect_backup, restore_backup

    database = _seed_v13_database(tmp_path, monkeypatch)
    backup = tmp_path / "pre-v14.backup"
    create_backup(database, backup)
    metadata = inspect_backup(backup)  # raises BackupValidationError if the backup is bad

    # migrate the original to v14, then roll back by restoring the verified
    # pre-v14 backup onto a fresh file: the pre-migration data must survive
    # (restore is the rollback path; opening migrates the v13 content forward).
    _service(database)
    restored = tmp_path / "restored-v13.db"
    restore_backup(backup, restored, expected_instance_id=metadata.instance_id)
    service = _service(restored)
    names = {document.name for document in service.list_documents()}
    assert {"one", "two"}.issubset(names)
    from agentcad.audit import AuditRecorder

    assert AuditRecorder(store=service.store, symbols=SymbolRegistry()).verify_chain().ok


# ---- R73-1: import lifecycle is registry-maintained and fail-closed ----

def test_import_lifecycle_registry_rows(tmp_path: Path) -> None:
    source = _service(tmp_path / "src.db")
    first = source.create_document(CreateDocumentRequest(name="i1", width=10, height=10))
    second = source.create_document(CreateDocumentRequest(name="i2", width=10, height=10))
    payload = [source.get_document(first.id), source.get_document(second.id)]

    target = _service(tmp_path / "dst.db")
    target.store.import_documents_atomic(payload)
    with target.store._connect() as connection:  # noqa: SLF001
        rows = connection.execute(
            "SELECT document_id, domain FROM documents_registry ORDER BY document_id"
        ).fetchall()
        assert {(row["document_id"], row["domain"]) for row in rows} == {
            (first.id, "pid"),
            (second.id, "pid"),
        }


def test_import_collision_with_cable_identity_rolls_back(tmp_path: Path) -> None:
    target = _service(tmp_path / "dst.db")
    with target.store._connect() as connection:  # noqa: SLF001
        connection.execute(
            "INSERT INTO documents_registry (document_id, domain, created_at) "
            "VALUES ('cab_x', 'cable', '2026-10-01T00:00:00')"
        )
        connection.commit()
    source = _service(tmp_path / "src.db")
    legit = source.create_document(CreateDocumentRequest(name="ok", width=10, height=10))
    impostor = source.create_document(CreateDocumentRequest(name="bad", width=10, height=10))
    payload = [
        impostor.model_copy(update={"id": "cab_x"}),
        source.get_document(legit.id),
    ]
    with pytest.raises(StoreDocumentIdentityError):
        target.store.import_documents_atomic(payload)
    with target.store._connect() as connection:  # noqa: SLF001
        # The whole import rolled back — not even the legit document landed.
        assert connection.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == 0
        assert connection.execute("SELECT COUNT(*) AS n FROM documents_registry").fetchone()["n"] == 1


# ---- R73-2: genuine pre-v14 backup evidence ----

def test_pre_v14_backup_is_taken_before_any_v14_touch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentcad.database_recovery import (
        create_backup,
        inspect_backup,
        restore_backup,
    )

    monkeypatch.setattr(recovery, "CURRENT_SCHEMA_VERSION", 13)
    database = tmp_path / "genuine-v13.db"
    service = _service(database)
    first = service.create_document(CreateDocumentRequest(name="one", width=10, height=10))
    second = service.create_document(CreateDocumentRequest(name="two", width=10, height=10))
    backup = tmp_path / "pre-v14.backup"
    create_backup(database, backup)  # CURRENT is still 13: no v14 touch possible
    metadata = inspect_backup(backup)
    assert metadata.schema_version == 13
    import zipfile

    with zipfile.ZipFile(backup) as archive:
        raw = archive.read("database.sqlite3")
    probe = tmp_path / "probe.db"
    probe.write_bytes(raw)
    probe_connection = sqlite3.connect(probe)
    assert probe_connection.execute("PRAGMA user_version").fetchone()[0] == 13
    probe_connection.close()

    # now move the original to v14, then roll back via the genuine v13 backup
    monkeypatch.undo()
    _service(database)
    restored = tmp_path / "restored.db"
    monkeypatch.setattr(recovery, "CURRENT_SCHEMA_VERSION", 13)
    restore_backup(backup, restored, expected_instance_id=metadata.instance_id)
    raw_connection = sqlite3.connect(restored)
    assert raw_connection.execute("PRAGMA user_version").fetchone()[0] == 13
    doc_ids = {
        row[0] for row in raw_connection.execute("SELECT id FROM documents")
    }
    raw_connection.close()
    assert {first.id, second.id}.issubset(doc_ids)


# ---- R73-3: same-connection explicit rollback + FK restore ----

def test_migration_failure_same_connection_rollback_and_fk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recovery, "CURRENT_SCHEMA_VERSION", 13)
    database = tmp_path / "fk.db"
    service = _service(database)
    service.create_document(CreateDocumentRequest(name="d", width=10, height=10))
    monkeypatch.undo()

    original = recovery._migration_14

    def exploding(connection: sqlite3.Connection) -> None:
        original(connection)
        raise RuntimeError("injected post-rebuild failure")

    monkeypatch.setitem(recovery._MIGRATIONS, 14, exploding)
    connection = sqlite3.connect(database)
    try:
        with pytest.raises(RuntimeError, match="injected post-rebuild failure"):
            recovery._migrate(connection)
        assert connection.in_transaction is False
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
    finally:
        connection.close()
    # retry after the injection is gone succeeds
    monkeypatch.undo()
    service = _service(database)
    assert service.get_document(service.list_documents()[0].id) is not None


# ---- R73-4: version-aware validation under a v14 binary ----

def test_v13_database_validates_under_v14_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recovery, "CURRENT_SCHEMA_VERSION", 13)
    database = tmp_path / "v13-validate.db"
    service = _service(database)
    service.create_document(CreateDocumentRequest(name="d", width=10, height=10))
    monkeypatch.undo()

    connection = sqlite3.connect(database)
    try:
        recovery._validate_required_schema(connection)  # v13 DB, v14 binary: must pass
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
    finally:
        connection.close()


# ---- R73-5: cascade contract equivalence with v13 ----

def _cascade_contract(database: Path) -> dict[str, dict[str, str]]:
    connection = sqlite3.connect(database)
    contract: dict[str, dict[str, str]] = {}
    for table in ("agent_sessions", "agent_approvals", "agent_tool_calls"):
        rows = connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        contract[table] = {
            row[3]: f"{row[5]}|{row[6]}"  # column -> "ON_DELETE|ON_UPDATE"
            for row in rows
        }
    connection.close()
    return contract


def test_governance_cascade_contract_matches_v13(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    contracts: dict[int, dict[str, dict[str, str]]] = {}
    for version in (13, 14):
        monkeypatch.setattr(recovery, "CURRENT_SCHEMA_VERSION", version)
        database = tmp_path / f"cascade-v{version}.db"
        _service(database)
        contracts[version] = _cascade_contract(database)
        monkeypatch.undo()
    assert contracts[14] == contracts[13]
