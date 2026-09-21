"""M5 §E2: the M4 semantics invariance gate.

The gate is only worth having if it can *fail*. These tests therefore do two things: prove the
current tree agrees with the manifest frozen at the M4 accepted commit, and prove that the kinds
of drift the gate exists to catch — a re-enabled rule, a lowered severity, a changed fixture
conclusion — are actually detected rather than absorbed.
"""

from __future__ import annotations

import json

from agentcad.m4_invariance import (
    FROZEN_MANIFEST,
    M4_ACCEPTED_SHA,
    build_manifest,
    compare_manifests,
    load_frozen_manifest,
    run_gate,
)


def test_the_frozen_manifest_names_the_m4_accepted_commit():
    frozen = load_frozen_manifest()
    assert frozen["m4_accepted_sha"] == M4_ACCEPTED_SHA
    assert FROZEN_MANIFEST.exists()
    assert FROZEN_MANIFEST.name.endswith(f"{M4_ACCEPTED_SHA[:7]}.json")


def test_the_manifest_is_reproducible():
    assert build_manifest() == build_manifest()


def test_current_semantics_match_the_frozen_manifest():
    report = run_gate()
    assert report.ok is True, report.codes()
    assert report.frozen_sha == M4_ACCEPTED_SHA


def test_a_changed_rule_severity_is_detected():
    frozen = load_frozen_manifest()
    drifted = json.loads(json.dumps(frozen))
    drifted["rule_catalog"][0]["default_severity"] = "info"
    findings = compare_manifests(frozen, drifted)
    assert any("rule_catalog[0].default_severity" == finding.path for finding in findings)


def test_a_dropped_rule_is_detected():
    frozen = load_frozen_manifest()
    drifted = json.loads(json.dumps(frozen))
    drifted["rule_catalog"] = drifted["rule_catalog"][:-1]
    findings = compare_manifests(frozen, drifted)
    assert any(finding.path.startswith("rule_catalog.length") for finding in findings)


def test_a_disabled_rule_is_detected():
    frozen = load_frozen_manifest()
    drifted = json.loads(json.dumps(frozen))
    moved = drifted["enabled_rules"].pop(0)
    drifted["disabled_rules"] = sorted([*drifted["disabled_rules"], moved])
    findings = compare_manifests(frozen, drifted)
    assert findings, "a rule silently disabled is exactly what the gate is for"


def test_a_changed_fixture_conclusion_is_detected():
    frozen = load_frozen_manifest()
    drifted = json.loads(json.dumps(frozen))
    projection = drifted["fixture_projections"]
    target = next(item for item in projection if item["issues"])
    target["issues"][0]["severity"] = "info"
    findings = compare_manifests(frozen, drifted)
    assert any(".issues[0].severity" in finding.path for finding in findings)


def test_a_relaxed_required_validator_list_is_detected():
    frozen = load_frozen_manifest()
    drifted = json.loads(json.dumps(frozen))
    drifted["release_policy"]["required_validators"] = [
        item for item in drifted["release_policy"]["required_validators"][:1]
    ]
    findings = compare_manifests(frozen, drifted)
    assert any("release_policy.required_validators" in finding.path for finding in findings)


def test_the_fixture_corpus_is_not_empty_and_records_conclusions():
    manifest = build_manifest()
    projections = manifest["fixture_projections"]
    assert len(projections) >= 4
    assert any(item["issues"] for item in projections), "at least one fixture must produce findings"
    assert any(not item["issues"] for item in projections), "and one must be clean"
