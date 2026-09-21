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
    classify_fixture_delta,
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
    assert report.findings == []
    assert not [
        delta
        for delta in report.fixture_deltas
        if delta.classification == "unexpected_delta"
    ], [delta.unexpected for delta in report.fixture_deltas]


def test_the_tag_fix_moves_only_the_three_expected_conclusions():
    """§E2's allowlist, asserted rather than described.

    Three things are allowed to have moved: a false ``TAG_MISSING`` that disappeared because the
    symbol resolves a tag, a ``TAG_DUPLICATE`` that appeared because two symbols resolve the same
    tag, and a required-port finding whose display name now uses the resolved tag. Everything
    else is a delta the gate must not absorb.
    """

    report = run_gate()
    expected = [d for d in report.fixture_deltas if d.classification == "expected_corrected_delta"]
    assert {delta.fixture_id for delta in expected} == {
        "clean_pair",
        "duplicate_tags",
        "dangling_endpoint",
    }
    for delta in expected:
        assert delta.polished is True, "only a polished drawing may correct a tag conclusion"
        assert delta.reasons, delta.fixture_id
    corrected = [
        delta for delta in expected if "TAG_DUPLICATE" in "".join(delta.reasons)
    ]
    assert [delta.fixture_id for delta in corrected] == ["duplicate_tags"]


def test_a_corpus_that_never_met_the_polish_may_not_move_at_all():
    report = run_gate()
    untouched = [d for d in report.fixture_deltas if not d.polished]
    assert untouched, "the corpus must keep a pre-polish drawing to prove this"
    assert all(delta.classification == "exactly_invariant" for delta in untouched)


def test_an_unjustified_tag_delta_is_unexpected():
    """A removed TAG_MISSING whose symbol resolves nothing is exactly what must fail the gate."""

    frozen = {
        "fixture_id": "f",
        "polished": True,
        "validators_run": [],
        "validators_skipped": [],
        "tag_source": {"v1": {"tag": "", "source": "none"}},
        "messages": {},
        "issues": [
            {
                "validator_id": "engineering-report",
                "rule_id": "engineering-report.TAG_MISSING",
                "code": "TAG_MISSING",
                "severity": "warning",
                "element_ids": ["v1"],
                "object_ids": [],
            }
        ],
    }
    current = json.loads(json.dumps(frozen))
    current["issues"] = []
    assert classify_fixture_delta(frozen, current).classification == "unexpected_delta"

    # The same removal *is* expected once the symbol really does resolve a tag.
    justified = json.loads(json.dumps(current))
    justified["tag_source"] = {"v1": {"tag": "HV-1", "source": "properties.tag"}}
    assert classify_fixture_delta(frozen, justified).classification == "expected_corrected_delta"


def test_a_duplicate_claim_that_is_not_a_duplicate_is_unexpected():
    frozen = {
        "fixture_id": "f",
        "polished": True,
        "validators_run": [],
        "validators_skipped": [],
        "tag_source": {
            "v1": {"tag": "HV-1", "source": "properties.tag"},
            "v2": {"tag": "HV-2", "source": "properties.tag"},
        },
        "messages": {},
        "issues": [],
    }
    current = json.loads(json.dumps(frozen))
    current["issues"] = [
        {
            "validator_id": "engineering-report",
            "rule_id": "engineering-report.TAG_DUPLICATE",
            "code": "TAG_DUPLICATE",
            "severity": "error",
            "element_ids": ["v1", "v2"],
            "object_ids": [],
        }
    ]
    assert classify_fixture_delta(frozen, current).classification == "unexpected_delta"


def test_a_severity_change_is_never_absorbed_by_the_allowlist():
    frozen = {
        "fixture_id": "f",
        "polished": True,
        "validators_run": [],
        "validators_skipped": [],
        "tag_source": {},
        "messages": {},
        "issues": [
            {
                "validator_id": "engineering-report",
                "rule_id": "engineering-report.TAG_MISSING",
                "code": "TAG_MISSING",
                "severity": "warning",
                "element_ids": ["v1"],
                "object_ids": [],
            }
        ],
    }
    current = json.loads(json.dumps(frozen))
    current["tag_source"] = {"v1": {"tag": "HV-1", "source": "properties.tag"}}
    current["issues"][0] = {**current["issues"][0], "severity": "info"}
    delta = classify_fixture_delta(frozen, current)
    assert delta.classification == "unexpected_delta"


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
