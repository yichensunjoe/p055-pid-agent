"""M7-2 phase 3: materializing a finalized canonical layout through the one production writer.

Grouped by the promise each part makes to the engineer who asked for the drawing: the drawing
exists as a committed revision, it is the drawing the layout decided (not a re-routed or
re-centred approximation of it), the same layout always writes the same drawing, and the revision
can be traced back to the specification it came from.
"""

from __future__ import annotations

import inspect
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
    DEFAULT_SYSTEM_GROUP_ID,
    LABEL_ELEMENT_ROLE,
    MATERIALIZATION_LAYER_ID,
    SYMBOL_LABEL_ANNOTATION_ROLE,
    LayoutIsNotFinalizedError,
    MaterializationCanvasError,
    MaterializationError,
    MaterializationLabelError,
    MaterializationProvenanceError,
    MaterializationTargetNotEmptyError,
    _label_box_matches_text,
    apply_materialized_layout,
    document_rows,
    label_text_for,
    materialization_digest,
    materialization_matches_document,
    materialization_payload,
    materialization_provenance,
    materialization_record,
    materialize_canonical_layout,
    materialized_element_id,
    materialized_transaction,
    provenance_version_values,
    require_empty_target,
    with_materialization_provenance,
)
from agentcad.models import (
    AddElementOperation,
    AddSystemOperation,
    CreateDocumentRequest,
    Document,
    Point,
    SystemGroup,
    TransactionRequest,
    UpdateLayerOperation,
)
from agentcad.service import DocumentService, RevisionConflictError
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry
from agentcad.tag_resolver import resolve_symbol_tag

#: The tags the fixture's engineering rows carry. Not a labels input -- it is what the
#: materializer is expected to *derive*, so the tests read it as the expected drawing text.
TAGS = {
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


def materialized():
    plan, topology = finalized_layout()
    return materialize_canonical_layout(plan, document_id="doc_m7"), plan, topology


def materialized_from(plan):
    """Materialize a plan that a test has deliberately perturbed, for the refusal cases."""

    return materialize_canonical_layout(plan, document_id="doc_m7")


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

    identities = materialization_provenance(layout)
    assert "resulting_revision" not in identities  # a prediction is not part of the write
    audit = AuditContext(
        actor="m7-materializer",
        surface="internal",
        tool_name="m7_materialize_layout",
        validation_status="valid",
        metadata=dict(identities),
    )
    result = apply_materialized_layout(
        service, replace(layout, document_id=document_id), expected_revision=0, audit=audit
    )
    assert result.applied_operations == len(layout.operations)
    document = service.get_document(document_id)
    assert document.revision == 1
    # The record is closed with the revision the writer committed, not with a number supplied
    # before the write -- and it is that revision, not "the one we expected".
    record = materialization_record(layout, result)
    assert record["resulting_revision"] == str(document.revision)
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
    first = materialize_canonical_layout(plan, document_id="doc_m7")
    second = materialize_canonical_layout(shifted, document_id="doc_m7")
    assert second.origin_x == first.origin_x + 10.0
    assert second.materialization_digest != first.materialization_digest
    assert [row["x"] for row in second.rows] != [row["x"] for row in first.rows]


def test_a_drawing_cannot_carry_words_its_geometry_was_not_measured_for() -> None:
    """A re-tagged plan is refused *here* too, independently of the layout's own gate.

    Two cases, and they are refused by different guards, which is worth naming:

    * A tag of a **different length** changes the box the text measures to, so this module refuses
      it -- the box the layout placed is evidence about the string, and that evidence is checked.
    * A tag of the **same length** leaves the box identical, so nothing here can see it; step 5's
      engineering preservation gate is what refuses that one, and it refuses it earlier.
    """

    plan, _topology = finalized_layout()

    def retagged(tag: str):
        return replace(
            plan,
            engineering_entities=tuple(
                replace(fact, tag=tag) if fact.engineering_id == "el_ar_tank" else fact
                for fact in plan.engineering_entities
            ),
        )

    with pytest.raises(MaterializationLabelError) as raised:
        materialized_from(retagged("V-1011"))
    assert "does not measure" in str(raised.value)
    same_length = retagged("V-102")
    assert len(label_text_for(same_length, "el_ar_tank")) == len(TAGS["el_ar_tank"])
    assert _label_box_matches_text(
        next(
            row for row in same_length.annotations if str(row["engineering_id"]) == "el_ar_tank"
        ),
        "V-102",
    )


def test_the_drawn_text_is_inside_the_digest() -> None:
    """A same-length text substitution leaves the box identical, so only the text can reveal it."""

    layout, _plan, _topology = materialized()
    payload = materialization_payload(
        document_canvas=(layout.canvas_width, layout.canvas_height),
        origin=(layout.origin_x, layout.origin_y),
        rows=layout.rows,
    )
    rewritten = [
        {**row, "text": "V-999"}
        if row["kind"] == "annotation" and row["engineering_id"] == "el_ar_tank"
        else row
        for row in layout.rows
    ]
    assert materialization_digest(payload) != materialization_digest(
        {**payload, "rows": rewritten}
    )


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
        materialize_canonical_layout(unfinished, document_id="doc_m7")
    assert STEP_4 in str(raised.value) and "finalized canonical layout" in str(raised.value)
    named = replace(unfinished, produced_at_step=STEP_5)
    assert named.canonical_layout_digest == ""
    with pytest.raises(LayoutIsNotFinalizedError) as named_raised:
        materialize_canonical_layout(named, document_id="doc_m7")
    assert "finalized canonical layout" in str(named_raised.value)


# ---------------------------------------------------------------------------------------
# The label text is derived, and the box has to fit it
# ---------------------------------------------------------------------------------------


def test_there_is_no_label_override_channel() -> None:
    """The first half of the fix: the free input does not exist, rather than being validated.

    A validated override would still be a caller deciding what the drawing says under an
    unchanged canonical layout digest. The signature is the boundary, so the signature is what
    this asserts.
    """

    parameters = inspect.signature(materialize_canonical_layout).parameters
    assert "labels" not in parameters
    assert set(parameters) == {"plan", "document_id"}


def test_the_label_text_is_derived_from_the_engineering_row() -> None:
    plan, _topology = finalized_layout()
    for node_id, tag in TAGS.items():
        assert label_text_for(plan, node_id) == tag
    with pytest.raises(MaterializationLabelError):
        label_text_for(plan, "el_ghost")


def test_the_materialized_text_is_the_tag_its_geometry_was_measured_for() -> None:
    layout, _plan, _topology = materialized()
    texts = {row["engineering_id"]: row["text"] for row in layout.rows if row["kind"] == "annotation"}
    assert texts == TAGS
    for row in layout.rows:
        if row["kind"] != "annotation":
            continue
        # The box the layout placed is exactly the box this text measures to: the width was
        # already the text length, which is why a same-length substitution would pass a
        # geometry-only check.
        assert _label_box_matches_text(
            {"x": row["x"], "y": row["y"], "width": row["width"], "height": row["height"]},
            str(row["text"]),
        )


def test_a_label_box_that_does_not_fit_the_tag_is_refused() -> None:
    """A box measured for other text would put words on the drawing nothing was measured for."""

    plan, _topology = finalized_layout()
    node_id = "el_ar_tank"
    stretched = replace(
        plan,
        annotations=tuple(
            {**row, "width": float(row["width"]) + 30.0}
            if str(row["engineering_id"]) == node_id
            else row
            for row in plan.annotations
        ),
    )
    with pytest.raises(MaterializationLabelError) as raised:
        materialized_from(stretched)
    assert node_id in str(raised.value) and "does not measure" in str(raised.value)


def test_an_entity_without_a_tag_has_no_label_to_write() -> None:
    """An annotated node whose engineering row carries no tag has no text the box was for."""

    plan, _topology = finalized_layout()
    node_id = "el_ar_tank"
    untagged = replace(
        plan,
        engineering_entities=tuple(
            replace(fact, tag="") if fact.engineering_id == node_id else fact
            for fact in plan.engineering_entities
        ),
    )
    with pytest.raises(MaterializationLabelError) as raised:
        materialized_from(untagged)
    assert "carries no tag" in str(raised.value)


def test_the_symbol_label_field_is_left_empty(tmp_path: Path) -> None:
    """One editable text per symbol: the tag lives in the annotation, not in ``label``."""

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    _document_id, document = _committed(service, layout)
    symbols = [element for element in document.elements if element.type == "symbol"]
    assert symbols and all(element.label == "" for element in symbols)
    assert resolve_symbol_tag(document, symbols[0]) == TAGS[
        str((symbols[0].properties or {}).get("engineering_id"))
    ]


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


# ---------------------------------------------------------------------------------------
# The pre-write boundary: a target holding content the layout did not decide
# ---------------------------------------------------------------------------------------


def _an_unrelated_element() -> object:
    from agentcad.models import SymbolElement

    return SymbolElement(
        id="el_unrelated",
        symbol_key="gas_tank",
        position=Point(x=40, y=40),
        width=120,
        height=90,
        system_id=DEFAULT_SYSTEM_GROUP_ID,
        metadata={"engineering_id": "el_unrelated"},
    )


def test_a_target_that_already_holds_an_element_is_refused_before_the_write(
    tmp_path: Path,
) -> None:
    """Reconciliation alone would commit the merge first and report it second.

    So the assertion is not only "it raised": it is that the revision did not move, the element
    is not in the drawing, and there is no history entry claiming a drawing was written.
    """

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    document_id = seed_document(service, layout)
    service.apply_transaction(
        document_id,
        TransactionRequest(
            operations=[AddElementOperation(element=_an_unrelated_element())],
            expected_revision=0,
        ),
    )
    before = service.get_document(document_id)
    assert before.revision == 1

    with pytest.raises(MaterializationTargetNotEmptyError) as raised:
        apply_materialized_layout(
            service, replace(layout, document_id=document_id), expected_revision=before.revision
        )
    assert "el_unrelated" in str(raised.value)
    after = service.get_document(document_id)
    assert after.revision == before.revision
    assert [element.id for element in after.elements] == ["el_unrelated"]
    assert not [
        element
        for element in after.elements
        if str((element.metadata or {}).get("engineering_id", "")) == "el_ar_tank"
    ]
    history = service.get_history(document_id)
    assert not [entry for entry in history if "materialization" in (entry.label or "")]


def test_a_target_that_already_holds_a_foreign_system_group_is_refused(tmp_path: Path) -> None:
    """Systems are in the drawing's reconciliation universe, so a stray one is not a baseline."""

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    document_id = seed_document(service, layout)
    service.apply_transaction(
        document_id,
        TransactionRequest(
            operations=[AddSystemOperation(system=SystemGroup(id="S_unrelated", name="Other"))],
            expected_revision=0,
        ),
    )
    with pytest.raises(MaterializationTargetNotEmptyError) as raised:
        apply_materialized_layout(
            service, replace(layout, document_id=document_id), expected_revision=1
        )
    assert "S_unrelated" in str(raised.value)
    assert service.get_document(document_id).revision == 1


def test_an_empty_target_is_permitted_including_the_default_system_group() -> None:
    """The baseline is "nothing engineered", not "no rows": a created document already has these."""

    document = Document(name="Empty")
    assert [system.id for system in document.systems] == [DEFAULT_SYSTEM_GROUP_ID]
    assert contract.MATERIALIZATION_TARGET_PERMITTED_SYSTEM_GROUP_ID == DEFAULT_SYSTEM_GROUP_ID
    require_empty_target(document)
    assert document.elements == []


def test_a_revision_that_moved_after_the_preflight_is_refused_atomically(tmp_path: Path) -> None:
    """The revision the preflight read is the revision the commit is bound to.

    Another writer landing N+1 between the read and the write must not be appended on top of: the
    drawing that was verified belongs to N, and the writer's own atomic expected_revision is what
    refuses it.
    """

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    document_id = seed_document(service, layout)
    preflighted = service.get_document(document_id).revision
    service.apply_transaction(
        document_id,
        TransactionRequest(
            operations=[AddElementOperation(element=_an_unrelated_element())],
            expected_revision=preflighted,
        ),
    )
    raced = service.get_document(document_id)
    assert raced.revision == preflighted + 1

    # The preflight itself now refuses, and if it were bypassed the writer still would.
    with pytest.raises(MaterializationError):
        apply_materialized_layout(
            service, replace(layout, document_id=document_id), expected_revision=preflighted
        )
    with pytest.raises(RevisionConflictError):
        service.apply_transaction(
            document_id,
            materialized_transaction(layout, expected_revision=preflighted),
            source="system",
        )
    settled = service.get_document(document_id)
    assert settled.revision == preflighted + 1
    assert [element.id for element in settled.elements] == ["el_unrelated"]


def test_the_preflight_is_not_the_only_completeness_protection() -> None:
    """Both halves stay declared: the pre-write boundary and the post-write reconciliation."""

    assert contract.MATERIALIZATION_PREFLIGHTS_THE_TARGET_BEFORE_IT_WRITES
    assert contract.MATERIALIZATION_COMMITS_AGAINST_THE_PREFLIGHTED_REVISION
    assert contract.MATERIALIZATION_RECONCILIATION_IS_NOT_THE_ONLY_COMPLETENESS_PROTECTION
    assert contract.MATERIALIZATION_MAY_CONTINUE_AFTER_A_REVISION_CONFLICT is False
    assert "apply_transaction" in inspect.getsource(apply_materialized_layout)
    assert "require_empty_target" in inspect.getsource(apply_materialized_layout)


def test_a_row_whose_written_text_was_edited_is_reported(tmp_path: Path) -> None:
    """A same-length text swap leaves the box identical, so nothing but the text can see it."""

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    _document_id, document = _committed(service, layout)
    assert materialization_matches_document(layout, document) == []
    label_id = layout.element_id_for("el_ar_tank", LABEL_ELEMENT_ROLE)
    for element in document.elements:
        if element.id == label_id:
            assert len(element.text) == len(TAGS["el_ar_tank"])
            element.text = "V-999"
    problems = materialization_matches_document(layout, document)
    assert any("V-999" in problem for problem in problems), problems


def test_a_symbol_label_written_after_the_commit_is_reported(tmp_path: Path) -> None:
    """``symbol.label`` is the second text surface, so reconciliation reads it rather than assumes it."""

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    _document_id, document = _committed(service, layout)
    assert materialization_matches_document(layout, document) == []
    symbol_id = layout.element_id_for("el_ar_tank", "symbol")
    for element in document.elements:
        if element.id == symbol_id:
            element.label = "V-999"
    problems = materialization_matches_document(layout, document)
    assert any("label" in problem and "V-999" in problem for problem in problems), problems


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


def test_the_provenance_chain_reaches_the_committed_revision(tmp_path: Path) -> None:
    """Five identities known before the write, plus the revision the writer committed."""

    layout, plan, topology = materialized()
    service = make_service(tmp_path)
    document_id = seed_document(service, layout)
    identities = materialization_provenance(layout)
    result = apply_materialized_layout(
        service,
        replace(layout, document_id=document_id),
        expected_revision=0,
        audit=AuditContext(
            actor="m7-materializer",
            surface="internal",
            tool_name="m7_materialize_layout",
            validation_status="valid",
            metadata=dict(identities),
        ),
    )
    record = materialization_record(layout, result)
    for link in contract.MATERIALIZATION_PROVENANCE_CHAIN:
        assert link in record, link
    assert record["resulting_revision"] == str(result.document.revision)
    assert record["adapter_topology_digest"] == topology.digest
    assert record["canonical_layout_digest"] == plan.canonical_layout_digest
    assert record["symbol_geometry_catalog_digest"] == plan.symbol_geometry_catalog_digest
    assert record["materialization_digest"] == layout.materialization_digest
    assert record["diagram_spec_semantic_digest"]
    # The revision half cannot be produced before the write: there is nothing to read it from.
    with pytest.raises(MaterializationError):
        materialization_record(layout, object())


def test_the_record_takes_no_caller_supplied_revision() -> None:
    """Same shape as the label channel: the boundary is the signature, not a validation.

    A parameter that accepted a revision would let the caller write down the number it hoped for,
    and the assertion that the record matches the commit would then be comparing the code with a
    copy of itself.
    """

    parameters = inspect.signature(materialization_record).parameters
    # The layout, and the write's own result. Nothing else -- in particular no plan, because a
    # caller able to pass a different plan could ask for one drawing's record and get another's.
    assert set(parameters) == {"layout", "result"}
    assert not {"revision", "result_revision", "predicted_revision", "predicted"} & set(parameters)
    assert '"revision"' in inspect.getsource(materialization_record)


def test_the_record_closes_on_the_committed_revision_not_on_a_prediction(tmp_path: Path) -> None:
    """A revision supplied before the write would be a claim about the *next* revision.

    Two writers can both make that claim. So the identity half is asserted to carry no revision at
    all, and the same code path is then run against a document that is not at revision 1 -- the
    record follows the commit rather than the expectation.
    """

    layout, plan, _topology = materialized()
    assert not contract.MATERIALIZATION_PROVENANCE_PREDICTS_THE_REVISION
    assert contract.MATERIALIZATION_PROVENANCE_READS_THE_REVISION_FROM_THE_COMMITTED_RESULT
    assert "resulting_revision" not in materialization_provenance(layout)
    service = make_service(tmp_path)
    document_id = seed_document(service, layout)
    # A revision that is not 1, reached without putting anything engineered in the target: the
    # baseline is about the drawing, not about the revision counter.
    service.apply_transaction(
        document_id,
        TransactionRequest(
            operations=[
                UpdateLayerOperation(layer_id=MATERIALIZATION_LAYER_ID, patch={"name": "Sheet 1"})
            ],
            expected_revision=0,
        ),
    )
    assert service.get_document(document_id).revision == 1
    result = apply_materialized_layout(
        service, replace(layout, document_id=document_id), expected_revision=1
    )
    assert result.document.revision == 2
    assert materialization_record(layout, result)["resulting_revision"] == "2"


def test_the_board_records_the_identities_as_an_audit_metadata_dict() -> None:
    """A dict of strings is what the existing audit path persists, so that is the shape checked."""

    layout, plan, _topology = materialized()
    context = with_materialization_provenance(layout)
    recorded = context.metadata[contract.M7_PROVENANCE_METADATA_KEY]
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in recorded.items())
    assert recorded["canonical_layout_digest"] == plan.canonical_layout_digest


# ---------------------------------------------------------------------------------------
# Provenance is the materializer's to record, not the caller's to omit or to overwrite
# ---------------------------------------------------------------------------------------


def _persisted_audit(service: DocumentService, document_id: str, revision: int):
    return service.audit.revision_evidence(document_id, revision).audit_record


def test_a_caller_that_passes_no_audit_still_gets_the_full_identity_chain(tmp_path: Path) -> None:
    """The write cannot be the way to get a revision without a traceable chain.

    ``audit`` is optional attribution, not optional provenance: when it is absent the materializer
    builds the context itself, and the persisted record is read back rather than trusted -- the
    claim is about what is in the database, not about what was passed in.
    """

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    document_id = seed_document(service, layout)
    result = apply_materialized_layout(
        service, replace(layout, document_id=document_id), expected_revision=0
    )

    record = _persisted_audit(service, document_id, result.document.revision)
    assert record is not None
    recorded = record.evidence["metadata"][contract.M7_PROVENANCE_METADATA_KEY]
    # The key set is asserted against the declarations, not against what the same call would return:
    # a comparison that derived both sides from one function would agree with itself if the record
    # quietly stopped carrying the versions that make its digests traceable.
    assert set(recorded) == set(contract.M7_PROVENANCE_REQUIRED_IDENTITIES) | set(
        contract.MATERIALIZATION_PROVENANCE_VERSION_FIELDS
    )
    for name in contract.M7_PROVENANCE_REQUIRED_IDENTITIES:
        assert recorded[name], name
    assert record.result_revision == result.document.revision
    # The complete relation: the five identities + the revision the writer committed.
    assert {**recorded, "resulting_revision": str(record.result_revision)} == (
        materialization_record(layout, result)
    )


def test_a_caller_cannot_supply_the_identity_chain(tmp_path: Path) -> None:
    """Refused before the write: silently overwriting would leave the caller believing its chain ran."""

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    document_id = seed_document(service, layout)
    forged = AuditContext(
        actor="m7-materializer",
        surface="internal",
        tool_name="m7_materialize_layout",
        validation_status="valid",
        metadata={contract.M7_PROVENANCE_METADATA_KEY: {"canonical_layout_digest": "forged"}},
    )
    with pytest.raises(MaterializationProvenanceError) as raised:
        apply_materialized_layout(
            service, replace(layout, document_id=document_id), expected_revision=0, audit=forged
        )
    assert contract.M7_PROVENANCE_METADATA_KEY in str(raised.value)
    after = service.get_document(document_id)
    assert after.revision == 0
    assert after.elements == []
    # Only the creation entry: no transaction was applied, so nothing claims a drawing was written.
    assert [entry.action for entry in service.get_history(document_id)] == ["create"]


def test_attribution_is_still_the_callers_to_give(tmp_path: Path) -> None:
    """The refusal is about the chain, not about the caller's context: attribution survives."""

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    document_id = seed_document(service, layout)
    audit = AuditContext(
        actor="web-user",
        surface="rest",
        tool_name="m7_nl_draw",
        validation_status="valid",
        metadata={"prompt_kind": "natural-language"},
    )
    result = apply_materialized_layout(
        service, replace(layout, document_id=document_id), expected_revision=0, audit=audit
    )
    record = _persisted_audit(service, document_id, result.document.revision)
    assert record.actor == "web-user"
    metadata = record.evidence["metadata"]
    assert metadata["prompt_kind"] == "natural-language"
    assert metadata[contract.M7_PROVENANCE_METADATA_KEY]["materialization_digest"] == (
        layout.materialization_digest
    )
    # The caller's own context is untouched: the materializer built a copy.
    assert contract.M7_PROVENANCE_METADATA_KEY not in audit.metadata


def test_every_identity_carries_the_version_that_defines_it(tmp_path: Path) -> None:
    """"Each digest has a version" is a different invariant from "the record has a version".

    The weaker one is satisfied by any single version field for the whole record, and it would let a
    stored digest be uninterpretable on its own. So the binding table is read here and each identity
    is asserted to carry *its own* versions -- the persisted record, not the object handed in.
    """

    service = make_service(tmp_path)
    layout, _plan, _topology = materialized()
    document_id = seed_document(service, layout)
    result = apply_materialized_layout(
        service, replace(layout, document_id=document_id), expected_revision=0
    )
    recorded = _persisted_audit(service, document_id, result.document.revision).evidence[
        "metadata"
    ][contract.M7_PROVENANCE_METADATA_KEY]

    bindings = dict(contract.PROVENANCE_IDENTITY_VERSION_BINDINGS)
    assert set(bindings) == set(contract.M7_PROVENANCE_REQUIRED_IDENTITIES)
    for identity, versions in contract.PROVENANCE_IDENTITY_VERSION_BINDINGS:
        assert recorded[identity], identity
        assert versions, identity
        for version_field in versions:
            assert recorded[version_field], (identity, version_field)
    # No version without an identity to interpret: the record's version set is exactly the bindings'.
    assert {key for key in recorded if key.endswith("_version")} == set(
        contract.MATERIALIZATION_PROVENANCE_VERSION_FIELDS
    )
    assert set(recorded) == set(contract.M7_PROVENANCE_REQUIRED_IDENTITIES) | {
        key for key in recorded if key.endswith("_version")
    }


def test_the_version_bindings_and_the_identities_cannot_drift_apart() -> None:
    """The relation is data, so the validator can check it in both directions."""

    bound = dict(contract.PROVENANCE_IDENTITY_VERSION_BINDINGS)
    assert set(bound) == set(contract.M7_PROVENANCE_REQUIRED_IDENTITIES)
    assert len(bound) == len(contract.PROVENANCE_IDENTITY_VERSION_BINDINGS)
    derived = tuple(
        dict.fromkeys(version for _identity, versions in bound.items() for version in versions)
    )
    assert contract.MATERIALIZATION_PROVENANCE_VERSION_FIELDS == derived
    # The values are declared, and none of them is a digest: a version is not an identity.
    values = provenance_version_values()
    assert set(values) == set(contract.MATERIALIZATION_PROVENANCE_VERSION_FIELDS)
    assert all(value and not value.startswith("m7-0") for value in values.values())
    assert not set(values) & set(contract.M7_PROVENANCE_REQUIRED_IDENTITIES)


def test_a_declared_version_that_is_not_persisted_is_reported(monkeypatch) -> None:
    """A binding that points at a field nobody writes would be a promise, not a record."""

    from agentcad import m7_layout_contract as module

    monkeypatch.setattr(
        module,
        "PROVENANCE_IDENTITY_VERSION_BINDINGS",
        (*list(module.PROVENANCE_IDENTITY_VERSION_BINDINGS), ("ghost_identity", ("x",))),
    )
    problems = module.validate_contract()
    assert any("version binding" in problem for problem in problems)


def test_a_layout_with_no_identity_chain_may_not_be_written() -> None:
    """The chain is refused when it is incomplete, not written and reported afterwards."""

    layout, _plan, _topology = materialized()
    with pytest.raises(MaterializationProvenanceError) as raised:
        with_materialization_provenance(replace(layout, provenance_identities=()))
    assert "diagram_spec_semantic_digest" in str(raised.value)
    # The five required identities are a subset of what is recorded: the versions travel with them.
    recorded = with_materialization_provenance(layout).metadata[
        contract.M7_PROVENANCE_METADATA_KEY
    ]
    assert set(contract.M7_PROVENANCE_REQUIRED_IDENTITIES) <= set(recorded)


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
