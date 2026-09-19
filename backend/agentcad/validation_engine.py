"""The validator registry, the adapters, and the run that produces one canonical result.

Three validators exist today, and all three are **adapters**: they run the checks the
repository already had (diagram quality, engineering IR graph, engineering report rules)
and restate their findings in the canonical contract from ``validation_models``. No rule
conclusion was rewritten, which is the point — the remote baseline (§7) requires interface
standardisation first, and a changed conclusion is a rule change, reviewable as one.

Two things this engine refuses to do, because both would quietly turn validation into a
liar:

* **Skip silently.** A validator whose context cannot be built is recorded in
  ``validators_skipped`` with the reason. "Not applicable" is not "passed"; the release
  gate treats a missing *required* validator as a failure (Charter §22).
* **Delete an issue because of a waiver.** A waiver annotates an issue. The issue stays in
  the deterministic result with ``waiver_status`` and the evidence, so "what is actually
  wrong" and "what we agreed to live with" never collapse into one number.

Validation is read-only: it builds documents in memory, never writes, and the REST/MCP/CLI
surfaces that expose it are registered as reads.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .diagram_quality import analyze_diagram_quality
from .diagram_quality_models import DiagramQualityReport
from .drafting_geometry import drafting_content_hash
from .engineering_ir import EngineeringGraph, build_engineering_graph
from .engineering_reports import EngineeringReport, build_engineering_report
from .models import Document
from .service import DocumentService
from .symbols import SymbolRegistry
from .validation_models import (
    ValidationCounts,
    ValidationIssue,
    ValidationResult,
    ValidationSeverity,
    ValidatorSkip,
    Waiver,
    WaiverEvidence,
    WaiverStatus,
    canonical_digest,
    sort_issues,
)
from .validation_profile import EffectiveProfile
from .validation_rules import DEFAULT_QUALITY_SCORE_THRESHOLD, rule_id_for

VALIDATION_ENGINE_VERSION = "1"


class ValidationTimeError(ValueError):
    """A caller asked validation to evaluate "now" without saying in which "now".

    The time contract belongs to the engine, not to its three current callers: waivers
    have a lower *and* an upper temporal bound, and ``evaluated_at`` is hashed, so a
    naive datetime is not a formatting detail — it is an unanswerable question (remote
    baseline R3 P0-2). ``code`` is the stable key a surface maps onto its own framing.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_evaluation_time(moment: datetime | None) -> datetime:
    """The engine's one answer to "when is this validation happening?".

    ``None`` means now. An aware timestamp is converted to UTC, so the same instant
    written as ``12:00:00Z`` and as ``08:00:00-04:00`` produces one ``evaluated_at`` and
    therefore one canonical ``result_hash``. A naive timestamp is refused: without a zone
    it names a different instant on every machine, and comparing it against a waiver's
    bounds would either raise a raw ``TypeError`` or, worse, silently agree.
    """

    if moment is None:
        return datetime.now(UTC)
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValidationTimeError(
            "as_of_not_timezone_aware",
            "the evaluation time must be timezone-aware, e.g. 2026-09-19T12:00:00Z",
        )
    return moment.astimezone(UTC)


class ValidationContextUnavailable(RuntimeError):
    """A validator could not build the context it declares it needs.

    Raised by the *context builders*, where the legacy failure mode is known (a drawing
    that references a symbol the catalog no longer has). Raising it there and catching
    only it in the run keeps "this validator was not applicable" apart from "this code is
    broken": an unexpected exception now fails the request instead of being relabelled a
    skip.

    ``code`` is the stable machine key the canonical ``ValidatorSkip`` publishes; the
    reason is the human sentence. Both are required, because a skip that is only a
    sentence cannot be asserted or handled.
    """

    def __init__(self, reason: str, *, code: str = "context_unavailable"):
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass
class RawIssue:
    """What an adapter produces before the profile and waivers are applied."""

    code: str
    severity: ValidationSeverity
    message: str
    object_ids: list[str] = field(default_factory=list)
    element_ids: list[str] = field(default_factory=list)
    expected: str = ""
    actual: str = ""
    suggested_repair: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    #: Set by rules whose decision depends on a number (today: the quality score gate).
    threshold: float | None = None


@dataclass
class ValidationContext:
    """Everything a validator may look at. Built once per run, lazily per domain."""

    document: Document
    registry: SymbolRegistry
    service: DocumentService | None = None
    project_id: str = ""
    _quality: DiagramQualityReport | None = None
    _graph: EngineeringGraph | None = None
    _report: EngineeringReport | None = None

    def quality(self) -> DiagramQualityReport:
        """Drafting-quality analysis for this document.

        There is deliberately **no** ``except`` here. The legacy analyzers already handle
        the one context condition they know about (a symbol key with no definition becomes
        a finding, not a crash), so catching ``KeyError``/``ValueError`` around a whole
        analyzer call could only ever convert a programming regression into a
        clean-looking "skipped" record — the exact failure R2 flagged and R3 re-flagged
        because the first fix moved the catch instead of removing it. A validator that
        genuinely cannot run raises :class:`ValidationContextUnavailable` itself, at the
        exact lookup that failed.
        """

        if self._quality is None:
            self._quality = analyze_diagram_quality(self.document, self.registry)
        return self._quality

    def graph(self) -> EngineeringGraph:
        """The engineering IR graph. No broad catch: see :meth:`quality`."""

        if self._graph is None:
            self._graph = build_engineering_graph(self.document, self.registry)
        return self._graph

    def report(self) -> EngineeringReport:
        """The engineering report. No broad catch: see :meth:`quality`."""

        if self._report is None:
            self._report = build_engineering_report(
                self.document, self.registry, scope="all"
            )
        return self._report


#: Context keys a validator can require. Kept as strings so a skip reason is readable.
CONTEXT_DOMAINS: tuple[str, ...] = ("diagram_quality", "engineering_graph", "engineering_report")


@dataclass(frozen=True)
class ValidatorDefinition:
    validator_id: str
    version: str
    title: str
    requires: tuple[str, ...]
    collect: Callable[[ValidationContext, EffectiveProfile], list[RawIssue]]


# -- adapters --------------------------------------------------------------- #


def _collect_diagram_quality(
    context: ValidationContext, profile: EffectiveProfile
) -> list[RawIssue]:
    """Diagram-quality findings, plus the score gate re-decided against the profile."""

    report = context.quality()
    issues: list[RawIssue] = [
        RawIssue(
            code=issue.code,
            severity=issue.severity,
            message=issue.message,
            element_ids=list(issue.element_ids),
            details=dict(issue.details),
        )
        for issue in report.issues
        # The one threshold rule is *not* inherited from the legacy verdict. The legacy
        # check only knows the built-in number, so carrying its decision over would make a
        # profile that raises the bar silently unable to open the gate. The gate is
        # re-decided below; every other rule conclusion is passed through untouched.
        if issue.code != "QUALITY_SCORE_BELOW_TARGET"
    ]
    issues.extend(_quality_score_gate(report, profile))
    return issues


def _quality_score_gate(
    report: DiagramQualityReport, profile: EffectiveProfile
) -> list[RawIssue]:
    """The only rule whose decision moves with a profile threshold.

    The adapter keeps the *decision* (score vs required score) and takes the number from
    the resolved profile, so raising the bar is a project decision that shows up in the
    result rather than a constant buried in a penalty formula.
    """

    threshold = _threshold_for(
        profile,
        rule_id_for("diagram-quality", "QUALITY_SCORE_BELOW_TARGET"),
        default=DEFAULT_QUALITY_SCORE_THRESHOLD,
    )
    score = float(report.score)
    if score >= threshold:
        return []
    return [
        RawIssue(
            code="QUALITY_SCORE_BELOW_TARGET",
            severity="error",
            message=f"diagram drafting-quality score {score:g} is below the required {threshold:g}",
            expected=f">= {threshold:g}",
            actual=f"{score:g}",
            suggested_repair=(
                "run the deterministic drafting engine, or accept the deviation "
                "explicitly with a waiver"
            ),
            details={"score": score, "target_score": threshold},
            threshold=threshold,
        )
    ]


def _collect_engineering_graph(
    context: ValidationContext, profile: EffectiveProfile
) -> list[RawIssue]:
    """Engineering-IR findings: stable object ids *and* drawing locators."""

    graph = context.graph()
    return [
        RawIssue(
            code=finding.code,
            severity=finding.severity,
            message=finding.message,
            object_ids=list(finding.object_ids),
            element_ids=list(finding.element_ids),
            details=dict(finding.details),
        )
        for finding in graph.findings
    ]


def _collect_engineering_report(
    context: ValidationContext, profile: EffectiveProfile
) -> list[RawIssue]:
    """Engineering-report rules: tag/line/port/endpoint checks."""

    report = context.report()
    return [
        RawIssue(
            code=finding.code,
            severity=finding.severity,
            message=finding.message,
            element_ids=list(finding.element_ids),
            details=dict(finding.details),
        )
        for finding in report.findings
    ]


#: The registry. Ordered by id so ``validators_run`` reads the same way twice.
VALIDATORS: tuple[ValidatorDefinition, ...] = tuple(
    sorted(
        (
            ValidatorDefinition(
                validator_id="diagram-quality",
                version="1",
                title="Drawing quality (geometry, routing, annotation placement)",
                requires=("diagram_quality",),
                collect=_collect_diagram_quality,
            ),
            ValidatorDefinition(
                validator_id="engineering-graph",
                version="1",
                title="Engineering semantic graph (identity, topology, off-page links)",
                requires=("engineering_graph",),
                collect=_collect_engineering_graph,
            ),
            ValidatorDefinition(
                validator_id="engineering-report",
                version="1",
                title="Engineering report rules (tags, lines, ports, endpoints)",
                requires=("engineering_report",),
                collect=_collect_engineering_report,
            ),
        ),
        key=lambda definition: definition.validator_id,
    )
)

VALIDATORS_BY_ID: dict[str, ValidatorDefinition] = {
    definition.validator_id: definition for definition in VALIDATORS
}


def _threshold_for(profile: EffectiveProfile, rule_id: str, *, default: float) -> float:
    rule = profile.rules.get(rule_id)
    if rule is None or rule.threshold is None:
        return default
    return float(rule.threshold)


# -- the run ---------------------------------------------------------------- #


def _waiver_evidence(
    waiver: Waiver, *, status: WaiverStatus, matched: int
) -> WaiverEvidence:
    return WaiverEvidence(
        waiver_id=waiver.waiver_id,
        actor=waiver.actor,
        reason=waiver.reason,
        granted_at=waiver.granted_at,
        expires_at=waiver.expires_at,
        max_revision=waiver.max_revision,
        object_ids=sorted(waiver.object_ids),
        element_ids=sorted(waiver.element_ids),
        status=status,
        selected_from=matched,
    )


def _waiver_status(
    issue: ValidationIssue, waivers: Sequence[Waiver], *, revision: int, now: datetime
) -> tuple[WaiverStatus, WaiverEvidence | None]:
    """Decide one issue's waiver state from *all* matching waivers.

    Returning on the first match would let declaration order decide the verdict: an
    expired broad waiver listed before an active narrow one would shadow it, and a
    document would read as expired because of where a line sits in a file. Active matches
    win; within a class the selection is by ``waiver_id``, so the same profile always
    yields the same evidence (remote baseline R2 P0-4).
    """

    matching = sorted(
        (waiver for waiver in waivers if waiver.matches(issue)),
        key=lambda item: item.waiver_id,
    )
    if not matching:
        return "not_waived", None
    active = [waiver for waiver in matching if waiver.is_active(revision=revision, now=now)]
    if active:
        return "waived", _waiver_evidence(active[0], status="waived", matched=len(matching))
    # Nothing is active. The remaining question is *why*, and the two answers are not
    # interchangeable: a waiver that has been granted and then lapsed (by expiry or by
    # the revision moving on) is `expired` evidence, while a waiver granted in the future
    # has not applied yet and must stay `not_waived`. Reporting the second as the first
    # would put an approval in the audit trail that nobody ever gave (R3 P0-1).
    lapsed = [waiver for waiver in matching if waiver.was_granted_by(now=now)]
    if lapsed:
        return "expired", _waiver_evidence(lapsed[0], status="expired", matched=len(matching))
    return "not_waived", None


def _count(issues: Iterable[ValidationIssue]) -> ValidationCounts:
    counts = {"blocker": 0, "error": 0, "warning": 0, "info": 0}
    waived = 0
    total = 0
    unregistered = 0
    for issue in issues:
        counts[issue.severity] += 1
        total += 1
        if issue.is_waived:
            waived += 1
        if not issue.registered:
            unregistered += 1
    return ValidationCounts(**counts, total=total, waived=waived, unregistered=unregistered)


def _effective_rule(profile: EffectiveProfile, validator_id: str, code: str):
    """The rule in force for one issue code, and its stable id.

    ``None`` means the adapter emitted a code the catalog does not know. That is a
    contract error to *surface*, not a finding to drop and not a rule to invent.
    """

    rule_id = rule_id_for(validator_id, code)
    return profile.rules.get(rule_id), rule_id


def run_validation(
    document: Document,
    registry: SymbolRegistry,
    profile: EffectiveProfile,
    *,
    project_id: str = "",
    service: DocumentService | None = None,
    now: datetime | None = None,
) -> ValidationResult:
    """Validate one document, deterministically, without writing anything.

    ``now`` is normalized here rather than by the caller: the engine owns the meaning of
    the evaluation instant, so REST, CLI, MCP and any future surface cannot each invent a
    slightly different time contract (R3 P0-2).
    """

    moment = normalize_evaluation_time(now)
    context = ValidationContext(
        document=document, registry=registry, service=service, project_id=project_id
    )
    issues: list[ValidationIssue] = []
    ran: list[str] = []
    skipped: list[ValidatorSkip] = []
    for definition in VALIDATORS:
        try:
            raw_issues = definition.collect(context, profile)
        except ValidationContextUnavailable as exc:
            # A validator that cannot build its own context is *skipped with a reason*,
            # never reported as clean: a drawing that references a symbol the library no
            # longer has is exactly the case where claiming "no findings" would be worst.
            # Only this error is caught. A programming mistake must fail the request
            # rather than disguise itself as a validator that was not applicable.
            skipped.append(
                ValidatorSkip(
                    validator_id=definition.validator_id,
                    code=exc.code,
                    reason=exc.reason,
                )
            )
            continue
        ran.append(definition.validator_id)
        for raw in raw_issues:
            rule, rule_id = _effective_rule(profile, definition.validator_id, raw.code)
            if rule is not None and not rule.enabled:
                continue
            registered = rule is not None
            issue = ValidationIssue(
                code=raw.code,
                severity=rule.severity if rule is not None else raw.severity,
                object_ids=sorted(set(raw.object_ids)),
                element_ids=sorted(set(raw.element_ids)),
                message=raw.message,
                expected=raw.expected,
                actual=raw.actual,
                suggested_repair=raw.suggested_repair,
                # An unregistered code is labelled as such. Labelling it "built-in"
                # would claim the catalog owns a rule it has never heard of.
                rule_source=rule.rule_source if rule is not None else "unregistered",
                registered=registered,
                threshold=(
                    raw.threshold
                    if raw.threshold is not None
                    else (rule.threshold if rule is not None else None)
                ),
                validator_id=definition.validator_id,
                rule_id=rule_id,
                profile_id=profile.profile_id,
                profile_version=profile.profile_version,
                details=raw.details,
            )
            status, evidence = _waiver_status(
                issue, profile.waivers, revision=document.revision, now=moment
            )
            issues.append(
                issue.model_copy(update={"waiver_status": status, "waiver": evidence})
            )
    ordered = sort_issues(issues)
    result = ValidationResult(
        document_id=document.id,
        revision=document.revision,
        content_hash=drafting_content_hash(document),
        project_id=project_id,
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        rule_bundle_fingerprint=profile.fingerprint,
        engine_version=VALIDATION_ENGINE_VERSION,
        symbol_registry_fingerprint=registry.fingerprint(),
        evaluated_at=moment,
        validator_versions={definition.validator_id: definition.version for definition in VALIDATORS},
        validators_run=ran,
        validators_skipped=skipped,
        issues=ordered,
        counts=_count(ordered),
    )
    return result.model_copy(update={"result_hash": _result_hash(result)})


def _result_hash(result: ValidationResult) -> str:
    """Hash over the canonical public form of the result, excluding the hash itself.

    The canonical form is the same one REST, CLI and MCP publish, so a ``result_hash``
    can be recomputed by a reviewer from the payload they were given.
    """

    return canonical_digest(result, exclude=frozenset({"result_hash"}))


def validate_document(
    service: DocumentService,
    document_id: str,
    profile: EffectiveProfile,
    *,
    project_id: str = "",
    now: datetime | None = None,
) -> ValidationResult:
    """Validate a stored document by id through the governed read path."""

    document = service.get_document(document_id)
    return run_validation(
        document,
        service.symbols,
        profile,
        project_id=project_id,
        service=service,
        now=now,
    )


def issue_summary(result: ValidationResult, *, limit: int = 20) -> list[dict[str, Any]]:
    """A bounded, JSON-friendly view of the most severe issues (for reports and CLI)."""

    return [
        {
            "code": issue.code,
            "severity": issue.severity,
            "rule_id": issue.rule_id,
            "rule_source": issue.rule_source,
            "object_ids": issue.object_ids,
            "element_ids": issue.element_ids,
            "message": issue.message,
            "threshold": issue.threshold,
            "registered": issue.registered,
            "waiver_status": issue.waiver_status,
        }
        for issue in result.issues[:limit]
    ]


__all__ = [
    "CONTEXT_DOMAINS",
    "VALIDATION_ENGINE_VERSION",
    "VALIDATORS",
    "VALIDATORS_BY_ID",
    "RawIssue",
    "ValidationContext",
    "ValidationContextUnavailable",
    "ValidationTimeError",
    "ValidatorDefinition",
    "issue_summary",
    "normalize_evaluation_time",
    "run_validation",
    "validate_document",
]
