"""Project the M5 coverage promotion: what moved, what did not, and what can be recomputed.

The promotion is a spec cut, so the questions a reviewer has to be able to answer are mechanical:

1. **What joined the catalogue?** A diff of the two operator catalogues, not a summary of it.
2. **What did the case set become?** The 72 acceptance cases derived for one fixed candidate under
   the *archived* v2 spec and under the working v3 spec, side by side. The v2 side is derived from
   :func:`agentcad.repair_benchmark.spec_fingerprint` with ``"2"``, so it is the case set the
   published v2 evidence was produced from, not a reconstruction from memory.
3. **What stayed put?** Thresholds, oracle version, family list, case layout and the safety suite
   are compared field by field; anything that moved is printed as moved.
4. **Which identities are still recomputable, and on which interpreter?** Each published identity
   is recomputed once as-is and once with ``generator_fingerprint`` replaced by another runtime's
   value. An identity that does not move is one two machines can compare; one that does move is
   runtime provenance and is labelled as such.

Run it from the repository root::

    .venv/bin/python scripts/m5_promotion_projection.py

It writes ``reports/m5-promotion/spec-projection.txt`` and
``reports/m5-promotion/case-projection.txt`` and prints both to stdout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agentcad import repair_benchmark as benchmark  # noqa: E402
from agentcad import repair_coverage_ledger as ledger  # noqa: E402
from agentcad.repair_benchmark import (  # noqa: E402
    ACCEPTANCE_CASES_PER_FAMILY,
    BENCHMARK_SPEC_VERSION,
    CORE_CORPUS_VERSION,
    FAMILIES,
    MUTATIONS,
    archived_operator_catalogue,
    generate_cases,
    generate_suite,
    spec_fingerprint,
    spec_payload,
)

CANDIDATE = "c" * 40
#: A value from a different CPython, used to stand in for "the same ledger on another interpreter".
OTHER_RUNTIME_GENERATOR_FINGERPRINT = "fda18c2fd1c7e53eb0d8ec7e76094ca127c85beb20294e868c2bedc38b38b67a"

OUT = ROOT / "reports" / "m5-promotion"


def _operator_table(payload: dict) -> dict[str, dict]:
    return {row["operator_id"]: row for row in payload["operators"]}


def spec_projection() -> str:
    pre = spec_payload("2")
    post = spec_payload()
    pre_ops = _operator_table(pre)
    post_ops = _operator_table(post)

    lines: list[str] = []
    lines.append("=== M5 coverage promotion · spec projection ===")
    lines.append(f"pre  spec_version = {pre['spec_version']}   fingerprint = {spec_fingerprint('2')}")
    lines.append(
        f"post spec_version = {post['spec_version']}   fingerprint = {spec_fingerprint()}"
    )
    lines.append(f"working BENCHMARK_SPEC_VERSION = {BENCHMARK_SPEC_VERSION}")
    lines.append(f"core corpus version = {CORE_CORPUS_VERSION}")
    lines.append("")

    lines.append("--- operators joined ---")
    for operator_id in sorted(set(post_ops) - set(pre_ops)):
        row = post_ops[operator_id]
        lines.append(
            f"+ {operator_id:28s} family={row['family']} code={row['target_code']} "
            f"validator={row['validator_id']} base={row['base_variant']} "
            f"budget={row['touched_budget_class']} "
            f"create={row['permits_creation']}({row['max_created_ids']}) "
            f"delete={row['permits_deletion']}({row['max_deleted_ids']})"
        )
    lines.append("")
    lines.append("--- operators that moved ---")
    moved = 0
    for operator_id in sorted(set(pre_ops) & set(post_ops)):
        if pre_ops[operator_id] != post_ops[operator_id]:
            moved += 1
            lines.append(f"~ {operator_id}")
            for key in sorted(pre_ops[operator_id]):
                if pre_ops[operator_id][key] != post_ops[operator_id][key]:
                    lines.append(
                        f"    {key}: {pre_ops[operator_id][key]!r} -> {post_ops[operator_id][key]!r}"
                    )
    if not moved:
        lines.append("(none: every pre-existing operator is byte-identical)")
    lines.append("")
    lines.append("--- operators removed ---")
    removed = sorted(set(pre_ops) - set(post_ops))
    lines.append("(none)" if not removed else "\n".join(f"- {item}" for item in removed))
    lines.append("")

    lines.append("--- fields that had to stay put ---")
    for key in (
        "oracle_version",
        "families",
        "thresholds",
        "dev_cases_per_family",
        "acceptance_cases_per_family",
        "safety_cases",
    ):
        same = pre[key] == post[key]
        lines.append(f"{'=' if same else '!'} {key}: {json.dumps(post[key], sort_keys=True)}")
    lines.append(f"operator_count: {len(pre_ops)} -> {len(post_ops)}")

    return "\n".join(lines) + "\n"


def case_projection() -> str:
    # The v2 side is derived from the *archived* v2 body -- both its fingerprint and its operator
    # rotation -- rather than from today's catalogue with v2 seeds, which would produce a case set
    # that never existed.
    v2_operators = archived_operator_catalogue("2")
    pre = []
    for family in FAMILIES:
        pre.extend(
            generate_cases(
                candidate_sha=CANDIDATE,
                family=family,
                count=ACCEPTANCE_CASES_PER_FAMILY,
                suite="acceptance",
                spec=spec_fingerprint("2"),
                catalogue=v2_operators,
            )
        )
    post = generate_suite(candidate_sha=CANDIDATE, suite="acceptance")

    lines: list[str] = []
    lines.append("=== M5 coverage promotion · acceptance case projection ===")
    lines.append(f"candidate_sha = {CANDIDATE}")
    lines.append(f"pre  derived with spec_fingerprint('2') = {spec_fingerprint('2')}")
    lines.append(f"post derived with spec_fingerprint()    = {spec_fingerprint()}")
    lines.append(f"cases: {len(pre)} -> {len(post)}")
    lines.append("")
    lines.append(f"{'idx':>3}  {'family':6} {'operator (pre)':30} {'operator (post)':30} seed")
    changed = 0
    for index, (before, after) in enumerate(zip(pre, post, strict=True)):
        mark = " " if before.operator_id == after.operator_id else "*"
        if before.operator_id != after.operator_id:
            changed += 1
        lines.append(
            f"{index:>3}  {after.family:6} {before.operator_id:30} {after.operator_id:30} "
            f"{mark} {before.seed} -> {after.seed}"
        )
    lines.append("")
    lines.append(f"cases whose operator changed: {changed}/{len(post)}")
    lines.append(
        "case ids are SHA-independent and stay in the same order (the layout did not change); the "
        "seeds moved because they derive from the spec fingerprint, which is the point of a spec cut"
    )
    lines.append("")
    lines.append("--- operators carried by the v3 acceptance set ---")
    for operator_id in sorted({case.operator_id for case in post}):
        occurrences = sum(1 for case in post if case.operator_id == operator_id)
        lines.append(f"{operator_id:32s} {occurrences:2d}")
    lines.append("")
    lines.append(f"registered operators: {len(MUTATIONS)}")
    return "\n".join(lines) + "\n"


def identity_table() -> str:
    """Which published identities survive a change of interpreter, proven rather than asserted."""

    before = {
        "spec_fingerprint('2')": spec_fingerprint("2"),
        "spec_fingerprint()": spec_fingerprint(),
        "core_corpus_fingerprint()": benchmark.core_corpus_fingerprint(),
        "ledger_digest()": ledger.ledger_digest(),
        "ledger_fingerprint()": ledger.ledger_fingerprint(),
        "generator_fingerprint()": benchmark.generator_fingerprint(),
    }

    original_benchmark = benchmark.generator_fingerprint
    original_ledger = ledger.generator_fingerprint
    try:
        benchmark.generator_fingerprint = lambda: OTHER_RUNTIME_GENERATOR_FINGERPRINT
        ledger.generator_fingerprint = lambda: OTHER_RUNTIME_GENERATOR_FINGERPRINT
        after = {
            "spec_fingerprint('2')": spec_fingerprint("2"),
            "spec_fingerprint()": spec_fingerprint(),
            "core_corpus_fingerprint()": benchmark.core_corpus_fingerprint(),
            "ledger_digest()": ledger.ledger_digest(),
            "ledger_fingerprint()": ledger.ledger_fingerprint(),
            "generator_fingerprint()": benchmark.generator_fingerprint(),
        }
    finally:
        benchmark.generator_fingerprint = original_benchmark
        ledger.generator_fingerprint = original_ledger

    lines: list[str] = []
    lines.append("=== M5 coverage promotion · identity table ===")
    lines.append(
        "Each identity is recomputed twice: as published, and with generator_fingerprint replaced "
        "by another CPython's value."
    )
    lines.append("")
    lines.append(f"{'identity':28} {'moved?':7} value")
    for name, value in before.items():
        moved = after[name] != value
        label = "RUNTIME" if moved else "stable"
        lines.append(f"{name:28} {label:7} {value}")
    lines.append("")
    lines.append(
        "stable = identical on 3.11 and 3.12 for the same ledger, so two machines may compare it.\n"
        "RUNTIME = a fact about the interpreter that produced it, published as provenance and never\n"
        "used as an identity (the remote's ruling on generator_fingerprint)."
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    spec_text = spec_projection()
    case_text = case_projection()
    identity_text = identity_table()
    (OUT / "spec-projection.txt").write_text(spec_text + "\n" + identity_text, encoding="utf-8")
    (OUT / "case-projection.txt").write_text(case_text, encoding="utf-8")
    sys.stdout.write(spec_text + "\n")
    sys.stdout.write(identity_text + "\n")
    sys.stdout.write(case_text)


if __name__ == "__main__":
    main()
