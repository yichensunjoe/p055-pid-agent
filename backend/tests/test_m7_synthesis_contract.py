"""M7 phase 1 is design and contract only — and this test is what makes that true.

The milestone exists because of one specific failure: a plan that lost 39% of its own
operations was assessed valid and recorded as a completed session, because the assessment
had one axis (``valid``) instead of two, and because nothing persisted what had been
*proposed*. So the tests below defend exactly that:

* the contract's own validator must stay quiet (``validate_contract()`` finds no violation);
* the two axes, the receipt, the continuation matrix, the catalogue audit, the layout
  intent and the responsibility split must all be named in the task book, so prose and
  data cannot separate — a task book that says "partial is never success" while the data
  says otherwise is the failure mode this whole file is aimed at;
* the *live* surface (FastAPI OpenAPI paths and MCP tool names, read the same way the
  surface-contract test reads them) must contain nothing synthesis-shaped while the
  milestone is in phase 1;
* each mutation below is a specific, tempting relaxation — including the exact one that
  caused the original failure — and each must be reported.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentcad import m7_synthesis_contract as contract
from agentcad.main import create_app
from agentcad.surface_contract import HTTP_SURFACE_BINDINGS, MCP_SURFACE_BINDINGS

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK_BOOK = REPO_ROOT / "docs" / "m7-semantic-first-synthesis.md"
MCP_SERVER_SOURCE = Path(__file__).resolve().parents[1] / "agentcad" / "mcp_server.py"


@pytest.fixture(scope="module")
def task_book() -> str:
    return TASK_BOOK.read_text(encoding="utf-8")


def _names_in_task_book(task_book: str, names: tuple[str, ...]) -> list[str]:
    return [name for name in names if name not in task_book]


# --------------------------------------------------------------------------------------
# The contract is coherent on its own terms
# --------------------------------------------------------------------------------------


def test_the_synthesis_contract_holds_together() -> None:
    assert contract.validate_contract() == []


def test_validity_and_completeness_are_two_axes() -> None:
    """The single structural change that makes the original failure impossible."""

    assert tuple(name for name, _ in contract.SYNTHESIS_ASSESSMENT_AXES) == (
        "validity",
        "completeness",
    )
    assert contract.VALIDITY_VALUES == ("valid", "invalid")
    assert contract.COMPLETENESS_VALUES == ("complete", "partial", "empty")
    assert contract.COMPLETENESS_IS_INDEPENDENT_OF_VALIDITY
    assert contract.COMPLETENESS_IS_DERIVED_FROM_COUNTS
    assert contract.SESSION_SUCCESS_REQUIRES == ("valid", "complete")


def test_the_assessment_records_what_was_proposed_not_only_what_compiled() -> None:
    assert set(contract.SYNTHESIS_COUNT_FIELDS) == {
        "proposed_operation_count",
        "compiled_operation_count",
        "rejected_operation_count",
    }


def test_a_rejected_operation_carries_a_usable_receipt() -> None:
    """The compiler already computes all of this and then discards it."""

    assert set(contract.REJECTED_OPERATION_RECEIPT_FIELDS) >= {
        "original_index",
        "operation_id",
        "operation_kind",
        "reason_code",
        "message",
        "field_path",
        "available_values",
        "suggestions",
    }


def test_the_raw_proposal_is_evidence_in_its_own_right() -> None:
    assert contract.RAW_PROPOSAL_IS_DURABLE_EVIDENCE
    assert contract.APPLIED_ONLY_PERSISTENCE_IS_FORBIDDEN
    assert contract.EVIDENCE_READABLE_WITHOUT_PROVIDER
    assert set(contract.PROPOSAL_EVIDENCE_REQUIRED_FIELDS) >= {
        "proposed_operations",
        "rejected_operations",
        "proposed_operation_count",
    }


def test_the_continuation_matrix_is_total_and_partial_never_succeeds() -> None:
    cells = [(rule.validity, rule.completeness) for rule in contract.CONTINUATION_MATRIX]
    expected = {
        (validity, completeness)
        for validity in contract.VALIDITY_VALUES
        for completeness in contract.COMPLETENESS_VALUES
    }
    assert set(cells) == expected
    assert len(cells) == len(expected)

    partial = contract.continuation_rule("valid", "partial")
    assert partial.action == "return_receipt_and_replan"
    assert contract.continuation_rule("valid", "complete").action == (
        "proceed_to_human_confirmation"
    )
    for rule in contract.CONTINUATION_MATRIX:
        if rule.validity == "invalid":
            assert rule.action == "existing_error_recovery"

    assert not contract.PARTIAL_MAY_BE_REPORTED_AS_SUCCESS
    assert not contract.PARTIAL_MAY_BE_RECORDED_AS_COMPLETED_SESSION
    assert not contract.RECEIPT_IS_OMITTABLE


def test_the_human_sees_the_counts_before_authorising() -> None:
    assert set(contract.UI_MUST_DISPLAY_COMPLETENESS_FIELDS) == {
        "proposed_operation_count",
        "compiled_operation_count",
        "rejected_operation_count",
        "completeness",
    }


def test_hidden_and_missing_are_different_defects() -> None:
    assert contract.CATALOGUE_AUDIT_STEPS == (
        "declared_requirement",
        "catalogue_existence",
        "visible_to_model",
        "compiler_accepts",
        "renderer_supports",
    )
    assert contract.CATALOGUE_AUDIT_ANCHOR_IS_DECLARED_REQUIREMENT
    assert contract.CATALOGUE_AUDIT_IS_MACHINE_VERIFIABLE
    assert contract.HIDDEN_IS_NOT_MISSING


def test_an_unrepresentable_requirement_becomes_a_reported_gap() -> None:
    assert set(contract.CATALOG_GAP_REQUIRED_FIELDS) == {
        "requested_type",
        "requested_tag",
        "source_requirement",
        "available_alternatives",
    }
    assert contract.CATALOG_GAP_IS_REPORTED_NOT_HIDDEN
    assert contract.CATALOG_GAP_FORCES_COMPLETENESS == "partial"
    assert not contract.AUTO_GENERIC_SUBSTITUTION_ALLOWED_IN_PHASE_1
    assert set(contract.CATALOGUE_GAP_FORBIDDEN_HANDLING) >= {
        "silent_omission",
        "substitute_a_lookalike_symbol",
        "invent_an_undefined_symbol_key",
    }


def test_layout_intent_carries_no_pixel_arithmetic() -> None:
    fields = {field.key: field for field in contract.LAYOUT_INTENT_FIELDS}
    assert set(fields) >= {
        "orientation",
        "preferred_aspect_class",
        "system_order",
        "primary_flow_direction",
    }
    assert set(fields["orientation"].allowed) == {"landscape", "portrait"}
    assert set(fields["preferred_aspect_class"].allowed) == {"standard", "wide", "extra_wide"}
    assert fields["orientation"].required and fields["preferred_aspect_class"].required
    assert contract.LAYOUT_INTENT_DATACLASS_NAME == "DrawingLayoutIntent"
    assert contract.DIAGRAM_SPEC_NAME == "DiagramSpec"


def test_the_canvas_follows_the_content_and_never_clips_it() -> None:
    assert contract.CANVAS_IS_DERIVED_FROM_CONTENT
    assert contract.CANVAS_GROWS_WHEN_CONTENT_EXCEEDS_CURRENT_BOUNDS
    assert not contract.CANVAS_SHRINKS_WITHOUT_EXPLICIT_REQUEST
    assert contract.CANVAS_MARGIN_IS_REQUIRED
    assert not contract.CONTENT_MAY_BE_CLIPPED_BY_CANVAS
    assert contract.EXTRA_WIDE_INTENT_SURVIVES_CONTENT_GROWTH
    assert "required_margin" in contract.CANVAS_DERIVATION_INPUTS


def test_the_responsibility_split_is_a_partition_and_the_model_gets_no_coordinates() -> None:
    assert not (set(contract.MODEL_OWNS) & set(contract.CODE_OWNS))
    assert "absolute_coordinates" in contract.CODE_OWNS
    assert "absolute_coordinates" not in contract.MODEL_OWNS
    assert "layout_intent" in contract.MODEL_OWNS
    assert not contract.DIAGRAM_SPEC_CARRIES_ABSOLUTE_COORDINATES
    assert set(contract.DIAGRAM_SPEC_SECTIONS) == {
        "systems",
        "equipment",
        "instruments",
        "connections",
        "required_loops",
        "annotations",
        "layout_intent",
    }
    assert set(contract.FORBIDDEN_IN_MODEL_OUTPUT) >= {
        "absolute_x",
        "absolute_y",
        "canvas_width",
        "canvas_height",
        "connector_waypoints",
    }
    assert not contract.PRESERVE_POSITIONS_IS_VALID_FOR_LARGE_DIAGRAM_LAYOUT


def test_three_fixtures_are_declared_with_positive_and_negative_expectations() -> None:
    assert [fixture.key for fixture in contract.ACCEPTANCE_FIXTURES] == ["A", "B", "C"]
    for fixture in contract.ACCEPTANCE_FIXTURES:
        assert fixture.must_observe, fixture.key
        assert fixture.must_not_observe, fixture.key
    fixture_a = contract.ACCEPTANCE_FIXTURES[0]
    assert fixture_a.name == "partial_compile_is_not_success"
    assert any("not recorded as completed" in item for item in fixture_a.must_observe)
    assert any("completed session status" in item for item in fixture_a.must_not_observe)


def test_cad_is_a_benchmark_and_the_representation_ratio_is_forbidden() -> None:
    assert not contract.CAD_IS_A_RUNTIME_INPUT
    assert contract.CAD_ROLE == "offline_benchmark_reference"
    assert "semantic_element_count_divided_by_cad_primitive_count" in (
        contract.FORBIDDEN_FIDELITY_METRICS
    )
    assert set(contract.BENCHMARK_ACCEPTANCE_ITEMS) >= {
        "required_equipment",
        "required_instruments",
        "required_connections",
        "required_closed_loops",
        "required_systems",
    }


def test_the_defect_register_leads_with_the_two_p0_defects() -> None:
    assert [severity for severity, _, _ in contract.DEFECT_REGISTER][:2] == ["P0", "P0"]
    keys = [key for _, key, _ in contract.DEFECT_REGISTER]
    assert keys[0] == "silent_partial_compilation"
    assert keys[1] == "model_owns_low_level_geometry"
    assert "prompt_quality" not in keys
    assert set(contract.REJECTED_ROOT_CAUSES) == {
        "prompt_quality",
        "model_capability",
        "cad_element_count",
    }


# --------------------------------------------------------------------------------------
# Prose and data cannot separate
# --------------------------------------------------------------------------------------


def test_the_task_book_names_the_two_axes_and_the_continuation_actions(task_book: str) -> None:
    missing = _names_in_task_book(
        task_book,
        (*contract.VALIDITY_VALUES, *contract.COMPLETENESS_VALUES, "SESSION_SUCCESS_REQUIRES"),
    )
    assert not missing, missing
    actions = {rule.action for rule in contract.CONTINUATION_MATRIX}
    missing_actions = _names_in_task_book(task_book, tuple(sorted(actions)))
    assert not missing_actions, missing_actions


def test_the_task_book_names_the_receipt_and_evidence_fields(task_book: str) -> None:
    missing = _names_in_task_book(
        task_book,
        (
            *contract.SYNTHESIS_COUNT_FIELDS,
            *contract.REJECTED_OPERATION_RECEIPT_FIELDS,
            *contract.PROPOSAL_EVIDENCE_REQUIRED_FIELDS,
        ),
    )
    assert not missing, missing


def test_the_task_book_names_the_catalogue_audit_and_gap_contract(task_book: str) -> None:
    missing = _names_in_task_book(
        task_book,
        (
            *contract.CATALOGUE_AUDIT_STEPS,
            *contract.CATALOG_GAP_REQUIRED_FIELDS,
            *contract.CATALOGUE_GAP_FORBIDDEN_HANDLING,
            "HIDDEN_IS_NOT_MISSING",
        ),
    )
    assert not missing, missing


def test_the_task_book_names_the_layout_intent_and_canvas_contract(task_book: str) -> None:
    names = [field.key for field in contract.LAYOUT_INTENT_FIELDS]
    for field in contract.LAYOUT_INTENT_FIELDS:
        names.extend(field.allowed)
    names.extend(
        [
            contract.LAYOUT_INTENT_DATACLASS_NAME,
            *contract.CANVAS_DERIVATION_INPUTS,
            "EXTRA_WIDE_INTENT_SURVIVES_CONTENT_GROWTH",
            "CONTENT_MAY_BE_CLIPPED_BY_CANVAS",
        ]
    )
    missing = _names_in_task_book(task_book, tuple(names))
    assert not missing, missing


def test_the_task_book_names_the_responsibility_split(task_book: str) -> None:
    missing = _names_in_task_book(
        task_book,
        (
            *contract.MODEL_OWNS,
            *contract.CODE_OWNS,
            *contract.FORBIDDEN_IN_MODEL_OUTPUT,
            *contract.DIAGRAM_SPEC_SECTIONS,
            contract.DIAGRAM_SPEC_NAME,
            "PRESERVE_POSITIONS_IS_VALID_FOR_LARGE_DIAGRAM_LAYOUT",
        ),
    )
    assert not missing, missing


def test_the_task_book_names_the_fixtures_the_phases_and_the_deferrals(task_book: str) -> None:
    names = [fixture.name for fixture in contract.ACCEPTANCE_FIXTURES]
    names.extend(contract.PHASE_1_FORBIDDEN_SURFACES)
    names.extend(key for key, _ in contract.DEFERRED_TO)
    names.extend(phase for _, phase in contract.DEFERRED_TO)
    missing = _names_in_task_book(task_book, tuple(names))
    assert not missing, missing


def test_the_task_book_names_the_incremental_and_benchmark_contract(task_book: str) -> None:
    missing = _names_in_task_book(
        task_book,
        (
            *contract.M7_3_SEMANTIC_PRIMITIVES,
            *contract.M7_3_LOW_LEVEL_PRIMITIVES_FORBIDDEN,
            *contract.M7_3_RECEIPT_FIELDS,
            *contract.BENCHMARK_ACCEPTANCE_ITEMS,
            *contract.FORBIDDEN_FIDELITY_METRICS,
            *contract.REJECTED_ROOT_CAUSES,
            *[key for _, key, _ in contract.DEFECT_REGISTER],
        ),
    )
    assert not missing, missing


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


def test_phase_one_added_no_synthesis_surface() -> None:
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
        "M7 is in phase 1 (design and contract only), but these surfaces already exist: "
        f"{offenders}"
    )


def test_the_forbidden_surface_list_is_not_empty() -> None:
    assert contract.PHASE_1_FORBIDDEN_SURFACE_TOKENS
    assert set(contract.PHASE_1_FORBIDDEN_SURFACES) >= {
        "new_http_route",
        "new_mcp_tool",
        "diagram_spec_runtime",
        "incremental_declaration_primitives",
    }


# --------------------------------------------------------------------------------------
# The validator is not decoration: each relaxation below must be reported
# --------------------------------------------------------------------------------------


def test_the_validator_reports_completeness_collapsed_into_validity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce the original defect in data: one axis instead of two."""

    monkeypatch.setattr(contract, "COMPLETENESS_IS_INDEPENDENT_OF_VALIDITY", False)
    problems = contract.validate_contract()
    assert any("collapsing them is the defect" in problem for problem in problems)


def test_the_validator_reports_a_partial_result_that_may_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "PARTIAL_MAY_BE_REPORTED_AS_SUCCESS", True)
    problems = contract.validate_contract()
    assert any("must never be reported as success" in problem for problem in problems)


def test_the_validator_reports_a_partial_session_marked_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "PARTIAL_MAY_BE_RECORDED_AS_COMPLETED_SESSION", True)
    problems = contract.validate_contract()
    assert any("never be recorded as a completed session" in problem for problem in problems)


def test_the_validator_reports_a_missing_rejected_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "SYNTHESIS_COUNT_FIELDS",
        ("proposed_operation_count", "compiled_operation_count"),
    )
    problems = contract.validate_contract()
    assert any("rejected_operation_count" in problem for problem in problems)


def test_the_validator_reports_an_incomplete_continuation_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trimmed = tuple(rule for rule in contract.CONTINUATION_MATRIX if rule.completeness != "empty")
    monkeypatch.setattr(contract, "CONTINUATION_MATRIX", trimmed)
    problems = contract.validate_contract()
    assert any("matrix must be total" in problem for problem in problems)


def test_the_validator_reports_a_partial_plan_that_proceeds_anyway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relaxed = tuple(
        contract.ContinuationRule(
            rule.validity, rule.completeness, "proceed_to_human_confirmation", rule.note
        )
        if (rule.validity, rule.completeness) == ("valid", "partial")
        else rule
        for rule in contract.CONTINUATION_MATRIX
    )
    monkeypatch.setattr(contract, "CONTINUATION_MATRIX", relaxed)
    problems = contract.validate_contract()
    assert any("must return the receipt and replan" in problem for problem in problems)


def test_the_validator_reports_the_applied_only_persistence_relaxation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "APPLIED_ONLY_PERSISTENCE_IS_FORBIDDEN", False)
    problems = contract.validate_contract()
    assert any("persisting only the applied transaction" in problem for problem in problems)


def test_the_validator_reports_a_missing_proposal_evidence_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "PROPOSAL_EVIDENCE_REQUIRED_FIELDS",
        tuple(f for f in contract.PROPOSAL_EVIDENCE_REQUIRED_FIELDS if f != "proposed_operations"),
    )
    problems = contract.validate_contract()
    assert any("must record 'proposed_operations'" in problem for problem in problems)


def test_the_validator_reports_a_receipt_without_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "REJECTED_OPERATION_RECEIPT_FIELDS",
        tuple(f for f in contract.REJECTED_OPERATION_RECEIPT_FIELDS if f != "reason_code"),
    )
    problems = contract.validate_contract()
    assert any("must carry 'reason_code'" in problem for problem in problems)


def test_the_validator_reports_hidden_and_missing_becoming_the_same_thing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "HIDDEN_IS_NOT_MISSING", False)
    problems = contract.validate_contract()
    assert any("hidden and missing are different defects" in problem for problem in problems)


def test_the_validator_reports_an_unreported_catalog_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "CATALOG_GAP_IS_REPORTED_NOT_HIDDEN", False)
    problems = contract.validate_contract()
    assert any("must be reported, never hidden" in problem for problem in problems)


def test_the_validator_reports_auto_generic_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "AUTO_GENERIC_SUBSTITUTION_ALLOWED_IN_PHASE_1", True)
    problems = contract.validate_contract()
    assert any("auto generic substitution stays forbidden" in problem for problem in problems)


def test_the_validator_reports_a_dropped_gap_handling_prohibition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "CATALOGUE_GAP_FORBIDDEN_HANDLING",
        ("silent_omission", "substitute_a_lookalike_symbol"),
    )
    problems = contract.validate_contract()
    assert any(
        "invent_an_undefined_symbol_key" in problem and "forbidden" in problem
        for problem in problems
    )


def test_the_validator_reports_a_canvas_that_may_clip_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "CONTENT_MAY_BE_CLIPPED_BY_CANVAS", True)
    problems = contract.validate_contract()
    assert any("never be clipped by the canvas" in problem for problem in problems)


def test_the_validator_reports_a_canvas_that_does_not_follow_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "CANVAS_IS_DERIVED_FROM_CONTENT", False)
    problems = contract.validate_contract()
    assert any("derived from the content" in problem for problem in problems)


def test_the_validator_reports_a_grow_rule_that_only_adds_height(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "EXTRA_WIDE_INTENT_SURVIVES_CONTENT_GROWTH", False)
    problems = contract.validate_contract()
    assert any("extra-wide intent must survive content growth" in problem for problem in problems)


def test_the_validator_reports_a_layout_intent_without_orientation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trimmed = tuple(field for field in contract.LAYOUT_INTENT_FIELDS if field.key != "orientation")
    monkeypatch.setattr(contract, "LAYOUT_INTENT_FIELDS", trimmed)
    problems = contract.validate_contract()
    assert any("layout intent must declare 'orientation'" in problem for problem in problems)


def test_the_validator_reports_coordinates_creeping_back_into_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "MODEL_OWNS", (*contract.MODEL_OWNS, "absolute_coordinates"))
    problems = contract.validate_contract()
    assert any("must not own absolute coordinates" in problem for problem in problems)
    assert any("must be disjoint" in problem for problem in problems)


def test_the_validator_reports_a_model_output_that_may_carry_canvas_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "FORBIDDEN_IN_MODEL_OUTPUT",
        tuple(f for f in contract.FORBIDDEN_IN_MODEL_OUTPUT if f != "canvas_width"),
    )
    problems = contract.validate_contract()
    assert any("'canvas_width' must be forbidden" in problem for problem in problems)


def test_the_validator_reports_preserve_positions_as_the_large_diagram_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "PRESERVE_POSITIONS_IS_VALID_FOR_LARGE_DIAGRAM_LAYOUT", True)
    problems = contract.validate_contract()
    assert any("model came to own macro placement" in problem for problem in problems)


def test_the_validator_reports_a_model_spec_that_carries_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "DIAGRAM_SPEC_CARRIES_ABSOLUTE_COORDINATES", True)
    problems = contract.validate_contract()
    assert any("must not carry absolute coordinates" in problem for problem in problems)


def test_the_validator_reports_a_fixture_that_forbids_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relaxed = tuple(
        contract.AcceptanceFixture(
            fixture.key, fixture.name, fixture.input_shape, fixture.must_observe, ()
        )
        if fixture.key == "B"
        else fixture
        for fixture in contract.ACCEPTANCE_FIXTURES
    )
    monkeypatch.setattr(contract, "ACCEPTANCE_FIXTURES", relaxed)
    problems = contract.validate_contract()
    assert any("fixture B must declare what it must not observe" in problem for problem in problems)


def test_the_validator_reports_a_demoted_p0_defect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    severity, key, note = contract.DEFECT_REGISTER[1]
    demoted = (
        contract.DEFECT_REGISTER[0],
        ("P1", key, note),
        *contract.DEFECT_REGISTER[2:],
    )
    monkeypatch.setattr(contract, "DEFECT_REGISTER", demoted)
    problems = contract.validate_contract()
    assert any("must both be P0" in problem for problem in problems)


def test_the_validator_reports_a_defect_register_missing_the_silent_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trimmed = tuple(
        entry for entry in contract.DEFECT_REGISTER if entry[1] != "silent_partial_compilation"
    )
    monkeypatch.setattr(contract, "DEFECT_REGISTER", trimmed)
    problems = contract.validate_contract()
    assert any("must name 'silent_partial_compilation'" in problem for problem in problems)


def test_the_validator_reports_cad_creeping_into_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "CAD_IS_A_RUNTIME_INPUT", True)
    problems = contract.validate_contract()
    assert any("CAD must not be a runtime input" in problem for problem in problems)


def test_the_validator_reports_the_representation_ratio_being_re_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "FORBIDDEN_FIDELITY_METRICS", ())
    problems = contract.validate_contract()
    assert any("representation-mismatched fidelity ratio" in problem for problem in problems)
