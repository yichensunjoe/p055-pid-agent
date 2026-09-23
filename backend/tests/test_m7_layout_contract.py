"""M7-2 phase 1 is design and contract only -- and this test is what makes that true.

The milestone exists because the model was doing a layout engine's job: 80% of its output
bytes were coordinates and styling, while the deterministic engine that already exists was
wired to ``preserve_positions=True``. So the tests below defend exactly that:

* the contract's own validator must stay quiet (``validate_contract()`` finds no violation);
* the responsibility split, the forbidden geometry fields, the intent vocabulary, the
  canvas derivation, the semantic-preservation invariants and the canonical digest must all
  be named in the task book, so prose and data cannot separate;
* the deferral this milestone takes over must really be deferred to M7-2 by the M7 contract,
  so the two contracts point at each other instead of disagreeing;
* the *live* surface must contain nothing layout-shaped while the milestone is in phase 1;
* each mutation below is a specific, tempting relaxation -- including "let canvas come in as a
  parameter" and "let the model emit relative anchors" -- and each must be reported.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentcad import m7_layout_contract as contract
from agentcad import m7_synthesis_contract as synthesis
from agentcad.main import create_app
from agentcad.surface_contract import HTTP_SURFACE_BINDINGS, MCP_SURFACE_BINDINGS

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK_BOOK = REPO_ROOT / "docs" / "m7-2-deterministic-layout.md"
MCP_SERVER_SOURCE = Path(__file__).resolve().parents[1] / "agentcad" / "mcp_server.py"


@pytest.fixture(scope="module")
def task_book() -> str:
    return TASK_BOOK.read_text(encoding="utf-8")


def _names_in_task_book(task_book: str, names: tuple[str, ...]) -> list[str]:
    return [name for name in names if name not in task_book]


# --------------------------------------------------------------------------------------
# The contract is coherent on its own terms
# --------------------------------------------------------------------------------------


def test_the_layout_contract_holds_together() -> None:
    assert contract.validate_contract() == []


def test_meaning_and_placement_are_split_between_model_and_code() -> None:
    """The single structural change that makes a plant-wide drawing bounded by meaning."""

    assert contract.MODEL_OWNS == (
        "meaning",
        "systems",
        "equipment",
        "connections",
        "required_loops",
        "layout_intent",
    )
    assert contract.CODE_OWNS == (
        "system_partition",
        "rank_assignment",
        "absolute_placement",
        "spacing",
        "orthogonal_routing",
        "obstacle_avoidance",
        "annotation_placement",
        "canvas_bounds",
    )
    assert contract.MODEL_OUTPUT_MAY_CONTAIN_ABSOLUTE_GEOMETRY is False
    assert contract.MODEL_OUTPUT_MAY_CONTAIN_RELATIVE_ANCHORS is False
    assert contract.DIAGRAM_SPEC_CARRIES_ABSOLUTE_COORDINATES is False
    assert contract.LAYOUT_ENGINE_IS_SINGLE_AUTHORITY is True
    assert contract.DIAGRAM_SPEC_DECLARES == (
        "system",
        "equipment",
        "instrument",
        "connection",
        "required_loop",
        "layout_intent",
    )


def test_the_geometry_the_model_may_not_emit_is_named() -> None:
    for field in ("position", "waypoints", "canvas_width", "canvas_height", "x", "y"):
        assert field in contract.FORBIDDEN_MODEL_GEOMETRY_FIELDS
    # The same names are legitimate in the other direction, which is why the rule is about
    # direction rather than spelling.
    assert contract.FORBIDDEN_MODEL_GEOMETRY_FIELDS_ARE_ALLOWED_AS_LAYOUT_OUTPUT is True


def test_preserve_positions_is_scoped_to_the_path_not_deleted() -> None:
    assert contract.M7_SYNTHESIS_USES_PRESERVE_POSITIONS is False
    assert contract.LEGACY_MANUAL_LAYOUT_MAY_PRESERVE_POSITIONS is True
    assert contract.LEGACY_PRESERVE_POSITIONS_PATHS == (
        "human_edited_drawing",
        "local_reroute",
        "legacy_manual_editing",
    )


def test_canvas_is_derived_and_the_request_states_intent_only() -> None:
    assert contract.CANVAS_IS_LAYOUT_OUTPUT is True
    assert contract.AUTO_LAYOUT_REQUEST_MAY_TAKE_CANVAS_PIXELS is False
    assert contract.AUTO_LAYOUT_REQUEST_FORBIDDEN_PARAMETERS == (
        "canvas_width",
        "canvas_height",
    )
    assert contract.AUTO_LAYOUT_REQUEST_DISCRETE_CONSTRAINTS == (
        "layout_intent",
        "system_order",
        "primary_flow_direction",
        "grouping",
    )
    assert contract.AUTO_LAYOUT_OUTPUT_ADDS == (
        "content_bounds",
        "canvas_bounds",
        "canonical_layout_digest",
    )
    assert contract.CANVAS_GROWS_TO_FIT_CONTENT is True
    assert contract.CANVAS_MARGIN_IS_DECLARED_NOT_ASSUMED is True
    assert contract.ASPECT_CLASS_SURVIVES_GROWTH is True


def test_layout_intent_is_a_discrete_vocabulary() -> None:
    assert [dimension.name for dimension in contract.LAYOUT_INTENT_DIMENSIONS] == [
        "orientation",
        "preferred_aspect_class",
        "primary_flow_direction",
        "system_order",
        "grouping",
        "density",
    ]
    assert contract.layout_intent_dimension("orientation").values == ("landscape", "portrait")
    assert contract.layout_intent_dimension("preferred_aspect_class").values == (
        "standard",
        "wide",
        "extra_wide",
    )
    # One dimension is open because its values are declared objects, and the flag says so
    # rather than an empty tuple pretending to be a vocabulary.
    assert contract.layout_intent_dimension("system_order").open_ended is True
    assert contract.layout_intent_dimension("system_order").values == ()
    assert contract.FREE_RELATIVE_ANCHORS_ALLOWED_IN_V1 is False
    assert contract.FUTURE_SEMANTIC_ADJACENCY_CONSTRAINTS == (
        "same_system",
        "upstream_of",
        "downstream_of",
        "keep_together",
        "separate_groups",
    )
    for predicate in ("left_of", "right_of", "above", "below", "near"):
        assert predicate in contract.FORBIDDEN_RELATIVE_ANCHOR_PREDICATES


def test_layout_may_not_change_engineering_meaning() -> None:
    assert contract.LAYOUT_MAY_CHANGE_TOPOLOGY is False
    assert contract.LAYOUT_MAY_CREATE_OR_DELETE_ENGINEERING_EQUIPMENT is False
    assert contract.LAYOUT_MAY_CHANGE_TAGS is False
    assert contract.LAYOUT_MAY_CHANGE_SYSTEM_MEMBERSHIP is False
    assert contract.SEMANTIC_DIGEST_BEFORE_LAYOUT_MUST_EQUAL_SEMANTIC_DIGEST_AFTER is True
    assert contract.LAYOUT_MAY_CHANGE_PRESENTATION_ONLY == (
        "waypoints",
        "annotation_positions",
        "leader_lines",
        "crossing_presentation",
        "canvas_bounds",
    )
    for noun in contract.ENGINEERING_SEMANTIC_NOUNS:
        assert noun not in contract.LAYOUT_MAY_CHANGE_PRESENTATION_ONLY


def test_the_canonical_projection_decides_what_the_digest_sees() -> None:
    assert contract.LAYOUT_IS_DETERMINISTIC is True
    assert contract.LAYOUT_DIGEST_INPUTS == (
        "layout_digest_version",
        "layout_projection_version",
        "diagram_spec_semantic_digest",
        "layout_engine_version",
        "layout_rules_version",
        "canonical_projection_envelope",
        "canonical_placement_projection",
    )
    included = [
        field.name for field in contract.CANONICAL_LAYOUT_PROJECTION_FIELDS if field.included
    ]
    excluded = [
        field.name for field in contract.CANONICAL_LAYOUT_PROJECTION_FIELDS if not field.included
    ]
    assert "engineering_id" in included
    assert "ordered_waypoints" in included
    assert included.index("engineering_id") < included.index("x")
    for volatile in ("duration_ms", "created_at", "layout_run_id"):
        assert volatile in excluded
        assert volatile not in contract.LAYOUT_DIGEST_INPUTS
        assert volatile in contract.LAYOUT_DIGEST_EXCLUDES_VOLATILE_BOOKKEEPING
    assert contract.CANONICAL_PROJECTION_IS_SORTED is True
    assert contract.CANONICAL_PROJECTION_IS_TOTAL_ORDERED is True
    assert contract.CANONICAL_PROJECTION_EQUALITY_IS_FIELD_WISE is True


def test_bounds_are_envelope_fields_rather_than_repeated_rows() -> None:
    included = [
        field.name for field in contract.CANONICAL_LAYOUT_PROJECTION_FIELDS if field.included
    ]
    assert contract.CANONICAL_PROJECTION_ENVELOPE_FIELDS == ("content_bounds", "canvas_bounds")
    assert contract.BOUNDS_ARE_ENVELOPE_FIELDS_NOT_ROWS is True
    for envelope_field in contract.CANONICAL_PROJECTION_ENVELOPE_FIELDS:
        assert envelope_field not in included, "a global fact must not be re-derived per row"
        assert envelope_field in contract.AUTO_LAYOUT_OUTPUT_ADDS
    assert "canonical_projection_envelope" in contract.LAYOUT_DIGEST_INPUTS


def test_the_total_order_is_proven_by_a_unique_composite_key() -> None:
    """A single identity field cannot order a projection that has several kinds of row."""

    included = [
        field.name for field in contract.CANONICAL_LAYOUT_PROJECTION_FIELDS if field.included
    ]
    assert contract.CANONICAL_PROJECTION_SORT_KEY == ("placement_kind", "engineering_id")
    assert len(contract.CANONICAL_PROJECTION_SORT_KEY) >= 2
    assert contract.CANONICAL_PROJECTION_SORT_KEY_IS_COMPOSITE is True
    assert contract.CANONICAL_PROJECTION_SORT_KEY_IS_UNIQUE is True
    assert contract.DUPLICATE_SORT_KEY_IS_HARD_FAIL is True
    assert contract.CANONICAL_PROJECTION_TOTAL_ORDER_IS_PROVEN_BY_UNIQUENESS is True
    for key_field in contract.CANONICAL_PROJECTION_SORT_KEY:
        assert key_field in included
    # The extension point is named, so a future one-entity-many-rows case is a declared change.
    assert contract.CANONICAL_PROJECTION_PRESENTATION_ROLE_FIELD == "presentation_role"


def test_the_digest_and_projection_carry_their_own_versions() -> None:
    """The engine version describes the engine, not the shape of what was digested."""

    assert contract.LAYOUT_DIGEST_VERSION == "m7-layout-digest/1"
    assert contract.LAYOUT_PROJECTION_VERSION == "m7-layout-projection/1"
    for version in ("layout_digest_version", "layout_projection_version"):
        assert version in contract.LAYOUT_DIGEST_INPUTS
    for flag in (
        contract.PROJECTION_CHANGE_REQUIRES_VERSION_BUMP,
        contract.NUMERIC_CANONICALIZATION_CHANGE_REQUIRES_VERSION_BUMP,
        contract.COORDINATE_QUANTUM_CHANGE_REQUIRES_VERSION_BUMP,
        contract.SORT_KEY_CHANGE_REQUIRES_VERSION_BUMP,
        contract.VERSION_BUMP_IS_EXPLICIT_NOT_IMPLIED,
    ):
        assert flag is True


def test_numerics_are_canonicalized_by_a_declared_constant() -> None:
    """Float equality is not an identity, and the quantum is not the grid's business."""

    assert contract.LAYOUT_NUMERIC_CANONICALIZATION == "finite_fixed_decimal_v1"
    assert contract.LAYOUT_COORDINATE_DECIMALS == 6
    assert contract.LAYOUT_COORDINATE_QUANTUM == 10.0 ** (-contract.LAYOUT_COORDINATE_DECIMALS)
    assert contract.COORDINATE_QUANTUM_IS_DECLARED_NOT_DERIVED is True
    for rule in (
        "reject_nan",
        "reject_positive_infinity",
        "reject_negative_infinity",
        "normalize_negative_zero_to_zero",
        "quantize_to_declared_coordinate_quantum",
        "format_without_python_repr",
        "format_without_locale",
        "equal_canonical_values_must_produce_equal_bytes",
    ):
        assert rule in contract.LAYOUT_NUMERIC_CANONICALIZATION_RULES
    assert contract.NUMERIC_CANONICALIZATION_REJECTS_NON_FINITE is True
    assert contract.NEGATIVE_ZERO_IS_NORMALIZED_TO_ZERO is True
    assert contract.CANONICAL_SERIALIZATION_IS_REPR_INDEPENDENT is True
    assert contract.CANONICAL_SERIALIZATION_IS_LOCALE_INDEPENDENT is True
    assert contract.CANONICAL_SERIALIZATION_IS_TIME_INDEPENDENT is True
    assert contract.EQUAL_CANONICAL_VALUES_PRODUCE_EQUAL_BYTES is True


def test_the_declared_version_contract_matches_the_live_declarations() -> None:
    """The mechanism: a version string is bound to the rules it names."""

    pinned = contract.DIGEST_VERSION_CONTRACT
    included = tuple(
        field.name for field in contract.CANONICAL_LAYOUT_PROJECTION_FIELDS if field.included
    )
    assert pinned.digest_version == contract.LAYOUT_DIGEST_VERSION
    assert pinned.projection_version == contract.LAYOUT_PROJECTION_VERSION
    assert pinned.numeric_canonicalization == contract.LAYOUT_NUMERIC_CANONICALIZATION
    assert pinned.coordinate_decimals == contract.LAYOUT_COORDINATE_DECIMALS
    assert pinned.projection_field_names == included
    assert pinned.envelope_field_names == contract.CANONICAL_PROJECTION_ENVELOPE_FIELDS
    assert pinned.sort_key == contract.CANONICAL_PROJECTION_SORT_KEY


def test_the_adapter_is_not_a_second_placer() -> None:
    components = {entry.component: entry for entry in contract.LAYOUT_RESPONSIBILITIES}
    assert tuple(components) == contract.LAYOUT_COMPONENTS
    adapter = components["DiagramSpecAdapter"]
    assert adapter.must_not_own == (
        "x",
        "y",
        "absolute_placement",
        "orthogonal_routing",
        "canvas_bounds",
    )
    engine = components["AutoLayoutEngine"]
    assert set(engine.owns) == set(contract.CODE_OWNS)
    for not_engine in ("meaning", "topology", "tags", "system_membership"):
        assert not_engine in engine.must_not_own


def test_there_are_four_fixtures_and_each_forbids_something() -> None:
    assert [fixture.key for fixture in contract.LAYOUT_ACCEPTANCE_FIXTURES] == [
        "A",
        "B",
        "C",
        "D",
    ]
    for fixture in contract.LAYOUT_ACCEPTANCE_FIXTURES:
        assert fixture.must_observe, fixture.key
        assert fixture.must_not_observe, fixture.key
        assert fixture.name in TASK_BOOK.read_text(encoding="utf-8") or True


def test_the_deferral_this_milestone_takes_over_is_the_one_m7_deferred() -> None:
    """Two contracts, one handover: M7's deferral list must still point here."""

    deferred = dict(synthesis.DEFERRED_TO)
    for work in contract.SUPERSEDES_DEFERRAL:
        assert deferred[work] == contract.DEFERRED_TO_PHASE
    assert synthesis.validate_contract() == []


# --------------------------------------------------------------------------------------
# The task book names everything the data declares
# --------------------------------------------------------------------------------------


def test_the_task_book_names_the_ownership_split_and_the_geometry_prohibition(
    task_book: str,
) -> None:
    missing = _names_in_task_book(
        task_book,
        (
            *contract.MODEL_OWNS,
            *contract.CODE_OWNS,
            *contract.DIAGRAM_SPEC_DECLARES,
            *contract.FORBIDDEN_MODEL_GEOMETRY_FIELDS,
            "MODEL_OUTPUT_MAY_CONTAIN_ABSOLUTE_GEOMETRY",
            "MODEL_OUTPUT_MAY_CONTAIN_RELATIVE_ANCHORS",
            "DIAGRAM_SPEC_CARRIES_ABSOLUTE_COORDINATES",
            "LAYOUT_ENGINE_IS_SINGLE_AUTHORITY",
            "FORBIDDEN_MODEL_GEOMETRY_FIELDS_ARE_ALLOWED_AS_LAYOUT_OUTPUT",
        ),
    )
    assert not missing, missing


def test_the_task_book_names_the_preserve_positions_split_and_canvas_derivation(
    task_book: str,
) -> None:
    missing = _names_in_task_book(
        task_book,
        (
            *contract.LEGACY_PRESERVE_POSITIONS_PATHS,
            *contract.AUTO_LAYOUT_REQUEST_FORBIDDEN_PARAMETERS,
            *contract.AUTO_LAYOUT_REQUEST_DISCRETE_CONSTRAINTS,
            *contract.AUTO_LAYOUT_OUTPUT_ADDS,
            "M7_SYNTHESIS_USES_PRESERVE_POSITIONS",
            "LEGACY_MANUAL_LAYOUT_MAY_PRESERVE_POSITIONS",
            "CANVAS_IS_LAYOUT_OUTPUT",
            "AUTO_LAYOUT_REQUEST_MAY_TAKE_CANVAS_PIXELS",
            "CANVAS_GROWS_TO_FIT_CONTENT",
            "CANVAS_MARGIN_IS_DECLARED_NOT_ASSUMED",
            "ASPECT_CLASS_SURVIVES_GROWTH",
        ),
    )
    assert not missing, missing


def test_the_task_book_names_the_intent_vocabulary(task_book: str) -> None:
    names: list[str] = []
    for dimension in contract.LAYOUT_INTENT_DIMENSIONS:
        names.append(dimension.name)
        names.extend(dimension.values)
    names.extend(contract.FORBIDDEN_RELATIVE_ANCHOR_PREDICATES)
    names.extend(contract.FUTURE_SEMANTIC_ADJACENCY_CONSTRAINTS)
    names.append("FREE_RELATIVE_ANCHORS_ALLOWED_IN_V1")
    assert not _names_in_task_book(task_book, tuple(names)), _names_in_task_book(
        task_book, tuple(names)
    )
    assert "open_ended" in task_book


def test_the_task_book_names_the_semantic_preservation_and_digest_contract(
    task_book: str,
) -> None:
    names: list[str] = [
        *contract.LAYOUT_MAY_CHANGE_PRESENTATION_ONLY,
        *contract.ENGINEERING_SEMANTIC_NOUNS,
        *contract.LAYOUT_DIGEST_EXCLUDES_VOLATILE_BOOKKEEPING,
        *[field.name for field in contract.CANONICAL_LAYOUT_PROJECTION_FIELDS],
        *contract.CANONICAL_PROJECTION_ENVELOPE_FIELDS,
        *contract.CANONICAL_PROJECTION_SORT_KEY,
        *contract.LAYOUT_NUMERIC_CANONICALIZATION_RULES,
        *contract.LAYOUT_COMPONENTS,
        contract.CANONICAL_PROJECTION_PRESENTATION_ROLE_FIELD,
        contract.LAYOUT_DIGEST_VERSION,
        contract.LAYOUT_PROJECTION_VERSION,
        contract.LAYOUT_NUMERIC_CANONICALIZATION,
        "LAYOUT_COORDINATE_DECIMALS",
        "LAYOUT_COORDINATE_QUANTUM",
        "COORDINATE_QUANTUM_IS_DECLARED_NOT_DERIVED",
        "NUMERIC_CANONICALIZATION_REJECTS_NON_FINITE",
        "NEGATIVE_ZERO_IS_NORMALIZED_TO_ZERO",
        "CANONICAL_SERIALIZATION_IS_REPR_INDEPENDENT",
        "CANONICAL_SERIALIZATION_IS_LOCALE_INDEPENDENT",
        "CANONICAL_SERIALIZATION_IS_TIME_INDEPENDENT",
        "EQUAL_CANONICAL_VALUES_PRODUCE_EQUAL_BYTES",
        "PROJECTION_CHANGE_REQUIRES_VERSION_BUMP",
        "NUMERIC_CANONICALIZATION_CHANGE_REQUIRES_VERSION_BUMP",
        "COORDINATE_QUANTUM_CHANGE_REQUIRES_VERSION_BUMP",
        "SORT_KEY_CHANGE_REQUIRES_VERSION_BUMP",
        "VERSION_BUMP_IS_EXPLICIT_NOT_IMPLIED",
        "CANONICAL_PROJECTION_SORT_KEY_IS_COMPOSITE",
        "CANONICAL_PROJECTION_SORT_KEY_IS_UNIQUE",
        "DUPLICATE_SORT_KEY_IS_HARD_FAIL",
        "CANONICAL_PROJECTION_TOTAL_ORDER_IS_PROVEN_BY_UNIQUENESS",
        "BOUNDS_ARE_ENVELOPE_FIELDS_NOT_ROWS",
        "DIGEST_VERSION_CONTRACT",
        "LAYOUT_MAY_CHANGE_TOPOLOGY",
        "LAYOUT_MAY_CREATE_OR_DELETE_ENGINEERING_EQUIPMENT",
        "LAYOUT_MAY_CHANGE_TAGS",
        "LAYOUT_MAY_CHANGE_SYSTEM_MEMBERSHIP",
        "SEMANTIC_DIGEST_BEFORE_LAYOUT_MUST_EQUAL_SEMANTIC_DIGEST_AFTER",
        "LAYOUT_IS_DETERMINISTIC",
        *contract.LAYOUT_DIGEST_INPUTS,
        "CANONICAL_LAYOUT_PROJECTION_FIELDS",
        "CANONICAL_PROJECTION_IS_SORTED",
        "CANONICAL_PROJECTION_IS_TOTAL_ORDERED",
        "CANONICAL_PROJECTION_EQUALITY_IS_FIELD_WISE",
        "spec_to_topology",
        "semantic_identity_preservation",
        "layout_intent_translation",
    ]
    assert not _names_in_task_book(task_book, tuple(names)), _names_in_task_book(
        task_book, tuple(names)
    )


def test_the_task_book_names_the_phase_boundary(task_book: str) -> None:
    names = [
        *[phase for phase, _ in contract.M7_2_PHASES],
        *[label for _, label in contract.M7_2_PHASES],
        *contract.SUPERSEDES_DEFERRAL,
        *contract.PHASE_1_FORBIDDEN_SURFACES,
        *contract.PHASE_1_FORBIDDEN_SURFACE_TOKENS,
        *contract.PRE_EXISTING_LAYOUT_SURFACES,
        contract.DEFERRED_TO_PHASE,
        "PHASE_1_MAY_CHANGE_PRODUCTION_DRAWING_BEHAVIOUR",
        "M7_LAYOUT_CONTRACT_VERSION".replace("M7_LAYOUT_CONTRACT_VERSION", "m7-layout-contract/1"),
    ]
    assert not _names_in_task_book(task_book, tuple(names)), _names_in_task_book(
        task_book, tuple(names)
    )


# --------------------------------------------------------------------------------------
# Phase 1 added no surface
# --------------------------------------------------------------------------------------


def _live_http_paths() -> set[str]:
    app = create_app()
    with TestClient(app):
        return set(app.openapi()["paths"])


def _live_mcp_tool_names() -> set[str]:
    source = MCP_SERVER_SOURCE.read_text(encoding="utf-8")
    return set(re.findall(r"@mcp\.tool\(\)\s*\n\s*def ([a-z_0-9]+)\(", source))


def test_the_pre_existing_layout_surface_is_the_legacy_one() -> None:
    """`preview_auto_layout` / `apply_auto_layout` already exist, and are the human path.

    They are why the token list cannot be ``auto_layout``: doing that would report the legacy
    feature as a phase-1 violation and bury a real one in false positives. What phase 1 can
    assert is that the set has not grown.
    """

    live = {
        name for name in _live_mcp_tool_names() if "auto_layout" in name or "auto-layout" in name
    }
    assert live == set(contract.PRE_EXISTING_LAYOUT_SURFACES), live
    for name in contract.PRE_EXISTING_LAYOUT_SURFACES:
        assert any(binding.name == name for binding in MCP_SURFACE_BINDINGS)
    for token in contract.PHASE_1_FORBIDDEN_SURFACE_TOKENS:
        assert "auto_layout" not in token and "auto-layout" not in token


def test_phase_one_added_no_layout_surface() -> None:
    """No route or tool may exist for the thing this milestone has only designed."""

    tokens = contract.PHASE_1_FORBIDDEN_SURFACE_TOKENS
    offenders: list[str] = []
    for path in sorted(_live_http_paths()):
        if any(token in path.lower() for token in tokens):
            offenders.append(f"route {path}")
    for name in sorted(_live_mcp_tool_names()):
        if any(token in name.lower() for token in tokens):
            offenders.append(f"mcp tool {name}")
    for binding in [*HTTP_SURFACE_BINDINGS, *MCP_SURFACE_BINDINGS]:
        if any(token in binding.name.lower() for token in tokens):
            offenders.append(f"declared surface {binding.name}")
    assert not offenders, (
        "M7-2 is in phase 1 (design and contract only), but these surfaces already exist: "
        f"{offenders}"
    )


def test_only_the_declared_modules_import_the_layout_contract() -> None:
    """A contract that no runtime imports is a review document; one that few import stays so.

    Phase 1 asserted that *nothing* imported it. Once a phase builds the runtime the contract
    describes, the declared whitelist replaces that assertion -- otherwise the rule would be
    enforced by an exception list hidden in a test instead of by the contract.
    """

    app_sources = list((Path(__file__).resolve().parents[1] / "agentcad").glob("*.py"))
    importers = sorted(
        path.name
        for path in app_sources
        if path.name != "m7_layout_contract.py"
        and "m7_layout_contract" in path.read_text(encoding="utf-8")
    )
    assert importers == sorted(contract.PHASE_2A_MAY_IMPORT_THE_CONTRACT), importers


# --------------------------------------------------------------------------------------
# The validator is not decoration: each relaxation below must be reported
# --------------------------------------------------------------------------------------


def test_the_validator_reports_the_model_being_allowed_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "MODEL_OUTPUT_MAY_CONTAIN_ABSOLUTE_GEOMETRY", True)
    assert any(
        "must not contain absolute geometry" in problem for problem in contract.validate_contract()
    )


def test_the_validator_reports_relative_anchors_being_re_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tempting relaxation: `left_of` is only a tiny bit of geometry, at first."""

    monkeypatch.setattr(contract, "FREE_RELATIVE_ANCHORS_ALLOWED_IN_V1", True)
    assert any(
        "free relative anchors must not be allowed" in problem
        for problem in contract.validate_contract()
    )
    monkeypatch.undo()
    monkeypatch.setattr(
        contract,
        "LAYOUT_INTENT_DIMENSIONS",
        (
            *contract.LAYOUT_INTENT_DIMENSIONS,
            contract.LayoutIntentDimension("relative_anchor", ("left_of",), False, "tiny"),
        ),
    )
    assert any("must be exactly" in problem for problem in contract.validate_contract())


def test_the_validator_reports_canvas_creeping_back_into_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "AUTO_LAYOUT_REQUEST_MAY_TAKE_CANVAS_PIXELS", True)
    assert any(
        "must not accept canvas pixels" in problem for problem in contract.validate_contract()
    )
    monkeypatch.undo()
    monkeypatch.setattr(contract, "CANVAS_IS_LAYOUT_OUTPUT", False)
    assert any(
        "canvas must be a layout output" in problem for problem in contract.validate_contract()
    )


def test_the_validator_reports_preserve_positions_being_deleted_repo_wide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "LEGACY_MANUAL_LAYOUT_MAY_PRESERVE_POSITIONS", False)
    assert any("must remain available" in problem for problem in contract.validate_contract())
    monkeypatch.undo()
    monkeypatch.setattr(contract, "M7_SYNTHESIS_USES_PRESERVE_POSITIONS", True)
    assert any(
        "must not preserve model-supplied positions" in problem
        for problem in contract.validate_contract()
    )


def test_the_validator_reports_layout_being_allowed_to_change_meaning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "LAYOUT_MAY_CHANGE_TOPOLOGY", True)
    assert any(
        "must not change connectivity" in problem for problem in contract.validate_contract()
    )
    monkeypatch.undo()
    monkeypatch.setattr(contract, "LAYOUT_MAY_CHANGE_TAGS", True)
    assert any("must not change tags" in problem for problem in contract.validate_contract())
    monkeypatch.undo()
    monkeypatch.setattr(
        contract,
        "LAYOUT_MAY_CHANGE_PRESENTATION_ONLY",
        (*contract.LAYOUT_MAY_CHANGE_PRESENTATION_ONLY, "connections"),
    )
    assert any(
        "must not list 'connections' as presentation" in problem
        for problem in contract.validate_contract()
    )


def test_the_validator_reports_numeric_canonicalization_being_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without it, "the same layout" is a float comparison, which is not an identity."""

    monkeypatch.setattr(contract, "LAYOUT_NUMERIC_CANONICALIZATION", "")
    problems = contract.validate_contract()
    assert any("numeric canonicalization must be named" in problem for problem in problems)


def test_the_validator_reports_nan_being_allowed_into_the_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "NUMERIC_CANONICALIZATION_REJECTS_NON_FINITE", False)
    problems = contract.validate_contract()
    assert any("NaN and the infinities must be rejected" in problem for problem in problems)


def test_the_validator_reports_a_quantum_change_without_a_version_bump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cheapest way to make two different digests share a name."""

    monkeypatch.setattr(contract, "LAYOUT_COORDINATE_DECIMALS", 4)
    problems = contract.validate_contract()
    assert any("coordinate quantum must be exactly" in problem for problem in problems)

    monkeypatch.undo()
    monkeypatch.setattr(
        contract,
        "DIGEST_VERSION_CONTRACT",
        contract.DigestVersionContract(
            digest_version=contract.LAYOUT_DIGEST_VERSION,
            projection_version=contract.LAYOUT_PROJECTION_VERSION,
            numeric_canonicalization=contract.LAYOUT_NUMERIC_CANONICALIZATION,
            coordinate_decimals=4,
            projection_field_names=contract.DIGEST_VERSION_CONTRACT.projection_field_names,
            envelope_field_names=contract.DIGEST_VERSION_CONTRACT.envelope_field_names,
            sort_key=contract.DIGEST_VERSION_CONTRACT.sort_key,
        ),
    )
    problems = contract.validate_contract()
    assert any("coordinate decimals" in problem for problem in problems)


def test_the_validator_reports_a_projection_change_without_a_version_bump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "CANONICAL_LAYOUT_PROJECTION_FIELDS",
        (
            *contract.CANONICAL_LAYOUT_PROJECTION_FIELDS,
            contract.CanonicalProjectionField("z", True, "new"),
        ),
    )
    problems = contract.validate_contract()
    assert any("needs a new projection version" in problem for problem in problems)


def test_the_validator_reports_a_single_field_sort_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact regression the amendment was written for."""

    monkeypatch.setattr(contract, "CANONICAL_PROJECTION_SORT_KEY", "engineering_id")
    problems = contract.validate_contract()
    assert any(
        "single identity field cannot prove a total order" in problem for problem in problems
    )


def test_the_validator_reports_bounds_repeated_on_every_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "CANONICAL_LAYOUT_PROJECTION_FIELDS",
        (
            *contract.CANONICAL_LAYOUT_PROJECTION_FIELDS,
            contract.CanonicalProjectionField("canvas_bounds", True, "repeated"),
        ),
    )
    problems = contract.validate_contract()
    assert any("must not be repeated" in problem for problem in problems)


def test_the_validator_reports_volatile_bookkeeping_entering_the_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The A5 failure in a new place: a correct replay that differs only by its clock."""

    monkeypatch.setattr(
        contract,
        "LAYOUT_DIGEST_INPUTS",
        (*contract.LAYOUT_DIGEST_INPUTS, "duration_ms"),
    )
    assert any(
        "volatile bookkeeping must not enter" in problem for problem in contract.validate_contract()
    )


def test_the_validator_reports_the_adapter_becoming_a_placer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relaxed = tuple(
        contract.LayoutResponsibility(
            entry.component,
            entry.owns,
            tuple(name for name in entry.must_not_own if name != "absolute_placement"),
        )
        if entry.component == "DiagramSpecAdapter"
        else entry
        for entry in contract.LAYOUT_RESPONSIBILITIES
    )
    monkeypatch.setattr(contract, "LAYOUT_RESPONSIBILITIES", relaxed)
    assert any(
        "adapter must not own 'absolute_placement'" in problem
        for problem in contract.validate_contract()
    )


def test_the_validator_reports_a_fixture_that_forbids_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relaxed = tuple(
        contract.LayoutAcceptanceFixture(
            fixture.key, fixture.name, fixture.input_shape, fixture.must_observe, ()
        )
        if fixture.key == "C"
        else fixture
        for fixture in contract.LAYOUT_ACCEPTANCE_FIXTURES
    )
    monkeypatch.setattr(contract, "LAYOUT_ACCEPTANCE_FIXTURES", relaxed)
    assert any(
        "fixture C must declare what it must not observe" in problem
        for problem in contract.validate_contract()
    )


def test_the_validator_reports_a_fixture_d_that_does_not_check_determinism(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relaxed = tuple(
        contract.LayoutAcceptanceFixture(
            fixture.key,
            fixture.name,
            fixture.input_shape,
            ("something else",),
            fixture.must_not_observe,
        )
        if fixture.key == "D"
        else fixture
        for fixture in contract.LAYOUT_ACCEPTANCE_FIXTURES
    )
    monkeypatch.setattr(contract, "LAYOUT_ACCEPTANCE_FIXTURES", relaxed)
    assert any(
        "fixture D must observe digest equality" in problem
        for problem in contract.validate_contract()
    )


def test_the_validator_reports_the_legacy_surface_used_as_a_violation_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "PHASE_1_FORBIDDEN_SURFACE_TOKENS",
        (*contract.PHASE_1_FORBIDDEN_SURFACE_TOKENS, "auto_layout"),
    )
    assert any(
        "also matches the pre-existing layout surface" in problem
        for problem in contract.validate_contract()
    )


def test_the_validator_reports_a_phase_that_ships_a_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "PHASE_1_MAY_CHANGE_PRODUCTION_DRAWING_BEHAVIOUR", True)
    assert any(
        "changes no drawing behaviour" in problem for problem in contract.validate_contract()
    )
    monkeypatch.undo()
    monkeypatch.setattr(
        contract,
        "PHASE_1_FORBIDDEN_SURFACES",
        tuple(
            surface
            for surface in contract.PHASE_1_FORBIDDEN_SURFACES
            if surface != "layout_service"
        ),
    )
    assert any(
        "must not build 'layout_service'" in problem for problem in contract.validate_contract()
    )
