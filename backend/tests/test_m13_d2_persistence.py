"""M13-D2 hard locks: v16 migration + change-set durable persistence.

Gate-frozen scope: CURRENT_SCHEMA_VERSION 15 -> 16; the single frozen table
with the four-state machine (staged -> approved -> applied / refused);
single-transaction migration with explicit rollback; upgrade fixture; the
v15-binary-backup -> v16-migrate -> restore-v15 -> re-migrate chain (R78-1
semantics). No impact/preview/executor logic here (D3/D4).
"""

import json
import sqlite3
from pathlib import Path

import pytest

from agentcad import database_recovery as recovery
from agentcad.database_recovery import (
    CURRENT_SCHEMA_VERSION,
    create_backup,
    inspect_backup,
    restore_backup,
)
from agentcad.service import DocumentService
from agentcad.store import (
    SQLiteDocumentStore,
    StoreDocumentConflictError,
)
from agentcad.symbols import SymbolRegistry

DEFAULT_PROJECT = "proj_m12default"


def _service(database: Path) -> DocumentService:
    return DocumentService(SQLiteDocumentStore(database), SymbolRegistry())


def _seed_v15_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a genuine v15 database, frozen at v15."""
    monkeypatch.setattr(recovery, "CURRENT_SCHEMA_VERSION", 15)
    database = tmp_path / "seed-v15.db"
    _service(database)
    monkeypatch.undo()
    return database


def _user_version(database: Path) -> int:
    connection = sqlite3.connect(database)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    connection.close()
    return int(version)


def test_v15_to_v16_migration_creates_change_set_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _seed_v15_database(tmp_path, monkeypatch)
    assert _user_version(database) == 15

    _service(database)  # reopens: migrates v15 -> v16
    assert _user_version(database) == CURRENT_SCHEMA_VERSION == 16

    connection = sqlite3.connect(database)
    ddl = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='project_change_sets'"
    ).fetchone()[0]
    connection.close()
    assert "'staged'" in ddl and "'approved'" in ddl
    assert "'applied'" in ddl and "'refused'" in ddl


def test_migration_failure_rolls_back_to_usable_v15(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _seed_v15_database(tmp_path, monkeypatch)
    original = recovery._migration_16

    def _boom(connection: sqlite3.Connection) -> None:
        original(connection)
        raise RuntimeError("injected post-migration failure")

    monkeypatch.setitem(recovery._MIGRATIONS, 16, _boom)
    with pytest.raises(RuntimeError, match="injected"):
        _service(database)
    monkeypatch.undo()
    assert _user_version(database) == 15
    connection = sqlite3.connect(database)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    connection.close()
    assert "project_change_sets" not in tables
    # After rollback the database still opens and migrates cleanly.
    _service(database)
    assert _user_version(database) == 16


def test_v15_backup_then_v16_migrate_then_restore_re_migrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R78-1 frozen chain, one version up: v15 binary backup -> v16 migrate
    -> restore yields v15 -> v16 binary re-migrates."""
    database = _seed_v15_database(tmp_path, monkeypatch)
    backup = tmp_path / "pre-v16.backup"
    monkeypatch.setattr(recovery, "CURRENT_SCHEMA_VERSION", 15)
    create_backup(database, backup)
    metadata = inspect_backup(backup)
    monkeypatch.undo()
    assert metadata.schema_version == 15

    _service(database)
    assert _user_version(database) == 16

    restored = tmp_path / "restored.db"
    restore_backup(
        backup,
        restored,
        expected_instance_id=metadata.instance_id,
        allow_pre_current_schema=True,
    )
    assert _user_version(restored) == 15
    _service(restored)
    assert _user_version(restored) == 16


def _stage(store: SQLiteDocumentStore, change_set_id: str) -> None:
    store.insert_change_set(
        change_set_id=change_set_id,
        project_id=DEFAULT_PROJECT,
        base_pins=json.dumps({"doc_a": 1}, sort_keys=True),
        intent=json.dumps({"project_id": DEFAULT_PROJECT}, sort_keys=True),
        intent_hash="h" + change_set_id,
        created_by="tester",
    )


def test_change_set_persistence_roundtrip_and_cas(tmp_path: Path) -> None:
    store = SQLiteDocumentStore(tmp_path / "d2.db")
    _stage(store, "cs_one")
    row = store.get_change_set("cs_one")
    assert row is not None
    assert row["status"] == "staged"
    assert row["base_pins"] == json.dumps({"doc_a": 1}, sort_keys=True)
    assert row["evidence"] == "{}"

    # happy-path transition with payload columns
    assert store.update_change_set_status(
        change_set_id="cs_one",
        expected_status="staged",
        new_status="approved",
        session_id="s1",
        approval_id="a1",
    ) is True
    row = store.get_change_set("cs_one")
    assert row["status"] == "approved"
    assert row["session_id"] == "s1" and row["approval_id"] == "a1"

    # CAS: wrong expected status fails closed and changes nothing
    assert store.update_change_set_status(
        change_set_id="cs_one",
        expected_status="staged",
        new_status="applied",
    ) is False
    assert store.get_change_set("cs_one")["status"] == "approved"

    # applied with result pins + evidence
    assert store.update_change_set_status(
        change_set_id="cs_one",
        expected_status="approved",
        new_status="applied",
        result_pins=json.dumps({"doc_a": 2}),
        evidence=json.dumps({"readiness_result_hash": "x" * 64}),
        tool_call_id="tc1",
    ) is True
    row = store.get_change_set("cs_one")
    assert row["status"] == "applied"
    assert row["result_pins"] == json.dumps({"doc_a": 2})
    assert row["tool_call_id"] == "tc1"

    # unknown id -> False, not an exception
    assert store.update_change_set_status(
        change_set_id="cs_nope",
        expected_status="staged",
        new_status="approved",
    ) is False
    assert store.get_change_set("cs_nope") is None


def test_change_set_status_check_rejects_unknown_status(tmp_path: Path) -> None:
    store = SQLiteDocumentStore(tmp_path / "check.db")
    _stage(store, "cs_bad")
    connection = sqlite3.connect(tmp_path / "check.db")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE project_change_sets SET status = 'weird' WHERE change_set_id = 'cs_bad'"
        )
        connection.commit()
    connection.close()


def test_change_set_duplicate_id_conflicts(tmp_path: Path) -> None:
    store = SQLiteDocumentStore(tmp_path / "dup.db")
    _stage(store, "cs_dup")
    with pytest.raises(StoreDocumentConflictError):
        _stage(store, "cs_dup")


def test_v16_database_passes_required_schema_validation(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"
    _service(database)
    connection = sqlite3.connect(database)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    row = connection.execute(
        "SELECT name FROM projects WHERE project_id = ?", (DEFAULT_PROJECT,)
    ).fetchone()
    connection.close()
    assert version == 16
    assert row is not None
