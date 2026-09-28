"""M8-Q1R G7 root-cause: route parity between the canonical layout and the staged document.

DEV-2 shape (tank -> pump -> heat exchanger, two connections) currently ends in
``MaterializationDocumentError: ... routed differently ...`` on main @ 1afeccab.
This test drives the same frozen chain the text-plan endpoint uses
(:func:`agentcad.api_semantic_agent._finalize_spec_layout`), materializes it, and
compares four point sets per connector to locate the first divergence:

1. canonical layout row points (what the engine decided),
2. materialized operation points (what the writer was asked to add),
3. normalized source/target port points (what ``service._normalize_connector``
   recomputed from the staged symbol geometry),
4. staged/committed document points (what actually landed).

The final assertion is the Q1R acceptance: staged points == canonical layout points
(route parity), so this file is both the diagnostic and the regression gate.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from agentcad.api_semantic_agent import _finalize_spec_layout
from agentcad.audit import AuditContext
from agentcad.m7_diagram_spec import (
    DiagramConnection,
    DiagramEntity,
    DiagramSpec,
    DiagramSystem,
)
from agentcad.m7_layout_materialization import (
    MaterializationDocumentError,
    apply_materialized_layout,
    materialization_matches_document,
    materialization_provenance,
    materialize_canonical_layout,
    materialized_transaction,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry
from test_m7_layout_materialization import make_service, seed_document


def dev2_spec() -> DiagramSpec:
    """The restated DEV-2 sentence as a specification: concrete ports, no ambiguity."""

    return DiagramSpec(
        label="DEV-2 restated",
        systems=[DiagramSystem(system_id="S_main", name="主工艺系统", order=0)],
        entities=[
            DiagramEntity(
                engineering_id="el_V_101",
                kind="equipment",
                system_id="S_main",
                tag="V-101",
                name="缓冲罐",
                equipment_class="tank",
                symbol_key="buffer_tank",
            ),
            DiagramEntity(
                engineering_id="el_P_101",
                kind="equipment",
                system_id="S_main",
                tag="P-101",
                name="离心泵",
                equipment_class="pump",
                symbol_key="centrifugal_pump",
            ),
            DiagramEntity(
                engineering_id="el_E_101",
                kind="equipment",
                system_id="S_main",
                tag="E-101",
                name="管壳式换热器",
                equipment_class="heat_exchanger",
                symbol_key="heat_exchanger",
            ),
        ],
        connections=[
            DiagramConnection(
                engineering_id="cn_1",
                source_engineering_id="el_V_101",
                source_port_id="out",
                target_engineering_id="el_P_101",
                target_port_id="suction",
                medium="process",
            ),
            DiagramConnection(
                engineering_id="cn_2",
                source_engineering_id="el_P_101",
                source_port_id="discharge",
                target_engineering_id="el_E_101",
                target_port_id="tube_in",
                medium="process",
            ),
        ],
    )


def _layout_row_points(layout, engineering_id: str) -> list[tuple[float, float]]:
    for row in layout.rows:
        if row["kind"] == "connector" and str(row["engineering_id"]) == engineering_id:
            return [
                (float(row["x"]), float(row["y"])),
                *[(float(p[0]), float(p[1])) for p in row["waypoints"]],
            ]
    raise AssertionError(f"no connector row for {engineering_id}")


def _op_points(layout, element_id: str) -> list[tuple[float, float]]:
    for operation in layout.operations:
        element = getattr(operation, "element", None)
        if element is not None and getattr(element, "id", "") == element_id:
            return [(float(p.x), float(p.y)) for p in element.points]
    raise AssertionError(f"no operation for {element_id}")


def _fmt(points: list[tuple[float, float]]) -> str:
    return " -> ".join(f"({x:g},{y:g})" for x, y in points)


def test_route_parity_dev2_shape(tmp_path: Path) -> None:
    service = DocumentService(SQLiteDocumentStore(tmp_path / "q1r.db"), SymbolRegistry())
    finalized = _finalize_spec_layout(dev2_spec())
    layout = materialize_canonical_layout(finalized, document_id="doc_q1r")
    document_id = seed_document(service, layout)

    # Inline the commit path (apply_materialized_layout) so the staged document stays
    # observable even when reconciliation fails -- the production helper raises before
    # returning anything, which is exactly the observability gap this test exists to close.
    current = service.get_document(document_id)
    assert current.revision == 0
    request = materialized_transaction(replace(layout, document_id=document_id), expected_revision=0)
    result = service.apply_transaction(
        document_id,
        request,
        source="system",
        audit=AuditContext(
            actor="m8-q1r-test",
            surface="internal",
            tool_name="m7_materialize_layout",
            validation_status="valid",
            metadata=dict(materialization_provenance(layout)),
        ),
    )
    document = result.document

    connectors = {
        element.metadata.get("engineering_id"): element
        for element in document.elements
        if element.type == "connector"
    }
    report_lines: list[str] = []
    for engineering_id in ("cn_1", "cn_2"):
        row_points = _layout_row_points(layout, engineering_id)
        op = _op_points(layout, f"el_connector_{engineering_id}")
        staged = connectors[engineering_id]
        staged_points = [(float(p.x), float(p.y)) for p in staged.points]
        port_points = [
            (float(staged.source.point.x), float(staged.source.point.y)),
            (float(staged.target.point.x), float(staged.target.point.y)),
        ]
        report_lines.append(f"== {engineering_id}")
        report_lines.append(f"  1 canonical layout : {_fmt(row_points)}")
        report_lines.append(f"  2 materialized op  : {_fmt(op)}")
        report_lines.append(f"  3 normalized ports : src {_fmt([port_points[0]])} tgt {_fmt([port_points[1]])}")
        report_lines.append(f"  4 staged document  : {_fmt(staged_points)}")
        print("\n".join(report_lines[-5:]))

    problems = materialization_matches_document(layout, document)
    print("reconciliation problems:", problems)
    assert problems == [], (
        "route parity broken:\n" + "\n".join(report_lines) + f"\nproblems: {problems}"
    )


def test_refused_materialization_leaves_no_trace(tmp_path: Path) -> None:
    """P1 (B'): a rejected materialization must not touch the stored state at all.

    The refusal vehicle is a deliberate one-unit waypoint perturbation of an otherwise
    valid layout (the G7 fix made the natural DEV-2 shape commit cleanly, so the test
    perturbs a copy instead of relying on a live defect). The commit helper must raise
    the protocol refusal *and* the stored document, undo/redo stacks and semantic source
    must be identical to the pre-request state -- no revision, no elements, no history.
    """

    service = make_service(tmp_path)
    finalized = _finalize_spec_layout(dev2_spec())
    layout = materialize_canonical_layout(finalized, document_id="doc_q1r")
    perturbed_rows = []
    for row in layout.rows:
        if row["kind"] == "connector" and row["waypoints"]:
            waypoints = [list(point) for point in row["waypoints"]]
            waypoints[0][0] = float(waypoints[0][0]) + 1.0
            perturbed_rows.append({**row, "waypoints": waypoints})
        else:
            perturbed_rows.append(row)
    layout = replace(layout, rows=tuple(perturbed_rows))

    document_id = seed_document(service, layout)
    before = service.get_document(document_id)
    stored_before = service.store.get(document_id)
    assert stored_before is not None

    with pytest.raises(MaterializationDocumentError):
        apply_materialized_layout(
            service,
            replace(layout, document_id=document_id),
            expected_revision=0,
            audit=AuditContext(
                actor="m8-q1r-test",
                surface="internal",
                tool_name="m7_materialize_layout",
                validation_status="valid",
                metadata=dict(materialization_provenance(layout)),
            ),
        )

    after = service.get_document(document_id)
    stored_after = service.store.get(document_id)
    assert stored_after is not None
    assert after.revision == before.revision == 0
    assert after.elements == before.elements == []
    assert stored_after.undo_stack == stored_before.undo_stack == []
    assert stored_after.redo_stack == stored_before.redo_stack == []
    assert after == before
