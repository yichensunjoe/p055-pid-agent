"""Project the M5 closeout: the corpus identity, and the two things it must not disturb.

The closeout adds one thing and must not move anything else, so the evidence is built around
questions a reviewer can answer by recomputation:

1. **What is the corpus's identity, and on which interpreter is it that value?** ``core_corpus_digest``
   for the working v3 corpus and for the archived v2 corpus, next to the manifest each is derived
   from, next to ``core_corpus_fingerprint`` -- recomputed once as-is and once with the *other*
   interpreter's ``generator_fingerprint`` substituted. The digest must not move; the fingerprint is
   shown moving, because that is what "provenance" means here.
2. **Did publishing the corpus coordinates move a published result hash?** The frozen v3 acceptance
   payload is re-hashed twice: as published, and with ``corpus_version`` + ``core_corpus_digest``
   added the way the runner now adds them. Both hashes -- the legacy ``benchmark_result_hash`` and
   the v1 ``benchmark_semantic_hash`` -- have to be the same number both times.
3. **Is the retired extension still auditable?** Its four published numbers, recomputed from the
   frozen JSON alone: file sha256, ``coverage_corpus_digest``, ``coverage_report_hash``, and the
   interpreter-specific published ``corpus_fingerprint``.

Nothing here regenerates published evidence or overwrites it: the outputs are new files under
``reports/m5-closeout/``.

Run it from the repository root::

    .venv/bin/python scripts/m5_closeout_identity.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agentcad import repair_coverage_ledger as ledger  # noqa: E402
from agentcad.repair_benchmark import (  # noqa: E402
    BENCHMARK_SPEC_VERSION,
    CORE_CORPUS_ID,
    CORE_CORPUS_VERSION,
    CORPUS_IDENTITY_EXCLUDED_KEYS,
    SPEC_FINGERPRINT_V2,
    core_corpus_digest,
    core_corpus_digest_manifest,
    core_corpus_fingerprint,
    core_corpus_manifest,
    corpus_definition_ast_projection,
    generator_fingerprint,
    spec_fingerprint,
)
from agentcad.repair_evidence import RepairBenchmarkResult  # noqa: E402
from agentcad.repair_models import (  # noqa: E402
    REPAIR_LEGACY_HASH_EXCLUDES,
    REPAIR_SEMANTIC_HASH_VERSION,
    REPAIR_VOLATILE_FIELDS,
    repair_digest,
    repair_semantic_digest,
)

#: The ``generator_fingerprint`` CPython 3.11 computed for this corpus (CI run 35579988999). It
#: stands in for "the same corpus on the other interpreter": the digest must not see it, and the
#: fingerprint must.
CI_3_11_GENERATOR_FINGERPRINT = "43979207ca8f9b608996277f63cf993cc33175c6e5cb79dd2ae71ee4ba60857c"

FROZEN_V3_ACCEPTANCE = ROOT / "reports" / "m5-promotion" / "acceptance-v3.json"
FROZEN_EXTENSION_EVIDENCE = ROOT / "reports" / "m5" / "repair-coverage-extension.json"

OUT = ROOT / "reports" / "m5-closeout"


def _canonical_hash(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _published_payload_hash(payload: Any) -> str:
    """The retired coverage track's own hash rule, kept here so its numbers stay recomputable."""

    def strip(value: Any) -> Any:
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


def _result_hashes(payload: dict[str, Any]) -> tuple[str, str]:
    blanked = {**payload, "benchmark_result_hash": "", "benchmark_semantic_hash": ""}
    model = RepairBenchmarkResult.model_validate(blanked)
    return (
        repair_digest(model, exclude=REPAIR_LEGACY_HASH_EXCLUDES),
        repair_semantic_digest(model),
    )


def identity_report() -> tuple[str, dict[str, Any]]:
    lines: list[str] = []
    data: dict[str, Any] = {"corpus_id": CORE_CORPUS_ID}

    lines.append("== corpus identity ==")
    live_generator = generator_fingerprint()
    lines.append(f"interpreter generator_fingerprint : {live_generator}")
    lines.append(f"3.11 generator_fingerprint (CI)   : {CI_3_11_GENERATOR_FINGERPRINT}")
    lines.append("")

    for label, version in (("v3 (working)", CORE_CORPUS_VERSION), ("v2 (archived)", "2")):
        digest = core_corpus_digest(version)
        digest_input = core_corpus_digest_manifest(version)
        manifest = core_corpus_manifest(version)
        catalogue = digest_input["operator_catalogue"]
        fields: dict[str, Any] = {
            "digest": digest,
            "digest_manifest_keys": sorted(digest_input),
            "case_count": len(digest_input["case_universe"]),
            "dev_case_count": len(digest_input["dev_case_universe"]),
            "operator_count": len(catalogue),
            "supported_code_count": len([row for row in catalogue if row["target_code"]]),
            "case_row_keys": sorted(digest_input["case_universe"][0]),
            "operator_row_keys": sorted(catalogue[0]),
            "environment_keys_in_identity": sorted(
                set(digest_input) & set(CORPUS_IDENTITY_EXCLUDED_KEYS)
            ),
            "environment_keys_in_manifest": sorted(
                set(manifest) & set(CORPUS_IDENTITY_EXCLUDED_KEYS)
            ),
        }
        lines.append(f"-- {label} --")
        lines.append(f"core_corpus_digest({version!r})              : {digest}")
        lines.append(
            f"corpus_version / spec_version        : {digest_input['corpus_version']} / {digest_input['spec_version']}"
        )
        lines.append(
            f"acceptance / dev cases               : {fields['case_count']} / {fields['dev_case_count']}"
        )
        lines.append(
            f"operators / codes with a producer    : {fields['operator_count']} / {fields['supported_code_count']}"
        )
        lines.append(
            f"projection top-level keys            : {', '.join(sorted(digest_input))}"
        )
        lines.append(f"per-case row keys                    : {', '.join(fields['case_row_keys'])}")
        lines.append(f"per-operator row keys                : {', '.join(fields['operator_row_keys'])}")
        lines.append(
            f"environment keys inside the identity : {fields['environment_keys_in_identity'] or 'none'}"
        )
        lines.append(
            f"environment keys beside it           : {fields['environment_keys_in_manifest'] or 'none'}"
        )
        if version == CORE_CORPUS_VERSION:
            as_built = core_corpus_fingerprint()
            under_3_11 = _canonical_hash(
                {**manifest, "generator_fingerprint": CI_3_11_GENERATOR_FINGERPRINT}
            )
            lines.append(f"core_corpus_fingerprint()            : {as_built}   (runtime provenance)")
            lines.append(f"same fingerprint on 3.11 bytecode    : {under_3_11}")
            lines.append(f"fingerprint identical under 3.11?    : {as_built == under_3_11}   (expected: no)")
            lines.append(f"digest identical under 3.11?         : {digest == core_corpus_digest()}   (expected: yes)")
            fields["fingerprint_local"] = as_built
            fields["fingerprint_with_3_11_generator"] = under_3_11
            fields["fingerprint_identical_under_3_11"] = as_built == under_3_11
        else:
            lines.append(
                "core_corpus_fingerprint for v2      : not recomputable (the v2 operators' bytecode is gone)"
            )
            lines.append(f"v2 spec_fingerprint (recorded)      : {SPEC_FINGERPRINT_V2}")
            lines.append(f"v2 spec_fingerprint (recomputed)    : {spec_fingerprint('2')}")
            fields["spec_fingerprint_v2"] = spec_fingerprint("2")
        lines.append("")
        data[version] = fields

    lines.append("== published identities ==")
    lines.append(f"BENCHMARK_SPEC_VERSION : {BENCHMARK_SPEC_VERSION}")
    lines.append(f"CORE_CORPUS_VERSION    : {CORE_CORPUS_VERSION}")
    lines.append(f"spec_fingerprint()     : {spec_fingerprint()}")
    lines.append(f"spec_fingerprint('2')  : {spec_fingerprint('2')}")
    lines.append(f"ledger_digest          : {ledger.ledger_digest()}")
    lines.append(f"ledger_fingerprint     : {ledger.ledger_fingerprint()}   (runtime provenance)")
    data["spec_version"] = BENCHMARK_SPEC_VERSION
    data["corpus_version"] = CORE_CORPUS_VERSION
    data["spec_fingerprint"] = spec_fingerprint()
    data["ledger_digest"] = ledger.ledger_digest()
    data["ledger_fingerprint"] = ledger.ledger_fingerprint()
    data["corpus_identity_excluded_keys"] = list(CORPUS_IDENTITY_EXCLUDED_KEYS)
    return "\n".join(lines), data


def hash_compatibility_report() -> tuple[str, dict[str, Any]]:
    payload = json.loads(FROZEN_V3_ACCEPTANCE.read_text(encoding="utf-8"))
    as_published = _result_hashes(payload)
    with_coordinates = {
        **payload,
        "corpus_version": CORE_CORPUS_VERSION,
        "core_corpus_digest": core_corpus_digest(),
    }
    after_closeout = _result_hashes(with_coordinates)

    lines = ["== hash compatibility: frozen v3 acceptance payload =="]
    lines.append(f"candidate_sha                     : {payload['candidate_sha']}")
    lines.append(f"recorded benchmark_result_hash    : {payload['benchmark_result_hash']}")
    lines.append(f"recorded benchmark_semantic_hash  : {payload['benchmark_semantic_hash']}")
    lines.append(f"semantic_hash_version             : {payload.get('semantic_hash_version')} (contract {REPAIR_SEMANTIC_HASH_VERSION})")
    lines.append("")
    lines.append(f"recomputed legacy   (as published): {as_published[0]}")
    lines.append(f"recomputed legacy   (+coordinates): {after_closeout[0]}")
    lines.append(f"legacy unchanged                  : {as_published[0] == after_closeout[0] == payload['benchmark_result_hash']}")
    lines.append(f"recomputed semantic (as published): {as_published[1]}")
    lines.append(f"recomputed semantic (+coordinates): {after_closeout[1]}")
    lines.append(f"semantic unchanged                : {as_published[1] == after_closeout[1] == payload['benchmark_semantic_hash']}")
    data = {
        "frozen_evidence": str(FROZEN_V3_ACCEPTANCE.relative_to(ROOT)),
        "candidate_sha": payload["candidate_sha"],
        "benchmark_result_hash": payload["benchmark_result_hash"],
        "benchmark_semantic_hash": payload["benchmark_semantic_hash"],
        "legacy_with_coordinates": after_closeout[0],
        "semantic_with_coordinates": after_closeout[1],
        "legacy_unchanged": as_published[0] == after_closeout[0] == payload["benchmark_result_hash"],
        "semantic_unchanged": as_published[1] == after_closeout[1] == payload["benchmark_semantic_hash"],
    }
    return "\n".join(lines), data


def extension_pin_report() -> tuple[str, dict[str, Any]]:
    raw = FROZEN_EXTENSION_EVIDENCE.read_bytes()
    report = json.loads(raw)
    corpus = report["corpus"]
    recomputed = {
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "coverage_corpus_digest": _published_payload_hash(
            {k: v for k, v in corpus.items() if k not in CORPUS_IDENTITY_EXCLUDED_KEYS}
        ),
        "coverage_report_hash": _published_payload_hash(
            {k: v for k, v in report.items() if k != "report_hash"}
        ),
        "corpus_fingerprint_3_12": _published_payload_hash(corpus),
    }
    published = {
        "file_sha256": None,
        "coverage_corpus_digest": report["corpus"].get("coverage_corpus_digest"),
        "coverage_report_hash": report["report_hash"],
        "corpus_fingerprint_3_12": report["corpus_fingerprint"],
    }
    lines = ["== retired extension: published identities, recomputed from the frozen file =="]
    for key, value in recomputed.items():
        lines.append(f"{key:26s}: {value}")
    lines.append("")
    lines.append("(recorded in the disposition ledger as an index, not as the evidence)")
    lines.append(f"cases held                : {', '.join(row['code'] for row in report['rows'])}")
    data = {"recomputed": recomputed, "published_in_report": published, "cases": [row["code"] for row in report["rows"]]}
    return "\n".join(lines), data


def definition_identity_report() -> tuple[str, dict[str, Any]]:
    """The interpreter-independent half of the producer identities, per definition.

    Published separately from the digests because it is the part a second interpreter can re-derive
    from the *source file* alone: parse, dump the AST, hash. No import, no pydantic, no bytecode.
    """

    trace = corpus_definition_ast_projection()
    lines = [f"== definition identities ({len(trace)} definitions folded into the producers) =="]
    for qualname, value in sorted(trace.items()):
        lines.append(f"{qualname:40s}: {value}")
    return "\n".join(lines), {"definition_ast_identities": trace}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sections = [
        identity_report(),
        definition_identity_report(),
        hash_compatibility_report(),
        extension_pin_report(),
    ]
    text = "\n\n".join(section[0] for section in sections) + "\n"
    payload = {
        "corpus_identity": sections[0][1],
        "definition_identity": sections[1][1],
        "hash_compatibility": sections[2][1],
        "retired_extension": sections[3][1],
    }
    (OUT / "corpus-identity.txt").write_text(text, encoding="utf-8")
    (OUT / "corpus-identity.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # The identity's *input*, published as pure JSON so another interpreter can hash it with the
    # standard library alone -- which is what `scripts/m5_closeout_identity_311_check.py` does on a
    # second CPython. Proving the digest stable on one interpreter is not a proof; proving the two
    # interpreters hash the same bytes to the same value is.
    inputs = {
        version: core_corpus_digest_manifest(version) for version in (CORE_CORPUS_VERSION, "2")
    }
    (OUT / "corpus-identity-inputs.json").write_text(
        json.dumps(inputs, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(text)
    print(
        "written: "
        + ", ".join(
            str(path.relative_to(ROOT))
            for path in (
                OUT / "corpus-identity.txt",
                OUT / "corpus-identity.json",
                OUT / "corpus-identity-inputs.json",
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
