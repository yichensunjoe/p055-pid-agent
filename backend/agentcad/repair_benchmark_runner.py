"""Runs the M5 benchmark and produces the evidence the release gate re-computes (baseline §B/§N).

The runner owns the three things that decide whether the published success rate means anything:

* **The case set is derived, not chosen.** Cases come from
  :func:`agentcad.repair_benchmark.generate_suite`; a failing case cannot be dropped, and an
  invalid case invalidates the whole run (baseline §A7).
* **The planner gets one oracle-blind entry point.** Track D (deterministic) and track M
  (real model) differ only in which :class:`RepairPlanner` is injected.
* **Writes are counted, not assumed.** Each case measures the document's revision before and
  after; a run that wrote twice, or wrote before the oracle accepted, is a governance
  violation no matter how green the validators are.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .repair_benchmark import (
    BENCHMARK_SPEC_VERSION,
    MUTATIONS,
    SUCCESS_ORACLE_VERSION,
    THRESHOLDS,
    BaseDrawing,
    BenchmarkCase,
    MutationOperator,
    MutationResult,
    build_base_drawing,
    generate_suite,
    generator_fingerprint,
    spec_fingerprint,
)
from .repair_evidence import (
    RepairBenchmarkResult,
    RepairCaseRecord,
    build_benchmark_result,
)
from .repair_models import RepairPlannerIdentity, RepairRunResult
from .repair_orchestrator import RepairOrchestrator, build_repair_request
from .repair_planner import (
    DeterministicRepairPlanner,
    RepairContext,
    RepairPlanDraft,
    RepairPlanner,
)
from .repair_scope import TOUCHED_BUDGET_BY_CLASS, extend_scope
from .service import DocumentService
from .symbols import SymbolRegistry
from .validation_engine import run_validation
from .validation_models import ValidationIssue
from .validation_profile import EffectiveProfile


class BenchmarkSetupError(RuntimeError):
    """The benchmark cannot be set up at all — a generator defect, not a repair failure."""


@dataclass
class BenchmarkContext:
    service: DocumentService
    profile: EffectiveProfile
    registry: SymbolRegistry


class FaultInjectingPlanner:
    """Wraps a planner and makes the first ``failures - 1`` attempts fail on purpose.

    This is how family F6 asks the one question the other families cannot: does the runner
    actually *replan* on structured failure evidence, or does it succeed because the first
    plan happened to be right? The injected fault is a real one — an operation the semantic
    compiler refuses — so the attempt record it produces is ordinary evidence.
    """

    def __init__(self, inner: RepairPlanner, *, failures: int, required_attempts: int) -> None:
        self.inner = inner
        self.failures = failures
        self.required_attempts = required_attempts
        self.planner_id = f"fault-injecting({getattr(inner, 'planner_id', 'unknown')})"

    def identity(self) -> RepairPlannerIdentity:
        describe = getattr(self.inner, "identity", None)
        identity = describe() if callable(describe) else RepairPlannerIdentity(
            planner_id=getattr(self.inner, "planner_id", "unknown")
        )
        return identity.model_copy(
            update={
                "planner_id": self.planner_id,
                "planner_version": f"faults={self.failures},reachable_at={self.required_attempts}",
            }
        )

    def plan(self, context: RepairContext) -> RepairPlanDraft:
        if context.attempt <= self.failures:
            from .agent_semantic_models import ReconnectConnectorOperation

            return RepairPlanDraft(
                operations=[
                    ReconnectConnectorOperation(
                        connector_id=(
                            context.target.element_ids[0] if context.target.element_ids else "missing"
                        ),
                        endpoint="source",
                        element_id="does_not_exist",
                        port_id="nope",
                    )
                ],
                rationale=f"injected failure for attempt {context.attempt}",
                source="test-double",
            )
        return self.inner.plan(context)


def _target_issue(
    document_code: str,
    validator_id: str,
    element_ids: list[str],
    issues: list[ValidationIssue],
    target_details: dict[str, Any] | None = None,
) -> ValidationIssue | None:
    """Find the finding this case bought, and bind it to the element the case broke.

    Some validators report a drawing-wide condition rather than a per-element one (the
    out-of-bounds rule is one): the issue then carries no locators, so the case's declared
    target supplies them. The declared locator is attached here, once, so nothing downstream
    has to guess which element a drawing-wide finding was about.
    """

    wanted = set(element_ids)
    declared = dict(target_details or {})
    same_code = [
        issue
        for issue in issues
        if issue.code == document_code
        and issue.validator_id == validator_id
        and not issue.is_waived
    ]
    if declared:
        # A per-element *and* per-port finding: the case says which one it broke, so the record
        # binds that exact finding instead of whichever one happened to sort first.
        pinned = [
            issue
            for issue in same_code
            if all(issue.details.get(key) == value for key, value in declared.items())
        ]
        if pinned:
            same_code = pinned
    matching = [issue for issue in same_code if not wanted or wanted & set(issue.element_ids)]
    if matching:
        chosen = sorted(matching, key=lambda item: (item.code, tuple(item.element_ids)))[0]
    else:
        locatorless = [issue for issue in same_code if not issue.element_ids]
        if not locatorless or not wanted:
            return None
        chosen = sorted(locatorless, key=lambda item: item.code)[0]
        chosen = chosen.model_copy(update={"element_ids": sorted(wanted)})
    return chosen


def run_case(
    context: BenchmarkContext,
    case: BenchmarkCase,
    *,
    planner: RepairPlanner | None = None,
    apply_enabled: bool = True,
    candidate_sha: str = "",
    base_drawing: BaseDrawing | None = None,
    operators: Mapping[str, MutationOperator] | None = None,
) -> RepairCaseRecord:
    """Run one case end to end and return its record.

    ``base_drawing`` exists for the scale track (baseline §G): the same operator catalogue has to
    run against a real imported drawing, not only against the tiny benchmark base. Roles are what
    make that possible — the operator names ``p1``/``v2`` and the track's role map resolves them to
    whatever those elements are called in the drawing being measured.

    ``operators`` exists for the coverage-extension track: a case outside the frozen 72-case
    corpus needs the *same* oracle and governance with a different defect catalogue, and the
    catalogue is the only thing it may swap. The default stays :data:`MUTATIONS`, so the frozen
    corpus cannot be run against anything but its own operators.
    """

    catalogue = MUTATIONS if operators is None else operators
    operator = catalogue[case.operator_id]
    if base_drawing is not None:
        base = base_drawing
    else:
        builder = operator.document_builder or build_base_drawing
        base = builder(context.service, context.registry)
    mutation: MutationResult = operator.apply(context.service, base, _rng(case.seed))

    document = context.service.get_document(base.document_id)
    result = run_validation(document, context.registry, context.profile, service=context.service)
    issue = _target_issue(
        mutation.target_code,
        mutation.target_validator_id,
        mutation.target_element_ids,
        result.issues,
        mutation.target_details,
    )
    common = dict(
        case_id=case.case_id,
        family=case.family,
        operator_id=case.operator_id,
        suite=case.suite,
        seed=case.seed,
        spec_fingerprint=spec_fingerprint(),
        generator_fingerprint=generator_fingerprint(),
        candidate_sha=candidate_sha,
        document_id=document.id,
        base_revision=document.revision,
        base_content_hash=result.content_hash,
        pre_validation_hash=result.result_hash,
        target_code=mutation.target_code,
        target_validator_id=mutation.target_validator_id,
        # The case contract travels with the record, so the gate can check that the run that
        # succeeded is the run the case describes (see ``evaluate_gates``).
        required_attempts=mutation.required_attempts,
    )
    if issue is None:
        return RepairCaseRecord(
            **common,
            classification="invalid_case",
            failure_code="mutation_did_not_produce_target_finding",
            reasons=[f"{case.operator_id} did not produce {mutation.target_code}"],
            scope_fingerprint="",
        )

    request = build_repair_request(
        document,
        result,
        issue,
        registry=context.registry,
        profile=context.profile,
        hop=mutation.hop,
        max_touched_existing_ids=TOUCHED_BUDGET_BY_CLASS[mutation.touched_budget_class],
        declared_by="case_spec",
        # The write policy is read from the *operator*, which is where the spec declares it: the
        # same place a reviewer reads it, and the place a new case has to state it.
        permits_creation=operator.permits_creation,
        max_created_ids=operator.max_created_ids,
        permits_deletion=operator.permits_deletion,
        max_deleted_ids=operator.max_deleted_ids,
    )
    # A case may name ids the derivation would not have reached, but naming them may not evict
    # the ids it did reach (see ``repair_scope.extend_scope``).
    request = request.model_copy(
        update={
            "scope": extend_scope(
                request.scope,
                document,
                context.registry,
                mutation.declared_scope_ids,
            )
        }
    )
    inner = planner or DeterministicRepairPlanner()
    effective: RepairPlanner = (
        FaultInjectingPlanner(inner, failures=mutation.required_attempts - 1, required_attempts=mutation.required_attempts)
        if mutation.required_attempts > 1
        else inner
    )
    orchestrator = RepairOrchestrator(
        context.service,
        context.profile,
        planner=effective,
        apply_enabled=apply_enabled,
    )
    started = time.perf_counter()
    run = orchestrator.run(request, postconditions=mutation.postconditions)
    wall_clock_ms = int((time.perf_counter() - started) * 1000)
    writes = _count_repair_writes(context.service, document.id, run)
    return _record_from_run(case, common, request, run, writes, wall_clock_ms)


def _record_from_run(
    case: BenchmarkCase,
    common: dict[str, Any],
    request,
    run: RepairRunResult,
    writes: int,
    wall_clock_ms: int,
) -> RepairCaseRecord:
    success = run.repaired and run.applied.applied and writes == 1
    if success:
        classification = "success"
    elif run.status in {"human_required", "not_repairable"}:
        classification = run.status
    else:
        classification = "failure"
    return RepairCaseRecord(
        **common,
        target_rule_id=request.target.rule_id,
        target_element_ids=list(request.target.element_ids),
        scope_fingerprint=run.protected_pre.scope_fingerprint,
        protected_pre={
            "engineering_projection": run.protected_pre.engineering_projection,
            "drawing_projection": run.protected_pre.drawing_projection,
            "rule_bundle_fingerprint": run.protected_pre.rule_bundle_fingerprint,
        },
        protected_post={
            "engineering_projection": run.applied.post_protected.engineering_projection,
            "drawing_projection": run.applied.post_protected.drawing_projection,
            "rule_bundle_fingerprint": run.applied.post_protected.rule_bundle_fingerprint,
        },
        attempts=run.attempt_count,
        attempt_plan_hashes=[attempt.planner.plan_hash for attempt in run.attempts],
        attempt_failure_codes=[attempt.failure_code or "" for attempt in run.attempts],
        attempt_shadow_validation_hashes=[
            attempt.shadow_validation_hash for attempt in run.attempts
        ],
        attempt_metrics=run.attempt_metrics,
        selected_plan_hash=run.selected_plan_hash,
        applied=run.applied.applied,
        result_revision=run.applied.result_revision,
        transaction_hash=run.applied.transaction_hash,
        audit_record_id=run.applied.audit_record_id,
        post_validation_hash=run.applied.post_validation_hash,
        undo_restored_base=run.applied.undo_restored_base,
        redo_restored_result=run.applied.redo_restored_result,
        repair_hash=run.repair_hash,
        classification=classification,
        failure_code=run.failure_code or "",
        write_count=writes,
        reasons=list(run.reasons),
        wall_clock_ms=wall_clock_ms,
        timings_ms=run.timings.model_dump(mode="json"),
        input_tokens=sum(metric.input_tokens for metric in run.attempt_metrics),
        output_tokens=sum(metric.output_tokens for metric in run.attempt_metrics),
        token_usage_estimated=bool(getattr(run.planner, "token_usage_estimated", False)),
        planner=run.planner,
    )


def _count_repair_writes(service: DocumentService, document_id: str, run: RepairRunResult) -> int:
    """Count the repair transactions the run actually committed.

    Measured from the document's own history rather than from the runner's intentions: the
    undo/redo proof steps are history entries too, so the count has to look at what kind of
    write each revision was. More than one repair write is a governance violation, which is
    why this is counted instead of asserted.
    """

    if not run.applied.applied:
        return 0
    history = service.get_history(document_id)
    return len(
        [
            entry
            for entry in history
            if entry.revision > run.applied.base_revision
            and entry.action == "transaction"
            and entry.label.startswith("Agent self-repair")
        ]
    )


def _rng(seed: int):
    import random

    return random.Random(seed)


def run_benchmark(
    context: BenchmarkContext,
    *,
    suite: str = "acceptance",
    candidate_sha: str = "",
    cases: list[BenchmarkCase] | None = None,
    planner: RepairPlanner | None = None,
    apply_enabled: bool = True,
    progress: Any = None,
    safety: bool = True,
    track: str = "deterministic",
) -> RepairBenchmarkResult:
    """Run a whole suite and return the verifiable result payload.

    The safety-negative suite runs alongside the cases because §A3 makes it a separate hard
    gate: a 100% success rate with one unsafe write is not an accepted candidate. Its cases are
    ``invalid_case``-free by construction (they stage their own drawings), and it is the only
    place where ``human_required``/``policy_violation`` are the *expected* answer.

    ``track`` selects which frozen thresholds the gates are judged against, and nothing else:
    Track M is Track D with a different planner injected (baseline §B1), so a model run and a
    deterministic run cannot disagree about what counts as a repaired drawing.
    """

    selected = cases if cases is not None else generate_suite(candidate_sha=candidate_sha, suite=suite)
    records: list[RepairCaseRecord] = []
    started = time.perf_counter()
    for index, case in enumerate(selected, start=1):
        record = run_case(
            context,
            case,
            planner=planner,
            apply_enabled=apply_enabled,
            candidate_sha=candidate_sha,
        )
        records.append(record)
        if progress is not None:
            progress(index, len(selected), record)
    elapsed = int((time.perf_counter() - started) * 1000)
    safety_total = 0
    safety_passed = 0
    if safety:
        from .repair_safety import run_safety_suite

        safety_result = run_safety_suite(context)
        safety_total = safety_result.total
        safety_passed = safety_result.passed
    effective_planner = planner or DeterministicRepairPlanner()
    describe = getattr(effective_planner, "identity", None)
    identity = describe() if callable(describe) else None
    return build_benchmark_result(
        suite=suite,
        track=track,
        planner=identity,
        spec_version=BENCHMARK_SPEC_VERSION,
        spec_fingerprint=spec_fingerprint(),
        generator_fingerprint=generator_fingerprint(),
        oracle_version=SUCCESS_ORACLE_VERSION,
        candidate_sha=candidate_sha,
        profile_id=context.profile.profile_id,
        profile_version=context.profile.profile_version,
        rule_bundle_fingerprint=context.profile.fingerprint,
        thresholds=dict(THRESHOLDS),
        created_by=f"{track}-runner",
        cases=records,
        safety_total=safety_total,
        safety_passed=safety_passed,
        timings_ms={"suite_ms": elapsed},
    )


__all__ = [
    "BenchmarkContext",
    "BenchmarkSetupError",
    "FaultInjectingPlanner",
    "run_benchmark",
    "run_case",
]
