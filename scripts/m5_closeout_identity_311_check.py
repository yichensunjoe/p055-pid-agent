"""Recompute the corpus identity on a *second* CPython, using the standard library only.

The corpus identity exists because the old value was bound to the interpreter: it hashed the
bytecode of the mutation operators, so the same frozen corpus printed one number on 3.11 and another
on 3.12. A test that only ever runs here cannot show the fix works; this script can be handed to any
interpreter in the project's supported range and asked for the same answer.

It reads the published inputs (``reports/m5-closeout/corpus-identity-inputs.json``) and the recorded
digests (``reports/m5-closeout/corpus-identity.json``), and it has to find them equal. Nothing from
``agentcad`` is imported on purpose: the point is that the identity is a function of the data, so
hashing it needs no project code and no particular interpreter.

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


def digest(payload: object) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    print(f"all identities reproduced on this interpreter: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
