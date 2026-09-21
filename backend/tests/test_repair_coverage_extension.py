"""The coverage-extension contract (baseline §B/§N, review §③).

Four properties have to hold, and each one is the reason a claim in the coverage matrix can be
believed:

* a producer makes the *canonical validator* raise the exact code it declares;
* the governed repair resolves it, judged by the frozen oracle and not by the matrix;
* a code called "unreachable" really cannot be staged by the write or the import surface;
* the frozen 72-case corpus does not move when coverage grows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcad.repair_benchmark import (
    BENCHMARK_SPEC_VERSION,
    core_corpus_fingerprint,
    core_corpus_manifest,
    generate_suite,
    spec_fingerprint,
    supported_code_manifest,
)
from agentcad.repair_benchmark_runner import BenchmarkContext
from agentcad.repair_coverage_extension import (
    COVERAGE_ENTRIES,
    EXTENSION_CORPUS_ID,
    EXTENSION_CORPUS_VERSION,
    EXTENSION_MUTATIONS,
    UNREACHABLE_ENTRIES,
    Manifestation,
    classify_row,
    coverage_fingerprint,
    coverage_manifest,
    extension_cases,
    extension_payload,
    payload_hash,
    probe_representability,
    prove_manifestation,
    run_coverage_extension,
    run_negative_controls,
)
from agentcad.repair_evidence import RepairCaseRecord, case_is_success
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry
from agentcad.validation_profile import built_in_profile, resolve_profile

#: The published corpus fingerprint. Changing a producer, a base drawing or a target binding has
#: to move this on purpose, because every published coverage number is read against it.
FROZEN_EXTENSION_FINGERPRINT = "c198eb77c73157b25eba537fd3fef4264110897f23325d30b37c2bad9d6095e4"


@pytest.fixture()
def context(tmp_path: Path) -> BenchmarkContext:
    service = DocumentService(SQLiteDocumentStore(tmp_path / "coverage.db"), SymbolRegistry())
    return BenchmarkContext(
        service=service,
        profile=resolve_profile(built_in_profile()),
        registry=service.symbols,
    )


def _case(operator_id: str):
    return next(case for case in extension_cases() if case.operator_id == operator_id)


def _manifestation(*, present: bool) -> Manifestation:
    return Manifestation(
        code="DUPLICATE_LABEL",
        validator_id="diagram-quality",
        base_codes=[],
        produced_codes=["DUPLICATE_LABEL"] if present else [],
        added_codes=["DUPLICATE_LABEL"] if present else [],
        target_present=present,
        target_finding={"code": "DUPLICATE_LABEL"} if present else None,
        base_revision=1,
        revision=2,
    )


def _record(**overrides) -> RepairCaseRecord:
    base = dict(
        case_id="unit:case",
        family="F1",
        operator_id="ext_duplicate_label",
        suite="coverage-extension",
        seed=1,
        applied=True,
        repair_hash="r",
        post_validation_hash="p",
        audit_record_id="a",
        selected_plan_hash="s",
        undo_restored_base=True,
        redo_restored_result=True,
        protected_pre={"engineering_projection": "x", "drawing_projection": "y"},
        protected_post={"engineering_projection": "x", "drawing_projection": "y"},
        classification="success",
    )
    return RepairCaseRecord(**{**base, **overrides})


# -- the producer has to really stage the defect it declares ------------------ #


@pytest.mark.parametrize("entry", COVERAGE_ENTRIES, ids=lambda entry: entry.code)
def test_producer_stages_the_exact_code_it_declares(context: BenchmarkContext, entry) -> None:
    """The writer's claim and the validator's verdict have to be the same fact.

    Nothing here reads the operator's name: the drawing is validated before and after the governed
    write, and the finding has to appear in the diff under the declared code and validator. A
    mutation that quietly stopped producing its code fails here instead of shrinking the
    denominator.
    """

    manifestation = prove_manifestation(context, _case(entry.operator_id))

    assert manifestation.ok, manifestation.added_codes
    assert manifestation.code == entry.code
    assert manifestation.validator_id == entry.validator_id
    assert manifestation.code in manifestation.added_codes
    assert manifestation.target_finding is not None
    assert manifestation.target_finding["validator_id"] == entry.validator_id
    assert manifestation.target_finding["waiver_status"] == "not_waived"
    # A real governed revision, not a fixture: the drawing moved forward by one revision.
    assert manifestation.revision > manifestation.base_revision


@pytest.mark.parametrize("entry", COVERAGE_ENTRIES, ids=lambda entry: entry.code)
def test_every_supported_code_is_covered_by_the_oracle(context: BenchmarkContext, entry) -> None:
    """Coverage is the *oracle's* verdict, recomputed from the record it published."""

    report = run_coverage_extension(context)
    row = next(item for item in report.rows if item.code == entry.code)

    assert row.status == "covered", row.notes
    assert row.manifestation.ok
    assert row.case.classification == "success"
    assert case_is_success(row.case)
    # One governed write, with the undo/redo proof travelling with the record.
    assert row.case.write_count == 1
    assert row.case.undo_restored_base and row.case.redo_restored_result
    assert row.case.protected_pre == row.case.protected_post
    assert row.case.audit_record_id
    assert row.manifestation.target_finding is not None
    assert row.manifestation.target_finding["code"] == row.case.target_code


def test_the_extension_corpus_adds_coverage_the_frozen_corpus_does_not_have() -> None:
    """A track that re-tested codes the 72-case corpus already produced would prove nothing."""

    frozen = {row["target_code"] for row in supported_code_manifest()}
    for entry in COVERAGE_ENTRIES:
        assert entry.code not in frozen, f"{entry.code} is already produced by the core corpus"

    # All seven codes this track exists for are accounted for, exactly once each.
    declared = [entry.code for entry in COVERAGE_ENTRIES]
    declared += [entry.code for entry in UNREACHABLE_ENTRIES]
    assert len(set(declared)) == len(declared)
    assert set(declared) == {
        "DUPLICATE_LABEL",
        "SYMBOL_DEFINITION_MISSING",
        "PORT_DIRECTION_MISMATCH",
        "CONNECTOR_ENDPOINT_PORT_MISSING",
        "CONNECTOR_ENDPOINT_POINT_MISMATCH",
        "UNBRIDGED_CROSSING",
        "ANNOTATION_OVERLAP",
    }


# -- the claim that a code cannot be staged ---------------------------------- #


def test_unreachable_codes_cannot_be_staged_by_any_supported_surface(
    context: BenchmarkContext,
) -> None:
    """Each probe has to show *how* the defect is prevented, not only that it is.

    The two surfaces answer differently and the difference is the finding: a governed write may
    refuse the change outright, or it may accept it and re-derive the value so the defect never
    exists. Only the second behaviour would be invisible to a test that just asserted "no
    exception was raised".
    """

    probes = {probe.code: probe for probe in probe_representability(context)}
    assert set(probes) == {entry.code for entry in UNREACHABLE_ENTRIES}

    for code, probe in probes.items():
        assert not probe.reachable, f"{code} became reachable: promote it to a real case"
        write, import_ = probe.attempts
        assert write.surface == "governed_write" and import_.surface == "import"
        assert write.defect_present is False
        assert import_.defect_present is False

    # An unknown symbol key and an unknown port id are refused on both surfaces.
    for code in ("SYMBOL_DEFINITION_MISSING", "CONNECTOR_ENDPOINT_PORT_MISSING"):
        write, import_ = probes[code].attempts
        assert write.accepted is False
        assert import_.accepted is False
        assert "unknown" in write.refusal and "unknown" in import_.refusal

    # A stale binding is the interesting one: the write succeeds and re-derives the point, so the
    # drawing never holds the defect; the import path refuses the file that carries it.
    stale_write, stale_import = probes["CONNECTOR_ENDPOINT_POINT_MISMATCH"].attempts
    assert stale_write.accepted is True and stale_write.defect_present is False
    assert stale_import.accepted is False
    assert "stale" in stale_import.refusal


# -- the gate can go red ----------------------------------------------------- #


def test_negative_controls_all_report(context: BenchmarkContext) -> None:
    """A broken producer, a mislabelled defect and two cheating plans must each be caught."""

    controls = {control.control: control for control in run_negative_controls(context)}
    assert set(controls) == {
        "producer_stages_nothing",
        "producer_declares_wrong_code",
        "planner_edits_outside_scope",
        "planner_deletes_the_target",
    }
    for control in controls.values():
        assert control.red, f"{control.control}: {control.observed_signal}"
        assert control.observed_signal == control.expected_signal


def test_a_row_that_was_never_manifested_is_not_coverage() -> None:
    """``classify_row`` refuses a producer's claim on its own, whatever the repair run did.

    This is the matrix's own defence against inflation: without it a case whose producer failed
    would still be counted the moment its (unrelated) repair happened to succeed.
    """

    assert classify_row(_manifestation(present=False), _record()) == "not_manifested"
    assert classify_row(_manifestation(present=True), _record()) == "covered"
    # A record whose evidence does not recompute as a success is not a covered code either: the
    # classification field alone never decides, because ``classify_case`` re-derives it.
    not_repaired = _record(applied=False, failure_code="target_not_resolved")
    assert not case_is_success(not_repaired)
    assert classify_row(_manifestation(present=True), not_repaired) == "not_repaired"
    required = _record(classification="human_required", applied=False, failure_code="ambiguous")
    assert classify_row(_manifestation(present=True), required) == "not_repaired"


# -- the frozen corpus does not move ----------------------------------------- #


def test_coverage_growth_does_not_move_the_frozen_corpus() -> None:
    """Spec v2 and the 72-case corpus stay exactly where the accepted evidence left them."""

    assert BENCHMARK_SPEC_VERSION == "2"
    assert coverage_manifest()["spec_fingerprint"] == spec_fingerprint()
    manifest = core_corpus_manifest()
    assert manifest["corpus_id"] == "m5-core-corpus"
    assert manifest["case_count"] == 72
    assert manifest["operator_count"] == 19
    assert core_corpus_fingerprint() != coverage_fingerprint()

    # No extension case is in a frozen suite.
    frozen_case_ids = {
        case.case_id
        for suite in ("dev", "acceptance")
        for case in generate_suite(candidate_sha="extension-boundary", suite=suite)
    }
    for case in extension_cases():
        assert case.case_id not in frozen_case_ids
        assert case.suite == "coverage-extension"


def test_extension_corpus_is_frozen() -> None:
    assert EXTENSION_CORPUS_ID == "m5-coverage-extension"
    assert EXTENSION_CORPUS_VERSION == "1"
    assert coverage_fingerprint() == FROZEN_EXTENSION_FINGERPRINT
    assert len(extension_cases()) == len(COVERAGE_ENTRIES)
    assert set(EXTENSION_MUTATIONS) == {entry.operator_id for entry in COVERAGE_ENTRIES}


def test_published_payload_is_recomputable(context: BenchmarkContext) -> None:
    """The report hash has to be recomputable from the report itself, without the runner."""

    payload = extension_payload(context)
    published = dict(payload)
    declared_hash = published.pop("report_hash")
    assert payload_hash(published) == declared_hash
    assert payload["corpus"] == coverage_manifest()
    assert payload["corpus_fingerprint"] == coverage_fingerprint()
    assert payload["negative_controls_all_red"] is True
    assert payload["uncovered"] == []
    assert set(payload["unreachable"]) == {entry.code for entry in UNREACHABLE_ENTRIES}

    # The rows are ordinary case records: readable without importing this module.
    for row in payload["rows"]:
        record = row["case"]
        assert record["schema"] == "pid-agent.repair-benchmark-case"
        assert record["suite"] == "coverage-extension"
        assert record["operator_id"] in EXTENSION_MUTATIONS
        assert record["target_code"] == row["code"]
        json.dumps(record)
