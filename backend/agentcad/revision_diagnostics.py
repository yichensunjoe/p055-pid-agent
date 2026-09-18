"""Canonical operational diagnostics emission for one committed revision.

Design note
-----------
The evidence chain (``document_audit`` + ``document_history_details``) is written
atomically by :class:`DocumentService`. The diagnostics log is the separate,
redacted *operational* log that operators and the quality harness read. Before
T0.5 every adapter emitted ``document.revision.created`` itself, so the fields
depended on which surface you came through and legacy surfaces emitted nothing.

This module derives the diagnostics event *from the committed audit record*, so
REST, MCP, CLI and internal callers all log the same canonical fields, and the
log can never claim something the evidence chain does not contain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .diagnostics import DiagnosticLogger
from .models import Document, HistorySource

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a module import cycle
    from .service import DocumentService

__all__ = ["emit_revision_diagnostics"]


def emit_revision_diagnostics(
    service: DocumentService,
    after: Document,
    *,
    action: str,
    source: HistorySource,
    diagnostics: DiagnosticLogger | None,
    label: str = "",
    operation_count: int = 1,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Emit one ``document.revision.created`` event from persisted audit evidence.

    Returns the audit evidence dict (empty when no record exists yet) so callers can
    reuse it without a second read. Never raises: diagnostics must not be able to
    fail a committed write.
    """
    record = service.store.get_audit_record_for_revision(after.id, after.revision)
    evidence = dict(record.evidence) if record is not None else {}
    if diagnostics is None:
        return evidence
    diagnostics.emit(
        "document.revision.created",
        document_id=after.id,
        base_revision=record.base_revision if record else None,
        revision=after.revision,
        source=source,
        action=action,
        label=label or (record.label if record else action.title()),
        operation_count=operation_count,
        affected_element_ids=evidence.get("changed_entity_ids", []),
        added_element_ids=evidence.get("added_element_ids", []),
        updated_element_ids=evidence.get("updated_element_ids", []),
        deleted_element_ids=evidence.get("deleted_element_ids", []),
        audit_record_id=record.record_id if record else "",
        audit_ordinal=record.ordinal if record else None,
        diff_hash=record.diff_hash if record else "",
        semantic_diff_persisted=True,
        **(extra or {}),
    )
    return evidence
