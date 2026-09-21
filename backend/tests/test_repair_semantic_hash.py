"""The repair semantic hash: what may move it, and what may never move it (remote ruling ⑥-2).

Two runs of one candidate used to publish two different ``benchmark_result_hash`` values while every
semantic field matched, because ``canonical_digest`` passes ``exclude`` to pydantic and pydantic drops
*top-level* keys only: the volatile facts live in ``cases[i]``, so 724 nested leaves reached the hash.

The fix is a second, explicit identity — ``benchmark_semantic_hash`` under
``REPAIR_SEMANTIC_HASH_VERSION`` — and this file is its contract:

* every field the contract calls volatile is mutated **at whatever depth it appears**, and the hash
  must not move (including the interpreter-bound ``generator_fingerprint``);
* every field that *is* the result is mutated, and the hash must move, including case order;
* the legacy ``benchmark_result_hash`` keeps its old rule, so a payload published before this change
  still recomputes to the value it was published with;
* the M4 layer's ``canonical_digest`` is untouched, because it is somebody else's provenance rule.
"""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import BaseModel

from agentcad.repair_evidence import (
    RepairCaseRecord,
    build_benchmark_result,
    verify_benchmark_result,
)
from agentcad.repair_models import (
    REPAIR_LEGACY_HASH_EXCLUDES,
    REPAIR_SEMANTIC_EXCLUDED_FIELDS,
    REPAIR_SEMANTIC_HASH_VERSION,
    canonical_repair_result_payload,
    repair_digest,
    repair_semantic_digest,
)
from agentcad.validation_models import canonical_digest

GENERATOR_FINGERPRINT = "13cc1254795b8fa6bcc13c31a8d0d96f85befbcc9c7247023e69761ef2d1eadc"


def _case(**overrides) -> RepairCaseRecord:
    base = dict(
        case_id="unit:case",
        family="F1",
        operator_id="unit_operator",
        suite="acceptance",
        seed=11,
        candidate_sha="candidate-sha",
        document_id="doc-1",
        base_revision=1,
        base_content_hash="base-content",
        pre_validation_hash="pre-validation",
        target_code="DEVICE_TAG_MISSING",
        target_validator_id="engineering-report",
        target_element_ids=["v1"],
        scope_fingerprint="scope",
        protected_pre={"drawing_projection": "d", "engineering_projection": "e"},
        attempts=1,
        attempt_plan_hashes=["plan-1"],
        attempt_failure_codes=[""],
        attempt_shadow_validation_hashes=["shadow-1"],
        selected_plan_hash="plan-1",
        applied=True,
        result_revision=2,
        transaction_hash="tx-1",
        audit_record_id="audit-1",
        post_validation_hash="post-validation",
        protected_post={"drawing_projection": "d", "engineering_projection": "e"},
        undo_restored_base=True,
        redo_restored_result=True,
        repair_hash="repair-1",
        classification="success",
        write_count=1,
    )
    return RepairCaseRecord(**{**base, **overrides})


def _result(*, cases: list[RepairCaseRecord] | None = None):
    return build_benchmark_result(
        suite="acceptance",
        track="deterministic",
        spec_version="2",
        spec_fingerprint="spec-fingerprint",
        generator_fingerprint=GENERATOR_FINGERPRINT,
        oracle_version="1",
        candidate_sha="candidate-sha",
        safety_total=2,
        safety_passed=2,
        cases=cases if cases is not None else [_case(case_id="unit:case-1"), _case(case_id="unit:case-2")],
    )


def _mutate_volatile_everywhere(payload):
    """Change every volatile field the contract declares, at every depth it appears.

    Returns the number of leaves touched, so a test can fail when the walk stops finding any: a
    determinism test that mutates nothing is a test that proves nothing.
    """

    changed = 0

    def walk(value):
        nonlocal changed
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if key in REPAIR_SEMANTIC_EXCLUDED_FIELDS:
                    out[key] = _different(item)
                    changed += len(item) if isinstance(item, (dict, list)) else 1
                else:
                    out[key] = walk(item)
            return out
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    def _different(item):
        if isinstance(item, dict):
            return {key: _different(value) for key, value in item.items()} or {"changed": True}
        if isinstance(item, list):
            return [_different(value) for value in item] or ["changed"]
        if isinstance(item, bool):
            return not item
        if isinstance(item, int):
            return item + 977
        return f"{item}-changed"

    return walk(payload), changed


# -- what must never move the identity --------------------------------------- #


def test_runtime_facts_do_not_move_the_semantic_hash() -> None:
    """Latency, timings, ids, runtime validation hashes and the bytecode digest are not the result."""

    result = _result()
    payload = json.loads(result.model_dump_json(by_alias=True))
    mutated, changed = _mutate_volatile_everywhere(payload)

    # The walk has to actually reach the nested records: the whole point of the fix is depth.
    assert changed > 20
    assert mutated != payload
    assert repair_semantic_digest(mutated) == result.benchmark_semantic_hash


def test_volatile_fields_are_stripped_at_every_depth() -> None:
    nested = {
        "cases": [
            {"latency_ms": 5, "keep": {"document_id": "x", "attempts": [{"timings_ms": 9, "ok": True}]}}
        ],
        "timings_ms": {"suite_ms": 1},
    }
    assert canonical_repair_result_payload(nested) == {"cases": [{"keep": {"attempts": [{"ok": True}]}}]}


def test_the_bytecode_fingerprint_is_not_part_of_the_result_identity() -> None:
    """The same candidate on CPython 3.11 and 3.12 must be the same result, not two results."""

    local = _result()
    elsewhere = local.model_copy(update={"generator_fingerprint": "fda18c2fd1c7e53eb0d8ec7e76094ca127c85beb20294e868c2bedc38b38b67a"})
    assert elsewhere.generator_fingerprint != local.generator_fingerprint
    assert repair_semantic_digest(elsewhere) == local.benchmark_semantic_hash


# -- what must move the identity -------------------------------------------- #


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda payload: payload["cases"][0].update({"classification": "failure"}), id="classification"),
        pytest.param(lambda payload: payload["cases"][0].update({"failure_code": "compile_failed"}), id="failure_code"),
        pytest.param(lambda payload: payload["cases"][0].update({"applied": False}), id="applied"),
        pytest.param(lambda payload: payload["cases"][0].update({"selected_plan_hash": "plan-2"}), id="plan"),
        pytest.param(lambda payload: payload["cases"][0].update({"undo_restored_base": False}), id="undo"),
        pytest.param(lambda payload: payload["cases"][0].update({"write_count": 2}), id="write_count"),
        pytest.param(lambda payload: payload["cases"][0].update({"attempts": 3}), id="attempts"),
        pytest.param(
            lambda payload: payload["cases"][0]["attempt_failure_codes"].append("locality_violation"),
            id="attempt_failure_codes",
        ),
        pytest.param(
            lambda payload: payload["cases"][0]["target_element_ids"].append("v2"),
            id="target_element_ids",
        ),
        pytest.param(
            lambda payload: payload["cases"][0]["protected_post"].update({"drawing_projection": "different"}),
            id="protected_post",
        ),
        pytest.param(lambda payload: payload.update({"gates": {**payload["gates"], "s5_overall": False}}), id="gate"),
        pytest.param(lambda payload: payload["counts"].update({"success": 1}), id="counts"),
        pytest.param(lambda payload: payload.update({"candidate_sha": "other-sha"}), id="candidate_sha"),
        pytest.param(lambda payload: payload.update({"spec_fingerprint": "other-spec"}), id="spec_fingerprint"),
        pytest.param(lambda payload: payload.update({"safety_passed": 1}), id="safety"),
    ],
)
def test_semantic_fields_move_the_semantic_hash(mutation) -> None:
    """Every claim the payload makes is bound: change one and the identity changes with it."""

    result = _result()
    payload = json.loads(result.model_dump_json(by_alias=True))
    mutation(payload)
    assert repair_semantic_digest(payload) != result.benchmark_semantic_hash


def test_the_processing_digest_of_one_record_is_not_a_result_claim() -> None:
    """``repair_hash`` is declared volatile (it is itself a digest), so it may not move the identity."""

    assert "repair_hash" in REPAIR_SEMANTIC_EXCLUDED_FIELDS
    result = _result()
    payload = json.loads(result.model_dump_json(by_alias=True))
    payload["cases"][0]["repair_hash"] = "something-else"
    assert repair_semantic_digest(payload) == result.benchmark_semantic_hash


def test_case_order_is_protocol_semantics() -> None:
    """The case list is a sequence, not a set: the two orders are two different published results.

    A hash that ignored order would let a run be reordered out of the evidence it proves, so order
    stays in. (The alternative the remote allows -- order is not semantic, therefore the spec sorts
    it -- is not this contract: the runner derives the sequence from the corpus.)
    """

    result = _result()
    reordered = _result(
        cases=[_case(case_id="unit:case-2"), _case(case_id="unit:case-1")]
    )
    assert repair_semantic_digest(reordered) != result.benchmark_semantic_hash


# -- the published payload has to be enough to recompute it ------------------ #


def test_semantic_hash_is_recomputable_from_the_published_payload() -> None:
    result = _result()
    published = json.loads(result.model_dump_json(by_alias=True))
    assert published["benchmark_semantic_hash"] == result.benchmark_semantic_hash
    assert published["semantic_hash_version"] == REPAIR_SEMANTIC_HASH_VERSION
    assert repair_semantic_digest(published) == result.benchmark_semantic_hash


def test_the_contract_version_is_published_but_not_hashed() -> None:
    """The name of the contract is metadata; a payload from before the field may still be compared.

    Hashing the version would break exactly the comparison a reviewer most wants — the payload
    published before the field existed against the one published after — so the version is published
    and checked separately from the digest.
    """

    assert "semantic_hash_version" in REPAIR_SEMANTIC_EXCLUDED_FIELDS
    result = _result()
    payload = json.loads(result.model_dump_json(by_alias=True))
    payload["semantic_hash_version"] = "99"
    payload.pop("benchmark_semantic_hash")
    assert repair_semantic_digest(payload) == repair_semantic_digest(
        {key: value for key, value in payload.items() if key != "semantic_hash_version"}
    )


def test_an_unknown_contract_version_is_flagged_rather_than_silently_compared() -> None:
    result = _result()
    payload = json.loads(result.model_dump_json(by_alias=True))
    payload["semantic_hash_version"] = "99"
    assert "semantic_hash_version_unknown" in verify_benchmark_result(payload).codes()


def test_a_payload_from_before_the_semantic_contract_is_not_flagged() -> None:
    """The published v2 evidence carries no semantic hash; it must not read as a mismatch."""

    result = _result()
    payload = json.loads(result.model_dump_json(by_alias=True))
    payload.pop("benchmark_semantic_hash")
    payload.pop("semantic_hash_version")
    codes = verify_benchmark_result(payload).codes()
    assert "semantic_hash_mismatch" not in codes
    assert "semantic_hash_version_unknown" not in codes
    assert "semantic_hash_missing" not in codes


def test_verification_reports_a_tampered_semantic_payload() -> None:
    """A reviewer checking a hand-edited payload must see the semantic hash disagree, not just the legacy one."""

    result = _result()
    payload = json.loads(result.model_dump_json(by_alias=True))
    assert "semantic_hash_mismatch" not in verify_benchmark_result(payload).codes()
    payload["cases"][0]["classification"] = "failure"
    report = verify_benchmark_result(payload)
    assert not report.ok
    assert "semantic_hash_mismatch" in report.codes()
    # The tamper is visible as a mismatched identity, not only as a moved count.
    assert report.recomputed_semantic_hash != payload["benchmark_semantic_hash"]


# -- the two things this change must not have touched ------------------------ #


def test_legacy_hash_rule_is_frozen() -> None:
    """The legacy field keeps its old rule, so payloads published before the change still verify.

    Recomputing it ignores the fields that exist only for the semantic hash, which is what keeps the
    already-published acceptance evidence (three runs, three legacy values) recomputable to the value
    each run published.
    """

    result = _result()
    blank = result.model_copy(update={"benchmark_result_hash": ""})
    assert result.benchmark_result_hash == repair_digest(blank, exclude=REPAIR_LEGACY_HASH_EXCLUDES)

    # Setting the semantic-only fields to something else must not move the legacy value.
    relabelled = blank.model_copy(
        update={"benchmark_semantic_hash": "0" * 64, "semantic_hash_version": "99"}
    )
    assert repair_digest(relabelled, exclude=REPAIR_LEGACY_HASH_EXCLUDES) == result.benchmark_result_hash


def test_repair_semantic_hash_does_not_change_m4_canonical_hashing() -> None:
    """M4 owns ``canonical_digest``; the repair layer adds a rule instead of editing that one."""

    class Inner(BaseModel):
        latency_ms: int = 0
        note: str = "x"

    class Outer(BaseModel):
        cases: list[Inner] = []
        latency_ms: int = 0

    model = Outer(cases=[Inner(latency_ms=7)], latency_ms=9)
    # Unchanged behaviour, top level only: the M4 provenance rule and every golden fixture with it.
    assert canonical_digest(model, exclude={"latency_ms"}) == canonical_digest(
        Outer(cases=[Inner(latency_ms=7)], latency_ms=1), exclude={"latency_ms"}
    )
    # The repair rule is the deeper one, and it is opt-in.
    assert canonical_repair_result_payload(model) == {"cases": [{"note": "x"}]}


def test_semantic_hash_matches_the_copy_that_carries_no_runtime_facts() -> None:
    """Two records that differ only in runtime facts are the same record, hash included."""

    one = _result()
    other = _result(
        cases=[
            _case(case_id="unit:case-1", document_id="doc-9", wall_clock_ms=99),
            _case(case_id="unit:case-2", audit_record_id="audit-9", post_validation_hash="post-9"),
        ]
    )
    assert copy.deepcopy(one.benchmark_result_hash) != other.benchmark_result_hash
    assert one.benchmark_semantic_hash == other.benchmark_semantic_hash
