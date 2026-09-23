"""M7-2 phase 2B step 3: materialized geometry, port bindings, routing and annotations.

The tests are grouped by the refusal each part exists to make, because that is what a reviewer
needs to see: an explicit port that does not exist, a drawing whose real sizes collide, a
connection that cannot be routed without crossing a vessel, a label that cannot find space.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_m7_diagram_adapter import fixture_a_payload

from agentcad import m7_layout_contract as contract
from agentcad.annotation_layout import text_bounds
from agentcad.auto_layout_geometry import (
    ANNOTATION_CLEARANCE,
    ANNOTATION_LABEL_GAP,
    INSTANCE_SCALE_FACTOR,
    ROUTE_STUB_LENGTH,
    AnnotationPlacementError,
    Rect,
    StepThreeError,
    UnroutableEdgeError,
    annotate_semantic_layout,
    materialize_semantic_layout,
    overlap_problems,
    placement_problems,
    route_semantic_layout,
)
from agentcad.auto_layout_semantic import (
    STEP_2,
    STEP_3,
    place_semantic_layout,
    plan_semantic_layout,
    system_rank_lanes,
)
from agentcad.m7_diagram_adapter import adapt
from agentcad.m7_endpoint_binding import (
    PortBindingError,
    bind_endpoint,
    resolve_endpoint_bindings,
)
from agentcad.m7_symbol_geometry import (
    EntityKindIncompatibleError,
    SymbolGeometryError,
    freeze_symbol_geometry,
)
from agentcad.models import Point, TextElement

SYMBOLS = ("gas_tank", "purifier", "pressure_indicator")


def fixture_a_plan():
    return place_semantic_layout(plan_semantic_layout(adapt(fixture_a_payload())))


def fixture_a_snapshot():
    return freeze_symbol_geometry(SYMBOLS)


def reflow_payload() -> dict:
    """Two tall vessels in one rank: their real heights cannot share the declared lane gap."""

    return {
        "label": "并排两个储罐",
        "systems": [{"system_id": "S1", "name": "供气", "order": 0}],
        "entities": [
            {
                "engineering_id": "el_1",
                "kind": "equipment",
                "system_id": "S1",
                "equipment_class": "purifier",
                "symbol_key": "purifier",
            },
            {
                "engineering_id": "el_2",
                "kind": "equipment",
                "system_id": "S1",
                "equipment_class": "gas_tank",
                "symbol_key": "gas_tank",
            },
            {
                "engineering_id": "el_3",
                "kind": "equipment",
                "system_id": "S1",
                "equipment_class": "gas_tank",
                "symbol_key": "gas_tank",
            },
        ],
        "connections": [
            {
                "engineering_id": "cn_a",
                "source_engineering_id": "el_1",
                "target_engineering_id": "el_2",
                "source_port_id": "out",
                "target_port_id": "in",
            },
            {
                "engineering_id": "cn_b",
                "source_engineering_id": "el_1",
                "target_engineering_id": "el_3",
                "source_port_id": "out",
                "target_port_id": "in",
            },
        ],
        "layout_intent": {"density": "compact"},
    }


# ---------------------------------------------------------------------------------------
# Port bindings: explicit must be real, omitted must be unambiguous
# ---------------------------------------------------------------------------------------


def test_an_explicit_port_resolves_and_says_so() -> None:
    snapshot = freeze_symbol_geometry(["purifier"])
    binding = bind_endpoint(
        connection_id="cn_1",
        role="source",
        node_id="el_1",
        fact=snapshot.require("purifier"),
        declared_port_id="out",
    )
    assert (binding.port_id, binding.resolution) == ("out", "explicit")
    assert binding.key == ("cn_1", "source")


def test_an_explicit_port_the_symbol_does_not_have_is_refused() -> None:
    snapshot = freeze_symbol_geometry(["purifier"])
    with pytest.raises(PortBindingError) as raised:
        bind_endpoint(
            connection_id="cn_1",
            role="source",
            node_id="el_1",
            fact=snapshot.require("purifier"),
            declared_port_id="tap",
        )
    assert raised.value.code == "port_not_found"
    assert "in" in str(raised.value)


def test_an_explicit_port_pointing_the_wrong_way_is_refused() -> None:
    snapshot = freeze_symbol_geometry(["purifier"])
    with pytest.raises(PortBindingError) as raised:
        bind_endpoint(
            connection_id="cn_1",
            role="source",
            node_id="el_1",
            fact=snapshot.require("purifier"),
            declared_port_id="in",
        )
    assert raised.value.code == "port_direction_mismatch"


def test_an_omitted_port_is_inferred_only_when_unique() -> None:
    snapshot = freeze_symbol_geometry(["purifier"])
    binding = bind_endpoint(
        connection_id="cn_1",
        role="source",
        node_id="el_1",
        fact=snapshot.require("purifier"),
        declared_port_id="",
    )
    assert (binding.port_id, binding.resolution) == ("out", "inferred_unique")


def test_an_omitted_port_with_no_candidate_is_refused() -> None:
    snapshot = freeze_symbol_geometry(["agitator"])  # declares no ports at all
    with pytest.raises(PortBindingError) as raised:
        bind_endpoint(
            connection_id="cn_1",
            role="source",
            node_id="el_1",
            fact=snapshot.require("agitator"),
            declared_port_id="",
        )
    assert raised.value.code == "no_compatible_port"


def test_an_omitted_port_with_several_candidates_is_refused() -> None:
    """The case that makes \"sort and take the first\" indefensible."""

    snapshot = freeze_symbol_geometry(["gas_tank"])
    with pytest.raises(PortBindingError) as raised:
        bind_endpoint(
            connection_id="cn_1",
            role="source",
            node_id="el_1",
            fact=snapshot.require("gas_tank"),  # out, top (bidirectional) and drain are all out-ish
            declared_port_id="",
        )
    assert raised.value.code == "ambiguous_port_binding"
    assert set(raised.value.candidates) == {"drain", "out", "top"}
    assert contract.SORTED_PORT_IDS_ARE_NOT_AN_INFERENCE_RULE is True


def test_a_bidirectional_port_satisfies_either_role() -> None:
    snapshot = freeze_symbol_geometry(["gas_tank"])
    fact = snapshot.require("gas_tank")
    for role in ("source", "target"):
        binding = bind_endpoint(
            connection_id="cn_1",
            role=role,
            node_id="el_1",
            fact=fact,
            declared_port_id="top",
        )
        assert binding.port_id == "top"


def test_the_fixture_blank_port_is_legitimate() -> None:
    """`cn_2` names no source port, and purifier admits exactly one answer."""

    plan = materialize_semantic_layout(fixture_a_plan(), fixture_a_snapshot())
    bindings = {(b.connection_id, b.role): b for b in plan.endpoint_bindings}
    assert bindings[("cn_2", "source")].resolution == "inferred_unique"
    assert bindings[("cn_2", "source")].port_id == "out"
    assert bindings[("cn_1", "source")].resolution == "explicit"


def test_bindings_are_one_per_endpoint_and_canonically_ordered() -> None:
    plan = materialize_semantic_layout(fixture_a_plan(), fixture_a_snapshot())
    keys = [binding.key for binding in plan.endpoint_bindings]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys)) == 2 * len(plan.connections)


def test_a_connection_to_an_unplaced_node_is_refused() -> None:
    plan = fixture_a_plan()
    broken = replace(
        plan,
        connections=(
            replace(plan.connections[0], target_engineering_id="el_missing"),
            *plan.connections[1:],
        ),
    )
    with pytest.raises(PortBindingError):
        materialize_semantic_layout(broken, fixture_a_snapshot())


# ---------------------------------------------------------------------------------------
# Materialization: real sizes, and the reflow only when they collide
# ---------------------------------------------------------------------------------------


def test_real_sizes_replace_the_rule_sizes() -> None:
    snapshot = fixture_a_snapshot()
    plan = materialize_semantic_layout(fixture_a_plan(), snapshot)
    sizes = {row["engineering_id"]: (row["width"], row["height"]) for row in plan.placement}
    assert sizes["el_ar_tank"] == (90.0, 140.0)
    assert sizes["el_purifier"] == (60.0, 40.0)
    assert sizes["el_pt_101"] == (50.0, 60.0)
    assert INSTANCE_SCALE_FACTOR == 1.0


def test_the_step_two_placement_is_kept_when_it_still_fits() -> None:
    plan = materialize_semantic_layout(fixture_a_plan(), fixture_a_snapshot())
    step_two = {row["engineering_id"]: (row["x"], row["y"]) for row in fixture_a_plan().placement}
    for row in plan.placement:
        assert (row["x"], row["y"]) == step_two[row["engineering_id"]]
    assert plan.placement_reflowed is False
    assert overlap_problems(plan.placement) == []


def test_real_geometry_that_collides_triggers_a_deterministic_reflow() -> None:
    snapshot = freeze_symbol_geometry(["purifier", "gas_tank"])
    plan_two = place_semantic_layout(plan_semantic_layout(adapt(reflow_payload())))
    # The step-2 coordinates were spaced for 120x80 placeholders; two 90x140 vessels cannot
    # share lanes that are 90 apart, so the initial solution is kept only if it still holds.
    laid_out = materialize_semantic_layout(plan_two, snapshot)
    assert laid_out.placement_reflowed is True
    rows = {row["engineering_id"]: row for row in laid_out.placement}
    # Both vessels kept their place in the flow and were separated along the cross axis.
    assert rows["el_2"]["x"] == rows["el_3"]["x"]
    assert rows["el_3"]["y"] - (rows["el_2"]["y"] + rows["el_2"]["height"]) >= 0
    assert overlap_problems(laid_out.placement) == []
    assert placement_problems(laid_out.placement, laid_out, laid_out.spacing) == []


def test_the_reflow_is_a_function_of_the_plan_and_the_snapshot() -> None:
    snapshot = freeze_symbol_geometry(["purifier", "gas_tank"])
    twice = [
        materialize_semantic_layout(
            place_semantic_layout(plan_semantic_layout(adapt(reflow_payload()))), snapshot
        )
        for _ in range(2)
    ]
    assert twice[0].placement == twice[1].placement
    assert twice[0].digest == twice[1].digest
    assert twice[0].placement_reflowed == twice[1].placement_reflowed is True


def test_a_symbol_the_catalogue_lacks_fails_before_anything_is_routed() -> None:
    plan = fixture_a_plan()
    payload = fixture_a_payload()
    payload["entities"][0]["symbol_key"] = "not_a_symbol"
    snapshot = fixture_a_snapshot()
    with pytest.raises(SymbolGeometryError) as raised:
        materialize_semantic_layout(
            place_semantic_layout(plan_semantic_layout(adapt(payload))), snapshot
        )
    assert raised.value.code == "symbol_geometry_missing"
    assert plan.produced_at_step == STEP_2


def test_an_instrument_bound_to_an_equipment_symbol_fails() -> None:
    payload = fixture_a_payload()
    payload["entities"][2]["symbol_key"] = "gas_tank"  # the PT-101 becomes a vessel
    with pytest.raises(EntityKindIncompatibleError):
        materialize_semantic_layout(
            place_semantic_layout(plan_semantic_layout(adapt(payload))), fixture_a_snapshot()
        )


def test_materializing_an_unplaced_plan_is_refused() -> None:
    with pytest.raises(StepThreeError):
        materialize_semantic_layout(
            plan_semantic_layout(adapt(fixture_a_payload())), fixture_a_snapshot()
        )


def test_the_materialized_plan_records_which_geometry_it_used() -> None:
    snapshot = fixture_a_snapshot()
    plan = materialize_semantic_layout(fixture_a_plan(), snapshot)
    assert plan.symbol_geometry_catalog_digest == snapshot.digest
    assert plan.symbol_geometry_closure == snapshot.closure
    assert plan.produced_at_step == STEP_3
    # A different catalogue with the same closure must not look like the same drawing.
    other = freeze_symbol_geometry((*SYMBOLS, "ball_valve"))
    assert other.digest != snapshot.digest


# ---------------------------------------------------------------------------------------
# Routing: exactly once, from frozen anchors, around everything it does not serve
# ---------------------------------------------------------------------------------------


def routed_fixture_a():
    snapshot = fixture_a_snapshot()
    return route_semantic_layout(materialize_semantic_layout(fixture_a_plan(), snapshot), snapshot)


def test_every_connection_is_routed_exactly_once() -> None:
    plan = routed_fixture_a()
    ids = [row["engineering_id"] for row in plan.routing]
    assert ids == sorted(row.connection_id for row in plan.connections)
    assert len(ids) == len(set(ids))


def test_routes_start_at_the_frozen_port_anchors() -> None:
    """The first point of a route is the instance anchor of the port the binding resolved."""

    snapshot = fixture_a_snapshot()
    plan = route_semantic_layout(materialize_semantic_layout(fixture_a_plan(), snapshot), snapshot)
    rows = {row["engineering_id"]: row for row in plan.placement}
    facts = {fact.symbol_key: fact for fact in snapshot.facts}
    for row in plan.routing:
        source = next(
            b for b in plan.endpoint_bindings if (b.connection_id, b.role) == (row["engineering_id"], "source")
        )
        placed = rows[source.node_id]
        port = facts[source.symbol_key].port(source.port_id)
        assert (row["x"], row["y"]) == (
            round(placed["x"] + port.normalized_x * placed["width"], 6),
            round(placed["y"] + port.normalized_y * placed["height"], 6),
        )


def test_routes_are_orthogonal_and_quantized() -> None:
    plan = routed_fixture_a()
    for row in plan.routing:
        points = [(row["x"], row["y"]), *[tuple(point) for point in row["ordered_waypoints"]]]
        assert len(points) >= 2
        for start, end in zip(points, points[1:], strict=False):
            assert start[0] == end[0] or start[1] == end[1]
        for x, y in points:
            assert round(x, contract.LAYOUT_COORDINATE_DECIMALS) == x
            assert round(y, contract.LAYOUT_COORDINATE_DECIMALS) == y


def test_a_route_does_not_cross_a_node_it_does_not_serve() -> None:
    plan = routed_fixture_a()
    rects = {row["engineering_id"]: Rect(row["x"], row["y"], row["width"], row["height"]) for row in plan.placement}
    for row in plan.routing:
        endpoints = {
            binding.node_id
            for binding in plan.endpoint_bindings
            if binding.connection_id == row["engineering_id"]
        }
        points = [(row["x"], row["y"]), *[tuple(point) for point in row["ordered_waypoints"]]]
        for start, end in zip(points, points[1:], strict=False):
            for node_id, rect in rects.items():
                if node_id in endpoints:
                    continue
                if abs(start[1] - end[1]) < 1e-9:
                    inside = rect.y < start[1] < rect.bottom and min(max(start[0], end[0]), rect.right) - max(min(start[0], end[0]), rect.x) > 1e-9
                else:
                    inside = rect.x < start[0] < rect.right and min(max(start[1], end[1]), rect.bottom) - max(min(start[1], end[1]), rect.y) > 1e-9
                assert not inside, (row["engineering_id"], node_id, start, end)


class _Node:
    """The binding reads three fields, so a three-field stand-in is the whole requirement."""

    def __init__(self, node_id: str, kind: str, symbol_key: str) -> None:
        self.engineering_id = node_id
        self.kind = kind
        self.symbol_key = symbol_key


def test_a_blocked_connection_is_a_hard_diagnostic_not_a_straight_line() -> None:
    """A wall between the two ends leaves no orthogonal shape at all.

    Every candidate the router is allowed to try crosses it, so the honest outcome is a refusal
    naming the connection -- the alternative would be a pipe drawn through a column.
    """

    snapshot = freeze_symbol_geometry(["purifier"])
    plan = materialize_semantic_layout(fixture_a_plan(), fixture_a_snapshot())
    nodes = (
        _Node("el_ar_tank", "equipment", "purifier"),
        _Node("el_purifier", "equipment", "purifier"),
        _Node("el_wall", "equipment", "purifier"),
    )
    walled = replace(
        plan,
        placement=(
            {"placement_kind": "equipment", "engineering_id": "el_ar_tank", "x": 0.0, "y": 0.0, "width": 60.0, "height": 40.0},
            {"placement_kind": "equipment", "engineering_id": "el_purifier", "x": 400.0, "y": 0.0, "width": 60.0, "height": 40.0},
            {"placement_kind": "equipment", "engineering_id": "el_wall", "x": 100.0, "y": -500.0, "width": 300.0, "height": 1000.0},
        ),
        node_symbols=tuple((node.engineering_id, node.symbol_key) for node in nodes),
        node_kinds=tuple((node.engineering_id, node.kind) for node in nodes),
        symbol_geometry_catalog_digest=snapshot.digest,
        connections=(
            replace(
                plan.connections[0],
                source_engineering_id="el_ar_tank",
                target_engineering_id="el_purifier",
                source_port_id="out",
                target_port_id="in",
            ),
        ),
    )
    walled = replace(
        walled,
        endpoint_bindings=resolve_endpoint_bindings(
            connections=walled.connections, nodes=nodes, snapshot=snapshot
        ),
    )
    with pytest.raises(UnroutableEdgeError) as raised:
        route_semantic_layout(walled, snapshot)
    assert raised.value.code == "no_orthogonal_route"
    assert "el_ar_tank" in str(raised.value)


def test_routing_refuses_a_snapshot_it_was_not_materialized_against() -> None:
    snapshot = fixture_a_snapshot()
    plan = materialize_semantic_layout(fixture_a_plan(), snapshot)
    with pytest.raises(StepThreeError):
        route_semantic_layout(plan, freeze_symbol_geometry((*SYMBOLS, "ball_valve")))


def test_routing_needs_bindings() -> None:
    snapshot = fixture_a_snapshot()
    plan = replace(
        materialize_semantic_layout(fixture_a_plan(), snapshot), endpoint_bindings=()
    )
    with pytest.raises(StepThreeError):
        route_semantic_layout(plan, snapshot)


def test_a_route_leaves_its_port_outward() -> None:
    """The stub is a declared rule, and the first step of a route goes away from the symbol."""

    assert ROUTE_STUB_LENGTH > 0
    snapshot = fixture_a_snapshot()
    plan = routed_fixture_a()
    rows = {row["engineering_id"]: row for row in plan.placement}
    facts = {fact.symbol_key: fact for fact in snapshot.facts}
    for row in plan.routing:
        points = [(row["x"], row["y"]), *[tuple(point) for point in row["ordered_waypoints"]]]
        anchor, following = points[0], points[1]
        source = next(
            binding
            for binding in plan.endpoint_bindings
            if binding.key == (row["engineering_id"], "source")
        )
        port = facts[source.symbol_key].port(source.port_id)
        placed = rows[source.node_id]
        if port.normalized_x == 0.0:
            assert following[0] <= anchor[0]
        elif port.normalized_x == 1.0:
            assert following[0] >= anchor[0]
        elif port.normalized_y == 0.0:
            assert following[1] <= anchor[1]
        elif port.normalized_y == 1.0:
            assert following[1] >= anchor[1]
        assert placed["width"] > 0


# ---------------------------------------------------------------------------------------
# Annotations: deterministic, from the pure text rule, and bounded
# ---------------------------------------------------------------------------------------


def annotated_fixture_a(labels: dict[str, str] | None = None):
    plan = routed_fixture_a()
    return annotate_semantic_layout(
        plan,
        labels
        or {
            "el_ar_tank": "V-101",
            "el_purifier": "X-201",
            "el_pt_101": "PT-101",
            "el_recycle": "X-301",
        },
    )


def test_annotations_are_deterministic_and_ordered() -> None:
    first = annotated_fixture_a()
    second = annotated_fixture_a()
    assert first.annotations == second.annotations
    assert [row["engineering_id"] for row in first.annotations] == sorted(
        row["engineering_id"] for row in first.annotations
    )


def test_annotation_extents_come_from_the_pure_text_rule() -> None:
    plan = annotated_fixture_a()
    node = {row["engineering_id"]: row for row in plan.placement}["el_ar_tank"]
    row = next(item for item in plan.annotations if item["engineering_id"] == "el_ar_tank")
    extent = text_bounds(
        TextElement(id="el_ar_tank", position=Point(x=0.0, y=0.0), text="V-101", font_size=12.0)
    )
    assert row["width"] == pytest.approx(extent.x2 - extent.x1)
    assert row["height"] == pytest.approx(extent.y2 - extent.y1)
    assert row["y"] == pytest.approx(node["y"] + node["height"] + ANNOTATION_LABEL_GAP)


def test_annotations_do_not_overlap_each_other_or_the_nodes() -> None:
    plan = annotated_fixture_a()
    boxes = [
        Rect(row["x"], row["y"], row["width"], row["height"]) for row in plan.annotations
    ]
    nodes = [
        Rect(row["x"], row["y"], row["width"], row["height"]) for row in plan.placement
    ]
    for index, box in enumerate(boxes):
        for other in boxes[index + 1 :]:
            assert not box.crosses(other)
        for node in nodes:
            assert not box.crosses(node)
    assert ANNOTATION_CLEARANCE >= 0

def test_two_labels_that_would_collide_are_separated() -> None:
    """Without this, the clearance rule could be deleted without a single test noticing.

    Two long labels under two close neighbours: the second one has to move, and the amount it
    moves by is the declared clearance rather than whatever looks alright.
    """

    plan = routed_fixture_a()
    close = replace(
        plan,
        placement=(
            {"placement_kind": "equipment", "engineering_id": "el_ar_tank", "x": 0.0, "y": 0.0, "width": 60.0, "height": 40.0},
            {"placement_kind": "equipment", "engineering_id": "el_purifier", "x": 61.0, "y": 0.0, "width": 60.0, "height": 40.0},
        ),
    )
    labels = {"el_ar_tank": "覆盖气回用净化器", "el_purifier": "覆盖气回用净化器"}
    annotated = annotate_semantic_layout(close, labels)
    boxes = {
        row["engineering_id"]: Rect(row["x"], row["y"], row["width"], row["height"])
        for row in annotated.annotations
    }
    first, second = boxes["el_ar_tank"], boxes["el_purifier"]
    assert first.crosses(second) is False
    assert second.y - first.bottom >= ANNOTATION_CLEARANCE - 1e-9


def test_a_node_without_a_label_gets_no_annotation() -> None:
    plan = annotate_semantic_layout(routed_fixture_a(), {"el_ar_tank": "V-101"})
    assert [row["engineering_id"] for row in plan.annotations] == ["el_ar_tank"]


def test_a_label_that_cannot_find_space_is_a_diagnostic() -> None:
    """A node that covers everything below it leaves the label nowhere to go.

    The search is bounded on purpose: a placement rule that keeps pushing until it succeeds is a
    rule that can hang, and the honest outcome is a refusal naming the label.
    """

    plan = routed_fixture_a()
    stacked = replace(
        plan,
        placement=(
            {"placement_kind": "equipment", "engineering_id": "el_ar_tank", "x": 0.0, "y": 0.0, "width": 300.0, "height": 80.0},
            {"placement_kind": "equipment", "engineering_id": "el_purifier", "x": 0.0, "y": 0.0, "width": 300.0, "height": 100000.0},
        ),
    )
    with pytest.raises(AnnotationPlacementError) as raised:
        annotate_semantic_layout(stacked, {"el_ar_tank": "V-101", "el_purifier": "X-201"})
    assert raised.value.code == "annotation_placement_failed"


# ---------------------------------------------------------------------------------------
# Binding the pieces to the contract and the task book
# ---------------------------------------------------------------------------------------


def test_the_whole_chain_runs_through_the_one_layout_authority() -> None:
    """Step 1, step 2 and step 3 all land on the engine, not beside it."""

    from agentcad.auto_layout import AutoLayoutEngine as BaseAutoLayoutEngine
    from agentcad.auto_layout_engine import AutoLayoutEngine

    engine = AutoLayoutEngine.__new__(AutoLayoutEngine)
    assert hasattr(BaseAutoLayoutEngine, "layout_semantic_geometry")
    assert (
        AutoLayoutEngine.layout_semantic_geometry
        is BaseAutoLayoutEngine.layout_semantic_geometry
    )
    snapshot = fixture_a_snapshot()
    plan = place_semantic_layout(engine.layout_semantic_topology(adapt(fixture_a_payload())))
    final = engine.layout_semantic_geometry(plan, snapshot, {"el_ar_tank": "V-101"})
    assert final.produced_at_step == STEP_3
    assert len(final.routing) == len(final.connections)
    assert len(final.endpoint_bindings) == 2 * len(final.connections)
    assert [row["engineering_id"] for row in final.annotations] == ["el_ar_tank"]


def test_the_contract_declares_step_three_and_stays_coherent() -> None:
    assert contract.validate_contract() == []
    assert contract.PHASE_2B_STEPS[2][1] == "orthogonal_routing_and_annotation_placement"
    assert "auto_layout_geometry.py" in contract.PHASE_2B_MAY_IMPORT_THE_CONTRACT
    assert "m7_endpoint_binding.py" in contract.PHASE_2B_MAY_IMPORT_THE_CONTRACT


def test_a_step_two_plan_that_was_never_placed_cannot_be_routed() -> None:
    snapshot = fixture_a_snapshot()
    plan = plan_semantic_layout(adapt(fixture_a_payload()))
    with pytest.raises(StepThreeError):
        route_semantic_layout(plan, snapshot)


def test_the_recorded_rank_lane_decomposition_is_shared_with_step_two() -> None:
    """Step 3 asks the same question step 2 asked, so it must not answer it a second time."""

    rows = system_rank_lanes(("a", "b", "c"), (("a", "b"), ("b", "c")))
    assert rows == (("a", 0, 0), ("b", 1, 0), ("c", 2, 0))
