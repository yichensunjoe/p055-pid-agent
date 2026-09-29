"""M9-WS1: the review workflow service — governance logic over the store's review plane.

Responsibilities frozen by the WS1 gate:
* governance mutations never touch the engineering transaction path — the drawing's
  revision is provably untouched by every operation here;
* every mutation is a CAS on ``expected_governance_seq``, atomic with its audit
  record, so concurrent reviewers can never lose each other's comments;
* human decisions (resolve / reopen / decide) require the trusted operator context
  (see :class:`OperatorIdentity`) — an agent can request and comment but can never
  approve, reject or close review threads;
* readiness binding is decision-time fresh: requesting records the request-time
  readiness hash as evidence, approving re-runs readiness and requires an eligible
  state, and stores the decision-time hash. The two are never required to match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .audit_models import AuditRecordDraft
from .release_validator import ReleaseReadiness, assess_document_release_readiness
from .review_models import (
    ActorKind,
    EngineeringApproval,
    ReviewComment,
    ReviewState,
    ReviewThread,
    new_approval_id,
    new_comment_id,
    new_thread_id,
    review_snapshot_digest,
    utcnow,
)
from .service import DocumentService
from .store import ReviewStateConflictError, SQLiteDocumentStore


class ReviewWorkflowError(RuntimeError):
    """Base for structured review-workflow refusals."""

    code = "review_workflow_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ReviewThreadNotFoundError(ReviewWorkflowError):
    code = "review_thread_not_found"


class ActorNotTrustedError(ReviewWorkflowError):
    code = "actor_not_trusted"


class ApprovalBlockedOpenThreadsError(ReviewWorkflowError):
    code = "approval_blocked_open_threads"


class ApprovalRevisionMismatchError(ReviewWorkflowError):
    code = "approval_revision_mismatch"


class ApprovalNotEligibleError(ReviewWorkflowError):
    code = "approval_not_eligible"


class ApprovalAlreadyActiveError(ReviewWorkflowError):
    code = "approval_already_active"


class ReviewStateConflict(ReviewWorkflowError):
    code = "review_state_conflict"


class ApprovalNotFoundError(ReviewWorkflowError):
    code = "approval_not_found"


@dataclass(frozen=True)
class Actor:
    """Who is acting. ``kind`` is decided by the transport, never by the caller."""

    identity: str
    kind: ActorKind


@dataclass(frozen=True)
class OperatorIdentity:
    """The v1 trusted-operator configuration (Gate-frozen local mode only).

    Human write operations exist only when the deployment is ``local`` AND an
    operator identity is explicitly configured. Shared deployments fail closed.
    """

    identity: str

    @classmethod
    def from_settings(cls, settings: Any) -> OperatorIdentity | None:
        identity = getattr(settings, "operator_identity", None)
        if getattr(settings, "deployment_mode", "local") != "local" or not identity:
            return None
        return cls(identity=str(identity))


class ReviewService:
    def __init__(
        self,
        store: SQLiteDocumentStore,
        service: DocumentService,
        *,
        readiness_profile: Any = None,
    ) -> None:
        self.store = store
        self.service = service
        self._readiness_profile = readiness_profile

    # ------------------------------------------------------------------ reads

    def get_state(self, document_id: str) -> ReviewState:
        """Pure read: zero writes, zero audit. Derived stale/orphaned marks are
        computed by the API layer at read time; this layer never persists them."""

        return self._raw_state(document_id)

    def snapshot_digest(self, document_id: str) -> str:
        return review_snapshot_digest(self._raw_state(document_id))

    def _raw_state(self, document_id: str) -> ReviewState:
        state = self.store.get_review_state(document_id)
        return state if state is not None else ReviewState(document_id=document_id)

    # ------------------------------------------------------------- governance

    def _persist(
        self,
        state: ReviewState,
        *,
        expected_seq: int,
        audit_label: str,
        actor: Actor,
        event_type: str,
        metadata: dict[str, Any],
    ) -> ReviewState:
        next_state = state.model_copy(update={"governance_seq": state.governance_seq + 1})
        draft = AuditRecordDraft(
            event_type=event_type,
            actor=actor.identity,
            surface="rest",
            tool_name="review_workflow",
            status="applied",
            document_id=state.document_id,
            label=audit_label,
            evidence={
                "actor_kind": actor.kind,
                "governance_seq": next_state.governance_seq,
                **metadata,
            },
        )
        try:
            if expected_seq == 0 and self.store.get_review_state(state.document_id) is None:
                self.store.create_review_state(next_state, audit=draft)
            else:
                self.store.save_review_state(next_state, expected_governance_seq=expected_seq, audit=draft)
        except ReviewStateConflictError as exc:
            raise ReviewStateConflict(str(exc)) from exc
        return next_state

    def create_thread(
        self,
        *,
        document_id: str,
        actor: Actor,
        body: str,
        element_id: str | None = None,
        subject_text: str = "",
        expected_governance_seq: int,
    ) -> ReviewState:
        state = self._raw_state(document_id)
        self._require_seq(state, expected_governance_seq)
        document = self.service.get_document(document_id)
        if element_id is not None and all(
            element.id != element_id for element in document.elements
        ):
            raise ReviewThreadNotFoundError(
                f"element {element_id!r} is not in document {document_id!r}"
            )
        now = utcnow()
        thread = ReviewThread(
            thread_id=new_thread_id(
                state, document_id=document_id, anchor_revision=document.revision, element_id=element_id
            ),
            document_id=document_id,
            anchor_revision=document.revision,
            element_id=element_id,
            subject_text=subject_text,
            status="open",
            created_by=actor.identity,
            created_by_kind=actor.kind,
            created_at=now,
            seq=state.governance_seq + 1,
        )
        comment = ReviewComment(
            comment_id=new_comment_id(thread.thread_id, 1, body, actor.identity),
            thread_id=thread.thread_id,
            entry_seq=1,
            body=body,
            author=actor.identity,
            author_kind=actor.kind,
            created_at=now,
        )
        next_state = state.model_copy(
            update={"threads": (*state.threads, thread), "comments": (*state.comments, comment)}
        )
        return self._persist(
            next_state,
            expected_seq=expected_governance_seq,
            audit_label=f"Review thread {thread.thread_id}",
            actor=actor,
            event_type="review.thread.created",
            metadata={"thread_id": thread.thread_id, "element_id": element_id},
        )

    def add_comment(
        self,
        *,
        document_id: str,
        thread_id: str,
        actor: Actor,
        body: str,
        expected_governance_seq: int,
    ) -> ReviewState:
        state = self._raw_state(document_id)
        self._require_seq(state, expected_governance_seq)
        self._thread(state, thread_id)  # existence check
        entry_seq = max((c.entry_seq for c in state.comments if c.thread_id == thread_id), default=0) + 1
        comment = ReviewComment(
            comment_id=new_comment_id(thread_id, entry_seq, body, actor.identity),
            thread_id=thread_id,
            entry_seq=entry_seq,
            body=body,
            author=actor.identity,
            author_kind=actor.kind,
            created_at=utcnow(),
        )
        next_state = state.model_copy(update={"comments": (*state.comments, comment)})
        return self._persist(
            next_state,
            expected_seq=expected_governance_seq,
            audit_label=f"Comment on {thread_id}",
            actor=actor,
            event_type="review.comment.posted",
            metadata={"thread_id": thread_id, "comment_id": comment.comment_id},
        )

    def resolve_thread(
        self,
        *,
        document_id: str,
        thread_id: str,
        actor: Actor,
        resolution_note: str,
        expected_governance_seq: int,
    ) -> ReviewState:
        self._require_operator(actor, "resolve a review thread")
        if not resolution_note.strip():
            raise ReviewWorkflowError(
                "resolving a thread requires a non-empty resolution_note",
                details={"code": "resolution_note_required"},
            )
        state = self._raw_state(document_id)
        self._require_seq(state, expected_governance_seq)
        thread = self._thread(state, thread_id)
        current_revision = self.service.get_document(document_id).revision
        # Stale / orphaned threads may be closed by a trusted operator — the anchor
        # never moves; the closure records where the drawing actually stands.
        updated = thread.model_copy(
            update={
                "status": "resolved",
                "resolved_by": actor.identity,
                "resolved_by_kind": actor.kind,
                "resolved_at": utcnow(),
                "resolution_note": resolution_note.strip(),
                "addressed_at_revision": current_revision,
            }
        )
        next_state = self._replace_thread(state, updated)
        return self._persist(
            next_state,
            expected_seq=expected_governance_seq,
            audit_label=f"Resolved {thread_id}",
            actor=actor,
            event_type="review.thread.resolved",
            metadata={"thread_id": thread_id, "addressed_at_revision": current_revision},
        )

    def reopen_thread(
        self,
        *,
        document_id: str,
        thread_id: str,
        actor: Actor,
        expected_governance_seq: int,
    ) -> ReviewState:
        self._require_operator(actor, "reopen a review thread")
        state = self._raw_state(document_id)
        self._require_seq(state, expected_governance_seq)
        thread = self._thread(state, thread_id)
        if thread.status != "resolved":
            raise ReviewWorkflowError(
                f"thread {thread_id} is {thread.status}; only resolved threads reopen",
                details={"code": "review_thread_not_resolved"},
            )
        updated = thread.model_copy(
            update={"status": "reopened", "reopened_by": actor.identity, "reopened_at": utcnow()}
        )
        next_state = self._replace_thread(state, updated)
        # A reopened (or newly created) unresolved thread invalidates any live
        # approval: the review surface moved after the decision, so the decision's
        # closed-loop claim no longer stands.
        next_state = self._invalidate_approvals(next_state, reason="review_reopened")
        return self._persist(
            next_state,
            expected_seq=expected_governance_seq,
            audit_label=f"Reopened {thread_id}",
            actor=actor,
            event_type="review.thread.reopened",
            metadata={"thread_id": thread_id},
        )

    # --------------------------------------------------------------- approvals

    def request_approval(
        self,
        *,
        document_id: str,
        actor: Actor,
        expected_governance_seq: int,
    ) -> ReviewState:
        state = self._raw_state(document_id)
        self._require_seq(state, expected_governance_seq)
        live = self._live_approval(state)
        if live is not None and live.status == "approved":
            raise ApprovalAlreadyActiveError(
                f"approval {live.approval_id} is already approved for revision "
                f"{live.engineering_revision}"
            )
        approvals = state.approvals
        if live is not None and live.status == "requested":
            # A still-undecided request is superseded by the fresh one: the review
            # surface moved (that is why a new request exists), so the old binding
            # must not shadow the new.
            approvals = tuple(
                approval.model_copy(
                    update={
                        "status": "superseded",
                        "invalidation_reason": "superseded_by_new_request",
                    }
                )
                if approval.approval_id == live.approval_id
                else approval
                for approval in approvals
            )
            state = state.model_copy(update={"approvals": approvals})
        document = self.service.get_document(document_id)
        readiness = self._readiness(document_id)
        approval = EngineeringApproval(
            approval_id=new_approval_id(document_id, document.revision, state.governance_seq + 1),
            document_id=document_id,
            engineering_revision=document.revision,
            review_snapshot_digest=review_snapshot_digest(state),
            requested_by=actor.identity,
            requested_by_kind=actor.kind,
            requested_at=utcnow(),
            request_readiness_hash=readiness.readiness_hash,
            seq=state.governance_seq + 1,
        )
        next_state = state.model_copy(update={"approvals": (*state.approvals, approval)})
        return self._persist(
            next_state,
            expected_seq=expected_governance_seq,
            audit_label=f"Approval requested for revision {document.revision}",
            actor=actor,
            event_type="approval.requested",
            metadata={"approval_id": approval.approval_id, "engineering_revision": document.revision},
        )

    def decide_approval(
        self,
        *,
        document_id: str,
        approval_id: str,
        actor: Actor,
        decision: Literal["approved", "rejected"],
        expected_governance_seq: int,
    ) -> ReviewState:
        self._require_operator(actor, f"{decision} an approval")
        state = self._raw_state(document_id)
        self._require_seq(state, expected_governance_seq)
        approval = next((a for a in state.approvals if a.approval_id == approval_id), None)
        if approval is None:
            raise ApprovalNotFoundError(f"approval {approval_id!r} not found")
        if approval.status != "requested":
            raise ReviewWorkflowError(
                f"approval {approval_id} is {approval.status}; only requested approvals can be decided",
                details={"code": "approval_not_requested"},
            )
        document = self.service.get_document(document_id)
        if approval.engineering_revision != document.revision:
            raise ApprovalRevisionMismatchError(
                f"approval binds revision {approval.engineering_revision}, drawing is at "
                f"{document.revision}; request a new approval"
            )
        if review_snapshot_digest(state) != approval.review_snapshot_digest:
            raise ReviewWorkflowError(
                "review state moved since the approval was requested; request a new approval",
                details={"code": "approval_review_snapshot_mismatch"},
            )
        open_threads = [
            t.thread_id for t in state.threads if t.status in {"open", "reopened"}
        ]
        if decision == "approved" and open_threads:
            raise ApprovalBlockedOpenThreadsError(
                f"unresolved review threads block approval: {open_threads}"
            )
        # Decision-time fresh readiness: the request-time hash is evidence only.
        readiness = self._readiness(document_id)
        if decision == "approved" and readiness.state != "eligible":
            raise ApprovalNotEligibleError(
                f"fresh release readiness is {readiness.state!r}; approval requires 'eligible'"
            )
        updated = approval.model_copy(
            update={
                "decision": decision,
                "decided_by": actor.identity,
                "decided_by_kind": actor.kind,
                "decided_at": utcnow(),
                "decision_readiness_hash": readiness.readiness_hash,
                "status": decision,
                "invalidation_reason": None if decision == "approved" else "rejected",
            }
        )
        next_state = self._replace_approval(state, updated)
        return self._persist(
            next_state,
            expected_seq=expected_governance_seq,
            audit_label=f"Approval {approval_id} {decision}",
            actor=actor,
            event_type="approval.decided",
            metadata={
                "approval_id": approval_id,
                "decision": decision,
                "readiness_hash": readiness.readiness_hash,
                "readiness_state": readiness.state,
            },
        )

    # ----------------------------------------------------------------- helpers

    def _readiness(self, document_id: str) -> ReleaseReadiness:
        if self._readiness_profile is None:
            raise ReviewWorkflowError(
                "review service has no readiness profile configured",
                details={"code": "readiness_profile_missing"},
            )
        return assess_document_release_readiness(
            self.service, document_id, self._readiness_profile
        )

    def _live_approval(self, state: ReviewState) -> EngineeringApproval | None:
        current = self.service.get_document(state.document_id).revision
        for approval in reversed(state.approvals):
            if approval.status in {"requested", "approved"} and approval.engineering_revision == current:
                return approval
        return None

    def _invalidate_approvals(self, state: ReviewState, *, reason: str) -> ReviewState:
        invalidated = tuple(
            approval.model_copy(
                update={"status": "stale", "invalidation_reason": reason}
            )
            if approval.status in {"requested", "approved"}
            else approval
            for approval in state.approvals
        )
        return state.model_copy(update={"approvals": invalidated})

    def _replace_thread(self, state: ReviewState, thread: ReviewThread) -> ReviewState:
        return state.model_copy(
            update={
                "threads": tuple(
                    thread if item.thread_id == thread.thread_id else item
                    for item in state.threads
                )
            }
        )

    def _replace_approval(self, state: ReviewState, approval: EngineeringApproval) -> ReviewState:
        return state.model_copy(
            update={
                "approvals": tuple(
                    approval if item.approval_id == approval.approval_id else item
                    for item in state.approvals
                )
            }
        )

    def _thread(self, state: ReviewState, thread_id: str) -> ReviewThread:
        thread = next((t for t in state.threads if t.thread_id == thread_id), None)
        if thread is None:
            raise ReviewThreadNotFoundError(f"review thread {thread_id!r} not found")
        return thread

    @staticmethod
    def _require_seq(state: ReviewState, expected: int) -> None:
        if state.governance_seq != expected:
            raise ReviewStateConflict(
                f"governance seq is {state.governance_seq}, request carried {expected}",
                details={"current_governance_seq": state.governance_seq},
            )

    @staticmethod
    def _require_operator(actor: Actor, action: str) -> None:
        if actor.kind != "operator":
            raise ActorNotTrustedError(
                f"only a trusted human operator can {action}; agents may request and "
                "comment but never decide"
            )
