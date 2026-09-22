"""M6 Phase-2A domain schemas: the governed candidate core.

The governance contract in :mod:`agentcad.m6_ingestion_contract` says what is permitted.
These schemas are where the permission becomes a type, and the split is the whole point:

* :class:`SemanticCandidate` describes **what something appears to be**. It has no
  operations, no op field and ``extra="forbid"``, so a candidate that carries a write
  operation is a validation error rather than a code-review question.
* :class:`ConfirmedSemanticFinding` describes **what a person accepted**, and it keeps the
  fact — not the patch. A later patch-format change cannot invalidate a historical decision.
* :class:`StructuredEngineeringPatch` describes **how the drawing would change**. It is
  produced only from a confirmed finding, by a deterministic compiler, and in Phase-2A it
  declares ``write_authority = "none"``: it can be built and inspected, never applied.

Two conventions are load-bearing and repeated everywhere:

* **Timestamps are provenance, never identity.** ``created_at`` / ``decided_at`` / ``imported_at``
  exist so a reviewer can order events, and they are excluded from every digest.
* **Nothing here is a permission.** ``confidence`` describes a judgement; ``review_status``
  describes where a candidate sits in the queue. Authority comes from a recorded review
  decision, and only from an explicit human one.

Where a schema must agree with the contract (states, transitions, producers, policy
vocabulary), it declares its own Literal and ``tests/test_m6_candidate_core.py`` asserts the
two sets are equal — so the contract cannot drift away from the code that enforces it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from .agent_semantic_models import SemanticOperation
from .m6_ingestion_contract import (
    CONFIDENCE_MEANING,
    M6_CONTRACT_VERSION,
)
from .models import StrictModel, utc_now

M6_CANDIDATE_SCHEMA = "pid-agent.m6-semantic-candidate"
M6_REVIEW_DECISION_SCHEMA = "pid-agent.m6-review-decision"
M6_CONFIRMED_FINDING_SCHEMA = "pid-agent.m6-confirmed-semantic-finding"
M6_PATCH_SCHEMA = "pid-agent.m6-structured-engineering-patch"

#: Phase-2A builds patches and never applies them. A patch that claimed otherwise would be
#: claiming the permission the milestone has not signed yet.
PATCH_WRITE_AUTHORITY = "none"

FactKind = Literal[
    "symbol_class",
    "equipment_tag",
    "annotation_role",
    "connection_relationship",
    "unresolved",
]

AnnotationRole = Literal["equipment_label", "line_label", "instrument_tag", "note", "unknown"]
UnresolvedReason = Literal["insufficient_evidence", "ambiguous", "out_of_catalog"]

CandidateStatus = Literal[
    "proposed",
    "needs_review",
    "confirmed",
    "rejected",
    "superseded",
    "conflicted",
    "applied",
]

ProducerKey = Literal["deterministic_rule_engine", "typesafe", "repair_llm_model"]
ConfidenceSource = Literal["model", "deterministic_rule", "consensus"]
CalibrationClass = Literal["not_measured", "measured_on_gold_corpus", "inherited_from_producer"]

#: A transition that is a *record of an event* rather than a person's decision. Only the
#: human kinds may carry a reviewer action, and only they may produce ``confirmed``.
DecisionKind = Literal[
    "filed",
    "human_confirm",
    "human_reject",
    "conflict_detected",
    "human_resolves_conflict",
    "superseded",
]

HUMAN_DECISION_KINDS: tuple[str, ...] = (
    "human_confirm",
    "human_reject",
    "human_resolves_conflict",
)

ConflictResolutionChoice = Literal["keep_existing", "accept_proposed"]

#: The declared write intents, in the same vocabulary as the contract's policy table.
WriteIntent = Literal[
    "creation",
    "metadata_enrichment",
    "relationship_addition",
    "overwrite_existing_authoritative_value",
    "delete_existing_engineering_object",
    "replace_topology",
]
PolicyDisposition = Literal[
    "allowed_after_confirmation",
    "conflict_requires_human_resolution",
    "forbidden",
]

#: Semantic paths M6 can describe. They are the comparison key's second half and the
#: vocabulary a review baseline is recorded in.
SemanticPath = Literal["equipment_tag", "equipment_class", "annotation_role", "existence"]


class SourceArtifactRef(StrictModel):
    """The imported drawing a judgement is about. Immutable: a re-import is a new artifact."""

    artifact_id: str = Field(min_length=1)
    source_document_id: str = Field(min_length=1)
    source_revision: int = Field(ge=0)
    content_hash: str = ""
    imported_at: datetime = Field(default_factory=utc_now)
    note: str = ""


class RegionGeometry(StrictModel):
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class TextSpan(StrictModel):
    text: str = Field(min_length=1)
    bounds: RegionGeometry | None = None


class SourceRegion(StrictModel):
    """Where in the artifact a judgement came from, re-locatable under the same revision.

    All three selector kinds are carried, not chosen between: a scan has only geometry, a
    vector drawing can also name the elements it overlapped, and an OCR/text pass adds spans.
    ``source_revision`` is part of the region because identity is "re-croppable *here*", not
    "these coordinates forever": the contract requires a new region identity when the source
    revision moves.
    """

    region_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    source_document_id: str = Field(min_length=1)
    source_revision: int = Field(ge=0)
    geometry_selector: RegionGeometry | None = None
    element_refs: list[str] = Field(default_factory=list)
    text_spans: list[TextSpan] = Field(default_factory=list)
    page: int | None = Field(default=None, ge=1)
    layer: str = ""
    coordinate_frame: str = "document"

    @model_validator(mode="after")
    def validate_has_a_selector(self) -> SourceRegion:
        if not (self.geometry_selector or self.element_refs or self.text_spans):
            raise ValueError(
                "a source region needs at least one selector "
                "(geometry_selector, element_refs or text_spans)"
            )
        if self.page is None and not self.geometry_selector and not self.element_refs:
            raise ValueError("a text-only region must name its page")
        return self


class ProposedSemantics(StrictModel):
    """Declared facts about the drawing — never write operations.

    ``target_identity`` names *what the fact is about* in the committed engineering model
    (an ``engineering_id``, a ``tag_key``, or ``element:<id>``). An empty target means the
    fact is about something that is not in the model yet, which is how creation is expressed.
    """

    target_identity: str = ""
    symbol_class: str = ""
    equipment_tag: str = ""
    annotation_role: AnnotationRole | None = None
    relationship_source: str = ""
    relationship_target: str = ""
    unresolved_reason: UnresolvedReason | None = None

    @model_validator(mode="after")
    def validate_declares_something(self) -> ProposedSemantics:
        declared = (
            self.symbol_class,
            self.equipment_tag,
            self.annotation_role,
            self.relationship_source,
            self.relationship_target,
            self.unresolved_reason,
        )
        if not any(declared):
            raise ValueError("proposed_semantics must declare at least one fact")
        if bool(self.relationship_source) != bool(self.relationship_target):
            raise ValueError("a relationship fact needs both endpoints")
        return self


class Confidence(StrictModel):
    """A description of certainty. Not a permission, and not a correctness guarantee.

    ``meaning`` is frozen to the contract's wording so no producer can restate a confidence
    as a promise. ``measured_on_gold_corpus`` needs a gate id: the class is signed by an
    independent calibration gate, never awarded by the producer that benefits from it.
    """

    value: float = Field(ge=0, le=1)
    calibration_class: CalibrationClass = "not_measured"
    source: ConfidenceSource
    meaning: str = CONFIDENCE_MEANING
    calibration_gate_id: str = ""

    @model_validator(mode="after")
    def validate_calibration_is_signed(self) -> Confidence:
        if self.meaning != CONFIDENCE_MEANING:
            raise ValueError("confidence.meaning is fixed by the contract and cannot be restated")
        if self.calibration_class == "measured_on_gold_corpus" and not self.calibration_gate_id:
            raise ValueError(
                "measured_on_gold_corpus requires a signed calibration gate id; "
                "M6 v1 has no such gate, so the class is not available yet"
            )
        if self.calibration_class != "measured_on_gold_corpus" and self.calibration_gate_id:
            raise ValueError("a calibration gate id only applies to measured_on_gold_corpus")
        return self


class CandidateEvidence(StrictModel):
    """What was observed, so a reviewer can disagree with the observation, not only the conclusion."""

    kind: Literal["observed_text", "geometry", "element_ref", "neighbour", "catalogue", "rule"]
    detail: str = Field(min_length=1)
    region_id: str = ""
    observed_value: str = ""


class ProducerRef(StrictModel):
    key: ProducerKey
    version: str = ""


class ProvenanceRef(StrictModel):
    provider: str = ""
    model: str = ""
    procedure_version: str = ""


class ConflictBaselineRecord(StrictModel):
    """The authoritative state a judgement was made against.

    The comparison key is ``comparison_identity`` + ``comparison_path``; only a digest of the
    value is stored, because the digest is what gets re-read and compared before apply.
    """

    baseline_revision: int = Field(ge=0)
    comparison_identity: str = Field(min_length=1)
    comparison_path: SemanticPath
    value_digest: str = Field(min_length=1)
    value_present: bool = False


class CandidateConflict(StrictModel):
    conflict_id: str = Field(min_length=1)
    baseline: ConflictBaselineRecord
    proposed_value_digest: str = Field(min_length=1)
    detected_at: datetime = Field(default_factory=utc_now)
    status: Literal["open", "resolved_keep_existing", "resolved_accept_proposed"] = "open"
    resolution_decision_id: str = ""


class SemanticCandidate(StrictModel):
    """A proposal. Never a patch, never a permission, never an update to the drawing."""

    schema_name: Literal["pid-agent.m6-semantic-candidate"] = Field(
        default=M6_CANDIDATE_SCHEMA, alias="schema"
    )
    version: Literal[1] = 1
    contract: Literal["m6-ingestion-contract/1"] = M6_CONTRACT_VERSION
    candidate_id: str = Field(min_length=1)
    artifact: SourceArtifactRef
    region: SourceRegion
    candidate_type: FactKind
    proposed_semantics: ProposedSemantics
    confidence: Confidence
    evidence: list[CandidateEvidence] = Field(min_length=1)
    producer: ProducerRef
    provenance: ProvenanceRef = Field(default_factory=ProvenanceRef)
    conflicts: list[CandidateConflict] = Field(default_factory=list)
    review_status: CandidateStatus = "proposed"
    supersedes: str | None = None
    derived_from: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_region_matches_artifact(self) -> SemanticCandidate:
        if self.region.source_document_id != self.artifact.source_document_id:
            raise ValueError("region and artifact must describe the same source document")
        if self.region.source_revision != self.artifact.source_revision:
            raise ValueError(
                "region and artifact must share the source revision: a region is re-locatable "
                "only under the revision it was cropped from"
            )
        return self

    @model_validator(mode="after")
    def validate_fact_kind_shape(self) -> SemanticCandidate:
        facts = self.proposed_semantics
        if self.candidate_type == "unresolved":
            if facts.unresolved_reason is None:
                raise ValueError("an unresolved candidate must declare why it is unresolved")
            if facts.symbol_class or facts.equipment_tag or facts.annotation_role:
                raise ValueError("an unresolved candidate must not also assert a fact")
        else:
            if facts.unresolved_reason is not None:
                raise ValueError("unresolved_reason belongs to candidate_type 'unresolved'")
            required = {
                "symbol_class": facts.symbol_class,
                "equipment_tag": facts.equipment_tag,
                "annotation_role": facts.annotation_role,
                "connection_relationship": facts.relationship_source,
            }[self.candidate_type]
            if not required:
                raise ValueError(
                    f"candidate_type {self.candidate_type!r} needs its corresponding fact"
                )
            if self.candidate_type == "symbol_class" and facts.symbol_class == facts.equipment_tag:
                raise ValueError("symbol_class must name a catalogue class, not repeat the tag")
        return self


class ReviewDecision(StrictModel):
    """One recorded transition of the review queue, append-only.

    The table this lands in is the transition log, which is why a producer's filing is a row
    too. What matters for authority is that only the human kinds carry a reviewer action and
    identity, and only a human decision can lead to ``confirmed``.
    """

    schema_name: Literal["pid-agent.m6-review-decision"] = Field(
        default=M6_REVIEW_DECISION_SCHEMA, alias="schema"
    )
    version: Literal[1] = 1
    review_decision_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    kind: DecisionKind
    from_status: CandidateStatus
    to_status: CandidateStatus
    reviewer_action: str = ""
    reviewer_identity: str = ""
    decided_at: datetime = Field(default_factory=utc_now)
    note: str = ""
    baseline: ConflictBaselineRecord | None = None
    #: The conflict this transition records. It lives on the decision (the append-only log)
    #: because a candidate row is insert-only: a fact's history grows, it is never edited.
    conflict: CandidateConflict | None = None
    successor_candidate_id: str = ""
    conflict_resolution: str = ""
    resolution_choice: ConflictResolutionChoice | None = None

    @property
    def is_human(self) -> bool:
        return self.kind in HUMAN_DECISION_KINDS

    @model_validator(mode="after")
    def validate_reviewer_action_belongs_to_a_person(self) -> ReviewDecision:
        if self.is_human:
            if not self.reviewer_action.strip():
                raise ValueError(
                    "a human decision must record the reviewer action, not just a new status"
                )
            if not self.reviewer_identity.strip():
                raise ValueError("a human decision must record who made it")
        elif self.reviewer_action or self.reviewer_identity:
            raise ValueError(
                f"{self.kind!r} is an event record, not a reviewer action: it must not claim one"
            )
        if self.kind == "human_resolves_conflict" and not self.conflict_resolution.strip():
            raise ValueError("resolving a conflict must state what the resolution was")
        if self.kind != "human_resolves_conflict" and self.conflict_resolution:
            raise ValueError("conflict_resolution only applies to human_resolves_conflict")
        if self.resolution_choice is not None and self.kind != "human_resolves_conflict":
            raise ValueError("resolution_choice only applies to human_resolves_conflict")
        if self.kind == "superseded" and not self.successor_candidate_id:
            raise ValueError("a superseded decision must name the successor candidate")
        return self


class ConfirmedSemanticFinding(StrictModel):
    """A fact a person accepted. Deliberately not a patch.

    The finding keeps ``confirmed_semantics`` (a fact) and the whole provenance chain, so the
    deterministic compiler can be re-run — with a different compiler version, or after a patch
    format change — without re-asking the person who decided.
    """

    schema_name: Literal["pid-agent.m6-confirmed-semantic-finding"] = Field(
        default=M6_CONFIRMED_FINDING_SCHEMA, alias="schema"
    )
    version: Literal[1] = 1
    finding_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    review_decision_id: str = Field(min_length=1)
    artifact: SourceArtifactRef
    region_id: str = Field(min_length=1)
    #: The region's selector, carried forward because a creation fact needs it to place the new
    #: object. It is provenance, but of the re-locatable kind, so it may enter a digest.
    region_geometry: RegionGeometry | None = None
    candidate_type: FactKind
    confirmed_semantics: ProposedSemantics
    evidence: list[CandidateEvidence] = Field(min_length=1)
    baseline: ConflictBaselineRecord
    producer: ProducerRef
    provenance: ProvenanceRef = Field(default_factory=ProvenanceRef)
    provenance_chain: list[str] = Field(min_length=4)
    confirmed_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_chain(self) -> ConfirmedSemanticFinding:
        expected = [
            self.artifact.artifact_id,
            self.region_id,
            self.candidate_id,
            self.review_decision_id,
        ]
        if self.provenance_chain[:4] != expected:
            raise ValueError(
                "the provenance chain must read artifact -> region -> candidate -> review decision, "
                f"got {self.provenance_chain[:4]}"
            )
        if self.candidate_type == "unresolved":
            raise ValueError("an unresolved candidate carries no confirmable fact")
        return self


class StructuredEngineeringPatch(StrictModel):
    """What the drawing would become, produced deterministically from confirmed findings.

    ``patch_id`` is derived from ``canonical_digest``, so compiling the same finding twice
    cannot produce two different patches that merely look alike: identity and content are the
    same statement. ``write_authority`` is fixed to ``none`` in Phase-2A.
    """

    schema_name: Literal["pid-agent.m6-structured-engineering-patch"] = Field(
        default=M6_PATCH_SCHEMA, alias="schema"
    )
    version: Literal[1] = 1
    patch_id: str = Field(min_length=1)
    finding_ids: list[str] = Field(min_length=1)
    intent: WriteIntent
    policy_disposition: PolicyDisposition
    operations: list[SemanticOperation] = Field(min_length=1)
    compiler: str = Field(min_length=1)
    compiler_version: str = Field(min_length=1)
    compiler_rules: str = Field(min_length=1)
    baseline_revision: int = Field(ge=0)
    canonical_digest: str = Field(min_length=16)
    write_authority: Literal["none"] = PATCH_WRITE_AUTHORITY

    @model_validator(mode="after")
    def validate_disposition(self) -> StructuredEngineeringPatch:
        if self.policy_disposition != "allowed_after_confirmation":
            raise ValueError(
                "only an allowed-after-confirmation intent may be compiled into a patch: "
                "overwrite is a conflict and destructive intents are forbidden"
            )
        return self


def candidate_document(candidate: SemanticCandidate) -> dict[str, Any]:
    """The candidate as a plain document, with provenance timestamps kept out of any digest."""

    return candidate.model_dump(mode="json", by_alias=True)
