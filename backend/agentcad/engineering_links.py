"""M12-D3: cross-domain engineering links + governed mutation (Gate CODE GO).

Design frozen at reports/m12-d1-design.md (M12-D1 DESIGN PASS, R77/F77/R78
amendments ratified). Charter §55B(4) freeze: link mutations enter the M10
runtime permission/session/audit boundary — there is NO direct actor-bypass
write path (D79-1). The only write paths are the three commit_engineering_link_*
store methods, driven through AgentHarnessRuntime + ProjectLinkDomainAdapter;
each re-validates every frozen invariant inside a BEGIN IMMEDIATE transaction
(D79-2) and lands mutation + governance audit + tool-call closure + approval
consumption + session closure atomically.

Equipment predicate (R77-Q3, fail-closed at write time): the target element
must exist in the current P&ID revision and its symbol category must not be
the instrument category. The canonical constant lives in the M7 layout
contract, which this module may not import (M7 phase import discipline); the
local binding below is pinned to the canonical value by test.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .audit_models import AuditRecordDraft
from .cable_service import CableDocumentNotFoundError, CableService
from .runtime.ports import AuditEvent, ClosureRequest, ExecutionOutcome
from .service import DocumentNotFoundError, DocumentService
from .store import (
    SQLiteDocumentStore,
    StoreDocumentConflictError,
    StoreDocumentIdentityError,
    StoreRevisionConflictError,
)

RELATION_CABLE_ENDPOINT_EQUIPMENT = "cable_endpoint_equipment"
_ENDPOINTS = ("from", "to")

# Equipment predicate (frozen at reports/m12-d1-design.md Q3 / R77-Q3): an
# element is equipment-eligible iff its symbol category is NOT the instrument
# category — the repo's "equipment is everything that is not an instrument"
# rule (EQUIPMENT_SYMBOL_CATEGORIES_ARE_THE_REST). The canonical constant lives
# in the M7 layout contract module, which this module may not import (M7
# phase import discipline, enforced textually by the existing test); this
# local binding is pinned to the canonical value by test_m12_d3_links.py.
EQUIPMENT_PREDICATE_INSTRUMENT_CATEGORY = "仪表"


class EngineeringLinkError(RuntimeError):
    """Fail-closed link mutation refusal with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


@dataclass(frozen=True)
class EngineeringLinkView:
    link_id: str
    project_id: str
    relation_type: str
    source_document_id: str
    source_object_ref: str
    source_endpoint: str
    target_document_id: str
    target_object_ref: str
    pinned_source_revision: int
    pinned_target_revision: int
    deleted: bool


class EngineeringLinkService:
    """Governed cross-domain link plane.

    Public surface:
      - reads: get_link / list_active_links (zero audit);
      - preview(intent): early stable-error checks for the adapter (pure reads;
        the authoritative re-check happens inside the store commit);
      - binding_document_id(intent): the document a runtime authorization binds
        to (the cable source document);
      - execute_create / execute_repin / execute_delete: called ONLY by the
        runtime adapter with an authorized context.
    """

    def __init__(
        self,
        store: SQLiteDocumentStore,
        pid_service: DocumentService,
        cable_service: CableService,
    ) -> None:
        self._store = store
        self._pid = pid_service
        self._cable = cable_service
        symbols = pid_service.symbols._symbols  # noqa: SLF001 - static catalogue snapshot
        self._known_symbol_keys = frozenset(symbols)
        self._instrument_symbol_keys = frozenset(
            key for key, symbol in symbols.items()
            if symbol.category == EQUIPMENT_PREDICATE_INSTRUMENT_CATEGORY
        )

    # ---- reads (zero audit) ----

    def get_link(self, link_id: str) -> EngineeringLinkView | None:
        row = self._store.get_engineering_link(link_id)
        return None if row is None else self._to_view(row)

    def list_active_links(self, project_id: str) -> list[EngineeringLinkView]:
        return [
            self._to_view(row) for row in self._store.list_active_engineering_links(project_id)
        ]

    # ---- early checks / binding (pure reads) ----

    def resolve_current_pins(self, intent: Any) -> tuple[int, int]:
        """Approval-time revision binding: the exact state the approval is
        issued against (D79-2). Returns (current cable, current pid)."""
        return self._resolve_pins(
            project_id=intent.project_id,
            source_document_id=intent.source_document_id,
            source_object_ref=intent.source_object_ref,
            source_endpoint=intent.source_endpoint,
            target_document_id=intent.target_document_id,
            target_object_ref=intent.target_object_ref,
        )

    def preview_create(self, intent: Any) -> None:
        """Raise EngineeringLinkError early when a frozen invariant already
        fails. The store commit re-checks everything inside the write
        transaction; this only converts predictable failures into stable codes
        before the harness is consumed."""
        self._resolve_pins(
            project_id=intent.project_id,
            source_document_id=intent.source_document_id,
            source_object_ref=intent.source_object_ref,
            source_endpoint=intent.source_endpoint,
            target_document_id=intent.target_document_id,
            target_object_ref=intent.target_object_ref,
        )

    def binding_document_id(self, intent: Any) -> str:
        """Runtime authorization binds to the cable source document."""
        if getattr(intent, "source_document_id", None):
            return str(intent.source_document_id)
        link_id = str(intent.link_id)
        row = self._require_link_row(link_id)
        return str(row["source_document_id"])

    # ---- governed execution (runtime adapter only) ----

    def execute_create(
        self,
        *,
        authorized: Any,
        audit_event: AuditEvent,
        closure: ClosureRequest,
        intent: Any,
    ) -> ExecutionOutcome:
        try:
            self._check_document_binding(authorized, intent.source_document_id)
            self._check_revision_binding(
                authorized,
                intent.expected_source_revision,
                getattr(intent, "expected_target_revision", None),
            )
        except EngineeringLinkError as exc:
            self.failure_closeout(authorized, audit_event, exc.code)
            raise
        link_id = f"lnk_{uuid4().hex[:12]}"
        audit = self._success_audit(
            authorized,
            audit_event,
            event_type="engineering_link.created",
            project_id=intent.project_id,
            label=(
                f"Create {RELATION_CABLE_ENDPOINT_EQUIPMENT} link "
                f"{intent.source_object_ref}:{intent.source_endpoint} "
                f"-> {intent.target_object_ref}"
            ),
            evidence={
                "link_id": link_id,
                "relation_type": RELATION_CABLE_ENDPOINT_EQUIPMENT,
                "source_object_ref": intent.source_object_ref,
                "source_endpoint": intent.source_endpoint,
                "target_document_id": intent.target_document_id,
                "target_object_ref": intent.target_object_ref,
            },
        )
        try:
            pins = self._store.commit_engineering_link_create(
                link_id=link_id,
                project_id=intent.project_id,
                relation_type=RELATION_CABLE_ENDPOINT_EQUIPMENT,
                source_document_id=intent.source_document_id,
                source_object_ref=intent.source_object_ref,
                source_endpoint=intent.source_endpoint,
                target_document_id=intent.target_document_id,
                target_object_ref=intent.target_object_ref,
                known_symbol_keys=self._known_symbol_keys,
                instrument_symbol_keys=self._instrument_symbol_keys,
                expected=(
                    intent.expected_source_revision,
                    intent.expected_target_revision,
                ),
                created_by=audit_event.actor,
                audit=audit,
                tool_call=self._closed_tool_call(authorized),
                approval=self._consumed_approval(authorized, closure),
                session=self._closed_session(authorized, closure),
            )
        except StoreDocumentConflictError as exc:
            self.failure_closeout(authorized, audit_event, "endpoint_already_connected")
            raise EngineeringLinkError(
                "endpoint_already_connected",
                f"cable endpoint {intent.source_object_ref!r}:{intent.source_endpoint!r} "
                "already has an active cable_endpoint_equipment link",
            ) from exc
        except StoreRevisionConflictError as exc:
            self.failure_closeout(authorized, audit_event, "revision_conflict")
            raise EngineeringLinkError("revision_conflict", str(exc)) from exc
        except ValueError as exc:
            self.failure_closeout(authorized, audit_event, str(exc).split(":", 1)[0])
            raise
        except sqlite3.IntegrityError:
            # R79-F3: a non-endpoint storage constraint fault keeps its real
            # identity instead of masquerading as an endpoint conflict.
            self.failure_closeout(authorized, audit_event, "storage_integrity_failure")
            raise
        return ExecutionOutcome(
            document_id=intent.source_document_id,
            base_revision=None,
            result_revision=pins[0],
            payload={"link_id": link_id, "pinned_source_revision": pins[0], "pinned_target_revision": pins[1]},
        )

    def execute_repin(
        self,
        *,
        authorized: Any,
        audit_event: AuditEvent,
        closure: ClosureRequest,
        intent: Any,
    ) -> ExecutionOutcome:
        link_id = str(intent.link_id)
        try:
            row = self._require_link_row(link_id)
        except EngineeringLinkError as exc:
            # R79-F2: authorized harness must never half-close — a link that
            # vanished (or was deleted) after authorization fails closed with
            # a stable code and a full rejection closeout.
            self.failure_closeout(authorized, audit_event, exc.code)
            raise
        try:
            self._check_document_binding(authorized, str(row["source_document_id"]))
            self._check_revision_binding(
                authorized,
                intent.expected_source_revision,
                getattr(intent, "expected_target_revision", None),
            )
        except EngineeringLinkError as exc:
            self.failure_closeout(authorized, audit_event, exc.code)
            raise
        audit = self._success_audit(
            authorized,
            audit_event,
            event_type="engineering_link.repinned",
            project_id=str(row["project_id"]),
            label=f"Re-pin link {link_id} to current revisions",
            evidence={"link_id": link_id},
        )
        try:
            pins = self._store.commit_engineering_link_repin(
                link_id=link_id,
                known_symbol_keys=self._known_symbol_keys,
                instrument_symbol_keys=self._instrument_symbol_keys,
                expected=(
                    intent.expected_source_revision,
                    intent.expected_target_revision,
                ),
                audit=audit,
                tool_call=self._closed_tool_call(authorized),
                approval=self._consumed_approval(authorized, closure),
                session=self._closed_session(authorized, closure),
            )
        except StoreRevisionConflictError as exc:
            self.failure_closeout(authorized, audit_event, "revision_conflict")
            raise EngineeringLinkError("revision_conflict", str(exc)) from exc
        except StoreDocumentIdentityError as exc:
            self.failure_closeout(authorized, audit_event, "link_not_active")
            raise EngineeringLinkError("link_not_active", str(exc)) from exc
        except ValueError as exc:
            self.failure_closeout(authorized, audit_event, str(exc).split(":", 1)[0])
            raise
        return ExecutionOutcome(
            document_id=str(row["source_document_id"]),
            base_revision=None,
            result_revision=pins[0],
            payload={"link_id": link_id, "pinned_source_revision": pins[0], "pinned_target_revision": pins[1]},
        )

    def execute_delete(
        self,
        *,
        authorized: Any,
        audit_event: AuditEvent,
        closure: ClosureRequest,
        intent: Any,
    ) -> ExecutionOutcome:
        link_id = str(intent.link_id)
        try:
            row = self._require_link_row(link_id)
        except EngineeringLinkError as exc:
            self.failure_closeout(authorized, audit_event, exc.code)
            raise
        try:
            self._check_document_binding(authorized, str(row["source_document_id"]))
        except EngineeringLinkError as exc:
            self.failure_closeout(authorized, audit_event, exc.code)
            raise
        audit = self._success_audit(
            authorized,
            audit_event,
            event_type="engineering_link.deleted",
            project_id=str(row["project_id"]),
            label=f"Soft-delete link {link_id}",
            evidence={"link_id": link_id},
        )
        try:
            self._store.commit_engineering_link_delete(
                link_id=link_id,
                deleted_by=audit_event.actor,
                audit=audit,
                tool_call=self._closed_tool_call(authorized),
                approval=self._consumed_approval(authorized, closure),
                session=self._closed_session(authorized, closure),
            )
        except StoreDocumentIdentityError as exc:
            self.failure_closeout(authorized, audit_event, "link_not_active")
            raise EngineeringLinkError("link_not_active", str(exc)) from exc
        return ExecutionOutcome(
            document_id=str(row["source_document_id"]),
            base_revision=None,
            result_revision=int(row["pinned_source_revision"]),
            payload={"link_id": link_id, "deleted": True},
        )

    # ---- internals ----

    def _check_document_binding(self, authorized: Any, source_document_id: str) -> None:
        """D79-1: the runtime authorization must be bound to the mutation's
        real cable source document — never some other document the caller
        happened to open a session on."""
        if authorized.record.document_id != source_document_id:
            raise EngineeringLinkError(
                "document_binding_mismatch",
                "authorization document does not match the link cable source",
            )

    def _check_revision_binding(
        self, authorized: Any, expected_source: int | None, expected_target: int | None
    ) -> None:
        """D79-2: the authorization base_revision must equal the
        approval-bound source revision, and the approval must carry both
        expected revisions (server-enriched at request time)."""
        if expected_source is None or expected_target is None:
            raise EngineeringLinkError(
                "approval_state_unbound",
                "approval intent lacks server-bound expected revisions",
            )
        if authorized.record.base_revision != expected_source:
            raise EngineeringLinkError(
                "authorization_binding_mismatch",
                "authorization base_revision does not match the approved source revision",
            )

    def _require_link_row(self, link_id: str) -> dict[str, Any]:
        row = self._store.get_engineering_link(link_id)
        if row is None:
            raise EngineeringLinkError("link_not_found", f"link {link_id!r} does not exist")
        if str(row["deleted_at"]):
            raise EngineeringLinkError("link_already_deleted", f"link {link_id!r} is already deleted")
        return row

    def _resolve_pins(
        self,
        *,
        project_id: str,
        source_document_id: str,
        source_object_ref: str,
        source_endpoint: str,
        target_document_id: str,
        target_object_ref: str,
    ) -> tuple[int, int]:
        """Pure-read mirror of the frozen invariants for early stable errors."""
        source_domain = self._store.document_domain(source_document_id)
        target_domain = self._store.document_domain(target_document_id)
        if source_domain is None or target_domain is None:
            raise EngineeringLinkError(
                "unknown_document", "both link endpoints must be registered documents"
            )
        if source_domain != "cable" or target_domain != "pid":
            raise EngineeringLinkError(
                "relation_orientation",
                "cable_endpoint_equipment requires source=cable and target=pid",
            )
        if source_endpoint not in _ENDPOINTS:
            raise EngineeringLinkError(
                "invalid_endpoint", f"source_endpoint must be one of {_ENDPOINTS}"
            )
        members = {
            document_id
            for document_id, _domain, _added_at in self._store.list_project_documents(project_id)
        }
        if source_document_id not in members:
            raise EngineeringLinkError(
                "source_not_in_project",
                f"cable document {source_document_id!r} is not a member of project {project_id!r}",
            )
        if target_document_id not in members:
            raise EngineeringLinkError(
                "target_not_in_project",
                f"pid document {target_document_id!r} is not a member of project {project_id!r}",
            )
        try:
            cable_view = self._cable.load(source_document_id)
        except CableDocumentNotFoundError:
            raise EngineeringLinkError(
                "missing_source_object", f"cable document {source_document_id!r} not found"
            ) from None
        if not any(s.id == source_object_ref for s in cable_view.document.segments):
            raise EngineeringLinkError(
                "missing_source_object",
                f"cable segment {source_object_ref!r} not found in {source_document_id!r}",
            )
        try:
            document = self._pid.get_document(target_document_id)
        except DocumentNotFoundError:
            raise EngineeringLinkError(
                "missing_target_object", f"pid document {target_document_id!r} not found"
            ) from None
        element = next((e for e in document.elements if e.id == target_object_ref), None)
        if element is None:
            raise EngineeringLinkError(
                "missing_target_object",
                f"element {target_object_ref!r} not found in {target_document_id!r}",
            )
        symbol_key = getattr(element, "symbol_key", None)
        if not symbol_key or symbol_key not in self._known_symbol_keys:
            raise EngineeringLinkError(
                "target_not_equipment", f"target element symbol {symbol_key!r} is not in the catalogue"
            )
        if symbol_key in self._instrument_symbol_keys:
            raise EngineeringLinkError(
                "target_not_equipment",
                f"target element {target_object_ref!r} is an instrument, not equipment",
            )
        return cable_view.document.revision, document.revision

    @staticmethod
    def _success_audit(
        authorized: Any,
        audit_event: AuditEvent,
        *,
        event_type: str,
        project_id: str,
        label: str,
        evidence: dict,
    ) -> AuditRecordDraft:
        from .audit import validation_evidence_hash

        return AuditRecordDraft(
            event_type=event_type,
            actor=audit_event.actor,
            surface=audit_event.surface,
            tool_name=authorized.definition.name,
            status="applied",
            document_id=authorized.record.document_id,
            project_id=project_id,
            session_id=authorized.session.id,
            approval_id=authorized.approval.id if authorized.approval else None,
            tool_call_id=authorized.record.id,
            base_revision=authorized.record.base_revision,
            provider=audit_event.provider,
            model=audit_event.model,
            intent_hash=audit_event.intent_hash,
            diff_preview_hash=audit_event.diff_preview_hash,
            validation_status=audit_event.validation_status or "valid",
            validation_hash=validation_evidence_hash(audit_event.validation_evidence),
            label=label,
            evidence={**evidence, "runtime_metadata": audit_event.metadata},
        )

    def failure_closeout(self, authorized: Any, audit_event: AuditEvent, error_code: str) -> None:
        from .audit import validation_evidence_hash

        now = datetime.now(UTC)
        audit = AuditRecordDraft(
            event_type="engineering_link.rejected",
            actor=audit_event.actor,
            surface=audit_event.surface,
            tool_name=authorized.definition.name,
            status="rejected",
            error_code=error_code,
            document_id=authorized.record.document_id,
            session_id=authorized.session.id,
            approval_id=authorized.approval.id if authorized.approval else None,
            tool_call_id=authorized.record.id,
            base_revision=authorized.record.base_revision,
            provider=audit_event.provider,
            model=audit_event.model,
            intent_hash=audit_event.intent_hash,
            diff_preview_hash=audit_event.diff_preview_hash,
            validation_status=audit_event.validation_status,
            validation_hash=validation_evidence_hash(audit_event.validation_evidence),
            label=f"Engineering link mutation denied: {error_code}",
            evidence=dict(audit_event.metadata),
        )
        self._store.engineering_link_failure_closeout(
            tool_call=authorized.record.model_copy(
                update={"status": "failed", "error_code": error_code, "completed_at": now}
            ),
            session=authorized.session.model_copy(
                update={"status": "failed", "updated_at": now}
            ),
            audit=audit,
        )

    @staticmethod
    def _closed_tool_call(authorized: Any) -> Any:
        return authorized.record.model_copy(
            update={"status": "completed", "completed_at": datetime.now(UTC)}
        )

    @staticmethod
    def _consumed_approval(authorized: Any, closure: ClosureRequest) -> Any:
        if authorized.approval is None or not closure.consume_approval:
            return None
        return authorized.approval.model_copy(
            update={"status": "consumed", "consumed_at": datetime.now(UTC)}
        )

    @staticmethod
    def _closed_session(authorized: Any, closure: ClosureRequest) -> Any:
        if not closure.close_session:
            return authorized.session
        return authorized.session.model_copy(
            update={"status": "completed", "updated_at": datetime.now(UTC)}
        )

    @staticmethod
    def _to_view(row: dict[str, Any]) -> EngineeringLinkView:
        return EngineeringLinkView(
            link_id=str(row["link_id"]),
            project_id=str(row["project_id"]),
            relation_type=str(row["relation_type"]),
            source_document_id=str(row["source_document_id"]),
            source_object_ref=str(row["source_object_ref"]),
            source_endpoint=str(row["source_endpoint"]),
            target_document_id=str(row["target_document_id"]),
            target_object_ref=str(row["target_object_ref"]),
            pinned_source_revision=int(row["pinned_source_revision"]),
            pinned_target_revision=int(row["pinned_target_revision"]),
            deleted=bool(str(row["deleted_at"])),
        )
