"""The canonical validation contract (Charter §43 Validator DoD, §33, §49 M4).

Before this module the repository had **three** unrelated ways of saying "something is
wrong with this drawing": ``DiagramQualityIssue`` (warning/error only), ``RuleFinding``
(info/warning/error) and ``GraphFinding`` (its own ``object_ids``/``element_ids`` pair).
Every one of them was a defensible local choice and none of them was the contract, so a
reviewer could not ask one question across all three, and nothing bound an issue to the
rule, profile and revision that produced it.

This module is that contract, and it is deliberately *only* a contract: it defines the
shape of an issue, the shape of a project profile and the shape of a release-readiness
verdict. The rules themselves keep living where they already live (see
``validation_engine`` adapters) — standardising an output must not silently change a
conclusion. The one reviewed exception is ``QUALITY_SCORE_BELOW_TARGET``, whose *decision*
was intentionally migrated to the canonical engine because a configurable threshold that
does not decide anything is a knob that can lie (remote baseline R2 §4).

Design rules that are load-bearing:

* ``code`` is machine-stable. Human wording may improve; a code's meaning may not change
  without a documented migration.
* ``object_ids`` are canonical engineering-object identities (M2), ``element_ids`` are
  drawing locators. They are **not** alternatives: an issue that knows both carries both.
* ``expected`` / ``actual`` always exist as strings (``""`` when not applicable) so the
  schema does not need a second variant for "no expected value".
* ``threshold`` is nullable but always present, so "this finding was decided against 97"
  is part of the evidence rather than a field that only some issues happen to carry.
* Time is an input, not ambient state. Waivers expire, so every result binds the
  ``evaluated_at`` moment it used; otherwise the same inputs could produce two different
  verdicts with no way to tell why (remote baseline R2 P0-5).
* Output order is part of the contract (``canonical_issue_key``), because a
  deterministic editor whose JSON reorders itself is not reproducible in review.
* Validation is a pure read: nothing here mutates a drawing. A separate, named workflow
  records evidence; validation itself never does.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ValidationSeverity = Literal["info", "warning", "error", "blocker"]

#: The approved public vocabulary. ``not_waived`` is deliberately spelled out rather than
#: ``none``: an issue is never "nothing", it is "not waived", and the two words differ in
#: exactly the case a reviewer cares about.
WaiverStatus = Literal["not_waived", "waived", "expired"]

#: ``release-phase`` is the normative name of the last, most specific layer. The same word
#: is used in the model, the resolver and the documentation, because two names for one
#: precedence layer is how a rule ends up enforced in one place and described in another.
ProfileLayerName = Literal["built-in", "standard", "company", "project", "release-phase"]
ReleaseReadinessState = Literal["eligible", "not_eligible"]

#: Most severe first. Used for ordering and for counting, so it is defined once.
SEVERITY_ORDER: tuple[ValidationSeverity, ...] = ("blocker", "error", "warning", "info")
SEVERITY_RANK: dict[ValidationSeverity, int] = {
    severity: rank for rank, severity in enumerate(SEVERITY_ORDER)
}

#: The precedence chain from Charter §12 (profiles: §13), weakest first. A profile's
#: layer list must be a strictly increasing subsequence of this chain: reordering it
#: would silently change which standard wins, so it is rejected rather than sorted.
PROFILE_PRECEDENCE: tuple[ProfileLayerName, ...] = (
    "built-in",
    "standard",
    "company",
    "project",
    "release-phase",
)
LAYER_RANK: dict[ProfileLayerName, int] = {
    layer: rank for rank, layer in enumerate(PROFILE_PRECEDENCE)
}


def _require_timezone_aware(value: datetime, field_name: str) -> datetime:
    """Naive datetimes are rejected: an expiry without a zone is not an expiry."""

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Waiver(ContractModel):
    """An accepted deviation. A waiver never deletes an issue (Charter §12).

    The issue stays in the deterministic result and carries ``waiver_status`` plus this
    evidence, so "who accepted this and why" is answerable later. Waivers are declared in
    the *server-side* project profile, never in a request body: a waiver an Agent could
    add itself would be a disguised self-approval of a release (Charter §44-10).

    ``granted_at`` has **no default**. A timestamp generated while loading a profile would
    make the same file fingerprint differently on every load, and it would invent approval
    evidence nobody signed — so the profile author states it, and the loader only reads it.
    """

    waiver_id: str = Field(min_length=1)
    #: Which rule it covers; ``*`` covers every rule (discouraged, but expressible).
    rule_id: str = "*"
    #: Optional narrowing to one issue code.
    code: str = "*"
    #: Optional narrowing to specific engineering objects / drawing elements.
    object_ids: list[str] = Field(default_factory=list)
    element_ids: list[str] = Field(default_factory=list)
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    granted_at: datetime
    expires_at: datetime | None = None
    #: A revision boundary: the waiver stops applying once the drawing moves past it,
    #: because the deviation was reviewed against *that* revision.
    max_revision: int | None = None

    @field_validator("granted_at", "expires_at")
    @classmethod
    def _aware(cls, value: datetime | None, info) -> datetime | None:
        if value is None:
            return None
        return _require_timezone_aware(value, info.field_name)

    def model_post_init(self, __context: Any) -> None:
        if self.expires_at is not None and self.expires_at <= self.granted_at:
            raise ValueError(
                f"waiver {self.waiver_id!r} expires at or before it was granted; "
                "a waiver that never applied is a configuration error, not a finding"
            )

    def matches(self, issue: ValidationIssue) -> bool:  # noqa: F821 - forward ref
        if self.rule_id not in {"*", issue.rule_id}:
            return False
        if self.code not in {"*", issue.code}:
            return False
        if self.object_ids and not set(self.object_ids) & set(issue.object_ids):
            return False
        if self.element_ids and not set(self.element_ids) & set(issue.element_ids):
            return False
        return True

    def is_active(self, *, revision: int, now: datetime) -> bool:
        if self.expires_at is not None and now >= self.expires_at:
            return False
        return self.max_revision is None or revision <= self.max_revision


class WaiverEvidence(ContractModel):
    """Why an issue is (or is not) waived, including the scope that matched.

    The scope is part of the evidence, not just of the waiver: a reviewer reading a
    waived finding needs to see that the approval covered *these* elements.
    """

    waiver_id: str
    actor: str
    reason: str
    granted_at: datetime
    expires_at: datetime | None = None
    max_revision: int | None = None
    object_ids: list[str] = Field(default_factory=list)
    element_ids: list[str] = Field(default_factory=list)
    status: WaiverStatus
    #: True when more than one waiver matched and this one was selected deterministically.
    selected_from: int = 1


class ValidationIssue(ContractModel):
    """One finding, in the one shape every validator must produce."""

    code: str = Field(min_length=1)
    severity: ValidationSeverity
    #: Canonical engineering-object identities (equipment/line/instrument/…), if known.
    object_ids: list[str] = Field(default_factory=list)
    #: Drawing locators (element ids). May coexist with ``object_ids``.
    element_ids: list[str] = Field(default_factory=list)
    message: str = Field(min_length=1)
    expected: str = ""
    actual: str = ""
    suggested_repair: str = ""
    #: Which layer of the profile chain supplied the rule that is in force here, or
    #: ``"unregistered"`` when the adapter emitted a code the catalog does not know.
    rule_source: str = "built-in"
    #: False when this code is not in the rule catalog. An unregistered finding is still a
    #: finding: it is surfaced and marked, never dropped and never disguised as built-in.
    registered: bool = True
    #: The effective numeric threshold a thresholded rule decided against; ``None`` for
    #: rules that do not compare numbers.
    threshold: float | None = None
    waiver_status: WaiverStatus = "not_waived"
    waiver: WaiverEvidence | None = None
    validator_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    profile_id: str = ""
    profile_version: str = ""
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_waived(self) -> bool:
        return self.waiver_status == "waived"


class ValidatorSkip(ContractModel):
    """A validator that did not run, and why. "Not run" is never "passed".

    ``code`` is the stable machine key a surface may branch on (``context_unavailable``,
    ``validator_error``); ``reason`` is the human text. Both exist because a skip that is
    only a sentence cannot be asserted in a test or handled by a client.
    """

    validator_id: str
    code: str = "context_unavailable"
    reason: str = ""


class ValidationCounts(ContractModel):
    blocker: int = Field(default=0, ge=0)
    error: int = Field(default=0, ge=0)
    warning: int = Field(default=0, ge=0)
    info: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    waived: int = Field(default=0, ge=0)
    unregistered: int = Field(default=0, ge=0)


class ValidationResult(ContractModel):
    """The deterministic result of one validation run.

    Everything a reviewer needs in order to reproduce it is bound in: which revision and
    content hash was validated, which profile and rule bundle were in force, which
    validator versions ran, which symbol catalog was consulted, and at what moment the
    time-dependent parts (waiver expiry) were evaluated.
    """

    schema_name: Literal["pid-agent.validation-result"] = Field(
        default="pid-agent.validation-result", alias="schema"
    )
    version: Literal[1] = 1
    document_id: str
    revision: int = Field(ge=0)
    content_hash: str = ""
    project_id: str = ""
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    rule_bundle_fingerprint: str = ""
    #: Identity of the inputs that are *not* the document: the engine, the symbol catalog
    #: validators consult, and the moment waivers were evaluated against.
    engine_version: str = ""
    symbol_registry_fingerprint: str = ""
    evaluated_at: datetime
    validator_versions: dict[str, str] = Field(default_factory=dict)
    validators_run: list[str] = Field(default_factory=list)
    validators_skipped: list[ValidatorSkip] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)
    counts: ValidationCounts = Field(default_factory=ValidationCounts)
    #: Hash over the canonical form of this result (without the hash itself). Same
    #: document + same profile + same rules + same evaluation time => same hash, and a
    #: changed hash means something in the inputs changed. Determinism evidence, not a
    #: promise.
    result_hash: str = ""

    @property
    def has_blockers(self) -> bool:
        return any(
            issue.severity == "blocker" and not issue.is_waived for issue in self.issues
        )

    def unwaived(self, severity: ValidationSeverity) -> list[ValidationIssue]:
        return [
            issue
            for issue in self.issues
            if issue.severity == severity and not issue.is_waived
        ]

    def unregistered(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if not issue.registered]


class ReleasePolicy(ContractModel):
    """What the project requires before a release *may be requested* (Charter §22)."""

    #: Validator ids that must have run for a formal release. A required validator that
    #: did not run fails the gate closed — absence of evidence is not evidence.
    required_validators: list[str] = Field(default_factory=list)
    #: Severities that make a release ineligible while unwaived.
    fail_on: list[ValidationSeverity] = Field(default_factory=lambda: ["blocker"])
    #: Release-phase (drawing-specific) overrides are the last, most specific layer of the
    #: chain. They are only permitted when the project profile says so (Charter §12).
    allow_release_phase_overrides: bool = False


class ReleaseReadiness(ContractModel):
    """An automated *readiness* verdict. It is not an approval and cannot become one.

    ``state`` is ``eligible`` or ``not_eligible`` — there is deliberately no field that
    could express ``Approved`` / ``IFC`` / ``AFC`` / a signature / a human sign-off, so no
    code path can produce one by accident, and nothing here is reachable as a write.
    Formal state transitions stay behind the human Approval Gate (Charter §11, §44-10).
    """

    schema_name: Literal["pid-agent.release-readiness"] = Field(
        default="pid-agent.release-readiness", alias="schema"
    )
    version: Literal[1] = 1
    document_id: str
    document_name: str = ""
    revision: int = Field(ge=0)
    content_hash: str = ""
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    rule_bundle_fingerprint: str = ""
    validation_hash: str = ""
    #: Same binding the validation result carries, so readiness is reproducible too.
    engine_version: str = ""
    release_validator_version: str = ""
    symbol_registry_fingerprint: str = ""
    evaluated_at: datetime
    state: ReleaseReadinessState
    reasons: list[str] = Field(default_factory=list)
    required_validators: list[str] = Field(default_factory=list)
    validators_run: list[str] = Field(default_factory=list)
    validators_skipped: list[ValidatorSkip] = Field(default_factory=list)
    missing_required_validators: list[str] = Field(default_factory=list)
    counts: ValidationCounts = Field(default_factory=ValidationCounts)
    unwaived_blockers: list[str] = Field(default_factory=list)
    unwaived_failures: list[str] = Field(default_factory=list)
    waivers_considered: list[str] = Field(default_factory=list)
    unregistered_codes: list[str] = Field(default_factory=list)
    policy: ReleasePolicy = Field(default_factory=ReleasePolicy)
    #: Always true: the automated verdict never grants a formal release state. Present so
    #: a consumer cannot mistake readiness for approval, and so tests can assert it.
    human_approval_required: Literal[True] = True
    #: Hash over the canonical form of this readiness payload, so a surface that persists
    #: or audits it can bind the exact payload it saw.
    readiness_hash: str = ""


def canonical_issue_key(issue: ValidationIssue) -> tuple[Any, ...]:
    """The one documented ordering for issues.

    Severity first (a reviewer reads blockers), then the stable code, then the locators,
    then the wording, then the validator. No dictionary iteration or set ordering leaks
    into the output, so two runs of the same inputs serialise byte-identically.
    """

    return (
        SEVERITY_RANK[issue.severity],
        issue.code,
        tuple(issue.object_ids),
        tuple(issue.element_ids),
        issue.message,
        issue.validator_id,
    )


def sort_issues(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    return sorted(issues, key=canonical_issue_key)


__all__ = [
    "LAYER_RANK",
    "PROFILE_PRECEDENCE",
    "SEVERITY_ORDER",
    "SEVERITY_RANK",
    "ProfileLayerName",
    "ReleasePolicy",
    "ReleaseReadiness",
    "ReleaseReadinessState",
    "ValidationCounts",
    "ValidationIssue",
    "ValidationResult",
    "ValidationSeverity",
    "ValidatorSkip",
    "Waiver",
    "WaiverEvidence",
    "WaiverStatus",
    "canonical_issue_key",
    "sort_issues",
]
