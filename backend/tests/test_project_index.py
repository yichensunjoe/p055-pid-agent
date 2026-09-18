"""Project engineering index tests (M2).

The index is a derived cache, so these tests exist to prove it never pretends to be
more than that: it is deterministic, it reports its own staleness instead of hiding
it, it never touches engineering content or the audit chain, and it resolves
cross-document off-page connectors while reporting the ones it cannot resolve.
"""

from __future__ import annotations

import pytest

from agentcad import project_index as project_index_module
from agentcad.engineering_ir import graph_fingerprint
from agentcad.models import (
    Document,
    Layer,
    Point,
    SymbolElement,
)
from agentcad.project_index import ProjectIndexService
from agentcad.store import SQLiteDocumentStore, StoredDocument
from agentcad.symbols import SymbolRegistry


@pytest.fixture()
def store(tmp_path) -> SQLiteDocumentStore:
    return SQLiteDocumentStore(tmp_path / "index.db")


@pytest.fixture()
def service(store: SQLiteDocumentStore) -> ProjectIndexService:
    return ProjectIndexService(store, SymbolRegistry())


def _save(store: SQLiteDocumentStore, document: Document) -> None:
    store.save(StoredDocument(document=document, undo_stack=[], redo_stack=[]))


def _pump(document_id: str, *, tag: str, element_id: str) -> SymbolElement:
    return SymbolElement(
        id=element_id,
        symbol_key="centrifugal_pump",
        position=Point(x=100, y=100),
        width=60,
        height=60,
        label=tag,
    )


def _opc(
    document_id: str,
    *,
    element_id: str,
    tag: str,
    direction: str,
    target: str,
) -> SymbolElement:
    return SymbolElement(
        id=element_id,
        symbol_key=f"off_page_connector_{direction}",
        position=Point(x=500, y=200),
        width=100,
        height=50,
        label=tag,
        properties={"target_document_id": target},
        metadata={},
    )


def _document(document_id: str, elements: list) -> Document:
    return Document(
        id=document_id,
        name=f"Drawing {document_id}",
        revision=1,
        layers=[Layer(id="layer_default", name="Process")],
        elements=elements,
    )


def test_rebuild_is_idempotent_and_deterministic(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    _save(store, _document("doc_a", [_pump("doc_a", tag="P-101", element_id="pump_a")]))

    first = service.rebuild_all()
    assert first.rebuilt == ["doc_a"]
    assert first.stale_after == []
    entry = service.get_entry("doc_a")
    assert entry is not None
    assert entry.staleness == "verified_fresh"
    assert entry.counts.equipment == 1
    assert entry.builder_version == project_index_module.IR_BUILDER_VERSION

    second = service.rebuild_all()
    assert second.rebuilt == []
    assert second.unchanged == ["doc_a"]

    forced = service.rebuild_all(force=True)
    assert forced.rebuilt == ["doc_a"]
    assert service.get_entry("doc_a").graph_hash == entry.graph_hash


def test_index_never_touches_engineering_content_or_audit_chain(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    _save(store, _document("doc_a", [_pump("doc_a", tag="P-101", element_id="pump_a")]))
    before = store.get("doc_a").document
    audit_before = len(store.all_audit_records())

    service.rebuild_all()
    service.project_graph()
    service.rebuild_all(force=True)

    after = store.get("doc_a").document
    assert after.model_dump(mode="json") == before.model_dump(mode="json")
    assert after.revision == before.revision
    assert len(store.all_audit_records()) == audit_before


def test_stale_row_is_reported_then_repaired(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    document = _document("doc_a", [_pump("doc_a", tag="P-101", element_id="pump_a")])
    _save(store, document)
    service.rebuild_all()

    edited = store.get("doc_a").document.model_copy(deep=True)
    edited.revision = 2
    edited.elements.append(_pump("doc_a", tag="P-102", element_id="pump_b"))
    _save(store, edited)

    entry = service.get_entry("doc_a")
    assert entry is not None
    assert entry.staleness == "stale"
    assert entry.stale_reasons == ["revision_changed", "content_hash_changed"]

    # The cheap project-wide listing can only prove the revision moved.
    listed = {item.document_id: item for item in service.list_entries()}
    assert listed["doc_a"].stale_reasons == ["revision_changed"]

    project = service.project_graph()
    assert project.stale_document_ids == ["doc_a"]
    assert [finding.code for finding in project.findings] == ["IR_INDEX_STALE"]
    # The stale row's counts are the *old* numbers, which is exactly why the graph
    # reports them as stale rather than silently serving them as current.
    assert project.totals.equipment == 1

    project = service.project_graph(refresh_stale=True)
    assert project.freshness == "verified"
    assert project.stale_document_ids == []
    assert project.totals.equipment == 2
    assert project.findings == []
    assert service.get_entry("doc_a").staleness == "verified_fresh"


def test_graph_returns_verified_content_for_the_live_revision(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    _save(store, _document("doc_a", [_pump("doc_a", tag="P-101", element_id="pump_a")]))
    service.rebuild_all()
    cached = service.graph("doc_a")
    assert cached.revision == 1

    edited = store.get("doc_a").document.model_copy(deep=True)
    edited.revision = 2
    edited.elements.append(_pump("doc_a", tag="P-102", element_id="pump_b"))
    _save(store, edited)

    refreshed = service.graph("doc_a")
    assert refreshed.revision == 2
    assert refreshed.counts.equipment == 2
    assert service.get_entry("doc_a").revision == 2
    assert service.get_entry("doc_a").staleness == "verified_fresh"

    with pytest.raises(KeyError):
        service.graph("doc_missing")


def _insert_orphan_index_row(store: SQLiteDocumentStore, document_id: str) -> None:
    """Write an index row for a document that does not exist.

    A cascade or a stale pre-v5 database can leave this residue. The raw SQLite
    connection (foreign keys default OFF) is used deliberately: the store itself must
    never be able to create such a row.
    """

    import sqlite3

    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute(
            """
            INSERT INTO project_index (
                document_id, document_name, revision, content_hash, graph_hash,
                builder_version, object_count, equipment_count, valve_count,
                instrument_count, line_count, off_page_count, error_count,
                warning_count, graph_json, built_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (document_id, "Orphan", 1, "h", "h", 1, 0, 0, 0, 0, 0, 0, 0, 0, "{}", "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()
    finally:
        connection.close()


def test_deleted_document_cascades_out_of_the_index(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    _save(store, _document("doc_a", [_pump("doc_a", tag="P-101", element_id="pump_a")]))
    service.rebuild_all()
    assert store.get_project_index("doc_a") is not None

    assert store.delete("doc_a", expected_revision=1) is True
    assert store.get_project_index("doc_a") is None
    assert service.get_entry("doc_a") is None
    assert store.prune_project_index() == []
    assert service.project_graph().document_count == 0


def test_orphan_index_rows_are_reported_and_pruned(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    _save(store, _document("doc_a", [_pump("doc_a", tag="P-101", element_id="pump_a")]))
    service.rebuild_all()
    _insert_orphan_index_row(store, "doc_orphan")

    entries = {entry.document_id: entry for entry in service.list_entries()}
    assert entries["doc_orphan"].staleness == "missing_document"
    assert entries["doc_orphan"].stale_reasons == ["document_deleted"]
    project = service.project_graph()
    assert "IR_INDEX_ORPHAN" in [finding.code for finding in project.findings]
    assert project.indexed_document_count == 1
    assert project.totals.equipment == 1  # the orphan contributes nothing

    removed = service.rebuild_all().removed
    assert removed == ["doc_orphan"]
    assert service.get_entry("doc_orphan") is None


def test_builder_version_change_marks_rows_stale(
    store: SQLiteDocumentStore,
    service: ProjectIndexService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save(store, _document("doc_a", [_pump("doc_a", tag="P-101", element_id="pump_a")]))
    service.rebuild_all()

    monkeypatch.setattr(project_index_module, "IR_BUILDER_VERSION", 999)
    entry = service.get_entry("doc_a")
    assert entry is not None
    assert entry.staleness == "stale"
    assert entry.stale_reasons == ["builder_version_changed"]

    report = service.rebuild_all()
    assert report.rebuilt == ["doc_a"]
    assert service.get_entry("doc_a").builder_version == 999


def test_cross_document_off_page_connectors_resolve_by_tag(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    _save(
        store,
        _document(
            "doc_a",
            [_opc("doc_a", element_id="opc_out", tag="PL-1001", direction="out", target="doc_b")],
        ),
    )
    _save(
        store,
        _document(
            "doc_b",
            [
                _opc("doc_b", element_id="opc_in", tag="PL-1001", direction="in", target="doc_a"),
            ],
        ),
    )
    service.rebuild_all()

    project = service.project_graph()
    assert project.findings == []
    assert len(project.cross_document_links) == 2
    assert {link.source_document_id for link in project.cross_document_links} == {"doc_a", "doc_b"}
    out_link = next(link for link in project.cross_document_links if link.direction == "out")
    in_link = next(link for link in project.cross_document_links if link.direction == "in")
    assert out_link.resolved is True
    assert out_link.matching_object_ids == ["opc:pl-1001"]
    assert in_link.resolved is True
    assert in_link.matching_object_ids == ["opc:pl-1001"]


def test_unresolved_cross_document_links_are_reported(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    _save(
        store,
        _document(
            "doc_a",
            [
                _opc("doc_a", element_id="opc_out", tag="PL-3003", direction="out", target="doc_b"),
                _opc(
                    "doc_a", element_id="opc_ghost", tag="PL-4004", direction="out", target="doc_ghost"
                ),
            ],
        ),
    )
    _save(
        store,
        _document(
            "doc_b",
            [
                _opc("doc_b", element_id="opc_in", tag="PL-9999", direction="in", target="doc_a"),
            ],
        ),
    )
    service.rebuild_all()

    project = service.project_graph()
    codes = [finding.code for finding in project.findings]
    # Both directions fail to match a tag, and one connector points at a document
    # that does not exist: all three are reported, none silently drops.
    assert codes == [
        "IR_CROSS_DOC_TAG_UNMATCHED",
        "IR_CROSS_DOC_TAG_UNMATCHED",
        "IR_CROSS_DOC_TARGET_MISSING",
    ]
    links = {link.source_object_id: link for link in project.cross_document_links}
    assert len(links) == 3
    assert links["opc:pl-3003"].target_document_found is True
    assert links["opc:pl-3003"].resolved is False
    assert links["opc:pl-4004"].target_document_found is False
    assert links["opc:pl-4004"].resolved is False


def test_empty_project_graph_is_valid(service: ProjectIndexService) -> None:
    project = service.project_graph()
    assert project.document_count == 0
    assert project.indexed_document_count == 0
    assert project.documents == []
    assert project.cross_document_links == []
    assert project.findings == []
    assert project.totals.objects == 0
    assert project.freshness == "cheap"


def test_project_graph_counts_come_from_stored_graphs(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    _save(store, _document("doc_a", [_pump("doc_a", tag="P-101", element_id="pump_a")]))
    _save(store, _document("doc_b", [_pump("doc_b", tag="P-201", element_id="pump_b")]))
    service.rebuild_all()

    project = service.project_graph()
    assert project.document_count == 2
    assert project.indexed_document_count == 2
    assert project.totals.equipment == 2
    for entry in project.documents:
        graph = service.graph(entry.document_id)
        assert entry.graph_hash == graph_fingerprint(graph)
