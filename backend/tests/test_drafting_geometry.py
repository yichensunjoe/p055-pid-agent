"""Geometry and drafting-semantics tests (M3).

These tests pin the facts the drafting engine reasons about: one port mapping, one
crossing definition, one text-overlap definition, deterministic canonical order, and a
junction classification that distinguishes a real branch from a dangling node.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcad.diagram_quality import analyze_diagram_quality
from agentcad.drafting_geometry import (
    canonical_document,
    canonical_elements,
    canonical_operations,
    classify_junctions,
    collision_findings,
    connector_segments,
    detect_crossings,
    drafting_content_hash,
    element_rect,
    resolve_ports,
    resolve_scope,
    snapshot,
    structural_findings,
)
from agentcad.drafting_models import DraftingPolicy, DraftingRequest
from agentcad.models import (
    AddElementOperation,
    ConnectorElement,
    ConnectorEndpoint,
    CreateDocumentRequest,
    JunctionElement,
    Point,
    SymbolElement,
    TextElement,
    TransactionRequest,
    UpdateElementOperation,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry


def make_service(tmp_path: Path) -> DocumentService:
    return DocumentService(SQLiteDocumentStore(tmp_path / "drafting-geometry.db"), SymbolRegistry())


def add_document(
    service: DocumentService,
    elements: list,
    *,
    name: str = "Drafting geometry",
    metadata: dict | None = None,
) -> str:
    document = service.create_document(CreateDocumentRequest(name=name, metadata=metadata or {}))
    service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=0,
            operations=[AddElementOperation(element=element) for element in elements],
        ),
    )
    return document.id


def seed_pump_line(service: DocumentService, *, y: float = 300) -> str:
    """A pump -> gate valve process line bound to real port points."""

    document = service.create_document(CreateDocumentRequest(name="Pump line"))
    pump = SymbolElement(
        id="pump",
        symbol_key="centrifugal_pump",
        position=Point(x=100, y=y),
        width=80,
        height=70,
        label="P-101",
    )
    valve = SymbolElement(
        id="valve",
        symbol_key="gate_valve",
        position=Point(x=500, y=y - 15),
        width=60,
        height=50,
        label="HV-101",
    )
    document = service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=0,
            operations=[AddElementOperation(element=pump), AddElementOperation(element=valve)],
        ),
    ).document
    seeded_pump = next(element for element in document.elements if element.id == "pump")
    seeded_valve = next(element for element in document.elements if element.id == "valve")
    assert seeded_pump.type == "symbol" and seeded_valve.type == "symbol"
    source_point = service._symbol_port_point(seeded_pump, "discharge")
    target_point = service._symbol_port_point(seeded_valve, "in")
    service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=document.revision,
            operations=[
                AddElementOperation(
                    element=ConnectorElement(
                        id="pipe",
                        points=[source_point, target_point],
                        source=ConnectorEndpoint(
                            element_id="pump", port_id="discharge", point=source_point
                        ),
                        target=ConnectorEndpoint(
                            element_id="valve", port_id="in", point=target_point
                        ),
                        routing="manual",
                        process_tag="PL-1001",
                        medium="water",
                        flow_direction="forward",
                    )
                )
            ],
        ),
    )
    return document.id


def test_port_points_have_exactly_one_implementation(tmp_path: Path) -> None:
    """The drafting port mapping and the service port mapping are the same mapping."""

    service = make_service(tmp_path)
    rotated = SymbolElement(
        id="rotated",
        symbol_key="horizontal_vessel",
        position=Point(x=200, y=140),
        width=260,
        height=160,
        rotation=90,
    )
    document = service.create_document(CreateDocumentRequest(name="Rotated ports"))
    document = service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=0,
            operations=[AddElementOperation(element=rotated)],
        ),
    ).document
    element = next(item for item in document.elements if item.id == "rotated")
    assert element.type == "symbol"

    ports = {
        port.port_id: port
        for port in resolve_ports(document, service.symbols)
        if port.element_id == "rotated"
    }
    definition = service.symbols.get("horizontal_vessel")
    assert set(ports) == {port.id for port in definition.ports}
    for port in definition.ports:
        expected = service._symbol_port_point(element, port.id)
        resolved = ports[port.id]
        assert resolved.point.x == pytest.approx(expected.x, abs=1e-9)
        assert resolved.point.y == pytest.approx(expected.y, abs=1e-9)
    # A rotated vessel exposes rotated outward normals, not symbol-space sides.
    assert ports["in"].outward_normal == (0, -1)
    assert ports["out"].outward_normal == (0, 1)


def test_resolve_ports_reports_pipe_load_and_outward_side(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = seed_pump_line(service)
    document = service.get_document(document_id)

    ports = {(port.element_id, port.port_id): port for port in resolve_ports(document, service.symbols)}
    discharge = ports[("pump", "discharge")]
    assert discharge.direction == "out"
    assert discharge.side == "top"
    assert discharge.outward_normal == (0, -1)
    assert discharge.connector_ids == ["pipe"]
    assert discharge.connected_element_ids == ["valve"]
    suction = ports[("pump", "suction")]
    assert suction.connector_ids == []
    assert suction.side == "left"


def test_crossing_classification_agrees_with_drawing_quality(tmp_path: Path) -> None:
    """The drafting crossing definition matches the definition used to score drawings."""

    service = make_service(tmp_path)
    document = service.create_document(CreateDocumentRequest(name="Crossing"))
    left = SymbolElement(
        id="left", symbol_key="centrifugal_pump", position=Point(x=80, y=400), width=80, height=70
    )
    right = SymbolElement(
        id="right", symbol_key="gate_valve", position=Point(x=600, y=385), width=60, height=50
    )
    top = SymbolElement(
        id="top", symbol_key="gate_valve", position=Point(x=322, y=120), width=60, height=50
    )
    bottom = SymbolElement(
        id="bottom", symbol_key="gate_valve", position=Point(x=322, y=700), width=60, height=50
    )
    document = service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=0,
            operations=[
                AddElementOperation(element=element) for element in (left, right, top, bottom)
            ],
        ),
    ).document
    symbols = {element.id: element for element in document.elements}
    assert all(element.type == "symbol" for element in symbols.values())
    a_start = service._symbol_port_point(symbols["left"], "discharge")  # type: ignore[arg-type]
    a_end = service._symbol_port_point(symbols["right"], "in")  # type: ignore[arg-type]
    b_start = service._symbol_port_point(symbols["top"], "out")  # type: ignore[arg-type]
    b_end = service._symbol_port_point(symbols["bottom"], "in")  # type: ignore[arg-type]
    horizontal = ConnectorElement(
        id="pipe_h",
        points=[a_start, Point(x=a_start.x, y=430), Point(x=a_end.x, y=430), a_end],
        source=ConnectorEndpoint(element_id="left", port_id="discharge", point=a_start),
        target=ConnectorEndpoint(element_id="right", port_id="in", point=a_end),
        routing="manual",
        flow_direction="forward",
    )
    vertical = ConnectorElement(
        id="pipe_v",
        points=[b_start, Point(x=352, y=b_start.y), Point(x=352, y=b_end.y), b_end],
        source=ConnectorEndpoint(element_id="top", port_id="out", point=b_start),
        target=ConnectorEndpoint(element_id="bottom", port_id="in", point=b_end),
        routing="manual",
        flow_direction="forward",
    )
    document = service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=document.revision,
            operations=[
                AddElementOperation(element=horizontal),
                AddElementOperation(element=vertical),
            ],
        ),
    ).document

    crossings = detect_crossings(document)
    assert [(item.first_connector_id, item.second_connector_id) for item in crossings] == [
        ("pipe_h", "pipe_v")
    ]
    assert crossings[0].bridged is False
    quality = analyze_diagram_quality(document, service.symbols)
    assert quality.metrics.geometric_crossings == len(crossings) == 1
    assert quality.metrics.unbridged_crossings == 1

    codes = {
        finding.code
        for finding in structural_findings(document, service.symbols, DraftingPolicy())
    }
    assert "DRAFT_CROSSING_UNBRIDGED" in codes

    bridged = document.model_copy(deep=True)
    for item in bridged.elements:
        if item.id == "pipe_v":
            item.crossing_style = "jump"  # type: ignore[union-attr]
    assert detect_crossings(bridged)[0].bridged_by == "pipe_v"
    assert not [
        finding
        for finding in structural_findings(bridged, service.symbols, DraftingPolicy())
        if finding.code == "DRAFT_CROSSING_UNBRIDGED"
    ]

    # Exactly one bridge: bridging both lines is reported, not silently accepted.
    both = document.model_copy(deep=True)
    for item in both.elements:
        if item.type == "connector":
            item.crossing_style = "jump"
    assert detect_crossings(both)[0].bridged_by == "both"
    assert "DRAFT_CROSSING_DOUBLE_BRIDGED" in {
        finding.code
        for finding in structural_findings(both, service.symbols, DraftingPolicy())
    }


def test_crossing_that_lands_on_a_junction_is_reported(tmp_path: Path) -> None:
    """A junction in the middle of a crossing claims connectivity the model does not have."""

    service = make_service(tmp_path)
    document = service.create_document(CreateDocumentRequest(name="Junction crossing"))
    document = service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=0,
            operations=[
                AddElementOperation(element=JunctionElement(id="junction", position=Point(x=300, y=300)))
            ],
        ),
    ).document
    horizontal = ConnectorElement(
        id="pipe_h",
        points=[Point(x=100, y=300), Point(x=500, y=300)],
        routing="manual",
        crossing_style="jump",
    )
    vertical = ConnectorElement(
        id="pipe_v",
        points=[Point(x=300, y=100), Point(x=300, y=500)],
        routing="manual",
        crossing_style="jump",
    )
    document = service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=document.revision,
            operations=[
                AddElementOperation(element=horizontal),
                AddElementOperation(element=vertical),
            ],
        ),
    ).document

    crossings = detect_crossings(document)
    assert [item.at_junction_id for item in crossings] == ["junction"]
    codes = {
        finding.code
        for finding in structural_findings(document, service.symbols, DraftingPolicy())
    }
    # The most specific fact wins: this is a topology claim, not a styling issue.
    assert "DRAFT_CROSSING_ON_JUNCTION" in codes
    assert "DRAFT_CROSSING_UNBRIDGED" not in codes


def test_junction_degree_distinguishes_branch_inline_and_dangling(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document = service.create_document(CreateDocumentRequest(name="Junctions"))
    document = service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=0,
            operations=[
                AddElementOperation(
                    element=JunctionElement(id=element_id, position=Point(x=x, y=300))
                )
                for element_id, x in (("branch", 300), ("inline", 600), ("dangling", 900))
            ],
        ),
    ).document

    operations = []
    for index, start in enumerate((Point(x=100, y=300), Point(x=300, y=100))):
        operations.append(
            AddElementOperation(
                element=ConnectorElement(
                    id=f"branch_in_{index}",
                    points=[start, Point(x=300, y=300)],
                    target=ConnectorEndpoint(
                        element_id="branch", port_id="node", point=Point(x=300, y=300)
                    ),
                    routing="manual",
                )
            )
        )
    operations.append(
        AddElementOperation(
            element=ConnectorElement(
                id="branch_out",
                points=[Point(x=300, y=300), Point(x=400, y=300)],
                source=ConnectorEndpoint(
                    element_id="branch", port_id="node", point=Point(x=300, y=300)
                ),
                routing="manual",
            )
        )
    )
    for index, start in enumerate((Point(x=500, y=300), Point(x=700, y=300))):
        operations.append(
            AddElementOperation(
                element=ConnectorElement(
                    id=f"inline_{index}",
                    points=[start, Point(x=600, y=300)],
                    target=ConnectorEndpoint(
                        element_id="inline", port_id="node", point=Point(x=600, y=300)
                    ),
                    routing="manual",
                )
            )
        )
    operations.append(
        AddElementOperation(
            element=ConnectorElement(
                id="dangling_pipe",
                points=[Point(x=800, y=300), Point(x=900, y=300)],
                target=ConnectorEndpoint(
                    element_id="dangling", port_id="node", point=Point(x=900, y=300)
                ),
                routing="manual",
            )
        )
    )
    document = service.apply_transaction(
        document.id,
        TransactionRequest(expected_revision=document.revision, operations=operations),
    ).document

    junctions = {item.element_id: item for item in classify_junctions(document)}
    assert (junctions["branch"].kind, junctions["branch"].degree) == ("branch", 3)
    assert (junctions["inline"].kind, junctions["inline"].degree) == ("inline", 2)
    assert (junctions["dangling"].kind, junctions["dangling"].degree) == ("dangling", 1)
    findings = structural_findings(document, service.symbols, DraftingPolicy())
    dangling = [finding for finding in findings if finding.code == "DRAFT_JUNCTION_DANGLING"]
    assert [finding.element_ids[0] for finding in dangling] == ["dangling"]
    assert dangling[0].details["degree"] == 1


def test_port_findings_expose_broken_endpoint_bindings(tmp_path: Path) -> None:
    """A binding the router silently ignores must still be reported."""

    service = make_service(tmp_path)
    document_id = seed_pump_line(service)
    document = service.get_document(document_id)
    broken = document.model_copy(deep=True)
    for element in broken.elements:
        if element.type == "connector":
            element.target = ConnectorEndpoint(  # type: ignore[union-attr]
                element_id="valve", port_id="nope", point=Point(x=500, y=300)
            )
    findings = [
        finding
        for finding in structural_findings(broken, service.symbols, DraftingPolicy())
        if finding.code == "DRAFT_PORT_UNRESOLVED"
    ]
    assert len(findings) == 1
    assert findings[0].element_ids == ["pipe", "valve"]
    assert findings[0].details["available_port_ids"] == ["in", "out"]


def test_text_collisions_report_editable_labels_only(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    pump = SymbolElement(
        id="pump", symbol_key="centrifugal_pump", position=Point(x=200, y=200), width=80, height=70
    )
    overlapping = TextElement(id="t1", position=Point(x=210, y=230), text="P-101")
    neighbour = TextElement(id="t2", position=Point(x=214, y=236), text="P-101")
    document_id = add_document(service, [pump, overlapping, neighbour])
    document = service.get_document(document_id)

    findings = collision_findings(document, service.symbols, DraftingPolicy())
    text_text = [finding for finding in findings if finding.code == "DRAFT_TEXT_TEXT_OVERLAP"]
    assert len(text_text) == 1
    assert set(text_text[0].element_ids) == {"t1", "t2"}
    # Derived symbol labels are not editable drafting geometry: they are absent from the
    # drafting collision set (the drawing rules still score them).
    assert not [finding for finding in findings if "virtual-label" in "".join(finding.element_ids)]
    quality = analyze_diagram_quality(document, service.symbols)
    assert quality.metrics.text_text_overlaps >= 1


def test_reserved_drawing_space_is_enforced_for_symbols_pipes_and_labels(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = add_document(
        service,
        [
            SymbolElement(
                id="pump",
                symbol_key="centrifugal_pump",
                position=Point(x=1200, y=700),
                width=80,
                height=70,
            ),
            ConnectorElement(
                id="pipe",
                points=[Point(x=100, y=840), Point(x=1500, y=840)],
                routing="manual",
            ),
            TextElement(id="note", position=Point(x=1300, y=810), text="备注"),
        ],
        metadata={
            "layout_regions": [
                {"x1": 1150, "y1": 620, "x2": 1550, "y2": 880, "kind": "legend", "label": "图例"}
            ]
        },
    )
    document = service.get_document(document_id)

    findings = [
        finding
        for finding in structural_findings(document, service.symbols, DraftingPolicy())
        if finding.code == "DRAFT_RESERVED_REGION_OVERLAP"
    ]
    assert {finding.element_ids[0] for finding in findings} == {"pump", "pipe", "note"}
    assert all(finding.severity == "error" for finding in findings)
    assert all(finding.details["region_kind"] == "legend" for finding in findings)
    assert snapshot(document, service.symbols, DraftingPolicy()).reserved_region_intrusions == 3


def test_canonical_order_and_content_hash_ignore_element_storage_order(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = add_document(
        service,
        [
            SymbolElement(
                id="b", symbol_key="gate_valve", position=Point(x=300, y=100), width=60, height=50
            ),
            SymbolElement(
                id="a",
                symbol_key="centrifugal_pump",
                position=Point(x=100, y=100),
                width=80,
                height=70,
            ),
            JunctionElement(id="c", position=Point(x=500, y=100)),
        ],
    )
    document = service.get_document(document_id)

    assert [element.id for element in canonical_elements(document)] == ["a", "b", "c"]
    shuffled = document.model_copy(deep=True)
    shuffled.elements = list(reversed(shuffled.elements))
    assert [element.id for element in canonical_elements(shuffled)] == ["a", "b", "c"]
    assert drafting_content_hash(document) == drafting_content_hash(shuffled)

    canonical = canonical_document(document)
    assert [element.id for element in canonical.elements] == ["a", "b", "c"]
    # A restyle is presentation only and must not change drafting identity.
    restyled = canonical.model_copy(deep=True)
    for element in restyled.elements:
        element.style.opacity = 0.5
    assert drafting_content_hash(canonical) == drafting_content_hash(restyled)

    operations = [
        UpdateElementOperation(element_id="b", patch={"position": {"x": 1, "y": 1}}),
        UpdateElementOperation(element_id="a", patch={"position": {"x": 2, "y": 2}}),
    ]
    assert [operation.element_id for operation in canonical_operations(operations)] == ["a", "b"]
    assert [
        operation.element_id for operation in canonical_operations(reversed(operations))
    ] == ["a", "b"]


def test_scope_resolution_prefers_region_then_selection(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = seed_pump_line(service)
    document = service.get_document(document_id)

    whole_ids, whole_kind = resolve_scope(document, DraftingRequest())
    assert whole_kind == "document"
    assert whole_ids == ["pipe", "pump", "valve"]

    region_ids, region_kind = resolve_scope(
        document, DraftingRequest(region={"x1": 0, "y1": 0, "x2": 260, "y2": 600})
    )
    assert region_kind == "region"
    assert region_ids == ["pump"]

    selection_ids, selection_kind = resolve_scope(
        document, DraftingRequest(element_ids=["valve"])
    )
    assert selection_kind == "selection"
    assert selection_ids == ["valve"]

    # A selection naming a connector pulls in the elements it is bound to.
    bound_ids, bound_kind = resolve_scope(document, DraftingRequest(element_ids=["pipe"]))
    assert bound_kind == "selection"
    assert bound_ids == ["pipe", "pump", "valve"]

    outside = DraftingRequest(region={"x1": 2000, "y1": 2000, "x2": 2100, "y2": 2100})
    assert resolve_scope(document, outside) == ([], "region")


def test_connector_segments_follow_the_declared_points() -> None:
    connector = ConnectorElement(
        id="pipe",
        points=[Point(x=0, y=0), Point(x=100, y=0), Point(x=100, y=100)],
        routing="manual",
    )
    segments = connector_segments(connector)
    assert [segment.length for segment in segments] == [100, 100]
    assert segments[0].horizontal and segments[1].vertical
    assert element_rect(connector) is None
