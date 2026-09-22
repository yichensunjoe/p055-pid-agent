"""The core corpus identity: what the corpus *is*, on any interpreter (remote A5).

``core_corpus_fingerprint`` hashes the whole corpus manifest, ``generator_fingerprint`` included,
and that one digests CPython bytecode: the same frozen corpus published ``c85995d2…`` on 3.12 and
``82b37b04…`` on 3.11. A frozen number that moves with the interpreter can only mean "frozen on
this machine", which is the opposite of what the word is for.

So the corpus gets a second, narrower identity, and this file is its contract:

* ``core_corpus_digest`` is a pure function of the corpus definition — the layout, the derivation
  rule, the operator catalogue **and the actual case universe** — and of nothing that describes the
  world it runs in (``spec_fingerprint``) or the interpreter it runs on
  (``generator_fingerprint``). Both halves are tested here: the sensitivity tests below prove the
  identity *sees* every field that decides which cases exist, and the stability tests prove it does
  not see the interpreter;
* the archived v2 corpus is replayed from its frozen spec body, so "these are the cases v2 ran" is
  still checkable, and v2 gets its own digest;
* ``core_corpus_fingerprint`` is *not* redefined: it keeps answering the provenance question it has
  always answered, and the test below proves the two are different questions;
* publishing the corpus coordinates in a result must not move either result hash: a payload
  published before the fields existed has to stay comparable with the same result after.

The two goldens are the values this shape of the corpus produces. They are pinned on purpose: the
digest is the number two machines have to agree on, so a silent change to it is the defect.
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agentcad import repair_benchmark as benchmark
from agentcad.repair_benchmark import (
    ACCEPTANCE_CASES_PER_FAMILY,
    ARCHIVED_DEFINITION_IDENTITY,
    CORE_CORPUS_VERSION,
    CORPUS_IDENTITY_EXCLUDED_KEYS,
    MUTATIONS,
    SAFETY_COUNT_MISMATCH,
    SAFETY_UNIVERSE_ARCHIVED,
    SAFETY_UNIVERSE_PROJECTED,
    SAFETY_UNIVERSE_SOURCE,
    SPEC_FINGERPRINT_V2,
    _referenced_names,
    core_corpus_digest,
    core_corpus_digest_manifest,
    core_corpus_fingerprint,
    core_corpus_manifest,
    core_corpus_projection,
    corpus_definition_ast_projection,
    generate_suite,
    spec_fingerprint,
)
from agentcad.repair_evidence import RepairBenchmarkResult, build_benchmark_result
from agentcad.repair_models import (
    REPAIR_LEGACY_HASH_EXCLUDES,
    REPAIR_SEMANTIC_EXCLUDED_FIELDS,
    canonical_repair_result_payload,
    repair_digest,
    repair_semantic_digest,
)
from agentcad.source_identity import ast_identity, module_definition_identities

#: The corpus identity of the frozen v3 corpus: pure data, identical on CPython 3.11 and 3.12.
V3_DIGEST = "96b999fa90403c82b58018ef82b06bb190932d97a66f179ebde2c0e2ef41c10f"

#: The archived v2 corpus's digest. Derived under A5, so it is an *archival* identity: v2 never
#: published this number, and saying otherwise would be inventing a field history.
V2_DIGEST = "edb1c5d385bfd4f13a195119444dcb157f3bf0d893afd35e595ddc28c4b1c36c"

#: The frozen v3 evidence, kept exactly as it was published. Recomputing it is the point.
V3_EVIDENCE = (
    Path(__file__).resolve().parents[2] / "reports" / "m5-promotion" / "acceptance-v3.json"
)

#: The frozen v2 payload, published under ``BENCHMARK_SPEC_VERSION`` 2.
V2_EVIDENCE = Path(__file__).resolve().parents[2] / "reports" / "m5" / "repair-benchmark-acceptance.json"


def _canonical_hash(payload: dict) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _recompute_hashes(payload: dict) -> tuple[str, str]:
    """``(legacy, semantic)`` for a published payload, the way the builder computes them.

    The legacy hash is taken over the record *before* the two hash fields are stamped, which is how
    the runner does it; blanking them is what makes the published payload recomputable.
    """

    blanked = {**payload, "benchmark_result_hash": "", "benchmark_semantic_hash": ""}
    model = RepairBenchmarkResult.model_validate(blanked)
    legacy = repair_digest(model, exclude=REPAIR_LEGACY_HASH_EXCLUDES)
    return legacy, repair_semantic_digest(model)


# -- the corpus identity ------------------------------------------------------ #


def test_v3_corpus_digest_is_pinned() -> None:
    """The working corpus has one identity, and it is this number."""

    assert CORE_CORPUS_VERSION == "3"
    assert core_corpus_digest() == V3_DIGEST
    assert core_corpus_digest(CORE_CORPUS_VERSION) == V3_DIGEST
    # Published input, not just a published output: a reviewer recomputes it from the manifest.
    assert _canonical_hash(core_corpus_digest_manifest()) == V3_DIGEST


def test_archived_v2_corpus_is_replayed_from_its_frozen_body() -> None:
    """v2's case universe comes back from the archived spec, so the digest is checkable."""

    manifest = core_corpus_manifest("2")
    assert core_corpus_digest("2") == V2_DIGEST
    assert manifest["corpus_version"] == "2"
    assert manifest["spec_version"] == "2"
    assert manifest["spec_fingerprint"] == spec_fingerprint("2") == SPEC_FINGERPRINT_V2
    # 19 operators, 16 of them carrying a code; 12 x 6 = 72 cases, the same layout v3 kept.
    assert manifest["operator_count"] == 19
    assert len(manifest["supported_codes"]) == 16
    assert manifest["case_count"] == 72
    # One field genuinely cannot come back, and its absence is documented rather than faked: the v2
    # operators' bytecode is gone with the code, so there is no v2 generator fingerprint to record.
    assert "generator_fingerprint" not in manifest


def test_unknown_corpus_version_raises_instead_of_answering_with_today() -> None:
    with pytest.raises(ValueError):
        core_corpus_digest("9")
    with pytest.raises(ValueError):
        core_corpus_manifest("9")


def _keys_at_any_depth(payload) -> set[str]:
    if isinstance(payload, dict):
        return set(payload) | {
            key for value in payload.values() for key in _keys_at_any_depth(value)
        }
    if isinstance(payload, list):
        return {key for item in payload for key in _keys_at_any_depth(item)}
    return set()


def test_digest_ignores_the_interpreter_and_the_spec_fingerprint() -> None:
    """The identity must survive exactly the two things that move under it."""

    assert set(CORPUS_IDENTITY_EXCLUDED_KEYS) == {"spec_fingerprint", "generator_fingerprint"}
    manifest = core_corpus_manifest()
    projection = core_corpus_digest_manifest()
    # Neither name appears at any depth of the projection -- the vocabulary the digest is written in
    # does not contain the two things that used to bind it to one machine and one spec revision.
    assert not _keys_at_any_depth(projection) & set(CORPUS_IDENTITY_EXCLUDED_KEYS)
    assert _canonical_hash(projection) == V3_DIGEST

    # The two published values that differ between 3.11 and 3.12, and between spec versions.
    swapped = {
        **manifest,
        "generator_fingerprint": "43979207ca8f9b608996277f63cf993cc33175c6e5cb79dd2ae71ee4ba60857c",
        "spec_fingerprint": "0" * 64,
    }
    assert core_corpus_digest() == V3_DIGEST

    # ... and the docstring's promise, measured: the *fingerprint* is a different question and it
    # does move, which is why it stays labelled provenance instead of identity.
    assert _canonical_hash(swapped) != core_corpus_fingerprint()
    assert _canonical_hash(manifest) == core_corpus_fingerprint()


# -- the result payload ------------------------------------------------------- #


def _minimal_result(**overrides):
    return build_benchmark_result(
        suite="acceptance",
        track="deterministic",
        spec_version="3",
        spec_fingerprint=spec_fingerprint(),
        generator_fingerprint="generator-fingerprint",
        oracle_version="1",
        candidate_sha="c" * 40,
        safety_total=1,
        safety_passed=1,
        **overrides,
    )


def test_publishing_corpus_coordinates_does_not_move_either_hash() -> None:
    """Derived metadata is metadata: the same result hashes the same with or without it."""

    without = _minimal_result()
    with_coordinates = _minimal_result(
        corpus_version=CORE_CORPUS_VERSION, core_corpus_digest=core_corpus_digest()
    )
    assert with_coordinates.corpus_version == "3"
    assert with_coordinates.core_corpus_digest == V3_DIGEST
    assert with_coordinates.benchmark_result_hash == without.benchmark_result_hash
    assert with_coordinates.benchmark_semantic_hash == without.benchmark_semantic_hash
    # Excluded by name in both contracts, so neither hash can see them at any depth.
    assert {"corpus_version", "core_corpus_digest"} <= REPAIR_SEMANTIC_EXCLUDED_FIELDS
    assert {"corpus_version", "core_corpus_digest"} <= REPAIR_LEGACY_HASH_EXCLUDES
    assert "corpus_version" not in canonical_repair_result_payload(with_coordinates)
    assert "core_corpus_digest" not in canonical_repair_result_payload(with_coordinates)


def test_frozen_v3_evidence_still_recomputes_with_the_new_fields() -> None:
    """The published v3 payload is the ground truth for pre/post-A5 compatibility."""

    payload = json.loads(V3_EVIDENCE.read_text(encoding="utf-8"))
    legacy, semantic = _recompute_hashes(payload)
    assert legacy == payload["benchmark_result_hash"]
    assert semantic == payload["benchmark_semantic_hash"]

    # The same payload as an A5+ runner would publish it: coordinates added, hashes unchanged.
    with_coordinates = {
        **payload,
        "corpus_version": CORE_CORPUS_VERSION,
        "core_corpus_digest": core_corpus_digest(),
    }
    assert _recompute_hashes(with_coordinates) == (legacy, semantic)
    model = RepairBenchmarkResult.model_validate({**with_coordinates, "benchmark_result_hash": "", "benchmark_semantic_hash": ""})
    assert model.core_corpus_digest == V3_DIGEST
    assert repair_semantic_digest(model) == payload["benchmark_semantic_hash"]
    assert (
        repair_digest(model, exclude=REPAIR_LEGACY_HASH_EXCLUDES)
        == payload["benchmark_result_hash"]
    )


def test_frozen_v2_evidence_still_recomputes() -> None:
    """The v2 payload keeps verifying under v3 code: the published evidence is not rewritten.

    It predates the semantic hash, so only the legacy value is a published claim; the semantic one
    is derived here for the first time and is labelled as such rather than pretended to be
    historical. That asymmetry is the compatibility invariant: new code, old payload, old number.
    """

    payload = json.loads(V2_EVIDENCE.read_text(encoding="utf-8"))
    legacy, semantic = _recompute_hashes(payload)
    assert legacy == payload["benchmark_result_hash"]
    assert "benchmark_semantic_hash" not in payload
    assert "semantic_hash_version" not in payload
    assert semantic != legacy
    assert payload["spec_fingerprint"] == SPEC_FINGERPRINT_V2
    # The v2 corpus coordinates are answerable from the payload's own spec version.


# -- what the identity sees --------------------------------------------------- #
#
# f60693d's digest covered the layout and the counts, and the remote review found the mechanical
# counterexample: rotation, base, policy and producer can all change while those numbers stay put.
# The tests below are the other half of that review -- every field that decides *which cases exist,
# drawn by which producer* has to move the digest -- followed by the interpreter stability they
# already had, so the two together are the boundary of the identity rather than one side of it.


def test_projection_carries_the_case_universe_and_the_catalogue() -> None:
    """Not a summary: every case in both suites, every operator, every declaration."""

    projection = core_corpus_projection()
    assert sorted(projection) == [
        "acceptance_cases_per_family",
        "case_derivation",
        "case_universe",
        "corpus_id",
        "corpus_version",
        "dev_case_universe",
        "dev_cases_per_family",
        "families",
        "operator_catalogue",
        "safety_case_universe",
        "spec_version",
    ]
    assert len(projection["operator_catalogue"]) == len(MUTATIONS) == 23
    assert len(projection["case_universe"]) == 72
    assert len(projection["dev_case_universe"]) == 24
    # Each case is self-contained: the producer that draws it and that producer's declaration, so a
    # reviewer reads the case universe without joining it against a separate table.
    assert sorted(projection["case_universe"][0]) == [
        "case_id",
        "family",
        "index",
        "operator_declaration",
        "operator_id",
        "suite",
    ]
    assert projection["case_derivation"] == {
        "rotation": "operators_for_family(family) -- sorted by operator_id -- cycled by case index",
        "seed_material": "spec_fingerprint(spec_version) : candidate_sha : family : index",
        "seed_digest": "sha256(material).hexdigest()[:12], read as a base-16 integer",
    }
    # And the catalogue names the code of the code that draws it, not only the declarations.
    row = projection["operator_catalogue"][0]
    assert row["producer_definition_identity"] and row["base_builder_definition_identity"]
    assert ARCHIVED_DEFINITION_IDENTITY not in row.values()


def test_projection_case_universe_is_the_suite_the_runner_builds() -> None:
    """The projection cannot drift from the generator: same rotation, same case ids."""

    rows = core_corpus_projection()["case_universe"]
    built = generate_suite(candidate_sha="f" * 40, suite="acceptance")
    assert [(row["case_id"], row["operator_id"]) for row in rows] == [
        (case.case_id, case.operator_id) for case in built
    ]


def test_the_safety_suite_is_projected_as_cases_not_as_a_count() -> None:
    """§D is a 100% requirement, so its cases belong in the identity like any other case set.

    The corpus published ``safety_case_count`` and nothing else, which is a number that cannot notice
    a case being added, removed, retitled or rebuilt. The rows below are the suite itself.
    """

    universe = core_corpus_projection()["safety_case_universe"]
    cases = universe["cases"]
    assert universe["status"] == SAFETY_UNIVERSE_PROJECTED
    assert universe["source"] == SAFETY_UNIVERSE_SOURCE
    assert len(cases) == 13
    assert sorted(cases[0]) == ["builder_definition_identity", "case_id", "title"]
    assert {row["case_id"] for row in cases} >= {
        "d1_ambiguous_delete",
        "d2_locked_layer",
        "d8b_stale_validation_hash",
        "d12_raw_bypass_mutation",
    }
    assert all(row["title"] and row["builder_definition_identity"] for row in cases)


def test_the_declared_and_actual_safety_counts_are_separate_fields() -> None:
    """One field must not mean two things: the spec's 12 is not quietly the suite's size.

    The declared number lives in the frozen spec body, so correcting it there would move
    ``spec_fingerprint``; the suite has 13 cases because ``d8b_stale_validation_hash`` joined the
    code table afterwards. What the identity owes a reader is both numbers and a name for the
    disagreement -- not a single field that silently stands for whichever one it happens to hold.
    """

    universe = core_corpus_projection()["safety_case_universe"]
    assert universe["spec_declared_safety_case_count"] == 12
    assert universe["actual_safety_case_count"] == 13
    # The invariant the remote asked to be a hard failure: the actual number is the universe's size,
    # always, so a corpus whose suite changed cannot keep publishing the old count.
    assert universe["actual_safety_case_count"] == len(universe["cases"])
    assert universe["count_disposition"] == {
        "declared": 12,
        "actual": 13,
        "status": SAFETY_COUNT_MISMATCH,
    }
    assert "safety_case_count" not in core_corpus_projection()


def test_an_archived_corpus_declares_its_missing_safety_universe() -> None:
    """``None`` is a documented absence, never "v2 had no safety cases" (which ``[]`` would say)."""

    universe = core_corpus_projection("2")["safety_case_universe"]
    assert universe["status"] == SAFETY_UNIVERSE_ARCHIVED
    assert universe["cases"] is None
    assert universe["actual_safety_case_count"] is None
    assert universe["spec_declared_safety_case_count"] == 12
    assert universe["count_disposition"]["status"] == SAFETY_UNIVERSE_ARCHIVED


def test_retitling_a_safety_case_moves_the_identity(monkeypatch) -> None:
    from agentcad import repair_safety

    cases = list(repair_safety.SAFETY_CASES)
    cases[0] = replace(cases[0], title="a different title")
    monkeypatch.setattr(repair_safety, "SAFETY_CASES", tuple(cases))
    assert core_corpus_digest() != V3_DIGEST


def test_rebuilding_a_safety_case_moves_the_identity(monkeypatch) -> None:
    """Same declarations, different code: the suite the corpus is judged by has changed."""

    from agentcad import repair_safety

    cases = list(repair_safety.SAFETY_CASES)

    def probe_builder(service, context):  # pragma: no cover - identity only, never run
        raise AssertionError("the identity must not depend on running the case")

    cases.append(repair_safety.SafetyCase("d13_probe", "probe", probe_builder))
    monkeypatch.setattr(repair_safety, "SAFETY_CASES", tuple(cases))
    assert core_corpus_digest() != V3_DIGEST


def test_archived_projection_declares_what_it_cannot_identify() -> None:
    """v2's declarative half survives; its implementation half is declared unavailable."""

    projection = core_corpus_projection("2")
    assert projection["corpus_version"] == "2" and projection["spec_version"] == "2"
    assert len(projection["operator_catalogue"]) == 19
    assert len(projection["case_universe"]) == 72
    assert {
        row["producer_definition_identity"] for row in projection["operator_catalogue"]
    } == {ARCHIVED_DEFINITION_IDENTITY}
    with pytest.raises(ValueError):
        core_corpus_projection("9")


def _with_operator(monkeypatch, key: str, operator) -> str:
    monkeypatch.setitem(MUTATIONS, key, operator)
    return core_corpus_digest()


def test_moving_one_operator_id_moves_the_identity(monkeypatch) -> None:
    target = MUTATIONS["f1_line_name_missing"]
    assert _with_operator(
        monkeypatch,
        "f1_line_name_missing",
        replace(target, operator_id="f1_line_name_renamed"),
    ) != V3_DIGEST


def test_exchanging_two_operator_ids_moves_the_identity(monkeypatch) -> None:
    """The id *set* is unchanged here, and only the assignment of producers to cases is."""

    first = MUTATIONS["f5_node_overlap"]
    second = MUTATIONS["f5_symbol_out_of_bounds"]
    monkeypatch.setitem(
        MUTATIONS, "f5_node_overlap", replace(first, operator_id=second.operator_id)
    )
    monkeypatch.setitem(
        MUTATIONS, "f5_symbol_out_of_bounds", replace(second, operator_id=first.operator_id)
    )
    assert core_corpus_digest() != V3_DIGEST


def test_moving_a_defect_code_moves_the_identity(monkeypatch) -> None:
    target = MUTATIONS["f2_endpoint_dangling"]
    assert _with_operator(
        monkeypatch, target.operator_id, replace(target, target_code="PROBE_CODE")
    ) != V3_DIGEST


def test_moving_a_base_variant_moves_the_identity(monkeypatch) -> None:
    target = MUTATIONS["f4_micro_segment"]
    assert _with_operator(
        monkeypatch, target.operator_id, replace(target, base_variant="three_valves")
    ) != V3_DIGEST


def test_moving_a_declared_write_policy_moves_the_identity(monkeypatch) -> None:
    target = MUTATIONS["f3_delete_middle_detach"]
    assert _with_operator(
        monkeypatch, target.operator_id, replace(target, max_deleted_ids=2)
    ) != V3_DIGEST


def test_replacing_a_producer_moves_the_identity(monkeypatch) -> None:
    """The half a declarative table cannot express: different code, same declaration."""

    def probe_producer(service, base, rng):  # pragma: no cover - identity only, never run
        raise AssertionError("the identity must not depend on running the producer")

    target = MUTATIONS["f4_unnecessary_bend"]
    assert _with_operator(
        monkeypatch, target.operator_id, replace(target, apply=probe_producer)
    ) != V3_DIGEST


def test_adding_a_case_moves_the_identity(monkeypatch) -> None:
    monkeypatch.setattr(
        benchmark, "ACCEPTANCE_CASES_PER_FAMILY", ACCEPTANCE_CASES_PER_FAMILY + 1
    )
    assert core_corpus_digest() != V3_DIGEST


def test_adding_an_operator_moves_the_identity(monkeypatch) -> None:
    target = MUTATIONS["f1_line_tag_missing"]
    assert _with_operator(
        monkeypatch,
        "f1_probe_operator",
        replace(target, operator_id="f1_probe_operator", target_code="PROBE_CODE"),
    ) != V3_DIGEST


def test_moving_the_seed_derivation_rule_moves_the_identity(monkeypatch) -> None:
    monkeypatch.setitem(benchmark.CASE_DERIVATION, "seed_digest", "sha256(material)[:8]")
    assert core_corpus_digest() != V3_DIGEST


# -- what the identity must not see ------------------------------------------- #


def test_interpreter_provenance_does_not_move_the_identity(monkeypatch) -> None:
    """Bytecode, and the spec fingerprint the seeds are derived from, are not the corpus."""

    monkeypatch.setattr(benchmark, "generator_fingerprint", lambda: "0" * 64)
    assert core_corpus_digest() == V3_DIGEST
    # ... while the fingerprint, which is provenance, moves -- that is the whole distinction.
    assert core_corpus_fingerprint() != core_corpus_manifest()["generator_fingerprint"]
    assert core_corpus_manifest()["generator_fingerprint"] == "0" * 64

    monkeypatch.setattr(
        benchmark,
        "spec_fingerprint",
        lambda version=None: spec_fingerprint(version) if version else "0" * 64,
    )
    assert core_corpus_digest() == V3_DIGEST


def test_definition_identities_are_recomputable_from_the_source_file() -> None:
    """The half that used to be bytecode can be checked by anyone, from the files alone."""

    package = Path(__file__).resolve().parents[1] / "agentcad"
    recomputed: dict[str, str] = {}
    for name in ("repair_benchmark.py", "repair_safety.py"):
        rows = module_definition_identities((package / name).read_text(encoding="utf-8"))
        recomputed.update({f"{name[:-3]}:{qualname}": value for qualname, value in rows.items()})
    trace = corpus_definition_ast_projection()
    assert len(trace) > 20
    assert set(trace) <= set(recomputed)
    assert all(recomputed[qualname] == value for qualname, value in trace.items())
    # Both modules are represented, which is the crossing that used to be invisible: a safety
    # builder's helpers live beside it, not in the benchmark module.
    assert {key.split(":", 1)[0] for key in trace} == {"repair_benchmark", "repair_safety"}


def test_the_identity_reads_names_off_the_source_not_off_the_compiler() -> None:
    """The bug this rule exists for: 3.12 inlined comprehensions and moved a name's classification.

    ``inspect.getclosurevars`` reported ``__name__`` as a resolved global on 3.11 and not on 3.12 for
    the same function, so a definition identity built from it was interpreter-dependent after all --
    invisible to the AST check, and visible only because a second interpreter ran the pinned digest.
    Names now come from the AST, where both interpreters read the same thing.
    """

    names = _referenced_names(
        "def f(items):\n"
        "    table = [row for row in items if row]\n"
        "    return __name__, table, LIMIT\n"
    )
    assert {"__name__", "items", "row", "table", "LIMIT"} <= set(names)
    # Sorted and deduplicated, so the material cannot depend on dict ordering either.
    assert names == sorted(set(names))


def test_canonical_form_ignores_a_field_that_carries_nothing() -> None:
    """``ast.dump`` cannot do this job: 3.12 gave ``FunctionDef`` a ``type_params`` field.

    Measured on this repository: the same ``_patch`` hashes ``930398d67e9220f4`` under ``ast.dump`` on
    3.11 and ``a182c517faab2e95`` on 3.12. The canonical projection drops ``None`` and empty fields,
    so a field a later release adds contributes nothing until it holds something -- which is the
    interpreter-independence the identity needs. The measurement is repeated here in its cheapest
    form: the same definition, once with empty containers and once with the ``None`` an absent field
    reads as, has to project to the same identity while ``ast.dump`` does not.
    """

    node = ast.parse("def f():\n    return 1\n").body[0]
    fields = type(node)._fields
    as_none = type(node)(
        **{
            name: None
            if isinstance(getattr(node, name), list) and not getattr(node, name)
            else getattr(node, name)
            for name in fields
        }
    )
    assert ast_identity(as_none) == ast_identity(node)
    assert ast.dump(as_none, include_attributes=False) != ast.dump(node, include_attributes=False)
