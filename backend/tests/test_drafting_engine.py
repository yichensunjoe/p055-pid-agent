"""Deterministic drafting engine tests (M3).

These tests pin the promises a drafting pass makes to an engineer:

* it is **preview-only** — no write happens without the governed transaction channel;
* it is **reproducible** — the same drawing content always produces the same
  transaction digest, regardless of element storage order;
* it **never changes engineering connectivity** — pipes may be re-routed, never re-bound;
* it **honours every lock** — element metadata, declared lock regions, and per-run locks;
* it **stays in scope** — a regional repair cannot touch geometry outside its region;
* it **never makes the drawing worse** — the monotone gate rejects a damaging stage;
* it **reports what it cannot fix** instead of guessing (crossings, junctions, ports,
  reserved drawing space).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcad.drafting_engine import DraftingEngine
from agentcad.drafting_models import (
    DRAFTING_ENGINE_VERSION,
    DraftingPolicy,
    DraftingRequest,
)
from agentcad.layout_models import LayoutRegion
from agentcad.models import (
    AddElementOperation,
    ConnectorElement,
    ConnectorEndpoint,
    CreateDocumentRequest,
    Document,
    JunctionElement,
    Point,
    SymbolElement,
    TextElement,
    TransactionRequest,
    UpdateElementOperation,
)
from agentcad.service import DocumentService, RevisionConflictError
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry

PUMP = "centrifugal_pump"
VALVE = "gate_valve"


def make_service(tmp_path: Path) -> DocumentService:
    return DocumentService(
        SQLiteDocumentStore(tmp_path / "drafting-engine.db"),
        SymbolRegistry(),
    )


def pump(element_id: str, x: float, y: float, *, label: str = "") -> SymbolElement:
    return SymbolElement(
        id=element_id,
        symbol_key=PUMP,
        position=Point(x=x, y=y),
        width=80,
        height=70,
        label=label,
    )


def valve(element_id: str, x: float, y: float, *, label: str = "") -> SymbolElement:
    return SymbolElement(
        id=element_id,
        symbol_key=VALVE,
        position=Point(x=x, y=y),
        width=60,
        height=50,
        label=label,
    )


def seed(service: DocumentService, elements: list, *, name: str = "Drafting", metadata=None) -> str:
    """Create a document and add every element in one governed transaction."""

    document = service.create_document(
        CreateDocumentRequest(name=name, metadata=metadata or {})
    )
    service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=0,
            label="Seed drafting fixture",
            operations=[AddElementOperation(element=element) for element in elements],
        ),
    )
    return document.id


def add(service: DocumentService, document_id: str, elements: list) -> Document:
    current = service.get_document(document_id)
    return service.apply_transaction(
        document_id,
        TransactionRequest(
            expected_revision=current.revision,
            label="Extend drafting fixture",
            operations=[AddElementOperation(element=element) for element in elements],
        ),
    ).document


def endpoint_point(service: DocumentService, element, port_id: str) -> Point:
    """The drawing point of one endpoint: a symbol port, or a junction node."""

    if element.type == "junction":
        assert port_id == "node"
        return Point.model_validate(element.position.model_dump())
    return service._symbol_port_point(element, port_id)


def bound_pipe(
    service: DocumentService,
    document: Document,
    connector_id: str,
    source_id: str,
    source_port: str,
    target_id: str,
    target_port: str,
    *,
    points: list[Point] | None = None,
    routing: str = "orthogonal",
    process_tag: str = "",
    flow_direction: str = "none",
    metadata: dict | None = None,
) -> ConnectorElement:
    element_map = {element.id: element for element in document.elements}
    source_point = endpoint_point(service, element_map[source_id], source_port)
    target_point = endpoint_point(service, element_map[target_id], target_port)
    return ConnectorElement(
        id=connector_id,
        points=points or [source_point, target_point],
        source=ConnectorEndpoint(
            element_id=source_id,
            port_id=source_port,
            point=source_point,
        ),
        target=ConnectorEndpoint(
            element_id=target_id,
            port_id=target_port,
            point=target_point,
        ),
        routing=routing,
        process_tag=process_tag,
        medium="process",
        flow_direction=flow_direction,
        metadata=metadata or {},
    )


def pump_to_valve(service: DocumentService, *, name: str = "Pump line") -> str:
    """One process line: pump -> valve, plus a label on the pump."""

    document_id = seed(
        service,
        [pump("pump", 100, 300, label="P-101"), valve("valve", 500, 285, label="HV-101")],
        name=name,
    )
    document = service.get_document(document_id)
    add(
        service,
        document_id,
        [bound_pipe(service, document, "pipe", "pump", "discharge", "valve", "in")],
    )
    return document_id


def positions(document: Document) -> dict[str, tuple[float, float]]:
    return {
        element.id: (element.position.x, element.position.y)
        for element in document.elements
        if element.type in {"symbol", "junction", "text"}
    }


def connector_bindings(document: Document) -> dict[str, tuple]:
    """Which port every pipe end is bound to — the engineering connectivity."""

    return {
        element.id: (
            (element.source.element_id, element.source.port_id) if element.source else None,
            (element.target.element_id, element.target.port_id) if element.target else None,
        )
        for element in document.elements
        if element.type == "connector"
    }


def endpoint_points(document: Document) -> dict[str, tuple]:
    return {
        element.id: (
            (element.source.point.x, element.source.point.y) if element.source else None,
            (element.target.point.x, element.target.point.y) if element.target else None,
        )
        for element in document.elements
        if element.type == "connector"
    }


def patch(service: DocumentService, document_id: str, operations: list) -> Document:
    current = service.get_document(document_id)
    return service.apply_transaction(
        document_id,
        TransactionRequest(
            expected_revision=current.revision,
            label="Patch drafting fixture",
            operations=operations,
        ),
    ).document


def operation_ids(preview) -> list[str]:
    if preview.transaction is None:
        return []
    return sorted(operation.element_id for operation in preview.transaction.operations)


# --------------------------------------------------------------------------- #
# Reproducibility
# --------------------------------------------------------------------------- #


def test_preview_is_deterministic_and_reports_its_engine_version(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = pump_to_valve(service)
    engine = DraftingEngine(service)

    first = engine.preview(document_id, DraftingRequest())
    second = engine.preview(document_id, DraftingRequest())

    assert first.reproducibility.transaction_digest == second.reproducibility.transaction_digest
    assert first.reproducibility.engine_version == DRAFTING_ENGINE_VERSION
    assert first.reproducibility.output_content_hash == second.reproducibility.output_content_hash
    assert first.metrics.after == second.metrics.after
    assert first.transaction is not None and second.transaction is not None
    assert [operation.model_dump(mode="json") for operation in first.transaction.operations] == [
        operation.model_dump(mode="json") for operation in second.transaction.operations
    ]
    assert first.gate == second.gate


def test_element_storage_order_does_not_change_the_result(tmp_path: Path):
    """The digest is a property of the drawing content, not of the row order."""

    service = make_service(tmp_path)
    forward_id = pump_to_valve(service, name="Forward")
    forward = service.get_document(forward_id)
    reversed_document = forward.model_copy(deep=True)
    reversed_document.elements = list(reversed(reversed_document.elements))
    engine = DraftingEngine(service)

    forward_preview = engine.preview_document(forward, DraftingRequest())
    reversed_preview = engine.preview_document(reversed_document, DraftingRequest())

    assert (
        forward_preview.reproducibility.transaction_digest
        == reversed_preview.reproducibility.transaction_digest
    )
    assert forward_preview.reproducibility.input_content_hash == (
        reversed_preview.reproducibility.input_content_hash
    )


def test_drafting_is_idempotent_and_settles(tmp_path: Path):
    """Applying a preview leaves a drawing the engine has nothing left to do on."""

    service = make_service(tmp_path)
    document_id = pump_to_valve(service)
    engine = DraftingEngine(service)
    preview = engine.preview(document_id, DraftingRequest())
    assert preview.transaction is not None
    assert preview.reproducibility.settled is False

    applied = service.apply_transaction(document_id, preview.transaction).document
    settled = engine.preview(applied.id, DraftingRequest(expected_revision=applied.revision))

    assert settled.transaction is None
    assert settled.reproducibility.settled is True
    assert settled.reproducibility.operation_count == 0
    assert settled.metrics.before == settled.metrics.after


# --------------------------------------------------------------------------- #
# Preview-only and topological safety
# --------------------------------------------------------------------------- #


def test_preview_and_report_never_write_the_document(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = pump_to_valve(service)
    before = service.get_document(document_id)
    engine = DraftingEngine(service)

    engine.report(document_id, DraftingRequest())
    engine.preview(document_id, DraftingRequest())

    after = service.get_document(document_id)
    assert after.revision == before.revision
    assert after.model_dump(mode="json") == before.model_dump(mode="json")


def test_applying_a_preview_never_changes_engineering_connectivity(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = pump_to_valve(service)
    engine = DraftingEngine(service)
    preview = engine.preview(document_id, DraftingRequest())

    before = service.get_document(document_id)
    applied = service.apply_transaction(document_id, preview.transaction).document

    assert connector_bindings(applied) == connector_bindings(before)
    # The cached endpoint coordinates follow the ports, so a pipe is still plugged
    # into the very same port after the move.
    assert endpoint_points(applied) != endpoint_points(before)
    assert sorted(element.id for element in applied.elements) == sorted(
        element.id for element in before.elements
    )
    assert sorted(element.type for element in applied.elements) == sorted(
        element.type for element in before.elements
    )
    assert all(finding.code != "DRAFT_TOPOLOGY_CHANGED" for finding in preview.findings)
    assert all(
        operation.op == "update_element" for operation in preview.transaction.operations
    )


def test_stale_expected_revision_is_rejected(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = pump_to_valve(service)
    with pytest.raises(RevisionConflictError):
        DraftingEngine(service).preview(
            document_id,
            DraftingRequest(expected_revision=99),
        )


# --------------------------------------------------------------------------- #
# Manual lock
# --------------------------------------------------------------------------- #


def test_element_metadata_lock_freezes_that_element(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = seed(
        service,
        [
            pump("pump", 100, 300, label="P-101"),
            SymbolElement(
                id="locked_pump",
                symbol_key=PUMP,
                position=Point(x=160, y=310),
                width=80,
                height=70,
                label="P-102",
                metadata={"drafting_lock": True},
            ),
        ],
    )
    preview = DraftingEngine(service).preview(document_id, DraftingRequest())

    assert "locked_pump" in preview.locked_element_ids
    assert "locked_pump" in preview.locks.metadata_element_ids
    assert "locked_pump" not in operation_ids(preview)
    assert "locked_pump" not in preview.moved_element_ids
    applied = service.apply_transaction(document_id, preview.transaction).document
    frozen = next(element for element in applied.elements if element.id == "locked_pump")
    assert frozen.position == Point(x=160, y=310)


def test_declared_lock_region_freezes_everything_inside_it(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = seed(
        service,
        [pump("pump", 100, 300, label="P-101"), valve("valve", 520, 300, label="HV-101")],
        name="Locked region",
        metadata={
            "layout_regions": [
                LayoutRegion(kind="lock", x1=80, y1=280, x2=220, y2=400, label="confirmed").model_dump(
                    mode="json"
                )
            ]
        },
    )
    preview = DraftingEngine(service).preview(document_id, DraftingRequest())

    assert "pump" in preview.locked_element_ids
    assert "pump" in preview.locks.region_element_ids
    assert preview.locks.region_labels == ["confirmed"]
    assert "pump" not in operation_ids(preview)
    assert "valve" in preview.moved_element_ids


def test_per_run_lock_is_honoured_without_touching_the_document(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = pump_to_valve(service)
    engine = DraftingEngine(service)

    locked = engine.preview(document_id, DraftingRequest(locked_element_ids=["valve"]))
    unlocked = engine.preview(document_id, DraftingRequest())

    assert locked.locks.request_element_ids == ["valve"]
    assert "valve" not in operation_ids(locked)
    assert "valve" not in locked.moved_element_ids
    assert "valve" in operation_ids(unlocked)
    assert service.get_document(document_id).metadata.get("drafting_lock") is None


def test_locked_connector_is_not_rerouted(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = seed(service, [pump("pump", 100, 300, label="P-101")])
    document = service.get_document(document_id)
    pipe = bound_pipe(
        service,
        document,
        "pipe",
        "pump",
        "discharge",
        "pump",
        "suction",
        routing="manual",
        points=[
            Point(x=148, y=300),
            Point(x=148, y=260),
            Point(x=60, y=260),
            Point(x=60, y=300),
            Point(x=100, y=300),
        ],
        metadata={"drafting_lock": True},
    )
    add(service, document_id, [pipe])

    preview = DraftingEngine(service).preview(document_id, DraftingRequest())
    assert "pipe" in preview.skipped_locked_element_ids
    assert "pipe" not in preview.rerouted_connector_ids


# --------------------------------------------------------------------------- #
# Scope and region repair
# --------------------------------------------------------------------------- #


def test_region_scope_confines_every_change_to_the_region(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = seed(
        service,
        [
            pump("near_pump", 120, 300, label="P-101"),
            valve("near_valve", 320, 300, label="HV-101"),
            pump("far_pump", 1400, 300, label="P-201"),
            valve("far_valve", 1600, 300, label="HV-201"),
        ],
        name="Two regions",
    )
    document = service.get_document(document_id)
    add(
        service,
        document_id,
        [
            bound_pipe(service, document, "near_pipe", "near_pump", "discharge", "near_valve", "in"),
            bound_pipe(service, document, "far_pipe", "far_pump", "discharge", "far_valve", "in"),
        ],
    )
    engine = DraftingEngine(service)
    region = DraftingRequest(region=LayoutRegion(x1=0, y1=0, x2=700, y2=700))
    preview = engine.preview(document_id, region)

    assert preview.transaction is not None
    touching = set(operation_ids(preview))
    assert touching <= {"near_pump", "near_valve", "near_pipe"}
    assert "far_pump" not in touching and "far_valve" not in touching
    assert all(finding.code != "DRAFT_OUT_OF_SCOPE_CHANGE" for finding in preview.findings)

    before = service.get_document(document_id)
    applied = service.apply_transaction(document_id, preview.transaction).document
    for element_id in ("far_pump", "far_valve"):
        assert positions(applied)[element_id] == positions(before)[element_id]
    far_pipe = next(element for element in applied.elements if element.id == "far_pipe")
    expected_far = next(element for element in before.elements if element.id == "far_pipe")
    assert far_pipe.points == expected_far.points
    assert connector_bindings(applied) == connector_bindings(before)


def test_selection_scope_keeps_unselected_geometry_untouched(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = seed(
        service,
        [
            pump("near_pump", 120, 300, label="P-101"),
            valve("near_valve", 320, 300, label="HV-101"),
            pump("far_pump", 1400, 300, label="P-201"),
        ],
        name="Selection",
    )
    document = service.get_document(document_id)
    add(
        service,
        document_id,
        [bound_pipe(service, document, "near_pipe", "near_pump", "discharge", "near_valve", "in")],
    )
    preview = DraftingEngine(service).preview(
        document_id,
        DraftingRequest(element_ids=["near_pump", "near_valve"]),
    )

    assert preview.in_scope_element_ids == ["near_pipe", "near_pump", "near_valve"]
    assert "far_pump" not in operation_ids(preview)


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def test_route_only_mode_tidies_routing_without_moving_symbols(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = seed(service, [pump("pump", 100, 300, label="P-101"), valve("valve", 500, 285, label="HV-101")])
    document = service.get_document(document_id)
    add(
        service,
        document_id,
        [
            bound_pipe(
                service,
                document,
                "pipe",
                "pump",
                "discharge",
                "valve",
                "in",
                routing="manual",
                points=[
                    Point(x=148, y=300),
                    Point(x=250, y=300),
                    Point(x=250, y=440),
                    Point(x=430, y=440),
                    Point(x=430, y=315),
                    Point(x=500, y=315),
                ],
            )
        ],
    )
    before = service.get_document(document_id)
    preview = DraftingEngine(service).preview(document_id, DraftingRequest(relayout=False))

    assert preview.transaction is not None
    assert "pipe" in preview.rerouted_connector_ids
    assert preview.moved_element_ids == []
    applied = service.apply_transaction(document_id, preview.transaction).document
    assert positions(applied) == positions(before)
    assert connector_bindings(applied) == connector_bindings(before)
    assert preview.metrics.after.score > preview.metrics.before.score
    assert preview.metrics.regressions == []


def test_a_route_never_gets_worse_even_when_it_cannot_be_improved(tmp_path: Path):
    """A clean drawing yields no operations rather than cosmetic churn."""

    service = make_service(tmp_path)
    document_id = pump_to_valve(service)
    engine = DraftingEngine(service)
    first = engine.preview(document_id, DraftingRequest())
    service.apply_transaction(document_id, first.transaction)
    current = service.get_document(document_id)
    second = engine.preview(current.id, DraftingRequest(expected_revision=current.revision))

    assert second.transaction is None
    assert second.metrics.regressions == []
    assert second.gate.passed


# --------------------------------------------------------------------------- #
# Crossings and junctions
# --------------------------------------------------------------------------- #


def crossing_document(service: DocumentService) -> str:
    """Two pipes that cross at (400, 235) without sharing an endpoint."""

    document_id = seed(
        service,
        [
            SymbolElement(
                id="ha",
                symbol_key="octagon_box",
                position=Point(x=100, y=200),
                width=70,
                height=70,
                label="EQ-1",
            ),
            SymbolElement(
                id="hb",
                symbol_key="octagon_box",
                position=Point(x=600, y=200),
                width=70,
                height=70,
                label="EQ-2",
            ),
            SymbolElement(
                id="va",
                symbol_key="cylinder_vessel",
                position=Point(x=365, y=20),
                width=70,
                height=100,
                label="EQ-3",
            ),
            SymbolElement(
                id="vb",
                symbol_key="cylinder_vessel",
                position=Point(x=365, y=300),
                width=70,
                height=100,
                label="EQ-4",
            ),
        ],
        name="Crossing",
    )
    document = service.get_document(document_id)
    add(
        service,
        document_id,
        [
            bound_pipe(
                service,
                document,
                "h_pipe",
                "ha",
                "out",
                "hb",
                "in",
                process_tag="PL-1",
                flow_direction="forward",
            ),
            bound_pipe(
                service,
                document,
                "v_pipe",
                "va",
                "bottom",
                "vb",
                "top",
                process_tag="PL-2",
            ),
        ],
    )
    return document_id


ROUTE_ONLY = {
    "relayout": False,
    "reroute_connectors": False,
    "place_annotations": False,
    "resolve_collisions": False,
}


def test_a_crossing_gets_exactly_one_bridge_on_the_secondary_line(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = crossing_document(service)
    preview = DraftingEngine(service).preview(
        document_id,
        DraftingRequest(bridge_crossings=True, **ROUTE_ONLY),
    )

    assert [crossing.bridged_by for crossing in preview.crossings] == ["v_pipe"]
    assert preview.transaction is not None
    assert preview.bridged_connector_ids == ["v_pipe"]
    assert [(operation.element_id, operation.patch) for operation in preview.transaction.operations] == [
        ("v_pipe", {"crossing_style": "jump"})
    ]
    applied = service.apply_transaction(document_id, preview.transaction).document
    styles = {
        element.id: element.crossing_style
        for element in applied.elements
        if element.type == "connector"
    }
    assert styles == {"h_pipe": "none", "v_pipe": "jump"}
    # The primary (declared-flow) line stays straight through the crossing.
    h_pipe = next(element for element in applied.elements if element.id == "h_pipe")
    assert len(h_pipe.points) == 2


def test_a_double_bridge_is_reduced_to_one(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = crossing_document(service)
    engine = DraftingEngine(service)
    patched = patch(
        service,
        document_id,
        [
            UpdateElementOperation(element_id="h_pipe", patch={"crossing_style": "jump"}),
            UpdateElementOperation(element_id="v_pipe", patch={"crossing_style": "jump"}),
        ],
    )
    assert patched is not None
    assert {
        crossing.bridged_by
        for crossing in engine.report(document_id, DraftingRequest()).crossings
    } == {"both"}
    preview = DraftingEngine(service).preview(
        document_id,
        DraftingRequest(bridge_crossings=True, **ROUTE_ONLY),
    )

    assert preview.transaction is not None
    assert [(operation.element_id, operation.patch) for operation in preview.transaction.operations] == [
        ("h_pipe", {"crossing_style": "none"})
    ]
    # The double bridge is gone from the result, so it is no longer a finding against it.
    assert [crossing.bridged_by for crossing in preview.crossings] == ["v_pipe"]
    assert all(
        finding.code != "DRAFT_CROSSING_DOUBLE_BRIDGED" for finding in preview.findings
    )


def test_a_locked_secondary_line_turns_a_crossing_into_a_reported_warning(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = crossing_document(service)
    patch(
        service,
        document_id,
        [UpdateElementOperation(element_id="v_pipe", patch={"metadata": {"drafting_lock": True}})],
    )
    preview = DraftingEngine(service).preview(
        document_id,
        DraftingRequest(bridge_crossings=True, **ROUTE_ONLY),
    )

    assert preview.transaction is None
    unbridged = [finding for finding in preview.findings if finding.code == "DRAFT_CROSSING_UNBRIDGED"]
    locked = [
        finding
        for finding in preview.findings
        if finding.code == "DRAFT_CROSSING_UNBRIDGED_LOCKED"
    ]
    assert len(unbridged) == 1
    assert set(unbridged[0].element_ids) == {"h_pipe", "v_pipe"}
    assert len(locked) == 1 and locked[0].severity == "warning"
    assert locked[0].waived is False
    assert preview.gate.passed is False
    assert "DRAFT_CROSSING_UNBRIDGED" in {finding.code for finding in preview.gate.blockers}
    # A blocker the engine could not fix was reported, never silently "resolved".
    assert [crossing.bridged for crossing in preview.crossings] == [False]


def test_a_dangling_junction_is_reported_and_the_engine_never_deletes_topology(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = seed(
        service,
        [
            pump("pump", 100, 300, label="P-101"),
            JunctionElement(id="junction", position=Point(x=300, y=335)),
        ],
        name="Dangling junction",
    )
    document = service.get_document(document_id)
    add(
        service,
        document_id,
        [
            bound_pipe(
                service,
                document,
                "stub",
                "pump",
                "discharge",
                "junction",
                "node",
                routing="manual",
                points=[Point(x=148, y=300), Point(x=148, y=335), Point(x=300, y=335)],
            )
        ],
    )
    preview = DraftingEngine(service).preview(document_id, DraftingRequest(relayout=False))

    report = DraftingEngine(service).report(document_id, DraftingRequest())
    assert [(j.element_id, j.degree, j.kind) for j in report.junctions] == [
        ("junction", 1, "dangling")
    ]
    assert any(finding.code == "DRAFT_JUNCTION_DANGLING" for finding in preview.findings)
    assert all(
        operation.op == "update_element" for operation in (preview.transaction.operations if preview.transaction else [])
    )
    applied = service.apply_transaction(document_id, preview.transaction).document if preview.transaction else service.get_document(document_id)
    assert sorted(element.id for element in applied.elements) == [
        "junction",
        "pump",
        "stub",
    ]


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #


def test_gate_blocks_an_unresolved_port_binding_and_records_a_visible_waiver(tmp_path: Path):
    """A report on a document whose port binding is broken must say so, loudly."""

    service = make_service(tmp_path)
    document_id = seed(service, [pump("pump", 100, 300, label="P-101")], name="Broken port")
    document = service.get_document(document_id)
    document.elements.append(
        ConnectorElement(
            id="broken",
            points=[Point(x=148, y=300), Point(x=148, y=200)],
            source=ConnectorEndpoint(
                element_id="pump",
                port_id="not_a_real_port",
                point=Point(x=148, y=300),
            ),
            target=None,
            routing="manual",
        )
    )
    engine = DraftingEngine(service)

    report = engine.report_document(document, DraftingRequest())
    unresolved = [finding for finding in report.findings if finding.code == "DRAFT_PORT_UNRESOLVED"]
    assert unresolved and unresolved[0].waived is False
    assert report.gate.passed is False
    assert "DRAFT_PORT_UNRESOLVED" in {finding.code for finding in report.gate.blockers}

    waived = engine.report_document(
        document,
        DraftingRequest(policy=DraftingPolicy(waived_codes=["DRAFT_PORT_UNRESOLVED"])),
    )
    marked = [finding for finding in waived.findings if finding.code == "DRAFT_PORT_UNRESOLVED"]
    assert marked and marked[0].waived is True
    assert waived.gate.blockers == []
    assert waived.gate.waived_codes == ["DRAFT_PORT_UNRESOLVED"]


def test_gate_fails_below_the_policy_score_target(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = pumping_mess(service)
    engine = DraftingEngine(service)

    running = engine.preview(document_id, DraftingRequest())
    assert running.gate.passed is True

    # The same drawing measured before a repair cannot satisfy a perfect-score policy.
    strict = engine.report(
        document_id, DraftingRequest(policy=DraftingPolicy(target_score=100))
    )
    assert strict.score < 100
    assert strict.gate.passed is False
    assert strict.gate.target_score == 100


def pumping_mess(service: DocumentService) -> str:
    """Two overlapping pumps plus a pipe with a pointless detour."""

    document_id = seed(
        service,
        [pump("pump_a", 100, 300, label="P-101"), pump("pump_b", 150, 320, label="P-102")],
        name="Mess",
    )
    document = service.get_document(document_id)
    add(
        service,
        document_id,
        [
            bound_pipe(
                service,
                document,
                "pipe",
                "pump_a",
                "discharge",
                "pump_b",
                "suction",
                routing="manual",
                points=[
                    Point(x=148, y=300),
                    Point(x=180, y=300),
                    Point(x=180, y=600),
                    Point(x=40, y=600),
                    Point(x=40, y=350),
                    Point(x=110, y=350),
                ],
            )
        ],
    )
    return document_id


def test_overlapping_nodes_are_separated_monotonically(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = pumping_mess(service)
    preview = DraftingEngine(service).preview(document_id, DraftingRequest())

    assert preview.metrics.before.node_overlaps >= 1
    assert preview.metrics.after.node_overlaps == 0
    assert preview.metrics.regressions == []
    assert preview.gate.passed is True
    applied = service.apply_transaction(document_id, preview.transaction).document
    a = next(element for element in applied.elements if element.id == "pump_a")
    b = next(element for element in applied.elements if element.id == "pump_b")
    assert abs(a.position.x - b.position.x) >= a.width or abs(a.position.y - b.position.y) >= a.height


# --------------------------------------------------------------------------- #
# Reserved drawing space and annotations
# --------------------------------------------------------------------------- #


def test_reserved_region_intrusion_is_reported_and_routes_are_planned_around_it(tmp_path: Path):
    """A pipe through the legend is reported, and the reroute goes *around* it."""

    service = make_service(tmp_path)
    legend = LayoutRegion(kind="legend", x1=200, y1=296, x2=420, y2=420, label="图例")
    document_id = seed(
        service,
        [pump("pump", 100, 300, label="P-101"), valve("valve", 520, 300, label="HV-101")],
        name="Legend",
        metadata={"layout_regions": [legend.model_dump(mode="json")]},
    )
    document = service.get_document(document_id)
    add(
        service,
        document_id,
        [
            bound_pipe(
                service,
                document,
                "pipe",
                "pump",
                "discharge",
                "valve",
                "in",
                routing="manual",
                points=[
                    Point(x=148, y=300),
                    Point(x=300, y=300),
                    Point(x=300, y=290),
                    Point(x=500, y=290),
                    Point(x=500, y=315),
                ],
                process_tag="PL-1001",
            )
        ],
    )
    engine = DraftingEngine(service)
    report = engine.report(document_id, DraftingRequest())
    intrusions = [
        finding for finding in report.findings if finding.code == "DRAFT_RESERVED_REGION_OVERLAP"
    ]
    assert intrusions and "pipe" in intrusions[0].element_ids
    assert intrusions[0].details["region_kind"] == "legend"
    # Reserved space is not a lock: nothing inside it is frozen, it is merely off limits.
    assert report.locks.locked_element_ids == []

    preview = engine.preview(document_id, DraftingRequest(relayout=False))
    assert preview.metrics.before.reserved_region_intrusions >= 1
    assert preview.metrics.after.reserved_region_intrusions == 0
    assert preview.metrics.regressions == []
    before = service.get_document(document_id)
    applied = service.apply_transaction(document_id, preview.transaction).document
    routed = next(element for element in applied.elements if element.id == "pipe")
    assert all(
        not (200 <= point.x <= 420 and 296 <= point.y <= 420) for point in routed.points
    )
    assert connector_bindings(applied) == connector_bindings(before)


def test_an_unlocked_symbol_drawn_on_the_legend_is_moved_out(tmp_path: Path):
    """A device parked on the legend is a defect, so drafting evicts it.

    Overlap relaxation cannot see this case: the obstacle is a declared zone, not
    another node, and the symbol overlaps nothing. The eviction is a deterministic
    move, and the pipe bound to the moved device follows it in the same run.
    """

    service = make_service(tmp_path)
    legend = LayoutRegion(kind="legend", x1=200, y1=296, x2=420, y2=420, label="图例")
    document_id = seed(
        service,
        [pump("pump", 260, 330, label="P-101"), valve("valve", 520, 300, label="HV-101")],
        name="Legend eviction",
        metadata={"layout_regions": [legend.model_dump(mode="json")]},
    )
    document = service.get_document(document_id)
    add(
        service,
        document_id,
        [bound_pipe(service, document, "pipe", "pump", "discharge", "valve", "in")],
    )
    engine = DraftingEngine(service)
    # Route-only mode: stage 1 is off, so only the drafting passes can be responsible.
    preview = engine.preview(document_id, DraftingRequest(relayout=False))

    assert preview.metrics.before.reserved_region_intrusions >= 1
    assert "pump" in preview.moved_element_ids
    assert preview.metrics.after.reserved_region_intrusions == 0
    assert [finding.code for finding in preview.gate.blockers] == []
    assert preview.metrics.regressions == []

    before = service.get_document(document_id)
    applied = service.apply_transaction(document_id, preview.transaction).document
    moved = next(element for element in applied.elements if element.id == "pump")
    assert not (200 <= moved.position.x <= 420 and 296 <= moved.position.y <= 420)
    # The pipe is re-bound to the same port on a device that moved: connectivity, not
    # geometry, is what must survive.
    assert connector_bindings(applied) == connector_bindings(before)
    settled = engine.preview(applied.id, DraftingRequest(relayout=False))
    assert settled.settled and settled.transaction is None


def test_a_locked_symbol_on_the_legend_is_reported_instead_of_moved(tmp_path: Path):
    """A lock wins over tidiness, and the residue is admitted as a blocker."""

    service = make_service(tmp_path)
    legend = LayoutRegion(kind="legend", x1=200, y1=296, x2=420, y2=420, label="图例")
    document_id = seed(
        service,
        [
            pump(
                "pump",
                260,
                330,
                label="P-101",
            ).model_copy(update={"metadata": {"drafting_lock": True}}),
            valve("valve", 520, 300, label="HV-101"),
        ],
        name="Locked on the legend",
        metadata={"layout_regions": [legend.model_dump(mode="json")]},
    )
    document = service.get_document(document_id)
    add(
        service,
        document_id,
        [bound_pipe(service, document, "pipe", "pump", "discharge", "valve", "in")],
    )
    engine = DraftingEngine(service)
    preview = engine.preview(document_id, DraftingRequest(relayout=False))

    assert preview.locks.metadata_element_ids == ["pump"]
    assert "pump" not in preview.moved_element_ids
    assert all(operation.element_id != "pump" for operation in preview.transaction.operations)
    assert preview.metrics.after.reserved_region_intrusions == preview.metrics.before.reserved_region_intrusions
    covered = sorted(
        {
            element_id
            for finding in preview.gate.blockers
            if finding.code == "DRAFT_RESERVED_REGION_OVERLAP"
            for element_id in finding.element_ids
        }
    )
    # The locked device stays where the engineer froze it, and the pipe bound to it
    # cannot avoid the zone either: both are reported, neither is silently rewritten.
    assert covered == ["pipe", "pump"]
    assert preview.gate.passed is False


def test_a_label_that_sits_on_top_of_a_symbol_is_moved_off(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = seed(
        service,
        [
            pump("pump", 100, 300, label="P-101"),
            TextElement(
                id="label_text",
                position=Point(x=105, y=340),
                text="P-101",
                metadata={"parent_element_id": "pump"},
            ),
        ],
        name="Label overlap",
    )
    engine = DraftingEngine(service)
    before = engine.report(document_id, DraftingRequest())
    assert (before.collisions and any(
        finding.code == "DRAFT_TEXT_SYMBOL_OVERLAP" for finding in before.collisions
    )) or not before.gate.passed

    preview = engine.preview(document_id, DraftingRequest(relayout=False))
    if preview.transaction is None:
        return
    applied = service.apply_transaction(document_id, preview.transaction).document
    moved = next(element for element in applied.elements if element.id == "label_text")
    assert moved.position != Point(x=105, y=340)
    assert moved.text == "P-101"


# --------------------------------------------------------------------------- #
# Report surface
# --------------------------------------------------------------------------- #


def test_report_lists_addressable_ports_and_is_canonical(tmp_path: Path):
    service = make_service(tmp_path)
    document_id = pump_to_valve(service)
    engine = DraftingEngine(service)

    report = engine.report(document_id, DraftingRequest())
    ports = {(port.element_id, port.port_id): port for port in report.ports}

    assert ("pump", "discharge") in ports
    assert ports[("pump", "discharge")].direction == "out"
    assert ports[("pump", "discharge")].side == "top"
    assert ports[("valve", "in")].direction == "in"
    assert ports[("pump", "discharge")].connector_ids == ["pipe"]
    assert report.content_hash == report.input_content_hash
    assert report.engine_version == DRAFTING_ENGINE_VERSION
    assert report.scope_kind == "document"
    assert report.gate.checked_codes


def test_hard_metrics_are_never_allowed_to_get_worse(tmp_path: Path):
    """Every accepted stage must leave every hard drafting metric at or above par."""

    from agentcad.drafting_geometry import hard_regressions

    service = make_service(tmp_path)
    for name, builder in (("mess", pumping_mess), ("crossing", crossing_document)):
        document_id = builder(service) if name == "crossing" else builder(service)
        preview = DraftingEngine(service).preview(document_id, DraftingRequest())
        assert preview.metrics.regressions == []
        assert hard_regressions(preview.metrics.before, preview.metrics.after) == []
        assert preview.metrics.after.score >= preview.metrics.before.score
        for field, value in preview.metrics.after.hard_signature():
            assert value <= dict(preview.metrics.before.hard_signature())[field]
