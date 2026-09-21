"""The success oracle: the only thing allowed to call a repair repair (baseline §A6).

A self-repair agent is a system that rewrites a drawing, so the question "did it work" has
to be answered by something other than the system that did the rewriting. This module is
that answer, and it is deliberately *mechanical*: every clause of baseline §A6 is a check
over two canonical M4 validation results plus the two documents that produced them.

Two clauses are the ones that make the difference between a real repair and a plausible one:

* **Rule bundle unchanged, required validators still ran.** The cheapest way to make a
  finding disappear is to make the rule disappear. Both are refusals here, and the safety
  suite proves the refusal fires.
* **Protected regions unchanged.** A candidate may only touch the frozen scope. A repaint
  of the whole drawing that happens to satisfy the validators fails locality, which is the
  only reason "success rate" is a meaningful number rather than a measure of how much of
  the drawing the agent was willing to rewrite.

The oracle never mutates anything and never writes: it is a verdict over evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .models import Document
from .repair_models import (
    RepairFailureCode,
    RepairFindingRef,
    RepairProtectedHashes,
    RepairRequest,
    RepairScope,
)
from .repair_scope import element_map
from .validation_models import SEVERITY_RANK, ValidationIssue, ValidationResult

#: Severities that count as a regression when newly introduced. ``info`` is excluded by the
#: baseline ("新 info 可以存在但必须报告"), everything else is not.
REGRESSION_SEVERITIES = ("blocker", "error", "warning")


def _locators(issue: ValidationIssue) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return tuple(issue.object_ids), tuple(issue.element_ids)


def _overlaps(issue: ValidationIssue, target: RepairFindingRef) -> bool:
    """Whether an issue is the same finding as the target.

    Matching is by ``(validator_id, code)`` plus *overlapping locators*, not by message: an
    issue that keeps the same code but names different equipment is a different defect, and
    treating it as "the target" would let a repair move a problem instead of fixing it.
    """

    if issue.validator_id != target.validator_id or issue.code != target.code:
        return False
    if not target.element_ids and not target.object_ids:
        return True
    # Some codes fire once per element *per port* ("this required port is unconnected"). Two such
    # findings have identical locators and differ only in the port, so without this an element
    # match alone would let a repair that attached the *other* port count as the target being
    # gone. The port is an extra constraint, not a replacement for the locator match.
    if target.details.get("port_id") and issue.details.get("port_id"):
        if issue.details["port_id"] != target.details["port_id"]:
            return False
    # A finding the validator reports about the *drawing* rather than about one element cannot
    # be narrowed to an element, so it is matched by class: "the out-of-bounds finding is gone"
    # is the only meaningful statement about it.
    if not issue.element_ids and not issue.object_ids:
        return True
    return bool(
        set(issue.element_ids) & set(target.element_ids)
        or set(issue.object_ids) & set(target.object_ids)
    )


def find_target(result: ValidationResult, target: RepairFindingRef) -> list[ValidationIssue]:
    """Every issue in ``result`` that is the target finding."""

    return [issue for issue in result.issues if _overlaps(issue, target)]


def target_present(result: ValidationResult, target: RepairFindingRef) -> bool:
    """Present *and* not waived: a waiver is a documented deviation, not a repair."""

    return any(not issue.is_waived for issue in find_target(result, target))


def _key(issue: ValidationIssue) -> tuple[Any, ...]:
    return (issue.validator_id, issue.code, *_locators(issue))


def collateral_regressions(
    base: ValidationResult, candidate: ValidationResult
) -> list[str]:
    """New unwaived findings at warning or above that the base did not have.

    Multiset difference, so two identical findings that become three are a regression too.
    """

    base_counts: dict[tuple[Any, ...], int] = {}
    for issue in base.issues:
        if issue.severity in REGRESSION_SEVERITIES and not issue.is_waived:
            base_counts[_key(issue)] = base_counts.get(_key(issue), 0) + 1
    regressions: list[str] = []
    for issue in candidate.issues:
        if issue.severity not in REGRESSION_SEVERITIES or issue.is_waived:
            continue
        key = _key(issue)
        if base_counts.get(key, 0) > 0:
            base_counts[key] -= 1
            continue
        regressions.append(issue.code)
    return sorted(regressions)


def required_validator_skips(
    base: ValidationResult, candidate: ValidationResult
) -> list[str]:
    """Validators that ran on the base and did not run on the candidate.

    "Required" is taken from the base result rather than from a policy list, because the
    mechanical claim is the one that matters: a candidate may not *lose* a validator that
    had already produced evidence, no matter how the profile is configured.
    """

    required = set(base.validators_run)
    skipped = {skip.validator_id for skip in candidate.validators_skipped}
    return sorted(required & skipped)


def new_unregistered_codes(
    base: ValidationResult, candidate: ValidationResult
) -> list[str]:
    known = {issue.code for issue in base.issues if issue.registered}
    return sorted(
        {
            issue.code
            for issue in candidate.issues
            if not issue.registered and issue.code not in known
        }
    )


def self_granted_waivers(
    base: ValidationResult, candidate: ValidationResult
) -> list[str]:
    """Findings that became ``waived`` without the base saying so.

    The repair runner may not touch a profile, so this should be impossible — which is the
    point of asserting it: an impossible state that is never checked is how a "self-repair"
    feature quietly becomes a self-approval feature (baseline §L).
    """

    waived_before = {
        _key(issue) for issue in base.issues if issue.waiver_status == "waived"
    }
    return sorted(
        {
            f"{issue.validator_id}.{issue.code}"
            for issue in candidate.issues
            if issue.waiver_status == "waived" and _key(issue) not in waived_before
        }
    )


def locality_violation_ids(
    scope: RepairScope,
    changed_ids: Iterable[str],
    created_ids: Iterable[str] = (),
    deleted_ids: Iterable[str] = (),
) -> list[str]:
    """Ids the repair had no right to change: outside the scope, or added/removed without leave.

    An *existing* id the scope never named is a violation whether it was edited or deleted —
    the scope is the whole claim about where the repair happens. Creations and deletions are
    judged against the case's own policy instead (baseline §B5), and they are judged *separately*
    because they are different risks: adding equipment is how a repair puts a removed device back,
    while removing equipment is how the cheapest pseudo-repair (§C4: "just delete the offending
    object") makes a finding disappear. A case that granted one did not grant the other, so
    neither defaults to on.
    """

    allowed = set(scope.allowed_element_ids)
    created = set(created_ids)
    deleted = set(deleted_ids)
    existing = set(changed_ids) - created - deleted
    violations = {element_id for element_id in existing if element_id not in allowed}
    violations |= {element_id for element_id in deleted if element_id not in allowed}
    if created and not scope.created_ids_allowed(created):
        violations |= created
    if deleted and not scope.deleted_ids_allowed(deleted):
        violations |= deleted
    return sorted(violations)


@dataclass(frozen=True)
class PostconditionResult:
    unmet: list[str] = field(default_factory=list)

    @property
    def satisfied(self) -> bool:
        return not self.unmet


def check_postconditions(
    before: Document, after: Document, postconditions: dict[str, Any] | None
) -> PostconditionResult:
    """Check the case's declared semantic postconditions (baseline §B5).

    The vocabulary is small on purpose. A case may assert that specific elements keep their
    identity, that specific connectors still exist and still bind the same
    ``element_id``/``port_id`` pairs, that named connectors kept their line metadata, that
    equipment did not move, and that tags stayed unique. Geometry cases assert almost none
    of this; connectivity cases assert most of it. Both are checked the same way.
    """

    if not postconditions:
        return PostconditionResult()
    unmet: list[str] = []
    before_map = element_map(before)
    after_map = element_map(after)

    for element_id in postconditions.get("element_ids_preserved", []):
        if element_id not in after_map:
            unmet.append(f"element_missing:{element_id}")

    for element_id in postconditions.get("element_ids_unchanged", []):
        if element_id not in after_map or element_id not in before_map:
            unmet.append(f"element_missing:{element_id}")
            continue
        if before_map[element_id].model_dump(mode="json") != after_map[element_id].model_dump(
            mode="json"
        ):
            unmet.append(f"element_changed:{element_id}")

    for connector_id, spec in postconditions.get("port_bindings", {}).items():
        connector = after_map.get(connector_id)
        if connector is None or connector.type != "connector":
            unmet.append(f"connector_missing:{connector_id}")
            continue
        for endpoint_name in ("source", "target"):
            if endpoint_name not in spec:
                continue
            expected_element, expected_port = spec[endpoint_name][0], spec[endpoint_name][1]
            endpoint = getattr(connector, endpoint_name)
            actual = (
                (endpoint.element_id, endpoint.port_id) if endpoint is not None else (None, None)
            )
            if actual != (expected_element, expected_port):
                unmet.append(
                    f"port_binding:{connector_id}.{endpoint_name}"
                    f"={actual[0]}:{actual[1]}!={expected_element}:{expected_port}"
                )

    for connector_id, spec in postconditions.get("line_metadata", {}).items():
        connector = after_map.get(connector_id)
        if connector is None or connector.type != "connector":
            unmet.append(f"connector_missing:{connector_id}")
            continue
        for attribute, expected in spec.items():
            if getattr(connector, attribute, None) != expected:
                unmet.append(f"line_metadata:{connector_id}.{attribute}")

    if postconditions.get("tags_unique"):
        seen: dict[str, str] = {}
        for element in after.elements:
            tag = ""
            if element.type == "symbol":
                tag = str(element.properties.get("tag") or element.label or "")
            if not tag:
                continue
            key = tag.strip().upper()
            if key in seen:
                unmet.append(f"tag_duplicate:{key}")
            seen[key] = element.id

    for connector_id in postconditions.get("endpoints_share_new_element", []):
        connector = after_map.get(connector_id)
        if connector is None or connector.type != "connector":
            unmet.append(f"connector_missing:{connector_id}")
            continue
        bound = {
            endpoint.element_id
            for endpoint in (connector.source, connector.target)
            if endpoint is not None and endpoint.element_id
        }
        free = [
            name
            for name in ("source", "target")
            if getattr(connector, name) is None or getattr(connector, name).element_id is None
        ]
        if free:
            unmet.append(f"endpoint_still_free:{connector_id}.{','.join(free)}")
            continue
        new_ids = bound - set(before_map)
        if not new_ids:
            unmet.append(f"no_replacement_element:{connector_id}")

    if postconditions.get("no_dangling_endpoints"):
        for connector in after.elements:
            if connector.type != "connector":
                continue
            for name in ("source", "target"):
                endpoint = getattr(connector, name)
                if endpoint is None or endpoint.element_id is None or endpoint.port_id is None:
                    unmet.append(f"dangling:{connector.id}.{name}")

    for element_id, point in postconditions.get("element_positions", {}).items():
        element = after_map.get(element_id)
        if element is None:
            unmet.append(f"element_missing:{element_id}")
            continue
        position = getattr(element, "position", None)
        if position is None or [position.x, position.y] != list(point):
            unmet.append(f"element_moved:{element_id}")

    return PostconditionResult(unmet=sorted(unmet))


@dataclass(frozen=True)
class RepairOracleVerdict:
    """The result of running every §A6 clause over one candidate."""

    target_resolved: bool
    collateral_regressions: list[str]
    locality_violation_ids: list[str]
    protected_region_changed: bool
    touched_ids: list[str]
    failure_code: RepairFailureCode | None
    notes: list[str]
    postconditions: PostconditionResult = field(default_factory=PostconditionResult)

    @property
    def accepted(self) -> bool:
        return self.failure_code is None


def evaluate_candidate(
    base: ValidationResult,
    candidate: ValidationResult,
    request: RepairRequest,
    *,
    before: Document,
    after: Document,
    protected_pre: RepairProtectedHashes,
    protected_post: RepairProtectedHashes,
    changed_ids: list[str],
    touched_ids: list[str],
    created_ids: Iterable[str] = (),
    deleted_ids: Iterable[str] = (),
    postconditions: dict[str, Any] | None = None,
) -> RepairOracleVerdict:
    """Run the §A6 clauses in the order a reviewer would, and return the first refusal.

    Every clause is evaluated before the verdict is chosen — not because a failing clause
    should be tolerated, but because the record has to carry *all* the evidence a reviewer
    needs to see why a candidate was refused. A short-circuiting oracle produces a tidy
    record that hides the second problem behind the first.
    """

    notes: list[str] = []
    resolved = not target_present(candidate, request.target)
    collateral = collateral_regressions(base, candidate)
    locality = locality_violation_ids(request.scope, changed_ids, created_ids, deleted_ids)
    protected_changed = (
        protected_pre.engineering_projection != protected_post.engineering_projection
        or protected_pre.drawing_projection != protected_post.drawing_projection
    )
    postconditions_result = check_postconditions(before, after, postconditions)
    touched_over_budget = len(touched_ids) > request.scope.max_touched_existing_ids

    failure: RepairFailureCode | None = None
    if candidate.rule_bundle_fingerprint != base.rule_bundle_fingerprint:
        notes.append("the candidate ran under a different rule bundle")
        failure = "rule_bundle_changed"
    else:
        lost = required_validator_skips(base, candidate)
        if lost:
            notes.append(f"validators that produced base evidence did not run: {lost}")
            failure = "required_validator_skipped"
        else:
            unregistered = new_unregistered_codes(base, candidate)
            if unregistered:
                notes.append(f"candidate introduced unregistered findings: {unregistered}")
                failure = "unregistered_finding"
            else:
                granted = self_granted_waivers(base, candidate)
                if granted:
                    notes.append(f"candidate waived findings the base did not: {granted}")
                    failure = "policy_violation"
                elif not resolved:
                    notes.append("the target finding is still present after the candidate")
                    failure = "target_not_resolved"
                elif collateral:
                    notes.append(f"candidate introduced regressions: {collateral}")
                    failure = "collateral_regression"
                elif locality:
                    if set(locality) <= set(deleted_ids):
                        # Removing the offending object is not a repair, and the report should say
                        # *that* rather than filing it under a scope bookkeeping failure.
                        notes.append(
                            f"candidate removed elements the case never permitted: {locality}"
                        )
                        failure = "deletion_not_permitted"
                    else:
                        notes.append(
                            f"candidate changed elements outside the frozen scope: {locality}"
                        )
                        failure = "locality_violation"
                elif touched_over_budget:
                    notes.append(
                        f"candidate touched {len(touched_ids)} existing elements, budget is "
                        f"{request.scope.max_touched_existing_ids}"
                    )
                    failure = "touched_budget_exceeded"
                elif protected_changed:
                    notes.append("a protected projection changed")
                    failure = "protected_region_changed"
                elif not postconditions_result.satisfied:
                    notes.append(f"unmet postconditions: {postconditions_result.unmet}")
                    failure = "validation_failed"

    return RepairOracleVerdict(
        target_resolved=resolved,
        collateral_regressions=collateral,
        locality_violation_ids=locality,
        protected_region_changed=protected_changed,
        touched_ids=list(touched_ids),
        failure_code=failure,
        notes=notes,
        postconditions=postconditions_result,
    )


def worst_severity(result: ValidationResult) -> str:
    """The most severe unwaived severity in a result, for reporting (``none`` if clean)."""

    severities = [issue.severity for issue in result.issues if not issue.is_waived]
    if not severities:
        return "none"
    return min(severities, key=lambda item: SEVERITY_RANK[item])


__all__ = [
    "REGRESSION_SEVERITIES",
    "PostconditionResult",
    "RepairOracleVerdict",
    "check_postconditions",
    "collateral_regressions",
    "evaluate_candidate",
    "find_target",
    "locality_violation_ids",
    "new_unregistered_codes",
    "required_validator_skips",
    "self_granted_waivers",
    "target_present",
    "worst_severity",
]
