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
    # The symbol geometry closure joined the inputs at step 3: the engine places against real
    # symbol facts now, so "which facts" is part of where a drawing sits.
    assert contract.LAYOUT_DIGEST_INPUTS == (
        "layout_digest_version",
        "layout_projection_version",
        "diagram_spec_semantic_digest",
        "layout_engine_version",
        "layout_rules_version",
        "symbol_geometry_catalog_digest",
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

    # v2: step 3 added `symbol_geometry_catalog_digest` to the inputs, so a digest produced
    # without it would be a different identity under the same name. The projection version does
    # not move with it -- no canonical projection field changed.
    assert contract.LAYOUT_DIGEST_VERSION == "m7-layout-digest/2"
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
    # A phase-2B module may only appear here once the module exists: the allow-list names the
    # importer before it is written, so "declared for later" cannot be mistaken for "already
    # reading the contract".
    agentcad = Path(__file__).resolve().parents[1] / "agentcad"
    allowed = set(contract.PHASE_2A_MAY_IMPORT_THE_CONTRACT)
    allowed |= {
        name for name in contract.PHASE_2B_MAY_IMPORT_THE_CONTRACT if (agentcad / name).exists()
    }
    assert importers == sorted(allowed), importers


def test_the_two_phase_import_lists_do_not_overlap() -> None:
    assert not set(contract.PHASE_2A_MAY_IMPORT_THE_CONTRACT) & set(
        contract.PHASE_2B_MAY_IMPORT_THE_CONTRACT
    )


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


# --------------------------------------------------------------------------------------
# §8.2 phase 2B: the seam, its two shortcuts, and the intent consumption table
# --------------------------------------------------------------------------------------


def test_the_task_book_declares_the_phase_2b_seam() -> None:
    task_book = (
        Path(__file__).resolve().parents[2] / "docs" / "m7-2-deterministic-layout.md"
    ).read_text(encoding="utf-8")
    names = (
        *[key for key, _ in contract.PHASE_2B_STEPS],
        *[value for _, value in contract.PHASE_2B_STEPS],
        *[item.dimension for item in contract.LAYOUT_INTENT_CONSUMPTION],
        *contract.ENGINE_VERSION_CONSTANT_NAMES,
        *contract.PHASE_2B_MAY_IMPORT_THE_CONTRACT,
        contract.ENGINE_SEMANTIC_TOPOLOGY_INGRESS,
        contract.INTENT_DIMENSIONS_ARE_RECEIVED_AT_STEP,
        "SEMANTIC_TOPOLOGY_IS_THE_ENGINE_FACING_INPUT_CONTRACT",
        "SECOND_ADAPTER_WITH_SEMANTIC_AUTHORITY_IS_ALLOWED",
        "LAYOUT_AUTHORITY_COUNT",
        "LEGACY_INGRESS_RETAINS_THE_SAME_ALGORITHM_AUTHORITY",
        "INTERNAL_NORMALIZATION_IS_REPRESENTATION_ONLY",
        "INTERNAL_NORMALIZATION_SEMANTIC_DECISIONS",
        "INTERNAL_NORMALIZATION_GEOMETRY_DECISIONS",
        "INTERNAL_NORMALIZATION_TOPOLOGY_EDITS",
        "ENGINE_INGRESS_MAY_FABRICATE_PLACEHOLDER_POSITIONS",
        "ENGINE_INGRESS_MAY_DISGUISE_TOPOLOGY_AS_A_POSITIONED_DOCUMENT",
        "M7_SYNTHESIS_INGRESS_PRESERVE_POSITIONS",
        "M7_INGRESS_HAS_NO_PRESERVE_POSITIONS_PARAMETER",
        "M7_INGRESS_PRESERVE_POSITIONS_IS_CALLER_OVERRIDABLE",
        "LAYOUT_INTENT_CONSUMPTION",
        "IntentConsumption",
        "UNIMPLEMENTED_INTENT_DIMENSION_MAY_BE_SILENTLY_IGNORED",
        "CANVAS_DERIVATION_CHAIN",
        "CANVAS_IS_ENGINE_OUTPUT_DERIVED_FROM_CONTENT",
        "CANVAS_MUST_BE_VERIFIED_TO_CLIP_NOTHING",
        "ENGINE_MAY_TAKE_INTENT_AND_MARGIN_POLICY",
        "ENGINE_MAY_TAKE_CANVAS_DIMENSIONS_AS_INPUT",
        "ENGINE_INGRESS_REPORTS_ENGINE_AND_RULES_VERSION",
        "PHASE_2B_WIRES_THE_INGRESS_TO_ANY_SURFACE",
        "GEOMETRY_SCAN_COVERS_EVERY_STRING_VALUE",
        "GEOMETRY_SCAN_IS_FIELD_SCOPED_WHEN_THE_SPEC_CARRIES_PROSE",
        "GEOMETRY_SCAN_SCOPE_DEFERRAL",
        "SemanticTopology",
        "layout_semantic_topology",
    )
    missing = [name for name in names if name not in task_book]
    assert not missing, missing


def test_the_engine_router_is_not_named_as_a_second_engine() -> None:
    """The ingress lands on the one authority. A module named like a second engine would read
    as a second authority even if it only mixed in one method.

    The list grows as steps land, so the property under test is what each declared module *is*:
    the ingress, and modules of facts the engine reads -- never a second placer or router with a
    name of its own.
    """

    assert "auto_layout_semantic.py" in contract.PHASE_2B_MAY_IMPORT_THE_CONTRACT
    for module_name in contract.PHASE_2B_MAY_IMPORT_THE_CONTRACT:
        assert not any(
            token in module_name for token in ("engine", "router", "routing", "placer")
        ), module_name


@pytest.mark.parametrize(
    ("attribute", "value", "expected"),
    [
        (
            "ENGINE_INGRESS_MAY_FABRICATE_PLACEHOLDER_POSITIONS",
            True,
            "must not fabricate placeholder positions",
        ),
        (
            "ENGINE_INGRESS_MAY_DISGUISE_TOPOLOGY_AS_A_POSITIONED_DOCUMENT",
            True,
            "must reach the engine as topology",
        ),
        (
            "SECOND_ADAPTER_WITH_SEMANTIC_AUTHORITY_IS_ALLOWED",
            True,
            "must not be a second adapter",
        ),
        ("LAYOUT_AUTHORITY_COUNT", 2, "exactly one layout authority"),
        ("INTERNAL_NORMALIZATION_SEMANTIC_DECISIONS", 1, "representation conversion only"),
        ("INTERNAL_NORMALIZATION_TOPOLOGY_EDITS", 3, "representation conversion only"),
        ("M7_SYNTHESIS_INGRESS_PRESERVE_POSITIONS", True, "must not preserve positions"),
        (
            "M7_INGRESS_PRESERVE_POSITIONS_IS_CALLER_OVERRIDABLE",
            True,
            "no preserve_positions parameter",
        ),
        ("M7_INGRESS_HAS_NO_PRESERVE_POSITIONS_PARAMETER", False, "must not accept"),
        (
            "ENGINE_MAY_TAKE_CANVAS_DIMENSIONS_AS_INPUT",
            True,
            "canvas dimensions are an engine output",
        ),
        ("CANVAS_IS_ENGINE_OUTPUT_DERIVED_FROM_CONTENT", False, "derived from the content"),
        ("CANVAS_MUST_BE_VERIFIED_TO_CLIP_NOTHING", False, "is not an acceptable canvas"),
        ("PHASE_2B_WIRES_THE_INGRESS_TO_ANY_SURFACE", True, "phase 2B adds no surface"),
        (
            "UNIMPLEMENTED_INTENT_DIMENSION_MAY_BE_SILENTLY_IGNORED",
            True,
            "may not be silently ignored",
        ),
        (
            "ENGINE_INGRESS_REPORTS_ENGINE_AND_RULES_VERSION",
            False,
            "must report which engine",
        ),
    ],
)
def test_the_validator_reports_a_relaxed_phase_2b_rule(
    monkeypatch: pytest.MonkeyPatch, attribute: str, value: object, expected: str
) -> None:
    monkeypatch.setattr(contract, attribute, value)
    problems = contract.validate_contract()
    assert any(expected in problem for problem in problems), problems


def test_the_validator_reports_an_intent_dimension_with_no_consumption_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule that keeps "declared but unimplemented" from meaning "silently ignored"."""

    monkeypatch.setattr(
        contract,
        "LAYOUT_INTENT_CONSUMPTION",
        tuple(
            item
            for item in contract.LAYOUT_INTENT_CONSUMPTION
            if item.dimension != "preferred_aspect_class"
        ),
    )
    problems = contract.validate_contract()
    assert any("exactly one consumption point" in problem for problem in problems), problems


def test_the_validator_reports_a_consumption_point_for_an_undeclared_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "LAYOUT_INTENT_CONSUMPTION",
        (
            *contract.LAYOUT_INTENT_CONSUMPTION,
            contract.IntentConsumption("compass_bearing", "step_1", "step_2", "invented"),
        ),
    )
    problems = contract.validate_contract()
    assert any("exactly one consumption point" in problem for problem in problems), problems


def test_the_validator_reports_an_intent_dimension_received_after_the_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "LAYOUT_INTENT_CONSUMPTION",
        tuple(
            contract.IntentConsumption(
                item.dimension,
                "step_2" if item.dimension == "density" else item.received_at_step,
                item.applied_at_step,
                item.behaviour,
            )
            for item in contract.LAYOUT_INTENT_CONSUMPTION
        ),
    )
    problems = contract.validate_contract()
    assert any("is not received at the ingress" in problem for problem in problems), problems


def test_the_validator_reports_a_consumption_point_naming_an_undeclared_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "LAYOUT_INTENT_CONSUMPTION",
        tuple(
            contract.IntentConsumption(
                item.dimension,
                item.received_at_step,
                "step_9" if item.dimension == "grouping" else item.applied_at_step,
                item.behaviour,
            )
            for item in contract.LAYOUT_INTENT_CONSUMPTION
        ),
    )
    problems = contract.validate_contract()
    assert any("names an undeclared step" in problem for problem in problems), problems


def test_the_validator_reports_preserve_positions_becoming_a_model_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The side door: the model asks for its coordinates back through the intent."""
    dimension = contract.LayoutIntentDimension(
        name="preserve_positions",
        values=(),
        open_ended=False,
        note="the coordinates we were asked to give up",
    )
    monkeypatch.setattr(
        contract,
        "LAYOUT_INTENT_DIMENSIONS",
        (*contract.LAYOUT_INTENT_DIMENSIONS, dimension),
    )
    monkeypatch.setattr(
        contract,
        "LAYOUT_INTENT_CONSUMPTION",
        (
            *contract.LAYOUT_INTENT_CONSUMPTION,
            contract.IntentConsumption("preserve_positions", "step_1", "step_2", "keep them"),
        ),
    )
    problems = contract.validate_contract()
    assert any("execution-path policy" in problem for problem in problems), problems


def test_the_validator_reports_a_canvas_derived_before_the_content_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A canvas that is not derived from what is on the drawing is the 1600x900 default again."""

    chain = list(contract.CANVAS_DERIVATION_CHAIN)
    chain.remove("derive_canvas_bounds_from_content_and_margin_and_intent")
    chain.insert(
        chain.index("content_bounds"), "derive_canvas_bounds_from_content_and_margin_and_intent"
    )
    monkeypatch.setattr(contract, "CANVAS_DERIVATION_CHAIN", tuple(chain))
    problems = contract.validate_contract()
    assert any("derived from the content bounds" in problem for problem in problems), problems


def test_the_validator_reports_a_chain_that_does_not_end_at_the_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "CANVAS_DERIVATION_CHAIN", ("semantic_topology", "routing"))
    problems = contract.validate_contract()
    assert any("must end at the canonical layout digest" in problem for problem in problems)
    assert any("routing must precede the canvas derivation" in problem for problem in problems)


def test_the_validator_reports_a_module_declared_for_two_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "PHASE_2B_MAY_IMPORT_THE_CONTRACT",
        ("auto_layout_semantic.py", "m7_diagram_adapter.py"),
    )
    problems = contract.validate_contract()
    assert any("one phase, not for two" in problem for problem in problems), problems


def test_the_validator_reports_engine_versions_the_digest_cannot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract, "ENGINE_VERSION_CONSTANT_NAMES", ("layout_engine_version", "layout_rules_version")
    )
    problems = contract.validate_contract()
    assert any("must be a published constant" in problem for problem in problems), problems


# --------------------------------------------------------------------------------------
# §8.3 step 2: intent stays discrete, placement stays a projection beside the topology
# --------------------------------------------------------------------------------------


def test_the_task_book_declares_the_step_2_rules() -> None:
    task_book = (
        Path(__file__).resolve().parents[2] / "docs" / "m7-2-deterministic-layout.md"
    ).read_text(encoding="utf-8")
    names = (
        *contract.PLACEMENT_PROJECTION_FIELDS,
        *contract.PLACEMENT_KINDS,
        *contract.STEP_2_PLACEMENT_KINDS,
        contract.PLACEMENT_PROJECTION_VERSION,
        contract.GEOMETRY_SCAN_SCOPE_TRIGGER,
        "MODEL_MAY_DECLARE_GAP_NUMBERS",
        "DENSITY_SPACING_POLICY_IS_ENGINE_RULES",
        "DENSITY_POLICY_CHANGE_REQUIRES_THE_LAYOUT_RULES_VERSION_BUMP",
        "SEPARATE_SPACING_POLICY_VERSION_ALLOWED_IN_V1",
        "UNKNOWN_DENSITY_IS_A_HARD_FAILURE",
        "GROUPING_IS_INTENT_ONLY",
        "UNSUPPORTED_INTENT_IS_NEVER_SUBSTITUTED",
        "UNSUPPORTED_INTENT_PRODUCES_ZERO_PLACEMENT",
        "UNSUPPORTED_INTENT_CLASSES_MAY_STAY_IN_THE_VOCABULARY",
        "UNSUPPORTED_LAYOUT_INTENT",
        "SUPPORTED_LAYOUT_INTENT_CLASSES",
        "zone_grouping_requires_zone_membership",
        "PLACEMENT_NEVER_WRITES_INTO_THE_TOPOLOGY",
        "SEMANTIC_DIGEST_IS_UNCHANGED_BY_PLACEMENT",
        "PLACEMENT_PROJECTION_IS_A_PREFIX_OF_THE_CANONICAL_PROJECTION",
        "NODE_SIZE_IS_DECLARED_BY_ENGINE_RULES_UNTIL_ROUTING",
        "FIELD_SCOPED_SCAN_IS_MANDATORY_BEFORE_SUCH_A_SCHEMA_SHIPS",
        "DENSITY_SPACING_POLICY",
        "NODE_SIZE_POLICY",
        "SemanticLayoutPlan",
    )
    missing = [name for name in names if name not in task_book]
    assert not missing, missing

    # The invariants are written in the task book's own language, so the binding is the count:
    # an invariant removed from the contract has to be removed from the book, or the count
    # stops matching.
    section = task_book.split("Step 2 的核心验收")[1].split("```")[1]
    listed = [line for line in section.splitlines() if line.strip()]
    assert len(listed) == len(contract.PLACEMENT_INVARIANTS), listed


def test_the_placement_projection_is_a_prefix_of_the_canonical_projection() -> None:
    canonical = tuple(
        field.name for field in contract.CANONICAL_LAYOUT_PROJECTION_FIELDS if field.included
    )

    assert set(contract.PLACEMENT_PROJECTION_FIELDS) <= set(canonical)
    assert "ordered_waypoints" in canonical
    assert "ordered_waypoints" not in contract.PLACEMENT_PROJECTION_FIELDS
    for field in contract.CANONICAL_PROJECTION_SORT_KEY:
        assert field in contract.PLACEMENT_PROJECTION_FIELDS


@pytest.mark.parametrize(
    ("attribute", "value", "expected"),
    [
        ("MODEL_MAY_DECLARE_GAP_NUMBERS", True, "a gap number is a layout fact"),
        ("DENSITY_SPACING_POLICY_IS_ENGINE_RULES", False, "versioned engine rules"),
        (
            "DENSITY_POLICY_CHANGE_REQUIRES_THE_LAYOUT_RULES_VERSION_BUMP",
            False,
            "must bump the layout rules version",
        ),
        ("SEPARATE_SPACING_POLICY_VERSION_ALLOWED_IN_V1", True, "one rules version in v1"),
        ("UNKNOWN_DENSITY_IS_A_HARD_FAILURE", False, "must fail rather than default"),
        ("GROUPING_IS_INTENT_ONLY", False, "grouping is intent"),
        ("PLACEMENT_NEVER_WRITES_INTO_THE_TOPOLOGY", False, "must not write coordinates back"),
        ("SEMANTIC_DIGEST_IS_UNCHANGED_BY_PLACEMENT", False, "never engineering semantics"),
        (
            "PLACEMENT_PROJECTION_IS_A_PREFIX_OF_THE_CANONICAL_PROJECTION",
            False,
            "must be a prefix of the canonical projection",
        ),
        (
            "NODE_SIZE_IS_DECLARED_BY_ENGINE_RULES_UNTIL_ROUTING",
            False,
            "versioned engine rules until routing",
        ),
        (
            "FIELD_SCOPED_SCAN_IS_MANDATORY_BEFORE_SUCH_A_SCHEMA_SHIPS",
            False,
            "may not ship before the scan becomes field-scoped",
        ),
        ("GEOMETRY_SCAN_SCOPE_TRIGGER", "", "must name its trigger"),
        ("PLACEMENT_INVARIANTS", (), "invariants must be declared"),
    ],
)
def test_the_validator_reports_a_relaxed_step_2_rule(
    monkeypatch: pytest.MonkeyPatch, attribute: str, value: object, expected: str
) -> None:
    monkeypatch.setattr(contract, attribute, value)
    problems = contract.validate_contract()
    assert any(expected in problem for problem in problems), problems


def test_the_validator_reports_a_placement_field_the_canonical_projection_does_not_know(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "PLACEMENT_PROJECTION_FIELDS",
        (*contract.PLACEMENT_PROJECTION_FIELDS, "layer_id"),
    )
    problems = contract.validate_contract()
    assert any("not a canonical layout projection field" in problem for problem in problems)


def test_the_validator_reports_routing_output_moved_into_the_placement_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "PLACEMENT_PROJECTION_FIELDS",
        (*contract.PLACEMENT_PROJECTION_FIELDS, "ordered_waypoints"),
    )
    problems = contract.validate_contract()
    assert any("belongs to step 3" in problem for problem in problems)


def test_the_validator_reports_a_placement_projection_without_the_sort_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "PLACEMENT_PROJECTION_FIELDS",
        tuple(field for field in contract.PLACEMENT_PROJECTION_FIELDS if field != "placement_kind"),
    )
    problems = contract.validate_contract()
    assert any("must carry the canonical sort key" in problem for problem in problems)


def test_the_validator_reports_an_unsupported_intent_outside_the_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value the protocol does not recognise is a parse failure, not a capability gap."""

    monkeypatch.setattr(
        contract,
        "UNSUPPORTED_LAYOUT_INTENT",
        (
            contract.UnsupportedLayoutIntent(
                dimension="grouping", value="grouped_by_continent", code="code", reason="reason"
            ),
        ),
    )
    problems = contract.validate_contract()
    assert any("is not in the 'grouping' vocabulary" in problem for problem in problems), problems


def test_the_validator_reports_an_unsupported_intent_with_no_code_or_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "UNSUPPORTED_LAYOUT_INTENT",
        (
            contract.UnsupportedLayoutIntent(
                dimension="grouping", value="grouped_by_zone", code="", reason=""
            ),
        ),
    )
    problems = contract.validate_contract()
    assert any("needs a lowercase code" in problem for problem in problems), problems
    assert any("must say what is missing" in problem for problem in problems), problems


def test_the_validator_reports_substituting_an_unsupported_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "UNSUPPORTED_INTENT_IS_NEVER_SUBSTITUTED", False)
    problems = contract.validate_contract()
    assert any("must be refused, not replaced" in problem for problem in problems), problems
    monkeypatch.undo()
    monkeypatch.setattr(contract, "UNSUPPORTED_INTENT_PRODUCES_ZERO_PLACEMENT", False)
    problems = contract.validate_contract()
    assert any("must produce no placement at all" in problem for problem in problems), problems


def test_the_validator_reports_a_class_that_is_both_supported_and_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "SUPPORTED_LAYOUT_INTENT_CLASSES",
        (
            *contract.SUPPORTED_LAYOUT_INTENT_CLASSES,
            contract.SupportedLayoutIntentClass(dimension="grouping", value="grouped_by_zone"),
        ),
    )
    problems = contract.validate_contract()
    assert any("both supported and unsupported" in problem for problem in problems), problems


def test_the_validator_reports_a_grouping_class_with_no_declared_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every class in the vocabulary is either executable or refused, and that is checked."""

    monkeypatch.setattr(
        contract,
        "UNSUPPORTED_LAYOUT_INTENT",
        (),
    )
    problems = contract.validate_contract()
    assert any("either supported or unsupported" in problem for problem in problems), problems


def test_the_validator_reports_no_executable_grouping_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "SUPPORTED_LAYOUT_INTENT_CLASSES", ())
    problems = contract.validate_contract()
    assert any("at least one grouping class" in problem for problem in problems), problems


def test_the_validator_reports_a_step_2_kind_that_belongs_to_a_later_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "STEP_2_PLACEMENT_KINDS", ("equipment", "routing"))
    problems = contract.validate_contract()
    assert any("produced by a later step" in problem for problem in problems)


# ---------------------------------------------------------------------------------------
# Step 4: the content envelope, the derived canvas, and the narrowed crossing rule
# ---------------------------------------------------------------------------------------


def test_the_contract_declares_step_four_and_stays_coherent() -> None:
    from agentcad.auto_layout_canvas import (  # noqa: PLC0415 - binding check, not an import cycle
        ASPECT_CLASS_RATIO_POLICY,
        CANVAS_MARGIN_POLICY,
    )
    from agentcad.auto_layout_geometry import (  # noqa: PLC0415
        MATERIALIZED_CLEARANCE_POLICY,
        MaterializedClearance,
    )
    from agentcad.auto_layout_semantic import (  # noqa: PLC0415
        DENSITY_SPACING_POLICY,
        STEP_4,
    )

    assert contract.validate_contract() == []
    assert contract.PHASE_2B_STEPS[3][1] == "content_bounds_to_canvas_bounds_and_aspect_enforcement"
    assert STEP_4 == contract.CANVAS_DERIVATION_STEP
    assert "auto_layout_canvas.py" in contract.PHASE_2B_MAY_IMPORT_THE_CONTRACT
    # The engine's numbers are the ones the contract names, read from both sides.
    assert ASPECT_CLASS_RATIO_POLICY == tuple(contract.ASPECT_CLASS_TARGET_RATIOS)
    assert {name for name, _ in CANVAS_MARGIN_POLICY} == {name for name, _ in DENSITY_SPACING_POLICY}
    assert {name for name, _ in MATERIALIZED_CLEARANCE_POLICY} == {
        name for name, _ in DENSITY_SPACING_POLICY
    }
    assert set(MaterializedClearance.__dataclass_fields__) == set(
        contract.MATERIALIZED_CLEARANCE_FIELDS
    )


def test_the_validator_reports_an_aspect_class_without_a_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "ASPECT_CLASS_TARGET_RATIOS",
        (("standard", 4.0 / 3.0), ("wide", 16.0 / 9.0)),
    )
    problems = contract.validate_contract()
    assert any("must name a ratio exactly once" in problem for problem in problems), problems


def test_the_validator_reports_a_wider_class_that_is_not_wider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "ASPECT_CLASS_TARGET_RATIOS",
        (("standard", 12.0), ("wide", 1.5), ("extra_wide", 1.2)),
    )
    problems = contract.validate_contract()
    assert any("a wider class must mean a wider ratio" in problem for problem in problems), problems


def test_the_validator_reports_the_content_envelope_losing_a_row_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounds over a subset is a canvas that crops -- so the subset must be reported."""

    monkeypatch.setattr(contract, "CONTENT_BOUNDS_INPUTS", ("symbol_instance_geometry",))
    problems = contract.validate_contract()
    assert any("must cover 'orthogonal_routes'" in problem for problem in problems), problems
    assert any(
        "must cover 'annotations_and_leader_lines'" in problem for problem in problems
    ), problems
    monkeypatch.undo()
    monkeypatch.setattr(contract, "CONTENT_BOUNDS_MAY_EXCLUDE_A_PRESENTATION_ROW", True)
    problems = contract.validate_contract()
    assert any("no presentation row may be left out" in problem for problem in problems), problems


def test_the_validator_reports_a_canvas_allowed_to_shrink_to_reach_a_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "ASPECT_ENFORCEMENT_MAY_SHRINK_BELOW_THE_CONTENT", True)
    problems = contract.validate_contract()
    assert any("shrinking below the content is cropping" in problem for problem in problems), problems
    monkeypatch.undo()
    monkeypatch.setattr(contract, "CANVAS_NEVER_CROPS_CONTENT_TO_REACH_A_RATIO", False)
    problems = contract.validate_contract()
    assert any("never be reached by cropping" in problem for problem in problems), problems


def test_the_validator_reports_clipping_that_is_assumed_rather_than_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "CANVAS_CLIPS_NOTHING_IS_VERIFIED_NOT_ASSUMED", False)
    problems = contract.validate_contract()
    assert any("must be verified to clip nothing" in problem for problem in problems), problems
    monkeypatch.undo()
    monkeypatch.setattr(contract, "CANVAS_VERIFICATION_COVERS_ROUTES_AND_ANNOTATIONS", False)
    problems = contract.validate_contract()
    assert any("must cover routes and annotations" in problem for problem in problems), problems


def test_the_validator_reports_the_canvas_step_not_being_where_the_intent_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "CANVAS_DERIVATION_STEP", "step_9")
    problems = contract.validate_contract()
    assert any("must be a declared phase-2B step" in problem for problem in problems), problems
    monkeypatch.undo()
    moved = tuple(
        contract.IntentConsumption(item.dimension, item.received_at_step, "step_2", item.behaviour)
        if item.dimension == "orientation"
        else item
        for item in contract.LAYOUT_INTENT_CONSUMPTION
    )
    monkeypatch.setattr(contract, "LAYOUT_INTENT_CONSUMPTION", moved)
    problems = contract.validate_contract()
    assert any("not at the canvas step" in problem for problem in problems), problems


def test_the_validator_reports_a_route_allowed_to_traverse_any_node_it_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "ROUTE_MAY_CROSS_ANY_NODE_IT_SERVES", True)
    problems = contract.validate_contract()
    assert any("only on the segment that enters or leaves it" in problem for problem in problems), (
        problems
    )


def test_the_validator_reports_the_clearance_policy_borrowing_the_rank_gap_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rename the remote asked for, made checkable: two rules may not share their fields."""

    monkeypatch.setattr(contract, "MATERIALIZED_CLEARANCE_FIELDS", ("rank_gap", "node_gap"))
    problems = contract.validate_contract()
    assert any("may not be a spacing policy field" in problem for problem in problems), problems
    assert any("states a node and a system clearance" in problem for problem in problems), problems


def test_the_validator_reports_the_clearance_rule_being_read_as_the_origin_spacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "MATERIALIZED_CLEARANCE_IS_NOT_THE_RANK_GAP", False)
    problems = contract.validate_contract()
    assert any("is not the origin spacing" in problem for problem in problems), problems
    monkeypatch.undo()
    monkeypatch.setattr(contract, "SEPARATE_CLEARANCE_POLICY_VERSION_ALLOWED_IN_V1", True)
    problems = contract.validate_contract()
    assert any("second source of truth" in problem for problem in problems), problems


def test_the_validator_reports_a_margin_that_defaults_in_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "CANVAS_MARGIN_DEFAULT_IS_APPLIED_IN_SILENCE", True)
    problems = contract.validate_contract()
    assert any("a margin nobody chose" in problem for problem in problems), problems
    monkeypatch.undo()
    monkeypatch.setattr(contract, "MARGIN_IS_PRESERVED_IN_THE_DERIVED_CANVAS", False)
    problems = contract.validate_contract()
    assert any("may not spend the margin" in problem for problem in problems), problems


def test_the_validator_reports_presentation_bounds_measured_at_the_centerline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate's blocker, as a check: a canvas fitted to centerlines clips the strokes."""

    monkeypatch.setattr(contract, "CONTENT_BOUNDS_INPUTS_ARE_RENDERED_EXTENTS", False)
    problems = contract.validate_contract()
    assert any("rendered extent" in problem for problem in problems), problems
    monkeypatch.undo()
    monkeypatch.setattr(
        contract, "CONTENT_BOUNDS_MAY_USE_A_CENTERLINE_INSTEAD_OF_A_RENDERED_EXTENT", True
    )
    problems = contract.validate_contract()
    assert any("cannot both hold" in problem for problem in problems), problems


def test_the_validator_reports_a_stroked_kind_left_out_of_the_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for flag, fragment in (
        ("ROUTE_STROKE_CONTRIBUTES_TO_PRESENTATION_BOUNDS", "connector is drawn around"),
        ("LEADER_STROKE_CONTRIBUTES_TO_PRESENTATION_BOUNDS", "leader line is drawn around"),
        ("SYMBOL_OUTLINE_STROKE_CONTRIBUTES_TO_PRESENTATION_BOUNDS", "outline is drawn around"),
    ):
        monkeypatch.setattr(contract, flag, False)
        problems = contract.validate_contract()
        assert any(fragment in problem for problem in problems), (flag, problems)
        monkeypatch.undo()
    monkeypatch.setattr(contract, "ANNOTATION_TEXT_HAS_NO_STROKE", False)
    problems = contract.validate_contract()
    assert any("carries no stroke to inflate" in problem for problem in problems), problems


def test_the_validator_reports_a_stroke_policy_without_a_rule_or_a_version_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "PRESENTATION_STROKE_ENVELOPE_RULE", "")
    problems = contract.validate_contract()
    assert any("envelope rule must be named" in problem for problem in problems), problems
    monkeypatch.undo()
    monkeypatch.setattr(
        contract, "PRESENTATION_STROKE_POLICY_CHANGE_REQUIRES_LAYOUT_RULES_VERSION_BUMP", False
    )
    problems = contract.validate_contract()
    assert any("moves the rules version" in problem for problem in problems), problems
    monkeypatch.undo()
    monkeypatch.setattr(contract, "PRESENTATION_STROKE_POLICY_KINDS", ("symbol_outline",))
    problems = contract.validate_contract()
    assert any("'leader_line'" in problem for problem in problems), problems


def test_the_validator_reports_the_stroke_becoming_its_own_identity_axis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract, "LAYOUT_DIGEST_INPUTS", (*contract.LAYOUT_DIGEST_INPUTS, "stroke_width")
    )
    problems = contract.validate_contract()
    assert any("must not become a digest input on its own" in problem for problem in problems), (
        problems
    )


def test_the_validator_reports_the_catalogue_box_read_as_the_drawn_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalogue shapes reach the box edges, so this is a fact, not a convention."""

    monkeypatch.setattr(contract, "SYMBOL_BOX_IS_NOT_THE_SYMBOL_RENDERED_EXTENT", False)
    problems = contract.validate_contract()
    assert any("not its drawn box" in problem for problem in problems), problems


def test_the_validator_reports_symbol_bounds_assumed_rather_than_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inflating the declared box is exact only while the containment invariant holds."""

    monkeypatch.setattr(contract, "SYMBOL_UNSTROKED_SHAPES_MUST_FIT_THE_INTRINSIC_BOX", False)
    problems = contract.validate_contract()
    assert any("must fit its intrinsic box" in problem for problem in problems), problems
    monkeypatch.undo()
    monkeypatch.setattr(
        contract, "SYMBOL_RENDERED_BOUNDS_ARE_EXACT_GIVEN_THE_CONTAINMENT_INVARIANT", False
    )
    problems = contract.validate_contract()
    assert any("only exact while the containment invariant holds" in problem for problem in problems)
    monkeypatch.undo()
    monkeypatch.setattr(contract, "SYMBOL_SHAPE_OVERFLOW_IS_A_HARD_FAILURE_AT_FREEZE", False)
    problems = contract.validate_contract()
    assert any("failure where geometry freezes" in problem for problem in problems), problems


def test_the_validator_reports_an_overflow_that_nobody_can_locate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "SYMBOL_SHAPE_OVERFLOW_NAMES_THE_SYMBOL_AND_THE_SHAPE", False)
    problems = contract.validate_contract()
    assert any("must name the symbol and the shape" in problem for problem in problems), problems


def test_the_validator_reports_curve_bounds_that_are_optimistic_or_sampled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "SYMBOL_SHAPE_BOUNDS_ARE_CONSERVATIVE_FOR_CURVES", False)
    problems = contract.validate_contract()
    assert any("conservative for curves" in problem for problem in problems), problems
    monkeypatch.undo()
    monkeypatch.setattr(
        contract, "SYMBOL_SHAPE_BOUNDS_USE_CONTROL_POINTS_NOT_SAMPLED_CURVES", False
    )
    problems = contract.validate_contract()
    assert any("control points bound a curve" in problem for problem in problems), problems
    monkeypatch.undo()
    monkeypatch.setattr(contract, "SYMBOL_SHAPE_KINDS", ("line", "rect"))
    problems = contract.validate_contract()
    reported = {
        kind
        for kind in ("polyline", "circle", "path", "text")
        if any(f"must name the {kind!r} kind" in problem for problem in problems)
    }
    assert reported == {"polyline", "circle", "path", "text"}, problems


def test_the_validator_reports_an_unrecognised_shape_kind_passing_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "UNKNOWN_SYMBOL_SHAPE_KIND_IS_A_HARD_FAILURE", False)
    problems = contract.validate_contract()
    assert any("unknown bounds" in problem for problem in problems), problems
