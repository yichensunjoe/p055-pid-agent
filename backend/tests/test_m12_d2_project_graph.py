"""M12-D2 hard locks: v15 migration + project identity/membership primitives.

Gate-frozen scope (M12-D1 DESIGN PASS, reports/m12-d1-design.md):
- _migration_15 creates projects + project_documents + engineering_links and
  backfills exactly one default project owning every registered document.
- Membership never stores domain (derived from documents_registry, R77-Q1);
  document_id UNIQUE enforces single-active-project ownership.
- engineering_links is schema foundation ONLY in D2: no link service/API.
- Backup chain: v14 binary backup -> v15 migrate -> restore yields v14 ->
  v15 binary re-migrates (R77-Q4 / F77 wording).
"""

import sqlite3
from pathlib import Path

import pytest

from agentcad import database_recovery as recovery
from agentcad.database_recovery import (
    CURRENT_SCHEMA_VERSION,
    DEFAULT_PROJECT_ID,
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


def _service(database: Path) -> DocumentService:
    return DocumentService(SQLiteDocumentStore(database), SymbolRegistry())


def _seed_v14_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a genuine v14 database (pid + cable docs), frozen at v14."""
    monkeypatch.setattr(recovery, "CURRENT_SCHEMA_VERSION", 14)
    database = tmp_path / "seed-v14.db"
    service = _service(database)
    first = service.create_document(
        __import__("agentcad.models", fromlist=["x"]).CreateDocumentRequest(
            name="pid-one", width=100, height=100
        )
    )
    from agentcad.cable_service import CableService

    cable = CableService(service.store)
    cable.create_document("cable-one")
    monkeypatch.undo()
    assert first.id
    return database


def _user_version(database: Path) -> int:
    connection = sqlite3.connect(database)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    connection.close()
    return int(version)


def test_v14_to_v15_migration_backfills_default_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _seed_v14_database(tmp_path, monkeypatch)
    assert _user_version(database) == 14

    service = _service(database)  # reopens: migrates v14 -> v15
    assert _user_version(database) == CURRENT_SCHEMA_VERSION == 15

    store = service.store
    projects = store.list_projects()
    assert projects == [(DEFAULT_PROJECT_ID, "P&ID Project")]

    members = store.list_project_documents(DEFAULT_PROJECT_ID)
    registry_domains = dict(
        store._connect().execute(  # noqa: SLF001
            "SELECT document_id, domain FROM documents_registry"
        ).fetchall()
    )
    assert len(members) == len(registry_domains) == 2
    for document_id, domain, _added_at in members:
        assert domain == registry_domains[document_id]
        assert domain in ("pid", "cable")


def test_migration_failure_rolls_back_to_usable_v14(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _seed_v14_database(tmp_path, monkeypatch)
    original = recovery._migration_15

    def _boom(connection: sqlite3.Connection) -> None:
        original(connection)
        raise RuntimeError("injected post-migration failure")

    monkeypatch.setitem(recovery._MIGRATIONS, 15, _boom)
    with pytest.raises(RuntimeError, match="injected"):
        _service(database)
    monkeypatch.undo()
    assert _user_version(database) == 14
    connection = sqlite3.connect(database)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    connection.close()
    assert "projects" not in tables
    # After rollback the database still opens and migrates cleanly.
    service = _service(database)
    assert service.store.get_project(DEFAULT_PROJECT_ID) is not None


def test_v14_backup_then_v15_migrate_then_restore_re_migrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R77-Q4 frozen chain: v14 binary generates+validates the .pidbak, v15
    binary migrates; restore yields a v14 database that the v15 binary then
    re-migrates."""
    database = _seed_v14_database(tmp_path, monkeypatch)
    backup = tmp_path / "pre-v15.backup"
    # Emulate the v14 binary: cap migrations at v14 while creating the backup
    # (create_backup -> initialize_database would otherwise migrate first).
    monkeypatch.setattr(recovery, "CURRENT_SCHEMA_VERSION", 14)
    create_backup(database, backup)
    metadata = inspect_backup(backup)
    monkeypatch.undo()
    assert metadata.schema_version == 14

    # v15 binary migrates the original.
    _service(database)
    assert _user_version(database) == 15

    # Rollback = restore (first yields v14), then v15 binary re-migrates.
    restored = tmp_path / "restored.db"
    restore_backup(
        backup,
        restored,
        expected_instance_id=metadata.instance_id,
        allow_pre_current_schema=True,
    )
    assert _user_version(restored) == 14
    service = _service(restored)
    assert _user_version(restored) == 15
    assert service.store.get_project(DEFAULT_PROJECT_ID) is not None


def test_membership_single_active_project_ownership(tmp_path: Path) -> None:
    service = _service(tmp_path / "m.db")
    store = service.store
    request = __import__("agentcad.models", fromlist=["x"]).CreateDocumentRequest(
        name="doc", width=10, height=10
    )
    document_id = service.create_document(request).id

    other_id = store.create_project("second")
    store.add_document_to_project(DEFAULT_PROJECT_ID, document_id)
    with pytest.raises(StoreDocumentConflictError):
        store.add_document_to_project(other_id, document_id)


def test_membership_domain_derived_not_stored(tmp_path: Path) -> None:
    SQLiteDocumentStore(tmp_path / "d.db")
    connection = sqlite3.connect(tmp_path / "d.db")
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(project_documents)").fetchall()
    }
    connection.close()
    assert "domain" not in columns


def test_membership_add_remove_roundtrip(tmp_path: Path) -> None:
    service = _service(tmp_path / "r.db")
    store = service.store
    request = __import__("agentcad.models", fromlist=["x"]).CreateDocumentRequest(
        name="doc", width=10, height=10
    )
    document_id = service.create_document(request).id
    assert store.list_project_documents(DEFAULT_PROJECT_ID) == []
    store.add_document_to_project(DEFAULT_PROJECT_ID, document_id, added_by="engineer")
    members = store.list_project_documents(DEFAULT_PROJECT_ID)
    assert [member[0] for member in members] == [document_id]
    assert members[0][1] == "pid"
    assert store.remove_document_from_project(DEFAULT_PROJECT_ID, document_id) is True
    assert store.remove_document_from_project(DEFAULT_PROJECT_ID, document_id) is False
    assert store.list_project_documents(DEFAULT_PROJECT_ID) == []


def test_engineering_links_schema_foundation_only(tmp_path: Path) -> None:
    """D2 freezes the links DDL but exposes no link behaviour."""
    store = SQLiteDocumentStore(tmp_path / "links.db")
    connection = sqlite3.connect(tmp_path / "links.db")
    ddl = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='engineering_links'"
    ).fetchone()[0]
    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    connection.close()
    assert "cable_endpoint_equipment" in ddl
    assert "idx_engineering_links_active_endpoint" in indexes
    # R77-Q2 invariant 6 + D2 scope: no link service/API surface exists yet.
    for forbidden in (
        "create_engineering_link",
        "list_engineering_links",
        "delete_engineering_link",
        "repin_engineering_link",
    ):
        assert not hasattr(store, forbidden)


def test_v15_database_passes_required_schema_validation(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"
    _service(database)
    connection = sqlite3.connect(database)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    default_row = connection.execute(
        "SELECT name FROM projects WHERE project_id = ?", (DEFAULT_PROJECT_ID,)
    ).fetchone()
    connection.close()
    assert version == 15
    assert default_row is not None
