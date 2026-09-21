"""M5 §D tests: the safety-negative suite must be complete, stable and never write.

The suite is a hard gate (100% required), so its *shape* is part of the contract: eleven of the
baseline's twelve required situations have to be present by name, every case has to end with an
untouched drawing and a stable machine answer, and the whole thing has to be reproducible.
"""

from __future__ import annotations

from pathlib import Path

from agentcad.drafting_geometry import drafting_content_hash
from agentcad.repair_benchmark_runner import BenchmarkContext
from agentcad.repair_safety import SAFE_STATUSES, SAFETY_CASES, run_safety_suite
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry
from agentcad.validation_profile import built_in_profile, resolve_profile


def _context(tmp_path: Path) -> BenchmarkContext:
    service = DocumentService(SQLiteDocumentStore(tmp_path / "safety.db"), SymbolRegistry())
    return BenchmarkContext(
        service=service,
        profile=resolve_profile(built_in_profile()),
        registry=service.symbols,
    )


def test_the_suite_covers_every_required_situation():
    case_ids = {case.case_id for case in SAFETY_CASES}
    required = {
        "d1_ambiguous_delete",
        "d2_locked_layer",
        "d3_locked_element",
        "d4_scope_exceeded",
        "d5_no_unique_repair",
        "d6_unregistered_finding",
        "d7_required_validator_skipped",
        "d8_stale_revision",
        "d9_rule_bundle_changed",
        "d10_waiver_request",
        "d11_release_state_request",
        "d12_raw_bypass_mutation",
    }
    assert required <= case_ids
    assert len(SAFETY_CASES) >= 12


def test_every_safety_case_leaves_the_drawing_alone(tmp_path: Path):
    context = _context(tmp_path)
    result = run_safety_suite(context)
    assert result.failures == []
    assert result.passed == result.total >= 12
    for outcome in result.outcomes:
        assert outcome.wrote_document is False, outcome.case_id
        assert outcome.content_unchanged is True, outcome.case_id
        assert outcome.revision_before == outcome.revision_after, outcome.case_id
        assert outcome.reason_codes, outcome.case_id


def test_every_safety_case_answers_with_a_stable_status(tmp_path: Path):
    context = _context(tmp_path)
    result = run_safety_suite(context)
    for outcome in result.outcomes:
        assert outcome.status in SAFE_STATUSES, (outcome.case_id, outcome.status)
        # ``repaired`` is the one answer no safety case may give.
        assert outcome.status != "repaired"


def test_the_suite_is_reproducible(tmp_path: Path):
    first = run_safety_suite(_context(tmp_path / "a"))
    second = run_safety_suite(_context(tmp_path / "b"))
    assert first.payload() == second.payload()


def test_a_locked_element_is_a_policy_refusal_not_a_budget_failure(tmp_path: Path):
    context = _context(tmp_path)
    result = run_safety_suite(context)
    locked = {
        outcome.case_id: outcome
        for outcome in result.outcomes
        if outcome.case_id in {"d2_locked_layer", "d3_locked_element"}
    }
    for outcome in locked.values():
        assert outcome.status == "policy_violation"
        assert "policy_violation" in outcome.reason_codes


def test_f3_creation_without_the_case_policy_is_refused(tmp_path: Path):
    """A repair that has to add equipment needs the case to have permitted it."""

    from agentcad.repair_benchmark import MUTATIONS
    from agentcad.repair_orchestrator import (
        RepairOrchestrator,
        build_repair_request,
    )
    from agentcad.validation_engine import run_validation

    context = _context(tmp_path)
    service = context.service
    operator = MUTATIONS["f3_delete_middle_detach"]
    drawing = operator.document_builder(service, service.symbols)
    mutation = operator.apply(service, drawing, __import__("random").Random(3))
    document = service.get_document(drawing.document_id)
    content_before = drafting_content_hash(document)
    result = run_validation(document, service.symbols, context.profile, service=service)
    issue = next(
        issue
        for issue in result.issues
        if issue.code == mutation.target_code
        and set(issue.element_ids) & set(mutation.target_element_ids)
    )
    request = build_repair_request(
        document,
        result,
        issue,
        registry=service.symbols,
        profile=context.profile,
        hop=2,
        declared_by="case_spec",
        permits_creation=False,
    )
    run = RepairOrchestrator(service, context.profile).run(request)
    assert run.status == "human_required"
    assert drafting_content_hash(service.get_document(document.id)) == content_before
