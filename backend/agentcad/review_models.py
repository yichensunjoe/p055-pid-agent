"""M9-WS1: the engineering review governance model — threads, comments, human approvals.

Governance facts live beside the engineering document, never inside it: a review
thread or an approval must not move any semantic/layout/geometry digest. The one
digest this module owns is :func:`review_snapshot_digest` — threads + comments +
thread lifecycle facts + the trusted actor identities, with approvals, the
governance sequence, timestamps and read-time derived states all excluded.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import ConfigDict

from .models import StrictModel

REVIEW_SNAPSHOT_VERSION = "review-snapshot/1"

ThreadStatus = Literal["open", "resolved", "reopened"]
ApprovalStatus = Literal["requested", "approved", "rejected", "stale", "superseded"]
ApprovalDecision = Literal["approved", "rejected"]
ActorKind = Literal["operator", "agent"]


def _now() -> datetime:
    return datetime.now(UTC)


class ReviewThread(StrictModel):
    """One review thread anchored to a drawing revision (and optionally an element).

    ``anchor_revision`` is pinned at creation and never drifts: a thread whose anchor
    revision is not current renders as stale; an element anchor whose element is gone
    renders as orphaned. Both are read-time derived states — the record is never
    re-anchored to whatever looks similar.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    thread_id: str
    document_id: str
    anchor_revision: int
    element_id: str | None = None
    subject_text: str = ""
    status: ThreadStatus = "open"
    created_by: str
    created_by_kind: ActorKind
    created_at: datetime
    resolved_by: str | None = None
    resolved_by_kind: ActorKind | None = None
    resolved_at: datetime | None = None
    resolution_note: str = ""
    addressed_at_revision: int | None = None
    reopened_by: str | None = None
    reopened_at: datetime | None = None
    seq: int


class ReviewComment(StrictModel):
    """One append-only entry in a thread. Entries are never edited or deleted."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    comment_id: str
    thread_id: str
    entry_seq: int
    body: str
    author: str
    author_kind: ActorKind
    created_at: datetime


class EngineeringApproval(StrictModel):
    """A human engineering approval — deliberately NOT the agent/tool ToolApproval.

    Status machine (frozen): ``requested → approved | rejected``; afterwards
    ``approved``/``requested → stale`` (revision drift or a reopened/new thread);
    ``superseded`` is reserved for future housekeeping. There is no persistent
    "active": liveness is derived — ``approved`` whose binding (revision + review
    digest + fresh readiness at decision time) still holds.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    approval_id: str
    document_id: str
    engineering_revision: int
    review_snapshot_digest: str
    requested_by: str
    requested_by_kind: ActorKind
    requested_at: datetime
    request_readiness_hash: str = ""
    decision: ApprovalDecision | None = None
    decided_by: str | None = None
    decided_by_kind: ActorKind | None = None
    decided_at: datetime | None = None
    decision_readiness_hash: str = ""
    status: ApprovalStatus = "requested"
    invalidation_reason: str | None = None
    seq: int


class ReviewState(StrictModel):
    """The whole governance surface of one document."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    document_id: str
    governance_seq: int = 0
    threads: tuple[ReviewThread, ...] = ()
    comments: tuple[ReviewComment, ...] = ()
    approvals: tuple[EngineeringApproval, ...] = ()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def review_snapshot_digest(state: ReviewState) -> str:
    """The deterministic governance digest the WS1 gate froze.

    Covers review facts only: threads (with lifecycle facts and the trusted actor
    identities — who resolved an issue is a governance fact), and comments.
    Excludes approvals entirely (an approval binds this digest; including approvals
    would be circular), the governance sequence, timestamps, and read-time derived
    stale/orphaned states.
    """

    projection = {
        "version": REVIEW_SNAPSHOT_VERSION,
        "threads": [
            {
                "thread_id": thread.thread_id,
                "anchor_revision": thread.anchor_revision,
                "element_id": thread.element_id,
                "status": thread.status,
                "created_by": thread.created_by,
                "created_by_kind": thread.created_by_kind,
                "resolved_by": thread.resolved_by,
                "resolved_by_kind": thread.resolved_by_kind,
                "resolution_note": thread.resolution_note,
                "addressed_at_revision": thread.addressed_at_revision,
                "reopened_by": thread.reopened_by,
            }
            for thread in sorted(state.threads, key=lambda item: item.thread_id)
        ],
        "comments": [
            {
                "comment_id": comment.comment_id,
                "thread_id": comment.thread_id,
                "entry_seq": comment.entry_seq,
                "body": comment.body,
                "author": comment.author,
                "author_kind": comment.author_kind,
            }
            for comment in sorted(
                state.comments, key=lambda item: (item.thread_id, item.entry_seq)
            )
        ],
    }
    return _hash(projection)


def new_thread_id(state: ReviewState, *, document_id: str, anchor_revision: int, element_id: str | None) -> str:
    material = f"{document_id}|{anchor_revision}|{element_id or ''}|{state.governance_seq + 1}"
    return "rt_" + _hash(material)[:12]


def new_comment_id(thread_id: str, entry_seq: int, body: str, author: str) -> str:
    return "rc_" + _hash(f"{thread_id}|{entry_seq}|{body}|{author}")[:12]


def new_approval_id(document_id: str, revision: int, seq: int) -> str:
    return "ap_" + _hash(f"{document_id}|{revision}|{seq}")[:12]


def utcnow() -> datetime:
    return _now()
