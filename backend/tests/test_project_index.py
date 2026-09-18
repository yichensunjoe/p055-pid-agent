"""Project engineering index tests (M2).

The index is a derived cache, so these tests exist to prove it never pretends to be
more than that: it is deterministic, it reports its own staleness instead of hiding
it, it never touches engineering content or the audit chain, and it resolves
cross-document off-page connectors into *tag-free* connection identities while
reporting the ones it cannot resolve.
"""

from __future__ import annotations

import pytest

from agentcad import project_index as project_index_module
from agentcad.engineering_ir import graph_fingerprint
from agentcad.models import (
    ConnectorElement,
    ConnectorEndpoint,
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


def _instrument(element_id: str, *, tag: str, x: float = 300.0) -> SymbolElement:
    return SymbolElement(
        id=element_id,
        symbol_key="pressure_indicator",
        position=Point(x=x, y=300),
        width=60,
        height=60,
        label=tag,
    )


def _signal_connector(
    element_id: str,
    source: tuple[str, str],
    target: tuple[str, str],
    *,
    medium: str = "electric signal",
) -> ConnectorElement:
    return ConnectorElement(
        id=element_id,
        points=[Point(x=300, y=300), Point(x=100, y=100)],
        source=ConnectorEndpoint(element_id=source[0], port_id=source[1], point=Point(x=300, y=300)),
        target=ConnectorEndpoint(element_id=target[0], port_id=target[1], point=Point(x=100, y=100)),
        routing="manual",
        medium=medium,
    )


def _opc(
    document_id: str,
    *,
    element_id: str,
    tag: str,
    direction: str,
    target: str,
    connection_id: str = "",
) -> SymbolElement:
    properties = {"target_document_id": target}
    if connection_id:
        properties["connection_id"] = connection_id
    return SymbolElement(
        id=element_id,
        symbol_key=f"off_page_connector_{direction}",
        position=Point(x=500, y=200),
        width=100,
        height=50,
        label=tag,
        properties=properties,
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


def test_index_reports_signal_counts(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    _save(
        store,
        _document(
            "doc_a",
            [
                _pump("doc_a", tag="P-101", element_id="pump_a"),
                _instrument("pi_a", tag="PI-101"),
                _signal_connector("sig_a", ("pi_a", "process"), ("pump_a", "discharge")),
            ],
        ),
    )
    service.rebuild_all()

    entry = service.get_entry("doc_a")
    assert entry is not None
    assert entry.counts.signals == 1
    assert store.get_project_index("doc_a")["signal_count"] == 1
    assert service.project_graph().totals.signals == 1


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
                instrument_count, signal_count, line_count, off_page_count, error_count,
                warning_count, graph_json, built_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                "Orphan",
                1,
                "h",
                "h",
                1,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                "{}",
                "2026-01-01T00:00:00+00:00",
            ),
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
    assert entry.staleness == "builder_outdated"
    assert entry.stale_reasons == ["builder_version_changed"]

    report = service.rebuild_all()
    assert report.rebuilt == ["doc_a"]
    assert service.get_entry("doc_a").builder_version == 999


def test_rows_written_by_an_older_builder_are_rebuilt_not_served(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    """The identity rework bumped the builder version; old rows must not leak through.

    A cached graph from the previous schema is *not* evidence about the current
    document, so it is reported as outdated and rebuilt instead of returned (an old
    graph has no ``engineering_id``, no signals and tag-derived object ids).
    """

    _save(store, _document("doc_a", [_pump("doc_a", tag="P-101", element_id="pump_a")]))
    service.rebuild_all()

    # Simulate a row written before the rework: same content hash, older builder.
    legacy_graph_json = store.get_project_index("doc_a")["graph_json"]
    _insert_legacy_row(store, "doc_a", builder_version=1, graph_json=legacy_graph_json)

    entry = service.get_entry("doc_a")
    assert entry is not None
    assert entry.staleness == "builder_outdated"
    assert entry.stale_reasons == ["builder_version_changed"]
    listed = {item.document_id: item for item in service.list_entries()}
    assert listed["doc_a"].staleness == "builder_outdated"

    # Reading the graph rebuilds rather than handing back the outdated cache.
    graph = service.graph("doc_a", require_fresh=False)
    assert graph.builder_version == project_index_module.IR_BUILDER_VERSION
    assert service.get_entry("doc_a").staleness == "verified_fresh"

    project = service.project_graph()
    assert project.stale_document_ids == []
    assert project.totals.equipment == 1


def _insert_legacy_row(
    store: SQLiteDocumentStore, document_id: str, *, builder_version: int, graph_json: str
) -> None:
    import sqlite3

    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute(
            "UPDATE project_index SET builder_version = ?, graph_json = ? WHERE document_id = ?",
            (builder_version, graph_json, document_id),
        )
        connection.commit()
    finally:
        connection.close()


def test_connection_identity_is_tag_free_and_symmetric(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    """Renaming the line number on both sides must not change the connection."""

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
            [_opc("doc_b", element_id="opc_in", tag="PL-1001", direction="in", target="doc_a")],
        ),
    )
    service.rebuild_all()

    project = service.project_graph()
    assert project.findings == []
    # One mutually declared connection, reported once with both ends filled in.
    assert len(project.off_page_connections) == 1
    connection = project.off_page_connections[0]
    assert connection.connection_id.startswith("opc_conn_")
    assert connection.resolved is True
    assert connection.matched_by == "reciprocal_declaration"
    assert connection.tag_agrees is True
    assert connection.source_document_id == "doc_a"
    assert connection.target_document_id == "doc_b"
    assert connection.source_tag == "PL-1001"
    assert connection.declared_by_document_ids == ["doc_a", "doc_b"]
    assert "pl-1001" not in connection.connection_id.casefold()

    first_id = connection.connection_id

    # Rename the service on both sides: same connection, new tag.
    for document_id, element_id in (("doc_a", "opc_out"), ("doc_b", "opc_in")):
        stored = store.get(document_id)
        edited = stored.document.model_copy(deep=True)
        edited.revision = 2
        for element in edited.elements:
            if element.id == element_id:
                element.label = "PL-7777"
        _save(store, edited)

    refreshed = service.project_graph(refresh_stale=True)
    assert [item.connection_id for item in refreshed.off_page_connections] == [first_id]
    renamed = refreshed.off_page_connections[0]
    assert renamed.source_tag == "PL-7777"
    assert renamed.tag_agrees is True


def test_connection_identity_uses_declared_endpoint_ids(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    _save(
        store,
        _document(
            "doc_a",
            [
                _opc(
                    "doc_a",
                    element_id="opc_out",
                    tag="PL-1001",
                    direction="out",
                    target="doc_b",
                    connection_id="OPC-CONN-0001",
                )
            ],
        ),
    )
    _save(
        store,
        _document(
            "doc_b",
            [
                _opc(
                    "doc_b",
                    element_id="opc_in",
                    tag="PL-1001",
                    direction="in",
                    target="doc_a",
                    connection_id="OPC-CONN-0001",
                )
            ],
        ),
    )
    service.rebuild_all()

    connection = service.project_graph().off_page_connections[0]
    assert connection.source_connection_endpoint_id == "OPC-CONN-0001"
    assert connection.target_connection_endpoint_id == "OPC-CONN-0001"


def test_tag_only_convention_match_is_labelled_as_such(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    """One-sided declarations still resolve, but the reason is recorded honestly."""

    _save(
        store,
        _document(
            "doc_a",
            [_opc("doc_a", element_id="opc_out", tag="PL-1001", direction="out", target="doc_b")],
        ),
    )
    _save(
        store,
        _document("doc_b", [_opc("doc_b", element_id="opc_in", tag="PL-1001", direction="in", target="")]),
    )
    service.rebuild_all()

    project = service.project_graph()
    connection = next(
        item for item in project.off_page_connections if item.source_document_id == "doc_a"
    )
    assert connection.resolved is True
    assert connection.matched_by == "service_convention"
    assert "IR_CROSS_DOC_CONVENTION_MATCH" in [finding.code for finding in project.findings]


def test_unresolved_and_ambiguous_connections_are_reported(
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
                _opc("doc_a", element_id="opc_dup", tag="PL-5005", direction="out", target="doc_b"),
            ],
        ),
    )
    _save(
        store,
        _document(
            "doc_b",
            [
                # Two reverse candidates with different tags: nothing uniquely matches.
                _opc("doc_b", element_id="opc_in_a", tag="PL-9999", direction="in", target="doc_a"),
                _opc("doc_b", element_id="opc_in_b", tag="PL-8888", direction="in", target="doc_a"),
            ],
        ),
    )
    service.rebuild_all()

    project = service.project_graph()
    codes = [finding.code for finding in project.findings]
    ambiguous = [
        finding for finding in project.findings if finding.code == "IR_CROSS_DOC_AMBIGUOUS"
    ]
    # Every declared end is reported separately and by object id: all four connectors
    # here point at a drawing whose reverse candidates cannot uniquely match them.
    assert len(ambiguous) == 4
    assert {object_id for finding in ambiguous for object_id in finding.object_ids} == {
        item.source_engineering_id
        for item in project.off_page_connections
        if item.source_tag in {"PL-3003", "PL-5005", "PL-9999", "PL-8888"}
    }
    assert codes.count("IR_CROSS_DOC_TARGET_MISSING") == 1

    by_source = {item.source_tag: item for item in project.off_page_connections}
    assert by_source["PL-4004"].target_document_found is False
    assert by_source["PL-4004"].resolved is False
    assert by_source["PL-3003"].matched_by == "ambiguous"
    assert by_source["PL-3003"].target_document_found is True
    # An unresolved connection still gets a stable, tag-free identity.
    assert by_source["PL-3003"].connection_id.startswith("opc_conn_")
    assert "pl-3003" not in by_source["PL-3003"].connection_id.casefold()


def test_cross_document_tag_mismatch_is_reported(
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
            [_opc("doc_b", element_id="opc_in", tag="PL-2002", direction="in", target="doc_a")],
        ),
    )
    service.rebuild_all()

    project = service.project_graph()
    connection = project.off_page_connections[0]
    # Reciprocal declarations are the identity; a differing service is a warning.
    assert connection.resolved is True
    assert connection.matched_by == "reciprocal_declaration"
    assert connection.tag_agrees is False
    mismatch = next(
        finding for finding in project.findings if finding.code == "IR_CROSS_DOC_TAG_MISMATCH"
    )
    assert mismatch.details["connection_id"] == connection.connection_id


def test_find_objects_locates_identity_tag_or_element_across_documents(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    _save(store, _document("doc_a", [_pump("doc_a", tag="P-101", element_id="pump_a")]))
    _save(store, _document("doc_b", [_pump("doc_b", tag="P-201", element_id="pump_b")]))
    service.rebuild_all()

    graph_a = service.graph("doc_a")
    engineering_id = graph_a.object("P-101").engineering_id
    by_identity = service.find_objects(engineering_id)
    assert [(item.document_id, item.engineering_id) for item in by_identity] == [
        ("doc_a", engineering_id)
    ]
    assert by_identity[0].resolved_from == ""
    assert by_identity[0].identity_basis == "element"

    by_tag = service.find_objects("P-201")
    assert [item.document_id for item in by_tag] == ["doc_b"]
    assert by_tag[0].tag_key == "equipment:p-201"
    assert by_tag[0].resolved_from == "P-201"

    by_element = service.find_objects("pump_a")
    assert [item.document_id for item in by_element] == ["doc_a"]

    assert service.find_objects("P-999") == []
    assert service.find_objects("   ") == []

    # The same tag reused in another drawing is a real project situation: both are
    # reported, and a caller can cap how many answers it wants.
    _save(
        store,
        _document("doc_c", [_pump("doc_c", tag="P-201", element_id="pump_c")]),
    )
    service.rebuild_all()
    both = service.find_objects("P-201")
    assert [item.document_id for item in both] == ["doc_b", "doc_c"]
    assert [item.document_id for item in service.find_objects("P-201", limit=1)] == ["doc_b"]


def test_find_objects_skips_rows_from_an_older_builder(
    store: SQLiteDocumentStore, service: ProjectIndexService
) -> None:
    _save(store, _document("doc_a", [_pump("doc_a", tag="P-101", element_id="pump_a")]))
    service.rebuild_all()
    assert service.find_objects("P-101") != []

    _insert_legacy_row(
        store,
        "doc_a",
        builder_version=1,
        graph_json=store.get_project_index("doc_a")["graph_json"],
    )
    # Outdated rows are not guessed at; a rebuild brings them back.
    assert service.find_objects("P-101") == []
    service.rebuild_all()
    assert service.find_objects("P-101") != []


def test_empty_project_graph_is_valid(service: ProjectIndexService) -> None:
    project = service.project_graph()
    assert project.document_count == 0
    assert project.indexed_document_count == 0
    assert project.documents == []
    assert project.off_page_connections == []
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
