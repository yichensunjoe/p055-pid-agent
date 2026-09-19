"""Release readiness: an automated gate that can never approve anything (Charter §22, §44-10).

The release validator answers one question — "given this project's rules, may a formal
release *be requested*?" — and it answers it with ``eligible`` or ``not_eligible``. It
deliberately cannot express ``Approved``, ``IFC``, ``AFC``, a signature or a human
sign-off, because those are human acts behind the Approval Gate, and an Agent that could
produce one by running a tool would have the authority the Charter forbids it.

Three invariants are enforced here rather than promised in a document:

* **Fail closed.** A required validator that did not run makes the document ineligible.
  Missing evidence is never treated as passing evidence.
* **Unwaived severity decides.** ``ReleasePolicy.fail_on`` (default ``blocker``) is what
  makes a release ineligible; a waiver annotates but never deletes the issue, so a waived
  blocker is eligible *and* still visible in the evidence.
* **Unregistered findings fail closed.** A code the catalog does not know is surfaced
  rather than dropped, and a formal release gate may not treat "we do not know what this
  is" as passing evidence.
* **No write path.** This module reads. It is exposed through read-only surfaces and has
  no counterpart that could persist an approval.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .models import Document
from .service import DocumentService
from .symbols import SymbolRegistry
from .validation_engine import VALIDATION_ENGINE_VERSION, run_validation
from .validation_models import ReleaseReadiness, ValidationResult
from .validation_profile import EffectiveProfile

RELEASE_VALIDATOR_VERSION = "1"


def _state_reasons(
    result: ValidationResult,
    profile: EffectiveProfile,
    missing_required: list[str],
) -> list[str]:
    reasons: list[str] = []
    if missing_required:
        reasons.append(
            "required validators did not run: " + ", ".join(sorted(missing_required))
        )
    unregistered = result.unregistered()
    if unregistered:
        codes = sorted({f"{issue.validator_id}.{issue.code}" for issue in unregistered})
        reasons.append(
            "findings whose rule is not registered in the catalog (fail closed): "
            + ", ".join(codes)
        )
    for severity in profile.release_policy.fail_on:
        failing = result.unwaived(severity)
        if failing:
            codes = sorted({issue.code for issue in failing})
            reasons.append(
                f"{len(failing)} unwaived {severity} issue(s): " + ", ".join(codes)
            )
    return reasons


def assess_release_readiness(
    document: Document,
    registry: SymbolRegistry,
    profile: EffectiveProfile,
    *,
    project_id: str = "",
    service: DocumentService | None = None,
    now: datetime | None = None,
    document_name: str = "",
) -> ReleaseReadiness:
    """Assess one document. Reads only; produces evidence, never an approval."""

    result = run_validation(
        document, registry, profile, project_id=project_id, service=service, now=now
    )
    run_ids = set(result.validators_run)
    required = list(profile.release_policy.required_validators)
    missing_required = sorted(set(required) - run_ids)
    reasons = _state_reasons(result, profile, missing_required)
    unwaived_blockers = sorted(
        {f"{issue.code}:{','.join(issue.element_ids)}" for issue in result.unwaived("blocker")}
    )
    unwaived_failures: list[str] = []
    for severity in profile.release_policy.fail_on:
        unwaived_failures.extend(
            f"{issue.severity}/{issue.code}:{','.join(issue.element_ids)}"
            for issue in result.unwaived(severity)
        )
    readiness = ReleaseReadiness(
        document_id=document.id,
        document_name=document_name or document.name,
        revision=document.revision,
        content_hash=result.content_hash,
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        rule_bundle_fingerprint=profile.fingerprint,
        validation_hash=result.result_hash,
        engine_version=VALIDATION_ENGINE_VERSION,
        release_validator_version=RELEASE_VALIDATOR_VERSION,
        symbol_registry_fingerprint=result.symbol_registry_fingerprint,
        evaluated_at=result.evaluated_at,
        state="eligible" if not reasons else "not_eligible",
        reasons=reasons,
        required_validators=sorted(required),
        validators_run=list(result.validators_run),
        validators_skipped=list(result.validators_skipped),
        missing_required_validators=missing_required,
        counts=result.counts,
        unwaived_blockers=unwaived_blockers,
        unwaived_failures=sorted(unwaived_failures),
        waivers_considered=sorted(
            {
                issue.waiver.waiver_id
                for issue in result.issues
                if issue.waiver is not None
            }
        ),
        unregistered_codes=sorted(
            {f"{issue.validator_id}.{issue.code}" for issue in result.unregistered()}
        ),
        policy=profile.release_policy,
    )
    return readiness.model_copy(update={"readiness_hash": _readiness_hash(readiness)})


def _readiness_hash(readiness: ReleaseReadiness) -> str:
    """Hash over the canonical readiness payload, so a surface that audits it binds it."""

    payload = readiness.model_dump(mode="json", exclude={"readiness_hash"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def assess_document_release_readiness(
    service: DocumentService,
    document_id: str,
    profile: EffectiveProfile,
    *,
    project_id: str = "",
    now: datetime | None = None,
) -> ReleaseReadiness:
    document = service.get_document(document_id)
    return assess_release_readiness(
        document,
        service.symbols,
        profile,
        project_id=project_id,
        service=service,
        now=now,
        document_name=document.name,
    )


__all__ = [
    "RELEASE_VALIDATOR_VERSION",
    "assess_document_release_readiness",
    "assess_release_readiness",
]
