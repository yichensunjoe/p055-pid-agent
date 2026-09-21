"""M5 §G tests: the scale track runs the same repairs on a genuinely large drawing.

A scale track is only worth its runtime if the drawing really is large, if the roles the
operators use are derived from the drawing rather than hard-coded, and if the numbers it
publishes say how localized the work stayed. These tests pin all three.

The deterministic suite is run once per module: it is the case list §G freezes before any model
run, and re-running it per assertion would cost minutes for no extra information.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcad.models import CreateDocumentRequest
from agentcad.repair_benchmark_runner import BenchmarkContext
from agentcad.repair_scale import (
    SCALE_CASES,
    SCALE_MODEL_CASES,
    ScaleReport,
    ScaleSetupError,
    ScaleSource,
    build_synthetic_drawing,
    role_map_for,
    run_scale_suite,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry
from agentcad.validation_profile import built_in_profile, resolve_profile


def _context(tmp_path: Path) -> BenchmarkContext:
    service = DocumentService(SQLiteDocumentStore(tmp_path / "scale.db"), SymbolRegistry())
    return BenchmarkContext(
        service=service,
        profile=resolve_profile(built_in_profile()),
        registry=service.symbols,
    )


@pytest.fixture(scope="module")
def synthetic_report(tmp_path_factory: pytest.TempPathFactory) -> ScaleReport:
    tmp = tmp_path_factory.mktemp("scale")
    return run_scale_suite(
        _context(tmp),
        source=ScaleSource(kind="synthetic", element_target=240),
        candidate_sha="scale-test-candidate",
    )


def test_the_case_list_is_frozen_before_any_model_run():
    """§G5: the three model cases are named here, not chosen after seeing what failed."""

    assert len(SCALE_CASES) == 5
    families = {case_id.split(":")[1] for case_id, _ in SCALE_CASES}
    assert len(families) >= 3
    assert len(SCALE_MODEL_CASES) == 3
    assert set(SCALE_MODEL_CASES) <= {case_id for case_id, _ in SCALE_CASES}


def test_every_case_passes_the_full_oracle_on_the_large_synthetic_drawing(synthetic_report):
    assert synthetic_report.total_cases == 5
    assert synthetic_report.passed_cases == 5
    assert synthetic_report.governance_violations == 0
    assert synthetic_report.reasons == []
    assert synthetic_report.passed() is True


def test_the_synthetic_drawing_is_large_and_still_legal(synthetic_report):
    """Size is the point; a drawing padded with unconnected junk would not be size."""

    assert synthetic_report.element_count >= 200
    assert synthetic_report.connector_count >= 50
    assert synthetic_report.symbol_count >= 100


def test_the_work_stays_localized_even_though_the_drawing_is_large(synthetic_report):
    """The claim the scale track exists to test: context does not grow with the page.

    A context that scaled with the drawing would be hundreds of kilobytes here; the bound is
    deliberately loose so this fails on a real regression rather than on formatting drift.
    """

    for case in synthetic_report.cases:
        assert case.context_elements <= 64, case.case_id
        assert case.context_bytes <= 64 * 1024, case.case_id
        assert case.attempts <= 5, case.case_id
        assert case.writes <= 1, case.case_id


def test_every_case_reports_the_numbers_the_baseline_asks_for(synthetic_report):
    for case in synthetic_report.cases:
        assert case.scope_size > 0, case.case_id
        assert case.validation_ms >= 0, case.case_id
        assert case.shadow_validation_ms >= 0, case.case_id
        assert case.wall_clock_ms > 0, case.case_id
        assert case.rss_bytes > 0, case.case_id


def test_the_seeded_train_never_names_the_case_it_will_be_used_for(tmp_path: Path):
    """§G seed rule: construction may shape the fixture, it may not brief the planner.

    The semantic seed is allowed to decide what the fixture is made of; if it also wrote the
    operator or the word "seed" into an element id, the document handed to the planner would
    say which defect is about to be injected — the case would have to be judged invalid.
    """

    from agentcad.repair_benchmark import MUTATIONS
    from agentcad.repair_scale import _seed_semantic_train, build_synthetic_drawing, role_map_for

    context = _context(tmp_path)
    # A drawing with a real canvas: the seed train is a drafting object, so seeding into an empty
    # document would fail its own quality gates for reasons unrelated to naming.
    document_id = build_synthetic_drawing(
        context.service, context.registry, element_target=120, name="scale host drawing"
    )
    _seed_semantic_train(context, document_id)
    document = context.service.get_document(document_id)

    visible = " ".join(
        [document.name]
        + [
            f"{element.id} {getattr(element, 'name', '')} "
            f"{getattr(element, 'label', '')} {getattr(element, 'process_tag', '')}"
            for element in document.elements
        ]
    ).lower()
    assert "seed" not in visible
    for operator_id in MUTATIONS:
        assert operator_id.lower() not in visible
        assert operator_id.split("_")[0].lower() not in visible

    # The point of the seed still holds: the train is discovered structurally, not by name.
    roles = role_map_for(document)
    assert {"p1", "v1", "v2", "v3"} <= set(roles)


def test_roles_are_derived_from_the_drawing_not_hard_coded(tmp_path: Path):
    """The role map has to be checkable, and the movable element has to be a movable one."""

    context = _context(tmp_path)
    document_id = build_synthetic_drawing(context.service, context.registry, element_target=240)
    document = context.service.get_document(document_id)
    roles = role_map_for(document)
    assert set(roles) >= {"p1", "v1", "v2", "v3"}
    elements = {element.id: element for element in document.elements}
    assert elements[roles["p1"]].type == "connector"
    assert elements[roles["v1"]].type == "symbol"
    assert elements[roles["v2"]].type == "symbol"
    assert elements[roles["v3"]].type == "symbol"
    # ``v1``/``v2`` are the two ends of ``p1``: the roles have to be a real relationship, not
    # three ids that merely happen to exist.
    trunk = elements[roles["p1"]]
    assert {trunk.source.element_id, trunk.target.element_id} == {roles["v1"], roles["v2"]}
    # And ``v3`` is one no connector terminates on, so moving it cannot re-route a line.
    bound = {
        endpoint.element_id
        for element in document.elements
        if element.type == "connector"
        for endpoint in (element.source, element.target)
        if endpoint is not None
    }
    assert roles["v3"] not in bound


def test_a_drawing_with_nothing_repairable_is_refused_not_faked(tmp_path: Path):
    """A raw CAD import is geometry: the track says so instead of inventing a connector."""

    context = _context(tmp_path)
    document = context.service.create_document(CreateDocumentRequest(name="no semantics"))
    with pytest.raises(ScaleSetupError):
        role_map_for(context.service.get_document(document.id))


def test_a_missing_source_is_a_setup_error_not_a_repair_failure(tmp_path: Path):
    context = _context(tmp_path)
    with pytest.raises(ScaleSetupError):
        run_scale_suite(
            context,
            source=ScaleSource(kind="dwg", path=str(tmp_path / "missing.dwg")),
            candidate_sha="scale-test-candidate",
        )


def test_the_scale_run_is_reproducible(tmp_path: Path):
    """Same candidate, same seeds: two runs must agree on the verdicts, not just the counts."""

    first = run_scale_suite(
        _context(tmp_path / "a"),
        source=ScaleSource(kind="synthetic", element_target=240),
        candidate_sha="scale-test-candidate",
        case_ids=("scale:F1", "scale:F2"),
    )
    second = run_scale_suite(
        _context(tmp_path / "b"),
        source=ScaleSource(kind="synthetic", element_target=240),
        candidate_sha="scale-test-candidate",
        case_ids=("scale:F1", "scale:F2"),
    )
    assert [case.classification for case in first.cases] == [
        case.classification for case in second.cases
    ]
    assert [case.attempts for case in first.cases] == [case.attempts for case in second.cases]
    assert [case.case_id for case in first.cases] == [case.case_id for case in second.cases]
