"""M11-D3: Cable validator profile (reuses the validation CONTRACT shape, never
the P&ID engine). Structural invariants are parse-time fail-closed in
cable_models; this module only judges post-load release readiness.

Frozen: two readiness rules (gauge_grammar blocker, document_non_empty warning
with also_fail_on_warning), exact gauge grammar + semantics, deterministic
profile/content/result hashes, unknown rule/profile fail-closed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .cable_models import CableDocument
from .cable_service import CableDocumentView

CABLE_PROFILE_ID = "cable-built-in"
CABLE_PROFILE_VERSION = 1

RULE_GAUGE = "cable.gauge_grammar"
RULE_NON_EMPTY = "cable.document_non_empty"

RULE_SEVERITY = {
    RULE_GAUGE: "blocker",
    RULE_NON_EMPTY: "warning",
}
ALSO_FAIL_ON_WARNING = (RULE_NON_EMPTY,)

_GAUGE_RE = re.compile(r"^(?:\d+(?:\.\d{1,2})?mm2|AWG\d{1,2})$")


class CableValidationError(ValueError):
    """Typed internal errors (no HTTP semantics)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def profile_fingerprint() -> str:
    rules = [
        {"rule_id": rule_id, "severity": RULE_SEVERITY[rule_id]}
        for rule_id in sorted(RULE_SEVERITY)
    ]
    return _sha256(
        _canonical(
            {
                "profile_id": CABLE_PROFILE_ID,
                "profile_version": CABLE_PROFILE_VERSION,
                "rules": rules,
            }
        )
    )


def content_hash_of(document: CableDocument, revision: int) -> str:
    payload_hash = _sha256(_canonical(document.model_dump(mode="json", exclude={"revision"})))
    return _sha256(f"{revision}:{payload_hash}")


@dataclass(frozen=True)
class CableRuleResult:
    rule_id: str
    severity: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class CableReadiness:
    document_id: str
    revision: int
    content_hash: str
    profile_id: str
    profile_version: int
    profile_fingerprint: str
    rule_results: tuple[CableRuleResult, ...]
    eligible: bool
    reasons: tuple[str, ...] = ()
    result_hash: str = ""


def _gauge_findings(document: CableDocument) -> list[str]:
    problems: list[str] = []
    for segment in document.segments:
        text = segment.gauge
        if _GAUGE_RE.fullmatch(text) is None:
            problems.append(f"{segment.id}: gauge {text!r} violates grammar")
            continue
        if text.startswith("AWG"):
            awg = int(text[3:])
            if not 0 <= awg <= 40:
                problems.append(f"{segment.id}: AWG {awg} outside 0-40")
        else:
            value = float(text[: -len("mm2")])
            if value <= 0:
                problems.append(f"{segment.id}: gauge value must be > 0")
    return problems


def assess(
    view: CableDocumentView,
    *,
    profile_version: int = CABLE_PROFILE_VERSION,
    rule_ids: tuple[str, ...] | None = None,
) -> CableReadiness:
    """Readiness assessment. Structural integrity was already enforced at load;
    unknown profile/rule inputs fail closed. Read-only."""
    if profile_version != CABLE_PROFILE_VERSION:
        raise CableValidationError(
            "unsupported_profile_version",
            f"cable profile version {profile_version} is not supported",
        )
    active = (RULE_GAUGE, RULE_NON_EMPTY) if rule_ids is None else tuple(rule_ids)
    for rule_id in active:
        if rule_id not in RULE_SEVERITY:
            raise CableValidationError("unknown_rule", f"unknown cable rule: {rule_id}")

    document = view.document
    revision = document.revision
    content_hash = content_hash_of(document, revision)

    results: list[CableRuleResult] = []
    for rule_id in active:
        if rule_id == RULE_GAUGE:
            problems = _gauge_findings(document)
            results.append(
                CableRuleResult(
                    rule_id=rule_id,
                    severity="blocker",
                    passed=not problems,
                    detail="; ".join(problems),
                )
            )
        elif rule_id == RULE_NON_EMPTY:
            non_empty = len(document.segments) > 0
            results.append(
                CableRuleResult(
                    rule_id=rule_id,
                    severity="warning",
                    passed=non_empty,
                    detail="" if non_empty else "document has no cable segments",
                )
            )

    blockers_failed = [r for r in results if r.severity == "blocker" and not r.passed]
    warnings_failed = [
        r for r in results if r.rule_id in ALSO_FAIL_ON_WARNING and not r.passed
    ]
    eligible = not blockers_failed and not warnings_failed
    reasons = [r.detail for r in (*blockers_failed, *warnings_failed) if r.detail]

    result_hash = _sha256(
        _canonical(
            {
                "profile_fingerprint": profile_fingerprint(),
                "document_id": view.document_id,
                "revision": revision,
                "content_hash": content_hash,
                "rule_results": [
                    {
                        "rule_id": r.rule_id,
                        "severity": r.severity,
                        "passed": r.passed,
                        "detail": r.detail,
                    }
                    for r in results
                ],
            }
        )
    )
    return CableReadiness(
        document_id=view.document_id,
        revision=revision,
        content_hash=content_hash,
        profile_id=CABLE_PROFILE_ID,
        profile_version=CABLE_PROFILE_VERSION,
        profile_fingerprint=profile_fingerprint(),
        rule_results=tuple(results),
        eligible=eligible,
        reasons=tuple(reasons),
        result_hash=result_hash,
    )
