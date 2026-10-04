"""M12-D4: project-level deterministic validation / readiness (Gate CODE GO).

Design frozen at reports/m12-d1-design.md Q5 + F77-1 + the D4 CODE GO:
- P&ID member readiness comes ONLY from assess_document_release_readiness(
  service, id, load_profile(), now=evaluation_as_of) — server-side profile,
  the caller cannot submit or override one;
- Cable member readiness comes from assess_cable_document();
- cross-domain checks use stable issue codes; stale is checked FIRST and
  once stale, only the stale finding is reported for that endpoint;
- soft-deleted links do not participate;
- readiness is only eligible / not_eligible — never Approved/Released;
- result_hash binds evaluation_as_of + per-member readiness hashes +
  project-level issues + active link pins (F77-1);
- the assessor is a pure read: zero audit events, zero writes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .cable_service import CableService
from .cable_validation import assess_cable_document
from .release_validator import assess_document_release_readiness
from .service import DocumentService
from .store import SQLiteDocumentStore
from .validation_profile import load_profile

PROFILE_ID = "project-built-in"
PROFILE_VERSION = 1

ISSUE_DANGLING_SOURCE = "dangling_link_source"
ISSUE_DANGLING_TARGET = "dangling_link_target"
ISSUE_MISSING_SOURCE = "missing_source_object"
ISSUE_MISSING_TARGET = "missing_target_object"
ISSUE_WRONG_DOMAIN = "wrong_domain_reference"
ISSUE_STALE_SOURCE = "stale_pinned_revision_source"
ISSUE_STALE_TARGET = "stale_pinned_revision_target"

_SEVERITY: dict[str, str] = {
    ISSUE_DANGLING_SOURCE: "blocker",
    ISSUE_DANGLING_TARGET: "blocker",
    ISSUE_MISSING_SOURCE: "blocker",
    ISSUE_MISSING_TARGET: "blocker",
    ISSUE_WRONG_DOMAIN: "blocker",
    ISSUE_STALE_SOURCE: "warning",
    ISSUE_STALE_TARGET: "warning",
}
_FAIL_ON_WARNING = frozenset({ISSUE_STALE_SOURCE, ISSUE_STALE_TARGET})

_RULE_SET_FROZEN = [
    [code, _SEVERITY[code], code in _FAIL_ON_WARNING] for code in sorted(_SEVERITY)
]


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


PROFILE_FINGERPRINT = _sha256(
    f"{PROFILE_ID}/v{PROFILE_VERSION}:" + _canonical(_RULE_SET_FROZEN)
)


@dataclass(frozen=True)
class ProjectIssue:
    code: str
    severity: str
    link_id: str


@dataclass(frozen=True)
class MemberReadinessSnapshot:
    document_id: str
    domain: str
    state: str
    readiness_hash: str
    revision: int


@dataclass(frozen=True)
class ProjectReadiness:
    project_id: str
    evaluation_as_of: datetime
    state: str  # "eligible" | "not_eligible" — never Approved/Released
    issues: tuple[ProjectIssue, ...]
    members: tuple[MemberReadinessSnapshot, ...]
    result_hash: str
    profile_id: str = PROFILE_ID
    profile_version: int = PROFILE_VERSION
    profile_fingerprint: str = PROFILE_FINGERPRINT


def assess_project_readiness_core(
    *,
    project_id: str,
    evaluation_as_of: datetime,
    member_snapshots: list[MemberReadinessSnapshot],
    link_rows: list[dict[str, Any]],
) -> ProjectReadiness:
    """Pure readiness engine (M13-D3 extraction): issue scan, eligible state
    and result_hash from already-assessed member snapshots plus canonical link
    rows. Rows may carry reader-resolved extras — ``_registry_source`` /
    ``_registry_target`` domains, ``_source_revision`` / ``_target_revision``
    current revisions, and ``_source_reference_exists`` / ``_target_object_exists``
    booleans. The core never touches storage: the D4 service reader fills the
    extras from the store; the M13 commit path fills them from staged state.
    Same engine + same inputs keeps D4 service semantics and the in-transaction
    snapshot hash-identical."""
    members = sorted(member_snapshots, key=lambda snapshot: snapshot.document_id)
    member_ids = {snapshot.document_id for snapshot in members}

    issues: list[ProjectIssue] = []
    pins: list[dict[str, Any]] = []
    for row in sorted(link_rows, key=lambda item: str(item["link_id"])):
        link_id = str(row["link_id"])
        source_id = str(row["source_document_id"])
        target_id = str(row["target_document_id"])
        pins.append(
            {
                "link_id": link_id,
                "pinned_source_revision": int(row["pinned_source_revision"]),
                "pinned_target_revision": int(row["pinned_target_revision"]),
            }
        )
        if source_id not in member_ids:
            issues.append(ProjectIssue(ISSUE_DANGLING_SOURCE, _SEVERITY[ISSUE_DANGLING_SOURCE], link_id))
        if target_id not in member_ids:
            issues.append(ProjectIssue(ISSUE_DANGLING_TARGET, _SEVERITY[ISSUE_DANGLING_TARGET], link_id))
        # D80-1 frozen definition: the finding fires when the link row's
        # DECLARED domain drifts from the registry's actual domain.
        declared_source = str(row["source_domain"])
        declared_target = str(row["target_domain"])
        registry_source = row.get("_registry_source")
        registry_target = row.get("_registry_target")
        if (
            registry_source is not None
            and registry_target is not None
            and (
                declared_source != registry_source
                or declared_target != registry_target
            )
        ):
            issues.append(ProjectIssue(ISSUE_WRONG_DOMAIN, _SEVERITY[ISSUE_WRONG_DOMAIN], link_id))

        # Stale FIRST: once an endpoint's pinned revision is behind, only the
        # stale finding is reported for that endpoint (F77/Q5 freeze).
        source_revision = row.get("_source_revision")
        target_revision = row.get("_target_revision")
        source_stale = source_revision is not None and source_revision != int(
            row["pinned_source_revision"]
        )
        target_stale = target_revision is not None and target_revision != int(
            row["pinned_target_revision"]
        )
        if source_stale:
            issues.append(ProjectIssue(ISSUE_STALE_SOURCE, _SEVERITY[ISSUE_STALE_SOURCE], link_id))
        if target_stale:
            issues.append(ProjectIssue(ISSUE_STALE_TARGET, _SEVERITY[ISSUE_STALE_TARGET], link_id))
        # D80-2 frozen definition: the source reference exists only when the
        # segment exists in the CURRENT cable revision AND the endpoint is a
        # legal 'from'/'to'. Absence (revision None / load failure) surfaces
        # as a missing finding, never as staleness.
        if not source_stale and not bool(row.get("_source_reference_exists")):
            issues.append(ProjectIssue(ISSUE_MISSING_SOURCE, _SEVERITY[ISSUE_MISSING_SOURCE], link_id))
        if not target_stale and not bool(row.get("_target_object_exists")):
            issues.append(ProjectIssue(ISSUE_MISSING_TARGET, _SEVERITY[ISSUE_MISSING_TARGET], link_id))

    failing = sum(
        1
        for issue in issues
        if issue.severity == "blocker" or issue.code in _FAIL_ON_WARNING
    )
    member_ready = all(snapshot.state == "eligible" for snapshot in members)
    state = "eligible" if (member_ready and failing == 0) else "not_eligible"

    result_hash = _sha256(
        _canonical(
            {
                "evaluation_as_of": evaluation_as_of.isoformat(),
                "issues": [
                    [issue.code, issue.severity, issue.link_id] for issue in issues
                ],
                "members": [
                    [
                        snapshot.document_id,
                        snapshot.domain,
                        snapshot.state,
                        snapshot.readiness_hash,
                        snapshot.revision,
                    ]
                    for snapshot in members
                ],
                "pins": pins,
                "profile_fingerprint": PROFILE_FINGERPRINT,
                "project_id": project_id,
            }
        )
    )
    return ProjectReadiness(
        project_id=project_id,
        evaluation_as_of=evaluation_as_of,
        state=state,
        issues=tuple(issues),
        members=tuple(members),
        result_hash=result_hash,
    )


class ProjectReadinessError(RuntimeError):
    """Fail-closed assessment refusal with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


class ProjectReadinessService:
    """Deterministic, read-only project readiness assessor."""

    def __init__(
        self,
        store: SQLiteDocumentStore,
        pid_service: DocumentService,
        cable_service: CableService,
    ) -> None:
        self._store = store
        self._pid = pid_service
        self._cable = cable_service

    def assess(self, *, project_id: str, evaluation_as_of: datetime) -> ProjectReadiness:
        if evaluation_as_of.tzinfo is None:
            raise ValueError("evaluation_as_of must be timezone-aware")
        if self._store.get_project(project_id) is None:
            # D80-3: a nonexistent Project Graph must fail closed — never
            # grade an empty member/link set as an eligible deliverable.
            raise ProjectReadinessError(
                "project_not_found", f"project {project_id!r} does not exist"
            )

        members = self._member_snapshots(project_id, evaluation_as_of)
        link_rows = []
        for row in self._store.list_active_engineering_links(project_id):  # ordered by link_id
            enriched = dict(row)
            enriched["_registry_source"] = self._store.document_domain(
                str(row["source_document_id"])
            )
            enriched["_registry_target"] = self._store.document_domain(
                str(row["target_document_id"])
            )
            enriched["_source_revision"] = self._current_source_revision(row)
            enriched["_target_revision"] = self._current_target_revision(row)
            enriched["_source_reference_exists"] = self._source_reference_exists(
                str(row["source_document_id"]),
                str(row["source_object_ref"]),
                str(row["source_endpoint"]),
            )
            enriched["_target_object_exists"] = self._element_exists(
                str(row["target_document_id"]), str(row["target_object_ref"])
            )
            link_rows.append(enriched)
        return assess_project_readiness_core(
            project_id=project_id,
            evaluation_as_of=evaluation_as_of,
            member_snapshots=members,
            link_rows=link_rows,
        )

    # ---- internals (pure reads) ----

    def _member_snapshots(
        self, project_id: str, evaluation_as_of: datetime
    ) -> list[MemberReadinessSnapshot]:
        rows = sorted(self._store.list_project_documents(project_id))
        snapshots = []
        for document_id, domain, _added_at in rows:
            if domain == "cable":
                readiness = assess_cable_document(self._cable, document_id)
                snapshots.append(
                    MemberReadinessSnapshot(
                        document_id=document_id,
                        domain="cable",
                        state=readiness.state,
                        readiness_hash=readiness.result_hash,
                        revision=readiness.revision,
                    )
                )
            else:
                readiness = assess_document_release_readiness(
                    self._pid,
                    document_id,
                    load_profile(),
                    project_id=project_id,
                    now=evaluation_as_of,
                )
                snapshots.append(
                    MemberReadinessSnapshot(
                        document_id=document_id,
                        domain="pid",
                        state=readiness.state,
                        readiness_hash=readiness.readiness_hash,
                        revision=readiness.revision,
                    )
                )
        return snapshots

    def _current_source_revision(self, row: dict[str, Any]) -> int | None:
        envelope = self._store.get_cable_envelope(str(row["source_document_id"]))
        if envelope is None:
            return None  # absence stays a missing_object finding, not staleness
        return int(envelope[0])

    def _current_target_revision(self, row: dict[str, Any]) -> int | None:
        stored = self._store.get(str(row["target_document_id"]))
        if stored is None:
            return None
        return stored.document.revision

    def _source_reference_exists(
        self, cable_id: str, segment_id: str, endpoint: str
    ) -> bool:
        if endpoint not in ("from", "to"):
            return False
        try:
            view = self._cable.load(cable_id)
        except Exception:
            return False
        return any(segment.id == segment_id for segment in view.document.segments)

    def _element_exists(self, pid_id: str, element_id: str) -> bool:
        try:
            document = self._pid.get_document(pid_id)
        except Exception:
            return False
        return any(element.id == element_id for element in document.elements)
