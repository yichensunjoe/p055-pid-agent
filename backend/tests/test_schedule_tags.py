"""The schedule's tag column, after the annotation-polish fix (M4 regression follow-up).

The tag rules were corrected to read the canonical resolver; the schedule was the one
engineering output still reading the raw ``symbol.label`` field, which the production polish
clears. So on a drawing the product itself wrote, the equipment/instrument schedules printed
an empty tag column while the resolver knew the tag. These tests fix the consumer and nothing
else: the row projection must be identical to before except for the tag text itself.

Two output paths are covered because they are two different models in the report —
``EquipmentScheduleRow`` and ``InstrumentScheduleRow`` — and only one of them was ever
exercised by the repair work.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentcad.engineering_reports import build_engineering_report
from agentcad.models import (
    AddElementOperation,
    CreateDocumentRequest,
    Document,
    Point,
    SymbolElement,
    TextElement,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry

EQUIPMENT_KEY = "ball_valve"
INSTRUMENT_KEY = "pressure_indicator"


def _registry() -> SymbolRegistry:
    return SymbolRegistry()


def _symbol(
    element_id: str,
    symbol_key: str,
    *,
    label: str = "",
    tag: str | None = None,
    x: float = 200.0,
) -> SymbolElement:
    definition = _registry().get(symbol_key)
    return SymbolElement(
        id=element_id,
        symbol_key=symbol_key,
        position=Point(x=x, y=300),
        width=definition.width,
        height=definition.height,
        label=label,
        properties={} if tag is None else {"tag": tag},
    )


def _annotation(element_id: str, text: str, *, subject: str) -> TextElement:
    return TextElement(
        id=element_id,
        position=Point(x=0, y=0),
        text=text,
        metadata={"parent_element_id": subject, "annotation_role": "symbol_label"},
    )


def _document(elements: list[object]) -> Document:
    return Document(id="doc_schedule", name="schedule fixture", elements=list(elements))


def _rows(document: Document) -> tuple[list[dict], list[dict]]:
    report = build_engineering_report(document, _registry())
    return (
        [row.model_dump(mode="json") for row in report.equipment],
        [row.model_dump(mode="json") for row in report.instruments],
    )


def _polished_document(tmp_path: Path) -> Document:
    """The production path: semantic compiler -> governed apply -> production polish."""

    from agentcad.agent_semantic_models import ConnectPortsOperation, SemanticTransaction
    from agentcad.semantic_compiler_engine import SemanticTransactionCompiler

    registry = _registry()
    service = DocumentService(SQLiteDocumentStore(tmp_path / "schedule.db"), registry)
    created = service.create_document(CreateDocumentRequest(name="schedule polish fixture"))
    operations = [
        AddElementOperation(
            element=_symbol("v1", EQUIPMENT_KEY, label="HV-101", tag="HV-101", x=200)
        ),
        AddElementOperation(
            element=_symbol("i1", INSTRUMENT_KEY, label="PT-101", tag="PT-101", x=700)
        ),
        ConnectPortsOperation(
            connector_id="p1",
            source_element_id="v1",
            source_port_id="out",
            target_element_id="i1",
            target_port_id="process",
            process_tag="L-1",
            medium="process",
            nominal_diameter="DN50",
        ),
    ]
    compiled = SemanticTransactionCompiler(service).compile(
        created.id,
        SemanticTransaction(operations=operations, expected_revision=0, label="schedule fixture"),
    )
    assert compiled.assessment.valid and compiled.transaction is not None, [
        issue.message for issue in compiled.assessment.issues
    ]
    service.apply_transaction(created.id, compiled.transaction, source="system")
    return service.get_document(created.id)


def test_a_polished_drawing_shows_the_resolved_tag_in_both_schedules(tmp_path: Path):
    document = _polished_document(tmp_path)
    by_id = {element.id: element for element in document.elements}

    # The premise of the test, asserted rather than assumed: the polish really did clear the
    # fixed label fields, so a raw-field read cannot produce these tags.
    assert by_id["v1"].label == ""
    assert by_id["i1"].label == ""

    equipment, instruments = _rows(document)
    assert [row["element_id"] for row in equipment] == ["v1"]
    assert [row["element_id"] for row in instruments] == ["i1"]
    assert equipment[0]["tag"] == "HV-101"
    assert instruments[0]["tag"] == "PT-101"


def test_annotation_only_symbols_resolve_in_both_schedules():
    document = _document(
        [
            _symbol("v1", EQUIPMENT_KEY),
            _symbol("i1", INSTRUMENT_KEY),
            _annotation("v1__label", "HV-300", subject="v1"),
            _annotation("i1__label", "PT-300", subject="i1"),
        ]
    )
    equipment, instruments = _rows(document)
    assert equipment[0]["tag"] == "HV-300"
    assert instruments[0]["tag"] == "PT-300"


def test_legacy_labels_still_feed_the_schedule_unchanged():
    document = _document(
        [
            _symbol("v1", EQUIPMENT_KEY, label="HV-401"),
            _symbol("i1", INSTRUMENT_KEY, label="PT-401"),
        ]
    )
    equipment, instruments = _rows(document)
    assert equipment[0]["tag"] == "HV-401"
    assert instruments[0]["tag"] == "PT-401"


def test_properties_tag_outranks_the_raw_label_in_the_schedule():
    document = _document(
        [
            _symbol("v1", EQUIPMENT_KEY, label="STALE-1", tag="HV-501"),
            _symbol("i1", INSTRUMENT_KEY, label="STALE-2", tag="PT-501"),
        ]
    )
    equipment, instruments = _rows(document)
    assert equipment[0]["tag"] == "HV-501"
    assert instruments[0]["tag"] == "PT-501"


def test_only_the_tag_field_moves_between_a_legacy_and_a_polished_row():
    """The same tag reached two ways has to produce the same row — everything else included."""

    # The product's own polished shape: the tag property survives, the fixed label field does
    # not, and the visible tag is an annotation. The legacy shape carries the same tag in the
    # same property with the field still set, so every other row field is comparable.
    legacy = _document([_symbol("v1", EQUIPMENT_KEY, label="HV-601", tag="HV-601")])
    polished = _document(
        [
            _symbol("v1", EQUIPMENT_KEY, tag="HV-601"),
            _annotation("v1__label", "HV-601", subject="v1"),
        ]
    )

    legacy_rows, _ = _rows(legacy)
    polished_rows, _ = _rows(polished)
    assert legacy_rows == polished_rows

    # And the equality is not an accident of both being empty: the tag is the resolved one.
    assert legacy_rows[0]["tag"] == "HV-601"
    assert json.dumps(legacy_rows[0], sort_keys=True) == json.dumps(polished_rows[0], sort_keys=True)


def test_an_untagged_symbol_still_shows_an_empty_tag_column():
    document = _document([_symbol("v1", EQUIPMENT_KEY)])
    equipment, _ = _rows(document)
    assert equipment[0]["tag"] == ""
