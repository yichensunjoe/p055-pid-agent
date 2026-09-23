"""M7-2 phase 2B step 4: the content envelope, the derived canvas, and the final geometry checks.

The tests are grouped by the failure each part exists to prevent: a canvas measured over a subset
of the drawing (which crops), a canvas that reaches its aspect ratio by shrinking, a route that
crosses a symbol it merely happens to end on, and a caller who hands the engine pixels.
"""

from __future__ import annotations

import inspect
from dataclasses import replace

import pytest
from test_m7_diagram_adapter import fixture_a_payload
from test_m7_step3_geometry import annotated_fixture_a, fixture_a_snapshot

from agentcad import m7_layout_contract as contract
from agentcad.auto_layout_canvas import (
    ASPECT_CLASS_RATIO_POLICY,
    CANVAS_MARGIN_POLICY,
    CanvasClippingError,
    RouteCrossesProtectedNodeError,
    StepFourError,
    UnknownAspectClassError,
    UnknownCanvasMarginClassError,
    UnknownOrientationError,
    aspect_class_ratio,
    bounds_rect,
    canvas_margin,
    clipping_problems,
    content_bounds_of,
    derive_canvas_bounds,
    derive_semantic_canvas,
    margin_is_preserved,
    presentation_boxes,
    route_node_intersection_problems,
)
from agentcad.auto_layout_geometry import (
    MATERIALIZED_CLEARANCE_POLICY,
    Rect,
    UnknownClearanceClassError,
    materialized_clearance,
    placement_problems,
)
from agentcad.auto_layout_semantic import (
    STEP_2,
    STEP_3,
    STEP_4,
    place_semantic_layout,
    plan_semantic_layout,
)
from agentcad.m7_diagram_adapter import adapt

TOLERANCE = 1e-6


def step_four_fixture_a():
    plan = annotated_fixture_a()
    assert plan.produced_at_step == STEP_3
    return derive_semantic_canvas(plan)


def _union(boxes) -> Rect:
    """The envelope of a handful of boxes, computed independently of the module under test."""

    x = min(box.x for box in boxes)
    y = min(box.y for box in boxes)
    return Rect(
        x,
        y,
        max(box.right for box in boxes) - x,
        max(box.bottom for box in boxes) - y,
    )


def _node_rects(plan) -> dict[str, Rect]:
    return {
        row["engineering_id"]: Rect(row["x"], row["y"], row["width"], row["height"])
        for row in plan.placement
    }


# ---------------------------------------------------------------------------------------
# The content envelope: measured over the whole drawing, and tight
# ---------------------------------------------------------------------------------------


def test_the_content_envelope_is_the_union_of_nodes_routes_and_annotations() -> None:
    plan = step_four_fixture_a()
    boxes = presentation_boxes(plan)
    content = content_bounds_of(plan)
    assert content == _union(boxes.boxes())
    # A route contributes waypoints, not a stroke: every waypoint is a point box.
    for _name, points in boxes.routes:
        for point in points:
            assert Rect(point[0], point[1], 0.0, 0.0).x >= content.x - TOLERANCE


def test_the_envelope_is_tight_on_every_side() -> None:
    """A loose envelope is a canvas that looks wrong for no reason; a loose *check* is worse."""

    plan = step_four_fixture_a()
    content = content_bounds_of(plan)
    boxes = [*presentation_boxes(plan).boxes()]
    assert any(abs(box.x - content.x) <= TOLERANCE for box in boxes)
    assert any(abs(box.y - content.y) <= TOLERANCE for box in boxes)
    assert any(abs(box.right - content.right) <= TOLERANCE for box in boxes)
    assert any(abs(box.bottom - content.bottom) <= TOLERANCE for box in boxes)


def test_annotations_and_routes_extend_the_envelope_past_the_nodes() -> None:
    """The reason the covered-input list is closed rather than convenient.

    Labels sit below their node and route stubs leave it sideways, so a node-only envelope is
    genuinely smaller than the drawing. This fixture is built so that the difference is larger
    than the declared margin, which is what makes the next test a real check rather than a
    coincidence of the numbers.
    """

    plan = step_four_fixture_a()
    nodes = _union(list(_node_rects(plan).values()))
    content = content_bounds_of(plan)
    assert content.bottom > nodes.bottom
    assert content.x < nodes.x


def test_a_route_that_leaves_the_node_envelope_by_more_than_the_margin_is_measured() -> None:
    plan = step_four_fixture_a()
    nodes = _union(list(_node_rects(plan).values()))
    margin = canvas_margin(plan.intent.density)
    row = plan.routing[0]
    points = [(row["x"], row["y"]), *[tuple(point) for point in row["ordered_waypoints"]]]
    far = (points[-1][0], nodes.bottom + margin * 4)
    detoured = replace(
        plan,
        routing=(
            {
                **row,
                "x": points[0][0],
                "y": points[0][1],
                "ordered_waypoints": [[*point] for point in (*points[1:], far)],
            },
            *plan.routing[1:],
        ),
    )
    content = content_bounds_of(detoured)
    assert content.bottom >= far[1] - TOLERANCE


# ---------------------------------------------------------------------------------------
# No-clipping: verified against the drawing, not argued from the code
# ---------------------------------------------------------------------------------------


def test_a_canvas_too_small_to_hold_the_drawing_is_refused_by_name() -> None:
    plan = step_four_fixture_a()
    canvas = bounds_rect(plan.canvas_bounds)
    shrunken = Rect(canvas.x + 200.0, canvas.y + 200.0, canvas.width, canvas.height)
    problems = clipping_problems(plan, shrunken)
    assert problems, "a canvas 200px in from the drawing must clip something"
    assert any("is outside the canvas" in problem for problem in problems)


def test_the_canvas_derivation_verifies_clipping_and_says_what_stuck_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring check: the derivation path really runs the verification.

    The envelope is shrunk on purpose here. Whether a *plausible* subset happens to clip depends
    on the margin, and a guard whose redness depends on an accident of the numbers is the fake
    guard this project keeps catching; shrinking the envelope past the margin removes the
    coincidence.
    """

    plan = annotated_fixture_a()
    tiny = Rect(0.0, 0.0, 1.0, 1.0)
    monkeypatch.setattr("agentcad.auto_layout_canvas.content_bounds_of", lambda _plan: tiny)
    with pytest.raises(CanvasClippingError) as raised:
        derive_semantic_canvas(plan)
    assert raised.value.code == "canvas_clips_content"
    assert "outside the canvas" in str(raised.value)


def test_a_subset_envelope_would_have_missed_a_real_row() -> None:
    """The mutation, on a fixture where it is genuinely wrong.

    A node-only envelope plus the margin still fails to contain a label that sits more than a
    margin below the equipment, so this is the case the closed-list rule is for -- and the one
    that goes red. The offset is vertical on purpose: under `extra_wide` the class ratio stretches
    the canvas to twelve times the content height, so the same offset sideways would still fit
    and the mutation would look harmless. The tight axis is the one to test.
    """

    plan = annotated_fixture_a()
    margin = canvas_margin(plan.intent.density)
    nodes = _union(list(_node_rects(plan).values()))
    far_label = {
        "placement_kind": "annotation",
        "engineering_id": "el_far",
        "x": nodes.x,
        "y": round(nodes.bottom + margin * 4, 6),
        "width": 50.0,
        "height": 15.6,
        "ordered_waypoints": [],
    }
    detoured = replace(plan, annotations=(*plan.annotations, far_label))
    subset_canvas = derive_canvas_bounds(
        nodes,
        margin,
        detoured.intent.orientation,
        detoured.intent.preferred_aspect_class,
    )
    problems = clipping_problems(detoured, subset_canvas)
    assert any("el_far" in problem for problem in problems), problems
    # ... and the real envelope does contain it, so the canvas derived from it clips nothing.
    derived = derive_semantic_canvas(detoured)
    assert clipping_problems(detoured, bounds_rect(derived.canvas_bounds)) == []
    assert bounds_rect(derived.canvas_bounds).bottom >= far_label["y"] + far_label["height"]


# ---------------------------------------------------------------------------------------
# The margin and the canvas derivation itself
# ---------------------------------------------------------------------------------------


def test_the_canvas_is_the_content_plus_the_declared_margin() -> None:
    plan = step_four_fixture_a()
    content = bounds_rect(plan.content_bounds)
    canvas = bounds_rect(plan.canvas_bounds)
    margin = canvas_margin(plan.intent.density)
    assert margin_is_preserved(content, canvas, margin)
    # `extra_wide` grows the width; the height is the content plus the margin, exactly.
    assert abs(canvas.height - (content.height + margin * 2)) <= TOLERANCE


def test_an_unknown_density_class_has_no_margin_to_apply() -> None:
    with pytest.raises(UnknownCanvasMarginClassError) as raised:
        canvas_margin("dense")
    assert raised.value.code == "unknown_canvas_margin_class"
    assert {name for name, _ in CANVAS_MARGIN_POLICY} == {"compact", "comfortable"}


def test_the_margin_class_is_keyed_by_the_density_class_the_caller_chose() -> None:
    compact = canvas_margin("compact")
    comfortable = canvas_margin("comfortable")
    assert compact < comfortable


# ---------------------------------------------------------------------------------------
# Aspect class and orientation: which axis grows, and never by shrinking
# ---------------------------------------------------------------------------------------


def test_the_aspect_class_sets_the_ratio_in_the_direction_the_drawing_reads() -> None:
    content = Rect(0.0, 0.0, 100.0, 400.0)
    landscape = derive_canvas_bounds(content, 10.0, "landscape", "extra_wide")
    portrait = derive_canvas_bounds(content, 10.0, "portrait", "extra_wide")
    assert abs(landscape.width / landscape.height - 12.0) <= TOLERANCE
    assert abs(portrait.height / portrait.width - 12.0) <= TOLERANCE
    for canvas in (landscape, portrait):
        assert canvas.width >= content.width + 20.0 - TOLERANCE
        assert canvas.height >= content.height + 20.0 - TOLERANCE


def test_a_wider_class_means_a_wider_drawing() -> None:
    content = Rect(0.0, 0.0, 200.0, 200.0)
    ratios = dict(ASPECT_CLASS_RATIO_POLICY)
    assert ratios["extra_wide"] > ratios["wide"] > ratios["standard"]
    widths = [
        derive_canvas_bounds(content, 10.0, "landscape", name).width
        for name in ("standard", "wide", "extra_wide")
    ]
    assert widths == sorted(widths)


def test_the_class_ratio_survives_the_growth_it_caused() -> None:
    """`extra_wide` is what the reference drawing needs; it must still be extra wide afterwards."""

    plan = step_four_fixture_a()
    canvas = bounds_rect(plan.canvas_bounds)
    assert plan.intent.preferred_aspect_class == "extra_wide"
    assert abs(canvas.width / canvas.height - 12.0) <= 1e-3


def test_an_unknown_orientation_or_aspect_class_is_a_hard_failure() -> None:
    content = Rect(0.0, 0.0, 100.0, 100.0)
    with pytest.raises(UnknownOrientationError) as raised:
        derive_canvas_bounds(content, 10.0, "diagonal", "wide")
    assert raised.value.code == "unknown_orientation"
    with pytest.raises(UnknownAspectClassError) as raised:
        aspect_class_ratio("ultra_wide")
    assert raised.value.code == "unknown_aspect_class"


def test_the_canvas_never_shrinks_below_the_content_it_was_asked_to_hold() -> None:
    for orientation in ("landscape", "portrait"):
        for name in ("standard", "wide", "extra_wide"):
            content = Rect(-50.0, -20.0, 300.0, 120.0)
            canvas = derive_canvas_bounds(content, 24.0, orientation, name)
            assert margin_is_preserved(content, canvas, 24.0)


# ---------------------------------------------------------------------------------------
# Determinism, the intent gate, and the one layout authority
# ---------------------------------------------------------------------------------------


def test_the_canvas_is_a_pure_function_of_the_plan() -> None:
    first = derive_semantic_canvas(annotated_fixture_a())
    second = derive_semantic_canvas(annotated_fixture_a())
    assert first.canvas_bounds == second.canvas_bounds
    assert first.content_bounds == second.content_bounds
    assert first.digest == second.digest


def test_every_intent_dimension_is_applied_once_the_canvas_exists() -> None:
    """Step 4 is where the last two dimensions land, so nothing may still be pending after it."""

    final = step_four_fixture_a()
    assert final.pending_intent_dimensions == ()
    for dimension in ("orientation", "preferred_aspect_class"):
        applied = next(
            item.applied_at_step
            for item in contract.LAYOUT_INTENT_CONSUMPTION
            if item.dimension == dimension
        )
        assert applied == contract.CANVAS_DERIVATION_STEP


def test_the_canvas_is_produced_at_step_four_from_a_routed_plan() -> None:
    final = step_four_fixture_a()
    assert final.produced_at_step == STEP_4
    assert final.canvas_bounds is not None
    snapshot = fixture_a_snapshot()
    # A plan that has not been routed has no content to measure, and saying so beats measuring it.
    unplaced = place_semantic_layout(plan_semantic_layout(adapt(fixture_a_payload())))
    assert unplaced.produced_at_step == STEP_2
    with pytest.raises(StepFourError) as raised:
        derive_semantic_canvas(unplaced)
    assert raised.value.code == "step_four_error"
    assert snapshot.digest  # the geometry the drawing was placed against is still recorded


def test_the_canvas_step_takes_no_canvas_from_the_caller() -> None:
    """The signature is the guard: with no parameter for pixels, there is nothing to pass."""

    from agentcad.auto_layout import AutoLayoutEngine as BaseAutoLayoutEngine
    from agentcad.auto_layout_engine import AutoLayoutEngine

    parameters = list(
        inspect.signature(BaseAutoLayoutEngine.layout_semantic_canvas).parameters
    )
    assert parameters == ["self", "plan"]
    assert (
        AutoLayoutEngine.layout_semantic_canvas
        is BaseAutoLayoutEngine.layout_semantic_canvas
    )


def test_steps_one_to_four_run_through_the_one_layout_authority() -> None:
    from agentcad.auto_layout_engine import AutoLayoutEngine

    engine = AutoLayoutEngine.__new__(AutoLayoutEngine)
    snapshot = fixture_a_snapshot()
    plan = place_semantic_layout(engine.layout_semantic_topology(adapt(fixture_a_payload())))
    geometry = engine.layout_semantic_geometry(plan, snapshot, {"el_ar_tank": "V-101"})
    final = engine.layout_semantic_canvas(geometry)
    assert final.produced_at_step == STEP_4
    assert final.canvas_bounds is not None
    assert clipping_problems(final, bounds_rect(final.canvas_bounds)) == []


# ---------------------------------------------------------------------------------------
# The narrowed crossing rule: only the escape segment may enter its own endpoint
# ---------------------------------------------------------------------------------------


def test_the_router_leaves_no_route_inside_a_node_it_does_not_enter_there() -> None:
    assert route_node_intersection_problems(step_four_fixture_a()) == []


def test_a_route_that_cuts_back_through_its_own_node_is_refused() -> None:
    plan = annotated_fixture_a()
    rects = _node_rects(plan)
    row = plan.routing[0]
    connection_id = row["engineering_id"]
    source = next(
        binding
        for binding in plan.endpoint_bindings
        if (binding.connection_id, binding.role) == (connection_id, "source")
    )
    target = next(
        binding
        for binding in plan.endpoint_bindings
        if (binding.connection_id, binding.role) == (connection_id, "target")
    )
    source_box = rects[source.node_id]
    target_box = rects[target.node_id]
    middle_y = source_box.y + source_box.height / 2
    crossing = {
        "placement_kind": "routing",
        "engineering_id": connection_id,
        "x": source_box.x,
        "y": middle_y,
        "width": 0.0,
        "height": 0.0,
        # Escape outward, then travel back across the node it just left, then on to the target.
        "ordered_waypoints": [
            [source_box.x - 24.0, middle_y],
            [source_box.right + 24.0, middle_y],
            [target_box.x + target_box.width / 2, target_box.y],
        ],
    }
    detoured = replace(plan, routing=(crossing, *plan.routing[1:]))
    problems = route_node_intersection_problems(detoured)
    assert any(
        "segment 1" in problem and source.node_id in problem for problem in problems
    ), problems
    with pytest.raises(RouteCrossesProtectedNodeError) as raised:
        derive_semantic_canvas(detoured)
    assert raised.value.code == "route_crosses_protected_node"


# ---------------------------------------------------------------------------------------
# Binding the materialized clearance policy to the contract
# ---------------------------------------------------------------------------------------


def test_the_clearance_policy_is_its_own_rule_and_matches_the_contract() -> None:
    assert contract.MATERIALIZED_NODE_CLEARANCE_POLICY_NAME
    assert set(contract.MATERIALIZED_CLEARANCE_FIELDS) == {
        "minimum_node_clearance",
        "minimum_system_clearance",
    }
    assert not set(contract.MATERIALIZED_CLEARANCE_FIELDS) & set(
        contract.DENSITY_SPACING_POLICY_FIELDS
    )
    clearance = materialized_clearance("compact")
    assert set(clearance.to_projection()) == set(contract.MATERIALIZED_CLEARANCE_FIELDS)
    assert {name for name, _ in MATERIALIZED_CLEARANCE_POLICY} == {"compact", "comfortable"}


def test_the_clearance_policy_differs_by_density_class_and_refuses_an_unknown_one() -> None:
    compact = materialized_clearance("compact")
    comfortable = materialized_clearance("comfortable")
    assert compact.minimum_node_clearance < comfortable.minimum_node_clearance
    assert compact.minimum_system_clearance < comfortable.minimum_system_clearance
    with pytest.raises(UnknownClearanceClassError) as raised:
        materialized_clearance("dense")
    assert raised.value.code == "unknown_clearance_class"


def test_the_reflow_is_checked_against_clearance_not_against_the_origin_spacing() -> None:
    """Zero gaps pass the overlap rule and must fail the clearance rule."""

    plan = annotated_fixture_a()
    touching = []
    for row in plan.placement:
        touching.append({**row, "x": round(row["x"], 6)})
    # Two nodes that share an edge: not overlapping, but with no clearance at all.
    stacked = (
        {"placement_kind": "equipment", "engineering_id": "a", "x": 0.0, "y": 0.0, "width": 100.0, "height": 100.0},
        {"placement_kind": "equipment", "engineering_id": "b", "x": 100.0, "y": 0.0, "width": 100.0, "height": 100.0},
    )
    same_system = replace(
        plan,
        placement=stacked,
        node_ids_by_system=(("S1", ("a", "b")),),
        node_kinds=(("a", "equipment"), ("b", "equipment")),
        flow_edges=(("a", "b"),),
        systems_in_reading_order=("S1",),
    )
    problems = placement_problems(same_system.placement, same_system, materialized_clearance("compact"))
    assert any("clearance" in problem for problem in problems), problems
    assert not any("rank gap" in problem or "node gap" in problem for problem in problems)
    assert touching
