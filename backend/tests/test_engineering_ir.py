"""Engineering IR tests (M2).

These tests pin the properties the project index and every surface depend on:
stable engineering identity, honest identity scope, line aggregation, signal
classification, reference integrity and deterministic tracing. They fail if the IR
ever silently merges two real objects, loses geometry-independent identity, or
confuses a process tap with an instrument signal.
"""

from __future__ import annotations

import pytest

from agentcad.engineering_ir import (
    IR_BUILDER_VERSION,
    build_engineering_graph,
    document_content_hash,
    graph_fingerprint,
    trace_engineering_object,
)
from agentcad.models import (
    ConnectorElement,
    ConnectorEndpoint,
    Document,
    JunctionElement,
    Layer,
    Point,
    Style,
    SymbolElement,
    TextElement,
)
from agentcad.symbols import SymbolRegistry


@pytest.fixture(scope="module")
def registry() -> SymbolRegistry:
    return SymbolRegistry()


def _symbol(
    element_id: str,
    symbol_key: str,
    *,
    tag: str = "",
    x: float = 0.0,
    y: float = 0.0,
    properties: dict | None = None,
    layer_id: str = "layer_default",
) -> SymbolElement:
    return SymbolElement(
        id=element_id,
        symbol_key=symbol_key,
        position=Point(x=x, y=y),
        width=60,
        height=60,
        label=tag,
        layer_id=layer_id,
        properties=properties or {},
    )


def _connector(
    element_id: str,
    source: tuple[str, str, float, float] | None,
    target: tuple[str, str, float, float] | None,
    *,
    tag: str = "",
    medium: str = "",
    diameter: str = "DN50",
    flow: str = "none",
    points: list[Point] | None = None,
    layer_id: str = "layer_default",
) -> ConnectorElement:
    def endpoint(value):
        if value is None:
            return None
        element_id_, port_id, x, y = value
        return ConnectorEndpoint(element_id=element_id_, port_id=port_id, point=Point(x=x, y=y))

    return ConnectorElement(
        id=element_id,
        points=points or [Point(x=0, y=0), Point(x=10, y=0)],
        source=endpoint(source),
        target=endpoint(target),
        routing="manual",
        process_tag=tag,
        medium=medium,
        nominal_diameter=diameter,
        flow_direction=flow,
        layer_id=layer_id,
    )


def _tagged_document(document_id: str = "doc_ir", **overrides) -> Document:
    """Two tagged pumps joined by a two-segment pipeline, plus an instrument."""

    pump = _symbol("pump_a", "centrifugal_pump", tag="P-101", x=100, y=100)
    pump_downstream = _symbol("pump_b", "centrifugal_pump", tag="P-102", x=400, y=100)
    instrument = _symbol("pi_a", "pressure_indicator", tag="PI-101", x=250, y=300)
    line_one = _connector(
        "line_a",
        ("pump_a", "discharge", 160, 100),
        ("junction_a", "node", 300, 100),
        tag="PL-1001",
        medium="water",
        flow="forward",
        points=[Point(x=160, y=100), Point(x=300, y=100)],
    )
    line_two = _connector(
        "line_b",
        ("junction_a", "node", 300, 100),
        ("pump_b", "suction", 400, 100),
        tag="PL-1001",
        medium="water",
        flow="forward",
        points=[Point(x=300, y=100), Point(x=400, y=100)],
    )
    junction = JunctionElement(id="junction_a", position=Point(x=300, y=100), label="J-1")
    element_list = [pump, pump_downstream, instrument, junction, line_one, line_two]
    payload = {
        "id": document_id,
        "name": overrides.pop("name", "IR fixture"),
        "revision": overrides.pop("revision", 1),
        "layers": [Layer(id="layer_default", name="Process")],
        "elements": element_list,
    }
    payload.update(overrides)
    return Document(**payload)


def test_objects_carry_stable_tag_scoped_identity(registry: SymbolRegistry) -> None:
    graph = build_engineering_graph(_tagged_document(), registry)

    ids = {record.object_id for record in graph.objects}
    assert "equipment:p-101" in ids
    pump = graph.object("equipment:p-101")
    assert pump is not None
    assert pump.kind == "equipment"
    assert pump.identity_scope == "tag"
    assert pump.tag == "P-101"

    line = graph.object("line:pl-1001")
    assert line is not None
    assert line.kind == "line"
    assert line.identity_scope == "tag"


def test_identity_survives_element_id_changes(registry: SymbolRegistry) -> None:
    """Re-importing a drawing must not create a new engineering identity."""

    first = build_engineering_graph(_tagged_document(), registry)
    renamed = _tagged_document()
    remapped = renamed.model_copy(deep=True)
    remapped.id = "doc_ir_copy"
    for element in remapped.elements:
        element.id = f"imported_{element.id}"
    for element in remapped.elements:
        if element.type != "connector":
            continue
        if element.source and element.source.element_id:
            element.source.element_id = f"imported_{element.source.element_id}"
        if element.target and element.target.element_id:
            element.target.element_id = f"imported_{element.target.element_id}"

    second = build_engineering_graph(remapped, registry)
    assert {record.object_id for record in first.objects} == {
        record.object_id for record in second.objects
    }
    assert graph_fingerprint(first) != graph_fingerprint(second) or first.content_hash


def test_duplicate_tag_is_disambiguated_and_reported(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    duplicate = _symbol("pump_duplicate", "centrifugal_pump", tag="P-101", x=600, y=100)
    document.elements.append(duplicate)

    graph = build_engineering_graph(document, registry)
    identities = sorted(
        record.object_id for record in graph.objects if record.kind == "equipment" and record.tag
    )
    assert "equipment:p-101" in identities
    assert "equipment:p-101#2" in identities
    finding = next(item for item in graph.findings if item.code == "IR_DUPLICATE_IDENTITY")
    assert finding.severity == "error"
    assert sorted(finding.object_ids) == ["equipment:p-101", "equipment:p-101#2"]
    assert finding.details["count"] == 2


def test_untagged_symbol_reports_element_identity_scope(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    document.elements.append(_symbol("untagged_pump", "centrifugal_pump", x=700, y=100))

    graph = build_engineering_graph(document, registry)
    untagged = next(
        record for record in graph.objects if record.primary_element_id == "untagged_pump"
    )
    assert untagged.identity_scope == "element"
    assert untagged.object_id == "equipment:untagged_pump"
    assert untagged.tag == ""


def test_pipeline_aggregates_connectors_and_sums_length(registry: SymbolRegistry) -> None:
    graph = build_engineering_graph(_tagged_document(), registry)
    line = graph.object("line:pl-1001")
    assert line is not None
    assert sorted(line.element_ids) == ["line_a", "line_b"]
    assert line.properties["member_count"] == 2
    assert line.length == pytest.approx(240.0)
    assert line.medium_class == "water"
    # Both member connectors resolve to the same pipeline object.
    edges = {edge.connector_id: edge.pipeline_object_id for edge in graph.edges}
    assert edges["line_a"] == "line:pl-1001"
    assert edges["line_b"] == "line:pl-1001"

    # A different medium must not be folded into the same pipeline.
    document = _tagged_document()
    document.elements.append(
        _connector(
            "line_c",
            ("pump_b", "discharge", 460, 100),
            None,
            tag="PL-1001",
            medium="steam",
            points=[Point(x=460, y=100), Point(x=500, y=100)],
        )
    )
    split = build_engineering_graph(document, registry)
    line_ids = sorted(
        record.object_id for record in split.objects if record.kind == "line" and record.tag
    )
    assert line_ids == ["line:pl-1001", "line:pl-1001#2"]


def test_process_tap_is_not_reclassified_as_signal(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    document.elements.append(
        _connector(
            "tap_1",
            ("pi_a", "process", 250, 240),
            ("line_a", "out", 250, 100),
            medium="water",
            points=[Point(x=250, y=240), Point(x=250, y=100)],
        )
    )

    graph = build_engineering_graph(document, registry)
    assert graph.signal_links == []


def test_declared_signal_medium_and_instrument_pair_are_classified(registry: SymbolRegistry) -> None:
    instrument = _symbol("pi_b", "pressure_indicator", tag="PI-102", x=250, y=380)
    document = _tagged_document()
    document.elements.extend(
        [
            instrument,
            _connector(
                "signal_1",
                ("pi_a", "process", 250, 300),
                ("pi_b", "process", 250, 380),
                medium="",
                points=[Point(x=250, y=300), Point(x=250, y=380)],
            ),
            _connector(
                "signal_2",
                ("pi_a", "process", 250, 300),
                ("pump_a", "discharge", 160, 100),
                medium="electric signal",
                points=[Point(x=250, y=300), Point(x=160, y=100)],
            ),
        ]
    )

    graph = build_engineering_graph(document, registry)
    by_connector = {link.connector_id: link for link in graph.signal_links}
    assert set(by_connector) == {"signal_1", "signal_2"}
    assert by_connector["signal_1"].classification == "instrument_to_instrument"
    assert by_connector["signal_2"].classification == "declared_medium"
    assert by_connector["signal_2"].instrument_object_ids == ["instrument:pi-101"]
    # A declared signal must not be reported as a signal without an instrument.
    assert not [
        finding for finding in graph.findings if finding.code == "IR_SIGNAL_WITHOUT_INSTRUMENT"
    ]


def test_signal_without_instrument_is_reported(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    document.elements.append(
        _connector(
            "signal_orphan",
            ("pump_a", "discharge", 160, 100),
            ("pump_b", "suction", 400, 100),
            medium="signal",
            points=[Point(x=160, y=100), Point(x=400, y=100)],
        )
    )

    graph = build_engineering_graph(document, registry)
    assert [finding.code for finding in graph.findings] == ["IR_SIGNAL_WITHOUT_INSTRUMENT"]


def test_off_page_connector_kind_direction_and_target(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    document.elements.extend(
        [
            _symbol(
                "opc_a",
                "off_page_connector_out",
                tag="PL-1001",
                x=600,
                y=100,
                properties={"target_document_id": "doc_other"},
            ),
            _symbol("opc_b", "off_page_connector_in", tag="PL-2002", x=700, y=100),
        ]
    )

    graph = build_engineering_graph(document, registry)
    out = graph.object("opc:pl-1001")
    assert out is not None
    assert out.kind == "off_page_connector"
    assert out.opc_direction == "out"
    assert out.target_document_id == "doc_other"
    assert graph.off_page_object_ids == ["opc:pl-1001", "opc:pl-2002"]
    missing = [
        finding for finding in graph.findings if finding.code == "IR_OPC_TARGET_MISSING"
    ]
    assert len(missing) == 1
    assert missing[0].object_ids == ["opc:pl-2002"]


def test_reference_integrity_finds_endpoints_to_missing_elements(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    document.elements.append(
        _connector(
            "line_dangling",
            None,
            ("ghost_element", "in", 800, 100),
            tag="PL-9001",
            medium="water",
            points=[Point(x=700, y=100), Point(x=800, y=100)],
        )
    )

    graph = build_engineering_graph(document, registry)
    finding = next(item for item in graph.findings if item.code == "IR_ENDPOINT_ELEMENT_MISSING")
    assert finding.severity == "error"
    assert "ghost_element" in finding.element_ids
    edge = next(edge for edge in graph.edges if edge.connector_id == "line_dangling")
    assert edge.target_object_id == "unbound:line_dangling:target"


def test_unknown_symbol_key_is_reported_not_guessed(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    document.elements.append(_symbol("ghost_symbol", "pressure_transmitter", tag="PT-101"))

    graph = build_engineering_graph(document, registry)
    finding = next(
        item for item in graph.findings if item.code == "IR_SYMBOL_DEFINITION_MISSING"
    )
    assert finding.severity == "error"
    record = graph.object("equipment:pt-101")
    assert record is not None
    assert record.symbol_name == ""


def test_isolated_object_and_orphan_line_findings(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    document.elements.extend(
        [
            _symbol("lonely_valve", "ball_valve", tag="XV-900"),
            _connector("orphan_line", None, None, tag="PL-9002", points=[Point(x=0, y=0), Point(x=5, y=0)]),
        ]
    )

    graph = build_engineering_graph(document, registry)
    codes = [finding.code for finding in graph.findings]
    assert "IR_ISOLATED_OBJECT" in codes
    assert "IR_ORPHAN_LINE" in codes
    orphan = next(item for item in graph.findings if item.code == "IR_ORPHAN_LINE")
    assert orphan.severity == "warning"
    assert orphan.object_ids == ["line:pl-9002"]


def test_connectivity_components_group_connected_objects_only(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    document.elements.append(_symbol("lonely_valve", "ball_valve", tag="XV-900"))
    document.elements.append(TextElement(id="note_1", position=Point(x=0, y=0), text="NOTE"))

    graph = build_engineering_graph(document, registry)
    groups = {frozenset(group) for group in graph.connectivity_components}
    main = frozenset({"equipment:p-101", "line:pl-1001", "junction:j-1", "equipment:p-102"})
    assert main in groups
    assert frozenset({"valve:xv-900"}) in groups
    # Annotations are not engineering topology.
    assert not any("annotation:note-1" in group for group in groups)


def test_content_hash_tracks_geometry_and_ignores_style(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    baseline = document_content_hash(document)

    restyled = document.model_copy(deep=True)
    for element in restyled.elements:
        element.style = Style(stroke="#ff00ff", stroke_width=9)
    assert document_content_hash(restyled) == baseline

    moved = document.model_copy(deep=True)
    moved.elements[0].position = Point(x=1, y=1)
    assert document_content_hash(moved) != baseline

    retagged = document.model_copy(deep=True)
    retagged.elements[0].label = "P-999"
    assert document_content_hash(retagged) != baseline


def test_graph_is_deterministic_for_the_same_document(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    first = build_engineering_graph(document, registry)
    second = build_engineering_graph(document.model_copy(deep=True), registry)
    assert graph_fingerprint(first) == graph_fingerprint(second)
    assert [record.object_id for record in first.objects] == [
        record.object_id for record in second.objects
    ]
    assert first.counts.objects == len(first.objects)
    assert first.builder_version == IR_BUILDER_VERSION


def test_trace_honours_declared_flow_direction(registry: SymbolRegistry) -> None:
    graph = build_engineering_graph(_tagged_document(), registry)

    downstream = trace_engineering_object(graph, "equipment:p-101", direction="downstream")
    assert [step.object_id for step in downstream.steps] == [
        "equipment:p-101",
        "junction:j-1",
        "equipment:p-102",
    ]
    assert all(step.via_connector_id for step in downstream.steps[1:])
    assert downstream.steps[1].direction == "downstream"
    assert downstream.traversed_pipeline_ids == ["line:pl-1001"]
    assert downstream.truncated is False

    upstream = trace_engineering_object(graph, "equipment:p-101", direction="upstream")
    assert [step.object_id for step in upstream.steps] == ["equipment:p-101"]


def test_trace_from_a_pipeline_enumerates_its_endpoints(registry: SymbolRegistry) -> None:
    graph = build_engineering_graph(_tagged_document(), registry)

    traced = trace_engineering_object(graph, "line:pl-1001")
    assert traced.steps[0].object_id == "line:pl-1001"
    assert sorted(traced.reached_object_ids) == [
        "equipment:p-101",
        "equipment:p-102",
        "junction:j-1",
        "line:pl-1001",
    ]
    direction_by_object = {step.object_id: step.direction for step in traced.steps}
    assert direction_by_object["equipment:p-101"] == "upstream"
    assert direction_by_object["junction:j-1"] == "downstream"


def test_trace_treats_undeclared_flow_as_bidirectional(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    document.elements.append(
        _connector(
            "line_undirected",
            ("pump_b", "discharge", 460, 100),
            None,
            tag="PL-3003",
            medium="water",
            points=[Point(x=460, y=100), Point(x=520, y=100)],
        )
    )
    graph = build_engineering_graph(document, registry)

    # Undeclared flow is traversable, but it must never be reported as a declared
    # downstream path: only the undirected endpoint is reached.
    downstream = trace_engineering_object(graph, "line:pl-3003", direction="downstream")
    assert [step.object_id for step in downstream.steps] == ["line:pl-3003", "equipment:p-102"]
    assert downstream.steps[1].direction == "undirected"
    assert downstream.steps[1].via_connector_id == "line_undirected"
    # An undeclared, half-dangling connection must not invent a downstream path.
    downstream_node = trace_engineering_object(graph, "equipment:p-102", direction="downstream")
    assert [step.object_id for step in downstream_node.steps] == ["equipment:p-102"]

    # Asking for both directions still reaches the whole connected group.
    both = trace_engineering_object(graph, "equipment:p-102")
    assert sorted(both.reached_object_ids) == [
        "equipment:p-101",
        "equipment:p-102",
        "junction:j-1",
    ]
    assert both.traversed_pipeline_ids == ["line:pl-1001"]


def test_trace_rejects_unknown_object_and_respects_max_steps(registry: SymbolRegistry) -> None:
    graph = build_engineering_graph(_tagged_document(), registry)
    with pytest.raises(KeyError):
        trace_engineering_object(graph, "equipment:does-not-exist")

    limited = trace_engineering_object(graph, "equipment:p-101", max_steps=2)
    assert len(limited.steps) == 2
    assert limited.truncated is True


def test_graphics_and_annotations_remain_addressable(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    document.elements.append(TextElement(id="note_1", position=Point(x=0, y=0), text="LINE NOTE"))

    graph = build_engineering_graph(document, registry)
    note = graph.object("annotation:note_1")
    assert note is not None
    assert note.label == "LINE NOTE"
    assert note.identity_scope == "element"
    assert graph.counts.annotations == 1
    assert graph.counts.graphics == 0
