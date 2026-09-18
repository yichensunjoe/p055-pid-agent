"""Engineering IR tests (M2).

These tests pin the properties the project index and every surface depend on:
identity that is independent of the (mutable) tag, honest identity basis, line
aggregation, first-class signals separated from process topology, off-page
connection identity, reference integrity and deterministic tracing. They fail if the
IR ever silently merges two real objects, lets a rename move an identity, confuses a
process tap with an instrument signal, or routes a signal through process flow.
"""

from __future__ import annotations

import pytest

from agentcad.engineering_ir import (
    IR_BUILDER_VERSION,
    IR_VERSION,
    build_engineering_graph,
    document_content_hash,
    graph_fingerprint,
    off_page_connection_id_for,
    off_page_connection_pair_id,
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
    metadata: dict | None = None,
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
        metadata=metadata or {},
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
    metadata: dict | None = None,
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
        metadata=metadata or {},
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


def _renamed_every_tag(document: Document) -> Document:
    """Return the same drawing with every tag/line number renamed."""

    renamed = document.model_copy(deep=True)
    for element in renamed.elements:
        if element.type == "symbol" and element.label.strip():
            element.label = "X-" + element.label.strip()
        elif element.type == "connector" and element.process_tag.strip():
            element.process_tag = "Y-" + element.process_tag.strip()
        elif element.type == "junction" and element.label.strip():
            element.label = "Z-" + element.label.strip()
    return renamed


# --------------------------------------------------------------------------- #
# Identity: decoupled from the tag
# --------------------------------------------------------------------------- #


def test_engineering_identity_is_stable_ids_not_tags(registry: SymbolRegistry) -> None:
    graph = build_engineering_graph(_tagged_document(), registry)

    pump = graph.object("equipment:p-101")
    assert pump is not None
    assert pump.kind == "equipment"
    assert pump.tag == "P-101"
    assert pump.tag_key == "equipment:p-101"
    # The identity is a surrogate derived from the drawing handle, not the tag.
    assert pump.engineering_id.startswith("eq_")
    assert "p-101" not in pump.engineering_id.casefold()
    assert pump.identity_basis == "element"

    line = graph.object("line:pl-1001")
    assert line is not None
    assert line.kind == "line"
    assert line.engineering_id.startswith("ln_")
    assert line.properties["line_number"] == "PL-1001"
    assert graph.version == IR_VERSION
    assert graph.builder_version == IR_BUILDER_VERSION


def test_renaming_a_tag_does_not_move_the_engineering_identity(registry: SymbolRegistry) -> None:
    """P-101 becoming P-201 is the same equipment, not a new one."""

    original = _tagged_document()
    renamed = _renamed_every_tag(original)

    first = build_engineering_graph(original, registry)
    second = build_engineering_graph(renamed, registry)

    assert sorted((obj.kind, obj.engineering_id) for obj in first.objects) == sorted(
        (obj.kind, obj.engineering_id) for obj in second.objects
    )
    # ... while the mutable attributes really did change.
    assert {obj.tag_key for obj in second.objects} != {obj.tag_key for obj in first.objects}
    assert graph_fingerprint(first) != graph_fingerprint(second)

    original_pump = first.object("P-101")
    assert original_pump is not None
    pump = first.object(original_pump.engineering_id)
    assert pump is not None
    # The same identity now answers to the new tag and no longer to the old one.
    renamed_pump = second.object(original_pump.engineering_id)
    assert renamed_pump is not None
    assert renamed_pump.tag == "X-P-101"
    assert renamed_pump.tag_key == "equipment:x-p-101"
    assert second.object("P-101") is None


def test_declared_ids_win_and_survive_element_id_changes(registry: SymbolRegistry) -> None:
    """A drawing may declare authoritative ids; they outrank the drawing handle.

    That is also the bridge for re-imports: identity is element-anchored by default,
    so an importer that regenerates element ids must carry the declared ids to keep the
    same objects.
    """

    declared = _tagged_document()
    declared.elements[0].properties["engineering_id"] = "EQ-ASSET-01"
    declared.elements[3].metadata["line_id"] = "L-1001-A"
    declared.elements[4].metadata["line_id"] = "L-1001-A"
    graph = build_engineering_graph(declared, registry)

    pump = graph.object("P-101")
    assert pump is not None
    assert pump.identity_basis == "declared"
    assert pump.declared_id == "EQ-ASSET-01"
    line = graph.object("line:pl-1001")
    assert line is not None
    assert line.identity_basis == "declared"
    assert line.declared_id == "L-1001-A"

    # Re-import: new element handles, same declared ids.
    reimported = declared.model_copy(deep=True)
    reimported.id = "doc_ir_copy"
    for element in reimported.elements:
        element.id = f"imported_{element.id}"
    for element in reimported.elements:
        if element.type != "connector":
            continue
        if element.source and element.source.element_id:
            element.source.element_id = f"imported_{element.source.element_id}"
        if element.target and element.target.element_id:
            element.target.element_id = f"imported_{element.target.element_id}"
    second = build_engineering_graph(reimported, registry)

    # The declared objects keep their identity; the element-anchored rest is new.
    assert second.object("P-101").engineering_id == pump.engineering_id
    assert second.object("line:pl-1001").engineering_id == line.engineering_id
    assert second.object("P-102").engineering_id not in {
        obj.engineering_id for obj in graph.objects
    }


def test_element_anchored_identity_changes_when_handles_are_discarded(
    registry: SymbolRegistry,
) -> None:
    """Honest limitation: without declared ids, a re-import with new handles is new."""

    document = _tagged_document()
    reimported = document.model_copy(deep=True)
    reimported.id = "doc_ir_other"
    for element in reimported.elements:
        element.id = f"imported_{element.id}"
    for element in reimported.elements:
        if element.type != "connector":
            continue
        if element.source and element.source.element_id:
            element.source.element_id = f"imported_{element.source.element_id}"
        if element.target and element.target.element_id:
            element.target.element_id = f"imported_{element.target.element_id}"

    first = build_engineering_graph(document, registry)
    second = build_engineering_graph(reimported, registry)
    assert not {obj.engineering_id for obj in first.objects} & {
        obj.engineering_id for obj in second.objects
    }
    # The tags still let a human (or an integration) find the same drawing content.
    assert {obj.tag_key for obj in first.objects} == {obj.tag_key for obj in second.objects}


def test_duplicate_tag_is_disambiguated_and_reported(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    duplicate = _symbol("pump_duplicate", "centrifugal_pump", tag="P-101", x=600, y=100)
    document.elements.append(duplicate)

    graph = build_engineering_graph(document, registry)
    pumps = sorted(
        (obj for obj in graph.objects if obj.kind == "equipment" and obj.tag == "P-101"),
        key=lambda item: item.engineering_id,
    )
    assert len(pumps) == 2
    # Two real objects, two distinct identities — and honest tag keys.
    assert len({obj.engineering_id for obj in pumps}) == 2
    assert sorted(obj.tag_key for obj in pumps) == ["equipment:p-101", "equipment:p-101#2"]

    finding = next(item for item in graph.findings if item.code == "IR_DUPLICATE_IDENTITY")
    assert finding.severity == "error"
    assert sorted(finding.object_ids) == sorted(obj.engineering_id for obj in pumps)
    assert finding.details["count"] == 2


def test_tag_lookup_still_resolves_and_reports_what_it_resolved(
    registry: SymbolRegistry,
) -> None:
    """Backward compatibility: tag-based references keep working."""

    graph = build_engineering_graph(_tagged_document(), registry)
    by_key = graph.object("equipment:p-101")
    by_tag = graph.object("P-101")
    by_element = graph.object("pump_a")
    assert by_key is not None and by_tag is not None and by_element is not None
    assert by_key.engineering_id == by_tag.engineering_id == by_element.engineering_id

    traced = trace_engineering_object(graph, "equipment:p-101", direction="downstream")
    assert traced.origin_engineering_id == by_key.engineering_id
    assert traced.resolved_from == "equipment:p-101"


# --------------------------------------------------------------------------- #
# Signals: first-class engineering objects, separated from process topology
# --------------------------------------------------------------------------- #


def _signal_document() -> Document:
    instrument = _symbol("pi_b", "pressure_indicator", tag="PI-102", x=250, y=380)
    document = _tagged_document()
    document.elements.extend(
        [
            instrument,
            _connector(
                "signal_1",
                ("pi_a", "process", 250, 300),
                ("pi_b", "process", 250, 380),
                points=[Point(x=250, y=300), Point(x=250, y=380)],
            ),
            _connector(
                "signal_2",
                ("pi_a", "process", 250, 300),
                ("pump_a", "discharge", 160, 100),
                medium="electric signal",
                tag="SI-1001",
                points=[Point(x=250, y=300), Point(x=160, y=100)],
            ),
        ]
    )
    return document


def test_signals_are_first_class_engineering_objects(registry: SymbolRegistry) -> None:
    graph = build_engineering_graph(_signal_document(), registry)

    assert graph.counts.signals == 2
    assert len(graph.signals) == 2
    signals = {obj.tag: obj for obj in graph.objects if obj.kind == "signal"}
    declared = signals["SI-1001"]

    assert declared.engineering_id.startswith("sg_")
    assert declared.engineering_id in graph.signals
    assert declared.tag_key == "signal:si-1001"
    assert declared.signal is not None
    assert declared.signal.signal_id == declared.engineering_id
    assert declared.signal.connector_id == "signal_2"
    assert declared.signal.signal_type == "electrical"
    assert declared.signal.classification == "declared_medium"
    assert declared.signal.provenance["basis"] == "medium"
    assert declared.signal.source_engineering_id == graph.object("PI-101").engineering_id
    assert declared.signal.target_engineering_id == graph.object("P-101").engineering_id
    assert declared.signal.instrument_engineering_ids == [graph.object("PI-101").engineering_id]

    structural = next(
        obj for obj in graph.objects if obj.kind == "signal" and obj.signal.connector_id == "signal_1"
    )
    assert structural.signal.classification == "instrument_to_instrument"
    assert structural.signal.signal_type == "unknown"
    assert len(structural.signal.instrument_engineering_ids) == 2
    assert structural.signal.provenance["basis"] == "endpoint_kinds"


def test_signal_identity_survives_a_medium_or_tag_change(registry: SymbolRegistry) -> None:
    document = _signal_document()
    changed = document.model_copy(deep=True)
    for element in changed.elements:
        if element.id == "signal_2":
            element.medium = "pneumatic signal"
            element.process_tag = "SI-9002"
    first = build_engineering_graph(document, registry)
    second = build_engineering_graph(changed, registry)

    assert first.signals == second.signals
    updated = second.object(first.signals[0])
    assert updated is not None


def test_signal_declared_id_is_honoured(registry: SymbolRegistry) -> None:
    document = _signal_document()
    for element in document.elements:
        if element.id == "signal_2":
            element.metadata["signal_id"] = "SIG-1001"
    graph = build_engineering_graph(document, registry)
    declared = next(
        obj for obj in graph.objects if obj.kind == "signal" and obj.declared_id == "SIG-1001"
    )
    assert declared.identity_basis == "declared"
    assert declared.signal is not None and declared.signal.signal_id == declared.engineering_id


def test_signal_wiring_is_kept_out_of_process_topology(registry: SymbolRegistry) -> None:
    document = _signal_document()
    # A process tap on the same instrument must stay a process line.
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

    signal_edge_connectors = {
        edge.connector_id for edge in graph.edges if edge.edge_class == "signal"
    }
    process_edge_connectors = {
        edge.connector_id for edge in graph.edges if edge.edge_class == "process"
    }
    assert signal_edge_connectors == {"signal_1", "signal_2"}
    assert "tap_1" in process_edge_connectors
    assert graph.counts.signal_edges == 2
    assert graph.counts.process_edges == 3  # two pipeline segments plus the tap
    # The tap is a process line, so it must not appear as a signal object.
    assert graph.counts.signals == 2
    # Signals never join process connectivity components.
    signal_ids = set(graph.signals)
    assert not any(
        signal_ids & set(component) for component in graph.connectivity_components
    )
    # A signal edge must never carry a pipeline object either.
    assert all(not edge.pipeline_engineering_id for edge in graph.edges if edge.edge_class == "signal")


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
    assert graph.signals == []


def test_instrument_air_stays_a_process_line(registry: SymbolRegistry) -> None:
    """``instrument air`` is a utility fluid, not instrument wiring."""

    document = _tagged_document()
    document.elements.append(
        _connector(
            "air_line",
            ("pump_b", "discharge", 460, 100),
            None,
            tag="IA-1001",
            medium="instrument air",
            points=[Point(x=460, y=100), Point(x=520, y=100)],
        )
    )
    graph = build_engineering_graph(document, registry)
    assert graph.signals == []
    assert any(obj.kind == "line" and obj.tag == "IA-1001" for obj in graph.objects)


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
    orphan = next(item for item in graph.findings if item.code == "IR_SIGNAL_WITHOUT_INSTRUMENT")
    assert orphan.severity == "warning"
    assert orphan.object_ids == graph.signals
    # An unnamed signal is worth flagging too, as information rather than an error.
    assert any(item.code == "IR_SIGNAL_UNNAMED" for item in graph.findings)


def test_tracing_a_signal_walks_wiring_not_flow(registry: SymbolRegistry) -> None:
    graph = build_engineering_graph(_signal_document(), registry)
    structural_signal_id = next(
        obj.engineering_id
        for obj in graph.objects
        if obj.kind == "signal" and obj.signal is not None and obj.signal.connector_id == "signal_1"
    )

    traced = trace_engineering_object(graph, "signal:si-1001")
    assert traced.edge_class == "signal"
    # Walking from the declared signal stays inside the instrument wiring network:
    # its own two ends, plus the rest of the signal wiring it is physically tied to.
    assert set(traced.reached_engineering_ids) == {
        graph.object("signal:si-1001").engineering_id,
        structural_signal_id,
        graph.object("PI-101").engineering_id,
        graph.object("PI-102").engineering_id,
        graph.object("P-101").engineering_id,
    }
    # ... and never walks the process line or junction it sits next to.
    assert not {
        graph.object("line:pl-1001").engineering_id,
        graph.object("junction:j-1").engineering_id,
    } & set(traced.reached_engineering_ids)
    assert traced.traversed_pipeline_ids == []

    # Starting from the process side keeps process flow: no signal is reached.
    process = trace_engineering_object(graph, "P-101")
    assert process.edge_class == "process"
    assert not set(graph.signals) & set(process.reached_engineering_ids)


# --------------------------------------------------------------------------- #
# Lines, off-page connectors, integrity
# --------------------------------------------------------------------------- #


def test_pipeline_aggregates_connectors_and_sums_length(registry: SymbolRegistry) -> None:
    graph = build_engineering_graph(_tagged_document(), registry)
    line = graph.object("line:pl-1001")
    assert line is not None
    assert sorted(line.element_ids) == ["line_a", "line_b"]
    assert line.properties["member_count"] == 2
    assert line.length == pytest.approx(240.0)
    assert line.medium_class == "water"
    # Both member connectors resolve to the same pipeline object.
    edges = {edge.connector_id: edge.pipeline_engineering_id for edge in graph.edges}
    assert edges["line_a"] == line.engineering_id
    assert edges["line_b"] == line.engineering_id

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
        obj.engineering_id for obj in split.objects if obj.kind == "line" and obj.tag
    )
    assert len(line_ids) == 2
    assert len(set(line_ids)) == 2
    assert sorted(
        obj.tag_key for obj in split.objects if obj.kind == "line" and obj.tag
    ) == ["line:pl-1001", "line:pl-1001#2"]


def test_line_identity_tracks_endpoints_not_the_line_number(registry: SymbolRegistry) -> None:
    """Splitting a segment in two keeps the line identity; renaming keeps it too."""

    document = _tagged_document()
    split = document.model_copy(deep=True)
    # Replace the single segment line_a with two segments through an extra junction.
    split.elements = [element for element in split.elements if element.id != "line_a"]
    split.elements.append(JunctionElement(id="junction_mid", position=Point(x=200, y=100), label="J-2"))
    split.elements.append(
        _connector(
            "line_a1",
            ("pump_a", "discharge", 160, 100),
            ("junction_mid", "node", 230, 100),
            tag="PL-1001",
            medium="water",
            flow="forward",
        )
    )
    split.elements.append(
        _connector(
            "line_a2",
            ("junction_mid", "node", 230, 100),
            ("junction_a", "node", 300, 100),
            tag="PL-1001",
            medium="water",
            flow="forward",
        )
    )

    first = build_engineering_graph(document, registry)
    second = build_engineering_graph(split, registry)
    assert first.object("line:pl-1001").engineering_id == second.object("line:pl-1001").engineering_id


def test_off_page_connector_identity_and_connection_id(registry: SymbolRegistry) -> None:
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
    assert out.engineering_id.startswith("opc_")
    assert out.opc_direction == "out"
    assert out.target_document_id == "doc_other"
    assert out.off_page_connection_id.startswith("opc_conn_")
    assert graph.off_page_object_ids == sorted([out.engineering_id, graph.object("opc:pl-2002").engineering_id])
    missing = [finding for finding in graph.findings if finding.code == "IR_OPC_TARGET_MISSING"]
    assert len(missing) == 1
    assert missing[0].object_ids == [graph.object("opc:pl-2002").engineering_id]

    # Renaming the service must not move the connection identity.
    renamed = _renamed_every_tag(document)
    renamed_graph = build_engineering_graph(renamed, registry)
    renamed_out = next(
        obj for obj in renamed_graph.objects if obj.kind == "off_page_connector" and obj.opc_direction == "out"
    )
    assert renamed_out.engineering_id == out.engineering_id
    assert renamed_out.off_page_connection_id == out.off_page_connection_id
    assert renamed_out.tag != out.tag


def test_declared_connection_id_is_honoured_and_duplicates_reported(
    registry: SymbolRegistry,
) -> None:
    document = _tagged_document()
    document.elements.extend(
        [
            _symbol(
                "opc_a",
                "off_page_connector_out",
                tag="PL-1001",
                properties={
                    "target_document_id": "doc_other",
                    "connection_id": "OPC-CONN-0001",
                },
            ),
            _symbol(
                "opc_b",
                "off_page_connector_out",
                tag="PL-3003",
                properties={
                    "target_document_id": "doc_other",
                    "connection_id": "OPC-CONN-0001",
                },
            ),
        ]
    )
    graph = build_engineering_graph(document, registry)
    declared = next(
        obj for obj in graph.objects if obj.kind == "off_page_connector" and obj.tag == "PL-1001"
    )
    assert declared.off_page_connection_id == "OPC-CONN-0001"
    assert declared.identity_basis == "declared"

    finding = next(
        item for item in graph.findings if item.code == "IR_OPC_CONNECTION_ID_DUPLICATE"
    )
    assert finding.severity == "error"
    assert finding.details["off_page_connection_id"] == "OPC-CONN-0001"


def test_connection_identity_helpers_are_symmetric_and_tag_free() -> None:
    assert off_page_connection_pair_id("eq_a", "opc_b") == off_page_connection_pair_id(
        "opc_b", "eq_a"
    )
    assert off_page_connection_pair_id("eq_a", "opc_b").startswith("opc_conn_")
    assert off_page_connection_id_for("opc_element_1") == off_page_connection_id_for(
        "opc_element_1"
    )
    assert off_page_connection_id_for("opc_element_1") != off_page_connection_id_for(
        "opc_element_2"
    )
    assert (
        off_page_connection_id_for("opc_element_1", declared="DECLARED-1") == "DECLARED-1"
    )


def test_untagged_symbol_reports_element_identity_basis(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    document.elements.append(_symbol("untagged_pump", "centrifugal_pump", x=700, y=100))

    graph = build_engineering_graph(document, registry)
    untagged = next(
        record for record in graph.objects if record.primary_element_id == "untagged_pump"
    )
    assert untagged.identity_basis == "element"
    assert untagged.tag == ""
    assert untagged.tag_key == ""


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
    assert edge.target_engineering_id == "unbound:line_dangling:target"


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
    assert orphan.object_ids == [graph.object("line:pl-9002").engineering_id]


def test_connectivity_components_group_connected_objects_only(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    document.elements.append(_symbol("lonely_valve", "ball_valve", tag="XV-900"))
    document.elements.append(TextElement(id="note_1", position=Point(x=0, y=0), text="NOTE"))

    graph = build_engineering_graph(document, registry)
    groups = {frozenset(group) for group in graph.connectivity_components}
    main = frozenset(
        {
            graph.object("P-101").engineering_id,
            graph.object("line:pl-1001").engineering_id,
            graph.object("junction:j-1").engineering_id,
            graph.object("P-102").engineering_id,
        }
    )
    assert main in groups
    assert frozenset({graph.object("valve:xv-900").engineering_id}) in groups
    # Annotations are not engineering topology.
    assert not any(graph.object("annotation:note_1").engineering_id in group for group in groups)


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
    assert [record.engineering_id for record in first.objects] == [
        record.engineering_id for record in second.objects
    ]
    assert first.counts.objects == len(first.objects)


def test_trace_honours_declared_flow_direction(registry: SymbolRegistry) -> None:
    graph = build_engineering_graph(_tagged_document(), registry)
    pump_a = graph.object("P-101").engineering_id
    pump_b = graph.object("P-102").engineering_id
    junction = graph.object("junction:j-1").engineering_id

    downstream = trace_engineering_object(graph, pump_a, direction="downstream")
    assert [step.engineering_id for step in downstream.steps] == [pump_a, junction, pump_b]
    assert all(step.via_connector_id for step in downstream.steps[1:])
    assert downstream.steps[1].direction == "downstream"
    assert downstream.traversed_pipeline_ids == [graph.object("line:pl-1001").engineering_id]
    assert downstream.truncated is False

    upstream = trace_engineering_object(graph, pump_a, direction="upstream")
    assert [step.engineering_id for step in upstream.steps] == [pump_a]


def test_trace_from_a_pipeline_enumerates_its_endpoints(registry: SymbolRegistry) -> None:
    graph = build_engineering_graph(_tagged_document(), registry)
    line = graph.object("line:pl-1001").engineering_id
    pump_a = graph.object("P-101").engineering_id
    pump_b = graph.object("P-102").engineering_id
    junction = graph.object("junction:j-1").engineering_id

    traced = trace_engineering_object(graph, line)
    assert traced.steps[0].engineering_id == line
    assert sorted(traced.reached_engineering_ids) == sorted([pump_a, pump_b, junction, line])
    direction_by_object = {step.engineering_id: step.direction for step in traced.steps}
    assert direction_by_object[pump_a] == "upstream"
    assert direction_by_object[junction] == "downstream"


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
    line_3003 = graph.object("line:pl-3003").engineering_id
    pump_b = graph.object("P-102").engineering_id
    pump_a = graph.object("P-101").engineering_id
    junction = graph.object("junction:j-1").engineering_id

    # Undeclared flow is traversable, but it must never be reported as a declared
    # downstream path: only the undirected endpoint is reached.
    downstream = trace_engineering_object(graph, line_3003, direction="downstream")
    assert [step.engineering_id for step in downstream.steps] == [line_3003, pump_b]
    assert downstream.steps[1].direction == "undirected"
    assert downstream.steps[1].via_connector_id == "line_undirected"
    # An undeclared, half-dangling connection must not invent a downstream path.
    downstream_node = trace_engineering_object(graph, pump_b, direction="downstream")
    assert [step.engineering_id for step in downstream_node.steps] == [pump_b]

    # Asking for both directions still reaches the whole connected group.
    both = trace_engineering_object(graph, pump_b)
    assert sorted(both.reached_engineering_ids) == sorted([pump_a, pump_b, junction])
    assert both.traversed_pipeline_ids == [graph.object("line:pl-1001").engineering_id]


def test_trace_rejects_unknown_object_and_respects_max_steps(registry: SymbolRegistry) -> None:
    graph = build_engineering_graph(_tagged_document(), registry)
    with pytest.raises(KeyError):
        trace_engineering_object(graph, "equipment:does-not-exist")

    limited = trace_engineering_object(graph, graph.object("P-101").engineering_id, max_steps=2)
    assert len(limited.steps) == 2
    assert limited.truncated is True


def test_graphics_and_annotations_remain_addressable(registry: SymbolRegistry) -> None:
    document = _tagged_document()
    document.elements.append(TextElement(id="note_1", position=Point(x=0, y=0), text="LINE NOTE"))

    graph = build_engineering_graph(document, registry)
    note = graph.object("annotation:note_1")
    assert note is not None
    assert note.label == "LINE NOTE"
    assert note.identity_basis == "element"
    assert graph.counts.annotations == 1
    assert graph.counts.graphics == 0
