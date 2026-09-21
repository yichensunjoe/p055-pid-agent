#!/usr/bin/env python3
"""Print or freeze the M4 validation-semantics manifest (§E2).

Run it against the working tree to inspect the current semantics:

    python3 scripts/m4_semantics_manifest.py --out /tmp/current.json

Run it against the accepted M4 tree to freeze the baseline. The module only uses M4 APIs, which
is what makes this possible:

    git archive ecedc00 backend/agentcad | tar -x -C /tmp/m4base
    cp backend/agentcad/m4_invariance.py /tmp/m4base/backend/agentcad/
    PYTHONPATH=/tmp/m4base/backend python3 scripts/m4_semantics_manifest.py \\
        --out backend/agentcad/frozen/m4-validation-semantics-ecedc00.json

Then `python3 scripts/m4_semantics_manifest.py --check` regenerates the manifest from the current
tree and compares it with the frozen one, which is what the gate does in CI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="write the manifest here")
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the regenerated manifest with the frozen one and exit non-zero on drift",
    )
    parser.add_argument("--json", action="store_true", help="print the payload as JSON")
    args = parser.parse_args()

    from agentcad.m4_invariance import FROZEN_MANIFEST, build_manifest, run_gate

    if args.check:
        report = run_gate()
        if args.json:
            print(json.dumps(report.payload(), indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print(f"m4_validation_semantics_invariance: {'ok' if report.ok else 'DRIFT'}")
            for finding in report.findings[:20]:
                print(f"  {finding.path}: {finding.frozen!r} -> {finding.current!r}")
        return 0 if report.ok else 1

    manifest = build_manifest()
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    out = args.out or FROZEN_MANIFEST
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(manifest['rule_catalog'])} rules)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
