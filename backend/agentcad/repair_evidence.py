"""The benchmark evidence contract and its independent verifier (baseline §N).

The baseline's requirement is unusual and specific: a reviewer must be able to take the
published payload and *recompute* it. That rules out a report that merely quotes numbers.
So this module publishes one machine contract (``pid-agent.repair-benchmark-result/v1``)
whose every case record carries the binding facts — spec and generator fingerprints, candidate
SHA, seed, base revision, pre/post validation hashes, per-attempt plan and shadow hashes,
audit id, undo/redo proof — and a verifier that:

1. recomputes the canonical hash of the payload;
2. recomputes every summary number from the case records;
3. checks that the denominator was not trimmed (counts must equal the case list);
4. checks that each success carries complete evidence;
5. checks the spec and generator fingerprints.

The verifier is deliberately independent of the runner: it takes the *published payload*, not
the runner's objects, so a bug in the runner cannot make a broken run verify.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .repair_models import (
    REPAIR_LEGACY_HASH_EXCLUDES,
    REPAIR_SEMANTIC_HASH_VERSION,
    RepairAttemptMetrics,
    RepairContractModel,
    RepairPlannerIdentity,
    repair_digest,
    repair_payload,
    repair_semantic_digest,
)

BENCHMARK_RESULT_SCHEMA = "pid-agent.repair-benchmark-result"
BENCHMARK_CASE_SCHEMA = "pid-agent.repair-benchmark-case"

CaseClassification = Literal[
    "success",
    "failure",
    "human_required",
    "not_repairable",
    "invalid_case",
]


class RepairCaseRecord(RepairContractModel):
    """One case's complete evidence, or the reason there is none.

    Self-describing on purpose: a case record is the unit an external reviewer reads, and a
    record whose schema has to be inferred from its file name is not evidence.
    """

    schema_name: Literal["pid-agent.repair-benchmark-case"] = Field(
        default=BENCHMARK_CASE_SCHEMA, alias="schema"
    )
    case_id: str
    family: str
    operator_id: str
    suite: str
    seed: int
    spec_fingerprint: str = ""
    generator_fingerprint: str = ""
    candidate_sha: str = ""
    document_id: str = ""
    base_revision: int = Field(default=0, ge=0)
    base_content_hash: str = ""
    pre_validation_hash: str = ""
    target_code: str = ""
    target_validator_id: str = ""
    target_rule_id: str = ""
    target_element_ids: list[str] = Field(default_factory=list)
    scope_fingerprint: str = ""
    protected_pre: dict[str, str] = Field(default_factory=dict)
    attempts: int = Field(default=0, ge=0)
    #: The attempt contract the case declares (the F6 injector makes the honest plan reachable on
    #: attempt N). Gated per case: a success that does not happen on the declared attempt is not
    #: the run the case describes, however green it looks.
    required_attempts: int = Field(default=1, ge=1)
    attempt_plan_hashes: list[str] = Field(default_factory=list)
    attempt_failure_codes: list[str] = Field(default_factory=list)
    attempt_shadow_validation_hashes: list[str] = Field(default_factory=list)
    attempt_metrics: list[RepairAttemptMetrics] = Field(default_factory=list)
    selected_plan_hash: str = ""
    applied: bool = False
    result_revision: int = Field(default=0, ge=0)
    transaction_hash: str = ""
    audit_record_id: str = ""
    post_validation_hash: str = ""
    protected_post: dict[str, str] = Field(default_factory=dict)
    undo_restored_base: bool = False
    redo_restored_result: bool = False
    repair_hash: str = ""
    classification: CaseClassification = "failure"
    failure_code: str = ""
    write_count: int = Field(default=0, ge=0)
    reasons: list[str] = Field(default_factory=list)
    wall_clock_ms: int = 0
    #: Per-phase cost of the attempt that was selected, or of the whole case when nothing was.
    #: Kept per case because "the model is slow" is only actionable if it names the phase.
    timings_ms: dict[str, int] = Field(default_factory=dict)
    #: Cumulative tokens for this case, from provider usage or the locked estimator (§F).
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    token_usage_estimated: bool = False
    planner: RepairPlannerIdentity | None = None


class RepairBenchmarkCounts(RepairContractModel):
    total: int = Field(default=0, ge=0)
    success: int = Field(default=0, ge=0)
    failure: int = Field(default=0, ge=0)
    human_required: int = Field(default=0, ge=0)
    not_repairable: int = Field(default=0, ge=0)
    invalid: int = Field(default=0, ge=0)
    governance_violations: int = Field(default=0, ge=0)


class RepairBenchmarkResult(RepairContractModel):
    schema_name: Literal["pid-agent.repair-benchmark-result"] = Field(
        default=BENCHMARK_RESULT_SCHEMA, alias="schema"
    )
    version: Literal[1] = 1
    suite: str
    #: Which track produced this payload. The two tracks share one oracle, one orchestrator and
    #: one case generator; they differ in which planner was injected and therefore in which
    #: frozen thresholds apply (§A4 / §A3).
    track: Literal["deterministic", "model"] = "deterministic"
    spec_version: str
    spec_fingerprint: str
    generator_fingerprint: str
    oracle_version: str
    #: The frozen corpus this result was drawn from, published as its own two coordinates.
    #: ``corpus_version`` names it; ``core_corpus_digest`` identifies it on any interpreter, so a
    #: reviewer on 3.11 and a run on 3.12 can agree on "same cases" from the payload alone.
    #: ``core_corpus_fingerprint`` is deliberately *not* published here: it digests the operator
    #: bytecode, so it would print a different number on every interpreter and read like part of
    #: the result's identity instead of what it is -- provenance. Both fields below are derived
    #: metadata and are excluded from both hashes (see ``REPAIR_LEGACY_HASH_EXCLUDES``).
    corpus_version: str = ""
    core_corpus_digest: str = ""
    candidate_sha: str
    profile_id: str = ""
    profile_version: str = ""
    rule_bundle_fingerprint: str = ""
    engine_version: str = ""
    created_by: str = ""
    thresholds: dict[str, float] = Field(default_factory=dict)
    cases: list[RepairCaseRecord] = Field(default_factory=list)
    counts: RepairBenchmarkCounts = Field(default_factory=RepairBenchmarkCounts)
    s_at: dict[str, float] = Field(default_factory=dict)
    family_s_at_5: dict[str, float] = Field(default_factory=dict)
    family_counts: dict[str, int] = Field(default_factory=dict)
    failure_taxonomy: dict[str, int] = Field(default_factory=dict)
    gates: dict[str, bool] = Field(default_factory=dict)
    gate_failures: list[str] = Field(default_factory=list)
    safety_total: int = Field(default=0, ge=0)
    safety_passed: int = Field(default=0, ge=0)
    timings_ms: dict[str, int] = Field(default_factory=dict)
    #: Cost summary, recomputed by the verifier from the case records rather than trusted.
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    token_usage_estimated: bool = False
    latency_ms: dict[str, int] = Field(default_factory=dict)
    #: Who planned, for the model track (baseline §A4). Empty on the deterministic track.
    planner: RepairPlannerIdentity | None = None
    #: The original result hash, kept exactly as it was: it binds the whole payload the way pydantic's
    #: top-level ``exclude`` allows, so on two runs of one candidate it moves with the wall clock.
    #: Published v2 evidence is not rewritten, so this stays as the legacy field.
    benchmark_result_hash: str = ""
    #: The result identity that a comparison may use: volatile facts dropped at every depth by
    #: :func:`~agentcad.repair_models.repair_semantic_digest`, so the same candidate, corpus and
    #: result hash to the same value on any machine and any run.
    benchmark_semantic_hash: str = ""
    #: Which semantic hash contract produced :attr:`benchmark_semantic_hash`.
    semantic_hash_version: str = ""

    def summary_payload(self) -> dict[str, Any]:
        """Everything except the per-case records, which a summary does not need."""

        return repair_payload(
            self,
            exclude=frozenset({"cases", "benchmark_result_hash", "benchmark_semantic_hash"}),
        )


class VerificationFinding(RepairContractModel):
    code: str
    detail: str = ""


class VerificationReport(RepairContractModel):
    ok: bool = False
    findings: list[VerificationFinding] = Field(default_factory=list)
    recomputed_result_hash: str = ""
    recomputed_semantic_hash: str = ""
    recomputed_counts: RepairBenchmarkCounts = Field(default_factory=RepairBenchmarkCounts)
    recomputed_s_at: dict[str, float] = Field(default_factory=dict)
    recomputed_family_s_at_5: dict[str, float] = Field(default_factory=dict)
    recomputed_taxonomy: dict[str, int] = Field(default_factory=dict)

    def codes(self) -> list[str]:
        return [finding.code for finding in self.findings]


def case_is_success(case: RepairCaseRecord) -> bool:
    """The published definition of a per-case success, applied to the *record*.

    A success that cannot be recomputed from its own evidence is not a success. This is what
    stops a runner bug from inflating the number: the verifier re-derives every count from
    these fields rather than trusting ``classification``.
    """

    return bool(
        case.applied
        and case.repair_hash
        and case.post_validation_hash
        and case.audit_record_id
        and case.selected_plan_hash
        and case.undo_restored_base
        and case.redo_restored_result
        and case.protected_pre
        and case.protected_post
        and case.protected_pre.get("engineering_projection")
        == case.protected_post.get("engineering_projection")
        and case.protected_pre.get("drawing_projection") == case.protected_post.get("drawing_projection")
        and not case.failure_code
    )


def classify_case(case: RepairCaseRecord) -> CaseClassification:
    if case.classification == "invalid_case":
        return "invalid_case"
    if case_is_success(case):
        return "success"
    if case.classification in {"human_required", "not_repairable"}:
        return case.classification  # type: ignore[return-value]
    return "failure"


def summarise(cases: list[RepairCaseRecord]) -> tuple[RepairBenchmarkCounts, dict[str, float], dict[str, float], dict[str, int], dict[str, int]]:
    """Recompute counts, S@1..S@5, per-family S@5 and the failure taxonomy from the records."""

    buckets = {
        "success": 0,
        "failure": 0,
        "human_required": 0,
        "not_repairable": 0,
        "invalid_case": 0,
    }
    taxonomy: dict[str, int] = {}
    family_total: dict[str, int] = {}
    viable = 0
    per_attempt: dict[int, int] = {attempt: 0 for attempt in range(1, 6)}
    for case in cases:
        classification = classify_case(case)
        buckets[classification] += 1
        if classification != "invalid_case":
            viable += 1
            family_total[case.family] = family_total.get(case.family, 0) + 1
        if classification == "success":
            attempt = max(1, min(5, case.attempts))
            for level in range(attempt, 6):
                per_attempt[level] += 1
        elif classification != "invalid_case":
            code = case.failure_code or classification
            taxonomy[code] = taxonomy.get(code, 0) + 1
    counts = RepairBenchmarkCounts(
        total=len(cases),
        success=buckets["success"],
        failure=buckets["failure"],
        human_required=buckets["human_required"],
        not_repairable=buckets["not_repairable"],
        invalid=buckets["invalid_case"],
        governance_violations=sum(1 for case in cases if case.write_count > 1),
    )
    denominator = max(viable, 1)
    s_at = {f"S@{level}": round(per_attempt[level] / denominator, 4) for level in range(1, 6)}
    family_success: dict[str, int] = {}
    for case in cases:
        if classify_case(case) == "success":
            family_success[case.family] = family_success.get(case.family, 0) + 1
    family_s_at_5 = {
        family: round(family_success.get(family, 0) / max(total, 1), 4)
        for family, total in sorted(family_total.items())
    }
    return counts, s_at, family_s_at_5, taxonomy, family_total


def token_totals(cases: list[RepairCaseRecord]) -> tuple[int, int]:
    """Cumulative tokens across cases, from the per-case sums the runner recorded."""

    return (
        sum(case.input_tokens for case in cases),
        sum(case.output_tokens for case in cases),
    )


def latency_percentiles(cases: list[RepairCaseRecord]) -> dict[str, int]:
    """p50/p95/max wall clock across the cases, and the same for each named phase.

    Nearest-rank percentiles on purpose: with 24 or 72 samples an interpolated p95 is a number
    nobody can reproduce by hand from the records.
    """

    series: dict[str, list[int]] = {"wall_clock": [case.wall_clock_ms for case in cases]}
    for case in cases:
        for phase, value in case.timings_ms.items():
            series.setdefault(phase, []).append(int(value))
    summary: dict[str, int] = {}
    for name, values in sorted(series.items()):
        samples = sorted(int(value) for value in values)
        if not samples:
            continue
        summary[f"{name}_p50"] = samples[(len(samples) - 1) * 50 // 100]
        summary[f"{name}_p95"] = samples[(len(samples) - 1) * 95 // 100]
        summary[f"{name}_max"] = samples[-1]
    return summary


def evaluate_gates(
    result: RepairBenchmarkResult, *, safety_passed: int | None = None
) -> tuple[dict[str, bool], list[str]]:
    """Apply the frozen thresholds from the benchmark spec to a computed summary."""

    thresholds = result.thresholds
    gates: dict[str, bool] = {}
    failures: list[str] = []
    denominators = max(
        result.counts.success
        + result.counts.failure
        + result.counts.human_required
        + result.counts.not_repairable,
        1,
    )
    # The model track gets the §A4 thresholds instead of the §A3 ones.
    #
    # Neither track gates on the global S@1. The suite deliberately contains cases whose contract
    # requires a retry (the F6 fault injector fails the first N-1 attempts), so a case built to
    # take two attempts fails S@1 *by construction*: a bar there measures the fixture, not the
    # planner. S@1..S@4 are published as observations. What is enforced instead is the contract
    # itself — a case that declares N > 1 required attempts must first succeed on attempt N, which
    # also proves the injection actually happened — plus F6's own S@5, because convergence under
    # injected failure is the property F6 exists to prove. Cases with no retry contract make no
    # attempt claim, so their attempt count stays an observation (it is what S@1..S@4 publish).
    if result.track == "model":
        s5_key, family_key = "model_s5_overall", "model_family_min"
    else:
        s5_key, family_key = "s5_overall", "s5_family_min"
    gates["s5_overall"] = result.s_at.get("S@5", 0.0) >= thresholds.get(s5_key, 1.0)
    if not gates["s5_overall"]:
        failures.append("s5_overall_below_threshold")
    family_ok = bool(result.family_s_at_5) and all(
        value >= thresholds.get(family_key, 1.0) for value in result.family_s_at_5.values()
    )
    gates["s5_family_min"] = family_ok
    if not family_ok:
        failures.append("s5_family_below_threshold")
    contract_violations = [
        case.case_id
        for case in result.cases
        if case.classification == "success"
        and case.required_attempts > 1
        and case.attempts != case.required_attempts
    ]
    gates["attempt_contract"] = not contract_violations
    if contract_violations:
        failures.extend(
            f"attempt_contract_mismatch:{case_id}" for case_id in contract_violations
        )
    if "F6" in result.family_s_at_5:
        f6_ok = result.family_s_at_5["F6"] >= thresholds.get("f6_s5_overall", 1.0)
        gates["f6_s5_overall"] = f6_ok
        if not f6_ok:
            failures.append("f6_s5_below_threshold")
    if result.track == "model":
        # §F makes the per-case token ceilings part of the model track's contract, so a case that
        # overspent is a failure even if its drawing ended up correct — the ceiling is what keeps
        # "the model fixed it" from meaning "at any cost".
        over_budget = [
            case.case_id
            for case in result.cases
            if case.failure_code == "token_budget_exceeded"
        ]
        gates["token_budget"] = not over_budget
        if over_budget:
            failures.append("token_budget_exceeded")
    gates["governance_violations_zero"] = result.counts.governance_violations == 0
    if not gates["governance_violations_zero"]:
        failures.append("governance_violation_present")
    gates["no_invalid_cases"] = result.counts.invalid == 0
    if not gates["no_invalid_cases"]:
        failures.append("invalid_benchmark_case")
    if safety_passed is not None:
        gates["safety_suite"] = safety_passed >= result.safety_total > 0
        if not gates["safety_suite"]:
            failures.append("safety_suite_incomplete")
    gates["denominator_intact"] = result.counts.total == len(result.cases)
    if not gates["denominator_intact"]:
        failures.append("denominator_trimmed")
    _ = denominators
    return gates, failures


def build_benchmark_result(**kwargs: Any) -> RepairBenchmarkResult:
    """Assemble a result, computing every derived number from the case records."""

    cases: list[RepairCaseRecord] = list(kwargs.pop("cases", []))
    counts, s_at, family_s_at_5, taxonomy, family_counts = summarise(cases)
    # Cost is derived here like every other published number, from the records, so a caller
    # cannot publish a cheap-looking summary over expensive cases.
    input_tokens, output_tokens = token_totals(cases)
    kwargs.update(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        token_usage_estimated=any(case.token_usage_estimated for case in cases),
        latency_ms=latency_percentiles(cases),
    )
    result = RepairBenchmarkResult(cases=cases, counts=counts, s_at=s_at, family_s_at_5=family_s_at_5, failure_taxonomy=taxonomy, family_counts=family_counts, **kwargs)
    gates, failures = evaluate_gates(result, safety_passed=result.safety_passed)
    result = result.model_copy(update={"gates": gates, "gate_failures": failures})
    # Two hashes, on purpose. The legacy one is what the published v2 evidence carries and is only
    # meaningful for the run that produced it; the semantic one is the value two runs (or two
    # machines) can be compared with. The version is stamped before hashing so the published
    # payload says which contract produced it.
    stamped = result.model_copy(update={"semantic_hash_version": REPAIR_SEMANTIC_HASH_VERSION})
    return stamped.model_copy(
        update={
            "benchmark_result_hash": repair_digest(
                stamped, exclude=REPAIR_LEGACY_HASH_EXCLUDES
            ),
            "benchmark_semantic_hash": repair_semantic_digest(stamped),
        }
    )


def verify_benchmark_result(payload: dict[str, Any] | RepairBenchmarkResult) -> VerificationReport:
    """Recompute everything a reviewer can recompute, from the payload alone."""

    model = (
        payload
        if isinstance(payload, RepairBenchmarkResult)
        else RepairBenchmarkResult.model_validate(payload)
    )
    findings: list[VerificationFinding] = []
    counts, s_at, family_s_at_5, taxonomy, _family_counts = summarise(model.cases)

    if counts.total != model.counts.total:
        findings.append(
            VerificationFinding(
                code="denominator_trimmed",
                detail=f"payload says {model.counts.total}, records say {counts.total}",
            )
        )
    if counts.success != model.counts.success:
        findings.append(
            VerificationFinding(
                code="success_count_mismatch",
                detail=f"payload says {model.counts.success}, records say {counts.success}",
            )
        )
    for level in range(1, 6):
        key = f"S@{level}"
        if abs(model.s_at.get(key, -1) - s_at[key]) > 1e-9:
            findings.append(
                VerificationFinding(
                    code="summary_mismatch",
                    detail=f"{key}: payload {model.s_at.get(key)}, records {s_at[key]}",
                )
            )
    for family, value in family_s_at_5.items():
        if abs(model.family_s_at_5.get(family, -1) - value) > 1e-9:
            findings.append(
                VerificationFinding(
                    code="summary_mismatch",
                    detail=f"{family} S@5: payload {model.family_s_at_5.get(family)}, records {value}",
                )
            )
    if model.failure_taxonomy != taxonomy:
        findings.append(
            VerificationFinding(
                code="taxonomy_mismatch",
                detail=f"payload {model.failure_taxonomy} != records {taxonomy}",
            )
        )

    for case in model.cases:
        if classify_case(case) == "success":
            missing = [
                name
                for name, value in (
                    ("repair_hash", case.repair_hash),
                    ("post_validation_hash", case.post_validation_hash),
                    ("audit_record_id", case.audit_record_id),
                    ("selected_plan_hash", case.selected_plan_hash),
                    ("transaction_hash", case.transaction_hash),
                    ("candidate_sha", case.candidate_sha),
                    ("spec_fingerprint", case.spec_fingerprint),
                    ("generator_fingerprint", case.generator_fingerprint),
                )
                if not value
            ]
            if missing:
                findings.append(
                    VerificationFinding(
                        code="success_evidence_incomplete",
                        detail=f"{case.case_id}: missing {missing}",
                    )
                )
            if not case.undo_restored_base or not case.redo_restored_result:
                findings.append(
                    VerificationFinding(
                        code="success_evidence_incomplete",
                        detail=f"{case.case_id}: undo/redo proof missing",
                    )
                )
        if case.spec_fingerprint and case.spec_fingerprint != model.spec_fingerprint:
            findings.append(
                VerificationFinding(
                    code="spec_fingerprint_mismatch",
                    detail=f"{case.case_id}: {case.spec_fingerprint} != {model.spec_fingerprint}",
                )
            )
        if case.generator_fingerprint and case.generator_fingerprint != model.generator_fingerprint:
            findings.append(
                VerificationFinding(
                    code="generator_fingerprint_mismatch",
                    detail=f"{case.case_id}: {case.generator_fingerprint} != {model.generator_fingerprint}",
                )
            )
        if case.write_count > 1:
            findings.append(
                VerificationFinding(
                    code="governance_violation",
                    detail=f"{case.case_id}: {case.write_count} repair writes",
                )
            )
        if (
            case.classification == "success"
            and case.required_attempts > 1
            and case.attempts != case.required_attempts
        ):
            findings.append(
                VerificationFinding(
                    code="attempt_contract_mismatch",
                    detail=(
                        f"{case.case_id}: succeeded on attempt {case.attempts}, "
                        f"contract says {case.required_attempts}"
                    ),
                )
            )
        if case.classification != classify_case(case):
            findings.append(
                VerificationFinding(
                    code="classification_mismatch",
                    detail=f"{case.case_id}: payload {case.classification}, recomputed {classify_case(case)}",
                )
            )

    # The gates are part of the payload a reviewer reads, so they are recomputed here too: a
    # published verdict that does not follow from the published numbers is not a verdict.
    recomputed_gates, recomputed_failures = evaluate_gates(
        model, safety_passed=model.safety_passed
    )
    if recomputed_gates != model.gates:
        findings.append(
            VerificationFinding(
                code="gates_mismatch",
                detail=f"payload {model.gates} != recomputed {recomputed_gates}",
            )
        )
    if sorted(recomputed_failures) != sorted(model.gate_failures):
        findings.append(
            VerificationFinding(
                code="gate_failures_mismatch",
                detail=f"payload {model.gate_failures} != recomputed {recomputed_failures}",
            )
        )
    recomputed_tokens = token_totals(model.cases)
    if (recomputed_tokens[0], recomputed_tokens[1]) != (model.input_tokens, model.output_tokens):
        findings.append(
            VerificationFinding(
                code="cost_summary_mismatch",
                detail=(
                    f"payload {model.input_tokens}/{model.output_tokens} tokens != records "
                    f"{recomputed_tokens[0]}/{recomputed_tokens[1]}"
                ),
            )
        )
    recomputed_latency = latency_percentiles(model.cases)
    if recomputed_latency != model.latency_ms:
        findings.append(
            VerificationFinding(
                code="latency_summary_mismatch",
                detail=f"payload {model.latency_ms} != records {recomputed_latency}",
            )
        )

    recomputed_hash = repair_digest(
        model.model_copy(update={"benchmark_result_hash": ""}),
        exclude=REPAIR_LEGACY_HASH_EXCLUDES,
    )
    if model.benchmark_result_hash and recomputed_hash != model.benchmark_result_hash:
        findings.append(
            VerificationFinding(
                code="result_hash_mismatch",
                detail="the payload does not hash to its own benchmark_result_hash",
            )
        )

    if model.semantic_hash_version and model.semantic_hash_version != REPAIR_SEMANTIC_HASH_VERSION:
        findings.append(
            VerificationFinding(
                code="semantic_hash_version_unknown",
                detail=(
                    f"payload declares semantic hash contract {model.semantic_hash_version}, this "
                    f"build computes {REPAIR_SEMANTIC_HASH_VERSION}"
                ),
            )
        )
    elif model.semantic_hash_version and not model.benchmark_semantic_hash:
        findings.append(
            VerificationFinding(
                code="semantic_hash_missing",
                detail="the payload declares a semantic hash contract but publishes no hash",
            )
        )

    recomputed_semantic_hash = repair_semantic_digest(model)
    if model.benchmark_semantic_hash and recomputed_semantic_hash != model.benchmark_semantic_hash:
        findings.append(
            VerificationFinding(
                code="semantic_hash_mismatch",
                detail="the payload does not hash to its own benchmark_semantic_hash",
            )
        )

    return VerificationReport(
        ok=not findings,
        findings=findings,
        recomputed_result_hash=recomputed_hash,
        recomputed_semantic_hash=recomputed_semantic_hash,
        recomputed_counts=counts,
        recomputed_s_at=s_at,
        recomputed_family_s_at_5=family_s_at_5,
        recomputed_taxonomy=taxonomy,
    )


__all__ = [
    "BENCHMARK_CASE_SCHEMA",
    "BENCHMARK_RESULT_SCHEMA",
    "CaseClassification",
    "RepairBenchmarkCounts",
    "RepairBenchmarkResult",
    "RepairCaseRecord",
    "VerificationFinding",
    "VerificationReport",
    "build_benchmark_result",
    "case_is_success",
    "classify_case",
    "evaluate_gates",
    "latency_percentiles",
    "summarise",
    "token_totals",
    "verify_benchmark_result",
]
