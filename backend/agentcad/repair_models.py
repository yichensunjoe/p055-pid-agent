"""The canonical self-repair contract (Charter §50 M5, remote baseline 2026-09-20 §A/§N).

M4 gave the project one way of saying *what is wrong with a drawing*. M5 needs one way of
saying *what an agent did about it*, and the remote baseline is explicit that this is a
machine contract rather than a report format: a reviewer must be able to recompute every
number from the record, and a case that cannot be recomputed is not evidence.

Three decisions in here are load-bearing:

* **The success oracle is a machine, not a sentence.** ``RepairRunResult.status`` may only
  be ``repaired`` when :mod:`agentcad.repair_oracle` proved it against the same canonical
  validator M4 published — target gone, rule bundle unchanged, required validators ran, no
  unregistered findings, no collateral regression, locality respected, one governed write,
  and a post-apply re-validation. A model that says "fixed" is not evidence.
* **Failed candidates leave no trace in the drawing.** Attempts live in the record, not in
  the document: every candidate is evaluated on a shadow copy, so ``base_revision`` still
  describes the drawing until the one apply that passed the oracle.
* **Volatile facts are kept out of the hash, not out of the record.** Token counts and
  timings must be reported, but a hash that moves when a machine is slow is not evidence
  of anything, so :data:`REPAIR_VOLATILE_FIELDS` are excluded from the digest while still
  being present in the payload. This is the same rule M4 applied to ``evaluated_at``: bind
  the inputs that decide the result, publish the rest.

The vocabulary is deliberately narrow. There is no ``approved``, ``signed`` or ``released``
status: automated self-repair can produce a candidate drawing and evidence, never a
formal release state (Charter §11, §44-10, remote baseline §L).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .validation_models import (
    ContractModel,
    ValidationSeverity,
    canonical_digest,
    canonical_json,
    canonical_payload,
)

REPAIR_REQUEST_SCHEMA = "pid-agent.repair-request"
REPAIR_RESULT_SCHEMA = "pid-agent.repair-run-result"

#: Why a run did not end in a repaired drawing. These are the stable machine codes the
#: benchmark counts, the surfaces publish and the safety suite asserts; human wording may
#: improve but a code's meaning may not change without a documented migration.
RepairFailureCode = Literal[
    "target_not_resolved",
    "budget_exhausted",
    "malformed_plan",
    "compile_failed",
    "validation_failed",
    "collateral_regression",
    "locality_violation",
    "protected_region_changed",
    "touched_budget_exceeded",
    "deletion_not_permitted",
    "context_budget_exceeded",
    "token_budget_exceeded",
    "rule_bundle_changed",
    "unregistered_finding",
    "required_validator_skipped",
    "stale_evidence",
    "revision_conflict",
    "apply_failed",
    "planner_declined",
    "planner_unavailable",
    "policy_violation",
    "undo_proof_failed",
    "post_apply_validation_failed",
    "internal_error",
]

#: ``repaired`` is the only status that means the drawing now satisfies the oracle.
#: ``human_required`` / ``not_repairable`` are *correct* outcomes for the safety suite:
#: refusing a repair there is the success condition, not a failure (baseline §D).
RepairStatus = Literal[
    "repaired",
    "failed",
    "human_required",
    "not_repairable",
    "stale_evidence",
    "policy_violation",
]

#: Attempts belong to the record, not to the drawing: the runner may try five candidates and
#: the document must still be untouched until one of them passes the oracle.
MAX_REPAIR_ATTEMPTS = 5

#: Fields excluded from ``repair_hash`` because they describe the machine rather than the
#: decision. Everything else in this module is hashed, including failure codes.
#:
#: ``document_id``, ``audit_record_id``, ``transaction_hash`` and the validation hashes are in
#: here for one reason: they are bound to *one machine's copy* of a case. Two runs of the same
#: benchmark case produce two documents with two ids, and a digest that moved with the id would
#: make "the same case behaves the same way" unassertable. What a record claims — the target,
#: the outcome, the failure code, the attempt count, the protected-region verdict — all stays in
#: the hash.
REPAIR_VOLATILE_FIELDS: frozenset[str] = frozenset(
    {
        "repair_hash",
        "attempt_metrics",
        "timings",
        # Cost and wall-clock describe the machine on the day, not the repair. They stay in the
        # published payload — a reviewer needs them — and stay out of the digest, because a
        # digest that moved with the wall clock could not answer "does this case behave the
        # same way on two runs".
        "timings_ms",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "token_usage_estimated",
        "planner",
        "document_id",
        "audit_record_id",
        "transaction_hash",
        "pre_validation_hash",
        "post_validation_hash",
        "shadow_validation_hash",
        "base_content_hash",
        "attempt_shadow_validation_hashes",
        "wall_clock_ms",
    }
)


class RepairContractModel(ContractModel):
    """Frozen, forbidding: a repair record is written once and then read by others."""


class RepairFindingRef(RepairContractModel):
    """One canonical M4 issue, reduced to the identity a repair chooses to act on.

    The repair never picks a target by prose. It binds the same tuple the harness uses to
    bind rule identity (``validator_id`` + ``code``) plus the locators, so "the agent
    repaired *that* finding" is checkable after the fact even if the wording improved.
    """

    code: str = Field(min_length=1)
    validator_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    severity: ValidationSeverity
    object_ids: list[str] = Field(default_factory=list)
    element_ids: list[str] = Field(default_factory=list)
    message: str = ""
    #: The finding's own evidence (``port_id``, counts, thresholds …). A repair is a function
    #: of the finding, so the evidence has to travel with it; leaving it behind forces the
    #: planner to re-derive facts the validator already computed.
    details: dict[str, Any] = Field(default_factory=dict)
    #: Whether the target is currently waived. A waived target is not a repair target.
    waiver_status: str = "not_waived"

    def identity(self) -> tuple[Any, ...]:
        return (
            self.validator_id,
            self.code,
            tuple(self.object_ids),
            tuple(self.element_ids),
        )


class RepairScope(RepairContractModel):
    """The frozen set of drawing elements a repair is allowed to touch (baseline §C1).

    ``hop`` is recorded because "how far may the agent reach" is a policy decision, not an
    implementation detail: 2-hop access must be declared in the case spec *before* planning
    and can never be widened by the planner mid-run.
    """

    hop: Literal[1, 2] = 1
    allowed_element_ids: list[str] = Field(default_factory=list)
    allowed_object_ids: list[str] = Field(default_factory=list)
    max_touched_existing_ids: int = Field(default=16, ge=0)
    declared_by: str = "derived"
    #: Whether the case permits the repair to *add* elements, and how many (baseline §B5's
    #: "permitted create/delete policy"). Replacing deleted equipment means creating an
    #: element, so this is the one policy switch F3 needs; it is frozen before planning and
    #: defaulted to "no" so no other family silently acquires the right to add equipment.
    permits_creation: bool = False
    max_created_ids: int = Field(default=0, ge=0)
    #: Whether the case permits the repair to *remove* an existing element, and how many. Off by
    #: default because deleting the offending object is the cheapest way to make a finding
    #: disappear while making the drawing worse, which is the pseudo-repair §C4 names first.
    permits_deletion: bool = False
    max_deleted_ids: int = Field(default=0, ge=0)

    def allows(self, element_id: str) -> bool:
        return element_id in self.allowed_element_ids

    def created_ids_allowed(self, created_ids: Iterable[str]) -> bool:
        ids = set(created_ids)
        return bool(ids) and self.permits_creation and len(ids) <= self.max_created_ids

    def deleted_ids_allowed(self, deleted_ids: Iterable[str]) -> bool:
        ids = set(deleted_ids)
        return bool(ids) and self.permits_deletion and len(ids) <= self.max_deleted_ids

    @field_validator("allowed_element_ids", "allowed_object_ids")
    @classmethod
    def _sorted_unique(cls, value: list[str]) -> list[str]:
        return sorted(set(value))


class RepairBudgets(RepairContractModel):
    """The budgets in baseline §F, as data rather than as prose in a docstring."""

    max_attempts: int = Field(default=MAX_REPAIR_ATTEMPTS, ge=1, le=MAX_REPAIR_ATTEMPTS)
    max_context_bytes: int = Field(default=256 * 1024, gt=0)
    max_context_elements: int = Field(default=256, gt=0)
    max_input_tokens: int = Field(default=64_000, gt=0)
    max_output_tokens: int = Field(default=16_000, gt=0)


class RepairRequest(RepairContractModel):
    """Everything the planner is allowed to know, and everything a reviewer needs.

    The binding mirrors ``ValidationResult``: document, revision, content hash, profile,
    rule bundle, symbol registry and the validation hash the target came from. A planner
    that acts on a stale binding is refused before it plans (baseline §A7), which is also
    how the safety suite's "stale result hash" case is expressed.
    """

    schema_name: Literal["pid-agent.repair-request"] = Field(
        default=REPAIR_REQUEST_SCHEMA, alias="schema"
    )
    version: Literal[1] = 1
    document_id: str = Field(min_length=1)
    revision: int = Field(ge=0)
    content_hash: str = ""
    project_id: str = ""
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    rule_bundle_fingerprint: str = ""
    symbol_registry_fingerprint: str = ""
    engine_version: str = ""
    validation_hash: str = ""
    evaluated_at: datetime
    target: RepairFindingRef
    scope: RepairScope = Field(default_factory=RepairScope)
    budgets: RepairBudgets = Field(default_factory=RepairBudgets)

    @field_validator("evaluated_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("evaluated_at must be timezone-aware")
        return value

    def request_hash(self) -> str:
        return canonical_digest(self)


class RepairPlanRef(RepairContractModel):
    """Which entity produced an attempt's plan, and what it was.

    ``source`` distinguishes the deterministic oracle-blind planner from a real model. The
    baseline requires the two to share one contract, so the difference is data here rather
    than a second code path.
    """

    attempt: int = Field(ge=1)
    source: Literal["deterministic", "model", "test-double"] = "deterministic"
    plan_hash: str = ""
    operation_count: int = 0
    planner_id: str = ""
    assessment_codes: list[str] = Field(default_factory=list)


class RepairAttemptRecord(RepairContractModel):
    """One planning attempt, including the ones that were thrown away.

    Rejected attempts are the interesting ones: they are the evidence that the runner
    converged rather than got lucky, and their ``failure_code`` is what makes S@1..S@5 a
    curve over real attempts instead of a scoreboard of retries.
    """

    attempt: int = Field(ge=1)
    planner: RepairPlanRef
    compiled: bool = False
    compiled_operation_count: int = 0
    compile_issue_codes: list[str] = Field(default_factory=list)
    shadow_content_hash: str = ""
    shadow_validation_hash: str = ""
    touched_existing_ids: list[str] = Field(default_factory=list)
    target_resolved: bool = False
    collateral_regressions: list[str] = Field(default_factory=list)
    locality_violation_ids: list[str] = Field(default_factory=list)
    protected_region_changed: bool = False
    outcome: Literal["selected", "rejected"] = "rejected"
    failure_code: RepairFailureCode | None = None
    notes: list[str] = Field(default_factory=list)


class RepairAttemptMetrics(RepairContractModel):
    """Volatile per-attempt facts: reported, never hashed (see module docstring)."""

    attempt: int = Field(ge=1)
    input_tokens: int = 0
    output_tokens: int = 0
    context_bytes: int = 0
    context_elements: int = 0
    latency_ms: int = 0


class RepairTimings(RepairContractModel):
    """Where the time went, phase by phase (baseline §F).

    ``plan_ms`` is cumulative across attempts because the question it answers is "how long did
    planning cost this case"; the rest describe the attempt that was actually evaluated.
    """

    plan_ms: int = 0
    compile_ms: int = 0
    shadow_validate_ms: int = 0
    apply_ms: int = 0
    post_validate_ms: int = 0


class RepairPlannerIdentity(RepairContractModel):
    """Who planned, exactly. Recorded for real models (baseline §A4), empty for the
    deterministic planner, whose identity is its own version string.

    Secrets are never recorded here: only the provider class, the exact model id and the
    parameters that change behaviour, plus fingerprints of the prompt and schema.
    """

    planner_id: str = ""
    planner_version: str = ""
    provider_class: str = ""
    base_url_class: str = ""
    model: str = ""
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    timeout_seconds: float | None = None
    prompt_fingerprint: str = ""
    schema_fingerprint: str = ""
    #: Whether the token figures came from the provider or from the project's estimator. A
    #: number whose provenance is unknown is not a budget report (baseline §F).
    token_usage_estimated: bool = False

    def digest(self) -> str:
        """Stable identity string used for a run's ``planner_fingerprint``."""

        return canonical_digest(self)


class RepairProtectedHashes(RepairContractModel):
    """What must not move while a repair happens (baseline §C3).

    Two projections are kept apart on purpose: a repair that only touches labels must not
    change engineering semantics, and a repair that changes connectivity must not silently
    rewrite unrelated geometry. One combined hash would hide which of the two happened.
    """

    engineering_projection: str = ""
    drawing_projection: str = ""
    rule_bundle_fingerprint: str = ""
    scope_fingerprint: str = ""


class RepairApplyProof(RepairContractModel):
    """The governed-write evidence for the one apply that was allowed to happen."""

    applied: bool = False
    base_revision: int = Field(default=0, ge=0)
    result_revision: int = Field(default=0, ge=0)
    transaction_hash: str = ""
    audit_record_id: str = ""
    logical_change_count: int = 0
    history_entries_added: int = 0
    undo_restored_base: bool = False
    redo_restored_result: bool = False
    post_validation_hash: str = ""
    post_protected: RepairProtectedHashes = Field(default_factory=RepairProtectedHashes)


class RepairRunResult(RepairContractModel):
    """The one record a repair produces, whether it worked or not."""

    schema_name: Literal["pid-agent.repair-run-result"] = Field(
        default=REPAIR_RESULT_SCHEMA, alias="schema"
    )
    version: Literal[1] = 1
    request_hash: str = ""
    document_id: str = Field(min_length=1)
    project_id: str = ""
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    engine_version: str = ""
    validation_hash: str = ""
    evaluated_at: datetime
    target: RepairFindingRef
    scope: RepairScope = Field(default_factory=RepairScope)
    budgets: RepairBudgets = Field(default_factory=RepairBudgets)
    planner: RepairPlannerIdentity = Field(default_factory=RepairPlannerIdentity)
    protected_pre: RepairProtectedHashes = Field(default_factory=RepairProtectedHashes)
    attempts: list[RepairAttemptRecord] = Field(default_factory=list)
    attempt_metrics: list[RepairAttemptMetrics] = Field(default_factory=list)
    timings: RepairTimings = Field(default_factory=RepairTimings)
    selected_attempt: int | None = None
    selected_plan_hash: str = ""
    applied: RepairApplyProof = Field(default_factory=RepairApplyProof)
    status: RepairStatus = "failed"
    failure_code: RepairFailureCode | None = None
    reasons: list[str] = Field(default_factory=list)
    #: Hash over the canonical public form minus the volatile fields. A reviewer recomputes
    #: this from the payload they were given; a mismatch means the record was edited.
    repair_hash: str = ""

    @property
    def repaired(self) -> bool:
        return self.status == "repaired"

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def successful_attempt(self) -> int | None:
        """The attempt number that satisfied the oracle, i.e. the S@N value for this case."""

        return self.selected_attempt

    def failures(self) -> list[RepairFailureCode]:
        return [a.failure_code for a in self.attempts if a.failure_code is not None]


def repair_payload(value: RepairContractModel, *, exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Canonical public form of a repair record, minus volatile fields."""

    return canonical_payload(value, exclude=REPAIR_VOLATILE_FIELDS | exclude)


def repair_json(value: RepairContractModel, *, exclude: frozenset[str] = frozenset()) -> str:
    """Canonical JSON text of a repair record, minus volatile fields."""

    return canonical_json(value, exclude=REPAIR_VOLATILE_FIELDS | exclude)


def repair_digest(value: RepairContractModel, *, exclude: frozenset[str] = frozenset()) -> str:
    """SHA-256 binding exactly what a surface would show for a repair record.

    Note what this can and cannot promise: ``canonical_digest`` hands ``exclude`` to pydantic, and
    pydantic's ``exclude`` drops *top-level* keys only. For one repair record that is the rule that
    was audited; for a benchmark *result*, whose volatile facts live inside ``cases[i]``, it is not
    enough — see :func:`repair_semantic_digest`.
    """

    return canonical_digest(value, exclude=REPAIR_VOLATILE_FIELDS | exclude)


#: Version of the repair *semantic* hash contract. It is part of the hash: a published
#: ``benchmark_semantic_hash`` is only comparable with another one computed under the same version,
#: so bump this when either the field list or the canonicalisation rule changes.
REPAIR_SEMANTIC_HASH_VERSION = "1"

#: What the semantic hash drops, by field name, **at every depth**: the volatile facts above, the
#: hashes themselves, and ``generator_fingerprint``.
#:
#: A result hash is a statement about the run, not about the repair, so the legacy hash is
#: runtime-bound by construction and would carry the very instability the semantic hash exists to
#: remove. ``generator_fingerprint`` is the other half: it digests CPython bytecode, so the same
#: candidate repaired identically on two interpreters would look like two different results. The
#: remote drew that line for the corpus identity (stable ``coverage_corpus_digest`` vs runtime
#: fingerprint) and measured it -- the frozen acceptance payload from CPython 3.11 and the one from
#: 3.12 differ in exactly this field plus the volatile ones, and in nothing else.
#: What stays in, deliberately: ``candidate_sha``, ``spec_version``/``spec_fingerprint``,
#: ``oracle_version``, every case's outcome and every gate.
#: ``semantic_hash_version`` is published but not hashed: it is the *name* of the contract, and a
#: rule change already moves the hash by moving the payload. Hashing it would break comparability
#: exactly once — between a payload produced before the field existed and one produced after — which
#: is the one comparison a reviewer is most likely to make. Verification checks the declared version
#: separately instead.
REPAIR_SEMANTIC_EXCLUDED_FIELDS: frozenset[str] = REPAIR_VOLATILE_FIELDS | frozenset(
    {
        "benchmark_result_hash",
        "benchmark_semantic_hash",
        "generator_fingerprint",
        "semantic_hash_version",
        # Corpus coordinates are *derived metadata*: they restate which frozen case set the run
        # drew from, and the run already says that through its own records. A corpus that really
        # changed changes those records (a case's operator, its target code, its outcome), so the
        # hash moves on its own; hashing a restatement as well would mean a payload published
        # before the field existed could not be compared with the same result after it did — which
        # is the one comparison this hash exists to make possible.
        "corpus_version",
        "core_corpus_digest",
    }
)

#: Fields that exist only for the semantic hash or as derived metadata. ``repair_digest`` keeps them
#: out so the legacy value of a record is the same number it was before they existed.
REPAIR_LEGACY_HASH_EXCLUDES: frozenset[str] = frozenset(
    {
        "benchmark_semantic_hash",
        "semantic_hash_version",
        "corpus_version",
        "core_corpus_digest",
    }
)


def canonical_repair_result_payload(value: Any) -> Any:
    """Recursive canonical form of a repair record: volatile facts dropped at every depth.

    A benchmark result is not one record but a record per case, and the volatile facts sit inside
    them (``cases[i].latency_ms``, ``cases[i].document_id``, the runtime validation hashes). Measured
    twice, the same candidate therefore hashed to two different numbers while every semantic field
    matched — a hash that cannot answer "is this the same result" is not doing the one job a result
    hash has.

    Models are dumped through the canonical public form first (public aliases, JSON mode), so a
    digest over a model and a digest over the payload a reviewer was handed are the same value.
    Reachable *nested* containers are all that is traversed: this is not a "delete any key that
    happens to share a name" pass over arbitrary objects, it is the same field contract
    (:data:`REPAIR_SEMANTIC_EXCLUDED_FIELDS`) applied at every record depth.
    """

    if isinstance(value, BaseModel):
        value = canonical_payload(value)
    if isinstance(value, dict):
        return {
            key: canonical_repair_result_payload(item)
            for key, item in value.items()
            if key not in REPAIR_SEMANTIC_EXCLUDED_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [canonical_repair_result_payload(item) for item in value]
    return value


def repair_semantic_digest(value: Any) -> str:
    """The result identity: same candidate, same corpus and same result, on any machine.

    Accepts a model or the plain payload, so a reviewer recomputes it from the JSON they were
    handed rather than from this process's memory.
    """

    text = json.dumps(
        canonical_repair_result_payload(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "MAX_REPAIR_ATTEMPTS",
    "REPAIR_LEGACY_HASH_EXCLUDES",
    "REPAIR_REQUEST_SCHEMA",
    "REPAIR_RESULT_SCHEMA",
    "REPAIR_SEMANTIC_EXCLUDED_FIELDS",
    "REPAIR_SEMANTIC_HASH_VERSION",
    "REPAIR_VOLATILE_FIELDS",
    "canonical_repair_result_payload",
    "repair_semantic_digest",
    "RepairApplyProof",
    "RepairAttemptMetrics",
    "RepairAttemptRecord",
    "RepairBudgets",
    "RepairContractModel",
    "RepairFailureCode",
    "RepairFindingRef",
    "RepairPlanRef",
    "RepairPlannerIdentity",
    "RepairProtectedHashes",
    "RepairRequest",
    "RepairRunResult",
    "RepairScope",
    "RepairStatus",
    "RepairTimings",
    "repair_digest",
    "repair_json",
    "repair_payload",
]
