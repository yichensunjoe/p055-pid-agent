"""Track M tests: the same repair contract, planned by a (stubbed) real model.

A real provider cannot be a test dependency, so these tests stub the transport and check the
things that would be wrong even with a perfect model: that the question contains no answer key,
that a correct model answer is accepted by the *production* orchestrator and oracle, that a
wrong or malformed answer is recorded as evidence rather than crashing the run, that tokens are
accounted for and capped, and that a missing credential reads as neither a pass nor a failure.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import httpx
import pytest

from agentcad.llm import ProviderNotConfiguredError
from agentcad.models import ProviderConfig
from agentcad.repair_benchmark import MUTATIONS, build_base_drawing
from agentcad.repair_benchmark_runner import BenchmarkContext
from agentcad.repair_evidence import verify_benchmark_result
from agentcad.repair_model_planner import (
    ModelRepairPlanner,
    build_model_prompt,
    prompt_fingerprint,
    schema_fingerprint,
)
from agentcad.repair_orchestrator import RepairOrchestrator, build_repair_request
from agentcad.repair_planner import build_repair_context
from agentcad.repair_qualification import (
    configured_provider,
    run_qualification,
)
from agentcad.repair_scope import TOUCHED_BUDGET_BY_CLASS
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry
from agentcad.validation_engine import run_validation
from agentcad.validation_profile import built_in_profile, resolve_profile

PROVIDER = ProviderConfig(base_url="http://127.0.0.1:9999/v1", model="stub-model")


class _StubResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = json.dumps(payload).encode("utf-8")

    def json(self) -> dict:
        return self._payload


class _StubClient:
    def __init__(self, responses: list) -> None:
        self._responses = responses
        self.requests: list[dict] = []

    def __enter__(self) -> _StubClient:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def post(self, url: str, **kwargs: object) -> _StubResponse:
        self.requests.append({"url": url, **kwargs})
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def _completion(payload: str, *, usage: dict | None = None) -> _StubResponse:
    body: dict = {"choices": [{"message": {"content": payload}}]}
    if usage is not None:
        body["usage"] = usage
    return _StubResponse(body)


def _planner(responses: list) -> ModelRepairPlanner:
    return ModelRepairPlanner(provider=PROVIDER, client_factory=lambda **_: _StubClient(responses))


def _context(tmp_path: Path) -> BenchmarkContext:
    service = DocumentService(SQLiteDocumentStore(tmp_path / "m.db"), SymbolRegistry())
    return BenchmarkContext(
        service=service,
        profile=resolve_profile(built_in_profile()),
        registry=service.symbols,
    )


def _request_for(context: BenchmarkContext, operator_id: str):
    operator = MUTATIONS[operator_id]
    drawing = operator.document_builder or build_base_drawing
    base = drawing(context.service, context.registry)
    mutation = operator.apply(context.service, base, random.Random(7))
    document = context.service.get_document(base.document_id)
    result = run_validation(document, context.registry, context.profile, service=context.service)
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
        registry=context.registry,
        profile=context.profile,
        hop=mutation.hop,
        max_touched_existing_ids=TOUCHED_BUDGET_BY_CLASS[mutation.touched_budget_class],
        declared_by="case_spec",
        permits_creation=operator.permits_creation,
        max_created_ids=operator.max_created_ids,
        permits_deletion=operator.permits_deletion,
        max_deleted_ids=operator.max_deleted_ids,
    )
    return request, document, result


def _repair_context(context: BenchmarkContext, operator_id: str = "f1_line_medium_missing"):
    request, document, result = _request_for(context, operator_id)
    issue = next(
        issue for issue in result.issues if issue.code == request.target.code
    )
    return build_repair_context(
        request, document, context.registry, [issue], class_findings=list(result.issues)
    )


# -- the question ------------------------------------------------------------ #


def test_the_model_prompt_carries_no_answer_key(tmp_path: Path):
    """The context is the drawing plus the finding. It may not contain the mutation or a fix."""

    context = _context(tmp_path)
    repair_context = _repair_context(context)
    prompt = build_model_prompt(repair_context)
    payload = json.dumps(repair_context.payload, sort_keys=True)

    assert "LINE_MEDIUM_MISSING" in prompt
    assert "scope" in prompt
    for forbidden in ("mutation", "postconditions", "expected_operations", "answer", "oracle"):
        assert forbidden not in payload
    # Only in-scope elements are described, so the model cannot read the rest of the drawing.
    allowed = set(repair_context.request.scope.allowed_element_ids)
    described = {item["id"] for item in repair_context.payload["scope_elements"]}
    assert described <= allowed
    assert "note1" not in described


def test_the_planner_identity_names_the_model_without_leaking_secrets():
    planner = _planner([_completion("{}")])
    identity = planner.identity()

    assert identity.provider_class == "openai-compatible"
    assert identity.base_url_class == "loopback"
    assert identity.model == "stub-model"
    assert identity.prompt_fingerprint == prompt_fingerprint()
    assert identity.schema_fingerprint == schema_fingerprint()
    assert identity.max_output_tokens == planner.identity().max_output_tokens
    assert "9999" not in identity.model_dump_json()
    assert "api" not in identity.model_dump_json().lower() or "openai-compatible" in identity.model_dump_json()


# -- the answer -------------------------------------------------------------- #


def test_a_correct_model_answer_goes_through_the_production_path(tmp_path: Path):
    """A stub that repairs the finding is accepted by the same orchestrator CI uses."""

    context = _context(tmp_path)
    request, document, result = _request_for(context, "f1_line_medium_missing")
    planner = _planner(
        [
            _completion(
                json.dumps(
                    {
                        "operations": [
                            {
                                "op": "update_element",
                                "element_id": "p1",
                                "patch": {"medium": "process"},
                            }
                        ],
                        "rationale": "restore the missing line medium",
                    }
                ),
                usage={"prompt_tokens": 1200, "completion_tokens": 90},
            )
        ]
    )
    run = RepairOrchestrator(context.service, context.profile, planner=planner).run(request)

    assert run.repaired
    assert run.applied.applied
    assert run.attempt_metrics[0].input_tokens == 1200
    assert run.attempt_metrics[0].output_tokens == 90
    assert run.planner.model == "stub-model"
    after = context.service.get_document(document.id)
    assert after.revision == run.applied.result_revision
    assert after.revision > document.revision


def test_a_declined_repair_is_a_recorded_refusal_with_no_write(tmp_path: Path):
    context = _context(tmp_path)
    request, document, _ = _request_for(context, "f1_line_medium_missing")
    planner = _planner(
        [
            _completion(
                json.dumps({"decline": "human_required", "reason": "the intent is ambiguous"})
            )
        ]
    )
    run = RepairOrchestrator(context.service, context.profile, planner=planner).run(request)

    assert run.status == "human_required"
    assert not run.applied.applied
    assert context.service.get_document(document.id).revision == document.revision


def test_a_plan_outside_the_scope_is_refused_by_the_oracle(tmp_path: Path):
    """The prompt says \"only these ids\"; the oracle is what makes it true."""

    context = _context(tmp_path)
    request, document, _ = _request_for(context, "f1_line_medium_missing")
    planner = _planner(
        [
            _completion(
                json.dumps(
                    {
                        "operations": [
                            {
                                "op": "update_element",
                                "element_id": "v3",
                                "patch": {"label": "HV-999"},
                            }
                        ]
                    }
                )
            )
        ]
    )
    run = RepairOrchestrator(context.service, context.profile, planner=planner).run(request)

    assert not run.repaired
    assert not run.applied.applied
    assert context.service.get_document(document.id).revision == document.revision
    assert any(
        attempt.failure_code in {"locality_violation", "target_not_resolved"}
        for attempt in run.attempts
    )


def test_a_malformed_answer_is_replanned_then_recorded(tmp_path: Path):
    context = _context(tmp_path)
    request, document, _ = _request_for(context, "f1_line_medium_missing")
    planner = _planner([_completion("not json at all")])
    run = RepairOrchestrator(context.service, context.profile, planner=planner).run(request)

    assert run.status == "failed"
    assert run.attempt_count == request.budgets.max_attempts
    assert all(attempt.failure_code == "malformed_plan" for attempt in run.attempts)
    assert context.service.get_document(document.id).revision == document.revision


def test_a_provider_failure_is_a_case_failure_not_a_crash(tmp_path: Path):
    context = _context(tmp_path)
    request, document, _ = _request_for(context, "f1_line_medium_missing")
    planner = _planner([httpx.ConnectError("connection refused")])
    run = RepairOrchestrator(context.service, context.profile, planner=planner).run(request)

    assert run.status == "failed"
    assert run.failure_code == "planner_unavailable"
    assert run.attempt_count == 1
    assert context.service.get_document(document.id).revision == document.revision


def test_the_token_budget_ends_the_case(tmp_path: Path):
    """§F caps the whole case, and an overrun is a failure with a named reason."""

    context = _context(tmp_path)
    request, document, _ = _request_for(context, "f1_line_medium_missing")
    planner = _planner(
        [
            _completion(
                json.dumps(
                    {
                        "operations": [
                            {"op": "update_element", "element_id": "p1", "patch": {"medium": "x"}}
                        ]
                    }
                ),
                usage={"prompt_tokens": 70_000, "completion_tokens": 10},
            )
        ]
    )
    run = RepairOrchestrator(context.service, context.profile, planner=planner).run(request)

    assert run.failure_code == "token_budget_exceeded"
    assert run.attempt_count == 1
    assert not run.applied.applied
    assert context.service.get_document(document.id).revision == document.revision


def test_missing_usage_is_reported_as_estimated(tmp_path: Path):
    """§F: a provider that reports no usage gets the estimator, and says so."""

    context = _context(tmp_path)
    request, _, _ = _request_for(context, "f1_line_medium_missing")
    planner = _planner([_completion("{\n  \"operations\": [\n    {\"op\": \"nope\"}\n  ]\n}")])
    run = RepairOrchestrator(context.service, context.profile, planner=planner).run(request)

    assert planner.token_usage_estimated is True
    assert all(attempt.failure_code == "malformed_plan" for attempt in run.attempts)
    assert run.attempt_metrics[0].input_tokens > 0


# -- qualification ----------------------------------------------------------- #


def test_qualification_reports_awaiting_without_a_credential(tmp_path: Path, monkeypatch):
    for name in (
        "PID_AGENT_LLM_BASE_URL",
        "PID_AGENT_LLM_MODEL",
        "AGENTCAD_LLM_BASE_URL",
        "AGENTCAD_LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    assert configured_provider() is None
    with pytest.raises(ProviderNotConfiguredError):
        ModelRepairPlanner()


def test_the_model_track_is_judged_by_the_model_thresholds():
    """S@5 = 5/6 clears §A4 (0.80) and would fail §A3 (0.90): the track picks the bar."""

    from agentcad.repair_benchmark import THRESHOLDS
    from agentcad.repair_evidence import RepairCaseRecord, build_benchmark_result

    def case(index: int, success: bool) -> dict:
        return {
            "case_id": f"dev:F1:{index:02d}",
            "family": "F1",
            "operator_id": "f1_line_medium_missing",
            "suite": "dev",
            "seed": index,
            "candidate_sha": "sha",
            "spec_fingerprint": "f",
            "generator_fingerprint": "g",
            "classification": "success" if success else "failure",
            "failure_code": "" if success else "budget_exhausted",
            "applied": success,
            "repair_hash": "r" if success else "",
            "post_validation_hash": "p" if success else "",
            "audit_record_id": "a" if success else "",
            "selected_plan_hash": "s" if success else "",
            "transaction_hash": "t" if success else "",
            "undo_restored_base": success,
            "redo_restored_result": success,
            "protected_pre": {"engineering_projection": "e", "drawing_projection": "d"},
            "protected_post": {"engineering_projection": "e", "drawing_projection": "d"},
        }

    cases = [RepairCaseRecord(**case(index, index < 5)) for index in range(6)]
    common = dict(
        suite="dev",
        spec_version="1",
        spec_fingerprint="f",
        generator_fingerprint="g",
        oracle_version="1",
        candidate_sha="sha",
        thresholds=dict(THRESHOLDS),
        safety_total=0,
        safety_passed=0,
    )
    deterministic = build_benchmark_result(cases=list(cases), track="deterministic", **common)
    model = build_benchmark_result(cases=list(cases), track="model", **common)

    assert deterministic.gates["s5_overall"] is False
    assert model.gates["s5_overall"] is True
    # Neither track rejects on the global S@1 any more (the remote's ruling): it stays a
    # published observation on both, and the contract gate carries the meaning instead.
    assert "s1_overall" not in deterministic.gates
    assert "s1_overall" not in model.gates
    assert "S@1" in deterministic.s_at and "S@1" in model.s_at
    assert "attempt_contract" in deterministic.gates
    assert verify_benchmark_result(model).ok is True


def test_qualification_reports_a_status_that_is_neither_pass_nor_fail(tmp_path: Path):
    """A stubbed model that repairs everything qualifies; the report still verifies."""

    context = _context(tmp_path)
    answer = json.dumps(
        {
            "operations": [
                {"op": "update_element", "element_id": "p1", "patch": {"medium": "process"}}
            ]
        }
    )
    planner = _planner([_completion(answer, usage={"prompt_tokens": 10, "completion_tokens": 5})])
    report, result = run_qualification(
        context, planner=planner, candidate_sha="e" * 40, suite="dev", safety=False
    )

    assert report.status in {"qualified", "not_qualified"}
    assert report.evidence_verified is True
    assert report.planner is not None and report.planner.model == "stub-model"
    assert result.track == "model"
    assert report.counts["total"] == 24
    assert report.latency_ms
