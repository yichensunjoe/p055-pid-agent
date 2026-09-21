"""The coverage ledger contract (baseline §B/§N, remote rulings §③ and "coverage-promotion").

Four properties have to hold, and each one is the reason a claim in the ledger can be believed:

* every code the ledger calls **promoted** really is carried by the frozen catalogue, and the case
  the frozen generator derives for it really makes the *canonical validator* raise that exact code
  and really gets resolved by the ordinary oracle;
* every code the ledger calls **unreachable** really cannot be staged by the write or the import
  surface, under the disposition the ledger names;
* the negative controls still go red, so the gate is a gate and not a report;
* the *superseded* identities of the staging area this ledger replaced are recorded rather than
  lost, and the published v2 spec is still recomputable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcad.repair_benchmark import (
    ACCEPTANCE_CASES_PER_FAMILY,
    BENCHMARK_SPEC_VERSION,
    CORE_CORPUS_ID,
    CORE_CORPUS_VERSION,
    MUTATIONS,
    SPEC_FINGERPRINT_V2,
    core_corpus_fingerprint,
    core_corpus_manifest,
    generate_cases,
    generate_suite,
    operators_for_family,
    spec_fingerprint,
    spec_payload,
    supported_code_manifest,
)
from agentcad.repair_benchmark_runner import BenchmarkContext
from agentcad.repair_coverage_ledger import (
    ENVIRONMENT_DERIVED_KEYS,
    LEDGER_ID,
    LEDGER_VERSION,
    NORMALIZED_OR_REJECTED_AT_SUPPORTED_INGRESS,
    PROMOTED_ENTRIES,
    SUPERSEDED_EXTENSION_IDENTITIES,
    UNREACHABLE_AT_SUPPORTED_INGRESS,
    UNREACHABLE_ENTRIES,
    CoverageLedgerError,
    RepresentationProbe,
    SurfaceAttempt,
    assert_ledger_is_sound,
    ledger_digest,
    ledger_fingerprint,
    ledger_manifest,
    ledger_payload,
    payload_hash,
    probe_representability,
    promotion_cases,
    promotion_status,
    prove_promotion,
    run_negative_controls,
)
from agentcad.repair_evidence import RepairCaseRecord, case_is_success
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry
from agentcad.validation_profile import built_in_profile, resolve_profile

#: The identities the staging area published before the promotion. They are *recorded*, never
#: recomputed: the corpus and the producer set they hashed no longer exist, and the full
#: fingerprint digests CPython bytecode, so it could not be recomputed on another interpreter even
#: then. This test exists so the numbers stay quoted next to the reason they are frozen.
PUBLISHED_EXTENSION_CORPUS_DIGEST = "3dc8ca1ade6c5adbe5f8c3a5fab8709eff3ffdeb4f904a4da4fd8f7ae6b19969"
PUBLISHED_EXTENSION_REPORT_HASH = "f56c2c5098119a0b753660ba6a33a0fb7bcc03c24a545f0fd1849f6e44fe0931"


@pytest.fixture()
def context(tmp_path: Path) -> BenchmarkContext:
    service = DocumentService(SQLiteDocumentStore(tmp_path / "coverage.db"), SymbolRegistry())
    return BenchmarkContext(
        service=service,
        profile=resolve_profile(built_in_profile()),
        registry=service.symbols,
    )


def _record(**overrides) -> RepairCaseRecord:
    base = dict(
        case_id="unit:case",
        family="F1",
        operator_id="f1_duplicate_label",
        suite="acceptance",
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


# -- the promotion is a fact about the frozen catalogue ---------------------- #


@pytest.mark.parametrize("entry", PROMOTED_ENTRIES, ids=lambda entry: entry.code)
def test_a_promoted_code_is_really_carried_by_the_frozen_catalogue(entry) -> None:
    """The ledger names an operator; the frozen catalogue has to agree, in the same family.

    Nothing here trusts the ledger: the operator has to be registered, has to declare the same
    code and validator, and has to be reachable through the family rotation the acceptance suite
    actually uses.
    """

    operator = MUTATIONS[entry.operator_id]
    assert operator.family == entry.family
    assert operator.target_code == entry.code
    assert operator.target_validator_id == entry.validator_id
    assert entry.operator_id in operators_for_family(entry.family)
    # And it is in the frozen corpus's own published code manifest, which is a claim the benchmark
    # enforces at run time: an operator that stops producing its code becomes an invalid case.
    manifest = {row["operator_id"]: row for row in supported_code_manifest()}
    assert manifest[entry.operator_id]["target_code"] == entry.code


@pytest.mark.parametrize("entry", PROMOTED_ENTRIES, ids=lambda entry: entry.code)
def test_a_promoted_code_is_covered_by_the_frozen_oracle(
    context: BenchmarkContext, entry
) -> None:
    """Coverage is the *oracle's* verdict, recomputed from the record the frozen runner published."""

    proof = next(item for item in prove_promotion(context) if item.code == entry.code)

    assert proof.status == "covered", proof.notes
    assert proof.manifested
    assert proof.target_finding is not None
    assert proof.target_finding["code"] == entry.code
    assert proof.target_finding["validator_id"] == entry.validator_id
    assert proof.target_finding["waiver_status"] == "not_waived"
    assert proof.case.classification == "success"
    assert case_is_success(proof.case)
    # One governed write, with the undo/redo proof travelling with the record.
    assert proof.case.write_count == 1
    assert proof.case.undo_restored_base and proof.case.redo_restored_result
    assert proof.case.protected_pre == proof.case.protected_post
    assert proof.case.audit_record_id


def test_promotion_cases_come_out_of_the_frozen_generator_not_a_table() -> None:
    """The case that owns a promoted code has to be the one CI derives for the same candidate."""

    cases = promotion_cases("candidate-under-test")
    assert set(cases) == {entry.code for entry in PROMOTED_ENTRIES}
    derived = {
        case.case_id
        for case in generate_suite(candidate_sha="candidate-under-test", suite="acceptance")
    }
    for entry in PROMOTED_ENTRIES:
        case = cases[entry.code]
        assert case.case_id in derived, "the ledger case is not in the frozen acceptance suite"
        assert case.operator_id == entry.operator_id
        assert case.family == entry.family
    # The same candidate derives the same promotion, and a different one is a different case set.
    assert {
        code: case.case_id for code, case in promotion_cases("candidate-under-test").items()
    } == {code: case.case_id for code, case in cases.items()}
    assert promotion_cases("other-candidate")["DUPLICATE_LABEL"].seed != cases["DUPLICATE_LABEL"].seed


def test_every_promoted_code_has_a_case_in_each_full_acceptance_suite() -> None:
    """A promotion that only appears for one candidate SHA would not be a corpus property."""

    for candidate in ("a" * 40, "b" * 40):
        derived = [
            case.operator_id
            for case in generate_suite(candidate_sha=candidate, suite="acceptance")
        ]
        for entry in PROMOTED_ENTRIES:
            assert entry.operator_id in derived
        # 12 acceptance cases per family still covers every operator in a four-operator family.
        for family in ("F2", "F4", "F5"):
            assert len(operators_for_family(family)) <= ACCEPTANCE_CASES_PER_FAMILY


def test_the_ledger_covers_the_codes_the_promotion_did_not_take() -> None:
    """All seven codes this track exists for are accounted for, exactly once each."""

    promoted = [entry.code for entry in PROMOTED_ENTRIES]
    dispositions = [entry.code for entry in UNREACHABLE_ENTRIES]
    assert len(set(promoted) & set(dispositions)) == 0
    assert set(promoted) | set(dispositions) == {
        "DUPLICATE_LABEL",
        "SYMBOL_DEFINITION_MISSING",
        "PORT_DIRECTION_MISMATCH",
        "CONNECTOR_ENDPOINT_PORT_MISSING",
        "CONNECTOR_ENDPOINT_POINT_MISMATCH",
        "UNBRIDGED_CROSSING",
        "ANNOTATION_OVERLAP",
    }
    # Each promotion is traceable back to the staging operator that justified it, and no two
    # promotions claim the same staging operator or the same frozen operator.
    assert len({entry.staged_by for entry in PROMOTED_ENTRIES}) == len(PROMOTED_ENTRIES)
    assert len({entry.operator_id for entry in PROMOTED_ENTRIES}) == len(PROMOTED_ENTRIES)
    for entry in PROMOTED_ENTRIES:
        assert entry.staged_by not in MUTATIONS, "the staging operator is not part of the corpus"


def test_a_promotion_that_stopped_manifesting_would_not_be_coverage() -> None:
    """``promotion_status`` refuses the claim on its own, whatever the repair run did.

    This is the ledger's own defence against inflation: without it a promoted code whose producer
    failed would still be counted the moment its (unrelated) repair happened to succeed.
    """

    assert promotion_status(False, _record()) == "not_manifested"
    assert promotion_status(True, _record()) == "covered"
    # A record whose evidence does not recompute as a success is not a covered code either: the
    # classification field alone never decides, because ``classify_case`` re-derives it.
    not_repaired = _record(applied=False, failure_code="target_not_resolved")
    assert not case_is_success(not_repaired)
    assert promotion_status(True, not_repaired) == "not_repaired"
    required = _record(classification="human_required", applied=False, failure_code="ambiguous")
    assert promotion_status(True, required) == "not_repaired"


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
        # The disposition is one of the remote's two formal names, and it is recorded on the probe
        # and in the manifest -- a reader can grep for it in a published report.
        assert probe.disposition in {
            UNREACHABLE_AT_SUPPORTED_INGRESS,
            NORMALIZED_OR_REJECTED_AT_SUPPORTED_INGRESS,
        }
        write, import_ = probe.attempts
        assert write.surface == "governed_write" and import_.surface == "import"
        assert write.defect_present is False
        assert import_.defect_present is False

    # An unknown symbol key and an unknown port id are refused on both surfaces.
    for code in ("SYMBOL_DEFINITION_MISSING", "CONNECTOR_ENDPOINT_PORT_MISSING"):
        write, import_ = probes[code].attempts
        assert probes[code].disposition == UNREACHABLE_AT_SUPPORTED_INGRESS
        assert write.accepted is False
        assert import_.accepted is False
        assert "unknown" in write.refusal and "unknown" in import_.refusal

    # A stale binding is the interesting one: the write succeeds and re-derives the point, so the
    # drawing never holds the defect; the import path refuses the file that carries it. That is why
    # its disposition is the other name.
    stale = probes["CONNECTOR_ENDPOINT_POINT_MISMATCH"]
    assert stale.disposition == NORMALIZED_OR_REJECTED_AT_SUPPORTED_INGRESS
    stale_write, stale_import = stale.attempts
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


def test_the_ledger_refuses_an_unsound_payload() -> None:
    """``assert_ledger_is_sound`` is what turns the three conditions into an exit code.

    Without it the command would print a report and exit 0 whatever the report said, which is the
    failure mode the whole track exists to avoid.
    """

    sound = {
        "promoted_but_not_covered": [],
        "became_reachable": [],
        "negative_controls_all_red": True,
    }
    assert_ledger_is_sound(sound)

    with pytest.raises(CoverageLedgerError):
        assert_ledger_is_sound({**sound, "promoted_but_not_covered": ["DUPLICATE_LABEL"]})
    with pytest.raises(CoverageLedgerError):
        assert_ledger_is_sound({**sound, "became_reachable": ["SYMBOL_DEFINITION_MISSING"]})
    with pytest.raises(CoverageLedgerError):
        assert_ledger_is_sound({**sound, "negative_controls_all_red": False})


def test_the_reachability_check_is_the_defect_and_not_the_write() -> None:
    """A probe that accepted the write but left no defect is not reachable, and says so."""

    silent = RepresentationProbe(
        code="CONNECTOR_ENDPOINT_POINT_MISMATCH",
        family="F2",
        validator_id="engineering-report",
        disposition=NORMALIZED_OR_REJECTED_AT_SUPPORTED_INGRESS,
        attempted_write="leave a connector endpoint bound to a stale coordinate",
        attempts=[
            SurfaceAttempt(
                surface="governed_write", accepted=True, refusal="", defect_present=False
            )
        ],
    )
    assert silent.reachable is False
    staged = RepresentationProbe(
        code="CONNECTOR_ENDPOINT_POINT_MISMATCH",
        family="F2",
        validator_id="engineering-report",
        disposition=NORMALIZED_OR_REJECTED_AT_SUPPORTED_INGRESS,
        attempted_write="leave a connector endpoint bound to a stale coordinate",
        attempts=[
            SurfaceAttempt(
                surface="governed_write", accepted=True, refusal="", defect_present=True
            )
        ],
    )
    assert staged.reachable is True


# -- the corpus and the ledger identities ------------------------------------ #


def test_the_promotion_cut_the_next_spec_and_corpus() -> None:
    """Spec v3 / corpus v3, with the four codes inside and the thresholds untouched."""

    assert BENCHMARK_SPEC_VERSION == "3"
    assert CORE_CORPUS_VERSION == "3"
    manifest = core_corpus_manifest()
    assert manifest["corpus_id"] == CORE_CORPUS_ID
    assert manifest["case_count"] == 72
    assert manifest["operator_count"] == len(MUTATIONS) == 23
    assert manifest["spec_version"] == BENCHMARK_SPEC_VERSION
    assert manifest["spec_fingerprint"] == spec_fingerprint()
    # The gate did not change shape: same thresholds, same oracle, same families, same layout.
    assert spec_payload()["oracle_version"] == "1"
    assert spec_payload()["acceptance_cases_per_family"] == 12
    assert spec_payload()["safety_cases"] == 12
    assert core_corpus_fingerprint() != ledger_fingerprint()

    frozen_codes = {row["target_code"] for row in supported_code_manifest()}
    for entry in PROMOTED_ENTRIES:
        assert entry.code in frozen_codes, f"{entry.code} is not in the frozen code manifest"


def test_the_published_v2_spec_is_still_recomputable() -> None:
    """A fingerprint that cannot be recomputed is a number a reader has to take on trust.

    The archive exists so the published v2 identity still has a body to hash. If somebody edits the
    archive -- or the working spec in a way that leaks into it -- this fails, which is the only
    reason an archive in the same module as the live catalogue is safe.
    """

    assert spec_fingerprint("2") == SPEC_FINGERPRINT_V2
    archived = spec_payload("2")
    assert archived["spec_version"] == "2"
    assert archived["oracle_version"] == "1"
    assert len(archived["operators"]) == 19
    assert "f1_duplicate_label" not in {row["operator_id"] for row in archived["operators"]}
    # The working spec is a different body, and asking for a version nobody archived is an error
    # rather than a silent answer with today's spec.
    assert spec_fingerprint() != SPEC_FINGERPRINT_V2
    with pytest.raises(ValueError):
        spec_fingerprint("1")
    with pytest.raises(ValueError):
        spec_payload("99")


def test_the_published_v2_case_set_is_still_derivable() -> None:
    """Not only the v2 fingerprint: the v2 *rotation* has to be recoverable from the archive.

    Deriving the old cases from today's catalogue with the old seeds would produce a case set that
    never ran -- the promoted operators would appear in it. This is the check that the pre side of
    the promotion projection is a derivation and not a reconstruction.
    """

    from agentcad.repair_benchmark import archived_operator_catalogue

    v2_operators = archived_operator_catalogue("2")
    pre = []
    for family in ("F1", "F2", "F3", "F4", "F5", "F6"):
        pre.extend(
            generate_cases(
                candidate_sha="c" * 40,
                family=family,
                count=ACCEPTANCE_CASES_PER_FAMILY,
                suite="acceptance",
                spec=spec_fingerprint("2"),
                catalogue=v2_operators,
            )
        )
    assert len(pre) == 72
    assert {case.operator_id for case in pre} == set(v2_operators)
    for entry in PROMOTED_ENTRIES:
        assert entry.operator_id not in {case.operator_id for case in pre}
    # And today's catalogue answers with a different set, which is what a spec cut means.
    post = generate_suite(candidate_sha="c" * 40, suite="acceptance")
    assert [case.operator_id for case in pre] != [case.operator_id for case in post]
    assert {case.operator_id for case in post} == set(MUTATIONS)


def test_the_superseded_extension_identities_are_recorded_not_recomputed() -> None:
    """The staging area's published numbers stay quoted next to the reason they are frozen."""

    assert SUPERSEDED_EXTENSION_IDENTITIES["corpus_id"] == "m5-coverage-extension"
    assert (
        SUPERSEDED_EXTENSION_IDENTITIES["coverage_corpus_digest"]
        == PUBLISHED_EXTENSION_CORPUS_DIGEST
    )
    assert (
        SUPERSEDED_EXTENSION_IDENTITIES["coverage_report_hash"]
        == PUBLISHED_EXTENSION_REPORT_HASH
    )
    assert set(SUPERSEDED_EXTENSION_IDENTITIES["coverage_fingerprint_by_interpreter"]) == {
        "3.11",
        "3.12",
    }
    superseded_by = SUPERSEDED_EXTENSION_IDENTITIES["superseded_by"]
    assert superseded_by["corpus_id"] == CORE_CORPUS_ID
    assert superseded_by["corpus_version"] == CORE_CORPUS_VERSION
    assert superseded_by["ledger_id"] == LEDGER_ID
    # The ledger's own identity is a new one, so nothing here reads as if the corpus digest above
    # still described something that exists.
    assert ledger_digest() != PUBLISHED_EXTENSION_CORPUS_DIGEST


def test_the_ledger_identity_is_the_corpus_independent_part_and_the_rest_is_provenance() -> None:
    """Same rule as the corpus identity: bytecode digests move with the interpreter, identities do not."""

    manifest = ledger_manifest()
    assert LEDGER_ID == "m5-coverage-ledger"
    assert LEDGER_VERSION == "1"
    assert ledger_fingerprint() == payload_hash(manifest)
    assert set(ENVIRONMENT_DERIVED_KEYS) <= set(manifest)
    assert ledger_digest() != ledger_fingerprint()
    # The digest is the manifest with the environment-derived keys removed, and nothing else.
    stripped = {
        key: value for key, value in manifest.items() if key not in ENVIRONMENT_DERIVED_KEYS
    }
    assert payload_hash(stripped) == ledger_digest()


def test_the_ledger_digest_does_not_move_with_the_interpreter(monkeypatch) -> None:
    """The property the corpus identity was split for, proven without a second interpreter.

    ``generator_fingerprint`` digests CPython bytecode, so it differs between 3.11 and 3.12 for the
    same ledger. Rather than wait for CI to demonstrate that, the field is replaced with a
    different runtime's value here: the identity has to stay put and the published fingerprint has
    to move. A digest that moved with the bytecode digest would be "frozen on this machine".
    """

    from agentcad import repair_coverage_ledger as ledger

    digest = ledger.ledger_digest()
    published = ledger.ledger_fingerprint()
    monkeypatch.setattr(
        ledger, "generator_fingerprint", lambda: "fda18c2fd1c7e53eb0d8ec7e76094ca127c85beb"
    )

    assert ledger.ledger_digest() == digest
    assert ledger.ledger_fingerprint() != published


def test_published_payload_is_recomputable(context: BenchmarkContext) -> None:
    """The report hash has to be recomputable from the report itself, without the runner."""

    payload = ledger_payload(context)
    published = {key: value for key, value in payload.items() if key != "report_hash"}
    assert payload_hash(published) == payload["report_hash"]
    assert payload["ledger"] == ledger_manifest()
    assert payload["ledger_fingerprint"] == ledger_fingerprint()
    assert payload["ledger_digest"] == ledger_digest()
    assert payload["negative_controls_all_red"] is True
    assert payload["promoted_but_not_covered"] == []
    assert set(payload["promoted"]) == {entry.code for entry in PROMOTED_ENTRIES}
    assert set(payload["unreachable"]) == {entry.code for entry in UNREACHABLE_ENTRIES}
    assert payload["became_reachable"] == []

    # The rows are ordinary case records: readable without importing this module.
    for proof in payload["promotions"]:
        record = proof["case"]
        assert record["schema"] == "pid-agent.repair-benchmark-case"
        assert record["suite"] == "acceptance"
        assert record["operator_id"] in MUTATIONS
        assert record["target_code"] == proof["code"]
        json.dumps(record)
