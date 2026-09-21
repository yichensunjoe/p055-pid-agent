"""The retired extension corpus stays auditable, without resurrecting its generator (remote A5).

``m5-coverage-extension/v1`` was retired when its four representable codes were promoted into the
frozen corpus. Retiring the *runner* is not the same as losing the *evidence*: the remote's retention
rule is that a future reviewer must still be able to check which four cases it held, what manifest
its published ``coverage_corpus_digest`` covers, and that the published report was not tampered with
afterwards.

So this file pins four numbers against the frozen artifact itself -- one file, read as bytes and as
JSON, never regenerated from today's generator:

* the file's own sha256, which is the tamper check;
* the historical ``coverage_corpus_digest``, recomputed from the manifest the file contains;
* the historical ``coverage_report_hash``, recomputed from the report the file contains;
* the historical published ``corpus_fingerprint``, which is **interpreter-specific** and is pinned
  as exactly that: a recorded published value, not the stable corpus identity. Anybody who moves it
  into the stable role has changed what it means.

The values also have to agree with ``SUPERSEDED_EXTENSION_IDENTITIES``, which is an index into this
evidence and must never become the evidence itself.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agentcad.repair_coverage_ledger import SUPERSEDED_EXTENSION_IDENTITIES
from agentcad.repair_models import REPAIR_VOLATILE_FIELDS

#: The published evidence, byte for byte as it was handed to the reviewer.
FROZEN_EVIDENCE = Path(__file__).resolve().parents[2] / "reports" / "m5" / "repair-coverage-extension.json"

FILE_SHA256 = "663461da0f6203efbe034f43a4a88bdf59897a77f1ef5b2967398825ca95b5fd"
COVERAGE_CORPUS_DIGEST = "3dc8ca1ade6c5adbe5f8c3a5fab8709eff3ffdeb4f904a4da4fd8f7ae6b19969"
COVERAGE_REPORT_HASH = "f56c2c5098119a0b753660ba6a33a0fb7bcc03c24a545f0fd1849f6e44fe0931"
#: Historical, interpreter-specific, published on CPython 3.12. Not the stable corpus identity.
PUBLISHED_CORPUS_FINGERPRINT = "c198eb77c73157b25eba537fd3fef4264110897f23325d30b37c2bad9d6095e4"

#: The manifest keys that described the machine rather than the corpus, which is why the retired
#: track published a digest beside its fingerprint.
ENVIRONMENT_DERIVED_KEYS = ("spec_fingerprint", "generator_fingerprint")

CASE_IDS = (
    "m5-coverage-extension:F1:00:ext_duplicate_label",
    "m5-coverage-extension:F2:01:ext_port_direction_mismatch",
    "m5-coverage-extension:F4:02:ext_unbridged_crossing",
    "m5-coverage-extension:F5:03:ext_annotation_overlap",
)


def _frozen_bytes() -> bytes:
    return FROZEN_EVIDENCE.read_bytes()


def _published_payload_hash(payload: object) -> str:
    """The canonical hash rule the retired track used, applied to the frozen payload.

    Reimplemented here rather than imported, because the module that produced these numbers is gone
    on purpose: the rule outlives the runner, and an audit that needs the deleted code to run is not
    an audit.
    """

    def strip(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: strip(item)
                for key, item in sorted(value.items())
                if key not in REPAIR_VOLATILE_FIELDS
            }
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    text = json.dumps(strip(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _report() -> dict:
    return json.loads(_frozen_bytes())


def test_frozen_extension_evidence_is_byte_identical() -> None:
    """Pin one: the published report has not been touched since it was published."""

    assert hashlib.sha256(_frozen_bytes()).hexdigest() == FILE_SHA256


def test_published_coverage_corpus_digest_still_recomputes() -> None:
    """Pin two: the digest's manifest is the ``corpus`` block the frozen report carries."""

    corpus = _report()["corpus"]
    digest_input = {k: v for k, v in corpus.items() if k not in ENVIRONMENT_DERIVED_KEYS}
    assert _published_payload_hash(digest_input) == COVERAGE_CORPUS_DIGEST
    assert set(ENVIRONMENT_DERIVED_KEYS) <= set(corpus)


def test_published_report_hash_still_recomputes() -> None:
    """Pin three: the report hash covers the report, minus itself."""

    report = _report()
    body = {key: value for key, value in report.items() if key != "report_hash"}
    assert _published_payload_hash(body) == COVERAGE_REPORT_HASH
    assert report["report_hash"] == COVERAGE_REPORT_HASH


def test_published_corpus_fingerprint_is_recorded_as_interpreter_specific() -> None:
    """Pin four: the full fingerprint is reproducible *as the historical 3.12 value*, and no more."""

    corpus = _report()
    assert _published_payload_hash(corpus["corpus"]) == PUBLISHED_CORPUS_FINGERPRINT
    assert corpus["corpus_fingerprint"] == PUBLISHED_CORPUS_FINGERPRINT
    identities = SUPERSEDED_EXTENSION_IDENTITIES
    assert identities["coverage_fingerprint_by_interpreter"]["3.12"] == PUBLISHED_CORPUS_FINGERPRINT
    # Two interpreters, two numbers for one corpus: that asymmetry is the reason the digest exists.
    published = set(identities["coverage_fingerprint_by_interpreter"].values())
    assert len(published) == 2
    assert identities["coverage_corpus_digest"] == COVERAGE_CORPUS_DIGEST
    assert identities["coverage_report_hash"] == COVERAGE_REPORT_HASH


def test_the_four_cases_and_their_dispositions_are_still_readable() -> None:
    """Which four cases the retired corpus held is a question the frozen file still answers."""

    report = _report()
    rows = report["rows"]
    assert [row["case"]["case_id"] for row in rows] == list(CASE_IDS)
    assert {row["code"] for row in rows} == {
        "DUPLICATE_LABEL",
        "PORT_DIRECTION_MISMATCH",
        "UNBRIDGED_CROSSING",
        "ANNOTATION_OVERLAP",
    }
    assert all(row["status"] == "covered" for row in rows)
    assert report["corpus"]["case_count"] == len(rows) == 4
    # The unreachable half of the retired track's claim travels with the same file.
    assert {row["code"] for row in report["corpus"]["unreachable_codes"]} == {
        "SYMBOL_DEFINITION_MISSING",
        "CONNECTOR_ENDPOINT_PORT_MISSING",
        "CONNECTOR_ENDPOINT_POINT_MISMATCH",
    }
