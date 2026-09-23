from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from .audit_hash import GENESIS_HASH, compute_record_hash
from .audit_models import AuditRecord, AuditRecordDraft
from .database_recovery import (
    database_instance_id as read_database_instance_id,
)
from .database_recovery import (
    database_schema_version,
    initialize_database,
)
from .harness_models import AgentSession, ToolApproval, ToolCallRecord
from .m6_candidate_models import (
    ConfirmedSemanticFinding,
    ReviewDecision,
    SemanticCandidate,
)
from .m7_synthesis_models import PROPOSAL_EVIDENCE_TABLE, SynthesisProposalEvidence
from .models import Document, DocumentSummary, HistoryEntry
from .project_io import ProjectSettings


def _new_audit_record_id() -> str:
    from uuid import uuid4

    return f"audit_{uuid4().hex}"


class StoreRevisionConflictError(RuntimeError):
    pass


class StoreDocumentConflictError(RuntimeError):
    pass


@dataclass
class StoredDocument:
    document: Document
    undo_stack: list[dict[str, Any]]
    redo_stack: list[dict[str, Any]]


class SQLiteDocumentStore:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._initialize()

    @property
    def database_instance_id(self) -> str:
        return read_database_instance_id(self.database_path)

    @property
    def schema_version(self) -> int:
        return database_schema_version(self.database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._lock:
            initialize_database(self.database_path)

    @staticmethod
    def _encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    def save(
        self,
        stored: StoredDocument,
        *,
        expected_revision: int | None = None,
        history: HistoryEntry | None = None,
        history_details: dict[str, Any] | None = None,
        audit: AuditRecordDraft | None = None,
        tool_call: ToolCallRecord | None = None,
        approval: ToolApproval | None = None,
        session: AgentSession | None = None,
    ) -> None:
        """Write the document revision and all of its provenance atomically.

        Document row, revision history (including the raw diff and engineering
        semantic diff), audit record, tool call completion and approval consumption
        are committed in one SQLite transaction. A crash or failure therefore can
        never leave a revision without its evidence, or a half-updated tool call.
        """
        document = stored.document
        values = (
            document.name,
            document.revision,
            self._encode(document.model_dump(mode="json")),
            self._encode(stored.undo_stack),
            self._encode(stored.redo_stack),
            document.updated_at.isoformat(),
        )
        with self._lock, self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if expected_revision is not None:
                    cursor = connection.execute(
                        """
                        UPDATE documents SET
                            name = ?, revision = ?, data_json = ?, undo_json = ?, redo_json = ?,
                            updated_at = ?
                        WHERE id = ? AND revision = ?
                        """,
                        (*values, document.id, expected_revision),
                    )
                    if cursor.rowcount != 1:
                        raise StoreRevisionConflictError(
                            f"document {document.id} no longer has revision {expected_revision}"
                        )
                else:
                    connection.execute(
                        """
                        INSERT INTO documents (
                            id, name, revision, data_json, undo_json, redo_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            name=excluded.name,
                            revision=excluded.revision,
                            data_json=excluded.data_json,
                            undo_json=excluded.undo_json,
                            redo_json=excluded.redo_json,
                            updated_at=excluded.updated_at
                        """,
                        (
                            document.id,
                            *values[:5],
                            document.created_at.isoformat(),
                            values[5],
                        ),
                    )
                if history is not None:
                    connection.execute(
                        """
                        INSERT INTO document_history (
                            document_id, revision, timestamp, source, action, label,
                            operation_count, details_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            history.document_id,
                            history.revision,
                            history.timestamp.isoformat(),
                            history.source,
                            history.action,
                            history.label,
                            history.operation_count,
                            self._encode(history_details or {}),
                        ),
                    )
                if audit is not None:
                    self._append_audit_record(connection, audit)
                if tool_call is not None:
                    self._write_tool_call(connection, tool_call)
                if approval is not None:
                    self._write_tool_approval(connection, approval)
                if session is not None:
                    self._write_agent_session(connection, session)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def document_ids(self) -> set[str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT id FROM documents").fetchall()
        return {str(row["id"]) for row in rows}

    def get_project_settings(self) -> ProjectSettings:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data_json FROM project_settings WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            return ProjectSettings()
        return ProjectSettings.model_validate_json(row["data_json"])

    def save_project_settings(
        self,
        settings: ProjectSettings,
        *,
        audit: AuditRecordDraft | None = None,
    ) -> ProjectSettings:
        normalized = ProjectSettings.model_validate(settings.model_dump(mode="python"))
        with self._lock, self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO project_settings (singleton_id, data_json, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(singleton_id) DO UPDATE SET
                        data_json=excluded.data_json, updated_at=excluded.updated_at
                    """,
                    (
                        self._encode(normalized.model_dump(mode="json")),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                if audit is not None:
                    self._append_audit_record(connection, audit)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return normalized

    def import_documents_atomic(
        self,
        documents: list[Document],
        *,
        project_settings: ProjectSettings | None = None,
        audits: list[AuditRecordDraft] | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                for document in documents:
                    connection.execute(
                        """
                        INSERT INTO documents (
                            id, name, revision, data_json, undo_json, redo_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, '[]', '[]', ?, ?)
                        """,
                        (
                            document.id,
                            document.name,
                            document.revision,
                            self._encode(document.model_dump(mode="json")),
                            document.created_at.isoformat(),
                            document.updated_at.isoformat(),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO document_history (
                            document_id, revision, timestamp, source, action, label,
                            operation_count, details_json
                        ) VALUES (?, ?, ?, 'system', 'create', 'Import document', 0, '{}')
                        """,
                        (document.id, document.revision, datetime.now(UTC).isoformat()),
                    )
                if project_settings is not None:
                    connection.execute(
                        """
                        INSERT INTO project_settings (singleton_id, data_json, updated_at)
                        VALUES (1, ?, ?)
                        ON CONFLICT(singleton_id) DO UPDATE SET
                            data_json=excluded.data_json, updated_at=excluded.updated_at
                        """,
                        (
                            self._encode(project_settings.model_dump(mode="json")),
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                for audit in audits or []:
                    self._append_audit_record(connection, audit)
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise StoreDocumentConflictError(str(exc)) from exc
            except Exception:
                connection.rollback()
                raise

    def update_history_details(
        self,
        document_id: str,
        revision: int,
        details: dict[str, Any],
    ) -> bool:
        try:
            with self._lock, self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE document_history
                    SET details_json = ?
                    WHERE id = (
                        SELECT id FROM document_history
                        WHERE document_id = ? AND revision = ?
                        ORDER BY id DESC LIMIT 1
                    )
                    """,
                    (self._encode(details), document_id, revision),
                )
                return cursor.rowcount == 1
        except sqlite3.Error:
            return False

    # ---------------------------------------------------------------- audit chain

    def _append_audit_record(
        self,
        connection: sqlite3.Connection,
        draft: AuditRecordDraft,
    ) -> AuditRecord:
        row = connection.execute(
            "SELECT ordinal, record_hash FROM audit_records ORDER BY ordinal DESC LIMIT 1"
        ).fetchone()
        prev_hash = str(row["record_hash"]) if row is not None else GENESIS_HASH
        ordinal = int(row["ordinal"]) + 1 if row is not None else 1
        record = AuditRecord(
            record_id=_new_audit_record_id(),
            ordinal=ordinal,
            recorded_at=datetime.now(UTC),
            prev_hash=prev_hash,
            **draft.model_dump(),
        )
        record = record.model_copy(update={"record_hash": compute_record_hash(record, prev_hash)})
        connection.execute(
            """
            INSERT INTO audit_records (
                record_id, ordinal, recorded_at, event_type, actor, surface, tool_name, status,
                document_id, project_id, base_revision, result_revision, session_id, approval_id,
                tool_call_id, provider, model, label, intent_hash, diff_hash, diff_preview_hash,
                diff_binding, validation_status, validation_hash, evidence_json, error_code,
                prev_hash, record_hash, chain_schema, chain_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.record_id,
                record.ordinal,
                record.recorded_at.isoformat(),
                record.event_type,
                record.actor,
                record.surface,
                record.tool_name,
                record.status,
                record.document_id,
                record.project_id,
                record.base_revision,
                record.result_revision,
                record.session_id,
                record.approval_id,
                record.tool_call_id,
                record.provider,
                record.model,
                record.label,
                record.intent_hash,
                record.diff_hash,
                record.diff_preview_hash,
                record.diff_binding,
                record.validation_status,
                record.validation_hash,
                self._encode(record.evidence),
                record.error_code,
                record.prev_hash,
                record.record_hash,
                record.chain_schema,
                record.chain_version,
            ),
        )
        return record

    def record_audit_event(
        self,
        draft: AuditRecordDraft,
        *,
        tool_call: ToolCallRecord | None = None,
        approval: ToolApproval | None = None,
        session: AgentSession | None = None,
    ) -> AuditRecord:
        """Append a standalone audit fact together with any close-out state.

        Used for events that do not write a document revision (create/delete/import/
        denial/index rebuild) and for rejected or failed attempts. Audit row and
        harness state still commit in one transaction.
        """
        with self._lock, self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                record = self._append_audit_record(connection, draft)
                if tool_call is not None:
                    self._write_tool_call(connection, tool_call)
                if approval is not None:
                    self._write_tool_approval(connection, approval)
                if session is not None:
                    self._write_agent_session(connection, session)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return record

    @staticmethod
    def _audit_record_from_row(row: sqlite3.Row) -> AuditRecord:
        payload = dict(row)
        raw_evidence = payload.pop("evidence_json", "{}")
        try:
            evidence = json.loads(raw_evidence) if raw_evidence else {}
        except json.JSONDecodeError:
            evidence = {"decode_error": True}
        payload["evidence"] = evidence if isinstance(evidence, dict) else {}
        return AuditRecord.model_validate(payload)

    _AUDIT_COLUMNS = (
        "record_id, ordinal, recorded_at, event_type, actor, surface, tool_name, status, "
        "document_id, project_id, base_revision, result_revision, session_id, approval_id, "
        "tool_call_id, provider, model, label, intent_hash, diff_hash, diff_preview_hash, "
        "diff_binding, validation_status, validation_hash, evidence_json, error_code, "
        "prev_hash, record_hash, chain_schema, chain_version"
    )

    def all_audit_records(self) -> list[AuditRecord]:
        """Return the whole chain in chain order (used by verification/export)."""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT {self._AUDIT_COLUMNS} FROM audit_records ORDER BY ordinal ASC"
            ).fetchall()
        return [self._audit_record_from_row(row) for row in rows]

    def list_audit_records(
        self,
        *,
        document_id: str | None = None,
        event_type: str | None = None,
        actor: str | None = None,
        status: str | None = None,
        tool_name: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 200,
    ) -> list[AuditRecord]:
        safe_limit = max(1, min(limit, 5000))
        clauses: list[str] = []
        parameters: list[Any] = []
        if document_id is not None:
            clauses.append("document_id = ?")
            parameters.append(document_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            parameters.append(event_type)
        if actor is not None:
            clauses.append("actor = ?")
            parameters.append(actor)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if tool_name is not None:
            clauses.append("tool_name = ?")
            parameters.append(tool_name)
        if since is not None:
            clauses.append("recorded_at >= ?")
            parameters.append(since)
        if until is not None:
            clauses.append("recorded_at <= ?")
            parameters.append(until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT {self._AUDIT_COLUMNS} FROM audit_records {where} "
                "ORDER BY ordinal DESC LIMIT ?",
                (*parameters, safe_limit),
            ).fetchall()
        return [self._audit_record_from_row(row) for row in rows]

    def get_audit_record(self, record_id: str) -> AuditRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"SELECT {self._AUDIT_COLUMNS} FROM audit_records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        return self._audit_record_from_row(row) if row is not None else None

    def get_audit_record_for_revision(
        self,
        document_id: str,
        revision: int,
    ) -> AuditRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"SELECT {self._AUDIT_COLUMNS} FROM audit_records "
                "WHERE document_id = ? AND result_revision = ? AND status = 'applied' "
                "ORDER BY ordinal DESC LIMIT 1",
                (document_id, revision),
            ).fetchone()
        return self._audit_record_from_row(row) if row is not None else None

    # --------------------------------------------------------------- state writes

    def get(self, document_id: str) -> StoredDocument | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data_json, undo_json, redo_json FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        return StoredDocument(
            document=Document.model_validate_json(row["data_json"]),
            undo_stack=json.loads(row["undo_json"]),
            redo_stack=json.loads(row["redo_json"]),
        )

    def delete(
        self,
        document_id: str,
        *,
        expected_revision: int,
        audit: AuditRecordDraft | None = None,
    ) -> bool:
        with self._lock, self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "DELETE FROM documents WHERE id = ? AND revision = ?",
                    (document_id, expected_revision),
                )
                if cursor.rowcount == 1:
                    # The audit row deliberately has no foreign key to documents: the
                    # evidence for a deletion must outlive the deleted document.
                    if audit is not None:
                        self._append_audit_record(connection, audit)
                    connection.commit()
                    return True

                current = connection.execute(
                    "SELECT revision FROM documents WHERE id = ?",
                    (document_id,),
                ).fetchone()
                connection.rollback()
                if current is None:
                    return False
                raise StoreRevisionConflictError(
                    f"document {document_id} no longer has revision {expected_revision}; "
                    f"current revision is {current['revision']}"
                )
            except Exception:
                connection.rollback()
                raise

    def list(self) -> list[DocumentSummary]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT data_json FROM documents ORDER BY updated_at DESC"
            ).fetchall()
        summaries: list[DocumentSummary] = []
        for row in rows:
            document = Document.model_validate_json(row["data_json"])
            summaries.append(
                DocumentSummary(
                    id=document.id,
                    name=document.name,
                    revision=document.revision,
                    element_count=len(document.elements),
                    updated_at=document.updated_at,
                    metadata=document.metadata,
                )
            )
        return summaries

    def list_history(self, document_id: str, limit: int = 100) -> list[HistoryEntry]:
        return [
            HistoryEntry.model_validate(
                {key: value for key, value in item.items() if key != "details"}
            )
            for item in self.list_history_detailed(document_id, limit)
        ]

    def get_history_revision_detailed(
        self,
        document_id: str,
        revision: int,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, document_id, revision, timestamp, source, action, label,
                       operation_count, details_json
                FROM document_history
                WHERE document_id = ? AND revision = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (document_id, revision),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        raw_details = item.pop("details_json", "{}")
        try:
            details = json.loads(raw_details) if raw_details else {}
        except json.JSONDecodeError:
            details = {"decode_error": True}
        item["details"] = details if isinstance(details, dict) else {}
        return item

    def list_history_detailed(self, document_id: str, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 500))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, document_id, revision, timestamp, source, action, label,
                       operation_count, details_json
                FROM document_history
                WHERE document_id = ?
                ORDER BY revision DESC, id DESC
                LIMIT ?
                """,
                (document_id, safe_limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw_details = item.pop("details_json", "{}")
            try:
                details = json.loads(raw_details) if raw_details else {}
            except json.JSONDecodeError:
                details = {"decode_error": True}
            item["details"] = details if isinstance(details, dict) else {}
            result.append(item)
        return result

    def create_agent_session(self, session: AgentSession) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_sessions (
                    id, document_id, actor, project_id, provider, model,
                    start_revision, end_revision, status, created_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.document_id,
                    session.actor,
                    session.project_id,
                    session.provider,
                    session.model,
                    session.start_revision,
                    session.end_revision,
                    session.status,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    self._encode(session.metadata),
                ),
            )

    def get_agent_session(self, session_id: str) -> AgentSession | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, document_id, actor, project_id, provider, model,
                       start_revision, end_revision, status, created_at, updated_at, metadata_json
                FROM agent_sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["metadata"] = json.loads(payload.pop("metadata_json") or "{}")
        return AgentSession.model_validate(payload)

    def _write_agent_session(self, connection: sqlite3.Connection, session: AgentSession) -> None:
        cursor = connection.execute(
            """
            UPDATE agent_sessions SET
                actor = ?, project_id = ?, provider = ?, model = ?,
                start_revision = ?, end_revision = ?, status = ?,
                updated_at = ?, metadata_json = ?
            WHERE id = ?
            """,
            (
                session.actor,
                session.project_id,
                session.provider,
                session.model,
                session.start_revision,
                session.end_revision,
                session.status,
                session.updated_at.isoformat(),
                self._encode(session.metadata),
                session.id,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"agent session not found: {session.id}")

    def update_agent_session(self, session: AgentSession) -> None:
        with self._lock, self._connect() as connection:
            self._write_agent_session(connection, session)

    def create_tool_approval(self, approval: ToolApproval) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
            INSERT INTO agent_approvals (
                id, session_id, tool_name, document_id, intent_hash, status,
                requested_by, resolved_by, reason, note, created_at, resolved_at, consumed_at,
                diff_preview_hash, evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    approval.id,
                    approval.session_id,
                    approval.tool_name,
                    approval.document_id,
                    approval.intent_hash,
                    approval.status,
                    approval.requested_by,
                    approval.resolved_by,
                    approval.reason,
                    approval.note,
                    approval.created_at.isoformat(),
                    approval.resolved_at.isoformat() if approval.resolved_at else None,
                    approval.consumed_at.isoformat() if approval.consumed_at else None,
                    approval.diff_preview_hash,
                    self._encode(approval.evidence),
                ),
            )

    def get_tool_approval(self, approval_id: str) -> ToolApproval | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, session_id, tool_name, document_id, intent_hash, status,
                       requested_by, resolved_by, reason, note, created_at, resolved_at, consumed_at,
                       diff_preview_hash, evidence_json
                FROM agent_approvals WHERE id = ?
                """,
                (approval_id,),
            ).fetchone()
        return self._approval_from_row(row) if row is not None else None

    @staticmethod
    def _approval_from_row(row: sqlite3.Row) -> ToolApproval:
        payload = dict(row)
        raw_evidence = payload.pop("evidence_json", "{}")
        try:
            evidence = json.loads(raw_evidence) if raw_evidence else {}
        except json.JSONDecodeError:
            evidence = {}
        payload["evidence"] = evidence if isinstance(evidence, dict) else {}
        return ToolApproval.model_validate(payload)

    def _write_tool_approval(self, connection: sqlite3.Connection, approval: ToolApproval) -> None:
        cursor = connection.execute(
            """
            UPDATE agent_approvals SET
                status = ?, resolved_by = ?, reason = ?, note = ?,
                resolved_at = ?, consumed_at = ?, diff_preview_hash = ?, evidence_json = ?
            WHERE id = ?
            """,
            (
                approval.status,
                approval.resolved_by,
                approval.reason,
                approval.note,
                approval.resolved_at.isoformat() if approval.resolved_at else None,
                approval.consumed_at.isoformat() if approval.consumed_at else None,
                approval.diff_preview_hash,
                self._encode(approval.evidence),
                approval.id,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"tool approval not found: {approval.id}")

    def update_tool_approval(self, approval: ToolApproval) -> None:
        with self._lock, self._connect() as connection:
            self._write_tool_approval(connection, approval)

    def list_tool_approvals(self, session_id: str) -> list[ToolApproval]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, session_id, tool_name, document_id, intent_hash, status,
                       requested_by, resolved_by, reason, note, created_at, resolved_at, consumed_at,
                       diff_preview_hash, evidence_json
                FROM agent_approvals
                WHERE session_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (session_id,),
            ).fetchall()
        return [self._approval_from_row(row) for row in rows]

    def create_tool_call(self, record: ToolCallRecord) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_tool_calls (
                    id, session_id, tool_name, document_id, permission, risk, approval_id,
                    intent_hash, base_revision, result_revision, status, error_code,
                    started_at, completed_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.session_id,
                    record.tool_name,
                    record.document_id,
                    record.permission,
                    record.risk,
                    record.approval_id,
                    record.intent_hash,
                    record.base_revision,
                    record.result_revision,
                    record.status,
                    record.error_code,
                    record.started_at.isoformat(),
                    record.completed_at.isoformat() if record.completed_at else None,
                    self._encode(record.metadata),
                ),
            )

    def _write_tool_call(self, connection: sqlite3.Connection, record: ToolCallRecord) -> None:
        cursor = connection.execute(
            """
            UPDATE agent_tool_calls SET
                approval_id = ?, result_revision = ?, status = ?, error_code = ?,
                completed_at = ?, metadata_json = ?
            WHERE id = ?
            """,
            (
                record.approval_id,
                record.result_revision,
                record.status,
                record.error_code,
                record.completed_at.isoformat() if record.completed_at else None,
                self._encode(record.metadata),
                record.id,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"tool call not found: {record.id}")

    def update_tool_call(self, record: ToolCallRecord) -> None:
        with self._lock, self._connect() as connection:
            self._write_tool_call(connection, record)

    def get_tool_call(self, tool_call_id: str) -> ToolCallRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, session_id, tool_name, document_id, permission, risk, approval_id,
                       intent_hash, base_revision, result_revision, status, error_code,
                       started_at, completed_at, metadata_json
                FROM agent_tool_calls WHERE id = ?
                """,
                (tool_call_id,),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        try:
            payload["metadata"] = json.loads(payload.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            payload["metadata"] = {}
        return ToolCallRecord.model_validate(payload)

    def list_tool_calls(self, session_id: str) -> list[ToolCallRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, session_id, tool_name, document_id, permission, risk, approval_id,
                       intent_hash, base_revision, result_revision, status, error_code,
                       started_at, completed_at, metadata_json
                FROM agent_tool_calls
                WHERE session_id = ?
                ORDER BY started_at ASC, id ASC
                """,
                (session_id,),
            ).fetchall()
        result: list[ToolCallRecord] = []
        for row in rows:
            payload = dict(row)
            payload["metadata"] = json.loads(payload.pop("metadata_json") or "{}")
            result.append(ToolCallRecord.model_validate(payload))
        return result

    # ------------------------------------------------------- derived project index

    _PROJECT_INDEX_COLUMNS = (
        "document_id, document_name, revision, content_hash, graph_hash, builder_version, "
        "object_count, equipment_count, valve_count, instrument_count, signal_count, "
        "line_count, off_page_count, error_count, warning_count, graph_json, built_at, built_by"
    )

    def upsert_project_index(self, record: dict[str, Any]) -> None:
        """Insert or replace one derived index row (never part of the audit chain).

        The index is a cache of a pure function of the document, so it carries no
        engineering authority and is deliberately not audited: auditing a
        recomputable cache would add noise to the evidence chain without adding
        evidence. Staleness is expressed by ``revision``/``content_hash`` instead.
        """

        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO project_index (
                    document_id, document_name, revision, content_hash, graph_hash,
                    builder_version, object_count, equipment_count, valve_count,
                    instrument_count, signal_count, line_count, off_page_count, error_count,
                    warning_count, graph_json, built_at, built_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    document_name = excluded.document_name,
                    revision = excluded.revision,
                    content_hash = excluded.content_hash,
                    graph_hash = excluded.graph_hash,
                    builder_version = excluded.builder_version,
                    object_count = excluded.object_count,
                    equipment_count = excluded.equipment_count,
                    valve_count = excluded.valve_count,
                    instrument_count = excluded.instrument_count,
                    signal_count = excluded.signal_count,
                    line_count = excluded.line_count,
                    off_page_count = excluded.off_page_count,
                    error_count = excluded.error_count,
                    warning_count = excluded.warning_count,
                    graph_json = excluded.graph_json,
                    built_at = excluded.built_at,
                    built_by = excluded.built_by
                """,
                (
                    record["document_id"],
                    record["document_name"],
                    record["revision"],
                    record["content_hash"],
                    record["graph_hash"],
                    record["builder_version"],
                    record["object_count"],
                    record["equipment_count"],
                    record["valve_count"],
                    record["instrument_count"],
                    record["signal_count"],
                    record["line_count"],
                    record["off_page_count"],
                    record["error_count"],
                    record["warning_count"],
                    record["graph_json"],
                    record["built_at"],
                    record.get("built_by", ""),
                ),
            )

    def get_project_index(self, document_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"SELECT {self._PROJECT_INDEX_COLUMNS} FROM project_index WHERE document_id = ?",
                (document_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_project_index(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT {self._PROJECT_INDEX_COLUMNS} FROM project_index ORDER BY document_id ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_project_index(self, document_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM project_index WHERE document_id = ?",
                (document_id,),
            )
            return cursor.rowcount == 1

    # -- M6 review aggregates ------------------------------------------------------------
    #
    # Three insert-only tables with no foreign key to `documents`, deliberately: review
    # evidence must outlive the drawing it is about, exactly like `audit_records`. The
    # methods below expose no update path at all, which is how "immutable" is enforced
    # rather than promised: a second insert with the same id conflicts, and there is no way
    # to rewrite a recorded decision.

    def insert_semantic_candidate(self, candidate: SemanticCandidate) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO semantic_candidates (
                    candidate_id, source_document_id, source_revision, region_id,
                    candidate_type, review_status_at_creation, producer_key,
                    producer_version, contract_version, created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.candidate_id,
                    candidate.artifact.source_document_id,
                    candidate.artifact.source_revision,
                    candidate.region.region_id,
                    candidate.candidate_type,
                    candidate.review_status,
                    candidate.producer.key,
                    candidate.producer.version,
                    candidate.contract,
                    candidate.created_at.isoformat(),
                    self._encode(candidate.model_dump(mode="json", by_alias=True)),
                ),
            )

    def get_semantic_candidate(self, candidate_id: str) -> SemanticCandidate | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM semantic_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            return None
        return SemanticCandidate.model_validate_json(row["payload_json"])

    def list_semantic_candidates(
        self, *, source_document_id: str | None = None
    ) -> list[SemanticCandidate]:
        query = "SELECT payload_json FROM semantic_candidates"
        params: tuple[Any, ...] = ()
        if source_document_id is not None:
            query += " WHERE source_document_id = ?"
            params = (source_document_id,)
        query += " ORDER BY created_at ASC, candidate_id ASC"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [SemanticCandidate.model_validate_json(row["payload_json"]) for row in rows]

    def insert_review_decision(self, decision: ReviewDecision) -> None:
        with self._lock, self._connect() as connection:
            self._insert_review_decision(connection, decision)

    def list_review_decisions(self, candidate_id: str) -> list[ReviewDecision]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM review_decisions WHERE candidate_id = ? "
                "ORDER BY decided_at ASC, review_decision_id ASC",
                (candidate_id,),
            ).fetchall()
        return [ReviewDecision.model_validate_json(row["payload_json"]) for row in rows]

    def get_review_decision(self, review_decision_id: str) -> ReviewDecision | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM review_decisions WHERE review_decision_id = ?",
                (review_decision_id,),
            ).fetchone()
        if row is None:
            return None
        return ReviewDecision.model_validate_json(row["payload_json"])

    def record_confirmation(
        self, decision: ReviewDecision, finding: ConfirmedSemanticFinding
    ) -> None:
        """Write a confirmation and the finding it creates in one transaction.

        A confirmation is one decision that produces two durable rows. Writing them separately
        leaves a window where the state machine says ``confirmed`` and no finding can be traced
        to the person who decided — so both rows commit together, or neither does. The exception
        path rolls the transaction back through the connection's own context manager.
        """

        if decision.kind != "human_confirm" or decision.to_status != "confirmed":
            raise ValueError(
                "record_confirmation writes a human confirmation; "
                f"got kind={decision.kind!r} to_status={decision.to_status!r}"
            )
        if finding.candidate_id != decision.candidate_id:
            raise ValueError("the finding and the decision must belong to the same candidate")
        if finding.review_decision_id != decision.review_decision_id:
            raise ValueError("the finding must cite the decision that produced it")
        with self._lock, self._connect() as connection:
            self._insert_review_decision(connection, decision)
            self._insert_confirmed_finding(connection, finding)

    def _insert_review_decision(
        self, connection: sqlite3.Connection, decision: ReviewDecision
    ) -> None:
        baseline = decision.baseline
        connection.execute(
            """
            INSERT INTO review_decisions (
                review_decision_id, candidate_id, kind, is_human, from_status, to_status,
                reviewer_identity, reviewer_action, baseline_revision, baseline_path,
                baseline_digest, decided_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.review_decision_id,
                decision.candidate_id,
                decision.kind,
                1 if decision.is_human else 0,
                decision.from_status,
                decision.to_status,
                decision.reviewer_identity,
                decision.reviewer_action,
                baseline.baseline_revision if baseline is not None else None,
                baseline.comparison_path if baseline is not None else "",
                baseline.value_digest if baseline is not None else "",
                decision.decided_at.isoformat(),
                self._encode(decision.model_dump(mode="json", by_alias=True)),
            ),
        )

    def _insert_confirmed_finding(
        self, connection: sqlite3.Connection, finding: ConfirmedSemanticFinding
    ) -> None:
        connection.execute(
            """
            INSERT INTO confirmed_semantic_findings (
                finding_id, candidate_id, review_decision_id, source_document_id,
                source_revision, region_id, candidate_type, baseline_revision,
                confirmed_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                finding.finding_id,
                finding.candidate_id,
                finding.review_decision_id,
                finding.artifact.source_document_id,
                finding.artifact.source_revision,
                finding.region_id,
                finding.candidate_type,
                finding.baseline.baseline_revision,
                finding.confirmed_at.isoformat(),
                self._encode(finding.model_dump(mode="json", by_alias=True)),
            ),
        )

    def insert_confirmed_finding(self, finding: ConfirmedSemanticFinding) -> None:
        with self._lock, self._connect() as connection:
            self._insert_confirmed_finding(connection, finding)

    def get_confirmed_finding(self, finding_id: str) -> ConfirmedSemanticFinding | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM confirmed_semantic_findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
        if row is None:
            return None
        return ConfirmedSemanticFinding.model_validate_json(row["payload_json"])

    def list_confirmed_findings(
        self, *, source_document_id: str | None = None
    ) -> list[ConfirmedSemanticFinding]:
        query = "SELECT payload_json FROM confirmed_semantic_findings"
        params: tuple[Any, ...] = ()
        if source_document_id is not None:
            query += " WHERE source_document_id = ?"
            params = (source_document_id,)
        query += " ORDER BY confirmed_at ASC, finding_id ASC"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [ConfirmedSemanticFinding.model_validate_json(row["payload_json"]) for row in rows]

    # ----------------------------------------------------------------------------------
    # M7 phase 2A: proposal evidence. Append-only on purpose -- a replan must add a row,
    # never rewrite the record of what was proposed before it.
    # ----------------------------------------------------------------------------------

    def append_synthesis_proposal_evidence(self, evidence: SynthesisProposalEvidence) -> None:
        problems = evidence.problems()
        if problems:
            raise ValueError(
                "refusing to persist an incoherent proposal record: " + "; ".join(problems)
            )
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                f"SELECT 1 FROM {PROPOSAL_EVIDENCE_TABLE} WHERE proposal_evidence_id = ?",
                (evidence.proposal_evidence_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f"proposal evidence {evidence.proposal_evidence_id} already exists; "
                    "the table is append-only"
                )
            connection.execute(
                f"""
                INSERT INTO {PROPOSAL_EVIDENCE_TABLE} (
                    proposal_evidence_id, session_id, document_id, proposal_attempt_index,
                    operation_accounting, validity, completeness, proposed_operation_count,
                    accepted_operation_count, compiled_operation_count, rejected_operation_count,
                    global_failure_reason, proposal_payload_digest, assessment_digest,
                    related_tool_call_id, created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.proposal_evidence_id,
                    evidence.session_id,
                    evidence.document_id,
                    evidence.proposal_attempt_index,
                    evidence.operation_accounting,
                    evidence.validity,
                    evidence.completeness,
                    evidence.proposed_operation_count,
                    evidence.accepted_operation_count,
                    evidence.compiled_operation_count,
                    evidence.rejected_operation_count,
                    evidence.global_failure_reason,
                    evidence.proposal_payload_digest,
                    evidence.assessment_digest,
                    evidence.related_tool_call_id,
                    evidence.created_at.isoformat(),
                    self._encode(evidence.model_dump(mode="json")),
                ),
            )
            connection.commit()

    def get_synthesis_proposal_evidence(
        self, proposal_evidence_id: str
    ) -> SynthesisProposalEvidence | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"SELECT payload_json FROM {PROPOSAL_EVIDENCE_TABLE} "
                "WHERE proposal_evidence_id = ?",
                (proposal_evidence_id,),
            ).fetchone()
        if row is None:
            return None
        return SynthesisProposalEvidence.model_validate_json(row["payload_json"])

    def list_synthesis_proposal_evidence(
        self, *, session_id: str | None = None
    ) -> list[SynthesisProposalEvidence]:
        query = f"SELECT payload_json FROM {PROPOSAL_EVIDENCE_TABLE}"
        params: tuple[Any, ...] = ()
        if session_id is not None:
            query += " WHERE session_id = ?"
            params = (session_id,)
        query += " ORDER BY proposal_attempt_index ASC, created_at ASC, proposal_evidence_id ASC"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [SynthesisProposalEvidence.model_validate_json(row["payload_json"]) for row in rows]

    def latest_synthesis_proposal_evidence(
        self, session_id: str
    ) -> SynthesisProposalEvidence | None:
        """The most recent attempt for a session, which is the one session completion answers to."""

        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"SELECT payload_json FROM {PROPOSAL_EVIDENCE_TABLE} WHERE session_id = ? "
                "ORDER BY proposal_attempt_index DESC, created_at DESC, "
                "proposal_evidence_id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return SynthesisProposalEvidence.model_validate_json(row["payload_json"])

    def prune_project_index(self) -> list[str]:
        """Drop index rows whose document no longer exists; return their ids."""

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT document_id FROM project_index "
                "WHERE document_id NOT IN (SELECT id FROM documents) "
                "ORDER BY document_id ASC"
            ).fetchall()
            orphans = [str(row["document_id"]) for row in rows]
            if orphans:
                connection.execute(
                    "DELETE FROM project_index WHERE document_id NOT IN (SELECT id FROM documents)"
                )
        return orphans
