"""M6 phase 1: the governance contract for semantic ingestion, as declared data.

The M6 success criterion is deliberately *not* "how much of a drawing can a model read".
It is: **an unconfirmed semantic judgement can never cross the governance boundary, and a
confirmed one can enter the engineering model deterministically, auditably and
reversibly.** That is a statement about permission, so it is written here as data that a
test can contradict — not as prose that a later implementer can quietly reinterpret.

Design choices worth stating, because they are the reason this is a module and not a
paragraph:

* **The layer chain is ordered and each layer declares its own immutable id.** A
  reviewer asking "which drawing revision produced this committed element against which
  reviewer's decision" is answered by the chain, not by a log message.
* **Exactly one layer may write the engineering model.** ``apply_v2_transaction`` is the
  only producer surface M6 is allowed to use; the candidate, review, finding and patch
  layers *request* a change and cannot perform one. Asserting "exactly one" is what keeps
  a future "shortcut" from being added without naming it in a test.
* **Confidence is declared as a description, never as an authority input.** The authority
  inputs are enumerated, and the validator refuses a confidence-shaped name among them.
  This is the mechanical form of "confidence = 1.00 is still not permission".
* **The state machine's two key guarantees are properties, not table rows.** Only
  ``confirmed`` may lead to ``applied``, and (in v1) reaching ``confirmed`` requires a
  recorded reviewer action because the auto-accept whitelist is empty. Both are checked by
  walking the declared transitions, so adding an edge that breaks them fails a test even
  if the author never reads this docstring.
* **Phase 1 is design and contract only.** The forbidden list at the bottom is part of the
  contract: no ingestion runtime, no candidate persistence, no new HTTP route. A test
  enumerates the live OpenAPI surface and asserts none of them exist yet.

Nothing here writes anything, and nothing here is imported by the application. It is a
review document with a build-breaking check attached, in the same spirit as
``surface_contract.py`` and ``m4_invariance.py``.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass
from typing import Literal

M6_CONTRACT_VERSION = "m6-ingestion-contract/1"

# --------------------------------------------------------------------------------------
# §2 Fixed architecture: the layer chain and where write permission lives
# --------------------------------------------------------------------------------------

WriteAuthority = Literal["engineering_model", "none"]


@dataclass(frozen=True)
class IngestionLayer:
    """One stage of the ingestion chain.

    ``immutable_id_field`` is the identity the stage contributes to the provenance chain.
    ``write_authority`` is the permission the stage itself holds — only the apply-v2
    transaction stage may touch the engineering model.
    """

    position: int
    key: str
    immutable_id_field: str
    produced_by: str
    write_authority: WriteAuthority
    requires_human_decision: bool
    evidence_obligations: tuple[str, ...]
    note: str


INGESTION_LAYERS: tuple[IngestionLayer, ...] = (
    IngestionLayer(
        position=1,
        key="imported_artifact",
        immutable_id_field="artifact_id",
        produced_by="import surface (human)",
        write_authority="none",
        requires_human_decision=False,
        evidence_obligations=("source_document_id", "source_revision"),
        note="The drawing as it arrived. Immutable: re-upload makes a new artifact, not a new revision of this one.",
    ),
    IngestionLayer(
        position=2,
        key="source_region",
        immutable_id_field="region_id",
        produced_by="deterministic extraction",
        write_authority="none",
        requires_human_decision=False,
        evidence_obligations=("artifact_id", "geometry_bounds", "text_spans"),
        note="What part of the artifact a judgement is about, so a reviewer can re-crop it later.",
    ),
    IngestionLayer(
        position=3,
        key="semantic_candidate",
        immutable_id_field="candidate_id",
        produced_by="rule engine | TypeSafe | repair/LLM model",
        write_authority="none",
        requires_human_decision=False,
        evidence_obligations=(
            "region_id",
            "producer",
            "producer_version",
            "confidence",
            "evidence",
        ),
        note="A proposal about what something IS. It has no patch authority and no write authority.",
    ),
    IngestionLayer(
        position=4,
        key="review_decision",
        immutable_id_field="review_decision_id",
        produced_by="human reviewer",
        write_authority="none",
        requires_human_decision=True,
        evidence_obligations=("candidate_id", "reviewer_action", "reviewer_identity", "decided_at"),
        note="The record of a person accepting or rejecting a fact. Not a status flag flip.",
    ),
    IngestionLayer(
        position=5,
        key="confirmed_semantic_finding",
        immutable_id_field="finding_id",
        produced_by="review decision",
        write_authority="none",
        requires_human_decision=True,
        evidence_obligations=("review_decision_id", "proposed_semantics", "provenance_chain"),
        note="An accepted FACT. It is not a patch and does not expire when the patch format changes.",
    ),
    IngestionLayer(
        position=6,
        key="structured_engineering_patch",
        immutable_id_field="patch_id",
        produced_by="deterministic patch compiler",
        write_authority="none",
        requires_human_decision=False,
        evidence_obligations=("finding_ids", "compiler_version", "patch_compiler_rules"),
        note="How the drawing would change. Deterministic from the findings: same findings + same compiler = same patch.",
    ),
    IngestionLayer(
        position=7,
        key="apply_v2_transaction",
        immutable_id_field="transaction_id",
        produced_by="apply-v2 gate",
        write_authority="engineering_model",
        requires_human_decision=True,
        evidence_obligations=(
            "patch_id",
            "dry_run_result",
            "locality_check",
            "conflict_check",
            "policy_verdict",
        ),
        note="The single production write entry point. Nothing else in M6 may write the engineering model.",
    ),
    IngestionLayer(
        position=8,
        key="committed_revision",
        immutable_id_field="revision",
        produced_by="document store commit",
        write_authority="none",
        requires_human_decision=False,
        evidence_obligations=("transaction_id", "audit_record", "revision_evidence"),
        note=(
            "The existing atomic commit path (revision + diff + audit + harness close-out in one "
            "transaction). It records what the transaction produced; the permission belonged to the "
            "transaction, not to the revision it created."
        ),
    ),
)

#: The one layer allowed to write the engineering model. Asserted, not assumed.
SOLE_WRITE_LAYER = "apply_v2_transaction"

# --------------------------------------------------------------------------------------
# §3 SemanticCandidate is its own schema
# --------------------------------------------------------------------------------------

FactKind = Literal[
    "symbol_class",
    "equipment_tag",
    "annotation_role",
    "connection_relationship",
    "unresolved",
]


@dataclass(frozen=True)
class CandidateField:
    name: str
    required: bool
    meaning: str


SEMANTIC_CANDIDATE_FIELDS: tuple[CandidateField, ...] = (
    CandidateField("candidate_id", True, "immutable id of this proposal"),
    CandidateField("source_document_id", True, "which document the proposal is about"),
    CandidateField("source_revision", True, "which revision of that document"),
    CandidateField("source_region", True, "cropped region / element_refs the judgement is about"),
    CandidateField("candidate_type", True, "which kind of fact this is"),
    CandidateField("proposed_semantics", True, "declared facts, never write operations"),
    CandidateField("confidence", True, "a description of certainty, never a permission"),
    CandidateField(
        "evidence", True, "what was observed, so a reviewer can disagree with the observation"
    ),
    CandidateField("producer", True, "rule engine / TypeSafe / repair model, and its version"),
    CandidateField(
        "provenance", True, "model/provider identity and the procedure or rule version used"
    ),
    CandidateField(
        "conflicts", True, "declared clashes with existing authoritative semantics (may be empty)"
    ),
    CandidateField("review_status", True, "position in the review state machine"),
    CandidateField("supersedes", False, "the candidate this one replaces, if any"),
    CandidateField("derived_from", False, "upstream candidate(s) this one was derived from"),
    CandidateField("created_at", True, "when the proposal was made"),
)

#: ``candidate_type`` values that carry no confirmable fact: they may be reviewed, but
#: there is nothing for a reviewer to confirm, so they cannot leave ``needs_review`` for
#: ``confirmed``. "No decision" and "insufficient evidence" are therefore recorded
#: (auditable) rather than silently dropped.
UNCONFIRMABLE_CANDIDATE_TYPES: tuple[str, ...] = ("unresolved",)

#: Negative rules that make the schema boundary reviewable at a glance.
CANDIDATE_FORBIDDEN_CONTENT: tuple[str, ...] = (
    "delete_element",
    "update_element",
    "move_element",
    "write_connector",
    "replace_symbol",
    "clear_document",
    "any_write_operation",
)

# --------------------------------------------------------------------------------------
# §4 Three judgement sources, strictly separated responsibilities
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgmentProducer:
    key: str
    role: str
    may_produce: tuple[str, ...]
    may_never: tuple[str, ...]
    auto_accept_classes: tuple[str, ...]
    may_grant_apply_authority: bool
    requires_human_review: bool


JUDGMENT_PRODUCERS: tuple[JudgmentProducer, ...] = (
    JudgmentProducer(
        key="deterministic_rule_engine",
        role="deterministic parsing, format constraints, geometry, existing-schema checks",
        may_produce=("deterministic_fact", "candidate"),
        may_never=(
            "grant_apply_authority",
            "widen_its_own_whitelist",
            "edit_the_engineering_model",
        ),
        auto_accept_classes=(),
        may_grant_apply_authority=False,
        requires_human_review=True,
    ),
    JudgmentProducer(
        key="typesafe",
        role="bounded judgement / classification (semantic judge)",
        may_produce=("candidate", "classification", "confidence", "evidence"),
        may_never=(
            "grant_apply_authority",
            "author_arbitrary_engineering_writes",
            "write_the_engineering_model",
            "act_as_patch_compiler",
        ),
        auto_accept_classes=(),
        may_grant_apply_authority=False,
        requires_human_review=True,
    ),
    JudgmentProducer(
        key="repair_llm_model",
        role="candidate interpretation, ambiguity resolution, structured suggestion",
        may_produce=("candidate", "proposal"),
        may_never=(
            "grant_apply_authority",
            "bypass_human_confirmation",
            "write_the_engineering_model",
        ),
        auto_accept_classes=(),
        may_grant_apply_authority=False,
        requires_human_review=True,
    ),
)

#: Classes that could in principle be auto-accepted *if* a separate gate signed them.
#: M6 v1 signs none of them, so every path to ``confirmed`` goes through a person.
AUTO_ACCEPT_WHITELIST: tuple[str, ...] = ()
AUTO_ACCEPT_CLASSES_DECLARED: tuple[str, ...] = ("deterministic_fact",)

# --------------------------------------------------------------------------------------
# §5 Confidence is never authority
# --------------------------------------------------------------------------------------

#: The only inputs the write-authority decision may read. ``confidence`` is deliberately
#: absent, and ``validate_contract()`` fails if a confidence-shaped name appears here.
AUTHORITY_DECISION_INPUTS: tuple[str, ...] = (
    "review_status",
    "policy_verdict",
    "conflict_state",
    "apply_v2_validation",
)

CONFIDENCE_FIELDS: tuple[str, ...] = (
    "value",
    "calibration_class",
    "source",
    "meaning",
)
CONFIDENCE_SOURCES: tuple[str, ...] = ("model", "deterministic_rule", "consensus")
CONFIDENCE_CALIBRATION_CLASSES: tuple[str, ...] = (
    "not_measured",
    "measured_on_gold_corpus",
    "inherited_from_producer",
)
CONFIDENCE_MEANING = (
    "classification confidence for this judgement, not a correctness guarantee and not a permission"
)
CONFIDENCE_CAN_AUTHORISE_A_WRITE = False

# --------------------------------------------------------------------------------------
# §6 The review queue is an explicit state machine
# --------------------------------------------------------------------------------------

CANDIDATE_STATES: tuple[str, ...] = (
    "proposed",
    "needs_review",
    "confirmed",
    "rejected",
    "superseded",
    "conflicted",
    "applied",
)


@dataclass(frozen=True)
class CandidateTransition:
    from_state: str
    to_state: str
    trigger: str
    requires: tuple[str, ...]


CANDIDATE_TRANSITIONS: tuple[CandidateTransition, ...] = (
    CandidateTransition("proposed", "needs_review", "producer_files_candidate", ()),
    CandidateTransition(
        "proposed",
        "conflicted",
        "contradicts_existing_authoritative_value",
        ("conflict_record",),
    ),
    CandidateTransition(
        "proposed", "superseded", "replaced_before_review", ("successor_candidate_id",)
    ),
    CandidateTransition(
        "needs_review",
        "confirmed",
        "human_confirm",
        ("reviewer_action", "review_decision"),
    ),
    CandidateTransition(
        "needs_review",
        "rejected",
        "human_reject",
        ("reviewer_action", "review_decision"),
    ),
    CandidateTransition("needs_review", "conflicted", "conflict_detected", ("conflict_record",)),
    CandidateTransition(
        "needs_review", "superseded", "replaced_during_review", ("successor_candidate_id",)
    ),
    CandidateTransition(
        "conflicted",
        "needs_review",
        "human_resolves_conflict",
        ("conflict_resolution", "reviewer_action"),
    ),
    CandidateTransition("conflicted", "rejected", "human_rejects_conflict", ("reviewer_action",)),
    CandidateTransition(
        "conflicted", "superseded", "replaced_while_conflicted", ("successor_candidate_id",)
    ),
    CandidateTransition(
        "confirmed",
        "applied",
        "apply_v2_accepted",
        ("compiled_patch", "policy_verdict", "conflict_check", "apply_v2_validation"),
    ),
    CandidateTransition(
        "confirmed", "superseded", "replaced_after_confirmation", ("successor_candidate_id",)
    ),
    CandidateTransition(
        "rejected", "superseded", "replaced_after_rejection", ("successor_candidate_id",)
    ),
    CandidateTransition(
        "applied", "superseded", "replaced_after_apply", ("successor_candidate_id",)
    ),
)

#: Transitions M6 v1 forbids outright. Declared so that a reviewer can see the intent and
#: so that the validator can fail on an accidental re-introduction.
FORBIDDEN_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("proposed", "applied"),
    ("proposed", "confirmed"),
    ("needs_review", "applied"),
    ("conflicted", "applied"),
    ("rejected", "applied"),
    ("superseded", "applied"),
    ("applied", "confirmed"),
    ("applied", "rejected"),
)

#: Reaching ``confirmed`` must record a person's action; ``validate_contract()`` only
#: tolerates a missing reviewer action when the auto-accept whitelist is non-empty.
REVIEWER_ACTION_EVIDENCE = "reviewer_action"

#: Undo is a new governed transaction, never a state rollback: the confirmed fact stays
#: confirmed (facts do not un-happen), and the reversal is its own auditable write.
UNDO_IS_A_STATE_ROLLBACK = False

# --------------------------------------------------------------------------------------
# §7 v1 write policy: default deny for destructive intents
# --------------------------------------------------------------------------------------

PolicyDisposition = Literal[
    "allowed_after_confirmation",
    "conflict_requires_human_resolution",
    "forbidden",
]


@dataclass(frozen=True)
class WritePolicyRow:
    intent: str
    disposition: PolicyDisposition
    reason: str


WRITE_POLICY_V1: tuple[WritePolicyRow, ...] = (
    WritePolicyRow(
        intent="creation",
        disposition="allowed_after_confirmation",
        reason="Adding a new object does not contradict an existing authoritative statement.",
    ),
    WritePolicyRow(
        intent="metadata_enrichment",
        disposition="allowed_after_confirmation",
        reason="Filling an unstated attribute is additive; a stated value is an overwrite, not enrichment.",
    ),
    WritePolicyRow(
        intent="relationship_addition",
        disposition="allowed_after_confirmation",
        reason="A new connection can be verified by dry-run and locality check before it lands.",
    ),
    WritePolicyRow(
        intent="overwrite_existing_authoritative_value",
        disposition="conflict_requires_human_resolution",
        reason=(
            "A model believing the drawing is wrong is a hypothesis, not a correction. It becomes a "
            "conflict candidate; a person resolves it."
        ),
    ),
    WritePolicyRow(
        intent="delete_existing_engineering_object",
        disposition="forbidden",
        reason="Deletion is not needed to add understanding, and its blast radius is the whole topology.",
    ),
    WritePolicyRow(
        intent="replace_topology",
        disposition="forbidden",
        reason="Replacing connectivity is the highest-consequence write and has no M6 use case that is not a rewrite.",
    ),
)

# --------------------------------------------------------------------------------------
# §10 Gold corpus from scratch: dimensions, required fields, and the seed-material boundary
# --------------------------------------------------------------------------------------

GOLD_CORPUS_DIMENSIONS: tuple[str, ...] = (
    "symbol_classification",
    "tag_association",
    "annotation_role",
    "connection_relationship",
    "ambiguous_no_decision",
    "conflict_with_existing_semantics",
    "insufficient_evidence",
)

GOLD_ITEM_REQUIRED_FIELDS: tuple[str, ...] = (
    "source_crop_reference",
    "expected_semantic_fact",
    "acceptable_ambiguity",
    "forbidden_interpretation",
    "review_provenance",
)

#: Where the R8 annotation material may live. Neither path is a corpus path, and no
#: entry there may be counted as expected truth.
SEED_MATERIAL_PATHS: tuple[str, ...] = (
    "research_examples/",
    "candidate_seed_material/",
)
GOLD_CORPUS_PATH = "backend/tests/m6_gold_corpus/"
SEED_MATERIAL_COUNTS_AS_EXPECTED_TRUTH = False

# --------------------------------------------------------------------------------------
# §11 Replay is not undo
# --------------------------------------------------------------------------------------

REPLAY_FROZEN_INPUTS: tuple[str, ...] = (
    "candidate",
    "review_decision",
    "patch_compiler_version",
    "patch_compiler_rules",
)
REPLAY_COMPARISON_TARGET = "applied_transaction_operations"
REPLAY_MUST_BE_BIT_IDENTICAL = True
UNDO_MECHANISM = "reverse_governed_transaction_through_apply_v2"

# --------------------------------------------------------------------------------------
# §14 Phase boundary: what this phase may and may not contain
# --------------------------------------------------------------------------------------

PHASE_1_DELIVERABLES: tuple[str, ...] = (
    "task_book",
    "governance_contract_data",
    "doc_contract_agreement_test",
)

PHASE_1_FORBIDDEN: tuple[str, ...] = (
    "ingestion_runtime",
    "candidate_persistence",
    "new_http_route",
    "new_mcp_tool",
    "review_queue_api",
    "patch_compiler",
    "gold_corpus_claim",
)

#: Substrings that must not appear in any live HTTP path or MCP tool name while M6 is in
#: phase 1. A test enumerates the real surface and fails if one appears.
PHASE_1_FORBIDDEN_SURFACE_TOKENS: tuple[str, ...] = (
    "candidate",
    "ingestion",
    "ingest",
    "semantic-finding",
    "confirmed-finding",
    "review-queue",
)

# --------------------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------------------


def _incoming_edges(state: str) -> list[CandidateTransition]:
    return [edge for edge in CANDIDATE_TRANSITIONS if edge.to_state == state]


def _reachable_states(start: str) -> set[str]:
    seen = {start}
    queue: deque[str] = deque([start])
    while queue:
        current = queue.popleft()
        for edge in CANDIDATE_TRANSITIONS:
            if edge.from_state == current and edge.to_state not in seen:
                seen.add(edge.to_state)
                queue.append(edge.to_state)
    return seen


def validate_contract() -> list[str]:
    """Return the list of contract violations (empty means the contract is coherent)."""

    problems: list[str] = []

    # §2: the chain is ordered, ids are unique, and exactly one layer may write.
    positions = [layer.position for layer in INGESTION_LAYERS]
    if positions != sorted(positions) or len(set(positions)) != len(positions):
        problems.append("ingestion layer positions must be unique and increasing")
    id_fields = [layer.immutable_id_field for layer in INGESTION_LAYERS]
    if len(set(id_fields)) != len(id_fields):
        problems.append("each ingestion layer must contribute a distinct immutable id field")
    writers = [
        layer.key for layer in INGESTION_LAYERS if layer.write_authority == "engineering_model"
    ]
    if writers != [SOLE_WRITE_LAYER]:
        problems.append(f"exactly one layer may write the engineering model, found {writers}")
    if SOLE_WRITE_LAYER not in {layer.key for layer in INGESTION_LAYERS}:
        problems.append(f"declared sole write layer {SOLE_WRITE_LAYER!r} is not in the chain")

    # §4: no judgement source may hold apply authority in v1.
    for producer in JUDGMENT_PRODUCERS:
        if producer.may_grant_apply_authority:
            problems.append(f"producer {producer.key!r} must not hold apply authority")
        if not producer.requires_human_review:
            problems.append(f"producer {producer.key!r} must require human review in v1")
        if producer.auto_accept_classes:
            problems.append(
                f"producer {producer.key!r} must not auto-accept anything while the whitelist is empty"
            )

    # §5: confidence is not an authority input.
    for name in AUTHORITY_DECISION_INPUTS:
        if "confidence" in name or "score" in name:
            problems.append(f"authority input {name!r} looks like a confidence, not a permission")

    # §6: state machine guarantees.
    known = set(CANDIDATE_STATES)
    for edge in CANDIDATE_TRANSITIONS:
        if edge.from_state not in known or edge.to_state not in known:
            problems.append(
                f"transition {edge.from_state}->{edge.to_state} names an undeclared state"
            )
    declared_pairs = {(edge.from_state, edge.to_state) for edge in CANDIDATE_TRANSITIONS}
    for pair in FORBIDDEN_TRANSITIONS:
        if pair in declared_pairs:
            problems.append(
                f"forbidden transition {pair[0]}->{pair[1]} is present in the state machine"
            )
    if ("proposed", "applied") not in FORBIDDEN_TRANSITIONS:
        problems.append("proposed->applied must be declared forbidden")
    into_applied = _incoming_edges("applied")
    if [edge.from_state for edge in into_applied] != ["confirmed"]:
        problems.append(
            "the only incoming edge to 'applied' must be from 'confirmed', found "
            f"{[edge.from_state for edge in into_applied]}"
        )
    for edge in into_applied:
        missing = [
            requirement
            for requirement in (
                "compiled_patch",
                "policy_verdict",
                "conflict_check",
                "apply_v2_validation",
            )
            if requirement not in edge.requires
        ]
        if missing:
            problems.append(f"'applied' edge is missing gate evidence: {missing}")
    if not AUTO_ACCEPT_WHITELIST:
        for edge in _incoming_edges("confirmed"):
            if (
                REVIEWER_ACTION_EVIDENCE not in edge.requires
                or "review_decision" not in edge.requires
            ):
                problems.append(
                    f"with an empty auto-accept whitelist, {edge.from_state}->confirmed must record a "
                    "reviewer action and a review decision"
                )
    if UNDO_IS_A_STATE_ROLLBACK:
        problems.append("undo must not be modelled as a state rollback")
    reachable = _reachable_states("proposed")
    if reachable != known:
        problems.append(f"states unreachable from 'proposed': {sorted(known - reachable)}")

    # §3: the candidate schema keeps its own identity and stays free of write operations.
    field_names = {field.name for field in SEMANTIC_CANDIDATE_FIELDS}
    for required in (
        "candidate_id",
        "candidate_type",
        "proposed_semantics",
        "confidence",
        "review_status",
    ):
        if required not in field_names:
            problems.append(f"candidate schema is missing {required!r}")
    if "operations" in field_names or "op" in field_names:
        problems.append("candidate schema must not carry patch operations")
    if not UNCONFIRMABLE_CANDIDATE_TYPES:
        problems.append("unconfirmable candidate types must be declared, not left implicit")

    # §7: destructive intents are never allowed in v1, and every row states why.
    for row in WRITE_POLICY_V1:
        if row.disposition not in (
            "allowed_after_confirmation",
            "conflict_requires_human_resolution",
            "forbidden",
        ):
            problems.append(f"policy row {row.intent!r} has an unknown disposition")
        if not row.reason.strip():
            problems.append(f"policy row {row.intent!r} must state a reason")
    destructive = {
        row.intent: row.disposition
        for row in WRITE_POLICY_V1
        if row.intent in {"delete_existing_engineering_object", "replace_topology"}
    }
    if set(destructive) != {"delete_existing_engineering_object", "replace_topology"}:
        problems.append("the v1 policy must cover both destructive intents")
    for intent, disposition in destructive.items():
        if disposition != "forbidden":
            problems.append(f"destructive intent {intent!r} must be forbidden in v1")

    # §10: seed material is not truth.
    if SEED_MATERIAL_COUNTS_AS_EXPECTED_TRUTH:
        problems.append("seed material must not count as expected truth")
    for path in SEED_MATERIAL_PATHS:
        if GOLD_CORPUS_PATH.startswith(path):
            problems.append(f"gold corpus path must not live under seed material {path!r}")

    # §11: replay is defined against frozen inputs, and it is not undo.
    if not REPLAY_MUST_BE_BIT_IDENTICAL:
        problems.append("replay must require an identical result, otherwise it proves nothing")
    if UNDO_MECHANISM not in {"reverse_governed_transaction_through_apply_v2"}:
        problems.append("undo must run through the same governed write path as everything else")

    return problems


def contract_document() -> dict[str, object]:
    """The contract as a plain document, so evidence can quote it instead of paraphrasing."""

    return {
        "contract": M6_CONTRACT_VERSION,
        "layers": [asdict(layer) for layer in INGESTION_LAYERS],
        "sole_write_layer": SOLE_WRITE_LAYER,
        "candidate_fields": [asdict(field) for field in SEMANTIC_CANDIDATE_FIELDS],
        "unconfirmable_candidate_types": list(UNCONFIRMABLE_CANDIDATE_TYPES),
        "producers": [asdict(producer) for producer in JUDGMENT_PRODUCERS],
        "auto_accept_whitelist": list(AUTO_ACCEPT_WHITELIST),
        "authority_decision_inputs": list(AUTHORITY_DECISION_INPUTS),
        "candidate_states": list(CANDIDATE_STATES),
        "candidate_transitions": [asdict(edge) for edge in CANDIDATE_TRANSITIONS],
        "forbidden_transitions": [list(pair) for pair in FORBIDDEN_TRANSITIONS],
        "write_policy_v1": [asdict(row) for row in WRITE_POLICY_V1],
        "gold_corpus_dimensions": list(GOLD_CORPUS_DIMENSIONS),
        "gold_item_required_fields": list(GOLD_ITEM_REQUIRED_FIELDS),
        "phase_1_forbidden": list(PHASE_1_FORBIDDEN),
        "phase_1_forbidden_surface_tokens": list(PHASE_1_FORBIDDEN_SURFACE_TOKENS),
    }


def main() -> int:
    problems = validate_contract()
    if problems:
        for problem in problems:
            print(f"FAIL {problem}")
        return 1
    print(f"PASS {M6_CONTRACT_VERSION}")
    print(json.dumps(contract_document(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - developer entry point
    raise SystemExit(main())
