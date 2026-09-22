"""Verify the M5 release-readiness record instead of trusting it.

A release summary is a claim, and a claim that nothing checks is how a track quietly stops being
true. So this checks three things about ``reports/m5-release-readiness/release-readiness.json``:

1. **the record is self-consistent** -- every track carries a known status, its evidence pointers
   all resolve, and the markdown next to it mentions the same anchors, commits, runs, digests,
   status labels and evidence paths (two documents that disagree are worse than one);
2. **its identities are recomputed, not quoted** -- spec/corpus/oracle/semantic-hash versions, the
   v3 and archived-v2 corpus digests, and the case/operator counts are derived from the code and
   compared with both the record and the frozen closeout evidence;
3. **the blocked track is blocked** -- the real-model qualification must be recorded as externally
   blocked with ``qualified: false``, and the readiness JSON it points at must still say
   ``awaiting_real_model_qualification``. "Prerequisite missing" is not a pass, and this script is
   where writing it as one would be caught.

It also refuses to run if the frozen evidence it references has been modified in this checkout.

    .venv/bin/python scripts/m5_release_readiness.py            # exit 0 = the record holds

The record path may be given as an argument, which is how the suite checks that a tampered record
is rejected instead of only checking that the real one passes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agentcad.repair_benchmark import (  # noqa: E402
    BENCHMARK_SPEC_VERSION,
    CORE_CORPUS_VERSION,
    MUTATIONS,
    SUCCESS_ORACLE_VERSION,
    core_corpus_digest,
    generate_suite,
    spec_fingerprint,
)
from agentcad.repair_models import REPAIR_SEMANTIC_HASH_VERSION  # noqa: E402

RECORD = ROOT / "reports" / "m5-release-readiness" / "release-readiness.json"
DOCUMENT = ROOT / "reports" / "m5-release-readiness" / "release-readiness.md"
CLOSEOUT_IDENTITY = ROOT / "reports" / "m5-closeout" / "corpus-identity.json"
FROZEN_DIRECTORIES = ("reports/m5", "reports/m5-promotion", "reports/m5-closeout")


def _fail(problems: list[str], message: str) -> None:
    problems.append(message)


def _git_clean(paths: tuple[str, ...]) -> tuple[bool, str]:
    """Whether the given paths are unmodified in this checkout, as far as git can tell."""

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", *paths],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:  # pragma: no cover - git absent
        return True, f"git unavailable ({error}); frozen-evidence check skipped"
    if result.returncode != 0:
        return True, "not a git checkout; frozen-evidence check skipped"
    if result.stdout.strip():
        return False, f"frozen evidence modified: {result.stdout.strip().splitlines()}"
    return True, f"{', '.join(paths)} unmodified"


def _check_record_structure(record: dict[str, Any], problems: list[str]) -> None:
    if record.get("schema") != "pid-agent.m5-release-readiness":
        _fail(problems, f"unexpected schema: {record.get('schema')!r}")
    if record.get("version") != 1:
        _fail(problems, f"unexpected version: {record.get('version')!r}")
    vocabulary = set(record.get("status_vocabulary", {}))
    tracks = record.get("tracks", [])
    if not tracks:
        _fail(problems, "the record lists no tracks")
    seen: set[str] = set()
    for track in tracks:
        identifier = track.get("id", "<missing id>")
        if identifier in seen:
            _fail(problems, f"duplicate track id: {identifier}")
        seen.add(identifier)
        if track.get("status") not in vocabulary:
            _fail(problems, f"{identifier}: unknown status {track.get('status')!r}")
        if not track.get("status_label"):
            _fail(problems, f"{identifier}: no status label to show a reader")
        if not track.get("reopen_triggers"):
            _fail(problems, f"{identifier}: a closed track must name how it reopens")
        if not track.get("evidence"):
            _fail(problems, f"{identifier}: no evidence pointer")


def _check_evidence_resolves(record: dict[str, Any], problems: list[str]) -> int:
    checked = 0
    for path in record.get("frozen_evidence_directories_untouched", []):
        if not (ROOT / path).is_dir():
            _fail(problems, f"frozen evidence directory missing: {path}")
    for track in record.get("tracks", []):
        for pointer in track.get("evidence", []):
            checked += 1
            if not (ROOT / pointer).is_file():
                _fail(problems, f"{track.get('id')}: evidence pointer does not resolve: {pointer}")
    return checked


def _check_document(record: dict[str, Any], problems: list[str]) -> None:
    if not DOCUMENT.is_file():
        _fail(problems, f"the human record is missing: {DOCUMENT.name}")
        return
    body = DOCUMENT.read_text(encoding="utf-8")

    def required(value: Any, label: str = "") -> None:
        if isinstance(value, str) and value and value not in body:
            described = f"{label} = {value!r}" if label else repr(value)
            _fail(problems, f"the record says {described} but the document does not mention it")

    required(record.get("release_anchor"))
    required(record.get("anchor_ci_run"))
    required(record.get("overall_status"))
    for key, value in record.get("identities", {}).items():
        if isinstance(value, str):
            required(value, key)
        elif isinstance(value, dict):
            for nested_key, nested in value.items():
                required(nested, f"{key}.{nested_key}")
    for track in record.get("tracks", []):
        label = track.get("id", "<track>")
        required(track.get("status_label"), label)
        required(track.get("anchor_sha"), label)
        for value in list(track.get("commits", [])) + list(track.get("ci_runs", {})) + list(
            track.get("visual_runs", {})
        ):
            required(value, label)
        for pointer in track.get("evidence", []):
            required(pointer, label)


def _check_identities(record: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    identities = record.get("identities", {})
    archived = identities.get("archived", {})
    closeout = json.loads(CLOSEOUT_IDENTITY.read_text(encoding="utf-8"))["corpus_identity"]

    computed = {
        "spec_version": BENCHMARK_SPEC_VERSION,
        "spec_fingerprint": spec_fingerprint(),
        "corpus_version": CORE_CORPUS_VERSION,
        "core_corpus_digest": core_corpus_digest("3"),
        "oracle_version": SUCCESS_ORACLE_VERSION,
        "semantic_hash_version": REPAIR_SEMANTIC_HASH_VERSION,
        "archived_spec_fingerprint": spec_fingerprint("2"),
        "archived_core_corpus_digest": core_corpus_digest("2"),
        "acceptance_cases": len(generate_suite(candidate_sha="readiness", suite="acceptance")),
        "dev_cases": len(generate_suite(candidate_sha="readiness", suite="dev")),
        "operators": len(MUTATIONS),
        # Two different counts live next to each other and mean different things: operators that
        # claim a code (the closeout evidence's "codes with a producer") and the codes themselves,
        # which is the smaller number because a family can carry more than one operator per code.
        "operators_with_a_target_code": len(
            [mutation for mutation in MUTATIONS.values() if mutation.target_code]
        ),
        "distinct_target_codes": len(
            {mutation.target_code for mutation in MUTATIONS.values() if mutation.target_code}
        ),
    }
    expected = {
        "spec_version": identities.get("spec_version"),
        "spec_fingerprint": identities.get("spec_fingerprint"),
        "corpus_version": identities.get("corpus_version"),
        "core_corpus_digest": identities.get("core_corpus_digest"),
        "oracle_version": identities.get("oracle_version"),
        "semantic_hash_version": identities.get("semantic_hash_version"),
        "archived_spec_fingerprint": archived.get("spec_fingerprint"),
        "archived_core_corpus_digest": archived.get("core_corpus_digest"),
        "acceptance_cases": identities.get("acceptance_cases"),
        "dev_cases": identities.get("dev_cases"),
        "operators": identities.get("operators"),
        "operators_with_a_target_code": identities.get("operators_with_a_target_code"),
        "distinct_target_codes": identities.get("distinct_target_codes"),
    }
    for name, value in expected.items():
        if value != computed[name]:
            _fail(
                problems,
                f"identity drift: {name} recorded {value!r} but this checkout computes {computed[name]!r}",
            )
    # The frozen closeout evidence has to agree, or the record quotes a number the corpus no longer has.
    if closeout["3"]["digest"] != computed["core_corpus_digest"]:
        _fail(problems, "the frozen closeout evidence disagrees with the recomputed v3 digest")
    if closeout["2"]["digest"] != computed["archived_core_corpus_digest"]:
        _fail(problems, "the frozen closeout evidence disagrees with the recomputed archived v2 digest")
    if closeout["spec_fingerprint"] != computed["spec_fingerprint"]:
        _fail(problems, "the frozen closeout evidence disagrees with the recomputed spec fingerprint")
    if archived.get("spec_fingerprint") != computed["archived_spec_fingerprint"]:
        _fail(problems, "the archived v2 spec fingerprint is not the one this checkout recomputes")
    return computed


def _check_history_is_historical(problems: list[str]) -> None:
    """The v2 acceptance evidence in reports/m5 must stay branded v2, not be mistaken for current."""

    v2_path = ROOT / "reports" / "m5" / "repair-benchmark-acceptance.json"
    v3_path = ROOT / "reports" / "m5-promotion" / "acceptance-v3.json"
    for path, expected in ((v2_path, "2"), (v3_path, "3")):
        if not path.is_file():
            _fail(problems, f"acceptance evidence missing: {path.relative_to(ROOT)}")
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("spec_version") != expected:
            _fail(
                problems,
                f"{path.relative_to(ROOT)} says spec_version {payload.get('spec_version')!r}, expected {expected!r}",
            )


def _check_blocked_track(record: dict[str, Any], problems: list[str]) -> None:
    blocked = [track for track in record.get("tracks", []) if track.get("status") == "blocked_external"]
    if not blocked:
        _fail(problems, "no track is recorded as externally blocked, so the A6 gap is unstated")
        return
    for track in blocked:
        measured = track.get("measured", {})
        if measured.get("qualified") is not False:
            _fail(problems, f"{track.get('id')}: a blocked track must record qualified: false")
        if measured.get("exit_code") != 3:
            _fail(problems, f"{track.get('id')}: the missing-prerequisite exit code must be 3")
    readiness = ROOT / "reports" / "m5-qualification" / "readiness.json"
    if not readiness.is_file():
        _fail(problems, "the readiness report the blocked track points at is missing")
        return
    payload = json.loads(readiness.read_text(encoding="utf-8"))
    if payload.get("status") != "awaiting_real_model_qualification":
        _fail(problems, f"readiness status is {payload.get('status')!r}, expected the awaiting status")
    if payload.get("s_at") or payload.get("gates"):
        _fail(problems, "the readiness report carries scored results, so it was not a readiness run")


def main(argv: list[str] | None = None) -> int:
    problems: list[str] = []
    arguments = sys.argv[1:] if argv is None else argv
    record_path = Path(arguments[0]).resolve() if arguments else RECORD
    if not record_path.is_file():
        print(f"FAIL: {record_path} does not exist")
        return 2
    record = json.loads(record_path.read_text(encoding="utf-8"))

    _check_record_structure(record, problems)
    pointers = _check_evidence_resolves(record, problems)
    _check_document(record, problems)
    computed = _check_identities(record, problems)
    _check_history_is_historical(problems)
    _check_blocked_track(record, problems)
    frozen_ok, frozen_note = _git_clean(FROZEN_DIRECTORIES)
    if not frozen_ok:
        _fail(problems, frozen_note)

    print(f"release anchor            : {record.get('release_anchor')}")
    print(f"overall status            : {record.get('overall_status')}")
    print(f"tracks                    : {len(record.get('tracks', []))}")
    print(f"evidence pointers         : {pointers} resolved")
    print(f"frozen evidence           : {frozen_note}")
    print(f"core_corpus_digest v3     : {computed['core_corpus_digest']}")
    print(f"archived v2 digest        : {computed['archived_core_corpus_digest']}")
    print(f"spec / corpus / oracle    : {computed['spec_version']} / {computed['corpus_version']} / {computed['oracle_version']}")
    if problems:
        print("\nFAIL — the release record does not hold:")
        for problem in problems:
            print(f"  - {problem}")
        return 2
    print("\nPASS — the record is self-consistent, its identities recompute, and the blocked track is blocked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
