from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from .audit_models import AuditContext, ProvenanceState
from .models import (
    AddElementOperation,
    AddLayerOperation,
    AddSystemOperation,
    ClearDocumentOperation,
    ConnectorElement,
    ConnectorEndpoint,
    CreateDocumentRequest,
    DeleteElementOperation,
    DeleteLayerOperation,
    DeleteSystemOperation,
    Document,
    Element,
    HistoryEntry,
    HistorySource,
    JunctionElement,
    Layer,
    Point,
    SymbolElement,
    SystemGroup,
    TransactionRequest,
    TransactionResult,
    UpdateElementOperation,
    UpdateLayerOperation,
    UpdateSystemOperation,
)
from .project_io import (
    ImportConflictPolicy,
    ImportResult,
    ProjectIOError,
    ProjectSettings,
    document_envelope,
    parse_document_payload,
    parse_project_payload,
    project_package,
    remap_conflicting_document_ids,
)
from .store import (
    SQLiteDocumentStore,
    StoredDocument,
    StoreDocumentConflictError,
    StoreRevisionConflictError,
)
from .symbols import SymbolRegistry

EDITOR_GROUP_KEY = "editor_group_id"
EDITOR_LOCK_KEY = "editor_locked"


def history_source_for_context(context: AuditContext | None, fallback: HistorySource | None) -> HistorySource:
    """Map server-derived attribution onto the legacy display column.

    The ``document_history.source`` column is a UI hint only. Attribution that
    matters for review lives in the audit record, which never reads a
    client-supplied ``TransactionRequest.source``.
    """
    if context is None:
        return fallback or "web"
    if context.session_id and context.surface == "rest":
        return "llm"
    if context.surface == "mcp":
        return "mcp"
    if context.surface in {"cli", "internal"} and context.actor == "system":
        return "system"
    return "web"


def revision_snapshot(document: Document) -> Document:
    """Detached copy of a document, used as the ``before`` side of a provenance diff.

    Adapters that mutate a document in place (rename, folder move) must capture this
    *before* mutating, otherwise the semantic diff and the audit record would compare
    a document with itself and report a false "no change".
    """
    return Document.model_validate(document.model_dump(mode="python"))


def context_label(context: AuditContext | None, fallback: str) -> str:
    if context is not None and context.label:
        return context.label
    return fallback


def _element_edit_locked(element: Element) -> bool:
    return element.metadata.get(EDITOR_LOCK_KEY) is True


def _unlock_only_patch(element: Element, patch: dict[str, Any]) -> bool:
    if set(patch) != {"metadata"} or not isinstance(patch.get("metadata"), dict):
        return False
    before = deepcopy(element.metadata)
    after = deepcopy(patch["metadata"])
    before.pop(EDITOR_LOCK_KEY, None)
    after.pop(EDITOR_LOCK_KEY, None)
    return before == after and patch["metadata"].get(EDITOR_LOCK_KEY) is not True


def _normalize_editor_groups(document: Document) -> None:
    members: dict[str, list[Element]] = {}
    for element in document.elements:
        group_id = element.metadata.get(EDITOR_GROUP_KEY)
        if isinstance(group_id, str) and group_id.strip():
            members.setdefault(group_id.strip(), []).append(element)
    stale = {group_id for group_id, grouped in members.items() if len(grouped) < 2}
    if not stale:
        return
    stale_elements = [
        element
        for element in document.elements
        if isinstance((group_id := element.metadata.get(EDITOR_GROUP_KEY)), str)
        and group_id.strip() in stale
    ]
    locked_stale = next((element for element in stale_elements if _element_edit_locked(element)), None)
    if locked_stale is not None:
        raise InvalidOperationError(f"element is locked: {locked_stale.id}")
    for element in stale_elements:
        element.metadata.pop(EDITOR_GROUP_KEY, None)

class DocumentNotFoundError(KeyError):
    pass


class RevisionConflictError(RuntimeError):
    pass


class InvalidOperationError(ValueError):
    pass


class DocumentService:
    def __init__(
        self,
        store: SQLiteDocumentStore,
        symbols: SymbolRegistry,
        history_limit: int = 100,
        audit=None,
    ):
        self.store = store
        self.symbols = symbols
        self.history_limit = history_limit
        if audit is None:
            # Deferred import: audit.py depends on semantic_diff.py, which imports
            # this module for its error types.
            from .audit import AuditRecorder

            audit = AuditRecorder(store=store, symbols=symbols, service=self)
        self.audit = audit

    def create_document(
        self,
        request: CreateDocumentRequest,
        *,
        source: HistorySource = "web",
        audit: AuditContext | None = None,
    ) -> Document:
        document = Document(
            name=request.name,
            canvas={"width": request.width, "height": request.height},
            metadata=request.metadata,
        )
        context = audit or AuditContext(actor="system", surface="internal", tool_name="create_document")
        draft = self.audit.build_event(
            "document.created",
            context,
            document_id=document.id,
            result_revision=document.revision,
            evidence={
                "name": document.name,
                "canvas": {"width": document.canvas.width, "height": document.canvas.height},
                "history_source": source,
            },
        )
        self.store.save(
            StoredDocument(document=document, undo_stack=[], redo_stack=[]),
            history=HistoryEntry(
                document_id=document.id,
                revision=document.revision,
                source=source,
                action="create",
                label="Create document",
                operation_count=0,
            ),
            audit=draft,
        )
        return document

    def create_document_with_operations(
        self,
        request: CreateDocumentRequest,
        *,
        operations: list[Any],
        label: str,
        source: HistorySource = "web",
        audit: AuditContext | None = None,
        provenance: dict[str, Any] | None = None,
        state: ProvenanceState | None = None,
    ) -> TransactionResult:
        """Create a document and apply a large operation set as ONE governed mutation.

        Bulk intake (CAD drawings arrive in tens of thousands of operations) needs the
        user-level semantics of a single change: one revision, one history entry, one
        audit record that carries the source binding, and one undo snapshot. Nothing is
        persisted until every operation has applied and the resulting document
        validates, so a failure part-way through cannot leave a drawing that looks
        finished but is not.

        This is a second *entry point* into the same governed path, not a second write
        channel: operation application, validation, provenance and persistence are the
        same code that ``apply_transaction`` uses.
        """

        document = Document(
            name=request.name,
            canvas={"width": request.width, "height": request.height},
            metadata=request.metadata,
        )
        context = audit or AuditContext(
            actor="system",
            surface="internal",
            tool_name="create_document_with_operations",
            label=label,
        )
        history_source = source or history_source_for_context(context, None)
        working = Document.model_validate(document.model_dump(mode="python"))
        for operation in operations:
            self._apply_operation(working, operation)
        _normalize_editor_groups(working)
        # Same revision convention as ``apply_transaction``: a new document starts at 0,
        # and the mutation that gives it content is revision 1.
        working.revision = document.revision + 1
        working.updated_at = datetime.now(UTC)
        try:
            working = Document.model_validate(working.model_dump(mode="python"))
        except ValidationError as exc:
            raise InvalidOperationError(f"resulting document is invalid: {exc}") from exc
        bundle = self.audit.build_revision_provenance(
            before=document,
            after=working,
            operations=operations,
            request=None,
            action="transaction",
            source=history_source,
            context=context,
            extra_evidence=provenance,
        )
        final = self.audit.finalize_state(state, result_revision=working.revision)
        try:
            self.store.save(
                StoredDocument(
                    document=working,
                    undo_stack=[document.model_dump(mode="json")],
                    redo_stack=[],
                ),
                history=HistoryEntry(
                    document_id=working.id,
                    revision=working.revision,
                    source=history_source,
                    action="create",
                    label=context_label(context, label),
                    operation_count=len(operations),
                ),
                history_details=bundle.history_details,
                audit=bundle.audit,
                tool_call=final.tool_call,
                approval=final.approval,
                session=final.session,
            )
        except StoreRevisionConflictError as exc:
            raise RevisionConflictError(str(exc)) from exc
        return TransactionResult(
            document=working,
            applied_operations=len(operations),
            label=label,
        )

    def list_documents(self):
        return self.store.list()

    def get_document(self, document_id: str) -> Document:
        return self._get_stored(document_id).document

    def get_history(self, document_id: str, limit: int = 100) -> list[HistoryEntry]:
        self._get_stored(document_id)
        return self.store.list_history(document_id, limit)

    def delete_document(
        self,
        document_id: str,
        expected_revision: int,
        *,
        audit: AuditContext | None = None,
    ) -> None:
        context = audit or AuditContext(
            actor="system", surface="internal", tool_name="delete_document"
        )
        snapshot = self._get_stored(document_id).document
        draft = self.audit.build_event(
            "document.deleted",
            context,
            document_id=document_id,
            base_revision=snapshot.revision,
            evidence={
                "name": snapshot.name,
                "element_count": len(snapshot.elements),
                "layer_count": len(snapshot.layers),
                "system_count": len(snapshot.systems),
            },
        )
        try:
            deleted = self.store.delete(
                document_id,
                expected_revision=expected_revision,
                audit=draft,
            )
        except StoreRevisionConflictError as exc:
            raise RevisionConflictError(str(exc)) from exc
        if not deleted:
            raise DocumentNotFoundError(document_id)

    def get_project_settings(self) -> ProjectSettings:
        return self.store.get_project_settings()

    def update_project_settings(
        self,
        settings: ProjectSettings,
        *,
        audit: AuditContext | None = None,
    ) -> ProjectSettings:
        context = audit or AuditContext(
            actor="system", surface="internal", tool_name="update_project_settings"
        )
        draft = self.audit.build_event(
            "project.settings_updated",
            context,
            evidence={"project_name": settings.name, "changed": True},
        )
        return self.store.save_project_settings(settings, audit=draft)

    def export_document_envelope(self, document_id: str):
        return document_envelope(self.get_document(document_id))

    def export_project_package(self):
        documents = [self.get_document(item.id) for item in self.list_documents()]
        if not documents:
            raise ProjectIOError("project package export requires at least one document", code="empty_project")
        return project_package(self.get_project_settings(), documents)

    def import_document_payload(
        self,
        payload: Any,
        *,
        conflict_policy: ImportConflictPolicy = "regenerate",
        audit: AuditContext | None = None,
    ) -> ImportResult:
        document = parse_document_payload(payload)
        self._validate_import_document(document)
        documents, id_map = remap_conflicting_document_ids(
            [document], self.store.document_ids(), conflict_policy
        )
        try:
            self.store.import_documents_atomic(
                documents,
                audits=self._import_audit_drafts(documents, id_map, audit, scope="document"),
            )
        except StoreDocumentConflictError as exc:
            raise ProjectIOError(
                "document id conflict occurred while importing", code="document_id_conflict"
            ) from exc
        return ImportResult(documents=documents, document_id_map=id_map)

    def import_project_payload(
        self,
        payload: Any,
        *,
        conflict_policy: ImportConflictPolicy = "regenerate",
        audit: AuditContext | None = None,
    ) -> ImportResult:
        package = parse_project_payload(payload)
        for document in package.documents:
            self._validate_import_document(document)
        documents, id_map = remap_conflicting_document_ids(
            package.documents, self.store.document_ids(), conflict_policy
        )
        try:
            self.store.import_documents_atomic(
                documents,
                project_settings=package.project,
                audits=self._import_audit_drafts(documents, id_map, audit, scope="project_package"),
            )
        except StoreDocumentConflictError as exc:
            raise ProjectIOError(
                "document id conflict occurred while importing project package",
                code="document_id_conflict",
            ) from exc
        return ImportResult(documents=documents, document_id_map=id_map, project=package.project)

    def _import_audit_drafts(
        self,
        documents: list[Document],
        id_map: dict[str, str],
        audit: AuditContext | None,
        *,
        scope: str,
    ) -> list[Any]:
        context = audit or AuditContext(
            actor="system", surface="internal", tool_name="import_documents"
        )
        drafts = []
        for document in documents:
            drafts.append(
                self.audit.build_event(
                    "document.imported",
                    context,
                    document_id=document.id,
                    result_revision=document.revision,
                    evidence={
                        "scope": scope,
                        "name": document.name,
                        "element_count": len(document.elements),
                        "original_document_id": next(
                            (key for key, value in id_map.items() if value == document.id),
                            document.id,
                        ),
                        "document_id_remapped": document.id in id_map.values(),
                    },
                )
            )
        return drafts

    def _validate_import_document(self, document: Document) -> None:
        element_map = {element.id: element for element in document.elements}
        for element in document.elements:
            if element.type == "symbol":
                try:
                    self.symbols.get(element.symbol_key)
                except KeyError as exc:
                    raise ProjectIOError(str(exc), code="unknown_symbol") from exc
                continue
            if element.type != "connector":
                continue
            points = element.points
            for endpoint_name, endpoint, point_index in (
                ("source", element.source, 0),
                ("target", element.target, -1),
            ):
                if endpoint is None:
                    continue
                if not self._same_point(endpoint.point, points[point_index]):
                    raise ProjectIOError(
                        f"connector {element.id} {endpoint_name} point does not match its route endpoint",
                        code="endpoint_route_mismatch",
                    )
                if endpoint.element_id is None:
                    continue
                referenced = element_map.get(endpoint.element_id)
                if referenced is None:
                    raise ProjectIOError(
                        f"connector {element.id} {endpoint_name} references missing element {endpoint.element_id}",
                        code="missing_endpoint_element",
                    )
                try:
                    normalized = self._normalize_endpoint(document, endpoint, endpoint_name)
                except InvalidOperationError as exc:
                    raise ProjectIOError(str(exc), code="invalid_endpoint_binding") from exc
                if not self._same_point(normalized.point, endpoint.point):
                    raise ProjectIOError(
                        f"connector {element.id} {endpoint_name} binding point is stale",
                        code="stale_endpoint_binding",
                    )

            if element.routing in {"orthogonal", "manual"}:
                for first, second in zip(points, points[1:], strict=False):
                    if first.x != second.x and first.y != second.y:
                        raise ProjectIOError(
                            f"connector {element.id} contains a non-orthogonal segment",
                            code="non_orthogonal_connector",
                        )
            if element.routing == "direct" and len(points) != 2:
                raise ProjectIOError(
                    f"direct connector {element.id} must contain exactly two points",
                    code="invalid_direct_connector",
                )

    @staticmethod
    def _same_point(first: Point, second: Point, tolerance: float = 1e-6) -> bool:
        return abs(first.x - second.x) <= tolerance and abs(first.y - second.y) <= tolerance

    def apply_transaction(
        self,
        document_id: str,
        transaction: TransactionRequest,
        *,
        source: HistorySource | None = None,
        audit: AuditContext | None = None,
        state: ProvenanceState | None = None,
    ) -> TransactionResult:
        stored = self._get_stored(document_id)
        current = stored.document
        if transaction.expected_revision is not None and transaction.expected_revision != current.revision:
            raise RevisionConflictError(
                f"expected revision {transaction.expected_revision}, current revision is {current.revision}"
            )

        working = Document.model_validate(current.model_dump(mode="python"))
        for operation in transaction.operations:
            self._apply_operation(working, operation)
        _normalize_editor_groups(working)

        working.revision = current.revision + 1
        working.updated_at = datetime.now(UTC)
        try:
            working = Document.model_validate(working.model_dump(mode="python"))
        except ValidationError as exc:
            raise InvalidOperationError(f"resulting document is invalid: {exc}") from exc
        undo_stack = [*stored.undo_stack, current.model_dump(mode="json")][-self.history_limit :]
        if audit is None:
            audit = AuditContext(
                actor="system",
                surface="internal",
                tool_name="apply_transaction",
                label=transaction.label,
            )
            history_source = source or transaction.source or "web"
        else:
            history_source = source or history_source_for_context(audit, None)
        bundle = self.audit.build_revision_provenance(
            before=current,
            after=working,
            operations=transaction.operations,
            request=transaction,
            action="transaction",
            source=history_source,
            context=audit,
        )
        final = self.audit.finalize_state(state, result_revision=working.revision)
        try:
            self.store.save(
                StoredDocument(document=working, undo_stack=undo_stack, redo_stack=[]),
                expected_revision=current.revision,
                history=HistoryEntry(
                    document_id=document_id,
                    revision=working.revision,
                    source=history_source,
                    action="transaction",
                    label=context_label(audit, transaction.label or "Apply transaction"),
                    operation_count=len(transaction.operations),
                ),
                history_details=bundle.history_details,
                audit=bundle.audit,
                tool_call=final.tool_call,
                approval=final.approval,
                session=final.session,
            )
        except StoreRevisionConflictError as exc:
            raise RevisionConflictError(str(exc)) from exc
        return TransactionResult(
            document=working,
            applied_operations=len(transaction.operations),
            label=transaction.label,
        )

    def undo(
        self,
        document_id: str,
        *,
        expected_revision: int | None = None,
        source: HistorySource = "web",
        audit: AuditContext | None = None,
    ) -> Document:
        stored = self._get_stored(document_id)
        if expected_revision is not None and expected_revision != stored.document.revision:
            raise RevisionConflictError(
                f"expected revision {expected_revision}, current revision is {stored.document.revision}"
            )
        if not stored.undo_stack:
            return stored.document
        previous = Document.model_validate(stored.undo_stack[-1])
        previous.revision = stored.document.revision + 1
        previous.updated_at = datetime.now(UTC)
        redo_stack = [*stored.redo_stack, stored.document.model_dump(mode="json")][
            -self.history_limit :
        ]
        context = audit or AuditContext(actor="system", surface="internal", tool_name="undo")
        bundle = self.audit.build_revision_provenance(
            before=stored.document,
            after=previous,
            operations=None,
            request=None,
            action="undo",
            source=history_source_for_context(context, source),
            context=context,
        )
        try:
            self.store.save(
                StoredDocument(
                    document=previous,
                    undo_stack=stored.undo_stack[:-1],
                    redo_stack=redo_stack,
                ),
                expected_revision=stored.document.revision,
                history=HistoryEntry(
                    document_id=document_id,
                    revision=previous.revision,
                    source=history_source_for_context(context, source),
                    action="undo",
                    label=context_label(context, "Undo"),
                    operation_count=1,
                ),
                history_details=bundle.history_details,
                audit=bundle.audit,
            )
        except StoreRevisionConflictError as exc:
            raise RevisionConflictError(str(exc)) from exc
        return previous

    def redo(
        self,
        document_id: str,
        *,
        expected_revision: int | None = None,
        source: HistorySource = "web",
        audit: AuditContext | None = None,
    ) -> Document:
        stored = self._get_stored(document_id)
        if expected_revision is not None and expected_revision != stored.document.revision:
            raise RevisionConflictError(
                f"expected revision {expected_revision}, current revision is {stored.document.revision}"
            )
        if not stored.redo_stack:
            return stored.document
        next_document = Document.model_validate(stored.redo_stack[-1])
        next_document.revision = stored.document.revision + 1
        next_document.updated_at = datetime.now(UTC)
        undo_stack = [*stored.undo_stack, stored.document.model_dump(mode="json")][
            -self.history_limit :
        ]
        context = audit or AuditContext(actor="system", surface="internal", tool_name="redo")
        bundle = self.audit.build_revision_provenance(
            before=stored.document,
            after=next_document,
            operations=None,
            request=None,
            action="redo",
            source=history_source_for_context(context, source),
            context=context,
        )
        try:
            self.store.save(
                StoredDocument(
                    document=next_document,
                    undo_stack=undo_stack,
                    redo_stack=stored.redo_stack[:-1],
                ),
                expected_revision=stored.document.revision,
                history=HistoryEntry(
                    document_id=document_id,
                    revision=next_document.revision,
                    source=history_source_for_context(context, source),
                    action="redo",
                    label=context_label(context, "Redo"),
                    operation_count=1,
                ),
                history_details=bundle.history_details,
                audit=bundle.audit,
            )
        except StoreRevisionConflictError as exc:
            raise RevisionConflictError(str(exc)) from exc
        return next_document

    def scene_summary(self, document_id: str) -> dict[str, Any]:
        document = self.get_document(document_id)
        by_type: dict[str, int] = {}
        symbols: list[dict[str, Any]] = []
        junctions: list[dict[str, Any]] = []
        connectors: list[dict[str, Any]] = []
        for element in document.elements:
            by_type[element.type] = by_type.get(element.type, 0) + 1
            base = {"layer_id": element.layer_id, "system_id": element.system_id}
            if element.type == "symbol":
                definition = self.symbols.get(element.symbol_key)
                symbols.append(
                    {
                        "id": element.id,
                        "symbol_key": element.symbol_key,
                        "label": element.label,
                        "position": element.position.model_dump(),
                        "properties": element.properties,
                        "ports": [port.model_dump() for port in definition.ports],
                        **base,
                    }
                )
            elif element.type == "junction":
                junctions.append(
                    {
                        "id": element.id,
                        "position": element.position.model_dump(),
                        "label": element.label,
                        **base,
                    }
                )
            elif element.type == "connector":
                connectors.append(
                    {
                        "id": element.id,
                        "process_tag": element.process_tag,
                        "medium": element.medium,
                        "nominal_diameter": element.nominal_diameter,
                        "flow_direction": element.flow_direction,
                        "arrow_position": element.arrow_position,
                        "crossing_style": element.crossing_style,
                        "routing": element.routing,
                        "points": [point.model_dump() for point in element.points],
                        "source": element.source.model_dump() if element.source else None,
                        "target": element.target.model_dump() if element.target else None,
                        **base,
                    }
                )
        return {
            "document_id": document.id,
            "name": document.name,
            "revision": document.revision,
            "canvas": document.canvas.model_dump(),
            "layers": [layer.model_dump() for layer in document.layers],
            "systems": [system.model_dump() for system in document.systems],
            "element_count": len(document.elements),
            "elements_by_type": by_type,
            "symbols": symbols,
            "junctions": junctions,
            "connectors": connectors,
        }

    def _get_stored(self, document_id: str) -> StoredDocument:
        stored = self.store.get(document_id)
        if stored is None:
            raise DocumentNotFoundError(document_id)
        return stored

    def _apply_operation(self, document: Document, operation: Any) -> None:
        if isinstance(operation, AddElementOperation):
            if any(item.id == operation.element.id for item in document.elements):
                raise InvalidOperationError(f"element already exists: {operation.element.id}")
            document.elements.append(self._prepare_element(document, operation.element))
            return

        if isinstance(operation, UpdateElementOperation):
            index = self._element_index(document, operation.element_id)
            current = document.elements[index]
            current_layer = next(item for item in document.layers if item.id == current.layer_id)
            if current_layer.locked:
                raise InvalidOperationError(f"layer is locked: {current_layer.id}")
            if _element_edit_locked(current) and not _unlock_only_patch(current, operation.patch):
                raise InvalidOperationError(f"element is locked: {current.id}")
            forbidden = {"id", "type"} & operation.patch.keys()
            if forbidden:
                raise InvalidOperationError(f"cannot update immutable fields: {sorted(forbidden)}")
            try:
                updated = type(current).model_validate(
                    {**current.model_dump(mode="python"), **deepcopy(operation.patch)}
                )
            except ValidationError as exc:
                raise InvalidOperationError(str(exc)) from exc
            updated = self._prepare_element(document, updated)
            connectable_geometry_changed = self._connectable_geometry_changed(current, updated)
            if connectable_geometry_changed:
                self._assert_connected_connectors_editable(document, updated.id)
            document.elements[index] = updated
            if connectable_geometry_changed:
                self._refresh_connected_connectors(document, updated.id)
            return

        if isinstance(operation, DeleteElementOperation):
            index = self._element_index(document, operation.element_id)
            current = document.elements[index]
            current_layer = next(item for item in document.layers if item.id == current.layer_id)
            if current_layer.locked:
                raise InvalidOperationError(f"layer is locked: {current_layer.id}")
            if _element_edit_locked(current):
                raise InvalidOperationError(f"element is locked: {current.id}")
            if current.type in {"symbol", "junction"}:
                self._assert_connected_connectors_editable(document, current.id)
            removed = document.elements.pop(index)
            if removed.type in {"symbol", "junction"}:
                self._detach_connectable_connectors(document, removed.id)
            return

        if isinstance(operation, AddLayerOperation):
            if any(layer.id == operation.layer.id for layer in document.layers):
                raise InvalidOperationError(f"layer already exists: {operation.layer.id}")
            document.layers.append(operation.layer)
            return

        if isinstance(operation, UpdateLayerOperation):
            index = self._layer_index(document, operation.layer_id)
            current = document.layers[index]
            if "id" in operation.patch:
                raise InvalidOperationError("layer id is immutable")
            try:
                document.layers[index] = Layer.model_validate(
                    {**current.model_dump(mode="python"), **deepcopy(operation.patch)}
                )
            except ValidationError as exc:
                raise InvalidOperationError(str(exc)) from exc
            return

        if isinstance(operation, DeleteLayerOperation):
            if operation.layer_id == "layer_default":
                raise InvalidOperationError("default layer cannot be deleted")
            layer_index = self._layer_index(document, operation.layer_id)
            if document.layers[layer_index].locked:
                raise InvalidOperationError(f"layer is locked: {operation.layer_id}")
            locked_elements = [element.id for element in document.elements if element.layer_id == operation.layer_id and _element_edit_locked(element)]
            if locked_elements:
                raise InvalidOperationError(f"element is locked: {locked_elements[0]}")
            self._layer_index(document, operation.move_elements_to)
            for element in document.elements:
                if element.layer_id == operation.layer_id:
                    element.layer_id = operation.move_elements_to
            document.layers = [layer for layer in document.layers if layer.id != operation.layer_id]
            return

        if isinstance(operation, AddSystemOperation):
            if any(system.id == operation.system.id for system in document.systems):
                raise InvalidOperationError(f"system already exists: {operation.system.id}")
            document.systems.append(operation.system)
            return

        if isinstance(operation, UpdateSystemOperation):
            index = self._system_index(document, operation.system_id)
            current = document.systems[index]
            if "id" in operation.patch:
                raise InvalidOperationError("system id is immutable")
            try:
                document.systems[index] = SystemGroup.model_validate(
                    {**current.model_dump(mode="python"), **deepcopy(operation.patch)}
                )
            except ValidationError as exc:
                raise InvalidOperationError(str(exc)) from exc
            return

        if isinstance(operation, DeleteSystemOperation):
            if operation.system_id == "system_default":
                raise InvalidOperationError("default system cannot be deleted")
            self._system_index(document, operation.system_id)
            locked_elements = [element.id for element in document.elements if element.system_id == operation.system_id and _element_edit_locked(element)]
            if locked_elements:
                raise InvalidOperationError(f"element is locked: {locked_elements[0]}")
            self._system_index(document, operation.move_elements_to)
            for element in document.elements:
                if element.system_id == operation.system_id:
                    element.system_id = operation.move_elements_to
            document.systems = [system for system in document.systems if system.id != operation.system_id]
            return

        if isinstance(operation, ClearDocumentOperation):
            locked = {layer.id for layer in document.layers if layer.locked}
            if any(element.layer_id in locked for element in document.elements):
                raise InvalidOperationError("cannot clear document while a non-empty layer is locked")
            if any(_element_edit_locked(element) for element in document.elements):
                raise InvalidOperationError("cannot clear document while an element is locked")
            document.elements.clear()
            return

        raise InvalidOperationError(f"unsupported operation: {type(operation).__name__}")

    def _prepare_element(self, document: Document, element: Element) -> Element:
        layer = next((item for item in document.layers if item.id == element.layer_id), None)
        if layer is None:
            raise InvalidOperationError(f"unknown layer: {element.layer_id}")
        if layer.locked:
            raise InvalidOperationError(f"layer is locked: {layer.id}")
        if not any(item.id == element.system_id for item in document.systems):
            raise InvalidOperationError(f"unknown system: {element.system_id}")
        if element.type == "symbol":
            try:
                self.symbols.get(element.symbol_key)
            except KeyError as exc:
                raise InvalidOperationError(str(exc)) from exc
        if element.type == "connector":
            return self._normalize_connector(document, element)
        return element

    def _normalize_connector(self, document: Document, connector: ConnectorElement) -> ConnectorElement:
        normalized = ConnectorElement.model_validate(connector.model_dump(mode="python"))
        start = normalized.points[0]
        end = normalized.points[-1]
        if normalized.source is not None:
            normalized.source = self._normalize_endpoint(document, normalized.source, "source")
            start = normalized.source.point
        if normalized.target is not None:
            normalized.target = self._normalize_endpoint(document, normalized.target, "target")
            end = normalized.target.point
        if normalized.routing == "orthogonal":
            normalized.points = self._orthogonal_route(start, end)
        elif normalized.routing == "direct":
            normalized.points = self._dedupe_points([start, end])
        else:
            points = [Point.model_validate(point.model_dump()) for point in normalized.points]
            normalized.points = self._bind_manual_endpoints(points, start, end)
        return normalized

    def _normalize_endpoint(self, document: Document, endpoint: ConnectorEndpoint, endpoint_name: str) -> ConnectorEndpoint:
        if endpoint.element_id is None:
            if endpoint.port_id is not None:
                raise InvalidOperationError(f"{endpoint_name} port_id requires element_id")
            return endpoint
        if endpoint.port_id is None:
            raise InvalidOperationError(f"{endpoint_name} element_id requires port_id")
        connectable = self._connectable_by_id(document, endpoint.element_id)
        if connectable.type == "symbol":
            point = self._symbol_port_point(connectable, endpoint.port_id)
        else:
            if endpoint.port_id != "node":
                raise InvalidOperationError(
                    f"junction {connectable.id} only exposes port 'node', not '{endpoint.port_id}'"
                )
            point = Point.model_validate(connectable.position.model_dump())
        return ConnectorEndpoint(element_id=connectable.id, port_id=endpoint.port_id, point=point)

    def _symbol_port_point(self, symbol: SymbolElement, port_id: str) -> Point:
        """Absolute point of a symbol port: the one port mapping, not a second copy.

        The arithmetic lives in :mod:`agentcad.drafting_geometry` because the drafting
        router, the drafting report and this service must agree to the last bit — a
        symbol whose pipe ends one pixel off its declared port is a defect that would be
        invisible until export (Charter §6.4). Imported locally: this module is the
        bottom of the dependency stack and must not grow a cycle to keep a formula.
        """

        from .drafting_geometry import symbol_port_point

        try:
            definition = self.symbols.get(symbol.symbol_key)
        except KeyError as exc:
            # A drawing can reference a symbol the loaded catalog no longer defines
            # (legacy files, a project library that moved on). That is a data error the
            # caller has to see, not a KeyError escaping through a read-only route.
            raise InvalidOperationError(
                f"unknown symbol '{symbol.symbol_key}' for element {symbol.id}"
            ) from exc
        port = next((item for item in definition.ports if item.id == port_id), None)
        if port is None:
            raise InvalidOperationError(
                f"unknown port '{port_id}' for symbol {symbol.id} ({symbol.symbol_key})"
            )
        return symbol_port_point(symbol, port, definition)

    @staticmethod
    def _connectable_geometry_changed(current: Element, updated: Element) -> bool:
        if current.type == "symbol" and updated.type == "symbol":
            return any(
                getattr(current, field) != getattr(updated, field)
                for field in ("position", "width", "height", "rotation", "symbol_key")
            )
        if current.type == "junction" and updated.type == "junction":
            return current.position != updated.position
        return False

    def _assert_connected_connectors_editable(self, document: Document, element_id: str) -> None:
        layer_map = {layer.id: layer for layer in document.layers}
        for element in document.elements:
            if element.type != "connector":
                continue
            source_match = element.source is not None and element.source.element_id == element_id
            target_match = element.target is not None and element.target.element_id == element_id
            if not (source_match or target_match):
                continue
            if _element_edit_locked(element):
                raise InvalidOperationError(f"connected element is locked: {element.id}")
            layer = layer_map.get(element.layer_id)
            if layer is not None and layer.locked:
                raise InvalidOperationError(f"connected element layer is locked: {layer.id}")

    def _refresh_connected_connectors(self, document: Document, element_id: str) -> None:
        for index, element in enumerate(document.elements):
            if element.type != "connector":
                continue
            source_match = element.source is not None and element.source.element_id == element_id
            target_match = element.target is not None and element.target.element_id == element_id
            if source_match or target_match:
                document.elements[index] = self._normalize_connector(document, element)

    @staticmethod
    def _detach_connectable_connectors(document: Document, element_id: str) -> None:
        for element in document.elements:
            if element.type != "connector":
                continue
            if element.source is not None and element.source.element_id == element_id:
                element.source = ConnectorEndpoint(point=element.source.point)
            if element.target is not None and element.target.element_id == element_id:
                element.target = ConnectorEndpoint(point=element.target.point)

    @staticmethod
    def _orthogonal_route(start: Point, end: Point) -> list[Point]:
        if start.x == end.x or start.y == end.y:
            return DocumentService._dedupe_points([start, end])
        if abs(end.x - start.x) >= abs(end.y - start.y):
            # dominant horizontal: run along the source row, turn at the target column
            points = [start, Point(x=end.x, y=start.y), end]
        else:
            # dominant vertical: run along the source column, turn at the target row
            points = [start, Point(x=start.x, y=end.y), end]
        return DocumentService._dedupe_points(points)

    @staticmethod
    def _bind_manual_endpoints(points: list[Point], start: Point, end: Point) -> list[Point]:
        endpoints_changed = (
            points[0].x != start.x
            or points[0].y != start.y
            or points[-1].x != end.x
            or points[-1].y != end.y
        )
        if len(points) <= 2:
            if endpoints_changed:
                return DocumentService._orthogonal_route(start, end)
            # A 2-point manual route that is diagonal cannot be orthogonal; re-route
            # through a clean elbow instead of rejecting (forgives LLM/manual input).
            if points[0].x != points[1].x and points[0].y != points[1].y:
                return DocumentService._orthogonal_route(start, end)
            return DocumentService._validate_manual_route(points)
        original = [Point.model_validate(point.model_dump()) for point in points]
        result = [Point.model_validate(point.model_dump()) for point in points]
        first_vertical = original[0].x == original[1].x
        last_vertical = original[-2].x == original[-1].x
        result[0] = Point.model_validate(start.model_dump())
        result[-1] = Point.model_validate(end.model_dump())
        if first_vertical:
            result[1].x = start.x
        else:
            result[1].y = start.y
        if last_vertical:
            result[-2].x = end.x
        else:
            result[-2].y = end.y
        return DocumentService._validate_manual_route(result)

    @staticmethod
    def _validate_manual_route(points: list[Point]) -> list[Point]:
        result = DocumentService._dedupe_points(points)
        for first, second in zip(result, result[1:], strict=False):
            if first.x != second.x and first.y != second.y:
                raise InvalidOperationError("manual connector segments must remain orthogonal")
        return result

    @staticmethod
    def _dedupe_points(points: list[Point]) -> list[Point]:
        result: list[Point] = []
        for point in points:
            if result and result[-1].x == point.x and result[-1].y == point.y:
                continue
            result.append(Point.model_validate(point.model_dump()))
        if len(result) == 1:
            result.append(Point.model_validate(result[0].model_dump()))
        return result

    @staticmethod
    def _element_index(document: Document, element_id: str) -> int:
        for index, element in enumerate(document.elements):
            if element.id == element_id:
                return index
        raise InvalidOperationError(f"element not found: {element_id}")

    @staticmethod
    def _layer_index(document: Document, layer_id: str) -> int:
        for index, layer in enumerate(document.layers):
            if layer.id == layer_id:
                return index
        raise InvalidOperationError(f"layer not found: {layer_id}")

    @staticmethod
    def _system_index(document: Document, system_id: str) -> int:
        for index, system in enumerate(document.systems):
            if system.id == system_id:
                return index
        raise InvalidOperationError(f"system not found: {system_id}")

    @staticmethod
    def _connectable_by_id(document: Document, element_id: str) -> SymbolElement | JunctionElement:
        for element in document.elements:
            if element.id == element_id:
                if element.type not in {"symbol", "junction"}:
                    raise InvalidOperationError(
                        f"connector endpoint must reference a symbol or junction: {element_id}"
                    )
                return element
        raise InvalidOperationError(f"connector endpoint element not found: {element_id}")
