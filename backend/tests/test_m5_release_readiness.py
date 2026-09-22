"""The M5 release record is checked by the suite, not just written once.

A release summary rots the moment nobody re-derives it: a track gets described as closed while the
number it quotes has moved, or the externally blocked qualification quietly reads as a pass. The
verifier does the re-derivation (identities against the code, evidence pointers against the tree,
the blocked track against its own readiness report), and this test is what makes forgetting it
impossible.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

VERIFIER = Path(__file__).resolve().parents[2] / "scripts" / "m5_release_readiness.py"


def test_the_m5_release_readiness_record_holds() -> None:
    result = subprocess.run(
        [sys.executable, str(VERIFIER)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_the_verifier_fails_when_the_record_is_wrong(tmp_path: Path) -> None:
    """The check has to bite: a record that claims a wrong digest must not verify."""

    import json

    record = VERIFIER.parents[1] / "reports" / "m5-release-readiness" / "release-readiness.json"
    tampered = tmp_path / "release-readiness.json"
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["identities"]["core_corpus_digest"] = "0" * 64
    tampered.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(VERIFIER), str(tampered)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "identity drift" in result.stdout
