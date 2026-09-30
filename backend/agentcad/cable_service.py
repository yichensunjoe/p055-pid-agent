"""M11-D2: Cable domain service — bootstrap lifecycle, payload IO, contexts.

The Cable loader resolves ONLY cable_documents: a P&ID id asked of the Cable
plane is a plain not-found (fail-closed isolation, mirrored by the P&ID
loader which never touches cable tables).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from .audit_models import AuditRecordDraft
from .cable_models import (
    CABLE_DOCUMENT_SCHEMA,
    CableDocument,
    parse_cable_payload,
    serialize_cable_payload,
)
from .runtime.ports import DocumentContext
from .store import SQLiteDocumentStore


class CableDocumentNotFoundError(KeyError):
    code = "cable_document_not_found"


@dataclass(frozen=True)
class CableDocumentView:
    document_id: str
    document: CableDocument


class CableService:
    def __init__(self, store: SQLiteDocumentStore) -> None:
        self.store = store

    @staticmethod
    def new_document_id() -> str:
        return f"cab_{uuid4().hex[:12]}"

    def create_document(self, name: str = "cable schematic") -> CableDocumentView:
        """Bootstrap (R11-D2-4): provisioning, audited, single transaction."""
        document_id = self.new_document_id()
        document = CableDocument(schema=CABLE_DOCUMENT_SCHEMA, name=name)
        audit = AuditRecordDraft(
            event_type="document.created",
            actor="cable-service",
            surface="internal",
            tool_name="cable.create_document",
            status="applied",
            document_id=document_id,
            label=f"Bootstrap cable document {name}",
            evidence={"cable_document_id": document_id},
        )
        self.store.create_cable_document(
            document_id=document_id,
            data_json=serialize_cable_payload(document),
            audit=audit,
        )
        return CableDocumentView(document_id=document_id, document=document)

    def load(self, document_id: str) -> CableDocumentView:
        envelope = self.store.get_cable_envelope(document_id)
        if envelope is None:
            raise CableDocumentNotFoundError(f"cable document {document_id!r} not found")
        revision, data_json = envelope
        document = parse_cable_payload(data_json, envelope_revision=revision)
        return CableDocumentView(document_id=document_id, document=document)

    def document_context(self, *, document_id: str) -> DocumentContext:
        envelope = self.store.get_cable_envelope(document_id)
        if envelope is None:
            raise CableDocumentNotFoundError(f"cable document {document_id!r} not found")
        return DocumentContext(document_id=document_id, revision=envelope[0])

    def persist(self, view: CableDocumentView) -> str:
        return serialize_cable_payload(view.document)
