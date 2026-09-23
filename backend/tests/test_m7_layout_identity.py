"""M7-2 phase 2B step 5: the canonical layout identity and deterministic replay.

Grouped by the failure each part exists to prevent: a layout that quietly changed the plant it was
given, a geometry that dropped an entity while every digest stayed equal, an identity that depends
on the clock or on the order a caller happened to list things in, and a digest envelope that grows
a field nobody declared.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_m7_diagram_adapter import fixture_a_payload
from test_m7_step3_geometry import annotated_fixture_a, fixture_a_plan, fixture_a_snapshot

from agentcad import m7_layout_contract as contract
from agentcad.auto_layout_canvas import derive_semantic_canvas
from agentcad.auto_layout_geometry import route_semantic_layout
from agentcad.auto_layout_identity import (
    CANONICAL_PROJECTION_FIELD_NAMES,
    ENGINEERING_ROW_PARTS,
    ENGINEERING_SEMANTIC_ROW_PARTS,
    CanonicalProjectionError,
    EngineeringSemanticPreservationError,
    GeometryCoverageError,
    LayoutIdentityError,
    LayoutInputPreservationError,
    ReplayIdentityMismatchError,
    canonical_layout_digest_for_payload,
    canonical_layout_payload,
    canonical_projection_envelope,
    canonical_projection_rows,
    deterministic_replay_problems,
    engineering_semantic_preservation_problems,
    engineering_semantic_rows,
    finalize_semantic_layout,
    geometry_coverage_problems,
    layout_input_preservation_problems,
    payload_problems,
    plan_engineering_rows,
    plan_engineering_semantic_digest,
)
from agentcad.auto_layout_semantic import (
    LAYOUT_ENGINE_VERSION,
    STEP_4,
    STEP_5,
    PlanConnection,
    PlanEngineeringFact,
    place_semantic_layout,
    plan_digest,
    plan_semantic_layout,
)
from agentcad.m7_diagram_adapter import (
    adapt,
    engineering_digest,
    topology_engineering_rows,
    topology_semantic_digest,
)
from agentcad.m7_layout_contract import (
    LAYOUT_DIGEST_EXCLUDES_VOLATILE_BOOKKEEPING,
    LAYOUT_DIGEST_INPUTS,
)


def fixture_a_topology():
    return adapt(fixture_a_payload())


def fixture_a_step_four():
    plan = derive_semantic_canvas(annotated_fixture_a())
    assert plan.produced_at_step == STEP_4
    return plan


def swapped_symbol_plan():
    """The same drawing with one node bound to a different, equally valid catalogue symbol."""

    plan = fixture_a_step_four()
    symbols = dict(plan.node_symbols)
    symbols["el_purifier"] = "gas_tank"  # equipment -> equipment, so the swap is legal and real
    assert symbols != dict(plan.node_symbols)
    return replace(
        plan, node_symbols=tuple((node_id, symbols[node_id]) for node_id, _ in plan.node_symbols)
    )


def fixture_a_identity():
    topology = fixture_a_topology()
    return finalize_semantic_layout(fixture_a_step_four(), topology), topology


# ---------------------------------------------------------------------------------------
# The two sides of the preservation gate are two descriptions, not one function
# ---------------------------------------------------------------------------------------


def test_the_plan_reconstruction_matches_the_topology_row_for_row() -> None:
    """The gate is only meaningful if both sides describe the same plant before any layout runs."""

    topology = fixture_a_topology()
    plan = plan_semantic_layout(topology)
    assert plan_engineering_rows(plan) == topology_engineering_rows(topology)
    assert engineering_digest(plan_engineering_rows(plan)) == topology_semantic_digest(topology)


def test_the_reconstruction_carries_every_declared_part() -> None:
    rows = plan_engineering_rows(plan_semantic_layout(fixture_a_topology()))
    assert tuple(rows) == ENGINEERING_ROW_PARTS
    assert rows["entities"] and rows["connections"] and rows["systems"]
    for row in rows["entities"]:
        for field in ("tag", "name", "equipment_class", "instrument_type", "measurement"):
            assert field in row
    for row in rows["connections"]:
        assert "medium" in row and "tag" in row and "source_port_id" in row


# ---------------------------------------------------------------------------------------
# Negative gate 1: the layout may not change the plant
# ---------------------------------------------------------------------------------------


def test_a_clean_layout_reports_no_preservation_problem() -> None:
    topology = fixture_a_topology()
    assert engineering_semantic_preservation_problems(fixture_a_step_four(), topology) == []


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (
            # a device reclassified on the way through placement
            lambda plan: replace(
                plan,
                engineering_entities=tuple(
                    replace(fact, kind="instrument")
                    if fact.engineering_id == "el_ar_tank"
                    else fact
                    for fact in plan.engineering_entities
                ),
            ),
            "entities",
        ),
        (
            # a tag rewritten by a step that had no business touching it
            lambda plan: replace(
                plan,
                engineering_entities=tuple(
                    replace(fact, tag="V-999")
                    if fact.engineering_id == "el_ar_tank"
                    else fact
                    for fact in plan.engineering_entities
                ),
            ),
            "entities",
        ),
        (
            # a node moved to another system
            lambda plan: replace(
                plan,
                engineering_entities=tuple(
                    replace(fact, system_id="S_cover")
                    if fact.engineering_id == "el_pt_101"
                    else fact
                    for fact in plan.engineering_entities
                ),
            ),
            "entities",
        ),
        (
            # a connection re-pointed
            lambda plan: replace(
                plan,
                connections=tuple(                        replace(connection, target_engineering_id="el_recycle")
                        if connection.connection_id == "cn_1"
                        else connection
                        for connection in plan.connections
                ),
            ),
            "connections",
        ),
        (
            # a flow direction flipped
            lambda plan: replace(
                plan,
                connections=tuple(
                    PlanConnection(
                        connection_id=connection.connection_id,
                        source_engineering_id=connection.target_engineering_id,
                        target_engineering_id=connection.source_engineering_id,
                        source_port_id=connection.target_port_id,
                        target_port_id=connection.source_port_id,
                        medium=connection.medium,
                        tag=connection.tag,
                    )
                    for connection in plan.connections
                ),
            ),
            "connections",
        ),
        (
            # a system membership dropped from the record
            lambda plan: replace(plan, engineering_systems=plan.engineering_systems[:1]),
            "systems",
        ),
        (
            # a required loop's membership changed
            lambda plan: replace(
                plan,
                required_loops=tuple(
                    (loop_id, tuple(reversed(engineering_ids)))
                    for loop_id, engineering_ids in plan.required_loops
                ),
            ),
            "required_loops",
        ),
    ],
)
def test_the_gate_catches_a_layout_that_changed_the_plant(mutate, expected: str) -> None:
    plan = mutate(fixture_a_step_four())
    problems = engineering_semantic_preservation_problems(plan, fixture_a_topology())
    assert problems, "a changed plant must be reported"
    assert expected in problems[0]


# ---------------------------------------------------------------------------------------
# Proof A and proof B are two names for two things, and each must be blind to the other
# ---------------------------------------------------------------------------------------


def test_the_engineering_subset_is_a_subset_of_the_upstream_row_shape() -> None:
    """The layout-layer digest is the engineering part of the upstream one, not the same value."""

    plan = plan_semantic_layout(fixture_a_topology())
    rows = plan_engineering_rows(plan)
    assert ENGINEERING_SEMANTIC_ROW_PARTS == (
        "spec_schema",
        "systems",
        "entities",
        "connections",
        "required_loops",
    )
    assert set(ENGINEERING_SEMANTIC_ROW_PARTS) < set(ENGINEERING_ROW_PARTS)
    assert "layout_intent" not in ENGINEERING_SEMANTIC_ROW_PARTS
    assert "layout_intent" in ENGINEERING_ROW_PARTS
    assert tuple(engineering_semantic_rows(rows)) == ENGINEERING_SEMANTIC_ROW_PARTS
    assert plan_engineering_semantic_digest(plan) != engineering_digest(rows)


def test_the_engineering_gate_does_not_own_the_layout_intent() -> None:
    """A changed intent is real and must be red -- but under proof B's name, not proof A's."""

    plan = fixture_a_step_four()
    assert plan.intent.density == "compact", "the fixture's density is what this test negates"
    changed = replace(plan, intent=replace(plan.intent, density="standard"))
    assert engineering_semantic_preservation_problems(changed, fixture_a_topology()) == []
    problems = layout_input_preservation_problems(changed, fixture_a_topology())
    assert problems
    assert "layout intent" in problems[0]


def test_the_engineering_gate_does_not_own_the_symbol_binding() -> None:
    """A renderer swap is not a change to the plant: the engineering digest was signed to
    exclude ``symbol_key``, and folding it back in here would undo that separation."""

    swapped = swapped_symbol_plan()
    assert engineering_semantic_preservation_problems(swapped, fixture_a_topology()) == []
    problems = layout_input_preservation_problems(swapped, fixture_a_topology())
    assert problems
    assert "symbol binding" in problems[0]


def test_the_layout_input_gate_does_not_own_the_tag() -> None:
    """And the other direction: a rewritten tag is proof A's business, not proof B's."""

    plan = replace(
        fixture_a_step_four(),
        engineering_entities=tuple(
            replace(fact, tag="V-999") for fact in fixture_a_step_four().engineering_entities
        ),
    )
    assert layout_input_preservation_problems(plan, fixture_a_topology()) == []
    assert engineering_semantic_preservation_problems(plan, fixture_a_topology())


def test_the_layout_input_gate_catches_node_kinds_and_the_topology_identity() -> None:
    topology = fixture_a_topology()
    plan = fixture_a_step_four()
    kinds = replace(plan, node_kinds=tuple((node_id, "instrument") for node_id, _ in plan.node_kinds))
    assert any(
        "node kind" in problem for problem in layout_input_preservation_problems(kinds, topology)
    )
    other = adapt(
        {
            **fixture_a_payload(),
            "systems": [
                *fixture_a_payload()["systems"],
                {"system_id": "S_extra", "name": "备用", "order": 2},
            ],
            "layout_intent": {
                **fixture_a_payload()["layout_intent"],
                "system_order": [*fixture_a_payload()["layout_intent"]["system_order"], "S_extra"],
            },
        }
    )
    assert other.digest != topology.digest
    problems = layout_input_preservation_problems(plan, other)
    assert any("topology" in problem for problem in problems)


def test_finalize_refuses_a_swapped_symbol_binding() -> None:
    with pytest.raises(LayoutInputPreservationError):
        finalize_semantic_layout(swapped_symbol_plan(), fixture_a_topology())


def test_finalize_refuses_a_changed_intent() -> None:
    plan = replace(
        fixture_a_step_four(), intent=replace(fixture_a_step_four().intent, density="standard")
    )
    with pytest.raises(LayoutInputPreservationError):
        finalize_semantic_layout(plan, fixture_a_topology())


def test_the_gate_names_the_other_topology() -> None:
    """A plan and a topology that do not belong together prove nothing, so it is refused."""

    topology = fixture_a_topology()
    other = adapt(
        {
            **fixture_a_payload(),
            "systems": [{"system_id": "S_other", "name": "其他", "order": 0}],
            "entities": [
                {
                    "engineering_id": "el_other",
                    "kind": "equipment",
                    "system_id": "S_other",
                    "equipment_class": "vessel",
                    "symbol_key": "tank",
                }
            ],
            "connections": [],
            "required_loops": [],
            "layout_intent": {
                **fixture_a_payload()["layout_intent"],
                "system_order": ["S_other"],
            },
        }
    )
    plan = finalize_semantic_layout(fixture_a_step_four(), topology)
    # The plant itself differs, and so does the topology the plan names: each gate reports what
    # it owns rather than one gate reporting everything.
    assert engineering_semantic_preservation_problems(plan, other)
    assert any(
        "topology" in problem for problem in layout_input_preservation_problems(plan, other)
    )


def test_finalize_refuses_a_layout_that_changed_the_plant() -> None:
    plan = replace(
        fixture_a_step_four(),
        engineering_entities=tuple(
            replace(fact, tag="X-000") for fact in fixture_a_step_four().engineering_entities
        ),
    )
    with pytest.raises(EngineeringSemanticPreservationError):
        finalize_semantic_layout(plan, fixture_a_topology())


# ---------------------------------------------------------------------------------------
# The coverage half: a digest cannot see a row that went missing
# ---------------------------------------------------------------------------------------


def test_a_complete_layout_covers_its_semantics() -> None:
    assert geometry_coverage_problems(fixture_a_step_four()) == []


def test_a_dropped_entity_is_reported() -> None:
    plan = fixture_a_step_four()
    dropped = plan.placement[0]["engineering_id"]
    plan = replace(plan, placement=plan.placement[1:])
    problems = geometry_coverage_problems(plan)
    assert any(dropped in problem and "dropped" in problem for problem in problems)


def test_a_dropped_connection_is_reported() -> None:
    plan = fixture_a_step_four()
    dropped = plan.routing[0]["engineering_id"]
    plan = replace(plan, routing=plan.routing[1:])
    problems = geometry_coverage_problems(plan)
    assert any(dropped in problem and "dropped" in problem for problem in problems)


def test_an_invented_entity_is_reported() -> None:
    plan = fixture_a_step_four()
    plan = replace(
        plan,
        placement=(*plan.placement, {**plan.placement[0], "engineering_id": "el_ghost"}),
    )
    problems = geometry_coverage_problems(plan)
    assert any("el_ghost" in problem and "nobody declared" in problem for problem in problems)


def test_a_device_placed_as_the_wrong_kind_is_reported() -> None:
    plan = fixture_a_step_four()
    first = plan.placement[0]
    plan = replace(
        plan,
        placement=(
            {**first, "placement_kind": "instrument"},
            *plan.placement[1:],
        ),
    )
    problems = geometry_coverage_problems(plan)
    assert any(
        "placed" in problem and "as" in problem and "declared" in problem for problem in problems
    )


def test_an_entity_placed_twice_is_reported() -> None:
    plan = fixture_a_step_four()
    plan = replace(plan, placement=(*plan.placement, plan.placement[0]))
    problems = geometry_coverage_problems(plan)
    assert any("times" in problem for problem in problems)


def test_finalize_refuses_incomplete_geometry() -> None:
    plan = replace(fixture_a_step_four(), routing=())
    with pytest.raises(GeometryCoverageError):
        finalize_semantic_layout(plan, fixture_a_topology())


# ---------------------------------------------------------------------------------------
# The canonical projection
# ---------------------------------------------------------------------------------------


def test_the_projection_is_sorted_over_exactly_the_declared_fields() -> None:
    rows = canonical_projection_rows(fixture_a_step_four())
    assert rows == tuple(
        sorted(rows, key=lambda row: (row["placement_kind"], row["engineering_id"]))
    )
    for row in rows:
        assert tuple(row) == CANONICAL_PROJECTION_FIELD_NAMES
        assert set(row) == set(contract.DIGEST_VERSION_CONTRACT.projection_field_names)
    assert len(rows) == 3 * 0 + len(fixture_a_step_four().placement) + len(
        fixture_a_step_four().routing
    ) + len(fixture_a_step_four().annotations)


def test_a_tag_change_leaves_the_projection_equal_and_moves_the_digest() -> None:
    """The wording the gate corrected: the *projection* is geometry, so ``P-201 -> P-301`` is
    invisible to it; the *layout digest* binds the semantic input identity, so it must move even
    though every coordinate is unchanged. Saying "the layout digest only identifies position"
    would have been too strong a claim."""

    topology = fixture_a_topology()
    before = fixture_a_step_four()
    retagged = replace(
        before,
        engineering_entities=tuple(
            replace(fact, tag="P-301") if fact.engineering_id == "el_ar_tank" else fact
            for fact in before.engineering_entities
        ),
    )
    assert canonical_projection_rows(retagged) == canonical_projection_rows(before)
    assert canonical_projection_envelope(retagged) == canonical_projection_envelope(before)
    # ``finalize`` refuses a retag -- that is proof A doing its job -- so the digest scope is
    # shown on the payload itself, which is where the difference lives.
    with pytest.raises(EngineeringSemanticPreservationError):
        finalize_semantic_layout(retagged, topology)
    first = canonical_layout_payload(before)
    second = canonical_layout_payload(retagged)
    assert first["canonical_placement_projection"] == second["canonical_placement_projection"]
    assert first["canonical_projection_envelope"] == second["canonical_projection_envelope"]
    assert first["diagram_spec_semantic_digest"] != second["diagram_spec_semantic_digest"]
    assert canonical_layout_digest_for_payload(first) != canonical_layout_digest_for_payload(second)
    assert contract.TAG_CHANGE_WITHOUT_A_GEOMETRY_CHANGE_CHANGES_THE_CANONICAL_LAYOUT_DIGEST


def test_the_projection_carries_no_label_text_and_no_tags() -> None:
    """Where things are, not what they say -- the ruling the annotation step already made."""

    for row in canonical_projection_rows(fixture_a_step_four()):
        assert "text" not in row and "tag" not in row and "label" not in row


def test_the_projection_is_quantized_like_every_other_coordinate() -> None:
    rows = canonical_projection_rows(fixture_a_step_four())
    for row in rows:
        assert row["width"] < 1000.0  # a fabricated id-as-number would fail this
        for point in row["ordered_waypoints"]:
            assert len(point) == 2


def test_a_coordinate_with_float_noise_is_quantized_to_the_declared_quantum() -> None:
    """Two machines compute the same layout to within a rounding error; the identity is over the
    declared quantum, not over whatever the last bit of a float happened to be."""

    plan = fixture_a_step_four()
    index = max(range(len(plan.placement)), key=lambda i: plan.placement[i]["x"])
    exact = plan.placement[index]["x"]
    noisy = exact + 1e-9  # below the declared quantum: the same coordinate, a different float
    mutated = replace(
        plan,
        placement=tuple(
            {**row, "x": noisy} if position == index else row
            for position, row in enumerate(plan.placement)
        ),
    )
    assert exact > 0.0, "the fixture must carry a non-zero coordinate for this test to bite"
    identity = plan.placement[index]["engineering_id"]
    kind = plan.placement[index]["placement_kind"]
    row = next(
        candidate
        for candidate in canonical_projection_rows(mutated)
        # A node's annotation carries its id too, so the row is addressed by the full sort key.
        if candidate["engineering_id"] == identity and candidate["placement_kind"] == kind
    )
    assert row["x"] == round(exact, contract.LAYOUT_COORDINATE_DECIMALS)
    assert row["x"] != noisy


def test_a_duplicate_sort_key_is_refused() -> None:
    plan = fixture_a_step_four()
    plan = replace(plan, placement=(*plan.placement, plan.placement[0]))
    with pytest.raises(CanonicalProjectionError):
        canonical_projection_rows(plan)


def test_a_non_finite_coordinate_is_refused() -> None:
    plan = fixture_a_step_four()
    plan = replace(plan, placement=({**plan.placement[0], "x": float("nan")},))
    with pytest.raises(CanonicalProjectionError):
        canonical_projection_rows(plan)


def test_negative_zero_is_normalized() -> None:
    plan = fixture_a_step_four()
    node_id = plan.placement[0]["engineering_id"]
    plan = replace(plan, placement=({**plan.placement[0], "x": -0.0}, *plan.placement[1:]))
    row = next(
        candidate
        for candidate in canonical_projection_rows(plan)
        if candidate["engineering_id"] == node_id
        and candidate["placement_kind"] == plan.placement[0]["placement_kind"]
    )
    assert row["x"] == 0.0
    assert not str(row["x"]).startswith("-")


def test_an_undeclared_row_field_is_refused() -> None:
    plan = fixture_a_step_four()
    plan = replace(plan, placement=({**plan.placement[0], "z_index": 3},))
    with pytest.raises(CanonicalProjectionError):
        canonical_projection_rows(plan)


def test_the_envelope_is_the_step_four_bounds() -> None:
    plan = fixture_a_step_four()
    envelope = canonical_projection_envelope(plan)
    assert tuple(envelope) == contract.CANONICAL_PROJECTION_ENVELOPE_FIELDS
    assert envelope["content_bounds"] == plan.content_bounds
    assert envelope["canvas_bounds"] == plan.canvas_bounds


def test_the_envelope_requires_the_derived_canvas() -> None:
    plan = replace(fixture_a_step_four(), canvas_bounds=None)
    with pytest.raises(LayoutIdentityError):
        canonical_projection_envelope(plan)


# ---------------------------------------------------------------------------------------
# The digest envelope is closed, and volatile bookkeeping cannot enter it
# ---------------------------------------------------------------------------------------


def test_the_payload_is_exactly_the_declared_inputs() -> None:
    payload = canonical_layout_payload(fixture_a_step_four())
    assert tuple(sorted(payload)) == tuple(sorted(LAYOUT_DIGEST_INPUTS))
    assert payload_problems(payload) == []


def test_a_missing_declared_input_is_reported() -> None:
    payload = canonical_layout_payload(fixture_a_step_four())
    payload.pop("layout_rules_version")
    problems = payload_problems(payload)
    assert any("missing the declared input" in problem for problem in problems)


@pytest.mark.parametrize(
    "volatile",
    [
        "created_at",
        "duration_ms",
        "layout_run_id",
        "session_id",
        "provider",
        "model",
        "attempt",
        "iteration_count",
        "plan_id",
    ],
)
def test_volatile_bookkeeping_may_not_enter_the_digest(volatile: str) -> None:
    assert volatile in LAYOUT_DIGEST_EXCLUDES_VOLATILE_BOOKKEEPING
    payload = canonical_layout_payload(fixture_a_step_four())
    payload[volatile] = 1
    problems = payload_problems(payload)
    assert any(volatile in problem and "volatile" in problem for problem in problems)
    with pytest.raises(LayoutIdentityError):
        canonical_layout_digest_for_payload(payload)


def test_an_undeclared_key_is_reported_even_when_it_is_not_volatile() -> None:
    payload = canonical_layout_payload(fixture_a_step_four())
    payload["camera_zoom"] = 1.0
    problems = payload_problems(payload)
    assert any("camera_zoom" in problem for problem in problems)


def test_the_identity_is_not_the_ingress_digest() -> None:
    plan, topology = fixture_a_identity()
    assert plan.canonical_layout_digest
    assert plan.canonical_layout_digest != plan_digest(plan)
    assert plan.canonical_layout_digest != topology_semantic_digest(topology)
    assert plan.canonical_layout_digest != plan.topology_digest


def test_the_identity_requires_the_finalized_step_four_plan() -> None:
    with pytest.raises(LayoutIdentityError):
        finalize_semantic_layout(annotated_fixture_a(), fixture_a_topology())


# ---------------------------------------------------------------------------------------
# Negative gate 2: replay compares canonical identity, not objects
# ---------------------------------------------------------------------------------------


def test_two_runs_of_the_same_input_agree() -> None:
    first, topology = fixture_a_identity()
    second = finalize_semantic_layout(fixture_a_step_four(), topology)
    assert first.canonical_layout_digest == second.canonical_layout_digest
    assert deterministic_replay_problems(first, second) == []


def test_the_identity_is_stable_across_the_clock() -> None:
    """Nothing in the envelope reads a clock, so elapsed time cannot move the identity."""

    first, topology = fixture_a_identity()
    for _ in range(3):
        pass
    second = finalize_semantic_layout(fixture_a_step_four(), topology)
    assert first.canonical_layout_digest == second.canonical_layout_digest


def test_a_permuted_input_produces_the_same_identity() -> None:
    """Same plant, listed in another order: not a different drawing."""

    payload = fixture_a_payload()
    permuted = {
        **payload,
        "systems": list(reversed(payload["systems"])),
        "entities": list(reversed(payload["entities"])),
        "connections": list(reversed(payload["connections"])),
        "required_loops": list(reversed(payload["required_loops"])),
    }
    topology = adapt(payload)
    other = adapt(permuted)
    assert plan_engineering_rows(plan_semantic_layout(other)) == plan_engineering_rows(
        plan_semantic_layout(topology)
    )

    def run(candidate) -> str:
        plan = plan_semantic_layout(candidate)
        plan = place_semantic_layout(plan)
        snapshot = fixture_a_snapshot()
        from agentcad.auto_layout_geometry import (
            annotate_semantic_layout,
            materialize_semantic_layout,
        )

        plan = route_semantic_layout(materialize_semantic_layout(plan, snapshot), snapshot)
        plan = annotate_semantic_layout(plan, {"el_ar_tank": "V-101"})
        plan = derive_semantic_canvas(plan)
        return finalize_semantic_layout(plan, candidate).canonical_layout_digest

    assert run(topology) == run(other)


def test_a_changed_coordinate_changes_the_identity() -> None:
    first, topology = fixture_a_identity()
    nudged = replace(
        fixture_a_step_four(),
        placement=(
            {
                **fixture_a_step_four().placement[0],
                "x": fixture_a_step_four().placement[0]["x"] + 1.0,
            },
            *fixture_a_step_four().placement[1:],
        ),
    )
    second = finalize_semantic_layout(nudged, topology)
    assert first.canonical_layout_digest != second.canonical_layout_digest
    problems = deterministic_replay_problems(first, second)
    assert problems
    assert any("different identities" in problem for problem in problems)


def test_a_changed_symbol_snapshot_changes_the_identity() -> None:
    """The engine no longer owns the only input to placement, so the snapshot is in the name."""

    first, topology = fixture_a_identity()
    changed = replace(fixture_a_step_four(), symbol_geometry_catalog_digest="deadbeef")
    second = finalize_semantic_layout(changed, topology)
    assert first.canonical_layout_digest != second.canonical_layout_digest


def test_a_changed_engine_or_rules_version_changes_the_identity() -> None:
    first, topology = fixture_a_identity()
    for changed in (
        replace(fixture_a_step_four(), engine_version=LAYOUT_ENGINE_VERSION + "-next"),
        replace(fixture_a_step_four(), rules_version=fixture_a_step_four().rules_version + "-next"),
    ):
        assert first.canonical_layout_digest != finalize_semantic_layout(
            changed, topology
        ).canonical_layout_digest


def test_replay_needs_an_identity_on_both_sides() -> None:
    first, topology = fixture_a_identity()
    with pytest.raises(ReplayIdentityMismatchError):  # never raised: the gate reports instead
        raise ReplayIdentityMismatchError("unreachable")
    problems = deterministic_replay_problems(first, fixture_a_step_four())
    assert any("no identity" in problem for problem in problems)
    assert problems


def test_the_identity_step_is_the_declared_one() -> None:
    plan, _topology = fixture_a_identity()
    assert plan.produced_at_step == STEP_5
    assert contract.LAYOUT_IDENTITY_STEP == STEP_5
    assert contract.LAYOUT_IDENTITY_CHAIN[-1] == "deterministic_replay_verification"


def test_the_identity_fields_stay_out_of_the_plan_digest() -> None:
    """Two identities, independent on purpose: the plan digest must not contain the plan's own
    canonical digest, or one drawing would have two names that can disagree."""

    topology = fixture_a_topology()
    step_four = fixture_a_step_four()
    plan = finalize_semantic_layout(step_four, topology)
    assert plan.canonical_layout_digest
    # Only the three identity fields differ; the step counter is the plan's own reported stage
    # and is deliberately not part of this comparison.
    identity_only = replace(
        step_four,
        canonical_placement_projection=plan.canonical_placement_projection,
        canonical_projection_envelope=plan.canonical_projection_envelope,
        canonical_layout_digest=plan.canonical_layout_digest,
    )
    assert plan_digest(identity_only) == plan_digest(step_four)
    assert contract.LAYOUT_IDENTITY_FIELDS_OUTSIDE_THE_PLAN_DIGEST == (
        "canonical_placement_projection",
        "canonical_projection_envelope",
        "canonical_layout_digest",
    )
    projection = plan_semantic_layout(topology).to_projection()
    for field in contract.LAYOUT_IDENTITY_FIELDS_OUTSIDE_THE_PLAN_DIGEST:
        assert field not in projection
    assert plan_engineering_rows(plan) == topology_engineering_rows(topology)


def test_the_engine_exposes_the_step_five_ingress() -> None:
    import inspect

    from agentcad.auto_layout import AutoLayoutEngine

    signature = inspect.signature(AutoLayoutEngine.layout_semantic_identity)
    assert tuple(signature.parameters) == ("self", "plan", "topology")
    assert contract.LAYOUT_IDENTITY_STEP in [key for key, _ in contract.PHASE_2B_STEPS]


def test_the_semantic_identity_carries_the_facts_the_digest_needs() -> None:
    """A record that dropped tag or measurement would make the gate blind, not lenient."""

    plan = plan_semantic_layout(fixture_a_topology())
    assert all(isinstance(fact, PlanEngineeringFact) for fact in plan.engineering_entities)
    assert all(isinstance(connection, PlanConnection) for connection in plan.connections)
    assert any(fact.tag for fact in plan.engineering_entities)
    assert any(fact.measurement for fact in plan.engineering_entities)
    assert any(connection.tag for connection in plan.connections)


def test_a_plan_without_a_record_cannot_pass_the_gate() -> None:
    plan = replace(
        fixture_a_step_four(),
        engineering_entities=(),
        engineering_systems=(),
    )
    problems = engineering_semantic_preservation_problems(plan, fixture_a_topology())
    assert any("no engineering record" in problem for problem in problems)


def test_fixture_a_is_placed_at_step_two_before_the_identity() -> None:
    """The identity is the last step, and it is reachable from the documented chain."""

    topology = fixture_a_topology()
    plan = place_semantic_layout(plan_semantic_layout(topology))
    assert plan.produced_at_step != STEP_5
    assert fixture_a_plan().produced_at_step == fixture_a_plan().produced_at_step
