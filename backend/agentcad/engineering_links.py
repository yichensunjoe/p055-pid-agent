"""M12-D3: cross-domain engineering links + governed mutation (Gate CODE GO).

Design frozen at reports/m12-d1-design.md (M12-D1 DESIGN PASS, R77/F77/R78
amendments ratified). Scope: create / re-pin / soft-delete of
`cable_endpoint_equipment` links with fail-closed relation invariants, the
equipment predicate, and governance audit on the existing global audit hash
chain (no hash-formation or ordinal change). D4 validator/readiness and D5
package/UI are explicitly NOT part of this slice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .audit_models import AuditRecordDraft
from .cable_service import CableDocumentNotFoundError, CableService
from .service import DocumentNotFoundError, DocumentService
from .store import SQLiteDocumentStore, StoreDocumentConflictError

RELATION_CABLE_ENDPOINT_EQUIPMENT = "cable_endpoint_equipment"
_ENDPOINTS = ("from", "to")

# Equipment predicate (frozen at reports/m12-d1-design.md Q3 / R77-Q3): an
# element is equipment-eligible iff its symbol category is NOT the instrument
# category — the repo's "equipment is everything that is not an instrument"
# rule (EQUIPMENT_SYMBOL_CATEGORIES_ARE_THE_REST). The canonical constant lives
# in the M7 layout contract module, which this module may not import (M7 phase
# import discipline); this local binding is pinned to the canonical value by
# test_m12_d3_links.py, so a future change to the contract fails closed here.
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
    """Governed cross-domain link mutation.

    Invariants (R77-Q2, all fail-closed before any write):
      1. cable_endpoint_equipment: source must be a cable document, target a
         P&ID document (domains come from documents_registry, never stored).
      2. source_endpoint in {'from', 'to'}.
      3. both documents are members of the link's project.
      4. pins are derived from the CURRENT revisions at create/re-pin time.
      5. at most one active link per cable endpoint (partial unique index is
         the transactional backstop).
      6. equipment predicate: the target element must exist in the current
         P&ID revision and its symbol category must not be the instrument
         category (the instrument category; the repo's equipment rule is
         "everything that is not an instrument", EQUIPMENT_SYMBOL_CATEGORIES_ARE_THE_REST).
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

    # ---- reads (zero audit) ----

    def get_link(self, link_id: str) -> EngineeringLinkView | None:
        row = self._store.get_engineering_link(link_id)
        return None if row is None else self._to_view(row)

    def list_active_links(self, project_id: str) -> list[EngineeringLinkView]:
        return [self._to_view(row) for row in self._store.list_active_engineering_links(project_id)]

    # ---- governed mutations ----

    def create_link(
        self,
        *,
        project_id: str,
        source_document_id: str,
        source_object_ref: str,
        source_endpoint: str,
        target_document_id: str,
        target_object_ref: str,
        actor: str,
    ) -> EngineeringLinkView:
        pins = self._resolve_pins(
            project_id=project_id,
            source_document_id=source_document_id,
            source_object_ref=source_object_ref,
            source_endpoint=source_endpoint,
            target_document_id=target_document_id,
            target_object_ref=target_object_ref,
        )
        link_id = f"lnk_{uuid4().hex[:12]}"
        audit = AuditRecordDraft(
            event_type="engineering_link.created",
            actor=actor,
            surface="internal",
            tool_name="engineering-link-service",
            status="applied",
            project_id=project_id,
            label=(
                f"Create {RELATION_CABLE_ENDPOINT_EQUIPMENT} link "
                f"{source_object_ref}:{source_endpoint} -> {target_object_ref}"
            ),
            evidence={
                "link_id": link_id,
                "relation_type": RELATION_CABLE_ENDPOINT_EQUIPMENT,
                "source_document_id": source_document_id,
                "source_object_ref": source_object_ref,
                "source_endpoint": source_endpoint,
                "target_document_id": target_document_id,
                "target_object_ref": target_object_ref,
                "pinned_source_revision": pins[0],
                "pinned_target_revision": pins[1],
            },
        )
        try:
            self._store.insert_engineering_link(
                link_id=link_id,
                project_id=project_id,
                relation_type=RELATION_CABLE_ENDPOINT_EQUIPMENT,
                source_domain="cable",
                source_document_id=source_document_id,
                source_object_ref=source_object_ref,
                source_endpoint=source_endpoint,
                target_domain="pid",
                target_document_id=target_document_id,
                target_object_ref=target_object_ref,
                pinned_source_revision=pins[0],
                pinned_target_revision=pins[1],
                created_by=actor,
                audit=audit,
            )
        except StoreDocumentConflictError as exc:
            raise EngineeringLinkError(
                "endpoint_already_connected",
                f"cable endpoint {source_object_ref!r}:{source_endpoint!r} already has an "
                "active cable_endpoint_equipment link",
            ) from exc
        view = self.get_link(link_id)
        assert view is not None
        return view

    def repin_link(self, *, link_id: str, actor: str) -> EngineeringLinkView:
        row = self._require_active(link_id)
        pins = self._resolve_pins(
            project_id=str(row["project_id"]),
            source_document_id=str(row["source_document_id"]),
            source_object_ref=str(row["source_object_ref"]),
            source_endpoint=str(row["source_endpoint"]),
            target_document_id=str(row["target_document_id"]),
            target_object_ref=str(row["target_object_ref"]),
        )
        audit = AuditRecordDraft(
            event_type="engineering_link.repinned",
            actor=actor,
            surface="internal",
            tool_name="engineering-link-service",
            status="applied",
            project_id=str(row["project_id"]),
            label=f"Re-pin link {link_id} to current revisions",
            evidence={
                "link_id": link_id,
                "pinned_source_revision": pins[0],
                "pinned_target_revision": pins[1],
            },
        )
        if not self._store.update_engineering_link_pins(
            link_id=link_id,
            pinned_source_revision=pins[0],
            pinned_target_revision=pins[1],
            audit=audit,
        ):
            raise EngineeringLinkError("link_not_active", f"link {link_id!r} is not active")
        view = self.get_link(link_id)
        assert view is not None
        return view

    def soft_delete_link(self, *, link_id: str, actor: str) -> None:
        row = self._store.get_engineering_link(link_id)
        if row is None:
            raise EngineeringLinkError("link_not_found", f"link {link_id!r} does not exist")
        if str(row["deleted_at"]):
            raise EngineeringLinkError("link_already_deleted", f"link {link_id!r} is already deleted")
        audit = AuditRecordDraft(
            event_type="engineering_link.deleted",
            actor=actor,
            surface="internal",
            tool_name="engineering-link-service",
            status="applied",
            project_id=str(row["project_id"]),
            label=f"Soft-delete link {link_id}",
            evidence={"link_id": link_id},
        )
        if not self._store.soft_delete_engineering_link(
            link_id=link_id, deleted_by=actor, audit=audit
        ):
            raise EngineeringLinkError("link_not_found", f"link {link_id!r} does not exist")

    # ---- internals ----

    def _require_active(self, link_id: str) -> dict[str, Any]:
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
        source_domain = self._store.document_domain(source_document_id)
        target_domain = self._store.document_domain(target_document_id)
        if source_domain is None or target_domain is None:
            raise EngineeringLinkError(
                "unknown_document",
                "both link endpoints must be registered documents",
            )
        if source_domain != "cable" or target_domain != "pid":
            raise EngineeringLinkError(
                "relation_orientation",
                "cable_endpoint_equipment requires source=cable and target=pid",
            )
        if source_endpoint not in _ENDPOINTS:
            raise EngineeringLinkError(
                "invalid_endpoint",
                f"source_endpoint must be one of {_ENDPOINTS}",
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
                "missing_source_object",
                f"cable document {source_document_id!r} not found",
            ) from None
        segment = next(
            (s for s in cable_view.document.segments if s.id == source_object_ref), None
        )
        if segment is None:
            raise EngineeringLinkError(
                "missing_source_object",
                f"cable segment {source_object_ref!r} not found in {source_document_id!r}",
            )
        try:
            document = self._pid.get_document(target_document_id)
        except DocumentNotFoundError:
            raise EngineeringLinkError(
                "missing_target_object",
                f"pid document {target_document_id!r} not found",
            ) from None
        element = next((e for e in document.elements if e.id == target_object_ref), None)
        if element is None:
            raise EngineeringLinkError(
                "missing_target_object",
                f"element {target_object_ref!r} not found in {target_document_id!r}",
            )
        symbol_key = getattr(element, "symbol_key", None)
        if not symbol_key:
            raise EngineeringLinkError(
                "target_not_equipment",
                f"target element {target_object_ref!r} carries no symbol identity",
            )
        try:
            category = self._pid.symbols.get(symbol_key).category
        except KeyError:
            raise EngineeringLinkError(
                "target_not_equipment",
                f"target element symbol {symbol_key!r} is not in the catalogue",
            ) from None
        if category == EQUIPMENT_PREDICATE_INSTRUMENT_CATEGORY:
            raise EngineeringLinkError(
                "target_not_equipment",
                f"target element {target_object_ref!r} is an instrument, not equipment",
            )
        return cable_view.document.revision, document.revision

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
