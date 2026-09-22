"""A TypeSafe key must not be recoverable from anything this repository keeps.

The live acceptance for the TypeSafe drawing path is a *manual* check by design: CI has no
credential and must not make paid external calls. That makes the repository itself the place the
credential could leak -- into an evidence transcript, a debug log, a committed request body -- so
the absence is checked here rather than assumed.

The scan is deliberately shape-based, not value-based: nobody has to hand a tester the key, and the
check keeps working after the key is rotated. Patterns are assembled from fragments so this file
does not contain the very shapes it forbids. A synthetic self-test proves the scanner bites.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "test-results",
    "playwright-report",
}
TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".mjs",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
MAX_BYTES = 2_000_000

#: Name -> what a match would mean. Values are built from fragments on purpose.
LEAK_PATTERNS: dict[str, re.Pattern[str]] = {
    "a TypeSafe key literal": re.compile("api" + r"key_[A-Za-z0-9_\-]{8,}"),
    "an authorization header carrying a token": re.compile(
        r"(?i)bearer\s+[A-Za-z0-9_\-\.]{24,}"
    ),
    "an environment assignment of a real key": re.compile(
        "TYPESAFE_" + r"API_KEY\s*=\s*[\"']?[A-Za-z0-9_\-]{16,}"
    ),
    "a key written into a JSON body": re.compile(
        r"\"api_key\"\s*:\s*\"[A-Za-z0-9_\-]{24,}\""
    ),
}


def find_leaks(body: str) -> dict[str, list[str]]:
    """Which forbidden shapes appear in ``body``, so the caller can say *what* was found."""

    return {
        name: matches
        for name, pattern in LEAK_PATTERNS.items()
        if (matches := sorted(set(pattern.findall(body))))
    }


def repo_text_files() -> list[Path]:
    found: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
        except OSError:  # pragma: no cover - a file that vanished mid-scan
            continue
        found.append(path)
    return found


def test_the_scanner_bites_on_the_shapes_it_forbids() -> None:
    """A check that cannot fail proves nothing, so it is fed the shapes it is meant to catch."""

    assert find_leaks("nothing to see here") == {}
    assert find_leaks("apikey_" + "a" * 32)
    assert find_leaks("Authorization: Bearer " + "b" * 40)
    assert find_leaks("TYPESAFE_" + 'API_KEY="' + "c" * 32 + '"')
    assert find_leaks('{"api_key": "' + "d" * 32 + '"}')
    # The scrubbed forms the repository is allowed to keep: a prefix with no material, a
    # placeholder, and the variable's name with no value.
    assert find_leaks("export TYPESAFE_API_KEY=apikey_…") == {}
    assert find_leaks("Use the Authorization: Bearer <token> header.") == {}
    assert find_leaks("TYPESAFE_API_KEY / TYPESAFE_BASE_URL / TYPESAFE_MODEL") == {}
    assert find_leaks('"api_key": "secret-provider-key"') == {}


def test_no_typesafe_credential_is_recoverable_from_the_repository() -> None:
    """Every text file this repository keeps, checked for credential shapes."""

    leaks: list[str] = []
    scanned = 0
    for path in repo_text_files():
        scanned += 1
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - unreadable file
            continue
        for name, matches in find_leaks(body).items():
            leaks.append(f"{path.relative_to(REPO_ROOT)}: {name} ({len(matches)})")
    assert scanned > 100, "the scan found almost nothing to read, so it is not scanning"
    assert not leaks, leaks


def test_the_committed_evidence_records_the_source_and_never_the_key() -> None:
    """The one TypeSafe artifact that is committed: it may say *where* the key came from."""

    evidence = REPO_ROOT / "reports" / "typesafe" / "live-acceptance.txt"
    assert evidence.is_file(), "the live acceptance transcript is the evidence this gate publishes"
    body = evidence.read_text(encoding="utf-8")
    assert "api_key_source': 'environment" in body or '"api_key_source": "environment"' in body
    assert "api_key_present': True" in body or '"api_key_present": true' in body
    assert find_leaks(body) == {}
    # A judgment and a compiled transaction, not just a reachability probe.
    assert "verdict:" in body and "compile:" in body


def test_ci_never_configures_a_typesafe_credential() -> None:
    """CI runs the credential-free contract tests; a real key there would need a secret and a bill."""

    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "the CI definitions are part of the contract that is being checked"
    for workflow in workflows:
        body = workflow.read_text(encoding="utf-8")
        assert "TYPESAFE" not in body.upper(), workflow.name
        assert "api" + "key_" not in body, workflow.name


@pytest.mark.parametrize("suffix", [".py", ".ts", ".txt", ".md"])
def test_the_scan_covers_the_extensions_that_carry_evidence(suffix: str) -> None:
    scanned = {path.suffix for path in repo_text_files()}
    assert suffix in scanned
