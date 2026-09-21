"""The primary real-model qualification run (baseline §A4, §M5-4).

CI may not depend on an external model provider, which would leave the release with no evidence
that the agent half of this project is an agent. So qualification is a *separate, local, required*
piece of release evidence:

* 24 cases, the same success oracle, the same orchestrator, the same five-attempt ceiling;
* S@5 ≥ 80% overall and at least 2 of 4 in **each** family;
* a 100% safe safety smoke (no write where the answer is a refusal);
* the exact provider/model/parameters/prompt/schema/timeout/candidate identity, without secrets.

Without a usable credential the honest status is ``awaiting_real_model_qualification``: the
candidate is not rejected, and it is emphatically not accepted. That distinction is the whole
reason this module exists rather than a footnote in a report.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from pydantic import Field

from .llm import ProviderNotConfiguredError
from .models import ProviderConfig
from .repair_evidence import (
    RepairBenchmarkResult,
    latency_percentiles,
    token_totals,
    verify_benchmark_result,
)
from .repair_models import RepairContractModel, RepairPlannerIdentity

QUALIFICATION_SCHEMA = "pid-agent.repair-model-qualification"

#: §A4 fixes the primary qualification at 24 cases, i.e. the development suite's four per family.
QUALIFICATION_CASES_PER_FAMILY = 4
QUALIFICATION_TOTAL_CASES = 24

QualificationStatus = Literal[
    "qualified",
    "not_qualified",
    "awaiting_real_model_qualification",
    "invalid_batch",
]


class QualificationReport(RepairContractModel):
    schema_name: Literal["pid-agent.repair-model-qualification"] = Field(
        default=QUALIFICATION_SCHEMA, alias="schema"
    )
    version: Literal[1] = 1
    status: QualificationStatus = "awaiting_real_model_qualification"
    candidate_sha: str = ""
    suite: str = "dev"
    cases_per_family: int = QUALIFICATION_CASES_PER_FAMILY
    planner: RepairPlannerIdentity | None = None
    thresholds: dict[str, float] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    s_at: dict[str, float] = Field(default_factory=dict)
    family_s_at_5: dict[str, float] = Field(default_factory=dict)
    failure_taxonomy: dict[str, int] = Field(default_factory=dict)
    safety_passed: int = 0
    safety_total: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    token_usage_estimated: bool = False
    latency_ms: dict[str, int] = Field(default_factory=dict)
    gates: dict[str, bool] = Field(default_factory=dict)
    gate_failures: list[str] = Field(default_factory=list)
    benchmark_result_hash: str = ""
    evidence_verified: bool = False
    verification_findings: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


def configured_provider() -> ProviderConfig | None:
    """The provider the environment describes, or ``None``. Never raises, never dials out."""

    base_url = os.getenv("PID_AGENT_LLM_BASE_URL") or os.getenv("AGENTCAD_LLM_BASE_URL")
    model = os.getenv("PID_AGENT_LLM_MODEL") or os.getenv("AGENTCAD_LLM_MODEL")
    if not base_url or not model:
        return None
    return ProviderConfig(base_url=base_url, model=model)


def awaiting_report(
    *, candidate_sha: str, reason: str, planner: RepairPlannerIdentity | None = None
) -> QualificationReport:
    return QualificationReport(
        status="awaiting_real_model_qualification",
        candidate_sha=candidate_sha,
        planner=planner,
        reasons=[reason],
    )


def _planner_identity(planner: Any) -> RepairPlannerIdentity | None:
    describe = getattr(planner, "identity", None)
    return describe() if callable(describe) else None


def run_qualification(
    context: Any,
    *,
    planner: Any,
    candidate_sha: str,
    suite: str = "dev",
    safety: bool = True,
    progress: Any = None,
) -> tuple[QualificationReport, RepairBenchmarkResult]:
    """Run the model track and turn it into a verdict a release gate can read.

    The benchmark payload is returned alongside the report because it is the evidence: the
    verdict quotes it, and a reviewer recomputes it with the same independent verifier CI uses.
    """

    from .repair_benchmark_runner import run_benchmark

    result = run_benchmark(
        context,
        suite=suite,
        candidate_sha=candidate_sha,
        planner=planner,
        safety=safety,
        track="model",
        progress=progress,
    )
    verification = verify_benchmark_result(result)
    input_tokens, output_tokens = token_totals(result.cases)
    reasons: list[str] = []

    unavailable = [
        case for case in result.cases if case.failure_code == "planner_unavailable"
    ]
    if result.cases and len(unavailable) == len(result.cases):
        status: QualificationStatus = "invalid_batch"
        reasons.append(
            "every case failed on provider availability, so this batch describes the provider "
            "rather than the candidate (baseline §F)"
        )
    elif (
        result.counts.total != QUALIFICATION_TOTAL_CASES
        or result.counts.invalid != 0
        or not verification.ok
    ):
        status = "invalid_batch"
        if result.counts.invalid:
            reasons.append(f"{result.counts.invalid} invalid case(s) void the whole run")
        if not verification.ok:
            reasons.append("the published evidence did not recompute")
        if result.counts.total != QUALIFICATION_TOTAL_CASES:
            reasons.append(
                f"the run produced {result.counts.total} cases, expected {QUALIFICATION_TOTAL_CASES}"
            )
    elif all(result.gates.values()) and not result.gate_failures:
        status = "qualified"
    else:
        status = "not_qualified"
        reasons.extend(result.gate_failures)

    return (
        QualificationReport(
            status=status,
            candidate_sha=candidate_sha,
            suite=suite,
            planner=result.planner or _planner_identity(planner),
            thresholds=dict(result.thresholds),
            counts=result.counts.model_dump(mode="json"),
            s_at=dict(result.s_at),
            family_s_at_5=dict(result.family_s_at_5),
            failure_taxonomy=dict(result.failure_taxonomy),
            safety_passed=result.safety_passed,
            safety_total=result.safety_total,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            token_usage_estimated=result.token_usage_estimated,
            latency_ms=latency_percentiles(result.cases),
            gates=dict(result.gates),
            gate_failures=list(result.gate_failures),
            benchmark_result_hash=result.benchmark_result_hash,
            evidence_verified=verification.ok,
            verification_findings=[
                f"{finding.code}: {finding.detail}" for finding in verification.findings
            ],
            reasons=reasons,
        ),
        result,
    )


def build_model_planner(provider: ProviderConfig | None = None) -> Any:
    """The Track M planner, or a named refusal when the project has no credential."""

    from .repair_model_planner import ModelRepairPlanner

    try:
        return ModelRepairPlanner(provider=provider)
    except ProviderNotConfiguredError as exc:
        raise ProviderNotConfiguredError(str(exc)) from exc


__all__ = [
    "QUALIFICATION_CASES_PER_FAMILY",
    "QUALIFICATION_SCHEMA",
    "QUALIFICATION_TOTAL_CASES",
    "QualificationReport",
    "QualificationStatus",
    "awaiting_report",
    "build_model_planner",
    "configured_provider",
    "run_qualification",
]
