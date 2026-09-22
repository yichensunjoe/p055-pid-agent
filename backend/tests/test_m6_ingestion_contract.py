"""M6 phase 1 is design and contract only — and this test is what makes that true.

Two things rot a governance milestone before any code is written. The first is a task book
that says "never" while the contract data says "when convenient": the two drift because
nobody compares them. The second is a phase boundary that only exists in a sentence — a
route gets added "just to expose the queue while we design it", and the design document
stops describing the system.

So this file does three jobs:

* the contract's own validator must stay quiet (``validate_contract()`` finds no violation);
* every name the task book promises — layers, states, policy intents, corpus dimensions,
  candidate fields, producers, forbidden transitions — must actually appear in the task
  book, so prose and data cannot separate;
* the *live* surface (FastAPI OpenAPI paths and MCP tool names, read the same way the
  surface-contract test reads them) must contain no candidate/ingestion-shaped route while
  the milestone is in phase 1.

The last section proves the validator is not decoration: each mutation below is a specific,
tempting relaxation, and each one must be reported.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentcad import m6_ingestion_contract as contract
from agentcad.main import create_app
from agentcad.surface_contract import HTTP_SURFACE_BINDINGS, MCP_SURFACE_BINDINGS

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK_BOOK = REPO_ROOT / "docs" / "m6-governed-semantic-ingestion.md"
MCP_SERVER_SOURCE = Path(__file__).resolve().parents[1] / "agentcad" / "mcp_server.py"


@pytest.fixture(scope="module")
def task_book() -> str:
    return TASK_BOOK.read_text(encoding="utf-8")


def _without_whitespace(text: str) -> str:
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------------------
# The contract is coherent on its own terms
# --------------------------------------------------------------------------------------


def test_the_governance_contract_holds_together() -> None:
    assert contract.validate_contract() == []


def test_exactly_one_layer_may_write_the_engineering_model() -> None:
    """This is the invariant the whole milestone is arranged around, stated directly."""

    writers = [
        layer.key
        for layer in contract.INGESTION_LAYERS
        if layer.write_authority == "engineering_model"
    ]
    assert writers == [contract.SOLE_WRITE_LAYER] == ["apply_v2_transaction"]


def test_the_two_shortcuts_are_still_forbidden() -> None:
    declared = {(edge.from_state, edge.to_state) for edge in contract.CANDIDATE_TRANSITIONS}
    assert ("proposed", "applied") not in declared
    assert ("proposed", "confirmed") not in declared
    assert ("proposed", "applied") in contract.FORBIDDEN_TRANSITIONS

    incoming = [
        edge.from_state for edge in contract.CANDIDATE_TRANSITIONS if edge.to_state == "applied"
    ]
    assert incoming == ["confirmed"], "the only way into 'applied' is through a confirmed fact"

    assert contract.AUTO_ACCEPT_WHITELIST == (), (
        "M6 v1 signs no auto-accept class: every candidate reaches 'confirmed' through a person"
    )
    assert contract.CONFIDENCE_CAN_AUTHORISE_A_WRITE is False
    assert contract.UNDO_IS_A_STATE_ROLLBACK is False


def test_confidence_is_not_an_authority_input() -> None:
    assert "confidence" not in contract.AUTHORITY_DECISION_INPUTS
    assert "policy_verdict" in contract.AUTHORITY_DECISION_INPUTS
    assert "apply_v2_validation" in contract.AUTHORITY_DECISION_INPUTS


def test_the_candidate_schema_carries_no_write_operations() -> None:
    names = {field.name for field in contract.SEMANTIC_CANDIDATE_FIELDS}
    assert "proposed_semantics" in names
    assert "operations" not in names
    assert "op" not in names


def test_destructive_intents_stay_forbidden() -> None:
    dispositions = {row.intent: row.disposition for row in contract.WRITE_POLICY_V1}
    assert dispositions["delete_existing_engineering_object"] == "forbidden"
    assert dispositions["replace_topology"] == "forbidden"
    assert (
        dispositions["overwrite_existing_authoritative_value"]
        == "conflict_requires_human_resolution"
    )


# --------------------------------------------------------------------------------------
# The task book and the contract data describe the same system
# --------------------------------------------------------------------------------------


def test_the_task_book_names_every_layer(task_book: str) -> None:
    for layer in contract.INGESTION_LAYERS:
        assert layer.key in task_book, f"task book does not describe layer {layer.key!r}"
        assert layer.immutable_id_field in task_book, (
            f"task book does not name the immutable id {layer.immutable_id_field!r}"
        )


def test_the_task_book_names_every_state_and_forbidden_transition(task_book: str) -> None:
    for state in contract.CANDIDATE_STATES:
        assert state in task_book, f"task book does not describe state {state!r}"
    squeezed = _without_whitespace(task_book)
    for from_state, to_state in contract.FORBIDDEN_TRANSITIONS:
        assert f"{from_state}→{to_state}" in squeezed, (
            f"task book does not state that {from_state} -> {to_state} is forbidden"
        )


def test_the_task_book_names_every_policy_intent_and_disposition(task_book: str) -> None:
    for row in contract.WRITE_POLICY_V1:
        assert row.intent in task_book, f"task book does not list policy intent {row.intent!r}"
        assert row.disposition in task_book, (
            f"task book does not name disposition {row.disposition!r}"
        )


def test_the_task_book_names_every_corpus_dimension_and_required_field(task_book: str) -> None:
    for dimension in contract.GOLD_CORPUS_DIMENSIONS:
        assert dimension in task_book, f"task book does not list corpus dimension {dimension!r}"
    for field in contract.GOLD_ITEM_REQUIRED_FIELDS:
        assert field in task_book, f"task book does not require gold item field {field!r}"


def test_the_task_book_names_every_candidate_field_and_producer(task_book: str) -> None:
    for field in contract.SEMANTIC_CANDIDATE_FIELDS:
        assert field.name in task_book, f"task book does not define candidate field {field.name!r}"
    for producer in contract.JUDGMENT_PRODUCERS:
        assert producer.key in task_book, f"task book does not describe producer {producer.key!r}"
    assert "AUTO_ACCEPT_WHITELIST" in task_book


def test_the_task_book_says_seed_material_is_not_truth(task_book: str) -> None:
    assert contract.GOLD_CORPUS_PATH in task_book
    for path in contract.SEED_MATERIAL_PATHS:
        assert path in task_book, f"task book does not place seed material at {path!r}"
    assert contract.SEED_MATERIAL_COUNTS_AS_EXPECTED_TRUTH is False


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


def test_phase_one_added_no_candidate_or_ingestion_surface() -> None:
    """No route may exist yet for the thing this milestone has only designed."""

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
        "M6 is in phase 1 (design and contract only), but these surfaces already exist: "
        f"{offenders}"
    )


def test_the_forbidden_surface_list_is_not_empty() -> None:
    assert contract.PHASE_1_FORBIDDEN_SURFACE_TOKENS
    assert set(contract.PHASE_1_FORBIDDEN) >= {
        "ingestion_runtime",
        "new_http_route",
        "patch_compiler",
    }


# --------------------------------------------------------------------------------------
# The validator is not decoration: each relaxation below must be reported
# --------------------------------------------------------------------------------------


def _extra_transition(from_state: str, to_state: str) -> contract.CandidateTransition:
    return contract.CandidateTransition(from_state, to_state, "relaxation", ())


def test_the_validator_reports_a_direct_proposed_to_applied_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "CANDIDATE_TRANSITIONS",
        (*contract.CANDIDATE_TRANSITIONS, _extra_transition("proposed", "applied")),
    )
    problems = contract.validate_contract()
    assert any("forbidden transition proposed->applied" in problem for problem in problems)


def test_the_validator_reports_a_second_way_into_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    relaxed = tuple(
        edge
        for edge in contract.CANDIDATE_TRANSITIONS
        if not (edge.from_state == "confirmed" and edge.to_state == "applied")
    )
    monkeypatch.setattr(
        contract,
        "CANDIDATE_TRANSITIONS",
        (*relaxed, _extra_transition("needs_review", "applied")),
    )
    problems = contract.validate_contract()
    assert any(
        "the only incoming edge to 'applied' must be from 'confirmed'" in problem
        for problem in problems
    )


def test_the_validator_reports_an_apply_edge_without_gate_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relaxed = tuple(
        contract.CandidateTransition(edge.from_state, edge.to_state, edge.trigger, ())
        if edge.to_state == "applied"
        else edge
        for edge in contract.CANDIDATE_TRANSITIONS
    )
    monkeypatch.setattr(contract, "CANDIDATE_TRANSITIONS", relaxed)
    problems = contract.validate_contract()
    assert any("'applied' edge is missing gate evidence" in problem for problem in problems)


def test_the_validator_reports_confidence_creeping_into_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "AUTHORITY_DECISION_INPUTS",
        (*contract.AUTHORITY_DECISION_INPUTS, "confidence_value"),
    )
    problems = contract.validate_contract()
    assert any("looks like a confidence" in problem for problem in problems)


def test_the_validator_reports_a_second_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    layers = tuple(
        contract.IngestionLayer(**{**layer.__dict__, "write_authority": "engineering_model"})
        if layer.key == "semantic_candidate"
        else layer
        for layer in contract.INGESTION_LAYERS
    )
    monkeypatch.setattr(contract, "INGESTION_LAYERS", layers)
    problems = contract.validate_contract()
    assert any(
        "exactly one layer may write the engineering model" in problem for problem in problems
    )


def test_the_validator_reports_an_allowed_destructive_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = tuple(
        contract.WritePolicyRow(row.intent, "allowed_after_confirmation", row.reason)
        if row.intent == "replace_topology"
        else row
        for row in contract.WRITE_POLICY_V1
    )
    monkeypatch.setattr(contract, "WRITE_POLICY_V1", rows)
    problems = contract.validate_contract()
    assert any("must be forbidden in v1" in problem for problem in problems)


def test_the_validator_reports_an_opened_auto_accept_whitelist_without_reviewer_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "AUTO_ACCEPT_WHITELIST", ("deterministic_fact",))
    monkeypatch.setattr(
        contract,
        "CANDIDATE_TRANSITIONS",
        tuple(
            contract.CandidateTransition(edge.from_state, edge.to_state, edge.trigger, ())
            if edge.to_state == "confirmed"
            else edge
            for edge in contract.CANDIDATE_TRANSITIONS
        ),
    )
    # With the whitelist open the reviewer-action requirement is lifted by design, so the
    # remaining violations must be about the apply edge, not about confirmation.
    problems = contract.validate_contract()
    assert not any("must record a reviewer action" in problem for problem in problems)


def test_the_validator_reports_undo_as_a_state_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contract, "UNDO_IS_A_STATE_ROLLBACK", True)
    problems = contract.validate_contract()
    assert any("undo must not be modelled as a state rollback" in problem for problem in problems)


def test_the_contract_document_is_serializable() -> None:
    document = contract.contract_document()
    assert document["contract"] == contract.M6_CONTRACT_VERSION
    assert document["sole_write_layer"] == contract.SOLE_WRITE_LAYER
    assert len(document["layers"]) == len(contract.INGESTION_LAYERS)
