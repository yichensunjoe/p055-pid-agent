"""Recompute the corpus identity on a *second* CPython, using the standard library only.

The corpus identity exists because the old value was bound to the interpreter: it hashed the
bytecode of the mutation operators, so the same frozen corpus printed one number on 3.11 and another
on 3.12. A test that only ever runs here cannot show the fix works; this script can be handed to any
interpreter in the project's supported range and asked for the same answer. It checks both halves:

1. **the digest**, recomputed from the published projection
   (``reports/m5-closeout/corpus-identity-inputs.json``) and compared with the recorded value;
2. **the definition identities**, re-derived from the source file alone -- parse it, project each
   function's AST into a canonical form, hash it -- and compared with the recorded map
   (``corpus-identity.json`` ``definition_identity``). This is the half that used to be bytecode,
   so it is the half a second interpreter most needs to check. The one project module imported is
   ``agentcad.source_identity``, which is stdlib-only *for this reason*: the canonical form has to
   be the same implementation on both sides, and it has to run where no dependency is installed.
   Nothing else from ``agentcad`` is touched.

    python3.11 scripts/m5_closeout_identity_311_check.py
    python3.12 scripts/m5_closeout_identity_311_check.py
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLOSEOUT = ROOT / "reports" / "m5-closeout"
DEFINITION_SOURCES = (
    ROOT / "backend" / "agentcad" / "repair_benchmark.py",
    ROOT / "backend" / "agentcad" / "repair_safety.py",
)

sys.path.insert(0, str(ROOT / "backend"))

from agentcad.source_identity import module_definition_identities  # noqa: E402


def digest(payload: object) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def definition_identities() -> dict[str, str]:
    """``module:qualname -> identity``, read straight off the source files.

    Module-qualified because a producer folds in the definitions it *calls*, and those are not always
    in the same file: the safety builders live in ``repair_safety`` beside the helpers they use.
    """

    computed: dict[str, str] = {}
    for path in DEFINITION_SOURCES:
        rows = module_definition_identities(path.read_text(encoding="utf-8"))
        computed.update({f"{path.stem}:{qualname}": value for qualname, value in rows.items()})
    return computed


def main() -> int:
    inputs = json.loads((CLOSEOUT / "corpus-identity-inputs.json").read_text(encoding="utf-8"))
    recorded = json.loads((CLOSEOUT / "corpus-identity.json").read_text(encoding="utf-8"))
    print(f"interpreter: {sys.version.split()[0]} ({platform.python_implementation()})")
    ok = True
    for version in sorted(inputs, reverse=True):
        computed = digest(inputs[version])
        published = recorded["corpus_identity"][version]["digest"]
        same = computed == published
        ok = ok and same
        print(f"corpus {version}: computed {computed}")
        print(f"corpus {version}: recorded {published}   identical: {same}")

    published_definitions = recorded["definition_identity"]["definition_ast_identities"]
    computed_definitions = definition_identities()
    missing = sorted(set(published_definitions) - set(computed_definitions))
    mismatched = sorted(
        qualname
        for qualname, value in published_definitions.items()
        if computed_definitions.get(qualname) != value
    )
    print(f"definition identities recorded    : {len(published_definitions)}")
    print(
        "definition identities recomputed  : "
        f"{len(computed_definitions)} in {len(DEFINITION_SOURCES)} source files"
    )
    print(f"missing from the source file      : {missing or 'none'}")
    print(f"mismatched on this interpreter    : {mismatched or 'none'}")
    identities_same = not missing and not mismatched
    ok = ok and identities_same
    print(f"all identities reproduced on this interpreter: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
