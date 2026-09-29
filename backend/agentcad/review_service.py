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
* approval liveness is reconciled uniformly: every governance mutation re-checks
  every live approval binding (revision drift first, then the mutation's own
  Gate-frozen invalidation reason) and persists the stale mark together with one
  ``approval.invalidated`` audit per invalidated approval, in the same transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .api_release import EvidenceBuildError, build_release_evidence_package
from .audit_models import AuditRecordDraft
from .release_validator import (
    ReleaseReadiness,
    assess_document_release_readiness,
    assess_release_readiness,
)
from .review_models import (
    ActorKind,
    EngineeringApproval,
    ReleaseRecord,
    ReviewComment,
    ReviewState,
    ReviewThread,
    new_approval_id,
    new_comment_id,
    new_release_id,
    new_thread_id,
    review_snapshot_digest,
    utcnow,
)
from .service import DocumentService
from .store import ReviewStateConflictError, SQLiteDocumentStore
from .validation_engine import run_validation


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


class ReleaseOpenThreadsError(ReviewWorkflowError):
    code = "release_open_threads"


class ReleaseApprovalNotLiveError(ReviewWorkflowError):
    code = "release_approval_not_live"


class ReleaseReadinessNotEligibleError(ReviewWorkflowError):
    code = "release_readiness_not_eligible"


class ReleaseAlreadyExistsError(ReviewWorkflowError):
    code = "release_already_exists"


class ReleaseConflictError(ReviewStateConflict):
    code = "release_conflict"


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
        invalidate_reason: str | None = None,
    ) -> ReviewState:
        next_state = state.model_copy(update={"governance_seq": state.governance_seq + 1})
        # Round-2 Gate freeze: the unified approval reconcile runs inside EVERY
        # governance mutation (not just reopen), so a live approval can never
        # survive a review-surface move it did not vet. The reconciled state and
        # one approval.invalidated audit per invalidated approval commit in the
        # same SQLite transaction as the mutation that triggered them.
        next_state, invalidated, superseded = self._reconcile(next_state, reason=invalidate_reason)
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
        invalidation_drafts = tuple(
            AuditRecordDraft(
                event_type="approval.invalidated",
                actor=actor.identity,
                surface="rest",
                tool_name="review_workflow",
                status="applied",
                document_id=state.document_id,
                label=f"Approval {approval_id} invalidated",
                evidence={
                    "actor_kind": actor.kind,
                    "governance_seq": next_state.governance_seq,
                    "approval_id": approval_id,
                    "reason": reason,
                    "trigger_event": event_type,
                },
            )
            for approval_id, reason in invalidated
        ) + tuple(
            AuditRecordDraft(
                event_type="release.superseded",
                actor=actor.identity,
                surface="rest",
                tool_name="review_workflow",
                status="applied",
                document_id=state.document_id,
                label=f"Release {release_id} superseded",
                evidence={
                    "actor_kind": actor.kind,
                    "governance_seq": next_state.governance_seq,
                    "release_id": release_id,
                    "reason": reason,
                    "trigger_event": event_type,
                },
            )
            for release_id, reason in superseded
        )
        try:
            if expected_seq == 0 and self.store.get_review_state(state.document_id) is None:
                self.store.create_review_state(next_state, audit=draft, extra_audits=invalidation_drafts)
            else:
                self.store.save_review_state(next_state, expected_governance_seq=expected_seq, audit=draft, extra_audits=invalidation_drafts)
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
            invalidate_reason="review_digest_changed",
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
            invalidate_reason="review_digest_changed",
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
        if thread.status not in {"open", "reopened"}:
            raise ReviewWorkflowError(
                f"thread {thread_id} is {thread.status}; only open or reopened threads resolve",
                details={"code": "review_thread_not_open"},
            )
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
        # Reopening an unresolved thread invalidates any live approval: the review
        # surface moved after the decision, so the decision's closed-loop claim no
        # longer stands. Handled by the unified reconcile inside _persist.
        return self._persist(
            next_state,
            expected_seq=expected_governance_seq,
            audit_label=f"Reopened {thread_id}",
            actor=actor,
            event_type="review.thread.reopened",
            metadata={"thread_id": thread_id},
            invalidate_reason="review_reopened",
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

    # ------------------------------------------------------------------ release

    def release_document(
        self,
        *,
        document_id: str,
        actor: Actor,
        expected_governance_seq: int,
        now: Any = None,
    ) -> ReviewState:
        """The one-shot formal release (M9-WS2, Gate-frozen two phases).

        Guard order is frozen: actor trust → open threads → approval live →
        one-live-release-per-binding → fresh readiness → package build. A guard
        refusal writes no state; F1/F2/F3 additionally record exactly one
        audit-only ``release.denied`` fact (no governance_seq movement), while an
        untrusted actor (F4) never reaches this layer and injects no audit.

        Phase A pins ONE in-memory Document snapshot — the only drawing read —
        and stages the whole evidence package in memory without touching the
        database write path. Phase B is the store's single atomic
        ``commit_release`` primitive: revision recheck, governance CAS, state
        write, immutable package row and ``release.released`` audit in one short
        transaction; any failure rolls back everything.
        """

        self._require_operator(actor, "release a drawing")
        state = self._raw_state(document_id)
        try:
            self._require_seq(state, expected_governance_seq)
        except ReviewStateConflict as exc:
            raise ReleaseConflictError(str(exc)) from exc
        open_threads = [
            thread.thread_id for thread in state.threads if thread.status in {"open", "reopened"}
        ]
        if open_threads:
            self._deny_release(
                state,
                actor=actor,
                error_code="release_open_threads",
                evidence={"open_threads": open_threads},
            )
        document = self.service.get_document(document_id)  # the one pinned read
        current_digest = review_snapshot_digest(state)
        approval = next(
            (
                item
                for item in reversed(state.approvals)
                if item.status == "approved"
                and item.engineering_revision == document.revision
                and item.review_snapshot_digest == current_digest
            ),
            None,
        )
        if approval is None:
            self._deny_release(
                state,
                actor=actor,
                error_code="release_approval_not_live",
                evidence={"engineering_revision": document.revision},
            )
        existing = next(
            (
                item
                for item in reversed(state.releases)
                if item.state == "released"
                and item.engineering_revision == document.revision
                and item.review_snapshot_digest == current_digest
            ),
            None,
        )
        if existing is not None:
            raise ReleaseAlreadyExistsError(
                f"release {existing.release_id} already binds revision "
                f"{document.revision} and the current review snapshot; it must "
                "supersede before a new release is possible"
            )
        if self._readiness_profile is None:
            raise ReviewWorkflowError(
                "review service has no readiness profile configured",
                details={"code": "readiness_profile_missing"},
            )

        # ---------------------------------------------------------- Phase A
        moment = now if now is not None else utcnow()
        profile = self._readiness_profile
        validation_result = run_validation(
            document,
            self.service.symbols,
            profile,
            service=self.service,
            now=moment,
        )
        readiness = assess_release_readiness(
            document,
            self.service.symbols,
            profile,
            service=self.service,
            now=moment,
            document_name=document.name,
        )
        if readiness.validation_hash != validation_result.result_hash:
            raise ReviewWorkflowError(
                "release readiness and validation evidence diverged on the same "
                "pinned snapshot — refusing to release",
                details={"code": "release_evidence_divergence"},
            )
        if readiness.state != "eligible":
            self._deny_release(
                state,
                actor=actor,
                error_code="release_readiness_not_eligible",
                evidence={
                    "readiness_state": readiness.state,
                    "reasons": readiness.reasons,
                    "readiness_hash": readiness.readiness_hash,
                },
            )
        release = ReleaseRecord(
            release_id=new_release_id(document_id, document.revision, state.governance_seq + 1),
            document_id=document_id,
            state="released",
            approval_id=approval.approval_id,
            engineering_revision=document.revision,
            review_snapshot_digest=current_digest,
            readiness_hash=readiness.readiness_hash,
            evidence_manifest_hash="",
            package_sha256="",
            released_by=actor.identity,
            released_by_kind=actor.kind,
            released_at=moment,
            seq=state.governance_seq + 1,
        )
        try:
            package = build_release_evidence_package(
                document=document,
                state_release=release,
                approval_json=approval.model_dump(mode="json"),
                readiness=readiness,
                validation_result=validation_result,
                service=self.service,
                store=self.store,
            )
        except EvidenceBuildError as exc:
            raise ReviewWorkflowError(
                str(exc), details={"code": "release_evidence_build_failed"}
            ) from exc
        release = release.model_copy(
            update={
                "evidence_manifest_hash": package.manifest_sha256,
                "package_sha256": package.package_sha256,
            }
        )

        # ---------------------------------------------------------- Phase B
        next_state = state.model_copy(
            update={
                "governance_seq": state.governance_seq + 1,
                "releases": (*state.releases, release),
            }
        )
        # Release creation never moves the review digest or the revision, so the
        # reconcile persists any not-yet-persisted supersede of older releases in
        # the same transaction — it can never touch the approval being consumed.
        next_state, _invalidated, superseded = self._reconcile(next_state, reason=None)
        draft = AuditRecordDraft(
            event_type="release.released",
            actor=actor.identity,
            surface="rest",
            tool_name="review_workflow",
            status="applied",
            document_id=document_id,
            label=f"Released revision {document.revision}",
            evidence={
                "actor_kind": actor.kind,
                "governance_seq": next_state.governance_seq,
                "release_id": release.release_id,
                "approval_id": approval.approval_id,
                "engineering_revision": document.revision,
                "evidence_manifest_hash": package.manifest_sha256,
                "package_sha256": package.package_sha256,
                "verified_through_ordinal": package.verified_through_ordinal,
                "verified_global_tip_hash": package.verified_global_tip_hash,
            },
        )
        supersede_drafts = tuple(
            AuditRecordDraft(
                event_type="release.superseded",
                actor=actor.identity,
                surface="rest",
                tool_name="review_workflow",
                status="applied",
                document_id=document_id,
                label=f"Release {release_id} superseded",
                evidence={
                    "actor_kind": actor.kind,
                    "governance_seq": next_state.governance_seq,
                    "release_id": release_id,
                    "reason": reason,
                    "trigger_event": "release.released",
                },
            )
            for release_id, reason in superseded
        )
        try:
            self.store.commit_release(
                next_state,
                expected_governance_seq=expected_governance_seq,
                expected_engineering_revision=document.revision,
                release_id=release.release_id,
                manifest_sha256=package.manifest_sha256,
                package_sha256=package.package_sha256,
                package_blob=package.zip_bytes,
                audit=draft,
                extra_audits=supersede_drafts,
            )
        except ReviewStateConflictError as exc:
            raise ReleaseConflictError(str(exc)) from exc
        return next_state

    def _deny_release(
        self,
        state: ReviewState,
        *,
        actor: Actor,
        error_code: str,
        evidence: dict[str, Any],
    ) -> None:
        """Record the one audit-only denial fact, then refuse (no state writes).

        Gate-frozen: F1/F2/F3 write exactly one ``release.denied`` audit with
        status ``rejected`` and move no governance sequence — a denial is a
        governance fact, not a mutation. F4 never reaches here (the actor check
        precedes), so an untrusted caller can never inject into the chain.
        """

        self.store.record_audit_event(
            AuditRecordDraft(
                event_type="release.denied",
                actor=actor.identity,
                surface="rest",
                tool_name="review_workflow",
                status="rejected",
                document_id=state.document_id,
                label=f"Release denied: {error_code}",
                evidence={
                    "actor_kind": actor.kind,
                    "error_code": error_code,
                    **evidence,
                },
            )
        )
        if error_code == "release_open_threads":
            raise ReleaseOpenThreadsError(
                f"unresolved review threads block release: {evidence['open_threads']}"
            )
        if error_code == "release_approval_not_live":
            raise ReleaseApprovalNotLiveError(
                "release requires an approved engineering approval whose revision "
                f"and review snapshot bindings still hold; none is live for revision "
                f"{evidence['engineering_revision']}"
            )
        raise ReleaseReadinessNotEligibleError(
            f"fresh release readiness is {evidence['readiness_state']!r}; release "
            f"requires 'eligible' (fresh hash {evidence['readiness_hash']})"
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

    def _reconcile(
        self, state: ReviewState, *, reason: str | None
    ) -> tuple[ReviewState, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
        """The single governance liveness reconcile (Round-2 + WS2 Gate freeze).

        Runs inside every governance mutation, before persistence, so the stored
        marks and their audit facts commit atomically with the mutation that
        triggered them:

        * approvals: engineering revision drift always wins — an approval bound
          to an old revision is stale (``revision_changed``) no matter what else
          happened; otherwise, when the mutation moved the review surface (new
          open thread, comment, reopen — the caller passes its Gate-frozen
          reason, or None for mutations that never invalidate: resolve, request,
          decide, release), every live approval goes ``stale`` with that reason;
        * releases: a released record is superseded when EITHER binding moves —
          the engineering revision (``revision_changed``) or the review snapshot
          digest (``review_snapshot_changed``). Releases never enter the review
          digest, so persisting a supersede can never cascade into another
          supersede; already-superseded records are never re-touched, so each
          transition produces exactly one ``release.superseded`` audit.
        """

        current_revision = self.service.get_document(state.document_id).revision
        current_digest = review_snapshot_digest(state)
        approvals: list[EngineeringApproval] = []
        invalidated: list[tuple[str, str]] = []
        for approval in state.approvals:
            if approval.status not in {"requested", "approved"}:
                approvals.append(approval)
                continue
            if approval.engineering_revision != current_revision:
                approvals.append(
                    approval.model_copy(
                        update={"status": "stale", "invalidation_reason": "revision_changed"}
                    )
                )
                invalidated.append((approval.approval_id, "revision_changed"))
            elif reason is not None:
                approvals.append(
                    approval.model_copy(update={"status": "stale", "invalidation_reason": reason})
                )
                invalidated.append((approval.approval_id, reason))
            else:
                approvals.append(approval)

        releases: list[ReleaseRecord] = []
        superseded: list[tuple[str, str]] = []
        for release in state.releases:
            if release.state != "released":
                releases.append(release)
                continue
            if release.engineering_revision != current_revision:
                releases.append(
                    release.model_copy(
                        update={"state": "superseded", "superseded_reason": "revision_changed"}
                    )
                )
                superseded.append((release.release_id, "revision_changed"))
            elif release.review_snapshot_digest != current_digest:
                releases.append(
                    release.model_copy(
                        update={
                            "state": "superseded",
                            "superseded_reason": "review_snapshot_changed",
                        }
                    )
                )
                superseded.append((release.release_id, "review_snapshot_changed"))
            else:
                releases.append(release)

        if not invalidated and not superseded:
            return state, (), ()
        return (
            state.model_copy(
                update={"approvals": tuple(approvals), "releases": tuple(releases)}
            ),
            tuple(invalidated),
            tuple(superseded),
        )

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
