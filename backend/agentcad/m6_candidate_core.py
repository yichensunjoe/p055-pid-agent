"""M6 Phase-2A: the governed candidate core.

This is the smallest loop in which the governance chain is *true* rather than described:

    artifact -> region -> SemanticCandidate -> ReviewDecision -> ConfirmedSemanticFinding
             -> deterministic compile -> StructuredEngineeringPatch

Three things are enforced here that the contract only declares, and each of them is a
refusal rather than a convention:

* **A status is never set, it is derived.** ``current_status`` replays the append-only
  decision log and refuses any transition the contract does not declare. Writing
  ``review_status = "confirmed"`` into a row grants nothing, because nothing reads it: the
  only thing that can produce a confirmed finding is a recorded human decision.
* **The apply edge is unreachable, not merely forbidden.** Recording a transition into
  ``applied`` raises :class:`GovernedWriteNotAuthorized` in this phase, so a patch cannot be
  applied even by a caller who constructs one by hand.
* **Compiling is idempotent in the strong sense.** ``patch_id`` is derived from the
  canonical digest, so the same finding compiled twice produces the same identity and the
  same content — there is no "almost equal" patch to argue about later.

Determinism deserves the emphasis because it is easy to fake. Every generated id in the
compiler is derived from the founding fact (never from a uuid or a clock), and the digest is
taken over a canonical projection that excludes timestamps and transaction ids. The lesson
from A5 and from the gate's replay ruling is the same one: a digest is only as stable as the
list of fields you remembered to leave out.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from .agent_semantic_models import SemanticOperation, UpdateElementOperation
from .engineering_ir import EngineeringGraph, build_engineering_graph
from .m6_candidate_models import (
    HUMAN_DECISION_KINDS,
    CandidateConflict,
    ConfirmedSemanticFinding,
    ConflictBaselineRecord,
    ConflictResolutionChoice,
    DecisionKind,
    ProposedSemantics,
    ReviewDecision,
    SemanticCandidate,
    SemanticPath,
    StructuredEngineeringPatch,
)
from .m6_ingestion_contract import (
    AUTO_ACCEPT_WHITELIST,
    CANDIDATE_STATES,
    CANDIDATE_TRANSITIONS,
    FORBIDDEN_TRANSITIONS,
    REPLAY_VOLATILE_FIELDS_EXCLUDED,
    WRITE_POLICY_V1,
)
from .models import (
    AddElementOperation,
    Document,
    Point,
    SymbolElement,
)
from .symbols import SymbolRegistry

M6_PATCH_COMPILER = "m6.patch_compiler"
M6_PATCH_COMPILER_VERSION = "0.1.0"
M6_PATCH_COMPILER_RULES = "m6-rules/1"

#: Intent vocabulary the compiler can express in Phase-2A. Everything else is refused with a
#: reason rather than guessed at; the remaining intents belong to the apply phase, where the
#: dry-run and conflict checks that make them reviewable exist.
PHASE_2A_SUPPORTED_INTENTS: tuple[str, ...] = ("creation", "metadata_enrichment")


class M6CoreError(RuntimeError):
    """A refusal. Every subclass carries a stable machine code, because the reason is data."""

    code = "m6_core_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class CandidateSchemaViolation(M6CoreError):
    code = "candidate_schema_violation"


class CandidateNotFound(M6CoreError):
    code = "candidate_not_found"


class IllegalTransition(M6CoreError):
    code = "illegal_transition"


class TransitionEvidenceMissing(M6CoreError):
    code = "transition_evidence_missing"


class NotConfirmedError(M6CoreError):
    code = "not_confirmed"


class CompilationRefused(M6CoreError):
    code = "compilation_refused"


class GovernedWriteNotAuthorized(M6CoreError):
    code = "governed_write_not_authorized"


# --------------------------------------------------------------------------------------
# Digests: what goes in, and (more importantly) what stays out
# --------------------------------------------------------------------------------------


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_digest(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _strip_volatile(value: Any) -> Any:
    """Drop provenance that is allowed to differ between two correct runs.

    Rebuilt rather than mutated so the caller's objects are never touched. The key set comes
    from the contract, which is the same list the replay contract excludes.
    """

    volatile = {
        *REPLAY_VOLATILE_FIELDS_EXCLUDED,
        "created_at",
        "decided_at",
        "confirmed_at",
        "imported_at",
    }
    if isinstance(value, dict):
        return {
            key: _strip_volatile(item)
            for key, item in value.items()
            if key not in volatile and key != "patch_id"
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


#: The baseline digest scheme. Named and versioned because Phase-2B's optimistic concurrency
#: will trust this number: an unversioned summary would be an identity nobody can re-derive
#: after the scheme changes.
SEMANTIC_VALUE_DIGEST_VERSION = "semantic-value-v1"


#: How each semantic path's value is canonicalized before hashing. Path-aware on purpose:
#: an equipment tag is an identifier this codebase already treats case-insensitively
#: (``resolve_object`` folds case when addressing an object by tag), but a semantic value is not
#: inherently case-insensitive — a future description, vendor string or free-text attribute may
#: carry meaning in its case. Applying tag normalization everywhere would let a real change read
#: as "unchanged", which is precisely the failure optimistic concurrency must not have.
#:
#: ``identifier`` = trim, collapse internal whitespace, casefold.
#: ``exact`` = no transformation at all. If a value means exactly what it says, the digest must
#: hash exactly what it says.
SEMANTIC_VALUE_CANONICALIZERS: dict[str, str] = {
    "equipment_tag": "identifier",
    "equipment_class": "identifier",
    "annotation_role": "exact",
    "existence": "exact",
}


def canonicalize_semantic_value(path: str, value: str) -> str:
    """Canonicalize a semantic value for hashing, by path.

    An unregistered path is a hard failure rather than a default: silently falling back to tag
    normalization is how a path that *does* care about case gets hashed as if it did not. Adding
    a path therefore means choosing a canonicalizer, which is the decision worth making visible.
    """

    rule = SEMANTIC_VALUE_CANONICALIZERS.get(path)
    if rule is None:
        raise IllegalTransition(
            f"no canonicalizer is declared for semantic path {path!r}; add one to "
            "SEMANTIC_VALUE_CANONICALIZERS instead of guessing how its value should be hashed",
            code="unregistered_semantic_path",
        )
    if rule == "identifier":
        return " ".join(value.split()).casefold()
    return value


def value_digest(path: str, value: str) -> str:
    """A digest of one semantic value. ``""`` is a meaningful value (unstated), not a missing one."""

    return canonical_digest(
        {
            "digest": SEMANTIC_VALUE_DIGEST_VERSION,
            "path": path,
            "canonicalizer": SEMANTIC_VALUE_CANONICALIZERS[path],
            "value": canonicalize_semantic_value(path, value),
        }
    )


# --------------------------------------------------------------------------------------
# Baseline: the authoritative state a judgement is compared against
# --------------------------------------------------------------------------------------


def build_baseline_graph(document: Document, registry: SymbolRegistry) -> EngineeringGraph:
    return build_engineering_graph(document, registry)


def resolve_identity(graph: EngineeringGraph, identity: str):
    """Resolve a fact's target identity to a committed engineering object.

    ``engineering_id``, ``tag_key``, tag and bare element id all work; ``element:<id>`` is
    accepted because that is how a fact that came from a drawing region names its anchor, and
    the graph's own resolver does not know that spelling.
    """

    needle = identity.strip()
    if not needle:
        return None
    if needle.startswith("element:"):
        return graph.object(needle.split(":", 1)[1])
    return graph.object(needle) or graph.object(needle.split(":", 1)[1])


def semantic_value(
    document: Document, graph: EngineeringGraph, identity: str, path: SemanticPath
) -> str:
    """The committed value at ``identity`` / ``path``. Read-only, derived, deterministic."""

    record = resolve_identity(graph, identity)
    if record is None:
        if path == "existence":
            return "absent"
        return ""
    if path == "equipment_tag":
        return record.tag
    if path == "equipment_class":
        return record.symbol_key
    if path == "existence":
        return "present"
    element = _find_element(document, record.primary_element_id)
    if element is None:
        return ""
    return str(element.properties.get(path, "") or "")


def _find_element(document: Document, element_id: str) -> Any:
    for element in document.elements:
        if element.id == element_id:
            return element
    return None


def baseline_record(
    document: Document,
    graph: EngineeringGraph,
    *,
    identity: str,
    path: SemanticPath,
) -> ConflictBaselineRecord:
    value = semantic_value(document, graph, identity, path)
    return ConflictBaselineRecord(
        baseline_revision=document.revision,
        comparison_identity=identity or "unstated",
        comparison_path=path,
        digest_version=SEMANTIC_VALUE_DIGEST_VERSION,
        value_digest=value_digest(path, value),
        value_present=bool(value),
    )


# --------------------------------------------------------------------------------------
# The repository the service needs. Implemented by SQLiteDocumentStore.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Confirmation:
    """The two durable artifacts of one human decision, written together or not at all."""

    decision: ReviewDecision
    finding: ConfirmedSemanticFinding


class M6CandidateRepository(Protocol):
    def insert_semantic_candidate(self, candidate: SemanticCandidate) -> None: ...

    def record_confirmation(
        self, decision: ReviewDecision, finding: ConfirmedSemanticFinding
    ) -> None:
        """Commit the decision and its finding in one transaction."""
        ...

    def get_semantic_candidate(self, candidate_id: str) -> SemanticCandidate | None: ...

    def list_semantic_candidates(
        self, *, source_document_id: str | None = None
    ) -> list[SemanticCandidate]: ...

    def insert_review_decision(self, decision: ReviewDecision) -> None: ...

    def list_review_decisions(self, candidate_id: str) -> list[ReviewDecision]: ...

    def insert_confirmed_finding(self, finding: ConfirmedSemanticFinding) -> None: ...

    def get_confirmed_finding(self, finding_id: str) -> ConfirmedSemanticFinding | None: ...

    def list_confirmed_findings(
        self, *, source_document_id: str | None = None
    ) -> list[ConfirmedSemanticFinding]: ...


# --------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------

#: kind -> the status it moves a candidate to. The transition itself is then looked up in the
#: contract, so the vocabulary cannot drift from the declared state machine.
_KIND_TARGET_STATUS: dict[str, str] = {
    "filed": "needs_review",
    "human_confirm": "confirmed",
    "human_reject": "rejected",
    "conflict_detected": "conflicted",
    "human_resolves_conflict": "needs_review",
    "superseded": "superseded",
}

if "applied" in _KIND_TARGET_STATUS.values():  # pragma: no cover - a structural guard
    raise RuntimeError(
        "no review decision may move a candidate to 'applied': that transition belongs to the "
        "apply-v2 gate, which Phase-2A does not have"
    )


def _declared_edge(from_status: str, to_status: str):
    for edge in CANDIDATE_TRANSITIONS:
        if edge.from_state == from_status and edge.to_state == to_status:
            return edge
    return None


class M6CandidateService:
    """The domain surface for Phase-2A. No HTTP route, no MCP tool, no document write."""

    def __init__(self, repository: M6CandidateRepository) -> None:
        self._repository = repository

    # -- filing ------------------------------------------------------------------------

    def file_candidate(self, candidate: SemanticCandidate) -> SemanticCandidate:
        if AUTO_ACCEPT_WHITELIST:
            raise CandidateSchemaViolation(
                "M6 v1 signs no auto-accept class, so filing must not be able to skip review",
                code="auto_accept_whitelist_not_empty",
            )
        if candidate.review_status != "proposed":
            raise CandidateSchemaViolation(
                "a filed candidate is born 'proposed'; its status is then derived from decisions",
                code="candidate_must_be_filed_as_proposed",
            )
        self._repository.insert_semantic_candidate(candidate)
        self.record_decision(candidate.candidate_id, "filed")
        return candidate

    # -- status ------------------------------------------------------------------------

    def current_status(self, candidate_id: str) -> str:
        candidate = self._repository.get_semantic_candidate(candidate_id)
        if candidate is None:
            raise CandidateNotFound(f"unknown candidate {candidate_id!r}")
        status = candidate.review_status
        for decision in self._repository.list_review_decisions(candidate_id):
            if decision.from_status != status:
                raise IllegalTransition(
                    f"decision {decision.review_decision_id} claims to leave {decision.from_status!r} "
                    f"but the candidate is {status!r}",
                    code="decision_log_out_of_order",
                )
            if _declared_edge(status, decision.to_status) is None:
                raise IllegalTransition(
                    f"{status} -> {decision.to_status} is not a declared transition",
                    code="undeclared_transition_in_log",
                )
            status = decision.to_status
        return status

    def decisions(self, candidate_id: str) -> list[ReviewDecision]:
        return self._repository.list_review_decisions(candidate_id)

    # -- the review queue --------------------------------------------------------------

    def record_decision(
        self,
        candidate_id: str,
        kind: DecisionKind,
        *,
        reviewer_identity: str = "",
        reviewer_action: str = "",
        note: str = "",
        baseline: ConflictBaselineRecord | None = None,
        conflict: CandidateConflict | None = None,
        successor_candidate_id: str = "",
        conflict_resolution: str = "",
        resolution_choice: ConflictResolutionChoice | None = None,
    ) -> ReviewDecision:
        decision = self._build_decision(
            candidate_id,
            kind,
            reviewer_identity=reviewer_identity,
            reviewer_action=reviewer_action,
            note=note,
            baseline=baseline,
            conflict=conflict,
            successor_candidate_id=successor_candidate_id,
            conflict_resolution=conflict_resolution,
            resolution_choice=resolution_choice,
        )
        self._repository.insert_review_decision(decision)
        return decision

    def _build_decision(
        self,
        candidate_id: str,
        kind: DecisionKind,
        *,
        reviewer_identity: str = "",
        reviewer_action: str = "",
        note: str = "",
        baseline: ConflictBaselineRecord | None = None,
        conflict: CandidateConflict | None = None,
        successor_candidate_id: str = "",
        conflict_resolution: str = "",
        resolution_choice: ConflictResolutionChoice | None = None,
    ) -> ReviewDecision:
        """Validate a transition and return the row, without writing it.

        Separated from the write so a confirmation can build its decision *and* its finding and
        then commit both through one repository call: the two durable artifacts of a single
        human decision must not be able to land half-formed.
        """

        if kind not in _KIND_TARGET_STATUS:
            raise IllegalTransition(f"unknown decision kind {kind!r}", code="unknown_decision_kind")
        to_status = _KIND_TARGET_STATUS[kind]
        from_status = self.current_status(candidate_id)
        if to_status == "applied":
            raise GovernedWriteNotAuthorized(
                "Phase-2A has no apply path: a candidate cannot be recorded as applied",
                code="applied_requires_apply_v2",
            )
        if (from_status, to_status) in FORBIDDEN_TRANSITIONS:
            raise IllegalTransition(
                f"{from_status} -> {to_status} is forbidden by the M6 contract",
                code="forbidden_transition",
            )
        edge = _declared_edge(from_status, to_status)
        if edge is None:
            raise IllegalTransition(
                f"{from_status} -> {to_status} is not one of the declared transitions "
                f"(kind {kind!r})",
                code="undeclared_transition",
            )
        decision = ReviewDecision(
            review_decision_id=review_decision_id(
                candidate_id=candidate_id,
                kind=kind,
                from_status=from_status,
                to_status=to_status,
                reviewer_identity=reviewer_identity,
                reviewer_action=reviewer_action,
                note=note,
                baseline=baseline,
                conflict_resolution=conflict_resolution,
                resolution_choice=resolution_choice,
                successor_candidate_id=successor_candidate_id,
            ),
            candidate_id=candidate_id,
            kind=kind,
            from_status=from_status,  # type: ignore[arg-type]
            to_status=to_status,  # type: ignore[arg-type]
            reviewer_action=reviewer_action,
            reviewer_identity=reviewer_identity,
            note=note,
            baseline=baseline,
            conflict=conflict,
            successor_candidate_id=successor_candidate_id,
            conflict_resolution=conflict_resolution,
            resolution_choice=resolution_choice,
        )
        missing = self._missing_evidence(edge.requires, decision)
        if missing:
            raise TransitionEvidenceMissing(
                f"{from_status} -> {to_status} is missing required evidence: {', '.join(missing)}",
                code="transition_evidence_missing",
            )
        if to_status == "confirmed" and decision.kind not in HUMAN_DECISION_KINDS:
            raise IllegalTransition(
                "only a recorded human decision can produce a confirmed candidate",
                code="confirmed_requires_human_decision",
            )
        return decision

    def _missing_evidence(self, required: tuple[str, ...], decision: ReviewDecision) -> list[str]:
        provided = {
            "review_decision",  # this row
            "new_review_decision",  # a decision is always new: an old one can never be reused
        }
        if decision.reviewer_action:
            provided.add("reviewer_action")
        if decision.baseline is not None:
            provided.add("baseline_recheck")
        if decision.conflict is not None or decision.baseline is not None:
            provided.add("conflict_record")
        if decision.conflict_resolution:
            provided.add("conflict_resolution")
        if decision.successor_candidate_id:
            provided.add("successor_candidate_id")
        # Phase-2A can never satisfy these: they belong to the apply edge.
        return [name for name in required if name not in provided]

    def confirm(
        self,
        candidate_id: str,
        *,
        reviewer_identity: str,
        reviewer_action: str,
        baseline: ConflictBaselineRecord,
        note: str = "",
    ) -> Confirmation:
        """Record a person's confirmation and the finding it creates, atomically.

        These are two rows but one decision. Committing them separately would leave a window in
        which the state machine says ``confirmed`` while no finding can be traced back to a
        person — exactly the crack Phase-2B must not inherit before it gains write permission.
        """

        candidate = self._repository.get_semantic_candidate(candidate_id)
        if candidate is None:
            raise CandidateNotFound(f"unknown candidate {candidate_id!r}")
        decision = self._build_decision(
            candidate_id,
            "human_confirm",
            reviewer_identity=reviewer_identity,
            reviewer_action=reviewer_action,
            baseline=baseline,
            note=note,
        )
        finding = self._build_finding(candidate, decision)
        self._repository.record_confirmation(decision, finding)
        return Confirmation(decision=decision, finding=finding)

    def reject(
        self,
        candidate_id: str,
        *,
        reviewer_identity: str,
        reviewer_action: str,
        note: str = "",
    ) -> ReviewDecision:
        return self.record_decision(
            candidate_id,
            "human_reject",
            reviewer_identity=reviewer_identity,
            reviewer_action=reviewer_action,
            note=note,
        )

    def recheck_baseline(
        self,
        candidate_id: str,
        *,
        current: ConflictBaselineRecord,
    ) -> ReviewDecision | None:
        """Re-read the authoritative value. A baseline that moved forces a conflict.

        This is the optimistic-concurrency rule from the contract: a confirmation was made
        against the revision the reviewer saw, and it stops being a permission the moment the
        engineering model moves underneath it. Returning ``None`` means "still valid".
        """

        status = self.current_status(candidate_id)
        if status not in {"needs_review", "confirmed"}:
            raise IllegalTransition(
                f"a baseline recheck only applies to a candidate still heading for a write, not {status!r}",
                code="baseline_recheck_not_applicable",
            )
        reviewed = self._reviewed_baseline(candidate_id)
        if reviewed is None:
            raise IllegalTransition(
                "no review baseline was recorded, so there is nothing to re-check",
                code="no_reviewed_baseline",
            )
        if reviewed.digest_version != current.digest_version:
            raise IllegalTransition(
                f"the reviewed baseline was hashed with {reviewed.digest_version!r} and the current "
                f"value with {current.digest_version!r}: the two digests are not comparable, and "
                "treating them as equal would read a scheme change as 'unchanged'",
                code="baseline_digest_version_mismatch",
            )
        if reviewed.value_digest == current.value_digest:
            return None
        conflict = CandidateConflict(
            conflict_id=_conflict_id(candidate_id, reviewed, current),
            baseline=reviewed,
            proposed_value_digest=current.value_digest,
        )
        return self.record_decision(
            candidate_id,
            "conflict_detected",
            baseline=reviewed,
            conflict=conflict,
            note=(
                f"authoritative baseline moved from revision {reviewed.baseline_revision} to "
                f"{current.baseline_revision}; the confirmation no longer applies"
            ),
        )

    def resolve_conflict(
        self,
        candidate_id: str,
        *,
        reviewer_identity: str,
        reviewer_action: str,
        conflict_resolution: str,
        resolution_choice: ConflictResolutionChoice,
        baseline: ConflictBaselineRecord,
        note: str = "",
    ) -> ReviewDecision:
        """Leaving a conflict costs a *new* decision; the old confirmation cannot be reused."""

        return self.record_decision(
            candidate_id,
            "human_resolves_conflict",
            reviewer_identity=reviewer_identity,
            reviewer_action=reviewer_action,
            conflict_resolution=conflict_resolution,
            resolution_choice=resolution_choice,
            baseline=baseline,
            note=note,
        )

    def supersede(
        self, candidate_id: str, *, successor_candidate_id: str, note: str = ""
    ) -> ReviewDecision:
        return self.record_decision(
            candidate_id,
            "superseded",
            successor_candidate_id=successor_candidate_id,
            note=note,
        )

    def _reviewed_baseline(self, candidate_id: str) -> ConflictBaselineRecord | None:
        for decision in reversed(self._repository.list_review_decisions(candidate_id)):
            if decision.baseline is not None:
                return decision.baseline
        return None

    # -- confirmation ------------------------------------------------------------------

    def latest_finding(self, candidate_id: str) -> ConfirmedSemanticFinding | None:
        """The finding a confirmation produced, read back from the store."""

        confirming = self._latest_confirming_decision(candidate_id)
        if confirming is None:
            return None
        return self._repository.get_confirmed_finding(
            _finding_id(candidate_id, confirming.review_decision_id)
        )

    def _build_finding(
        self, candidate: SemanticCandidate, confirming: ReviewDecision
    ) -> ConfirmedSemanticFinding:
        candidate_id = candidate.candidate_id
        if confirming.baseline is None:
            raise NotConfirmedError(
                "the confirmation did not record the baseline it was made against",
                code="confirmation_without_baseline",
            )
        if candidate.candidate_type == "unresolved":
            raise NotConfirmedError(
                "an unresolved candidate carries no fact, so there is nothing to confirm",
                code="unresolved_has_no_finding",
            )
        finding = ConfirmedSemanticFinding(
            finding_id=_finding_id(candidate_id, confirming.review_decision_id),
            candidate_id=candidate_id,
            review_decision_id=confirming.review_decision_id,
            artifact=candidate.artifact,
            region_id=candidate.region.region_id,
            region_geometry=candidate.region.geometry_selector,
            candidate_type=candidate.candidate_type,
            confirmed_semantics=candidate.proposed_semantics,
            evidence=candidate.evidence,
            baseline=confirming.baseline,
            producer=candidate.producer,
            provenance=candidate.provenance,
            provenance_chain=[
                candidate.artifact.artifact_id,
                candidate.region.region_id,
                candidate_id,
                confirming.review_decision_id,
            ],
        )
        return finding

    def _latest_confirming_decision(self, candidate_id: str) -> ReviewDecision | None:
        for decision in reversed(self._repository.list_review_decisions(candidate_id)):
            if decision.to_status == "confirmed" and decision.kind in HUMAN_DECISION_KINDS:
                return decision
        return None

    # -- compilation -------------------------------------------------------------------

    def compile_finding(
        self,
        finding_id: str,
        *,
        document: Document,
        registry: SymbolRegistry,
    ) -> StructuredEngineeringPatch:
        finding = self._repository.get_confirmed_finding(finding_id)
        if finding is None:
            raise NotConfirmedError(
                f"no confirmed finding {finding_id!r}: there is nothing a compiler is allowed to read",
                code="compilation_requires_confirmed_finding",
            )
        return self._compile(finding, document=document, registry=registry)

    def _compile(
        self,
        finding: ConfirmedSemanticFinding,
        *,
        document: Document,
        registry: SymbolRegistry,
    ) -> StructuredEngineeringPatch:
        graph = build_engineering_graph(document, registry)
        facts = finding.confirmed_semantics

        intent, operations = self._operations_for(
            finding, facts, document=document, graph=graph, registry=registry
        )
        disposition = _disposition_for(intent)
        if disposition != "allowed_after_confirmation":
            raise CompilationRefused(
                f"intent {intent!r} is {disposition!r} in M6 v1; a model's belief that an existing "
                "value is wrong is a conflict for a person to resolve, not a patch to compile",
                code=f"{intent}_requires_human_resolution",
            )

        payload = {
            "compiler": M6_PATCH_COMPILER,
            "compiler_version": M6_PATCH_COMPILER_VERSION,
            "compiler_rules": M6_PATCH_COMPILER_RULES,
            "intent": intent,
            "finding_facts": _facts_payload(facts),
            "operations": [_strip_volatile(op.model_dump(mode="json")) for op in operations],
            "baseline_revision": finding.baseline.baseline_revision,
        }
        digest = canonical_digest(payload)
        return StructuredEngineeringPatch(
            patch_id=f"m6patch_{digest}",
            finding_ids=[finding.finding_id],
            intent=intent,  # type: ignore[arg-type]
            policy_disposition=disposition,
            operations=operations,
            compiler=M6_PATCH_COMPILER,
            compiler_version=M6_PATCH_COMPILER_VERSION,
            compiler_rules=M6_PATCH_COMPILER_RULES,
            baseline_revision=finding.baseline.baseline_revision,
            canonical_digest=digest,
        )

    def _operations_for(
        self,
        finding: ConfirmedSemanticFinding,
        facts: ProposedSemantics,
        *,
        document: Document,
        graph: EngineeringGraph,
        registry: SymbolRegistry,
    ) -> tuple[str, list[SemanticOperation]]:
        if finding.candidate_type == "unresolved":
            raise CompilationRefused(
                "an unresolved candidate carries no fact, so there is nothing to compile",
                code="unresolved_carries_no_fact",
            )
        if finding.candidate_type == "connection_relationship":
            raise CompilationRefused(
                "relationship addition is not compiled in Phase-2A: the dry-run and port checks that "
                "make a new connection reviewable arrive with the apply phase",
                code="relationship_addition_not_compiled_in_phase_2a",
            )

        target = resolve_identity(graph, facts.target_identity) if facts.target_identity else None

        if finding.candidate_type == "symbol_class":
            if target is not None:
                raise CompilationRefused(
                    "changing the class of an object that already exists is a topology change, "
                    "not metadata enrichment",
                    code="symbol_class_change_not_supported",
                )
            return "creation", [self._creation_operation(finding, facts, registry)]

        if not facts.target_identity:
            raise CompilationRefused(
                "metadata enrichment needs a target identity: there is no existing object to annotate",
                code="metadata_enrichment_requires_target",
            )
        if target is None:
            raise CompilationRefused(
                f"target {facts.target_identity!r} is not in the committed engineering model",
                code="target_not_resolved",
            )

        path = _policy_path(finding)
        existing = semantic_value(document, graph, facts.target_identity, path)
        proposed = _proposed_value(facts, finding.candidate_type)
        if existing and existing != proposed:
            raise CompilationRefused(
                f"{path} at {target.engineering_id} already states {existing!r}; overwriting an "
                "authoritative value is a conflict for a human to resolve",
                code="overwrite_requires_human_resolution",
            )
        if existing == proposed:
            raise CompilationRefused(
                f"{path} at {target.engineering_id} already states the same value",
                code="already_stated",
            )

        element_id = target.primary_element_id
        if finding.candidate_type == "equipment_tag":
            return "metadata_enrichment", [
                UpdateElementOperation(element_id=element_id, patch={"label": proposed})
            ]
        return "metadata_enrichment", [
            UpdateElementOperation(element_id=element_id, patch={"properties": {path: proposed}})
        ]

    def _creation_operation(
        self,
        finding: ConfirmedSemanticFinding,
        facts: ProposedSemantics,
        registry: SymbolRegistry,
    ) -> SemanticOperation:
        definition = _symbol_definition(registry, facts.symbol_class)
        if definition is None:
            raise CompilationRefused(
                f"symbol class {facts.symbol_class!r} is not in the symbol catalogue",
                code="out_of_catalogue",
            )
        geometry = finding.region_geometry
        if geometry is None:
            raise CompilationRefused(
                "creating an object needs the region geometry: there is nothing to place it from",
                code="creation_requires_geometry",
            )
        return AddElementOperation(
            element=SymbolElement(
                id=_created_element_id(finding, facts),
                symbol_key=definition.key,
                position=Point(x=geometry.x, y=geometry.y),
                width=definition.width,
                height=definition.height,
                label=facts.equipment_tag,
            )
        )

    # -- the boundary Phase-2A must not cross ------------------------------------------

    def request_apply(self, patch_id: str) -> None:
        """The only place a governed write could start, and in Phase-2A it always refuses."""

        raise GovernedWriteNotAuthorized(
            "Phase-2A has no governed write: the apply-v2 gate, locality check and conflict "
            "re-check arrive in Phase-2B, and a patch compiles without them",
            code="apply_not_authorized_in_phase_2a",
        )

    def current_states(self) -> tuple[str, ...]:
        return CANDIDATE_STATES


def _policy_path(finding: ConfirmedSemanticFinding) -> SemanticPath:
    if finding.candidate_type == "symbol_class":
        return "equipment_class"
    if finding.candidate_type == "annotation_role":
        return "annotation_role"
    return "equipment_tag"


def _proposed_value(facts: ProposedSemantics, candidate_type: str) -> str:
    if candidate_type == "equipment_tag":
        return facts.equipment_tag
    if candidate_type == "annotation_role":
        return facts.annotation_role or ""
    return facts.symbol_class


def _disposition_for(intent: str) -> str:
    for row in WRITE_POLICY_V1:
        if row.intent == intent:
            return row.disposition
    raise CompilationRefused(
        f"intent {intent!r} has no row in the v1 write policy", code="unknown_intent"
    )


def _facts_payload(facts: ProposedSemantics) -> dict[str, Any]:
    return {
        "target_identity": facts.target_identity,
        "symbol_class": facts.symbol_class,
        "equipment_tag": facts.equipment_tag,
        "annotation_role": facts.annotation_role,
        "relationship": [facts.relationship_source, facts.relationship_target],
        "unresolved_reason": facts.unresolved_reason,
    }


def _symbol_definition(registry: SymbolRegistry, key: str):
    try:
        return registry.get(key)
    except Exception:  # the registry raises its own error types for unknown keys
        return None


#: The review-decision identity scheme, versioned for the same reason as the value digest: a
#: reader in Phase-2B must be able to re-derive this id from the decision alone.
REVIEW_DECISION_ID_VERSION = "review-decision-v1"


def review_decision_payload(
    *,
    candidate_id: str,
    kind: str,
    from_status: str,
    to_status: str,
    reviewer_identity: str,
    reviewer_action: str,
    note: str,
    baseline: ConflictBaselineRecord | None,
    conflict_resolution: str,
    resolution_choice: str | None,
    successor_candidate_id: str,
) -> dict[str, Any]:
    """The immutable content of a decision, canonicalized for hashing.

    A decision id must identify the *decision*, not the moment it was recorded. Hashing only
    (candidate, from, to, kind) would collapse two genuinely different human decisions into one
    id — the same person confirming against revision 7 and revision 8, by different reasoning,
    would be one audit event — and the provenance foreign key would then rest on an identity that
    cannot tell them apart.

    ``decided_at`` is excluded: it is bookkeeping, and a timestamp must never be able to change
    an identity.

    The baseline is validated here rather than trusted, because this function runs *before* the
    decision model exists and would otherwise turn a malformed baseline into an ``AttributeError``
    instead of the ``ValidationError`` the caller deserves.
    """

    if baseline is not None and not isinstance(baseline, ConflictBaselineRecord):
        baseline = ConflictBaselineRecord.model_validate(baseline)
    return {
        "id": REVIEW_DECISION_ID_VERSION,
        "candidate": candidate_id,
        "kind": kind,
        "from": from_status,
        "to": to_status,
        "reviewer_identity": reviewer_identity,
        "reviewer_action": reviewer_action,
        "note": note,
        "baseline": None
        if baseline is None
        else {
            "revision": baseline.baseline_revision,
            "identity": baseline.comparison_identity,
            "path": baseline.comparison_path,
            "digest_version": baseline.digest_version,
            "digest": baseline.value_digest,
            "value_present": baseline.value_present,
        },
        "conflict_resolution": conflict_resolution,
        "resolution_choice": resolution_choice,
        "successor_candidate_id": successor_candidate_id,
    }


def review_decision_id(**payload: Any) -> str:
    return "m6dec_" + canonical_digest(review_decision_payload(**payload))


def _conflict_id(
    candidate_id: str, reviewed: ConflictBaselineRecord, current: ConflictBaselineRecord
) -> str:
    return "m6cfl_" + canonical_digest(
        {
            "candidate": candidate_id,
            "path": reviewed.comparison_path,
            "identity": reviewed.comparison_identity,
            "reviewed": reviewed.value_digest,
            "current": current.value_digest,
        }
    )


def _finding_id(candidate_id: str, review_decision_id: str) -> str:
    return "m6find_" + canonical_digest({"candidate": candidate_id, "decision": review_decision_id})


#: Content-derived identities carry the whole digest. These are long-lived audit objects, and
#: 64 bits of a hash is not a saving worth a collision in an engineering record.


def _created_element_id(finding: ConfirmedSemanticFinding, facts: ProposedSemantics) -> str:
    """Still derived, still deterministic — and full length.

    A generated element id becomes a real engineering object's identity, which is the one thing
    in this module that is *more* consequential than a patch id: a truncated hash there is a
    collision in the drawing, not in a report.
    """

    return "el_m6" + canonical_digest(
        {"finding": finding.finding_id, "class": facts.symbol_class, "tag": facts.equipment_tag}
    )


__all__ = [
    "M6_PATCH_COMPILER",
    "M6_PATCH_COMPILER_RULES",
    "M6_PATCH_COMPILER_VERSION",
    "PHASE_2A_SUPPORTED_INTENTS",
    "CandidateNotFound",
    "CandidateSchemaViolation",
    "CompilationRefused",
    "Confirmation",
    "GovernedWriteNotAuthorized",
    "IllegalTransition",
    "M6CandidateRepository",
    "M6CandidateService",
    "M6CoreError",
    "NotConfirmedError",
    "TransitionEvidenceMissing",
    "REVIEW_DECISION_ID_VERSION",
    "SEMANTIC_VALUE_CANONICALIZERS",
    "SEMANTIC_VALUE_DIGEST_VERSION",
    "baseline_record",
    "build_baseline_graph",
    "canonical_digest",
    "canonicalize_semantic_value",
    "resolve_identity",
    "review_decision_id",
    "review_decision_payload",
    "semantic_value",
    "value_digest",
]
