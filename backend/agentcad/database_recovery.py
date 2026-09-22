from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

CURRENT_SCHEMA_VERSION = 7
BACKUP_FORMAT = "pid-agent.sqlite-backup"
BACKUP_VERSION = 1
BACKUP_DATABASE_MEMBER = "database.sqlite3"
BACKUP_METADATA_MEMBER = "metadata.json"
_METADATA_TABLE = "database_metadata"
_EXPECTED_BACKUP_MEMBERS = {BACKUP_DATABASE_MEMBER, BACKUP_METADATA_MEMBER}
_MAX_METADATA_BYTES = 64 * 1024


class DatabaseRecoveryError(RuntimeError):
    code = "database_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class DatabaseMigrationError(DatabaseRecoveryError):
    code = "migration_failed"


class DatabaseVersionError(DatabaseRecoveryError):
    code = "unsupported_database_version"


class DatabaseIntegrityError(DatabaseRecoveryError):
    code = "database_integrity_failed"


class BackupValidationError(DatabaseRecoveryError):
    code = "invalid_backup"


class RestoreInstanceMismatchError(DatabaseRecoveryError):
    code = "restore_instance_mismatch"


class UnsafeDatabaseTargetError(DatabaseRecoveryError):
    code = "unsafe_database_target"


@dataclass(frozen=True)
class DatabaseInfo:
    path: str
    schema_version: int
    current_schema_version: int
    instance_id: str
    document_count: int
    size_bytes: int
    page_count: int
    page_size: int


@dataclass(frozen=True)
class BackupMetadata:
    format: str
    version: int
    created_at: str
    schema_version: int
    instance_id: str
    database_sha256: str
    database_size_bytes: int
    source_database_name: str

    @classmethod
    def from_payload(cls, payload: Any) -> BackupMetadata:
        if not isinstance(payload, dict):
            raise BackupValidationError("backup metadata must be a JSON object")
        expected = {
            "format",
            "version",
            "created_at",
            "schema_version",
            "instance_id",
            "database_sha256",
            "database_size_bytes",
            "source_database_name",
        }
        if set(payload) != expected:
            raise BackupValidationError("backup metadata fields are incomplete or unsupported")
        try:
            metadata = cls(**payload)
        except TypeError as exc:
            raise BackupValidationError("backup metadata is malformed") from exc
        if metadata.format != BACKUP_FORMAT or metadata.version != BACKUP_VERSION:
            raise BackupValidationError(
                f"unsupported backup format/version: {metadata.format!r}/{metadata.version!r}"
            )
        if type(metadata.schema_version) is not int:
            raise BackupValidationError("backup schema version is invalid")
        if metadata.schema_version > CURRENT_SCHEMA_VERSION:
            raise DatabaseVersionError(
                f"backup schema version {metadata.schema_version} is newer than supported "
                f"version {CURRENT_SCHEMA_VERSION}"
            )
        if metadata.schema_version <= 0:
            raise BackupValidationError("backup schema version must be positive")
        if type(metadata.database_size_bytes) is not int or metadata.database_size_bytes <= 0:
            raise BackupValidationError("backup database size is invalid")
        if not _is_sha256(metadata.database_sha256):
            raise BackupValidationError("backup database SHA-256 is invalid")
        if not _is_instance_id(metadata.instance_id):
            raise BackupValidationError("backup instance id is invalid")
        try:
            created_at = datetime.fromisoformat(metadata.created_at)
        except (TypeError, ValueError) as exc:
            raise BackupValidationError("backup creation time is invalid") from exc
        if created_at.tzinfo is None:
            raise BackupValidationError("backup creation time must include a UTC offset")
        if (
            not isinstance(metadata.source_database_name, str)
            or not metadata.source_database_name
            or Path(metadata.source_database_name).name != metadata.source_database_name
            or "/" in metadata.source_database_name
            or "\\" in metadata.source_database_name
        ):
            raise BackupValidationError("backup source database name is invalid")
        return metadata


@dataclass(frozen=True)
class BackupResult:
    path: str
    metadata: BackupMetadata


@dataclass(frozen=True)
class RestoreResult:
    path: str
    metadata: BackupMetadata
    replaced_existing_database: bool


def initialize_database(database_path: str | Path) -> DatabaseInfo:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists() or path.is_symlink()
    _reject_unsafe_existing_file(path, allow_missing=True)
    try:
        with closing(sqlite3.connect(path, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            _migrate(connection)
            info = _database_info_from_connection(path, connection)
        if not existed:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return info
    except DatabaseRecoveryError:
        raise
    except sqlite3.DatabaseError as exc:
        raise DatabaseIntegrityError(f"unable to open SQLite database: {exc}") from exc


def database_info(database_path: str | Path, *, migrate: bool = False) -> DatabaseInfo:
    path = Path(database_path)
    if migrate:
        return initialize_database(path)
    _reject_unsafe_existing_file(path, allow_missing=False)
    try:
        with closing(_readonly_connection(path)) as connection:
            _validate_supported_schema(connection)
            _validate_required_schema(connection)
            return _database_info_from_connection(path, connection)
    except DatabaseRecoveryError:
        raise
    except sqlite3.DatabaseError as exc:
        raise DatabaseIntegrityError(f"unable to read SQLite database: {exc}") from exc


def database_instance_id(database_path: str | Path) -> str:
    return database_info(database_path).instance_id


def database_schema_version(database_path: str | Path) -> int:
    """Read only the stored schema version without requiring an up-to-date schema."""
    path = Path(database_path)
    _reject_unsafe_existing_file(path, allow_missing=False)
    try:
        with closing(_readonly_connection(path)) as connection:
            return _schema_version(connection)
    except DatabaseRecoveryError:
        raise
    except sqlite3.DatabaseError as exc:
        raise DatabaseIntegrityError(f"unable to read SQLite database: {exc}") from exc


def verify_database(database_path: str | Path, *, expected_instance_id: str | None = None) -> DatabaseInfo:
    path = Path(database_path)
    _reject_unsafe_existing_file(path, allow_missing=False)
    try:
        with closing(_readonly_connection(path)) as connection:
            _validate_supported_schema(connection)
            _validate_required_schema(connection)
            quick_check = connection.execute("PRAGMA quick_check").fetchall()
            if not quick_check or any(str(row[0]).lower() != "ok" for row in quick_check):
                detail = "; ".join(str(row[0]) for row in quick_check[:10])
                raise DatabaseIntegrityError(f"SQLite quick_check failed: {detail}")
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_keys:
                raise DatabaseIntegrityError(
                    f"SQLite foreign_key_check found {len(foreign_keys)} violation(s)"
                )
            info = _database_info_from_connection(path, connection)
    except DatabaseRecoveryError:
        raise
    except sqlite3.DatabaseError as exc:
        raise DatabaseIntegrityError(f"SQLite integrity verification failed: {exc}") from exc
    if expected_instance_id is not None and info.instance_id != expected_instance_id:
        raise BackupValidationError(
            "database instance id does not match backup metadata",
            code="backup_instance_mismatch",
        )
    return info


def create_backup(
    database_path: str | Path,
    output_path: str | Path,
    *,
    overwrite: bool = False,
) -> BackupResult:
    source = Path(database_path)
    output = Path(output_path)
    source_info = initialize_database(source)
    _prepare_output_target(output, source=source, overwrite=overwrite)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pid-agent-backup-", dir=output.parent) as temp_dir:
        temp_root = Path(temp_dir)
        snapshot = temp_root / BACKUP_DATABASE_MEMBER
        try:
            with closing(sqlite3.connect(source, timeout=30)) as source_connection:
                source_connection.execute("PRAGMA foreign_keys=ON")
                with closing(sqlite3.connect(snapshot)) as destination_connection:
                    source_connection.backup(destination_connection)
        except sqlite3.DatabaseError as exc:
            raise DatabaseIntegrityError(f"online SQLite backup failed: {exc}") from exc

        snapshot_info = verify_database(snapshot, expected_instance_id=source_info.instance_id)
        database_sha256, database_size = _hash_file(snapshot)
        metadata = BackupMetadata(
            format=BACKUP_FORMAT,
            version=BACKUP_VERSION,
            created_at=datetime.now(UTC).isoformat(),
            schema_version=snapshot_info.schema_version,
            instance_id=snapshot_info.instance_id,
            database_sha256=database_sha256,
            database_size_bytes=database_size,
            source_database_name=source.name,
        )

        fd, temporary_archive_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        os.close(fd)
        temporary_archive = Path(temporary_archive_name)
        try:
            with zipfile.ZipFile(
                temporary_archive, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                archive.write(snapshot, BACKUP_DATABASE_MEMBER)
                archive.writestr(
                    BACKUP_METADATA_MEMBER,
                    json.dumps(asdict(metadata), ensure_ascii=False, sort_keys=True, indent=2)
                    + "\n",
                )
            _fsync_file(temporary_archive)
            inspect_backup(temporary_archive, verify_database_payload=True)
            os.chmod(temporary_archive, stat.S_IRUSR | stat.S_IWUSR)
            try:
                os.replace(temporary_archive, output)
            except OSError as exc:
                raise DatabaseRecoveryError(
                    f"unable to publish backup atomically: {exc}",
                    code="backup_write_failed",
                ) from exc
            _fsync_directory(output.parent)
        finally:
            temporary_archive.unlink(missing_ok=True)
    return BackupResult(path=str(output), metadata=metadata)


def inspect_backup(
    backup_path: str | Path,
    *,
    verify_database_payload: bool = False,
) -> BackupMetadata:
    path = Path(backup_path)
    _reject_unsafe_existing_file(path, allow_missing=False)
    try:
        with zipfile.ZipFile(path, mode="r") as archive:
            member_names = archive.namelist()
            names = set(member_names)
            if len(member_names) != len(names):
                raise BackupValidationError("backup contains duplicate archive members")
            if names != _EXPECTED_BACKUP_MEMBERS:
                raise BackupValidationError(
                    f"backup must contain exactly {sorted(_EXPECTED_BACKUP_MEMBERS)}"
                )
            for info in archive.infolist():
                if info.is_dir() or info.flag_bits & 0x1:
                    raise BackupValidationError("backup contains a directory or encrypted member")
            metadata_info = archive.getinfo(BACKUP_METADATA_MEMBER)
            if metadata_info.file_size > _MAX_METADATA_BYTES:
                raise BackupValidationError("backup metadata is too large")
            try:
                payload = json.loads(archive.read(BACKUP_METADATA_MEMBER).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BackupValidationError("backup metadata is not valid UTF-8 JSON") from exc
            metadata = BackupMetadata.from_payload(payload)
            database_info_member = archive.getinfo(BACKUP_DATABASE_MEMBER)
            if database_info_member.file_size != metadata.database_size_bytes:
                raise BackupValidationError("backup database size does not match metadata")
            with archive.open(BACKUP_DATABASE_MEMBER, mode="r") as stream:
                digest, size = _hash_stream(stream)
            if size != metadata.database_size_bytes or digest != metadata.database_sha256:
                raise BackupValidationError("backup database SHA-256 does not match metadata")
            if verify_database_payload:
                with tempfile.TemporaryDirectory(prefix=".pid-agent-inspect-") as temp_dir:
                    candidate = Path(temp_dir) / BACKUP_DATABASE_MEMBER
                    with archive.open(BACKUP_DATABASE_MEMBER, mode="r") as source, candidate.open(
                        "wb"
                    ) as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
                    verify_database(candidate, expected_instance_id=metadata.instance_id)
            return metadata
    except DatabaseRecoveryError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupValidationError(f"unable to read backup archive: {exc}") from exc


def restore_backup(
    backup_path: str | Path,
    database_path: str | Path,
    *,
    expected_instance_id: str | None = None,
    allow_instance_mismatch: bool = False,
) -> RestoreResult:
    backup = Path(backup_path)
    target = Path(database_path)
    if backup.resolve() == target.resolve():
        raise UnsafeDatabaseTargetError("backup input and restore target must be different files")
    metadata = inspect_backup(backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_restore_target(target)

    replaced_existing = target.exists()
    original_target_identity = _file_identity(target) if replaced_existing else None
    target_info: DatabaseInfo | None = None
    target_unreadable = False
    if replaced_existing:
        try:
            target_info = database_info(target)
        except DatabaseRecoveryError:
            target_unreadable = True

    if target_info is not None and target_info.instance_id != metadata.instance_id:
        if not allow_instance_mismatch:
            raise RestoreInstanceMismatchError(
                "backup belongs to a different database instance; use an explicit instance "
                "override only when cloning or replacing the intended target"
            )
        if expected_instance_id != metadata.instance_id:
            raise RestoreInstanceMismatchError(
                "instance override requires --expect-instance-id matching the backup instance id",
                code="restore_instance_confirmation_required",
            )
    elif target_info is None and (not replaced_existing or target_unreadable):
        if expected_instance_id != metadata.instance_id:
            raise RestoreInstanceMismatchError(
                "missing or unreadable target requires explicit confirmation of the backup "
                "instance id",
                code="restore_instance_confirmation_required",
            )
    elif expected_instance_id is not None and expected_instance_id != metadata.instance_id:
        raise RestoreInstanceMismatchError(
            "expected instance id does not match the backup",
            code="restore_instance_confirmation_failed",
        )

    fd, candidate_name = tempfile.mkstemp(
        prefix=f".{target.name}.restore-", suffix=".sqlite3", dir=target.parent
    )
    os.close(fd)
    candidate = Path(candidate_name)
    try:
        try:
            with zipfile.ZipFile(backup, mode="r") as archive, archive.open(
                BACKUP_DATABASE_MEMBER, mode="r"
            ) as source, candidate.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
        except (OSError, zipfile.BadZipFile) as exc:
            raise BackupValidationError(f"unable to extract backup database: {exc}") from exc
        digest, size = _hash_file(candidate)
        if digest != metadata.database_sha256 or size != metadata.database_size_bytes:
            raise BackupValidationError("extracted database does not match backup metadata")
        verify_database(candidate, expected_instance_id=metadata.instance_id)
        if target.exists() or target.is_symlink():
            _reject_unsafe_existing_file(target, allow_missing=False)
        _reject_new_restore_sidecars(target)
        if original_target_identity is None:
            if target.exists():
                raise UnsafeDatabaseTargetError(
                    "restore target appeared during validation", code="restore_target_changed"
                )
            mode = stat.S_IRUSR | stat.S_IWUSR
        else:
            if not target.exists() or _file_identity(target) != original_target_identity:
                raise UnsafeDatabaseTargetError(
                    "restore target changed during validation", code="restore_target_changed"
                )
            mode = stat.S_IMODE(target.stat().st_mode)
        try:
            os.chmod(candidate, mode)
            os.replace(candidate, target)
        except OSError as exc:
            raise DatabaseRecoveryError(
                f"unable to replace database atomically: {exc}",
                code="restore_replace_failed",
            ) from exc
        for suffix in ("-wal", "-shm"):
            try:
                Path(f"{target}{suffix}").unlink(missing_ok=True)
            except OSError:
                pass
        _fsync_directory(target.parent)
    finally:
        candidate.unlink(missing_ok=True)
    return RestoreResult(
        path=str(target), metadata=metadata, replaced_existing_database=replaced_existing
    )


def _migrate(connection: sqlite3.Connection) -> None:
    version = _schema_version(connection)
    if version > CURRENT_SCHEMA_VERSION:
        raise DatabaseVersionError(
            f"database schema version {version} is newer than supported version "
            f"{CURRENT_SCHEMA_VERSION}"
        )
    if version == 0:
        version = _infer_unversioned_schema(connection)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        if _schema_version(connection) == 0 and version > 0:
            connection.execute(f"PRAGMA user_version={version}")
        while version < CURRENT_SCHEMA_VERSION:
            next_version = version + 1
            _MIGRATIONS[next_version](connection)
            connection.execute(f"PRAGMA user_version={next_version}")
            version = next_version
        _validate_required_schema(connection)
        connection.commit()
    except DatabaseRecoveryError:
        connection.rollback()
        raise
    except sqlite3.DatabaseError as exc:
        connection.rollback()
        raise DatabaseMigrationError(f"database migration failed: {exc}") from exc


def _infer_unversioned_schema(connection: sqlite3.Connection) -> int:
    tables = _table_names(connection)
    if not tables:
        return 0
    if {"documents", "document_history"}.issubset(tables):
        return 1
    raise DatabaseMigrationError(
        "unversioned SQLite database does not match a supported P&ID-Agent schema"
    )


def _migration_1(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
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
        "CREATE INDEX IF NOT EXISTS idx_documents_updated_at ON documents(updated_at DESC)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS document_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL,
            action TEXT NOT NULL,
            label TEXT NOT NULL,
            operation_count INTEGER NOT NULL,
            FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
        )
        """
    )


def _migration_2(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_updated_at ON documents(updated_at DESC)"
    )
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(document_history)").fetchall()
    }
    if "details_json" not in columns:
        connection.execute(
            "ALTER TABLE document_history ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_document_revision "
        "ON document_history(document_id, revision DESC, id DESC)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS project_settings (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_METADATA_TABLE} (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
            instance_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
        """
    )
    row = connection.execute(
        f"SELECT instance_id FROM {_METADATA_TABLE} WHERE singleton_id = 1"
    ).fetchone()
    if row is None:
        connection.execute(
            f"INSERT INTO {_METADATA_TABLE} (singleton_id, instance_id, created_at) "
            "VALUES (1, ?, ?)",
            (uuid.uuid4().hex, datetime.now(UTC).isoformat()),
        )


def _migration_3(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_sessions (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            project_id TEXT,
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            start_revision INTEGER NOT NULL,
            end_revision INTEGER,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_sessions_document_created "
        "ON agent_sessions(document_id, created_at DESC)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_approvals (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            document_id TEXT NOT NULL,
            intent_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            resolved_by TEXT,
            reason TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            consumed_at TEXT,
            FOREIGN KEY(session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_approvals_session_created "
        "ON agent_approvals(session_id, created_at ASC)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_tool_calls (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            document_id TEXT NOT NULL,
            permission TEXT NOT NULL,
            risk TEXT NOT NULL,
            approval_id TEXT,
            intent_hash TEXT NOT NULL,
            base_revision INTEGER,
            result_revision INTEGER,
            status TEXT NOT NULL,
            error_code TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            completed_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
            FOREIGN KEY(approval_id) REFERENCES agent_approvals(id) ON DELETE SET NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_session_started "
        "ON agent_tool_calls(session_id, started_at ASC)"
    )


def _migration_4(connection: sqlite3.Connection) -> None:
    """Audit / provenance chain (T0.5).

    ``audit_records`` deliberately has no foreign key to ``documents``: evidence for
    a deletion must outlive the deleted document. The append-only hash chain is
    continued inside the same transaction as the document write.
    """
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_records (
            record_id TEXT PRIMARY KEY,
            ordinal INTEGER NOT NULL UNIQUE,
            recorded_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            surface TEXT NOT NULL,
            tool_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            document_id TEXT,
            project_id TEXT,
            base_revision INTEGER,
            result_revision INTEGER,
            session_id TEXT,
            approval_id TEXT,
            tool_call_id TEXT,
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            intent_hash TEXT NOT NULL DEFAULT '',
            diff_hash TEXT NOT NULL DEFAULT '',
            diff_preview_hash TEXT NOT NULL DEFAULT '',
            diff_binding TEXT NOT NULL DEFAULT 'not_recorded',
            validation_status TEXT NOT NULL DEFAULT 'not_run',
            validation_hash TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            error_code TEXT NOT NULL DEFAULT '',
            prev_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL,
            chain_schema TEXT NOT NULL,
            chain_version INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_records_document_revision "
        "ON audit_records(document_id, result_revision DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_records_recorded_at "
        "ON audit_records(recorded_at DESC, ordinal DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_records_session "
        "ON audit_records(session_id, ordinal ASC)"
    )
    approval_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(agent_approvals)").fetchall()
    }
    if "diff_preview_hash" not in approval_columns:
        connection.execute(
            "ALTER TABLE agent_approvals ADD COLUMN diff_preview_hash TEXT NOT NULL DEFAULT ''"
        )
    if "evidence_json" not in approval_columns:
        connection.execute(
            "ALTER TABLE agent_approvals ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}'"
        )


def _migration_5(connection: sqlite3.Connection) -> None:
    """Derived project engineering index (M2).

    ``project_index`` caches the engineering semantic graph per document so that
    project-wide queries (cross-document OPC links, schedules, rule rollups) do not
    have to load and re-derive every drawing. It is a *derived cache*: every row
    stores the document revision and the content hash it was built from, so a stale
    row is always detectable and can never be mistaken for engineering truth. The
    cascade keeps it consistent with document deletion when foreign keys are on;
    ``prune`` repairs rows left behind by older databases or FK-off connections.
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS project_index (
            document_id TEXT PRIMARY KEY,
            document_name TEXT NOT NULL,
            revision INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            graph_hash TEXT NOT NULL,
            builder_version INTEGER NOT NULL,
            object_count INTEGER NOT NULL,
            equipment_count INTEGER NOT NULL,
            valve_count INTEGER NOT NULL,
            instrument_count INTEGER NOT NULL,
            line_count INTEGER NOT NULL,
            off_page_count INTEGER NOT NULL,
            error_count INTEGER NOT NULL,
            warning_count INTEGER NOT NULL,
            graph_json TEXT NOT NULL,
            built_at TEXT NOT NULL,
            built_by TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_index_built_at "
        "ON project_index(built_at DESC)"
    )


def _migration_6(connection: sqlite3.Connection) -> None:
    """Signal count in the derived project index (M2 identity rework).

    Signals became first-class engineering objects, so the index reports how many a
    drawing has. Existing rows keep their cached graph until the builder version bump
    marks them stale and the next rebuild fills this in; the default is 0 rather than
    NULL so a stored 0 still means "zero", never "unknown".
    """

    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(project_index)").fetchall()
    }
    if "signal_count" not in columns:
        connection.execute(
            "ALTER TABLE project_index ADD COLUMN signal_count INTEGER NOT NULL DEFAULT 0"
        )


def _migration_7(connection: sqlite3.Connection) -> None:
    """M6 review aggregates: candidates, review decisions and confirmed findings.

    Three tables, and deliberately **no foreign key to ``documents``**. This follows the
    existing precedent for ``audit_records``: evidence must outlive the thing it is evidence
    about. A ``ON DELETE CASCADE`` here would erase the record that a person once confirmed a
    fact the moment somebody deleted the drawing, which is exactly the history an M6 review
    needs to keep; an ``ON DELETE RESTRICT``-style reference would instead make a drawing
    undeletable, because a row about it exists.

    The review decision table is the append-only transition log for a candidate, including the
    producer's filing. Only rows whose ``kind`` is a human decision carry a reviewer action —
    ``ReviewDecision`` enforces that at the model level — so ``confirmed`` stays traceable to a
    person even though the log also holds events.
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS semantic_candidates (
            candidate_id TEXT PRIMARY KEY,
            source_document_id TEXT NOT NULL,
            source_revision INTEGER NOT NULL,
            region_id TEXT NOT NULL,
            candidate_type TEXT NOT NULL,
            review_status_at_creation TEXT NOT NULL,
            producer_key TEXT NOT NULL,
            producer_version TEXT NOT NULL DEFAULT '',
            contract_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_semantic_candidates_document "
        "ON semantic_candidates(source_document_id, created_at DESC)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS review_decisions (
            review_decision_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            is_human INTEGER NOT NULL,
            from_status TEXT NOT NULL,
            to_status TEXT NOT NULL,
            reviewer_identity TEXT NOT NULL DEFAULT '',
            reviewer_action TEXT NOT NULL DEFAULT '',
            baseline_revision INTEGER,
            baseline_path TEXT NOT NULL DEFAULT '',
            baseline_digest TEXT NOT NULL DEFAULT '',
            decided_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_decisions_candidate "
        "ON review_decisions(candidate_id, decided_at, review_decision_id)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS confirmed_semantic_findings (
            finding_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            review_decision_id TEXT NOT NULL,
            source_document_id TEXT NOT NULL,
            source_revision INTEGER NOT NULL,
            region_id TEXT NOT NULL,
            candidate_type TEXT NOT NULL,
            baseline_revision INTEGER NOT NULL,
            confirmed_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_confirmed_findings_document "
        "ON confirmed_semantic_findings(source_document_id, confirmed_at DESC)"
    )


_MIGRATIONS = {
    1: _migration_1,
    2: _migration_2,
    3: _migration_3,
    4: _migration_4,
    5: _migration_5,
    6: _migration_6,
    7: _migration_7,
}


def _validate_supported_schema(connection: sqlite3.Connection) -> None:
    version = _schema_version(connection)
    if version > CURRENT_SCHEMA_VERSION:
        raise DatabaseVersionError(
            f"database schema version {version} is newer than supported version "
            f"{CURRENT_SCHEMA_VERSION}"
        )
    if version != CURRENT_SCHEMA_VERSION:
        raise DatabaseVersionError(
            f"database schema version {version} must be migrated to version "
            f"{CURRENT_SCHEMA_VERSION} before this operation",
            code="database_migration_required",
        )


def _validate_required_schema(connection: sqlite3.Connection) -> None:
    required_tables = {
        "documents",
        "document_history",
        "project_settings",
        "agent_sessions",
        "agent_approvals",
        "agent_tool_calls",
        "audit_records",
        "project_index",
        "semantic_candidates",
        "review_decisions",
        "confirmed_semantic_findings",
        _METADATA_TABLE,
    }
    missing = required_tables - _table_names(connection)
    if missing:
        raise DatabaseMigrationError(f"database schema is missing tables: {sorted(missing)}")
    required_columns = {
        "documents": {
            "id",
            "name",
            "revision",
            "data_json",
            "undo_json",
            "redo_json",
            "created_at",
            "updated_at",
        },
        "document_history": {
            "id",
            "document_id",
            "revision",
            "timestamp",
            "source",
            "action",
            "label",
            "operation_count",
            "details_json",
        },
        "project_settings": {"singleton_id", "data_json", "updated_at"},
        "agent_sessions": {
            "id", "document_id", "actor", "project_id", "provider", "model",
            "start_revision", "end_revision", "status", "created_at", "updated_at", "metadata_json",
        },
    "agent_approvals": {
        "id", "session_id", "tool_name", "document_id", "intent_hash", "status",
        "requested_by", "resolved_by", "reason", "note", "created_at", "resolved_at",
        "consumed_at", "diff_preview_hash", "evidence_json",
    },
    "audit_records": {
        "record_id", "ordinal", "recorded_at", "event_type", "actor", "surface",
        "tool_name", "status", "document_id", "project_id", "base_revision",
        "result_revision", "session_id", "approval_id", "tool_call_id", "provider",
        "model", "label", "intent_hash", "diff_hash", "diff_preview_hash",
        "diff_binding", "validation_status", "validation_hash", "evidence_json",
        "error_code", "prev_hash", "record_hash", "chain_schema", "chain_version",
    },
        "agent_tool_calls": {
            "id", "session_id", "tool_name", "document_id", "permission", "risk", "approval_id",
            "intent_hash", "base_revision", "result_revision", "status", "error_code",
            "started_at", "completed_at", "metadata_json",
        },
        "project_index": {
            "document_id", "document_name", "revision", "content_hash", "graph_hash",
            "builder_version", "object_count", "equipment_count", "valve_count",
            "instrument_count", "signal_count", "line_count", "off_page_count",
            "error_count", "warning_count", "graph_json", "built_at", "built_by",
        },
        "semantic_candidates": {
            "candidate_id", "source_document_id", "source_revision", "region_id",
            "candidate_type", "review_status_at_creation", "producer_key",
            "producer_version", "contract_version", "created_at", "payload_json",
        },
        "review_decisions": {
            "review_decision_id", "candidate_id", "kind", "is_human", "from_status",
            "to_status", "reviewer_identity", "reviewer_action", "baseline_revision",
            "baseline_path", "baseline_digest", "decided_at", "payload_json",
        },
        "confirmed_semantic_findings": {
            "finding_id", "candidate_id", "review_decision_id", "source_document_id",
            "source_revision", "region_id", "candidate_type", "baseline_revision",
            "confirmed_at", "payload_json",
        },
        _METADATA_TABLE: {"singleton_id", "instance_id", "created_at"},
    }
    for table, expected in required_columns.items():
        actual = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        missing_columns = expected - actual
        if missing_columns:
            raise DatabaseMigrationError(
                f"database table {table} is missing columns: {sorted(missing_columns)}"
            )
    row = connection.execute(
        f"SELECT instance_id FROM {_METADATA_TABLE} WHERE singleton_id = 1"
    ).fetchone()
    if row is None or not str(row[0]).strip():
        raise DatabaseMigrationError("database instance metadata is missing")


def _database_info_from_connection(path: Path, connection: sqlite3.Connection) -> DatabaseInfo:
    version = _schema_version(connection)
    row = connection.execute(
        f"SELECT instance_id FROM {_METADATA_TABLE} WHERE singleton_id = 1"
    ).fetchone()
    if row is None:
        raise DatabaseMigrationError("database instance metadata is missing")
    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    document_count = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    return DatabaseInfo(
        path=str(path),
        schema_version=version,
        current_schema_version=CURRENT_SCHEMA_VERSION,
        instance_id=str(row[0]),
        document_count=document_count,
        size_bytes=path.stat().st_size if path.exists() else page_count * page_size,
        page_count=page_count,
        page_size=page_size,
    )


def _schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _reject_unsafe_existing_file(path: Path, *, allow_missing: bool) -> None:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return
        raise UnsafeDatabaseTargetError(f"database file does not exist: {path}") from None
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise UnsafeDatabaseTargetError("database path must be a regular file, not a symlink")


def _prepare_output_target(output: Path, *, source: Path, overwrite: bool) -> None:
    if output.resolve() == source.resolve():
        raise UnsafeDatabaseTargetError("backup output cannot replace the live database")
    if output.exists() or output.is_symlink():
        _reject_unsafe_existing_file(output, allow_missing=False)
        if not overwrite:
            raise UnsafeDatabaseTargetError(
                f"backup output already exists: {output}", code="backup_output_exists"
            )


def _reject_restore_target(target: Path) -> None:
    if target.exists() or target.is_symlink():
        _reject_unsafe_existing_file(target, allow_missing=False)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{target}{suffix}")
        if sidecar.exists() or sidecar.is_symlink():
            raise UnsafeDatabaseTargetError(
                f"restore target has SQLite sidecar {sidecar.name}; stop the service and "
                "checkpoint/close the database before restoring",
                code="database_may_be_active",
            )


def _reject_new_restore_sidecars(target: Path) -> None:
    wal = Path(f"{target}-wal")
    shm = Path(f"{target}-shm")
    if shm.exists() or shm.is_symlink():
        raise UnsafeDatabaseTargetError(
            f"restore target acquired SQLite sidecar {shm.name} during validation",
            code="database_may_be_active",
        )
    if wal.exists() or wal.is_symlink():
        if wal.is_symlink() or wal.stat().st_size > 0:
            raise UnsafeDatabaseTargetError(
                f"restore target acquired SQLite sidecar {wal.name} during validation",
                code="database_may_be_active",
            )


def _file_identity(path: Path) -> tuple[int, int]:
    file_stat = path.lstat()
    return file_stat.st_dev, file_stat.st_ino


def _hash_file(path: Path) -> tuple[str, int]:
    with path.open("rb") as stream:
        return _hash_stream(stream)


def _hash_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value.lower()
    )


def _is_instance_id(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(
        character in "0123456789abcdef" for character in value.lower()
    )


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)
