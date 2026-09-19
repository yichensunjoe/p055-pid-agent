"""Engineering-validation read surfaces (Charter §43, §49; remote baseline R2 §6, §11).

Three routes, and all three are **reads**:

* ``GET /validation/profile`` — the resolved rule bundle (id, version, fingerprint, every
  effective rule with the layer that decided it). A reviewer must be able to see *which*
  rules were in force before arguing about a finding.
* ``GET /validation/documents/{id}`` — the canonical ``ValidationResult``.
* ``GET /validation/documents/{id}/release-readiness`` — readiness evidence
  (``eligible`` / ``not_eligible``).

What these routes deliberately cannot do:

* accept a profile, rule, severity override or waiver from the request body — the profile
  is resolved server-side (``PID_AGENT_VALIDATION_PROFILE``), because a caller that could
  disable the rule about to fail it would make the gate decorative;
* approve, sign, issue IFC/AFC or move a document to a released state — readiness is
  evidence, and the human Approval Gate stays where it is (Charter §11, §44-10);
* write a revision. Validating is reading. The optional audit event is a *read* event
  (``validation.completed`` / ``release.readiness.assessed``) that references the canonical
  hashes; it is not ``revision.created`` and it never appends to document history.

The ``as_of`` parameter exists because waivers expire: without it the same request could
return two different verdicts and the reason would be invisible.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from .audit import request_audit_context
from .release_validator import (
    RELEASE_VALIDATOR_VERSION,
    assess_document_release_readiness,
)
from .service import DocumentNotFoundError, DocumentService
from .validation_engine import (
    VALIDATION_ENGINE_VERSION,
    ValidationTimeError,
    normalize_evaluation_time,
    validate_document,
)
from .validation_models import ReleaseReadiness, ValidationIssue, ValidationResult
from .validation_profile import EffectiveProfile, ProfileError, load_profile

#: A profile this deployment cannot resolve is a deployment error, not a client error.
PROFILE_ERROR_STATUS = 500


def _moment(as_of: datetime | None) -> datetime:
    """Normalize the evaluation instant through the engine's own contract.

    The engine, not this route, decides what a valid evaluation time is (R3 P0-2); the
    route only maps the engine's stable error code onto HTTP 422 framing.
    """

    try:
        return normalize_evaluation_time(as_of)
    except ValidationTimeError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": exc.code,
                "message": "as_of must be timezone-aware (for example 2026-09-19T12:00:00Z)",
                "retryable": False,
            },
        ) from exc


def create_validation_router(service: DocumentService) -> APIRouter:
    router = APIRouter(prefix="/api/v2/validation", tags=["P&ID-Agent validation"])

    def _profile() -> EffectiveProfile:
        try:
            return load_profile()
        except ProfileError as exc:
            raise HTTPException(
                status_code=PROFILE_ERROR_STATUS,
                detail={
                    "error": exc.code,
                    "message": exc.message,
                    "retryable": False,
                },
            ) from exc

    def _document(document_id: str):
        try:
            return service.get_document(document_id)
        except DocumentNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "document_not_found",
                    "message": f"no document {document_id!r}",
                    "retryable": False,
                },
            ) from exc

    @router.get("/profile")
    def validation_profile() -> dict:
        """The resolved rule bundle: what is in force, and which layer decided it."""

        profile = _profile()
        return {
            "schema": "pid-agent.validation-profile.effective",
            "profile_id": profile.profile_id,
            "profile_version": profile.profile_version,
            "source": profile.source,
            "fingerprint": profile.fingerprint,
            "rule_bundle_fingerprint": profile.fingerprint,
            "engine_version": VALIDATION_ENGINE_VERSION,
            "release_validator_version": RELEASE_VALIDATOR_VERSION,
            "release_policy": profile.release_policy.model_dump(mode="json"),
            "waivers": [
                {
                    "waiver_id": waiver.waiver_id,
                    "rule_id": waiver.rule_id,
                    "code": waiver.code,
                    "actor": waiver.actor,
                    "reason": waiver.reason,
                    "granted_at": waiver.granted_at.isoformat(),
                    "expires_at": waiver.expires_at.isoformat() if waiver.expires_at else None,
                    "max_revision": waiver.max_revision,
                    "object_ids": sorted(waiver.object_ids),
                    "element_ids": sorted(waiver.element_ids),
                }
                for waiver in profile.waivers
            ],
            "rules": [
                {
                    "rule_id": rule.rule_id,
                    "validator_id": rule.validator_id,
                    "code": rule.code,
                    "enabled": rule.enabled,
                    "severity": rule.severity,
                    "threshold": rule.threshold,
                    "rule_source": rule.rule_source,
                }
                for rule in sorted(profile.rules.values(), key=lambda item: item.rule_id)
            ],
        }

    @router.get("/documents/{document_id}", response_model=ValidationResult)
    def validate(
        document_id: str,
        as_of: datetime | None = None,
        audit: bool = False,
    ) -> ValidationResult:
        """The canonical validation result for one document. Read-only.

        ``audit=true`` records a read/tool evidence event referencing the result hash. It
        does not create a revision, a history entry or an approval.
        """

        document = _document(document_id)
        profile = _profile()
        moment = _moment(as_of)
        result = validate_document(service, document_id, profile, now=moment)
        if audit:
            service.audit.record_event(
                "validation.completed",
                request_audit_context(
                    "validate_document",
                    label=f"Validate {document.name}",
                    validation_status="valid" if not result.has_blockers else "invalid",
                    validation_evidence={
                        "result_hash": result.result_hash,
                        "rule_bundle_fingerprint": result.rule_bundle_fingerprint,
                        "counts": result.counts.model_dump(mode="json"),
                    },
                ),
                document_id=document.id,
                base_revision=document.revision,
                status="applied",
                evidence={
                    "validation_hash": result.result_hash,
                    "profile_id": result.profile_id,
                    "profile_version": result.profile_version,
                    "rule_bundle_fingerprint": result.rule_bundle_fingerprint,
                    "evaluated_at": result.evaluated_at.isoformat(),
                    "counts": result.counts.model_dump(mode="json"),
                    "unregistered_codes": sorted(
                        {
                            f"{issue.validator_id}.{issue.code}"
                            for issue in result.unregistered()
                        }
                    ),
                },
            )
        return result

    @router.get(
        "/documents/{document_id}/release-readiness", response_model=ReleaseReadiness
    )
    def release_readiness(
        document_id: str,
        as_of: datetime | None = None,
        audit: bool = False,
    ) -> ReleaseReadiness:
        """Readiness evidence. Not an approval, and unable to become one."""

        document = _document(document_id)
        profile = _profile()
        moment = _moment(as_of)
        readiness = assess_document_release_readiness(
            service, document_id, profile, now=moment
        )
        if audit:
            service.audit.record_event(
                "release.readiness.assessed",
                request_audit_context(
                    "assess_release_readiness",
                    label=f"Assess release readiness for {document.name}",
                    validation_status="valid" if readiness.state == "eligible" else "invalid",
                    validation_evidence={
                        "readiness_hash": readiness.readiness_hash,
                        "state": readiness.state,
                    },
                ),
                document_id=document.id,
                base_revision=document.revision,
                status="applied",
                evidence={
                    "readiness_hash": readiness.readiness_hash,
                    "state": readiness.state,
                    "validation_hash": readiness.validation_hash,
                    "release_validator_version": readiness.release_validator_version,
                    "rule_bundle_fingerprint": readiness.rule_bundle_fingerprint,
                    "evaluated_at": readiness.evaluated_at.isoformat(),
                    "missing_required_validators": readiness.missing_required_validators,
                    "unregistered_codes": readiness.unregistered_codes,
                    # Stated in the evidence itself so a reader of the audit trail cannot
                    # mistake this event for a release decision.
                    "human_approval_required": True,
                },
            )
        return readiness

    return router


def issue_rows(result: ValidationResult) -> list[dict]:
    """A flat, UI-shaped view of the issues a reviewer must be able to read.

    Mirrors ``ValidationIssue`` rather than inventing a second contract: code, severity,
    message, locators, expected vs actual, rule source, suggested repair, waiver status.
    """

    def row(issue: ValidationIssue) -> dict:
        return {
            "code": issue.code,
            "severity": issue.severity,
            "message": issue.message,
            "object_ids": list(issue.object_ids),
            "element_ids": list(issue.element_ids),
            "expected": issue.expected,
            "actual": issue.actual,
            "suggested_repair": issue.suggested_repair,
            "rule_source": issue.rule_source,
            "rule_id": issue.rule_id,
            "threshold": issue.threshold,
            "registered": issue.registered,
            "waiver_status": issue.waiver_status,
            "waiver_id": issue.waiver.waiver_id if issue.waiver else "",
        }

    return [row(issue) for issue in result.issues]


__all__ = ["create_validation_router", "issue_rows"]
