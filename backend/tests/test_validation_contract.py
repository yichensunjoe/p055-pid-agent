"""M4 tests: the canonical contract, the profile chain, waivers and the release gate.

Three kinds of test live here, and the distinction is deliberate:

* **Parity** — the adapters must reproduce what the pre-M4 validators concluded. Same
  code, same severity, same referenced elements, for the same fixture. A change here means
  a rule conclusion changed, which is a rule change and must be reviewed as one.
* **Contract** — ordering, the presence of every field, determinism across runs, and the
  binding of a result to the revision/profile/rule bundle that produced it.
* **Boundaries** — invalid profiles are rejected rather than ignored, waivers never hide
  an issue, the release gate fails closed on missing evidence, and no code path can turn a
  readiness verdict into an approval.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from cad_fixtures import simple_dxf
from pydantic import ValidationError

from agentcad import validation_profile as profiles
from agentcad.diagram_quality import analyze_diagram_quality
from agentcad.engineering_ir import build_engineering_graph
from agentcad.engineering_reports import build_engineering_report
from agentcad.models import (
    AddElementOperation,
    CreateDocumentRequest,
    Point,
    SymbolElement,
    TransactionRequest,
)
from agentcad.release_validator import assess_release_readiness
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry
from agentcad.validation_engine import (
    ValidationContextUnavailable,
    run_validation,
    validate_document,
)
from agentcad.validation_models import ValidationIssue, Waiver
from agentcad.validation_profile import (
    ProfileError,
    ProfileLayer,
    ReleasePolicy,
    RuleOverride,
    ValidationProfile,
    built_in_profile,
    load_profile,
    resolve_profile,
)
from agentcad.validation_rules import DEFAULT_QUALITY_SCORE_THRESHOLD

#: A fixed evaluation moment. Waiver expiry makes validation time-dependent, so every
#: test that asserts reproducibility states the time it used instead of letting the wall
#: clock decide whether the test is deterministic.
_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


@pytest.fixture()
def service(tmp_path) -> DocumentService:
    return DocumentService(SQLiteDocumentStore(tmp_path / "validation.db"), SymbolRegistry())


@pytest.fixture()
def registry() -> SymbolRegistry:
    return SymbolRegistry()


@pytest.fixture()
def profile():
    return load_profile()


@pytest.fixture()
def profile_factory():
    """Resolve a profile document, surfacing ProfileError to the test that caused it."""

    def build(document: ValidationProfile):
        return resolve_profile(document, source=document.profile_id)

    return build


def _correlated(issue: ValidationIssue) -> tuple:
    """The parity unit: one finding, with its references attached to its own code."""

    return (
        issue.code,
        issue.severity,
        tuple(sorted(issue.object_ids)),
        tuple(sorted(issue.element_ids)),
    )


def _advance_revision(service: DocumentService, document) -> None:
    """Move a document forward by one real governed transaction.

    Used wherever a test needs a *different revision* but no interesting content change:
    an empty operation list is not a legal transaction, and inventing one would test a
    path the product does not have.
    """

    service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=document.revision,
            operations=[
                AddElementOperation(
                    element=SymbolElement(
                        id="sym_marker",
                        symbol_key="gate_valve",
                        label="HV-199",
                        position=Point(x=10, y=90),
                        width=30,
                        height=30,
                    )
                )
            ],
            label="advance revision",
        ),
    )


def _document_with_a_duplicate_tag(service: DocumentService):
    """A drawing with two symbols sharing a tag, plus a dangling pipeline."""

    document = service.create_document(CreateDocumentRequest(name="Review fixture"))
    symbols = [
        SymbolElement(
            id="sym_a",
            symbol_key="gate_valve",
            label="HV-101",
            position=Point(x=10, y=10),
            width=30,
            height=30,
        ),
        SymbolElement(
            id="sym_b",
            symbol_key="gate_valve",
            label="HV-101",
            position=Point(x=80, y=10),
            width=30,
            height=30,
        ),
    ]
    operations = [AddElementOperation(element=symbol) for symbol in symbols]
    return service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=document.revision, operations=operations, label="fixture"
        ),
    ).document


# -- parity with the pre-M4 validators -------------------------------------- #


def test_diagram_quality_adapter_reproduces_the_legacy_findings(
    service: DocumentService, registry: SymbolRegistry, profile
) -> None:
    document = _document_with_a_duplicate_tag(service)
    legacy = analyze_diagram_quality(document, registry)
    result = run_validation(document, registry, profile)
    adapted = [issue for issue in result.issues if issue.validator_id == "diagram-quality"]

    # One correlated tuple per finding, compared as a multiset. Comparing codes and
    # references as separate multisets would pass even if two findings swapped the
    # elements they point at - the exact failure a reviewer cannot see.
    assert sorted(_correlated(issue) for issue in adapted) == sorted(
        (issue.code, issue.severity, (), tuple(sorted(issue.element_ids)))
        for issue in legacy.issues
    )


def test_engineering_graph_adapter_reproduces_the_legacy_findings(
    service: DocumentService, registry: SymbolRegistry, profile
) -> None:
    document = _document_with_a_duplicate_tag(service)
    graph = build_engineering_graph(document, registry)
    result = run_validation(document, registry, profile)
    adapted = [issue for issue in result.issues if issue.validator_id == "engineering-graph"]

    assert sorted(_correlated(issue) for issue in adapted) == sorted(
        (
            finding.code,
            finding.severity,
            tuple(sorted(finding.object_ids)),
            tuple(sorted(finding.element_ids)),
        )
        for finding in graph.findings
    )


def test_engineering_report_adapter_reproduces_the_legacy_findings(
    service: DocumentService, registry: SymbolRegistry, profile
) -> None:
    document = _document_with_a_duplicate_tag(service)
    report = build_engineering_report(document, registry, scope="all")
    result = run_validation(document, registry, profile)
    adapted = [issue for issue in result.issues if issue.validator_id == "engineering-report"]

    assert sorted(_correlated(issue) for issue in adapted) == sorted(
        (finding.code, finding.severity, (), tuple(sorted(finding.element_ids)))
        for finding in report.findings
    )


# -- contract --------------------------------------------------------------- #


def test_every_canonical_field_is_present(
    service: DocumentService, registry: SymbolRegistry, profile
) -> None:
    document = _document_with_a_duplicate_tag(service)
    result = run_validation(document, registry, profile)

    assert result.issues, "the fixture must produce findings"
    for issue in result.issues:
        payload = issue.model_dump(mode="json")
        for field in (
            "code",
            "severity",
            "object_ids",
            "element_ids",
            "message",
            "expected",
            "actual",
            "suggested_repair",
            "rule_source",
            "waiver_status",
            "validator_id",
            "rule_id",
            "profile_id",
            "profile_version",
            "details",
        ):
            assert field in payload, f"{field} is missing from the canonical issue"
    assert {issue.severity for issue in result.issues} <= {"info", "warning", "error", "blocker"}


def test_output_order_is_deterministic(
    service: DocumentService, registry: SymbolRegistry, profile
) -> None:
    document = _document_with_a_duplicate_tag(service)
    first = run_validation(document, registry, profile, now=_NOW)
    second = run_validation(document, registry, profile, now=_NOW)

    assert first.result_hash == second.result_hash
    assert [issue.model_dump(mode="json") for issue in first.issues] == [
        issue.model_dump(mode="json") for issue in second.issues
    ]
    order = [(issue.severity, issue.code) for issue in first.issues]
    ranks = {"blocker": 0, "error": 1, "warning": 2, "info": 3}
    assert order == sorted(order, key=lambda item: (ranks[item[0]], item[1]))


def test_a_different_revision_changes_the_result_hash(
    service: DocumentService, registry: SymbolRegistry, profile
) -> None:
    document = _document_with_a_duplicate_tag(service)
    before = validate_document(service, document.id, profile, now=_NOW)
    _advance_revision(service, document)
    after = validate_document(service, document.id, profile, now=_NOW)

    assert after.revision > before.revision
    assert after.result_hash != before.result_hash


def test_validation_binds_revision_content_and_rule_bundle(
    service: DocumentService, registry: SymbolRegistry, profile
) -> None:
    document = _document_with_a_duplicate_tag(service)
    result = validate_document(service, document.id, profile)

    assert result.document_id == document.id
    assert result.revision == service.get_document(document.id).revision
    assert len(result.content_hash) == 64
    assert result.rule_bundle_fingerprint == profile.fingerprint
    assert sorted(result.validators_run) == [
        "diagram-quality",
        "engineering-graph",
        "engineering-report",
    ]
    assert result.validator_versions["diagram-quality"] == "1"


def test_validation_does_not_mutate_the_drawing(
    service: DocumentService, registry: SymbolRegistry, profile
) -> None:
    document = _document_with_a_duplicate_tag(service)
    before = service.get_document(document.id)
    history_before = [entry.model_dump(mode="json") for entry in service.get_history(document.id)]
    validate_document(service, document.id, profile)

    after = service.get_document(document.id)
    assert after.revision == before.revision
    assert [element.model_dump(mode="json") for element in after.elements] == [
        element.model_dump(mode="json") for element in before.elements
    ]
    # Validation must not append a history entry of its own. "Read-only" is a claim about
    # the store, not about the function signature, so it is checked against the store.
    assert [entry.model_dump(mode="json") for entry in service.get_history(document.id)] == (
        history_before
    )


# -- profiles and precedence ------------------------------------------------ #


def test_default_profile_keeps_every_rule_at_its_legacy_severity(profile) -> None:
    assert profile.rules["engineering-report.TAG_DUPLICATE"].severity == "error"
    assert profile.rules["engineering-report.LINE_TAG_MISSING"].severity == "warning"
    assert (
        profile.rules["diagram-quality.QUALITY_SCORE_BELOW_TARGET"].threshold
        == DEFAULT_QUALITY_SCORE_THRESHOLD
    )
    assert profile.rules["engineering-graph.IR_ORPHAN_LINE"].rule_source == "built-in"


def test_later_layers_win_and_the_source_is_recorded(profile_factory) -> None:
    profile = profile_factory(
        ValidationProfile(
            profile_id="P055",
            profile_version="2.0.0",
            layers=[
                ProfileLayer(
                    layer="standard",
                    source="ISA-5.1",
                    rules=[RuleOverride(rule_id="engineering-report.LINE_TAG_MISSING", severity="error")],
                ),
                ProfileLayer(
                    layer="company",
                    source="MSR",
                    rules=[RuleOverride(rule_id="engineering-report.LINE_TAG_MISSING", enabled=False)],
                ),
            ],
        )
    )

    rule = profile.rules["engineering-report.LINE_TAG_MISSING"]
    assert rule.enabled is False
    assert rule.rule_source == "company:MSR"
    # Untouched rules keep the built-in layer as their source.
    assert profile.rules["engineering-report.TAG_DUPLICATE"].rule_source == "built-in"


def test_a_wildcard_override_applies_to_a_whole_validator(profile_factory) -> None:
    profile = profile_factory(
        ValidationProfile(
            profile_id="P055-lenient",
            profile_version="1",
            layers=[
                ProfileLayer(
                    layer="project",
                    source="project-design-rules",
                    rules=[RuleOverride(rule_id="diagram-quality.*", severity="warning")],
                )
            ],
        )
    )

    assert profile.rules["diagram-quality.NODE_OVERLAP"].severity == "warning"
    assert profile.rules["diagram-quality.NODE_OVERLAP"].rule_source == "project:project-design-rules"
    assert profile.rules["engineering-report.TAG_DUPLICATE"].severity == "error"


def test_a_lowered_threshold_changes_the_quality_verdict(
    service: DocumentService, registry: SymbolRegistry, profile_factory
) -> None:
    # The fixture must actually lose points, otherwise no threshold below 100 can fire and
    # the test would pass for the wrong reason. The duplicate tag costs a deterministic 8.
    document = _document_with_a_duplicate_tag(service)
    score = analyze_diagram_quality(document, registry).score
    assert score < DEFAULT_QUALITY_SCORE_THRESHOLD - 5

    lenient = profile_factory(
        ValidationProfile(
            profile_id="lenient",
            profile_version="1",
            layers=[
                ProfileLayer(
                    layer="project",
                    source="project-design-rules",
                    rules=[
                        RuleOverride(
                            rule_id="diagram-quality.QUALITY_SCORE_BELOW_TARGET",
                            threshold=max(0.0, score - 1.0),
                        )
                    ],
                )
            ],
        )
    )
    strict = profile_factory(
        ValidationProfile(
            profile_id="strict",
            profile_version="1",
            layers=[
                ProfileLayer(
                    layer="project",
                    source="project-design-rules",
                    rules=[
                        RuleOverride(
                            rule_id="diagram-quality.QUALITY_SCORE_BELOW_TARGET",
                            threshold=min(100.0, score + 5.0),
                        )
                    ],
                )
            ],
        )
    )

    lenient_issues = [
        issue
        for issue in run_validation(document, registry, lenient).issues
        if issue.code == "QUALITY_SCORE_BELOW_TARGET"
    ]
    strict_issues = [
        issue
        for issue in run_validation(document, registry, strict).issues
        if issue.code == "QUALITY_SCORE_BELOW_TARGET"
    ]

    assert lenient_issues == []
    assert strict_issues, "raising the threshold must surface the score gate"
    assert strict_issues[0].rule_source == "project:project-design-rules"
    assert strict_issues[0].expected.startswith(">=")


def test_a_layer_out_of_order_is_rejected(profile_factory) -> None:
    with pytest.raises(ProfileError) as excinfo:
        profile_factory(
            ValidationProfile(
                profile_id="bad",
                profile_version="1",
                layers=[
                    ProfileLayer(layer="project", source="project"),
                    ProfileLayer(layer="standard", source="ISA"),
                ],
            )
        )

    assert excinfo.value.code == "profile_layer_out_of_order"


def test_a_duplicated_layer_is_rejected(profile_factory) -> None:
    with pytest.raises(ProfileError) as excinfo:
        profile_factory(
            ValidationProfile(
                profile_id="bad",
                profile_version="1",
                layers=[
                    ProfileLayer(layer="project", source="one"),
                    ProfileLayer(layer="project", source="two"),
                ],
            )
        )

    assert excinfo.value.code == "profile_layer_duplicated"


def test_an_unknown_validator_is_rejected(profile_factory) -> None:
    with pytest.raises(ProfileError) as excinfo:
        profile_factory(
            ValidationProfile(
                profile_id="bad",
                profile_version="1",
                layers=[
                    ProfileLayer(
                        layer="project",
                        source="project",
                        rules=[RuleOverride(rule_id="no-such-validator.*")],
                    )
                ],
            )
        )

    assert excinfo.value.code == "profile_unknown_validator"


def test_overrides_without_permission_are_rejected(profile_factory) -> None:
    with pytest.raises(ProfileError) as excinfo:
        profile_factory(
            ValidationProfile(
                profile_id="bad",
                profile_version="1",
                release_policy=ReleasePolicy(allow_release_phase_overrides=False),
                layers=[
                    ProfileLayer(
                        layer="release-phase",
                        source="drawing-A",
                        rules=[RuleOverride(rule_id="engineering-report.TAG_MISSING", severity="info")],
                    )
                ],
            )
        )

    assert excinfo.value.code == "profile_overrides_not_permitted"


def test_a_threshold_on_a_rule_without_one_is_rejected(profile_factory) -> None:
    with pytest.raises(ProfileError) as excinfo:
        profile_factory(
            ValidationProfile(
                profile_id="bad",
                profile_version="1",
                layers=[
                    ProfileLayer(
                        layer="project",
                        source="project",
                        rules=[
                            RuleOverride(
                                rule_id="diagram-quality.NODE_OVERLAP", threshold=3.0
                            )
                        ],
                    )
                ],
            )
        )

    assert excinfo.value.code == "profile_threshold_not_configurable"


def test_an_unreadable_profile_file_is_an_error_not_a_fallback(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "no-such-profile.json"
    monkeypatch.setenv(profiles.PROFILE_PATH_ENV, str(missing))

    with pytest.raises(ProfileError) as excinfo:
        load_profile()

    assert excinfo.value.code == "profile_unreadable"


def test_a_malformed_profile_file_is_an_error_not_a_fallback(tmp_path, monkeypatch) -> None:
    path = tmp_path / "profile.json"
    path.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv(profiles.PROFILE_PATH_ENV, str(path))

    with pytest.raises(ProfileError) as excinfo:
        load_profile()

    assert excinfo.value.code == "profile_not_json"


def test_a_valid_profile_file_loads_and_fingerprints(tmp_path, monkeypatch) -> None:
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(
            {
                "profile_id": "P055",
                "profile_version": "3",
                "layers": [
                    {
                        "layer": "standard",
                        "source": "ISA-5.1",
                        "rules": [
                            {
                                "rule_id": "engineering-report.TAG_MISSING",
                                "severity": "error",
                            }
                        ],
                    }
                ],
                "waivers": [],
                "release_policy": {"required_validators": ["engineering-graph"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(profiles.PROFILE_PATH_ENV, str(path))

    profile = load_profile()

    assert profile.profile_id == "P055"
    assert profile.profile_version == "3"
    assert len(profile.fingerprint) == 64
    assert profile.rules["engineering-report.TAG_MISSING"].severity == "error"


def test_the_fingerprint_changes_when_a_rule_changes(profile_factory) -> None:
    base = profile_factory(ValidationProfile(profile_id="P", profile_version="1"))
    changed = profile_factory(
        ValidationProfile(
            profile_id="P",
            profile_version="1",
            layers=[
                ProfileLayer(
                    layer="project",
                    source="project",
                    rules=[RuleOverride(rule_id="engineering-report.TAG_MISSING", enabled=False)],
                )
            ],
        )
    )

    assert base.fingerprint != changed.fingerprint


# -- waivers ---------------------------------------------------------------- #


def test_a_waiver_marks_an_issue_without_deleting_it(
    service, registry, profile, profile_factory
) -> None:
    document = _document_with_a_duplicate_tag(service)
    target = next(
        issue
        for issue in run_validation(document, registry, profile_factory(ValidationProfile(profile_id="P", profile_version="1"))).issues
        if issue.code == "TAG_DUPLICATE"
    )

    waived_profile = profile_factory(
        ValidationProfile(
            profile_id="P",
            profile_version="1",
            waivers=[
                Waiver(
                    waiver_id="W-1",
                    rule_id=target.rule_id,
                    actor="chief-engineer",
                    reason="tag renumbered in the next revision",
                    granted_at=_NOW - timedelta(days=2),
                )
            ],
        )
    )
    result = run_validation(document, registry, waived_profile)
    same_code = [issue for issue in result.issues if issue.code == "TAG_DUPLICATE"]

    assert len(same_code) == 1, "a waiver must not remove the issue from the result"
    assert same_code[0].waiver_status == "waived"
    assert same_code[0].waiver is not None and same_code[0].waiver.actor == "chief-engineer"
    assert result.counts.total == run_validation(document, registry, profile).counts.total


def test_an_expired_waiver_stops_applying(service, registry, profile_factory) -> None:
    document = _document_with_a_duplicate_tag(service)
    expired = profile_factory(
        ValidationProfile(
            profile_id="P",
            profile_version="1",
            waivers=[
                Waiver(
                    waiver_id="W-2",
                    rule_id="engineering-report.TAG_DUPLICATE",
                    actor="chief-engineer",
                    reason="temporary",
                    granted_at=_NOW - timedelta(days=30),
                    expires_at=_NOW - timedelta(days=1),
                )
            ],
        )
    )

    result = run_validation(document, registry, expired)
    issue = next(issue for issue in result.issues if issue.code == "TAG_DUPLICATE")

    assert issue.waiver_status == "expired"
    assert issue.waiver is not None and issue.waiver.status == "expired"


def test_a_waiver_past_its_revision_boundary_stops_applying(
    service, registry, profile_factory
) -> None:
    document = _document_with_a_duplicate_tag(service)
    bounded = profile_factory(
        ValidationProfile(
            profile_id="P",
            profile_version="1",
            waivers=[
                Waiver(
                    waiver_id="W-3",
                    rule_id="engineering-report.TAG_DUPLICATE",
                    actor="chief-engineer",
                    reason="reviewed against revision 1",
                    granted_at=_NOW - timedelta(days=2),
                    max_revision=document.revision,
                )
            ],
        )
    )
    at_boundary = run_validation(document, registry, bounded)
    assert next(
        issue for issue in at_boundary.issues if issue.code == "TAG_DUPLICATE"
    ).waiver_status == "waived"

    _advance_revision(service, document)
    later = service.get_document(document.id)
    after_boundary = run_validation(later, registry, bounded)

    assert next(
        issue for issue in after_boundary.issues if issue.code == "TAG_DUPLICATE"
    ).waiver_status == "expired"


def test_a_waiver_scoped_to_another_object_does_not_apply(service, registry, profile_factory) -> None:
    document = _document_with_a_duplicate_tag(service)
    scoped = profile_factory(
        ValidationProfile(
            profile_id="P",
            profile_version="1",
            waivers=[
                Waiver(
                    waiver_id="W-4",
                    rule_id="engineering-report.TAG_DUPLICATE",
                    element_ids=["el_somewhere_else"],
                    actor="chief-engineer",
                    reason="wrong scope",
                    granted_at=_NOW - timedelta(days=2),
                )
            ],
        )
    )

    result = run_validation(document, registry, scoped)
    issue = next(issue for issue in result.issues if issue.code == "TAG_DUPLICATE")

    assert issue.waiver_status == "not_waived"


# -- the release gate ------------------------------------------------------- #


def _blocker_profile(profile_factory, *, waiver: Waiver | None = None):
    """A project that promotes duplicate tags to blockers and gates on blockers."""

    return profile_factory(
        ValidationProfile(
            profile_id="P-blockers",
            profile_version="1",
            release_policy=ReleasePolicy(
                required_validators=["engineering-graph", "engineering-report"],
                fail_on=["blocker"],
            ),
            layers=[
                ProfileLayer(
                    layer="project",
                    source="project-design-rules",
                    rules=[
                        RuleOverride(
                            rule_id="engineering-report.TAG_DUPLICATE", severity="blocker"
                        )
                    ],
                )
            ],
            waivers=[waiver] if waiver else [],
        )
    )


def test_an_unwaived_blocker_makes_a_document_ineligible(
    service, registry, profile_factory
) -> None:
    document = _document_with_a_duplicate_tag(service)
    readiness = assess_release_readiness(
        document, registry, _blocker_profile(profile_factory)
    )

    assert readiness.state == "not_eligible"
    assert readiness.unwaived_blockers
    assert any("unwaived blocker" in reason for reason in readiness.reasons)


def test_a_waived_blocker_makes_a_document_eligible_but_stays_visible(
    service, registry, profile_factory
) -> None:
    document = _document_with_a_duplicate_tag(service)
    waived = _blocker_profile(
        profile_factory,
        waiver=Waiver(
            waiver_id="W-5",
            rule_id="engineering-report.TAG_DUPLICATE",
            actor="chief-engineer",
            reason="tag renumbered in the next revision",
            granted_at=_NOW - timedelta(days=2),
        ),
    )

    readiness = assess_release_readiness(document, registry, waived)

    assert readiness.state == "eligible"
    assert "W-5" in readiness.waivers_considered
    assert readiness.counts.blocker >= 1, "the issue must remain counted after waiving"
    assert readiness.unwaived_blockers == []


def _patch_collector(monkeypatch, validator_id: str, collect) -> None:
    """Swap one validator's collector, keeping the registry otherwise intact."""

    from agentcad import validation_engine

    monkeypatch.setattr(
        validation_engine,
        "VALIDATORS",
        tuple(
            validation_engine.ValidatorDefinition(
                validator_id=definition.validator_id,
                version=definition.version,
                title=definition.title,
                requires=definition.requires,
                collect=(
                    collect if definition.validator_id == validator_id else definition.collect
                ),
            )
            for definition in validation_engine.VALIDATORS
        ),
    )


def test_a_missing_required_validator_fails_closed(
    service, registry, profile_factory, monkeypatch
) -> None:
    document = _document_with_a_duplicate_tag(service)
    profile = profile_factory(
        ValidationProfile(
            profile_id="P",
            profile_version="1",
            release_policy=ReleasePolicy(required_validators=["engineering-report"]),
        )
    )
    def explode(context, effective):
        raise ValidationContextUnavailable(
            "symbol definition missing", code="engineering_report_context_unavailable"
        )

    _patch_collector(monkeypatch, "engineering-report", explode)

    readiness = assess_release_readiness(document, registry, profile)

    assert readiness.state == "not_eligible"
    assert readiness.missing_required_validators == ["engineering-report"]
    skipped = [
        skip for skip in readiness.validators_skipped if skip.validator_id == "engineering-report"
    ]
    assert skipped and skipped[0].code == "engineering_report_context_unavailable"
    assert skipped[0].reason == "symbol definition missing"
    assert any("required validators did not run" in reason for reason in readiness.reasons)


def test_an_unexpected_validator_error_is_not_relabelled_a_skip(
    service, registry, profile_factory, monkeypatch
) -> None:
    """A bug must fail the request, not masquerade as "this validator did not apply"."""

    document = _document_with_a_duplicate_tag(service)
    profile = profile_factory(ValidationProfile(profile_id="P", profile_version="1"))

    def explode(context, effective):
        raise KeyError("a programming mistake, not a missing symbol")

    _patch_collector(monkeypatch, "engineering-report", explode)

    with pytest.raises(KeyError):
        run_validation(document, registry, profile)


def test_readiness_carries_the_evidence_a_reviewer_needs(
    service, registry, profile_factory
) -> None:
    document = _document_with_a_duplicate_tag(service)
    readiness = assess_release_readiness(
        document,
        registry,
        profile_factory(ValidationProfile(profile_id="P-hash", profile_version="9")),
    )

    assert readiness.document_id == document.id
    assert readiness.revision == document.revision
    assert len(readiness.content_hash) == 64
    assert len(readiness.validation_hash) == 64
    assert readiness.profile_id == "P-hash"
    assert readiness.profile_version == "9"
    assert len(readiness.rule_bundle_fingerprint) == 64
    assert readiness.validators_run


def test_readiness_cannot_express_an_approval(service, registry, profile) -> None:
    """The one thing the release validator must be structurally unable to say."""

    document = _document_with_a_duplicate_tag(service)
    readiness = assess_release_readiness(document, registry, profile)
    payload = readiness.model_dump(mode="json")
    forbidden = {
        "approved",
        "ifc",
        "afc",
        "signature",
        "signed_by",
        "approved_by",
        "release_state",
        "approved_at",
    }

    assert forbidden.isdisjoint(payload)
    assert readiness.state in {"eligible", "not_eligible"}
    assert readiness.human_approval_required is True
    # The model refuses unknown fields, so a forged approval cannot be smuggled in either.
    with pytest.raises(ValidationError):
        type(readiness).model_validate({**payload, "approved": True})


def test_validation_result_cannot_be_forged_into_an_approval(service, registry, profile) -> None:
    document = _document_with_a_duplicate_tag(service)
    result = run_validation(document, registry, profile)
    payload = result.model_dump(mode="json")

    assert "approved" not in payload
    with pytest.raises(ValidationError):
        type(result).model_validate({**payload, "release_state": "AFC"})


def test_the_result_hash_is_a_hash_of_the_canonical_result(service, registry, profile) -> None:
    document = _document_with_a_duplicate_tag(service)
    result = run_validation(document, registry, profile)
    payload = result.model_dump(mode="json", exclude={"result_hash"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    assert result.result_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_an_issue_model_is_frozen_and_strict() -> None:
    issue = ValidationIssue(
        code="X",
        severity="info",
        message="m",
        validator_id="diagram-quality",
        rule_id="diagram-quality.X",
    )

    with pytest.raises(ValidationError):
        issue.severity = "error"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ValidationIssue.model_validate(
            {
                "code": "X",
                "severity": "info",
                "message": "m",
                "validator_id": "diagram-quality",
                "rule_id": "diagram-quality.X",
                "unexpected": 1,
            }
        )


def test_simple_dxf_import_validates_without_findings(
    service: DocumentService, registry: SymbolRegistry, profile
) -> None:
    """A clean fixture must not produce noise: controlled false positives matter."""

    from agentcad.cad_import import CadImporter
    from agentcad.cad_models import CadImportOptions

    importer = CadImporter(service)
    result = importer.import_bytes(simple_dxf(), filename="clean.dxf", options=CadImportOptions())
    validation = validate_document(service, result.document_id, profile)

    assert validation.counts.blocker == 0
    assert validation.counts.error == 0


def test_resolve_profile_is_pure() -> None:
    profile = resolve_profile(built_in_profile(), source="built-in")

    assert profile.rules["engineering-report.TAG_MISSING"].enabled is True
    assert profile.fingerprint == resolve_profile(built_in_profile()).fingerprint


# -- round-2 gate fixes ----------------------------------------------------- #
#
# Each test below pins one item the remote gate required before the M4-1/2/3 commit.
# They are boundary tests: they fail if the fix is reverted, not if wording changes.


def test_an_unknown_exact_rule_is_rejected(profile_factory) -> None:
    """A typo must be loud. There is no "unknown exact rule" escape hatch."""

    with pytest.raises(ProfileError) as excinfo:
        profile_factory(
            ValidationProfile(
                profile_id="typo",
                profile_version="1",
                layers=[
                    ProfileLayer(
                        layer="project",
                        source="project-design-rules",
                        rules=[RuleOverride(rule_id="diagram-quality.DUPLICATE_LABELS")],
                    )
                ],
            )
        )

    assert excinfo.value.code == "profile_unknown_rule"


def test_an_unknown_required_validator_is_rejected(profile_factory) -> None:
    with pytest.raises(ProfileError) as excinfo:
        profile_factory(
            ValidationProfile(
                profile_id="unknown-validator",
                profile_version="1",
                release_policy=ReleasePolicy(required_validators=["engineering-vibes"]),
            )
        )

    assert excinfo.value.code == "profile_unknown_validator"


def test_the_issue_carries_the_effective_threshold(
    service: DocumentService, registry: SymbolRegistry, profile, profile_factory
) -> None:
    """`threshold` is nullable but always present, and it is the *resolved* number."""

    document = _document_with_a_duplicate_tag(service)
    built_in = run_validation(document, registry, profile)
    gate = next(
        (issue for issue in built_in.issues if issue.code == "QUALITY_SCORE_BELOW_TARGET"),
        None,
    )
    other = next(
        issue for issue in built_in.issues if issue.code != "QUALITY_SCORE_BELOW_TARGET"
    )

    assert other.threshold is None, "a rule that compares no number reports no threshold"
    assert gate is not None, "the fixture loses points, so the built-in gate must fire"
    assert gate.threshold == DEFAULT_QUALITY_SCORE_THRESHOLD
    assert gate.expected == ">= 95"
    assert gate.rule_source == "built-in"

    raised = profile_factory(
        ValidationProfile(
            profile_id="raised",
            profile_version="1",
            layers=[
                ProfileLayer(
                    layer="project",
                    source="project-design-rules",
                    rules=[
                        RuleOverride(
                            rule_id="diagram-quality.QUALITY_SCORE_BELOW_TARGET",
                            threshold=97.0,
                        )
                    ],
                )
            ],
        )
    )
    strict_gate = next(
        issue
        for issue in run_validation(document, registry, raised).issues
        if issue.code == "QUALITY_SCORE_BELOW_TARGET"
    )

    assert strict_gate.threshold == 97.0
    assert strict_gate.expected == ">= 97"
    assert strict_gate.rule_source == "project:project-design-rules"


def test_the_waiver_vocabulary_is_not_waived(service, registry, profile) -> None:
    """The public vocabulary is not_waived|waived|expired - never "none"."""

    document = _document_with_a_duplicate_tag(service)
    result = run_validation(document, registry, profile)

    assert {issue.waiver_status for issue in result.issues} == {"not_waived"}
    with pytest.raises(ValidationError):
        ValidationIssue(
            code="X",
            severity="info",
            message="m",
            validator_id="diagram-quality",
            rule_id="diagram-quality.X",
            waiver_status="none",  # type: ignore[arg-type]
        )


def test_waiver_object_scope_changes_the_fingerprint(profile_factory) -> None:
    """Two approvals covering different equipment are not the same rule bundle."""

    def build(object_ids: list[str]):
        return profile_factory(
            ValidationProfile(
                profile_id="P",
                profile_version="1",
                waivers=[
                    Waiver(
                        waiver_id="W-scope",
                        rule_id="engineering-report.TAG_DUPLICATE",
                        object_ids=object_ids,
                        actor="chief-engineer",
                        reason="reviewed",
                        granted_at=_NOW,
                    )
                ],
            )
        )

    assert build([]).fingerprint != build(["OBJ-1"]).fingerprint
    assert build(["OBJ-1"]).fingerprint != build(["OBJ-2"]).fingerprint
    # Scope order is not semantics: the same set fingerprints the same.
    assert build(["OBJ-2", "OBJ-1"]).fingerprint == build(["OBJ-1", "OBJ-2"]).fingerprint


def test_waiver_element_scope_changes_the_fingerprint(profile_factory) -> None:
    def build(element_ids: list[str]):
        return profile_factory(
            ValidationProfile(
                profile_id="P",
                profile_version="1",
                waivers=[
                    Waiver(
                        waiver_id="W-scope",
                        rule_id="engineering-report.TAG_DUPLICATE",
                        element_ids=element_ids,
                        actor="chief-engineer",
                        reason="reviewed",
                        granted_at=_NOW,
                    )
                ],
            )
        )

    assert build([]).fingerprint != build(["sym_a"]).fingerprint
    assert build(["sym_a"]).fingerprint != build(["sym_b"]).fingerprint


def test_loading_the_same_profile_twice_is_stable(profile_factory) -> None:
    """No implicit grant timestamp: the same file must fingerprint the same."""

    document = ValidationProfile(
        profile_id="P",
        profile_version="1",
        waivers=[
            Waiver(
                waiver_id="W-1",
                rule_id="engineering-report.TAG_DUPLICATE",
                actor="chief-engineer",
                reason="reviewed",
                granted_at=_NOW,
            )
        ],
    )

    assert profile_factory(document).fingerprint == profile_factory(document).fingerprint


def test_a_waiver_without_an_explicit_grant_time_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Waiver(  # type: ignore[call-arg]
            waiver_id="W-1",
            rule_id="*",
            actor="chief-engineer",
            reason="missing evidence",
        )


def test_a_naive_waiver_time_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Waiver(
            waiver_id="W-1",
            rule_id="*",
            actor="chief-engineer",
            reason="naive timestamp",
            granted_at=datetime(2026, 9, 19, 12, 0),
        )


def test_a_waiver_expiring_before_it_was_granted_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Waiver(
            waiver_id="W-1",
            rule_id="*",
            actor="chief-engineer",
            reason="impossible window",
            granted_at=_NOW,
            expires_at=_NOW - timedelta(days=1),
        )


def test_an_expired_broad_waiver_does_not_shadow_an_active_specific_one(
    service, registry, profile_factory
) -> None:
    """Declaration order must not decide the verdict."""

    document = _document_with_a_duplicate_tag(service)
    expired = Waiver(
        waiver_id="W-expired",
        rule_id="*",
        actor="chief-engineer",
        reason="a broad waiver that has since lapsed",
        granted_at=_NOW - timedelta(days=90),
        expires_at=_NOW - timedelta(days=30),
    )
    active = Waiver(
        waiver_id="W-active",
        rule_id="engineering-report.TAG_DUPLICATE",
        actor="chief-engineer",
        reason="renumbered next revision",
        granted_at=_NOW - timedelta(days=1),
    )

    for order in ([expired, active], [active, expired]):
        ordered = profile_factory(
            ValidationProfile(
                profile_id="P", profile_version="1", waivers=list(order)
            )
        )
        issue = next(
            issue
            for issue in run_validation(document, registry, ordered).issues
            if issue.code == "TAG_DUPLICATE"
        )
        assert issue.waiver_status == "waived"
        assert issue.waiver is not None and issue.waiver.waiver_id == "W-active"
        assert issue.waiver.selected_from == 2


def test_reordering_equivalent_waivers_changes_nothing(
    service, registry, profile_factory
) -> None:
    document = _document_with_a_duplicate_tag(service)
    first = Waiver(
        waiver_id="W-a",
        rule_id="engineering-report.TAG_DUPLICATE",
        actor="chief-engineer",
        reason="one",
        granted_at=_NOW,
    )
    second = Waiver(
        waiver_id="W-b",
        rule_id="engineering-report.TAG_DUPLICATE",
        actor="chief-engineer",
        reason="two",
        granted_at=_NOW,
    )

    def run(order):
        resolved = profile_factory(
            ValidationProfile(profile_id="P", profile_version="1", waivers=list(order))
        )
        return resolved, run_validation(document, registry, resolved, now=_NOW)

    forward_profile, forward = run([first, second])
    reverse_profile, reverse = run([second, first])

    assert forward_profile.fingerprint == reverse_profile.fingerprint
    assert forward.result_hash == reverse.result_hash
    assert forward.issues == reverse.issues


def test_a_quality_threshold_out_of_domain_is_rejected(profile_factory) -> None:
    def build(threshold: float):
        return profile_factory(
            ValidationProfile(
                profile_id="P",
                profile_version="1",
                layers=[
                    ProfileLayer(
                        layer="project",
                        source="project",
                        rules=[
                            RuleOverride(
                                rule_id="diagram-quality.QUALITY_SCORE_BELOW_TARGET",
                                threshold=threshold,
                            )
                        ],
                    )
                ],
            )
        )

    with pytest.raises(ProfileError) as out_of_range:
        build(140.0)
    assert out_of_range.value.code == "profile_threshold_out_of_range"

    for not_a_number in (float("nan"), float("inf")):
        with pytest.raises((ProfileError, ValidationError)):
            build(not_a_number)


def test_a_fixed_evaluation_time_reproduces_the_result_hash(
    service, registry, profile_factory
) -> None:
    """Wall clock is an input. Same inputs + same as_of => same result."""

    document = _document_with_a_duplicate_tag(service)
    expiring = profile_factory(
        ValidationProfile(
            profile_id="P",
            profile_version="1",
            waivers=[
                Waiver(
                    waiver_id="W-1",
                    rule_id="engineering-report.TAG_DUPLICATE",
                    actor="chief-engineer",
                    reason="short lived",
                    granted_at=_NOW - timedelta(days=1),
                    expires_at=_NOW + timedelta(days=1),
                )
            ],
        )
    )

    first = run_validation(document, registry, expiring, now=_NOW)
    second = run_validation(document, registry, expiring, now=_NOW)
    assert first.result_hash == second.result_hash
    assert first.evaluated_at == _NOW

    later = run_validation(document, registry, expiring, now=_NOW + timedelta(days=2))
    before_issue = next(i for i in first.issues if i.code == "TAG_DUPLICATE")
    after_issue = next(i for i in later.issues if i.code == "TAG_DUPLICATE")

    assert before_issue.waiver_status == "waived"
    assert after_issue.waiver_status == "expired"
    assert later.evaluated_at != first.evaluated_at
    assert later.result_hash != first.result_hash


def test_an_unregistered_adapter_code_is_surfaced_not_dropped(
    service, registry, profile, profile_factory, monkeypatch
) -> None:
    """A code the catalog does not know is a contract error, not a finding to hide."""

    from agentcad.validation_engine import RawIssue

    document = _document_with_a_duplicate_tag(service)

    def emit(context, effective):
        return [
            RawIssue(
                code="NOT_IN_THE_CATALOG",
                severity="error",
                message="a rule nobody registered",
                element_ids=["sym_a"],
            )
        ]

    _patch_collector(monkeypatch, "engineering-report", emit)

    result = run_validation(document, registry, profile)
    issue = next(i for i in result.issues if i.code == "NOT_IN_THE_CATALOG")

    assert issue.registered is False
    assert issue.rule_source == "unregistered"
    assert result.counts.unregistered == 1

    readiness = assess_release_readiness(document, registry, profile)
    assert readiness.state == "not_eligible"
    assert readiness.unregistered_codes == ["engineering-report.NOT_IN_THE_CATALOG"]
