"""M7-2 phase 2B step 2: deterministic partition, rank and absolute placement.

The claim here is the one the milestone is ultimately about: with **no coordinates anywhere in
the model's output**, the engine produces a unique, complete, recomputable placement while
engineering semantics stay byte-identical. Whether that placement is *pretty* is step 3's and
step 4's problem -- routing, annotations, content bounds and the canvas are all still missing,
and each has to be able to fail on its own.

Four things carry more weight than the rest:

* **The topology is not written to.** Placement is a projection *beside* the topology, so
  `topology_semantic_digest` can be compared before and after and must be equal. A number
  written back into a node would make that comparison meaningless.
* **The numbers are engine rules, not intent.** The model asks for `compact`; the engine owns
  what that means, under one rules version. A gap number the model could state would hand
  coordinate authority back through the intent.
* **Completeness is checked in both directions.** Every declared node is placed exactly once,
  and no row names an entity the topology does not declare.
* **The same input is the same placement.** Not "usually" -- the digest of two runs is compared,
  including after shuffling the order the caller listed things in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcad.auto_layout_semantic import (
    DENSITY_SPACING_POLICY,
    NODE_SIZE_POLICY,
    SEMANTIC_LAYOUT_PLAN_DIGEST_VERSION,
    STEP_2,
    UnknownDensityError,
    UnknownPlacementKindError,
    UnsupportedLayoutIntentError,
    place_semantic_layout,
    placement_digest,
    plan_semantic_layout,
    resolve_grouping,
    spacing_policy,
)
from agentcad.m7_diagram_adapter import adapt, spec_semantic_digest, topology_semantic_digest
from agentcad.m7_diagram_spec import load_diagram_spec
from agentcad.m7_layout_contract import (
    LAYOUT_COORDINATE_DECIMALS,
    LAYOUT_DIGEST_INPUTS,
    LAYOUT_INTENT_CONSUMPTION,
    LAYOUT_INTENT_DIMENSIONS,
    PLACEMENT_INVARIANTS,
    PLACEMENT_KINDS,
    PLACEMENT_PROJECTION_FIELDS,
    PLACEMENT_PROJECTION_VERSION,
    STEP_2_PLACEMENT_KINDS,
)

TASK_BOOK = Path(__file__).resolve().parents[2] / "docs" / "m7-2-deterministic-layout.md"


def fixture_a_payload() -> dict:
    from test_m7_diagram_adapter import fixture_a_payload as payload

    return payload()


def placed(payload: dict | None = None):
    return place_semantic_layout(plan_semantic_layout(adapt(payload or fixture_a_payload())))


def rows_by_id(plan) -> dict[str, dict]:
    return {row["engineering_id"]: row for row in plan.placement}


# --------------------------------------------------------------------------------------
# Completeness, in both directions
# --------------------------------------------------------------------------------------


def test_every_declared_node_is_placed_exactly_once() -> None:
    topology = adapt(fixture_a_payload())
    plan = place_semantic_layout(plan_semantic_layout(topology))
    declared = {node.engineering_id for node in topology.nodes}
    placed_ids = [row["engineering_id"] for row in plan.placement]

    assert sorted(placed_ids) == sorted(declared)
    assert len(set(placed_ids)) == len(placed_ids)


def test_no_placement_names_an_entity_the_topology_does_not_declare() -> None:
    topology = adapt(fixture_a_payload())
    plan = place_semantic_layout(plan_semantic_layout(topology))
    declared = {node.engineering_id for node in topology.nodes}

    assert not [row for row in plan.placement if row["engineering_id"] not in declared]


def test_every_row_carries_exactly_the_declared_projection_fields() -> None:
    plan = placed()

    assert PLACEMENT_PROJECTION_FIELDS == (
        "placement_kind",
        "engineering_id",
        "x",
        "y",
        "width",
        "height",
    )
    for row in plan.placement:
        assert tuple(row) != () and set(row) == set(PLACEMENT_PROJECTION_FIELDS)


def test_step_2_places_the_kinds_it_declares_and_no_others() -> None:
    plan = placed()

    assert STEP_2_PLACEMENT_KINDS == ("equipment", "instrument")
    assert {row["placement_kind"] for row in plan.placement} <= set(PLACEMENT_KINDS)
    assert {row["placement_kind"] for row in plan.placement} == set(STEP_2_PLACEMENT_KINDS)


def test_an_unknown_kind_is_a_failure_rather_than_an_invented_row() -> None:
    from dataclasses import replace

    plan = plan_semantic_layout(adapt(fixture_a_payload()))
    mutated = replace(
        plan,
        node_kinds=tuple(
            (node_id, "annotation" if node_id == "el_ar_tank" else kind)
            for node_id, kind in plan.node_kinds
        ),
    )
    with pytest.raises(UnknownPlacementKindError):
        place_semantic_layout(mutated)


def test_the_topology_and_the_plan_are_immutable_by_construction() -> None:
    """A write-back is not merely discouraged: there is nothing to write through.

    The semantic digest is computed over these very objects, so merging placement into the
    topology would require unfreezing the things whose equality the digest measures -- which is
    why this is enforced by the types rather than by a convention. The placement step is also
    handed the plan alone, so a coordinate has no reference back to the topology at all.
    """

    import dataclasses
    import inspect

    node = adapt(fixture_a_payload()).nodes[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.x = 1.0  # type: ignore[misc]
    plan = plan_semantic_layout(adapt(fixture_a_payload()))
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.placement = ()  # type: ignore[misc]
    assert tuple(inspect.signature(place_semantic_layout).parameters) == ("plan",)


def test_placement_does_not_write_into_the_topology() -> None:
    """The semantic digest is the check: same specification, same digest, before and after."""

    payload = fixture_a_payload()
    spec = load_diagram_spec(payload)
    topology = adapt(spec)
    before = topology_semantic_digest(topology)

    plan = place_semantic_layout(plan_semantic_layout(topology))

    assert topology_semantic_digest(topology) == before == spec_semantic_digest(spec)
    assert plan.topology_digest == topology.digest
    # Nothing was written onto a node: the adapter's nodes are frozen and carry no coordinates.
    assert not any(hasattr(node, "x") or hasattr(node, "position") for node in topology.nodes)
    assert "x" not in json.dumps(
        [node.engineering_id for node in topology.nodes] + list(topology.intent.system_order)
    )


# --------------------------------------------------------------------------------------
# Determinism: the same input is the same placement
# --------------------------------------------------------------------------------------


def test_the_same_topology_and_intent_produce_the_same_placement_digest() -> None:
    first = placed()
    second = placed()

    assert first.placement == second.placement
    assert placement_digest(first) == placement_digest(second)
    assert len(first.placement) == len(second.placement)


def test_listing_order_does_not_change_the_placement() -> None:
    shuffled = fixture_a_payload()
    shuffled["entities"] = list(reversed(shuffled["entities"]))
    shuffled["connections"] = list(reversed(shuffled["connections"]))
    shuffled["systems"] = list(reversed(shuffled["systems"]))

    assert placement_digest(placed(shuffled)) == placement_digest(placed())


def test_the_placement_projection_is_sorted_and_unique_on_the_canonical_key() -> None:
    plan = placed()
    keys = [(row["placement_kind"], row["engineering_id"]) for row in plan.placement]

    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)


def test_density_changes_the_numbers_and_the_density_class_changes_the_identity() -> None:
    compact = fixture_a_payload()
    compact["layout_intent"]["density"] = "compact"
    comfortable = fixture_a_payload()
    comfortable["layout_intent"]["density"] = "comfortable"

    compact_plan, comfortable_plan = placed(compact), placed(comfortable)

    assert compact_plan.placement != comfortable_plan.placement
    assert placement_digest(compact_plan) != placement_digest(comfortable_plan)
    assert compact_plan.intent.density == "compact"
    assert compact_plan.spacing == spacing_policy("compact")


def test_flow_direction_turns_the_drawing_without_changing_which_system_comes_first() -> None:
    horizontal = fixture_a_payload()
    horizontal["layout_intent"]["primary_flow_direction"] = "left_to_right"
    vertical = fixture_a_payload()
    vertical["layout_intent"]["primary_flow_direction"] = "top_to_bottom"

    horizontal_plan, vertical_plan = placed(horizontal), placed(vertical)
    horizontal_rows = rows_by_id(horizontal_plan)
    vertical_rows = rows_by_id(vertical_plan)

    # The tank leads the supply system either way; the axis it leads along is what changed.
    assert horizontal_rows["el_ar_tank"]["x"] < horizontal_rows["el_purifier"]["x"]
    assert vertical_rows["el_ar_tank"]["y"] < vertical_rows["el_purifier"]["y"]
    assert horizontal_plan.systems_in_reading_order == vertical_plan.systems_in_reading_order
    assert horizontal_plan.placement != vertical_plan.placement


def test_the_same_system_keeps_its_nodes_together() -> None:
    """`grouped_by_system` is an ordering fact, and it is visible in the coordinates."""

    plan = placed()
    rows = rows_by_id(plan)
    cross = {row["engineering_id"]: row["y"] for row in plan.placement}

    supply = [rows["el_ar_tank"], rows["el_purifier"], rows["el_pt_101"]]
    cover = rows["el_recycle"]
    # The supply band's furthest node is still nearer the origin than the cover band's first.
    assert max(cross[node["engineering_id"]] for node in supply) < cover["y"]


def test_flat_grouping_chains_systems_instead_of_stacking_them() -> None:
    grouped = fixture_a_payload()
    grouped["layout_intent"]["grouping"] = "grouped_by_system"
    flat = fixture_a_payload()
    flat["layout_intent"]["grouping"] = "flat"

    grouped_rows = rows_by_id(placed(grouped))
    flat_rows = rows_by_id(placed(flat))

    assert grouped_rows["el_recycle"]["y"] > 0 and grouped_rows["el_recycle"]["x"] == 0
    assert flat_rows["el_recycle"]["y"] == 0 and flat_rows["el_recycle"]["x"] > 0


def test_a_grouping_class_the_engine_cannot_honour_is_refused_with_a_code() -> None:
    """Refused, not replaced. A substitute -- even one that reports itself -- is still a drawing
    nobody asked for, and it looks like an answer while being the wrong one."""

    payload = fixture_a_payload()
    payload["layout_intent"]["grouping"] = "grouped_by_zone"

    with pytest.raises(UnsupportedLayoutIntentError) as failure:
        placed(payload)

    assert failure.value.code == "zone_grouping_requires_zone_membership"
    assert failure.value.dimension == "grouping"
    assert failure.value.value == "grouped_by_zone"
    assert "zone membership" in failure.value.reason


def test_a_refused_intent_produces_no_placement_at_all() -> None:
    """Zero placement, not a partial one: the refusal happens before anything is computed."""

    from agentcad.auto_layout_semantic import placement_digest as digest_of

    payload = fixture_a_payload()
    payload["layout_intent"]["grouping"] = "grouped_by_zone"
    plan = plan_semantic_layout(adapt(payload))

    # The plan itself is still built -- it records the intent it was given ...
    assert plan.intent.grouping == "grouped_by_zone"
    assert plan.placement == ()
    assert plan.spacing is None
    # ... and the refusal is where the drawing would have been.
    with pytest.raises(UnsupportedLayoutIntentError):
        place_semantic_layout(plan)
    assert plan.placement == ()
    assert digest_of(plan) != digest_of(placed())


def test_the_supported_grouping_classes_are_exactly_the_declared_supported_ones() -> None:
    """Recognition and executability are different facts: a class may stay in the vocabulary."""

    from agentcad.m7_layout_contract import (
        SUPPORTED_LAYOUT_INTENT_CLASSES,
        UNSUPPORTED_LAYOUT_INTENT,
    )

    vocabulary = tuple(layout_intent_dimension_values("grouping"))
    unsupported = {item.value for item in UNSUPPORTED_LAYOUT_INTENT}
    supported = {item.value for item in SUPPORTED_LAYOUT_INTENT_CLASSES}

    assert "grouped_by_zone" in vocabulary
    assert "grouped_by_zone" in unsupported
    assert set(vocabulary) - unsupported == supported == {"grouped_by_system", "flat"}
    for value in sorted(supported):
        assert resolve_grouping(value) == value
    for value in sorted(unsupported):
        with pytest.raises(UnsupportedLayoutIntentError):
            resolve_grouping(value)


def test_an_unknown_density_is_a_hard_failure() -> None:
    with pytest.raises(UnknownDensityError) as failure:
        spacing_policy("roomy")

    assert "roomy" in str(failure.value)


# --------------------------------------------------------------------------------------
# The numbers are engine rules, and the drawing is legible in the weak sense
# --------------------------------------------------------------------------------------


def test_every_density_class_has_exactly_one_policy() -> None:
    density_classes = tuple(layout_intent_dimension_values("density"))

    assert tuple(name for name, _ in DENSITY_SPACING_POLICY) == density_classes
    assert len({name for name, _ in DENSITY_SPACING_POLICY}) == len(DENSITY_SPACING_POLICY)


def layout_intent_dimension_values(name: str) -> tuple[str, ...]:
    for dimension in LAYOUT_INTENT_DIMENSIONS:
        if dimension.name == name:
            return tuple(dimension.values)
    raise AssertionError(f"no declared intent dimension {name!r}")


def test_the_spacing_policy_keeps_nodes_apart_by_construction() -> None:
    """No overlap, and not by luck: the lane pitch is at least the tallest node, and the rank
    pitch is at least the widest."""

    tallest = max(size.height for _, size in NODE_SIZE_POLICY)
    widest = max(size.width for _, size in NODE_SIZE_POLICY)

    for name, policy in DENSITY_SPACING_POLICY:
        assert policy.node_gap >= tallest, name
        assert policy.rank_gap >= widest, name
        assert policy.system_gap > 0 and policy.component_gap > 0, name


@pytest.mark.parametrize("density", ["compact", "comfortable"])
def test_no_two_placed_nodes_overlap(density: str) -> None:
    payload = fixture_a_payload()
    payload["layout_intent"]["density"] = density
    plan = placed(payload)

    for index, first in enumerate(plan.placement):
        for second in plan.placement[index + 1 :]:
            overlapping = (
                first["x"] < second["x"] + second["width"]
                and second["x"] < first["x"] + first["width"]
                and first["y"] < second["y"] + second["height"]
                and second["y"] < first["y"] + first["height"]
            )
            assert not overlapping, (first["engineering_id"], second["engineering_id"])


def test_every_coordinate_is_finite_and_quantized_to_the_declared_quantum() -> None:
    plan = placed()

    for row in plan.placement:
        for field in ("x", "y"):
            value = row[field]
            assert isinstance(value, (int, float))
            assert value == round(value, LAYOUT_COORDINATE_DECIMALS)
        assert row["width"] > 0 and row["height"] > 0


def test_a_recycle_loop_is_ranked_rather_than_rejected() -> None:
    """A loop is a chain that comes back. The purifier returns to the tank it draws from, so
    the two occupy consecutive ranks instead of collapsing into one pile."""

    payload = fixture_a_payload()
    payload["connections"].append(
        {
            "engineering_id": "cn_loop_back",
            "source_engineering_id": "el_purifier",
            "target_engineering_id": "el_ar_tank",
            "medium": "argon",
        }
    )
    plan = placed(payload)
    rows = rows_by_id(plan)

    policy = spacing_policy(plan.intent.density)
    # The two ends of the cycle take consecutive ranks rather than collapsing onto one point.
    assert rows["el_ar_tank"]["x"] < rows["el_purifier"]["x"]
    assert rows["el_purifier"]["x"] - rows["el_ar_tank"]["x"] == policy.rank_gap
    assert sorted(row["engineering_id"] for row in plan.placement) == sorted(rows)


def test_a_cross_system_connection_does_not_merge_two_rank_structures() -> None:
    """The declared rule, stated as a test: partitions keep meaning something, and a connection
    between two systems is a step-3 routing problem rather than a reason to merge bands."""

    payload = fixture_a_payload()
    payload["connections"].append(
        {
            "engineering_id": "cn_cross_loop",
            "source_engineering_id": "el_recycle",
            "target_engineering_id": "el_ar_tank",
            "medium": "argon",
        }
    )
    plan = placed(payload)
    baseline = placed()
    rows = rows_by_id(plan)

    # The return edge is real topology (it changes the plan's identity) ...
    assert plan.digest != baseline.digest
    # ... and it changes no rank, because it crosses two systems' bands.
    assert plan.placement == baseline.placement
    assert rows["el_ar_tank"]["x"] == 0.0 and rows["el_recycle"]["x"] == 0.0


# --------------------------------------------------------------------------------------
# Step boundaries: what step 2 still does not do
# --------------------------------------------------------------------------------------


def test_step_2_produces_no_routing_no_envelope_and_no_canvas() -> None:
    plan = placed()

    assert plan.produced_at_step == STEP_2
    assert plan.canvas_bounds is None
    assert "canvas_bounds" not in json.dumps(plan.placement)
    for row in plan.placement:
        assert "ordered_waypoints" not in row and "bounds" not in row
    assert plan.pending_intent_dimensions == ("orientation", "preferred_aspect_class")


def test_only_the_engine_numbers_are_model_visible() -> None:
    """The model states a class; the plan records the class and the engine's numbers separately."""

    plan = placed()
    intent_fields = plan.intent.to_projection()

    assert set(intent_fields) == {item.dimension for item in LAYOUT_INTENT_CONSUMPTION}
    assert not [
        value
        for value in intent_fields.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    assert set(plan.spacing.to_projection()) == {
        "rank_gap",
        "node_gap",
        "system_gap",
        "component_gap",
    }


def test_the_placement_digest_is_not_the_canonical_layout_digest() -> None:
    plan = placed()

    assert PLACEMENT_PROJECTION_VERSION not in LAYOUT_DIGEST_INPUTS
    assert SEMANTIC_LAYOUT_PLAN_DIGEST_VERSION not in LAYOUT_DIGEST_INPUTS
    assert SEMANTIC_LAYOUT_PLAN_DIGEST_VERSION != PLACEMENT_PROJECTION_VERSION
    assert plan.digest_version != PLACEMENT_PROJECTION_VERSION
    assert placement_digest(plan) != plan.digest


def test_the_placement_step_refuses_an_already_placed_plan() -> None:
    """Otherwise the second run's input is the first run's output, and a replay would compound."""

    from agentcad.auto_layout_semantic import SemanticTopologyIngressError

    once = placed()
    with pytest.raises(SemanticTopologyIngressError) as failure:
        place_semantic_layout(once)

    assert "step-1 plan" in str(failure.value)


def test_the_task_book_and_the_contract_agree_on_the_step_2_invariants() -> None:
    task_book = TASK_BOOK.read_text(encoding="utf-8")

    assert "PLACEMENT_INVARIANTS" in task_book
    assert "DENSITY_SPACING_POLICY" in task_book
    assert PLACEMENT_INVARIANTS
    assert "no two placed nodes overlap" in PLACEMENT_INVARIANTS
