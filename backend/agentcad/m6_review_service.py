"""M6 Phase-2B D3: the human review surface for semantic candidates.

The D2 adapter files proposals; this module is where a person answers them. It adds no new
judgement — every state change is one of the contract's declared transitions, executed by
the unchanged ``M6CandidateService`` — and four disciplines are enforced here rather than
trusted:

* **The pinned source is re-verified before every decision** (Gate D88-1 carried into
  review): the candidate's ``SourceArtifactRef`` is checked against the live store on
  existence, pinned revision and content hash. A drifted or missing source refuses the
  decision with the D2 stable codes and zero writes — a review written against evidence that
  moved would be evidence of nothing.
* **A decision and its audit fact are one transaction.** ``record_confirmation`` carries the
  confirm pair plus the audit; every other decision goes through
  ``record_review_decision(decision, audit=...)``; a reassignment's four rows (new candidate,
  its filing event, the supersede on the old candidate, the audit) commit through
  ``file_superseding_candidate``. There is no path here that writes a decision and hopes the
  audit lands later.
* **Reviewer identity is declared, never derived.** ``reviewer_identity`` comes from the
  request, verbatim; the audit evidence records the four-tuple (authentication evidence =
  ``service-token`` or ``local-process``, reviewer attribution, the decision row, and
  ``identity_assurance="declared"``). A shared deployment's Bearer token authenticates the
  request — it never becomes a person.
* **Reassignment appends, never edits.** A corrected binding files a *new* candidate
  (``derived_from`` the old one) and marks the old one superseded by it. The old evidence
  chain stays readable; history is not rewritten.
"""

from __future__ import annotations

from typing import Any, Literal

from .audit_models import AuditRecordDraft
from .m6_candidate_core import (
    M6CandidateService,
    M6CoreError,
    baseline_record,
    build_baseline_graph,
    canonical_digest,
    filing_decision,
    review_decision_id,
    value_digest,
)
from .m6_candidate_models import (
    CandidateEvidence,
    Confidence,
    ConflictBaselineRecord,
    ConflictResolutionChoice,
    ProducerRef,
    ProposedSemantics,
    ProvenanceRef,
    ReviewDecision,
    SemanticCandidate,
    SemanticPath,
)
from .m6_ingestion_contract import CANDIDATE_STATES, CANDIDATE_TRANSITIONS
from .m6_region import bbox_geometry, build_source_region, element_bbox, union_bbox
from .m6_source_adapter import (
    M6_SOURCE_ADAPTER_VERSION,
    M6_SOURCE_RULES,
    CandidateIdentityConflict,
    M6SourceAdapter,
    SourceVerificationError,
)
from .models import Document, StrictModel
from .store import M6FilingError, SQLiteDocumentStore
from .symbols import SymbolRegistry

ReviewAction = Literal["confirm", "reject", "recheck", "resolve_conflict", "reassign"]

#: One queue page never exceeds this (design baseline §8.1: paged reads stay bounded).
MAX_PAGE_SIZE = 200


class ReviewSurfaceError(M6CoreError):
    """A review-surface refusal. The code is the contract; nothing is written."""

    code = "review_surface_error"


class DecisionOutcome(StrictModel):
    """What one decision request did, returned to the caller and shown by the UI."""

    candidate_id: str
    action: str
    changed: bool
    review_decision_id: str = ""
    status: str
    finding_id: str = ""
    successor_candidate_id: str = ""


def _comparison_key(candidate: SemanticCandidate) -> tuple[str, SemanticPath]:
    """The baseline key a confirm/recheck/resolve is made against, on the *target* document.

    A creation-shaped fact conflicts when the thing it claims already exists by the time it
    would land: a tag fact keys on the equipment tag's existence; a class/role fact has no
    tag to collide on, so it keys on its evidence region — honest in that it can only report
    "nothing derived from this region exists yet".
    """

    facts = candidate.proposed_semantics
    if candidate.candidate_type == "equipment_tag":
        return f"equipment:{facts.equipment_tag.strip().casefold()}", "existence"
    return f"region:{candidate.region.region_id}", "existence"


class M6ReviewService:
    """The review queue: bounded reads plus the five decision actions."""

    def __init__(
        self,
        store: SQLiteDocumentStore,
        registry: SymbolRegistry,
        *,
        deployment_mode: str = "local",
    ) -> None:
        self._store = store
        self._registry = registry
        self._candidates = M6CandidateService(store)
        self._adapter = M6SourceAdapter(store, registry)
        self._deployment_mode = deployment_mode

    # -- reads (no writes, no audit) -------------------------------------------------

    def list_candidates(
        self,
        source_document_id: str,
        *,
        status: str | None = None,
        offset: int = 0,
        limit: int = MAX_PAGE_SIZE,
    ) -> dict[str, Any]:
        if status is not None and status not in CANDIDATE_STATES:
            raise ReviewSurfaceError(
                f"unknown review status filter {status!r}",
                code="unknown_review_status",
            )
        candidates = self._store.list_semantic_candidates(source_document_id=source_document_id)
        source = self._source_status(candidates)
        rows = [self._row(candidate) for candidate in candidates]
        if status is not None:
            rows = [row for row in rows if row["review_status"] == status]
        total = len(rows)
        page = rows[max(0, offset) : max(0, offset) + max(1, min(limit, MAX_PAGE_SIZE))]
        return {
            "document_id": source_document_id,
            "source": source,
            "total": total,
            "offset": max(0, offset),
            "limit": max(1, min(limit, MAX_PAGE_SIZE)),
            "candidates": page,
        }

    def candidate_detail(self, source_document_id: str, candidate_id: str) -> dict[str, Any]:
        candidate = self._store.get_semantic_candidate(candidate_id)
        if candidate is None or candidate.artifact.source_document_id != source_document_id:
            raise ReviewSurfaceError(
                f"unknown candidate {candidate_id!r} for source {source_document_id!r}",
                code="candidate_not_found",
            )
        elements = self._evidence_elements(candidate)
        return {
            "candidate": candidate.model_dump(mode="json", by_alias=True),
            "review_status": self._candidates.current_status(candidate_id),
            "decisions": [
                decision.model_dump(mode="json", by_alias=True)
                for decision in self._candidates.decisions(candidate_id)
            ],
            "source": self._source_status([candidate]),
            "evidence_elements": elements,
            "target": self._target_status(candidate),
        }

    def _row(self, candidate: SemanticCandidate) -> dict[str, Any]:
        facts = candidate.proposed_semantics.model_dump(mode="json")
        return {
            "candidate_id": candidate.candidate_id,
            "candidate_type": candidate.candidate_type,
            "review_status": self._candidates.current_status(candidate.candidate_id),
            "proposed_semantics": {
                key: value for key, value in facts.items() if value not in ("", None)
            },
            "region_id": candidate.region.region_id,
            "target_document_id": candidate.target_document_id,
            "producer": candidate.producer.model_dump(mode="json"),
            "confidence": candidate.confidence.value,
            "evidence_count": len(candidate.evidence),
            "created_at": candidate.created_at.isoformat(),
        }

    def _source_status(self, candidates: list[SemanticCandidate]) -> dict[str, Any]:
        """Read-only re-verification of the pinned source: reads never refuse, they report."""

        if not candidates:
            return {"available": False, "code": "source_snapshot_unavailable", "revision": None}
        artifact = candidates[0].artifact
        try:
            document = self._adapter.verify_source(artifact)
        except SourceVerificationError as exc:
            return {"available": False, "code": exc.code, "revision": None}
        return {"available": True, "code": "", "revision": document.revision}

    def _target_status(self, candidate: SemanticCandidate) -> dict[str, Any]:
        target_id = candidate.target_document_id
        if not target_id:
            return {"target_document_id": "", "exists": False, "revision": None}
        stored = self._store.get(target_id)
        return {
            "target_document_id": target_id,
            "exists": stored is not None,
            "revision": stored.document.revision if stored else None,
        }

    def _evidence_elements(self, candidate: SemanticCandidate) -> list[dict[str, Any]]:
        """The referenced source elements, resolved for the evidence view."""

        stored = self._store.get(candidate.artifact.source_document_id)
        if stored is None:
            return []
        by_id = {element.id: element for element in stored.document.elements}
        return [
            by_id[ref].model_dump(mode="json")
            for ref in candidate.region.element_refs
            if ref in by_id
        ]

    # -- decisions (governance writes, audited) ------------------------------------------

    def decide(
        self,
        candidate_id: str,
        *,
        action: ReviewAction,
        reviewer_identity: str,
        reviewer_action: str = "",
        note: str = "",
        conflict_resolution: str = "",
        resolution_choice: ConflictResolutionChoice | None = None,
        element_refs: list[str] | None = None,
        equipment_tag: str = "",
    ) -> DecisionOutcome:
        candidate = self._store.get_semantic_candidate(candidate_id)
        if candidate is None:
            raise ReviewSurfaceError(
                f"unknown candidate {candidate_id!r}", code="candidate_not_found"
            )
        if not reviewer_identity.strip():
            raise ReviewSurfaceError(
                "a human decision must name the reviewer (a declared identity, recorded "
                "verbatim — never derived from a token)",
                code="reviewer_identity_required",
            )
        reviewer_identity = reviewer_identity.strip()
        # The pinned source is re-verified before every decision: a review written against
        # evidence that moved is evidence of nothing (Gate D88-1 carried into review).
        document = self._adapter.verify_source(candidate.artifact)

        try:
            if action == "confirm":
                return self._confirm(
                    candidate,
                    reviewer_identity=reviewer_identity,
                    reviewer_action=reviewer_action,
                    note=note,
                )
            if action == "reject":
                return self._reject(
                    candidate,
                    reviewer_identity=reviewer_identity,
                    reviewer_action=reviewer_action,
                    note=note,
                )
            if action == "recheck":
                return self._recheck(candidate, reviewer_identity=reviewer_identity)
            if action == "resolve_conflict":
                return self._resolve(
                    candidate,
                    reviewer_identity=reviewer_identity,
                    reviewer_action=reviewer_action,
                    conflict_resolution=conflict_resolution,
                    resolution_choice=resolution_choice,
                    note=note,
                )
            if action == "reassign":
                return self._reassign(
                    candidate,
                    document,
                    reviewer_identity=reviewer_identity,
                    reviewer_action=reviewer_action,
                    note=note,
                    element_refs=element_refs or [],
                    equipment_tag=equipment_tag,
                )
        except M6FilingError as exc:
            # The store's in-transaction refusal (final source verification or the
            # decision-log CAS), mapped once — the API layer sees only typed errors.
            raise self._map_store_error(exc) from exc
        raise ReviewSurfaceError(f"unknown review action {action!r}", code="unknown_review_action")

    @staticmethod
    def _map_store_error(exc: M6FilingError) -> M6CoreError:
        """One mapping for the store's in-transaction refusals (D89-1, unified).

        A drift refusal inside the write transaction is the same refusal as the entry-time
        verification — the same family, the same codes, so a caller never has to ask which
        layer caught it. ``decision_state_moved`` is the concurrency CAS: the queue moved
        between the caller's read and the write, which is a conflict, not a crash.
        """

        if exc.code.startswith("source_"):
            return SourceVerificationError(str(exc), code=exc.code)
        if exc.code == "candidate_identity_conflict":
            return CandidateIdentityConflict(str(exc))
        if exc.code in {"decision_state_moved", "decision_log_out_of_order", "candidate_not_found"}:
            return ReviewSurfaceError(str(exc), code=exc.code)
        return exc

    def _confirm(
        self,
        candidate: SemanticCandidate,
        *,
        reviewer_identity: str,
        reviewer_action: str,
        note: str,
    ) -> DecisionOutcome:
        self._require_reviewer_action(reviewer_action)
        baseline = self._baseline_for(candidate)
        confirmation = self._candidates.confirm(
            candidate.candidate_id,
            reviewer_identity=reviewer_identity,
            reviewer_action=reviewer_action,
            baseline=baseline,
            note=note,
            audit=self._audit(
                candidate,
                action="confirm",
                reviewer_identity=reviewer_identity,
                note=note,
            ),
        )
        return DecisionOutcome(
            candidate_id=candidate.candidate_id,
            action="confirm",
            changed=True,
            review_decision_id=confirmation.decision.review_decision_id,
            status="confirmed",
            finding_id=confirmation.finding.finding_id,
        )

    def _reject(
        self,
        candidate: SemanticCandidate,
        *,
        reviewer_identity: str,
        reviewer_action: str,
        note: str,
    ) -> DecisionOutcome:
        self._require_reviewer_action(reviewer_action)
        decision = self._candidates.reject(
            candidate.candidate_id,
            reviewer_identity=reviewer_identity,
            reviewer_action=reviewer_action,
            note=note,
            audit=self._audit(
                candidate, action="reject", reviewer_identity=reviewer_identity, note=note
            ),
        )
        return DecisionOutcome(
            candidate_id=candidate.candidate_id,
            action="reject",
            changed=True,
            review_decision_id=decision.review_decision_id,
            status="rejected",
        )

    def _recheck(self, candidate: SemanticCandidate, *, reviewer_identity: str) -> DecisionOutcome:
        """Re-read the authoritative baseline; a moved baseline falls into conflict.

        The row this may write is a machine event (``conflict_detected``), so it carries no
        reviewer action — the human who asked for the recheck is on the audit fact instead.
        """

        decision = self._candidates.recheck_baseline(
            candidate.candidate_id,
            current=self._baseline_for(candidate),
            audit=self._audit(
                candidate, action="recheck", reviewer_identity=reviewer_identity, note=""
            ),
        )
        if decision is None:
            return DecisionOutcome(
                candidate_id=candidate.candidate_id,
                action="recheck",
                changed=False,
                status=self._candidates.current_status(candidate.candidate_id),
            )
        return DecisionOutcome(
            candidate_id=candidate.candidate_id,
            action="recheck",
            changed=True,
            review_decision_id=decision.review_decision_id,
            status="conflicted",
        )

    def _resolve(
        self,
        candidate: SemanticCandidate,
        *,
        reviewer_identity: str,
        reviewer_action: str,
        conflict_resolution: str,
        resolution_choice: ConflictResolutionChoice | None,
        note: str,
    ) -> DecisionOutcome:
        self._require_reviewer_action(reviewer_action)
        if not conflict_resolution.strip():
            raise ReviewSurfaceError(
                "resolving a conflict must state what the resolution was",
                code="conflict_resolution_required",
            )
        if resolution_choice is None:
            raise ReviewSurfaceError(
                "resolving a conflict must choose keep_existing or accept_proposed",
                code="resolution_choice_required",
            )
        decision = self._candidates.resolve_conflict(
            candidate.candidate_id,
            reviewer_identity=reviewer_identity,
            reviewer_action=reviewer_action,
            conflict_resolution=conflict_resolution,
            resolution_choice=resolution_choice,
            baseline=self._baseline_for(candidate),
            note=note,
            audit=self._audit(
                candidate,
                action="resolve_conflict",
                reviewer_identity=reviewer_identity,
                note=note,
            ),
        )
        return DecisionOutcome(
            candidate_id=candidate.candidate_id,
            action="resolve_conflict",
            changed=True,
            review_decision_id=decision.review_decision_id,
            status="needs_review",
        )

    def _reassign(
        self,
        candidate: SemanticCandidate,
        document: Document,
        *,
        reviewer_identity: str,
        reviewer_action: str,
        note: str,
        element_refs: list[str],
        equipment_tag: str,
    ) -> DecisionOutcome:
        """A corrected binding: file the corrected candidate, supersede the old one.

        Both rows plus the audit commit in one store transaction; the old candidate's
        evidence chain stays readable (append-only), and retrying the same reassignment
        is refused because the old candidate no longer has a supersede edge.
        """

        self._require_reviewer_action(reviewer_action)
        refs = sorted({ref.strip() for ref in element_refs if ref.strip()})
        if not refs:
            raise ReviewSurfaceError(
                "a reassignment must name the corrected evidence element set",
                code="reassign_requires_element_refs",
            )
        if candidate.candidate_type == "annotation_role":
            raise ReviewSurfaceError(
                "annotation_role candidates carry no equipment binding to reassign",
                code="reassign_unsupported_type",
            )
        tag = candidate.proposed_semantics.equipment_tag or equipment_tag.strip()
        if not tag:
            raise ReviewSurfaceError(
                "the old candidate carries no tag; the reassignment must state one",
                code="reassign_requires_equipment_tag",
            )

        known = {element.id: element for element in document.elements}
        missing = [ref for ref in refs if ref not in known]
        if missing:
            raise ReviewSurfaceError(
                f"the corrected element refs are not in the source document: {missing}",
                code="reassign_unknown_element_refs",
            )
        artifact = candidate.artifact
        boxes = [element_bbox(known[ref]) for ref in refs]
        region = build_source_region(
            artifact=artifact,
            geometry_selector=bbox_geometry(union_bbox(boxes)) if boxes else None,
            element_refs=refs,
            text_spans=list(candidate.region.text_spans),
            layer=candidate.region.layer,
        )
        facts = ProposedSemantics(equipment_tag=tag)
        new_id = "cand_m6_" + canonical_digest(
            {
                "rules": M6_SOURCE_RULES,
                "reassigned_from": candidate.candidate_id,
                "artifact": artifact.artifact_id,
                "source_revision": artifact.source_revision,
                "region": region.region_id,
                "target_document_id": candidate.target_document_id,
                "candidate_type": "equipment_tag",
                "facts": facts.model_dump(mode="json"),
                "producer": "deterministic_rule_engine",
                "producer_version": M6_SOURCE_ADAPTER_VERSION,
            }
        )
        if new_id == candidate.candidate_id:
            raise ReviewSurfaceError(
                "the corrected binding is identical to the existing one",
                code="reassign_no_change",
            )

        # The extraction facts are rule-derived; the *binding* is the human's, and the
        # evidence says so — the supersede decision and the audit record carry the identity.
        corrected = SemanticCandidate(
            candidate_id=new_id,
            artifact=artifact,
            region=region,
            target_document_id=candidate.target_document_id,
            candidate_type="equipment_tag",
            proposed_semantics=facts,
            confidence=Confidence(
                value=1.0, calibration_class="not_measured", source="deterministic_rule"
            ),
            evidence=[
                *candidate.evidence,
                CandidateEvidence(
                    kind="rule",
                    detail=(
                        "the binding was corrected through human review; the reassignment "
                        "decision and its audit fact carry the reviewer identity"
                    ),
                    region_id=region.region_id,
                ),
            ],
            producer=ProducerRef(key="deterministic_rule_engine", version=M6_SOURCE_ADAPTER_VERSION),
            provenance=ProvenanceRef(provider="agentcad", procedure_version=M6_SOURCE_RULES),
            derived_from=[candidate.candidate_id],
        )
        supersede = self._build_supersede_decision(
            candidate.candidate_id,
            successor_id=new_id,
            note=note,
        )
        self._store.file_superseding_candidate(
            corrected,
            filing_decision=filing_decision(corrected),
            supersede_decision=supersede,
            audit=self._audit(
                candidate,
                action="reassign",
                reviewer_identity=reviewer_identity,
                note=note,
                extra={"successor_candidate_id": new_id},
            ),
            source_document_id=artifact.source_document_id,
            expected_source_revision=artifact.source_revision,
            expected_source_content_hash=artifact.content_hash,
        )
        return DecisionOutcome(
            candidate_id=candidate.candidate_id,
            action="reassign",
            changed=True,
            review_decision_id=supersede.review_decision_id,
            status="superseded",
            successor_candidate_id=new_id,
        )

    # -- shared plumbing -------------------------------------------------------------

    def _baseline_for(self, candidate: SemanticCandidate) -> ConflictBaselineRecord:
        """The baseline this decision reviews against: the *target* document's current state.

        A target that does not exist yet records the same digest an empty target would, so
        creating the document later does not itself read as a conflict — only the fact the
        candidate claims can drift.
        """

        identity, path = _comparison_key(candidate)
        stored = self._store.get(candidate.target_document_id) if candidate.target_document_id else None
        if stored is None:
            value = "absent" if path == "existence" else ""
            return ConflictBaselineRecord(
                baseline_revision=0,
                comparison_identity=identity,
                comparison_path=path,
                digest_version="semantic-value-v1",
                value_digest=value_digest(path, value),
                value_present=bool(value),
            )
        graph = build_baseline_graph(stored.document, self._registry)
        return baseline_record(stored.document, graph, identity=identity, path=path)

    def _build_supersede_decision(
        self, candidate_id: str, *, successor_id: str, note: str
    ) -> ReviewDecision:
        """The supersede row, validated against the contract's declared edges.

        Built with public pieces (the replayed status, the contract transition table and the
        shared decision-id derivation); the row then goes through the same replay rules as
        any decision, which the surface tests prove by reading the status back.
        """

        from_status = self._candidates.current_status(candidate_id)
        if not any(
            edge.from_state == from_status and edge.to_state == "superseded"
            for edge in CANDIDATE_TRANSITIONS
        ):
            raise ReviewSurfaceError(
                f"a {from_status} candidate has no supersede edge",
                code="supersede_not_applicable",
            )
        return ReviewDecision(
            review_decision_id=review_decision_id(
                candidate_id=candidate_id,
                kind="superseded",
                from_status=from_status,
                to_status="superseded",
                reviewer_identity="",
                reviewer_action="",
                note=note,
                baseline=None,
                conflict_resolution="",
                resolution_choice=None,
                successor_candidate_id=successor_id,
            ),
            candidate_id=candidate_id,
            kind="superseded",
            from_status=from_status,  # type: ignore[arg-type]
            to_status="superseded",
            successor_candidate_id=successor_id,
            note=note,
        )

    def _require_reviewer_action(self, reviewer_action: str) -> None:
        if not reviewer_action.strip():
            raise ReviewSurfaceError(
                "a human decision must record what the reviewer did, not only the new status",
                code="reviewer_action_required",
            )

    def _audit(
        self,
        candidate: SemanticCandidate,
        *,
        action: str,
        reviewer_identity: str,
        note: str,
        extra: dict[str, Any] | None = None,
    ) -> AuditRecordDraft:
        return AuditRecordDraft(
            event_type="m6.review.decision",
            actor=reviewer_identity,
            surface="rest",
            tool_name="m6_review",
            status="applied",
            document_id=candidate.artifact.source_document_id,
            label=f"M6 review {action}: {candidate.candidate_id}",
            evidence={
                "candidate_id": candidate.candidate_id,
                "action": action,
                "authentication_evidence": (
                    "service-token" if self._deployment_mode == "shared" else "local-process"
                ),
                "reviewer_attribution": reviewer_identity,
                "identity_assurance": "declared",
                "note": note,
                **(extra or {}),
            },
        )

__all__ = [
    "MAX_PAGE_SIZE",
    "DecisionOutcome",
    "M6ReviewService",
    "ReviewAction",
    "ReviewSurfaceError",
]
