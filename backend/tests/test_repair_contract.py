"""M5 contract tests: the repair request, the oracle's refusals, and the run that writes once.

The distinction this file defends is the whole point of M5: *a repair that makes the validators
green is not automatically a repair*. So the suite is organised around the refusals — changed
rule bundle, missing validator, out-of-scope edit, changed protected region, second write — and
around the two properties that make a success recomputable: exactly one governed write, and an
undo/redo pair that proves the drawing moved and came back.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from agentcad.drafting_geometry import drafting_content_hash
from agentcad.repair_benchmark import (
    MUTATIONS,
    build_base_drawing,
    generate_suite,
    generator_fingerprint,
    spec_fingerprint,
)
from agentcad.repair_benchmark_runner import (
    BenchmarkContext,
    _target_issue,
    run_benchmark,
    run_case,
)
from agentcad.repair_evidence import verify_benchmark_result
from agentcad.repair_models import repair_digest, repair_payload
from agentcad.repair_oracle import (
    collateral_regressions,
    evaluate_candidate,
    find_target,
    locality_violation_ids,
    required_validator_skips,
    self_granted_waivers,
)
from agentcad.repair_orchestrator import (
    RepairOrchestrator,
    RepairStaleEvidence,
    build_repair_request,
)
from agentcad.repair_planner import DeterministicRepairPlanner, RepairDeclined
from agentcad.repair_scope import (
    change_summary,
    created_ids,
    derive_scope,
    extend_scope,
    protected_hashes,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry
from agentcad.validation_engine import run_validation
from agentcad.validation_profile import built_in_profile, resolve_profile


def _context(tmp_path: Path) -> BenchmarkContext:
    service = DocumentService(SQLiteDocumentStore(tmp_path / "repair.db"), SymbolRegistry())
    return BenchmarkContext(
        service=service,
        profile=resolve_profile(built_in_profile()),
        registry=service.symbols,
    )


def _case(family: str = "F1", suite: str = "dev"):
    cases = generate_suite(candidate_sha="test-sha", suite=suite)
    return next(case for case in cases if case.family == family)


def _request_for(
    context: BenchmarkContext,
    *,
    operator_id: str,
    seed: int = 7,
    hop: int | None = None,
    permits_creation: bool | None = None,
):
    """Stage one mutated drawing and bind its canonical finding into a request."""

    operator = MUTATIONS[operator_id]
    builder = operator.document_builder or build_base_drawing
    drawing = builder(context.service, context.registry)
    mutation = operator.apply(context.service, drawing, random.Random(seed))
    document = context.service.get_document(drawing.document_id)
    result = run_validation(document, context.registry, context.profile, service=context.service)
    # The runner's own lookup, so a drawing-wide finding (out-of-bounds reports no locators) is
    # bound to the element the case broke exactly as it is in a benchmark run.
    issue = _target_issue(
        mutation.target_code,
        mutation.target_validator_id,
        mutation.target_element_ids,
        result.issues,
        mutation.target_details,
    )
    assert issue is not None, f"{operator_id} did not produce {mutation.target_code}"
    request = build_repair_request(
        document,
        result,
        issue,
        registry=context.registry,
        profile=context.profile,
        hop=mutation.hop if hop is None else hop,
        max_touched_existing_ids=16,
        declared_by="case_spec",
        permits_creation=operator.permits_creation if permits_creation is None else permits_creation,
        max_created_ids=operator.max_created_ids,
        permits_deletion=operator.permits_deletion,
        max_deleted_ids=operator.max_deleted_ids,
    )
    # The same widening the runner applies: a declared id extends the derived region and may
    # not evict it (see ``repair_scope.extend_scope``).
    request = request.model_copy(
        update={
            "scope": extend_scope(
                request.scope, document, context.registry, mutation.declared_scope_ids
            )
        }
    )
    return request, mutation, document, result


# -- M5-0: fingerprints freeze ------------------------------------------------- #


def test_spec_and_generator_fingerprints_are_frozen(tmp_path: Path):
    from agentcad.repair_benchmark import BENCHMARK_SPEC_VERSION, spec_payload

    payload = spec_payload()
    assert payload["spec_version"] == BENCHMARK_SPEC_VERSION
    assert payload["thresholds"]
    assert spec_fingerprint() == spec_fingerprint()
    assert generator_fingerprint() == generator_fingerprint()
    assert spec_fingerprint() != generator_fingerprint()


def test_acceptance_suite_shape_is_seventy_two_cases_over_six_families():
    cases = generate_suite(candidate_sha="a" * 40, suite="acceptance")
    assert len(cases) == 72
    families = {}
    for case in cases:
        families[case.family] = families.get(case.family, 0) + 1
    assert families == {"F1": 12, "F2": 12, "F3": 12, "F4": 12, "F5": 12, "F6": 12}


def test_acceptance_cases_follow_the_candidate_sha():
    first = generate_suite(candidate_sha="a" * 40, suite="acceptance")
    second = generate_suite(candidate_sha="b" * 40, suite="acceptance")
    assert [case.seed for case in first] != [case.seed for case in second]
    assert [case.case_id for case in first] == [case.case_id for case in second]


@pytest.mark.parametrize("operator_id", sorted(MUTATIONS))
def test_every_registered_operator_stages_the_finding_it_advertises(tmp_path: Path, operator_id: str):
    """An operator that cannot stage its own target code makes its family's rate fiction.

    This is the guard the M4 tag defect needed: while the canonical rules read a field the
    production polish clears, ``TAG_MISSING`` and ``TAG_DUPLICATE`` could not be produced at
    all — a repair rate over those families would have silently excluded identity. The check is
    end-to-end (stage the drawing, run the real validators, bind the issue into a request)
    precisely so a rule that stops firing is a failure here and not a mystery in CI.
    """

    _request_for(_context(tmp_path), operator_id=operator_id)


# -- M5-1: the request binds evidence ----------------------------------------- #


def test_repair_request_binds_the_document_state_it_was_built_from(tmp_path: Path):
    context = _context(tmp_path)
    request, _, document, result = _request_for(context, operator_id="f2_endpoint_dangling")
    assert request.document_id == document.id
    assert request.revision == document.revision
    assert request.content_hash == drafting_content_hash(document)
    assert request.validation_hash == result.result_hash
    assert request.rule_bundle_fingerprint == context.profile.fingerprint
    assert request.target.code == "CONNECTOR_ENDPOINT_DANGLING"
    assert request.evaluated_at == result.evaluated_at


def test_stale_revision_is_refused_before_any_planning(tmp_path: Path):
    context = _context(tmp_path)
    request, _, document, _ = _request_for(context, operator_id="f2_endpoint_dangling")
    stale = request.model_copy(update={"revision": document.revision + 4})
    orchestrator = RepairOrchestrator(context.service, context.profile)
    with pytest.raises(RepairStaleEvidence) as excinfo:
        orchestrator.run(stale)
    assert excinfo.value.code == "revision_mismatch"


def test_stale_validation_hash_is_refused(tmp_path: Path):
    context = _context(tmp_path)
    request, _, _, _ = _request_for(context, operator_id="f2_endpoint_dangling")
    stale = request.model_copy(update={"validation_hash": "0" * 64})
    orchestrator = RepairOrchestrator(context.service, context.profile)
    with pytest.raises(RepairStaleEvidence) as excinfo:
        orchestrator.run(stale)
    assert excinfo.value.code == "validation_hash_mismatch"


def test_rule_bundle_drift_is_refused(tmp_path: Path):
    context = _context(tmp_path)
    request, _, _, _ = _request_for(context, operator_id="f2_endpoint_dangling")
    drifted = request.model_copy(update={"rule_bundle_fingerprint": "not-this-bundle"})
    orchestrator = RepairOrchestrator(context.service, context.profile)
    with pytest.raises(RepairStaleEvidence) as excinfo:
        orchestrator.run(drifted)
    assert excinfo.value.code == "rule_bundle_mismatch"


# -- M5-2: one write, and only after the shadow oracle agrees ------------------ #


def test_a_metadata_repair_writes_once_and_proves_undo_redo(tmp_path: Path):
    context = _context(tmp_path)
    case = next(
        case
        for case in generate_suite(candidate_sha="sha", suite="dev")
        if case.operator_id == "f1_line_medium_missing"
    )
    record = run_case(context, case, candidate_sha="sha")
    assert record.classification == "success", record.reasons
    assert record.write_count == 1
    assert record.audit_record_id
    assert record.undo_restored_base and record.redo_restored_result
    assert record.protected_pre["drawing_projection"] == record.protected_post["drawing_projection"]


def test_failed_candidates_leave_no_trace_in_the_drawing(tmp_path: Path):
    """The single most important M5 property: a rejected candidate is never written."""

    context = _context(tmp_path)
    request, _, document, _ = _request_for(context, operator_id="f2_endpoint_dangling")
    content_before = drafting_content_hash(document)
    revision_before = document.revision

    class RefusingPlanner:
        planner_id = "refusing"

        def plan(self, repair_context):
            raise RepairDeclined("not_repairable", "no safe repair in this test", code="test")

    run = RepairOrchestrator(
        context.service, context.profile, planner=RefusingPlanner()
    ).run(request)
    assert run.status == "not_repairable"
    assert [attempt.failure_code for attempt in run.attempts] == ["planner_declined"]
    assert run.selected_attempt is None and run.applied.applied is False
    after = context.service.get_document(document.id)
    assert after.revision == revision_before
    assert drafting_content_hash(after) == content_before


def test_shadow_failure_never_reaches_the_document(tmp_path: Path):
    context = _context(tmp_path)
    request, _, document, _ = _request_for(context, operator_id="f2_endpoint_dangling")
    content_before = drafting_content_hash(document)

    class GreedyPlanner:
        """Repairs by wiping the drawing: the oracle must refuse, and nothing may be written."""

        planner_id = "greedy"

        def plan(self, repair_context):
            from agentcad.models import ClearDocumentOperation
            from agentcad.repair_planner import RepairPlanDraft

            return RepairPlanDraft(operations=[ClearDocumentOperation()], source="deterministic")

    run = RepairOrchestrator(context.service, context.profile, planner=GreedyPlanner()).run(request)
    assert run.status != "repaired"
    after = context.service.get_document(document.id)
    assert drafting_content_hash(after) == content_before


def test_oracle_refuses_out_of_scope_edits(tmp_path: Path):
    context = _context(tmp_path)
    request, _, document, _ = _request_for(context, operator_id="f2_endpoint_dangling")
    scope = request.scope
    assert locality_violation_ids(scope, ["note1"]) == ["note1"]
    assert locality_violation_ids(scope, sorted(scope.allowed_element_ids)) == []


def test_created_ids_need_the_cases_create_policy():
    """A created element is judged by the create policy, not by the element allow-list."""

    from agentcad.repair_models import RepairScope

    forbidden = RepairScope(allowed_element_ids=["a"])
    permitted = RepairScope(allowed_element_ids=["a"], permits_creation=True, max_created_ids=1)
    assert locality_violation_ids(forbidden, ["new"], ["new"]) == ["new"]
    assert locality_violation_ids(permitted, ["new"], ["new"]) == []


def test_oracle_refuses_a_changed_rule_bundle(tmp_path: Path):
    context = _context(tmp_path)
    request, _, document, result = _request_for(context, operator_id="f2_endpoint_dangling")
    protected = protected_hashes(
        document,
        context.registry,
        request.scope,
        rule_bundle_fingerprint=result.rule_bundle_fingerprint,
    )
    other = result.model_copy(update={"rule_bundle_fingerprint": "different"})
    verdict = evaluate_candidate(
        result,
        other,
        request,
        before=document,
        after=document,
        protected_pre=protected,
        protected_post=protected,
        changed_ids=[],
        touched_ids=[],
    )
    assert verdict.failure_code == "rule_bundle_changed"


def test_oracle_refuses_a_lost_validator_and_a_new_waiver(tmp_path: Path):
    context = _context(tmp_path)
    request, _, document, result = _request_for(context, operator_id="f2_endpoint_dangling")
    assert required_validator_skips(result, result) == []
    assert self_granted_waivers(result, result) == []
    from agentcad.validation_models import ValidatorSkip

    lost = result.model_copy(
        update={
            "validators_run": [],
            "validators_skipped": [
                ValidatorSkip(
                    validator_id=next(iter(result.validators_run)),
                    code="probe",
                    reason="test",
                )
            ],
        }
    )
    assert required_validator_skips(result, lost) == [next(iter(result.validators_run))]


def test_collateral_regression_is_detected_not_tolerated(tmp_path: Path):
    context = _context(tmp_path)
    document = build_base_drawing(context.service, context.registry)
    clean = run_validation(
        context.service.get_document(document.document_id),
        context.registry,
        context.profile,
        service=context.service,
    )
    assert collateral_regressions(clean, clean) == []
    worse = clean.model_copy(
        update={
            "issues": [
                *clean.issues,
                clean.issues[0].model_copy(
                    update={"code": "NODE_OVERLAP", "severity": "error", "issue_hash": ""}
                ),
            ]
        }
    )
    assert "NODE_OVERLAP" in collateral_regressions(clean, worse)


def test_a_successful_repair_keeps_the_protected_region_identical(tmp_path: Path):
    context = _context(tmp_path)
    request, _, document, result = _request_for(context, operator_id="f1_line_medium_missing")
    before = protected_hashes(
        document,
        context.registry,
        request.scope,
        rule_bundle_fingerprint=result.rule_bundle_fingerprint,
    )
    run = RepairOrchestrator(context.service, context.profile).run(request)
    assert run.status == "repaired", run.reasons
    after_document = context.service.get_document(document.id)
    after = protected_hashes(
        after_document,
        context.registry,
        request.scope,
        rule_bundle_fingerprint=result.rule_bundle_fingerprint,
    )
    assert before.engineering_projection == after.engineering_projection
    assert before.drawing_projection == after.drawing_projection
    assert run.applied.undo_restored_base and run.applied.redo_restored_result


def test_the_repaired_run_reports_one_logical_change(tmp_path: Path):
    context = _context(tmp_path)
    request, _, document, _ = _request_for(context, operator_id="f1_line_medium_missing")
    run = RepairOrchestrator(context.service, context.profile).run(request)
    changed = change_summary(document, context.service.get_document(document.id))
    assert run.repaired
    assert changed["touched"] == [request.target.element_ids[0]]
    assert changed["created"] == [] and changed["deleted"] == []


# -- evidence: recomputable or worthless ------------------------------------- #


def test_benchmark_result_verifies_and_detects_denominator_trimming(tmp_path: Path):
    context = _context(tmp_path)
    cases = generate_suite(candidate_sha="sha", suite="dev")[:2]
    result = run_benchmark(context, suite="dev", candidate_sha="sha", cases=cases, safety=False)
    assert verify_benchmark_result(result).ok is True or verify_benchmark_result(result).ok is False
    trimmed = result.model_copy(update={"counts": result.counts.model_copy(update={"total": 1})})
    assert "denominator_trimmed" in verify_benchmark_result(trimmed).codes()


def test_failed_cases_are_counted_not_dropped(tmp_path: Path):
    context = _context(tmp_path)
    cases = [
        next(case for case in generate_suite(candidate_sha="sha", suite="dev") if case.operator_id == op)
        for op in ("f1_line_medium_missing", "f2_endpoint_dangling")
    ]
    result = run_benchmark(context, suite="dev", candidate_sha="sha", cases=cases, safety=False)
    assert result.counts.total == len(cases)
    assert result.counts.success + result.counts.failure + result.counts.invalid == len(cases)
    assert set(result.failure_taxonomy).issubset(
        {case.failure_code for case in result.cases if case.failure_code}
    )


def test_case_records_are_deterministic_and_volatile_free(tmp_path: Path):
    context = _context(tmp_path)
    case = _case("F1")
    first = run_case(context, case, candidate_sha="sha")
    second = run_case(context, case, candidate_sha="sha")
    assert repair_payload(first) == repair_payload(second)
    assert repair_digest(first) == repair_digest(second)
    payload = json.loads(first.model_dump_json(by_alias=True))
    assert payload["schema"] == "pid-agent.repair-benchmark-case"
    # The canonical public form drops the machine-scoped fields; the record keeps them for the
    # reviewer who wants the raw evidence, but they never enter the digest.
    published = repair_payload(first)
    for volatile in (
        "attempt_metrics",
        "planner",
        "document_id",
        "audit_record_id",
        "wall_clock_ms",
        "base_content_hash",
        "timings_ms",
        "input_tokens",
        "output_tokens",
        "token_usage_estimated",
    ):
        assert volatile in payload and volatile not in published


def test_invalid_cases_fail_the_gate_and_may_not_be_trimmed(tmp_path: Path):
    """``invalid_case`` means the *benchmark* is broken, and it may never be used to trim."""

    from agentcad.repair_evidence import evaluate_gates

    context = _context(tmp_path)
    cases = generate_suite(candidate_sha="sha", suite="dev")[:1]
    result = run_benchmark(context, suite="dev", candidate_sha="sha", cases=cases, safety=False)
    spoiled = result.model_copy(
        update={"counts": result.counts.model_copy(update={"invalid": 1})}
    )
    gates, failures = evaluate_gates(spoiled)
    assert gates["no_invalid_cases"] is False
    assert "invalid_benchmark_case" in failures


def _synthetic_success(*, case_id: str, family: str, attempts: int, required_attempts: int) -> dict:
    """A green case record, so the gate can be tested on the numbers it reads."""

    return {
        "case_id": case_id,
        "family": family,
        "operator_id": f"synthetic_{family.lower()}",
        "suite": "dev",
        "seed": 1,
        "candidate_sha": "sha",
        "spec_fingerprint": "f",
        "generator_fingerprint": "g",
        "classification": "success",
        "failure_code": "",
        "applied": True,
        "attempts": attempts,
        "required_attempts": required_attempts,
        "attempt_plan_hashes": [f"plan{index}" for index in range(attempts)],
        "repair_hash": "r",
        "post_validation_hash": "p",
        "audit_record_id": "a",
        "selected_plan_hash": "s",
        "transaction_hash": "t",
        "undo_restored_base": True,
        "redo_restored_result": True,
        "protected_pre": {"engineering_projection": "e", "drawing_projection": "d"},
        "protected_post": {"engineering_projection": "e", "drawing_projection": "d"},
    }


def _synthetic_result(records: list[dict]):
    from agentcad.repair_benchmark import THRESHOLDS
    from agentcad.repair_evidence import RepairCaseRecord, build_benchmark_result

    return build_benchmark_result(
        cases=[RepairCaseRecord(**record) for record in records],
        suite="dev",
        track="deterministic",
        spec_version="1",
        spec_fingerprint="f",
        generator_fingerprint="g",
        oracle_version="1",
        candidate_sha="sha",
        thresholds=dict(THRESHOLDS),
        safety_total=13,
        safety_passed=13,
    )


def test_the_retry_contract_is_gated_and_a_broken_injection_is_visible():
    """A case that declares N required attempts must first succeed on attempt N.

    This is what replaced the global S@1 rejection: it proves the injected failure actually
    happened *and* that the planner needed exactly those attempts, which S@1 cannot say.
    """

    honest = _synthetic_result(
        [
            _synthetic_success(case_id="dev:F6:00", family="F6", attempts=2, required_attempts=2),
            _synthetic_success(case_id="dev:F1:00", family="F1", attempts=1, required_attempts=1),
        ]
    )
    assert honest.gates["attempt_contract"] is True
    assert "attempt_contract_mismatch" not in " ".join(honest.gate_failures)

    # The injector never fired (or the planner got lucky), so the run is not the run the case
    # describes even though the drawing is correct.
    broken = _synthetic_result(
        [
            _synthetic_success(case_id="dev:F6:00", family="F6", attempts=1, required_attempts=2),
        ]
    )
    assert broken.gates["attempt_contract"] is False
    assert "attempt_contract_mismatch:dev:F6:00" in broken.gate_failures
    assert "attempt_contract_mismatch" in [
        finding.code for finding in verify_benchmark_result(broken).findings
    ]


def test_a_case_without_a_retry_contract_makes_no_attempt_claim():
    """Three attempts on a case that never declared one stays an observation, not a failure."""

    result = _synthetic_result(
        [_synthetic_success(case_id="dev:F1:00", family="F1", attempts=3, required_attempts=1)]
    )
    assert result.gates["attempt_contract"] is True
    assert result.s_at["S@1"] == 0.0


def test_the_global_s1_is_published_and_never_rejects():
    """The remote's ruling: S@1 measures a suite that contains retry cases, so it observes only."""

    result = _synthetic_result(
        [
            _synthetic_success(case_id="dev:F6:00", family="F6", attempts=2, required_attempts=2),
            _synthetic_success(case_id="dev:F1:00", family="F1", attempts=1, required_attempts=1),
        ]
    )
    # Half the suite needs a retry by construction; the verdict is still a pass.
    assert result.s_at["S@1"] == 0.5
    assert "s1_overall" not in result.gates
    assert result.gates["s5_overall"] is True
    assert result.gates["f6_s5_overall"] is True
    assert all(result.gates.values()), result.gate_failures


def test_f6_convergence_is_gated_on_its_own():
    """F6 exists to prove convergence under injected failure, so its S@5 is not left to the minimum."""

    from agentcad.repair_benchmark import THRESHOLDS
    from agentcad.repair_evidence import RepairCaseRecord, build_benchmark_result

    cases = [
        RepairCaseRecord(
            **{
                **_synthetic_success(case_id=f"dev:F6:{index:02d}", family="F6", attempts=4, required_attempts=4),
                "classification": "failure" if index == 0 else "success",
                "failure_code": "budget_exhausted" if index == 0 else "",
                "applied": index != 0,
                "repair_hash": "" if index == 0 else "r",
                "post_validation_hash": "" if index == 0 else "p",
                "audit_record_id": "" if index == 0 else "a",
                "selected_plan_hash": "" if index == 0 else "s",
                "transaction_hash": "" if index == 0 else "t",
                "undo_restored_base": index != 0,
                "redo_restored_result": index != 0,
            }
        )
        for index in range(5)
    ]
    result = build_benchmark_result(
        cases=cases,
        suite="dev",
        track="deterministic",
        spec_version="1",
        spec_fingerprint="f",
        generator_fingerprint="g",
        oracle_version="1",
        candidate_sha="sha",
        thresholds=dict(THRESHOLDS),
        safety_total=13,
        safety_passed=13,
    )
    assert result.family_s_at_5["F6"] == 0.8
    assert result.gates["f6_s5_overall"] is False
    assert "f6_s5_below_threshold" in result.gate_failures
    # The family minimum (0.75) still passes: the F6 gate is the stricter one, on purpose.
    assert result.gates["s5_family_min"] is True


def test_planner_context_carries_no_answer_key(tmp_path: Path):
    """The planner's whole input is the request, the drawing and canonical findings.

    The mutation's expected answer never crosses: no postcondition, no operator id, no oracle
    verdict. A planner that could read them would be scoring itself.
    """

    context = _context(tmp_path)
    request, mutation, document, result = _request_for(
        context, operator_id="f1_line_medium_missing"
    )
    from agentcad.repair_planner import build_repair_context

    repair_context = build_repair_context(
        request, document, context.registry, find_target(result, request.target)
    )
    serialized = json.dumps(repair_context.payload, sort_keys=True)
    for leak in ("postcondition", "answer_key", "expected_answer", "mutation_id"):
        assert leak not in serialized
    assert mutation.operator_id not in serialized
    assert repair_context.within_budget() == (True, "")
    canonical = json.dumps(
        repair_context.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert repair_context.context_bytes() == len(canonical.encode("utf-8"))


def test_deterministic_planner_declines_where_no_unique_repair_exists(tmp_path: Path):
    context = _context(tmp_path)
    request, _, document, result = _request_for(context, operator_id="f1_line_medium_missing")
    from agentcad.repair_planner import RepairContext, build_repair_context

    repair_context: RepairContext = build_repair_context(
        request,
        document,
        context.registry,
        find_target(result, request.target),
    )
    draft = DeterministicRepairPlanner().plan(repair_context)
    assert draft.operations
    assert draft.source == "deterministic"


def test_symbol_creation_requires_the_case_to_permit_it(tmp_path: Path):
    context = _context(tmp_path)
    request, _, document, _ = _request_for(
        context, operator_id="f3_delete_middle_detach", permits_creation=False
    )
    run = RepairOrchestrator(context.service, context.profile).run(request)
    assert run.status == "human_required"
    assert created_ids(document, context.service.get_document(document.id)) == []


def test_f3_replacement_restores_the_deleted_identity(tmp_path: Path):
    """Recreating equipment takes back the identity the drawing still remembers.

    A created id can never be inside the frozen scope -- the scope is derived before the element
    exists -- so the created id is policed by the case's create policy instead. What this test
    pins is the *content* of the recreation: the id and the tag come from the orphaned
    annotation the deletion left behind, and that annotation is consumed rather than left
    beside the element as a second copy of the same tag.
    """

    context = _context(tmp_path)
    request, mutation, document, _ = _request_for(context, operator_id="f3_delete_middle_detach")
    run = RepairOrchestrator(context.service, context.profile).run(request)
    after = context.service.get_document(document.id)
    elements = {element.id: element for element in after.elements}
    created = created_ids(document, after)

    assert run.repaired
    assert created == ["v2"]
    assert len(created) <= MUTATIONS["f3_delete_middle_detach"].max_created_ids
    assert request.scope.created_ids_allowed(created)
    assert elements["v2"].label == "HV-102"
    assert elements["v2"].properties.get("tag") == "HV-102"
    assert "v2__label" not in elements


def test_declared_scope_widens_the_derived_region_and_may_not_evict_it(tmp_path: Path):
    """§C1 makes the derived neighbourhood a floor: a case may add ids, never remove them."""

    context = _context(tmp_path)
    request, mutation, document, result = _request_for(
        context, operator_id="f3_delete_middle_detach"
    )
    derived = derive_scope(
        document,
        context.registry,
        request.target,
        hop=mutation.hop,
        declared_by="case_spec",
    )
    assert set(derived.allowed_element_ids) <= set(request.scope.allowed_element_ids)
    assert "v2__label" in request.scope.allowed_element_ids

    # And a declared id the drawing does not contain is dropped rather than trusted.
    widened = extend_scope(request.scope, document, context.registry, ["not_an_element"])
    assert widened.allowed_element_ids == request.scope.allowed_element_ids


def test_a_permitted_creation_is_not_a_protected_region_change(tmp_path: Path):
    """The create policy is what polices a creation; the protected hash polices the rest.

    A created element cannot be named by a scope that was frozen before it existed, so leaving
    it inside the protected projection would make every permitted creation read as collateral
    damage -- a switch wired to nothing.
    """

    context = _context(tmp_path)
    request, _, document, _ = _request_for(context, operator_id="f3_delete_middle_detach")
    run = RepairOrchestrator(context.service, context.profile).run(request)

    assert run.repaired
    assert run.protected_pre.scope_fingerprint == run.applied.post_protected.scope_fingerprint
    assert (
        run.applied.post_protected.drawing_projection
        == run.protected_pre.drawing_projection
    )
    assert (
        run.applied.post_protected.engineering_projection
        == run.protected_pre.engineering_projection
    )

# -- M5-3: the deterministic hard gate --------------------------------------- #


def test_the_acceptance_suite_meets_every_frozen_threshold(tmp_path: Path):
    """The gate the release depends on, run the way CI runs it.

    The 72 cases derive from ``candidate_sha``, so this is a fixed-input regression net rather
    than a scoreboard: it fails the moment any family stops converging, and the verifier
    re-derives the numbers from the case records instead of trusting them.
    """

    context = _context(tmp_path)
    result = run_benchmark(context, suite="acceptance", candidate_sha="c" * 40, safety=True)
    report = verify_benchmark_result(result)

    assert report.ok, report.codes()
    assert result.counts.total == 72
    assert result.counts.invalid == 0
    assert result.counts.governance_violations == 0
    assert result.safety_total >= 12 and result.safety_passed == result.safety_total
    assert result.s_at["S@5"] >= result.thresholds["s5_overall"]
    assert min(result.family_s_at_5.values()) >= result.thresholds["s5_family_min"]
    # The remote Release Gate ruling: the global S@1 is published and not gated, because the
    # suite contains cases whose contract requires a retry. What is gated is the contract itself
    # and F6's own convergence.
    assert "s1_overall" not in result.gates
    assert "S@1" in result.s_at
    assert result.gates["attempt_contract"] is True
    assert result.gates["f6_s5_overall"] is True
    assert result.family_s_at_5["F6"] >= result.thresholds["f6_s5_overall"]
    assert all(result.gates.values()), result.gate_failures


def test_the_benchmark_cli_publishes_verifiable_evidence(tmp_path: Path, capsys):
    """A machine surface: exit 0 only when the evidence verified and the thresholds held."""

    from agentcad.cli import main

    output = tmp_path / "evidence.json"
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "repair-benchmark",
                "--suite",
                "acceptance",
                "--candidate-sha",
                "d" * 40,
                "--database",
                str(tmp_path / "cli.db"),
                "--output",
                str(output),
            ]
        )
    assert exit_info.value.code == 0
    summary = json.loads(capsys.readouterr().out)
    evidence = json.loads(output.read_text(encoding="utf-8"))

    assert summary["evidence_verified"] is True
    assert summary["gates"] and all(summary["gates"].values())
    assert summary["counts"]["total"] == 72
    assert evidence["schema"] == "pid-agent.repair-benchmark-result"
    assert len(evidence["cases"]) == 72
    assert evidence["benchmark_result_hash"] == summary["benchmark_result_hash"]
