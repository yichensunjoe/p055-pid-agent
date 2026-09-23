"""M7-2 phase 3: materializing a finalized canonical layout through the one production writer.

Grouped by the promise each part makes to the engineer who asked for the drawing: the drawing
exists as a committed revision, it is the drawing the layout decided (not a re-routed or
re-centred approximation of it), the same layout always writes the same drawing, and the revision
can be traced back to the specification it came from.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_m7_diagram_adapter import fixture_a_payload
from test_m7_step3_geometry import annotated_fixture_a

from agentcad import m7_layout_contract as contract
from agentcad.audit_models import AuditContext
from agentcad.auto_layout_canvas import derive_semantic_canvas
from agentcad.auto_layout_identity import (
    EngineeringSemanticPreservationError,
    finalize_semantic_layout,
)
from agentcad.auto_layout_semantic import STEP_4, STEP_5
from agentcad.diagram_quality import analyze_diagram_quality
from agentcad.m7_diagram_adapter import adapt
from agentcad.m7_layout_materialization import (
    MATERIALIZATION_LAYER_ID,
    SYMBOL_LABEL_ANNOTATION_ROLE,
    LayoutIsNotFinalizedError,
    MaterializationCanvasError,
    MaterializationError,
    MaterializationLabelError,
    apply_materialized_layout,
    document_rows,
    materialization_digest,
    materialization_matches_document,
    materialization_payload,
    materialization_provenance,
    materialize_canonical_layout,
    materialized_element_id,
    materialized_transaction,
)
from agentcad.models import CreateDocumentRequest, Point, TransactionRequest
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry
from agentcad.tag_resolver import resolve_symbol_tag

LABELS = {
    "el_ar_tank": "V-101",
    "el_purifier": "X-201",
    "el_pt_101": "PT-101",
    "el_recycle": "X-301",
}


def make_service(tmp_path: Path) -> DocumentService:
    return DocumentService(SQLiteDocumentStore(tmp_path / "m7-materialization.db"), SymbolRegistry())


def finalized_layout():
    topology = adapt(fixture_a_payload())
    plan = finalize_semantic_layout(derive_semantic_canvas(annotated_fixture_a()), topology)
    assert plan.produced_at_step == STEP_5
    return plan, topology


def materialized(labels: dict | None = None):
    plan, topology = finalized_layout()
    return materialize_canonical_layout(plan, document_id="doc_m7", labels=labels or LABELS), plan, topology


def seed_document(service: DocumentService, layout) -> str:
    """A document whose canvas is the layout's, created through the existing creation path."""

    document = service.create_document(
        CreateDocumentRequest(
            name="Materialized A",
            width=layout.canvas_width,
            height=layout.canvas_height,
        )
    )
    return document.id


# ---------------------------------------------------------------------------------------
# The acceptance: a no-coordinate specification becomes a real document revision
# ---------------------------------------------------------------------------------------


def test_a_finalized_layout_becomes_a_committed_revision(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    layout, plan, _topology = materialized()
    document_id = seed_document(service, layout)
    assert document_id == layout.document_id or document_id != ""  # the id comes from creation

    chain = materialization_provenance(plan, layout, result_revision=1)
    audit = AuditContext(
        actor="m7-materializer",
        surface="internal",
        tool_name="m7_materialize_layout",
        validation_status="valid",
        metadata=dict(chain),
    )
    result = apply_materialized_layout(
        service, replace(layout, document_id=document_id), expected_revision=0, audit=audit
    )
    assert result.applied_operations == len(layout.operations)
    document = service.get_document(document_id)
    assert document.revision == 1
    assert materialization_matches_document(layout, document) == []
    assert len(document.elements) == len(layout.rows)
    assert {element.system_id for element in document.elements} == {"S_supply", "S_cover"}
    assert {system.id for system in document.systems} >= {"S_supply", "S_cover"}


def test_the_committed_drawing_holds_every_presentation_row_exactly_once(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    document_id = seed_document(service, layout)
    apply_materialized_layout(
        service, replace(layout, document_id=document_id), expected_revision=0
    )
    document = service.get_document(document_id)
    symbols = [element for element in document.elements if element.type == "symbol"]
    connectors = [element for element in document.elements if element.type == "connector"]
    texts = [element for element in document.elements if element.type == "text"]
    assert len(symbols) == 4  # noqa: E501 - the counts are the claim
    assert len(connectors) == 3
    assert len(texts) == 4
    assert len({element.id for element in document.elements}) == 11


def test_the_materialized_drawing_is_a_clean_drawing(tmp_path: Path) -> None:
    """The quality analyzer is the existing consumer: the materialized drawing must satisfy it."""

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    document_id = seed_document(service, layout)
    apply_materialized_layout(
        service, replace(layout, document_id=document_id), expected_revision=0
    )
    document = service.get_document(document_id)
    report = analyze_diagram_quality(document, service.symbols)
    codes = {issue.code for issue in report.issues}
    assert "SYMBOL_OUT_OF_BOUNDS" not in codes
    assert "CONNECTOR_OUT_OF_BOUNDS" not in codes
    for element in document.elements:
        if element.type == "symbol":
            assert element.position.x >= -1e-9 and element.position.y >= -1e-9
            assert element.position.x + element.width <= document.canvas.width + 1e-9
            assert element.position.y + element.height <= document.canvas.height + 1e-9


def test_the_engine_route_survives_the_writer(tmp_path: Path) -> None:
    """``manual`` is the routing mode that keeps the engine's obstacle-aware waypoints."""

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    document_id = seed_document(service, layout)
    apply_materialized_layout(
        service, replace(layout, document_id=document_id), expected_revision=0
    )
    document = service.get_document(document_id)
    written = {
        str((element.metadata or {}).get("engineering_id", element.id)): element
        for element in document.elements
        if element.type == "connector"
    }
    for row in layout.rows:
        if row["kind"] != "connector":
            continue
        element = written[str(row["engineering_id"])]
        assert element.routing == "manual"
        assert [[round(p.x, 6), round(p.y, 6)] for p in element.points][1:] == [
            [round(p[0], 6), round(p[1], 6)] for p in row["waypoints"]
        ]
        assert element.points[0].x == row["x"] and element.points[0].y == row["y"]


def test_labels_resolve_back_to_their_symbols(tmp_path: Path) -> None:
    """The editable label is a ``symbol_label`` annotation, and the tag is the semantic field."""

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    document_id = seed_document(service, layout)
    apply_materialized_layout(
        service, replace(layout, document_id=document_id), expected_revision=0
    )
    document = service.get_document(document_id)
    symbols = {element.id: element for element in document.elements if element.type == "symbol"}
    assert resolve_symbol_tag(document, symbols[layout.element_id_for("el_ar_tank", "symbol")]) == (
        "V-101"
    )
    tags = {symbol.properties.get("tag") for symbol in symbols.values()}
    assert tags == {"V-101", "X-201", "PT-101", "X-301"}
    annotations = [element for element in document.elements if element.type == "text"]
    for annotation in annotations:
        assert annotation.metadata["annotation_role"] == SYMBOL_LABEL_ANNOTATION_ROLE
        assert annotation.metadata["parent_element_id"] in symbols
        assert annotation.system_id in {"S_supply", "S_cover"}
        assert annotation.layer_id == MATERIALIZATION_LAYER_ID


# ---------------------------------------------------------------------------------------
# Determinism: the same layout writes the same drawing
# ---------------------------------------------------------------------------------------


def test_the_same_layout_materializes_to_the_same_drawing(tmp_path: Path) -> None:
    first, _plan, _topology = materialized()
    second, _plan, _topology = materialized()
    assert first.materialization_digest == second.materialization_digest
    assert [row for row in first.rows] == [row for row in second.rows]
    assert [op.model_dump(mode="json") for op in first.operations] == [
        op.model_dump(mode="json") for op in second.operations
    ]


def test_two_documents_hold_the_same_element_ids(tmp_path: Path) -> None:
    """Ids are derived from engineering identities, so the drawing is comparable across writes."""

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    first_id = seed_document(service, layout)
    apply_materialized_layout(
        service, replace(layout, document_id=first_id), expected_revision=0
    )
    second = service.create_document(
        CreateDocumentRequest(name="B", width=layout.canvas_width, height=layout.canvas_height)
    )
    apply_materialized_layout(service, replace(layout, document_id=second.id), expected_revision=0)
    first = service.get_document(first_id)
    other = service.get_document(second.id)
    assert [element.id for element in first.elements] == [element.id for element in other.elements]
    assert document_rows(first) == document_rows(other)
    assert first.id != other.id


def test_the_document_id_is_not_part_of_the_drawing_identity(tmp_path: Path) -> None:
    layout, _plan, _topology = materialized()
    same_layout_other_target = replace(layout, document_id="doc_somewhere_else")
    assert same_layout_other_target.materialization_digest == layout.materialization_digest
    assert "document_id" in contract.MATERIALIZATION_EXCLUDES_VOLATILE_BOOKKEEPING


def test_the_materialization_payload_is_the_declared_envelope() -> None:
    layout, _plan, _topology = materialized()
    payload = materialization_payload(
        document_canvas=(layout.canvas_width, layout.canvas_height),
        origin=(layout.origin_x, layout.origin_y),
        rows=layout.rows,
    )
    assert tuple(sorted(payload)) == tuple(sorted(contract.MATERIALIZATION_DIGEST_INPUTS))
    for volatile in ("created_at", "updated_at", "document_id", "session_id", "run_id"):
        assert volatile not in payload


def test_the_origin_is_digested_and_not_only_recorded() -> None:
    """Two drawings that differ only by the origin are not the same drawing.

    The origin is digested twice over: as envelope state, and inside every translated coordinate.
    Both halves are asserted, because a payload that records an origin it does not apply -- or
    applies one it does not record -- would still be "deterministic" while naming the wrong
    drawing, and each half alone would leave the other half's bug invisible.
    """

    layout, _plan, _topology = materialized()
    base = materialization_payload(
        document_canvas=(layout.canvas_width, layout.canvas_height),
        origin=(layout.origin_x, layout.origin_y),
        rows=layout.rows,
    )
    shifted_origin = materialization_payload(
        document_canvas=(layout.canvas_width, layout.canvas_height),
        origin=(layout.origin_x + 10.0, layout.origin_y),
        rows=layout.rows,
    )
    assert base["origin"] != shifted_origin["origin"]
    assert materialization_digest(base) != materialization_digest(shifted_origin)
    moved_rows = [{**row, "x": float(row["x"]) + 10.0} for row in layout.rows]
    assert materialization_digest(base) != materialization_digest({**base, "rows": moved_rows})


def test_a_re_origined_canvas_is_a_different_drawing() -> None:
    """The same layout written against a translated canvas digests differently.

    The engine derives the canvas; a caller that re-origins it is drawing the same plant with its
    margins moved, which is a second drawing and must not answer to the first one's identity.
    """

    plan, _topology = finalized_layout()
    envelope = plan.canonical_projection_envelope
    assert envelope is not None
    bounds = envelope["canvas_bounds"]
    shifted = replace(
        plan,
        canonical_projection_envelope={
            **envelope,
            "canvas_bounds": {**bounds, "x": float(bounds["x"]) + 10.0},
        },
    )
    first = materialize_canonical_layout(plan, document_id="doc_m7", labels=LABELS)
    second = materialize_canonical_layout(shifted, document_id="doc_m7", labels=LABELS)
    assert second.origin_x == first.origin_x + 10.0
    assert second.materialization_digest != first.materialization_digest
    assert [row["x"] for row in second.rows] != [row["x"] for row in first.rows]


def test_a_changed_label_changes_the_drawing_identity() -> None:
    """Label text is content: it is in the materialization digest even though it is not geometry."""

    first, _plan, _topology = materialized()
    second, _plan, _topology = materialized({**LABELS, "el_ar_tank": "V-102"})
    assert first.materialization_digest != second.materialization_digest
    assert [row for row in first.rows] != [row for row in second.rows]


def test_the_materialization_digest_is_not_the_layout_digest() -> None:
    layout, plan, _topology = materialized()
    assert layout.materialization_digest
    assert layout.materialization_digest != plan.canonical_layout_digest
    assert layout.digest_version != contract.LAYOUT_DIGEST_VERSION
    assert contract.MATERIALIZATION_DIGEST_IS_NOT_THE_LAYOUT_DIGEST


# ---------------------------------------------------------------------------------------
# The gates: what the materializer refuses to do
# ---------------------------------------------------------------------------------------


def test_an_unfinalized_layout_has_no_drawing_to_materialize() -> None:
    """Both halves of the gate, named: the step, and the identity the step exists to produce."""

    unfinished = derive_semantic_canvas(annotated_fixture_a())
    assert unfinished.produced_at_step == STEP_4
    with pytest.raises(LayoutIsNotFinalizedError) as raised:
        materialize_canonical_layout(unfinished, document_id="doc_m7", labels=LABELS)
    assert STEP_4 in str(raised.value) and "finalized canonical layout" in str(raised.value)
    named = replace(unfinished, produced_at_step=STEP_5)
    assert named.canonical_layout_digest == ""
    with pytest.raises(LayoutIsNotFinalizedError) as named_raised:
        materialize_canonical_layout(named, document_id="doc_m7", labels=LABELS)
    assert "finalized canonical layout" in str(named_raised.value)


def test_a_missing_label_is_refused() -> None:
    with pytest.raises(MaterializationLabelError):
        materialized({key: value for key, value in LABELS.items() if key != "el_ar_tank"})


def test_an_extra_label_is_refused() -> None:
    """Text for a node the layout never placed means geometry and text came from two runs."""

    with pytest.raises(MaterializationLabelError):
        materialized({**LABELS, "el_ghost": "G-1"})


def test_the_operations_are_adds_only() -> None:
    layout, _plan, _topology = materialized()
    kinds = {operation.op for operation in layout.operations}
    assert kinds <= set(contract.MATERIALIZATION_OPERATION_KINDS)
    assert "delete_element" not in kinds
    assert "update_element" not in kinds
    request = materialized_transaction(layout, expected_revision=0)
    assert isinstance(request, TransactionRequest)
    assert request.expected_revision == 0
    assert request.label.startswith("M7 layout materialization")


def test_a_smaller_document_canvas_is_refused(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    document = service.create_document(CreateDocumentRequest(name="Tiny", width=320, height=240))
    with pytest.raises(MaterializationCanvasError):
        apply_materialized_layout(service, replace(layout, document_id=document.id), expected_revision=0)
    assert service.get_document(document.id).revision == 0
    assert service.get_document(document.id).elements == []


def test_a_wrong_expected_revision_is_refused(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    document_id = seed_document(service, layout)
    with pytest.raises(MaterializationError):
        apply_materialized_layout(
            service, replace(layout, document_id=document_id), expected_revision=7
        )


def test_a_document_that_lost_a_row_is_reported(tmp_path: Path) -> None:
    """The verification reads the committed document, so a write that dropped a row is visible."""

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    document_id = seed_document(service, layout)
    apply_materialized_layout(
        service, replace(layout, document_id=document_id), expected_revision=0
    )
    document = service.get_document(document_id)
    trimmed = document.model_copy(update={"elements": document.elements[1:]})
    problems = materialization_matches_document(layout, trimmed)
    assert problems
    assert any("is missing" in problem for problem in problems)


def test_a_document_with_an_extra_element_is_reported(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    document_id = seed_document(service, layout)
    apply_materialized_layout(
        service, replace(layout, document_id=document_id), expected_revision=0
    )
    document = service.get_document(document_id)
    from agentcad.models import SymbolElement

    ghost = SymbolElement(
        id="el_symbol_ghost",
        symbol_key="gas_tank",
        position={"x": 10, "y": 10},
        width=120,
        height=90,
        system_id="S_supply",
        metadata={"engineering_id": "el_ghost"},
    )
    problems = materialization_matches_document(
        layout, document.model_copy(update={"elements": [*document.elements, ghost]})
    )
    assert any("never decided" in problem for problem in problems)


def _committed(service: DocumentService, layout) -> tuple[str, object]:
    document_id = seed_document(service, layout)
    apply_materialized_layout(
        service, replace(layout, document_id=document_id), expected_revision=0
    )
    return document_id, service.get_document(document_id)


def test_a_row_whose_content_drifted_is_reported(tmp_path: Path) -> None:
    """Not only a missing or an extra row: a row that moved, lost its tag, or was re-routed.

    The id sets still match in all three cases, so these are what keep the check from degrading
    into a comparison of element names.
    """

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    _document_id, document = _committed(service, layout)

    moved = []
    retagged = []
    for element in document.elements:
        if element.type == "symbol":
            position = Point(x=element.position.x + 5.0, y=element.position.y)
            moved.append(element.model_copy(update={"position": position}))
            properties = dict(element.properties)
            properties["tag"] = "Z-999"
            retagged.append(element.model_copy(update={"properties": properties}))
        else:
            moved.append(element)
            retagged.append(element)

    drift = materialization_matches_document(
        layout, document.model_copy(update={"elements": moved})
    )
    assert any("has x" in problem for problem in drift)
    tag_drift = materialization_matches_document(
        layout, document.model_copy(update={"elements": retagged})
    )
    assert any("has tag" in problem for problem in tag_drift)

    rerouted = []
    for element in document.elements:
        if element.type == "connector":
            points = [*element.points]
            middle = points[len(points) // 2]
            points[len(points) // 2] = middle.model_copy(
                update={"x": middle.x + 3.0, "y": middle.y}
            )
            rerouted.append(element.model_copy(update={"points": points}))
        else:
            rerouted.append(element)
    route_drift = materialization_matches_document(
        layout, document.model_copy(update={"elements": rerouted})
    )
    assert any("routed differently" in problem for problem in route_drift)


def test_element_ids_are_distinct_per_role_and_survive_normalization_clashes() -> None:
    """A node and its label are two elements, and two ids that normalize alike still differ."""

    assert materialized_element_id("el_ar_tank", "symbol") != materialized_element_id(
        "el_ar_tank", "label"
    )
    assert materialized_element_id("el_ar_tank", "symbol") != materialized_element_id(
        "el_ar_tank", "symbol", disambiguator=1
    )
    layout, _plan, _topology = materialized()
    ids = [element_id for _row_id, _role, element_id in layout.element_ids]
    assert len(ids) == len(set(ids))
    assert all(element_id.startswith("el_") for element_id in ids)


# ---------------------------------------------------------------------------------------
# Provenance: the revision is traceable to the specification
# ---------------------------------------------------------------------------------------


def test_the_provenance_chain_reaches_the_committed_revision() -> None:
    layout, plan, topology = materialized()
    chain = materialization_provenance(plan, layout, result_revision=3)
    for link in contract.MATERIALIZATION_PROVENANCE_CHAIN:
        assert link in chain
    assert chain["resulting_revision"] == "3"
    assert chain["adapter_topology_digest"] == topology.digest
    assert chain["canonical_layout_digest"] == plan.canonical_layout_digest
    assert chain["symbol_geometry_catalog_digest"] == plan.symbol_geometry_catalog_digest
    assert chain["materialization_digest"] == layout.materialization_digest
    assert chain["diagram_spec_semantic_digest"]


def test_the_board_records_the_chain_as_an_audit_metadata_dict() -> None:
    """A dict of strings is what the existing audit path persists, so that is the shape checked."""

    layout, plan, _topology = materialized()
    chain = materialization_provenance(plan, layout, result_revision=1)
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in chain.items())
    context = AuditContext(
        actor="m7-materializer",
        surface="internal",
        tool_name="m7_materialize_layout",
        validation_status="valid",
        metadata=dict(chain),
    )
    assert context.metadata["canonical_layout_digest"] == plan.canonical_layout_digest


# ---------------------------------------------------------------------------------------
# The contract's own claims about phase 3
# ---------------------------------------------------------------------------------------


def test_phase_three_is_declared_and_wired() -> None:
    assert contract.PHASE_3_NAME.startswith("M7-2 Phase-3")
    assert contract.MATERIALIZER_MODULE in contract.PHASE_3_MAY_IMPORT_THE_CONTRACT
    assert contract.MATERIALIZER_MODULE not in contract.PHASE_2B_MAY_IMPORT_THE_CONTRACT
    assert contract.MATERIALIZATION_CONNECTOR_ROUTING_MODE == "manual"
    assert contract.MATERIALIZATION_MAY_CREATE_A_SECOND_WRITER is False
    assert contract.LLM_SUPPLIES_GEOMETRY_ON_THE_M7_MATERIALIZATION_PATH is False
    assert contract.MATERIALIZATION_PROVENANCE_CHAIN[-1] == "resulting_revision"


def test_the_task_book_names_the_phase_three_boundary() -> None:
    """A phase the contract declares and the task book never describes is unreviewable.

    The names are the ones a reader would search for: the phase, its step, its module, both of
    its versions and the three rules a later reader is most likely to relax by accident.
    """

    task_book = (Path(__file__).resolve().parents[2] / "docs" / "m7-2-deterministic-layout.md")
    text = task_book.read_text(encoding="utf-8")
    for name in (
        contract.PHASE_3_NAME,
        contract.PHASE_3_MATERIALIZATION_STEP,
        contract.MATERIALIZER_MODULE,
        contract.MATERIALIZER_VERSION,
        contract.MATERIALIZATION_DIGEST_VERSION,
        "MATERIALIZATION_MAY_CREATE_A_SECOND_WRITER",
        "MATERIALIZATION_MAY_CROP_TO_A_SMALLER_CANVAS",
        "MATERIALIZATION_MAY_LET_THE_WRITER_RE_ROUTE",
    ):
        assert name in text, f"the task book does not name {name!r}"
    assert "## 15." in text


def test_materialization_does_not_consume_semantic_operations() -> None:
    """The legacy path the phase exists to retire: no semantic transaction is imported here."""

    import agentcad.m7_layout_materialization as module

    source = Path(module.__file__).read_text()
    for forbidden in (
        "SemanticTransaction",
        "semantic_compiler",
        "agent_semantic",
        "create_document_with_operations",
    ):
        assert forbidden not in source
    assert "apply_transaction" in source


def test_a_layout_that_changed_the_plant_never_reaches_the_writer() -> None:
    """Proof A of step 5 stands in front of this module: a re-tagged plan cannot be written,
    because it cannot even be named."""

    topology = adapt(fixture_a_payload())
    step_four = derive_semantic_canvas(annotated_fixture_a())
    assert step_four.produced_at_step == STEP_4
    retagged = replace(
        step_four,
        engineering_entities=tuple(
            replace(fact, tag="P-999") for fact in step_four.engineering_entities
        ),
    )
    with pytest.raises(EngineeringSemanticPreservationError):
        finalize_semantic_layout(retagged, topology)
