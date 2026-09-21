"""The core corpus identity: what the corpus *is*, on any interpreter (remote A5).

``core_corpus_fingerprint`` hashes the whole corpus manifest, ``generator_fingerprint`` included,
and that one digests CPython bytecode: the same frozen corpus published ``c85995d2…`` on 3.12 and
``82b37b04…`` on 3.11. A frozen number that moves with the interpreter can only mean "frozen on
this machine", which is the opposite of what the word is for.

So the corpus gets a second, narrower identity, and this file is its contract:

* ``core_corpus_digest`` is a pure function of the corpus definition — the layout, the case counts,
  the operator catalogue it supports — and of nothing that describes the world it runs in
  (``spec_fingerprint``) or the interpreter it runs on (``generator_fingerprint``);
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

import hashlib
import json
from pathlib import Path

import pytest

from agentcad.repair_benchmark import (
    CORE_CORPUS_VERSION,
    CORPUS_IDENTITY_EXCLUDED_KEYS,
    SPEC_FINGERPRINT_V2,
    core_corpus_digest,
    core_corpus_digest_manifest,
    core_corpus_fingerprint,
    core_corpus_manifest,
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

#: The corpus identity of the frozen v3 corpus: pure data, identical on CPython 3.11 and 3.12.
V3_DIGEST = "a60e07f11d55769e235f05da7f975f7a1b4617fc73b5f49bc7d4e12b16a1bd49"

#: The archived v2 corpus's digest. Derived under A5, so it is an *archival* identity: v2 never
#: published this number, and saying otherwise would be inventing a field history.
V2_DIGEST = "acb4a3bde4ed0b313588d344f717c2c2fad68ffbc782b3b1d3e710b363102900"

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


def test_digest_ignores_the_interpreter_and_the_spec_fingerprint() -> None:
    """The identity must survive exactly the two things that move under it."""

    assert set(CORPUS_IDENTITY_EXCLUDED_KEYS) == {"spec_fingerprint", "generator_fingerprint"}
    manifest = core_corpus_manifest()
    assert not (set(core_corpus_digest_manifest()) & set(CORPUS_IDENTITY_EXCLUDED_KEYS))

    # The two published values that differ between 3.11 and 3.12, and between spec versions.
    swapped = {
        **manifest,
        "generator_fingerprint": "43979207ca8f9b608996277f63cf993cc33175c6e5cb79dd2ae71ee4ba60857c",
        "spec_fingerprint": "0" * 64,
    }
    assert _canonical_hash({k: v for k, v in swapped.items() if k not in CORPUS_IDENTITY_EXCLUDED_KEYS}) == V3_DIGEST
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
