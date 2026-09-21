"""The repair loop: validator → plan → shadow → oracle → one governed write (baseline §A1).

This module is the only place in M5 that is allowed to write to a drawing, and it writes at
most once per run. Everything else happens on a shadow copy:

.. code-block:: text

    canonical M4 result  →  freeze scope + protected projections
    → planner (oracle-blind)  →  semantic compile
    → shadow apply  →  shadow canonical validation  →  success oracle
    → (replan up to the attempt budget)
    → one governed apply  →  post-apply validation  →  undo/redo proof

Three properties are structural rather than promised:

* **A rejected candidate cannot leave a trace.** Shadow work is a deep copy plus
  ``DocumentService._apply_operation``; the store is never touched until the oracle accepts.
  The benchmark asserts this by comparing the stored revision before and after a failing run.
* **The repair cannot become an approval.** There is no waiver, profile, release-policy or
  readiness code path here. A run produces a drawing and evidence; ``ReleaseReadiness`` and the
  human Approval Gate stay where M3/M4 put them (baseline §L).
* **The time contract stays with M4.** ``RepairRequest.evaluated_at`` is bound once and every
  canonical validation in the run is executed at exactly that instant, so a repair can be
  replayed and produces the same hashes.
"""

from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from .agent_semantic import SemanticTransactionCompiler
from .audit_models import AuditContext
from .drafting_geometry import drafting_content_hash
from .llm import LLMPlanValidationError, LLMResponseError, PlannerError
from .models import Document, TransactionRequest
from .repair_models import (
    MAX_REPAIR_ATTEMPTS,
    RepairApplyProof,
    RepairAttemptMetrics,
    RepairAttemptRecord,
    RepairBudgets,
    RepairFailureCode,
    RepairFindingRef,
    RepairPlannerIdentity,
    RepairPlanRef,
    RepairRequest,
    RepairRunResult,
    RepairScope,
    RepairStatus,
    RepairTimings,
    repair_digest,
)
from .repair_oracle import evaluate_candidate, find_target
from .repair_planner import (
    PLANNER_VERSION,
    DeterministicRepairPlanner,
    RepairDeclined,
    RepairPlanner,
    build_repair_context,
)
from .repair_scope import (
    TOUCHED_BUDGET_BY_CLASS,
    changed_existing_ids,
    created_ids,
    deleted_ids,
    derive_scope,
    protected_hashes,
    touched_existing_ids,
)
from .service import EDITOR_LOCK_KEY, DocumentService, RevisionConflictError
from .validation_engine import run_validation
from .validation_models import ValidationIssue, ValidationResult
from .validation_profile import EffectiveProfile

#: The surface a repair writes through. Audited like every other governed write.
REPAIR_SURFACE_ACTOR = "agent-self-repair"
REPAIR_TOOL_NAME = "repair_drawing"


class RepairStaleEvidence(RuntimeError):
    """The request no longer describes the document (baseline §A7, safety case 8)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


#: The refusals that are answers about the *drawing's* rules rather than about the agent's
#: ability to converge. When every attempt lands in this set, the run is a policy refusal.
POLICY_REFUSAL_CODES: frozenset[str] = frozenset(
    {
        "policy_violation",
        "locality_violation",
        "protected_region_changed",
        "touched_budget_exceeded",
        "deletion_not_permitted",
        "unregistered_finding",
        "required_validator_skipped",
    }
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def locked_plan_targets(document: Document, operations: list[object]) -> list[str]:
    """Locked elements or layers a candidate plan would write, sorted.

    Locks are checked against the *plan*, not against the drawing as a whole: a repair that
    avoids locked content is perfectly legal, and one that touches it is not. ``editor_locked``
    is the same key the governed apply path honours, so the gate and the write path cannot
    disagree about what "locked" means.
    """

    existing = {element.id: element.model_dump(mode="json") for element in document.elements}
    locked_layers = {layer.id for layer in document.layers if layer.locked}
    blocked: set[str] = set()
    for operation in operations:
        payload = operation.model_dump(mode="json") if hasattr(operation, "model_dump") else {}
        patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else {}
        added = payload.get("element") if isinstance(payload.get("element"), dict) else {}
        element_id = str(payload.get("element_id") or added.get("id") or "")
        current = existing.get(element_id) or {}
        metadata = {**(current.get("metadata") or {}), **(patch.get("metadata") or {})}
        if current.get("metadata", {}).get(EDITOR_LOCK_KEY) is True or (
            element_id in existing and metadata.get(EDITOR_LOCK_KEY) is True and patch.get("metadata")
        ):
            blocked.add(element_id)
            continue
        layer_id = str(patch.get("layer_id") or current.get("layer_id") or added.get("layer_id") or "")
        if layer_id in locked_layers:
            blocked.add(f"{element_id} (layer {layer_id})")
            continue
        target_layer = payload.get("layer_id") or (payload.get("layer") or {}).get("id")
        if str(target_layer or "") in locked_layers:
            blocked.add(f"layer {target_layer}")
    return sorted(blocked)


def _operations_digest(operations: list[object]) -> str:
    payload = [
        operation.model_dump(mode="json")  # type: ignore[attr-defined]
        for operation in operations
    ]
    return _sha(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def build_repair_request(
    document: Document,
    result: ValidationResult,
    issue: ValidationIssue,
    *,
    registry,
    profile: EffectiveProfile,
    project_id: str = "",
    hop: int = 1,
    max_touched_existing_ids: int | None = None,
    budgets: RepairBudgets | None = None,
    declared_by: str = "derived",
    permits_creation: bool = False,
    max_created_ids: int = 0,
    permits_deletion: bool = False,
    max_deleted_ids: int = 0,
) -> RepairRequest:
    """Bind one canonical finding to the exact document state it was found in.

    Every field here answers a question the run will be judged on: *which* revision, under
    *which* profile and rule bundle, at *which* evaluation instant, with *which* allowed scope.
    Deriving the request from the M4 result rather than from a caller's prose is what makes the
    "stale evidence" safety case expressible at all.
    """

    target = RepairFindingRef(
        code=issue.code,
        validator_id=issue.validator_id,
        rule_id=issue.rule_id,
        severity=issue.severity,
        object_ids=list(issue.object_ids),
        element_ids=list(issue.element_ids),
        message=issue.message,
        details=dict(issue.details),
        waiver_status=issue.waiver_status,
    )
    if max_touched_existing_ids is None:
        max_touched_existing_ids = TOUCHED_BUDGET_BY_CLASS["multi_connector"]
    scope = derive_scope(
        document,
        registry,
        target,
        hop=hop,
        max_touched_existing_ids=max_touched_existing_ids,
        declared_by=declared_by,
        permits_creation=permits_creation,
        max_created_ids=max_created_ids,
        permits_deletion=permits_deletion,
        max_deleted_ids=max_deleted_ids,
    )
    return RepairRequest(
        document_id=document.id,
        revision=document.revision,
        content_hash=drafting_content_hash(document),
        project_id=project_id,
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        rule_bundle_fingerprint=profile.fingerprint,
        symbol_registry_fingerprint=registry.fingerprint(),
        engine_version=result.engine_version,
        validation_hash=result.result_hash,
        evaluated_at=result.evaluated_at,
        target=target,
        scope=scope,
        budgets=budgets or RepairBudgets(),
    )


class RepairOrchestrator:
    """Runs one repair request to a verdict, writing at most once."""

    def __init__(
        self,
        service: DocumentService,
        profile: EffectiveProfile,
        *,
        planner: RepairPlanner | None = None,
        apply_enabled: bool = True,
        prove_undo: bool = True,
    ) -> None:
        self.service = service
        self.profile = profile
        self.registry = service.symbols
        self.planner = planner or DeterministicRepairPlanner()
        self.apply_enabled = apply_enabled
        self.prove_undo = prove_undo
        #: Timings of the attempt currently being evaluated; published with the run so a slow
        #: model names the phase it was slow in rather than "the repair took 40 seconds".
        self._last_timings = RepairTimings()

    # -- public ---------------------------------------------------------- #

    def run(
        self,
        request: RepairRequest,
        *,
        target_issue: ValidationIssue | None = None,
        postconditions: dict | None = None,
    ) -> RepairRunResult:
        base_document = self.service.get_document(request.document_id)
        self._assert_fresh(request, base_document)
        base_result = run_validation(
            base_document,
            self.registry,
            self.profile,
            project_id=request.project_id,
            service=self.service,
            now=request.evaluated_at,
        )
        if request.validation_hash and base_result.result_hash != request.validation_hash:
            raise RepairStaleEvidence(
                "validation_hash_mismatch",
                "the validation result hash no longer matches this request",
            )
        if not find_target(base_result, request.target):
            # Nothing to repair is not a repair; report it rather than inventing a finding.
            return self._result(
                request,
                status="failed",
                failure_code="target_not_resolved",
                reasons=["the target finding is not present in the current validation result"],
                attempts=[],
            )
        # The planner is shown the target's own findings, plus the whole canonical result as a
        # *class* view: when a device is removed, every line that pointed at it is broken, and a
        # plan that rebinds one of them has not repaired the drawing. The target binding itself
        # stays exactly what the request declared.
        findings = find_target(base_result, request.target)
        class_findings = list(base_result.issues)

        scope = self._effective_scope(request, base_document)
        protected_pre = protected_hashes(
            base_document,
            self.registry,
            scope,
            rule_bundle_fingerprint=base_result.rule_bundle_fingerprint,
        )
        attempts: list[RepairAttemptRecord] = []
        metrics: list[RepairAttemptMetrics] = []
        selected: tuple[RepairAttemptRecord, object] | None = None
        declared: RepairDeclined | None = None
        self._last_timings = RepairTimings()
        plan_total_ms = 0

        for attempt in range(1, request.budgets.max_attempts + 1):
            context = build_repair_context(
                request,
                base_document,
                self.registry,
                findings,
                class_findings=class_findings,
                prior_attempts=attempts,
                attempt=attempt,
            )
            within, budget_code = context.within_budget()
            if not within:
                attempts.append(
                    RepairAttemptRecord(
                        attempt=attempt,
                        planner=RepairPlanRef(
                            attempt=attempt,
                            source=self._source(),
                            planner_id=getattr(self.planner, "planner_id", "unknown"),
                        ),
                        outcome="rejected",
                        failure_code=budget_code,  # type: ignore[arg-type]
                        notes=["the localized repair context exceeded the published budget"],
                    )
                )
                break
            metrics.append(
                RepairAttemptMetrics(
                    attempt=attempt,
                    context_bytes=context.context_bytes(),
                    context_elements=context.context_elements(),
                )
            )
            attempt_started = time.perf_counter()
            try:
                draft = self.planner.plan(context)
            except RepairDeclined as declined:
                declared = declined
                attempts.append(
                    RepairAttemptRecord(
                        attempt=attempt,
                        planner=RepairPlanRef(
                            attempt=attempt,
                            source=self._source(),
                            planner_id=getattr(self.planner, "planner_id", "unknown"),
                        ),
                        outcome="rejected",
                        failure_code="planner_declined",
                        notes=[f"{declined.kind}: {declined.reason}"],
                    )
                )
                break
            except PlannerError as exc:
                # A model that answers outside the schema is a replannable problem and the next
                # attempt is shown what was wrong; a provider that cannot be reached is not, so
                # the case stops there instead of spending the rest of its budget on a wall
                # (baseline §F: "timeout 后没有 usable plan = case failure").
                replannable = isinstance(exc, (LLMPlanValidationError, LLMResponseError))
                attempts.append(
                    RepairAttemptRecord(
                        attempt=attempt,
                        planner=RepairPlanRef(
                            attempt=attempt,
                            source=self._source(),
                            planner_id=getattr(self.planner, "planner_id", "unknown"),
                        ),
                        outcome="rejected",
                        failure_code="malformed_plan" if replannable else "planner_unavailable",
                        notes=[str(exc)],
                    )
                )
                # A model that answered with something unusable still spent tokens, and the
                # case budget is about what was *spent*, not about what was usable (§F).
                usage = getattr(self.planner, "last_usage", None) or {}
                metrics[-1] = metrics[-1].model_copy(
                    update={
                        "latency_ms": int((time.perf_counter() - attempt_started) * 1000),
                        "input_tokens": max(0, int(usage.get("input_tokens", 0))),
                        "output_tokens": max(0, int(usage.get("output_tokens", 0))),
                    }
                )
                if not replannable:
                    break
                continue
            metrics[-1] = metrics[-1].model_copy(
                update={
                    "latency_ms": int((time.perf_counter() - attempt_started) * 1000),
                    "input_tokens": max(0, int(draft.input_tokens)),
                    "output_tokens": max(0, int(draft.output_tokens)),
                }
            )
            spent_in = sum(item.input_tokens for item in metrics)
            spent_out = sum(item.output_tokens for item in metrics)
            if (
                spent_in > request.budgets.max_input_tokens
                or spent_out > request.budgets.max_output_tokens
            ):
                # The budget is cumulative for the case, and it is spent whether or not the
                # candidate was any good: stopping here is what keeps "five attempts" from
                # becoming "five attempts at any price" (baseline §F).
                attempts.append(
                    RepairAttemptRecord(
                        attempt=attempt,
                        planner=RepairPlanRef(
                            attempt=attempt,
                            source=self._source(),
                            planner_id=getattr(self.planner, "planner_id", "unknown"),
                            operation_count=len(draft.operations),
                        ),
                        outcome="rejected",
                        failure_code="token_budget_exceeded",
                        notes=[
                            f"the case has spent {spent_in} input / {spent_out} output tokens",
                            f"budget is {request.budgets.max_input_tokens} / "
                            f"{request.budgets.max_output_tokens}",
                        ],
                    )
                )
                break
            plan_total_ms += int((time.perf_counter() - attempt_started) * 1000)
            self._last_timings = self._last_timings.model_copy(update={"plan_ms": plan_total_ms})
            record = self._evaluate_attempt(
                request=request,
                scope=scope,
                base_document=base_document,
                base_result=base_result,
                protected_pre=protected_pre,
                attempt=attempt,
                draft=draft,
                postconditions=postconditions,
            )
            attempts.append(record)
            if record.outcome == "selected":
                selected = (record, draft)
                break

        timings = self._last_timings.model_copy(update={"plan_ms": plan_total_ms})
        if selected is None:
            status: RepairStatus = "failed"
            code: RepairFailureCode = "budget_exhausted"
            reasons = ["no candidate satisfied the repair oracle within the attempt budget"]
            if attempts and attempts[-1].failure_code in {
                "token_budget_exceeded",
                "context_budget_exceeded",
                "planner_unavailable",
            }:
                # The run stopped because a *stated* budget ran out, not because five plans
                # were tried and none converged. Recording the mechanism instead of the reason
                # would file a token overrun under "the agent could not fix it".
                code = attempts[-1].failure_code  # type: ignore[assignment]
                reasons = list(attempts[-1].notes)
            elif declared is not None:
                status = declared.kind
                code = "planner_declined"
                reasons = [declared.reason]
            elif attempts and all(
                record.failure_code in POLICY_REFUSAL_CODES for record in attempts
            ):
                # Every candidate was refused because the drawing forbids it — a locked element,
                # a scope the plan stepped outside, a finding nobody registered — not because the
                # agent failed to converge. Reporting the attempt budget instead would hide the
                # honest answer behind a mechanism, so the refusal is what gets recorded, with
                # the *specific* code kept as the reason.
                status = "policy_violation"
                code = attempts[0].failure_code or "policy_violation"
                reasons = sorted({note for record in attempts for note in record.notes})
            self._record_repair_evidence(
                request,
                event_type="repair.refused",
                status="rejected" if status == "human_required" else "failed",
                evidence={
                    "target_validation_hash": request.validation_hash,
                    "attempt_count": len(attempts),
                    "selected_plan_hash": "",
                    "scope_fingerprint": protected_pre.scope_fingerprint,
                    "final_validation_hash": "",
                    "failure_code": code,
                    "outcome": status,
                },
                error_code=code,
            )
            return self._result(
                request,
                status=status,
                failure_code=code,
                reasons=reasons,
                attempts=attempts,
                metrics=metrics,
                protected_pre=protected_pre,
                timings=timings,
            )

        record, draft = selected
        if not self.apply_enabled:
            return self._result(
                request,
                status="repaired",
                failure_code=None,
                reasons=["candidate accepted on the shadow document; apply is disabled"],
                attempts=attempts,
                metrics=metrics,
                protected_pre=protected_pre,
                timings=timings,
                selected=record,
                applied=RepairApplyProof(applied=False, base_revision=request.revision),
            )

        apply_started = time.perf_counter()
        proof, failure = self._apply_and_prove(
            request=request,
            draft=draft,
            scope=scope,
            base_document=base_document,
            protected_pre=protected_pre,
            attempts=attempts,
            selected_attempt=record.attempt,
        )
        self._record_repair_evidence(
            request,
            event_type="repair.completed" if failure is None else "repair.refused",
            status="applied" if failure is None else "failed",
            evidence={
                "target_validation_hash": request.validation_hash,
                "attempt_count": len(attempts),
                "selected_plan_hash": record.planner.plan_hash,
                "scope_fingerprint": protected_pre.scope_fingerprint,
                "final_validation_hash": proof.post_validation_hash,
                "result_revision": proof.result_revision,
                "repair_hash": proof.transaction_hash,
                "failure_code": failure or "",
            },
            error_code=failure or "",
            result_revision=proof.result_revision or None,
        )
        timings = self._last_timings.model_copy(
            update={"apply_ms": int((time.perf_counter() - apply_started) * 1000)}
        )
        if failure is not None:
            return self._result(
                request,
                status="failed",
                failure_code=failure,
                reasons=["the governed apply or its proof did not hold"],
                attempts=attempts,
                metrics=metrics,
                protected_pre=protected_pre,
                timings=timings,
                selected=record,
                applied=proof,
            )
        return self._result(
            request,
            status="repaired",
            failure_code=None,
            reasons=[],
            attempts=attempts,
            metrics=metrics,
            protected_pre=protected_pre,
            timings=timings,
            selected=record,
            applied=proof,
        )

    # -- steps ----------------------------------------------------------- #

    def _source(self) -> str:
        planner_id = getattr(self.planner, "planner_id", "")
        if "model" in planner_id or "llm" in planner_id:
            return "model"
        return "deterministic"

    def _assert_fresh(self, request: RepairRequest, document: Document) -> None:
        if document.revision != request.revision:
            raise RepairStaleEvidence(
                "revision_mismatch",
                f"request binds revision {request.revision}, the document is at {document.revision}",
            )
        if request.content_hash and drafting_content_hash(document) != request.content_hash:
            raise RepairStaleEvidence(
                "content_hash_mismatch", "the drawing content changed since the request was built"
            )
        if request.rule_bundle_fingerprint and self.profile.fingerprint != request.rule_bundle_fingerprint:
            raise RepairStaleEvidence(
                "rule_bundle_mismatch",
                "the effective rule bundle changed between planning and repair",
            )

    def _effective_scope(self, request: RepairRequest, document: Document) -> RepairScope:
        if request.scope.allowed_element_ids:
            return request.scope
        return derive_scope(
            document,
            self.registry,
            request.target,
            hop=request.scope.hop,
            max_touched_existing_ids=request.scope.max_touched_existing_ids or 16,
            declared_by=request.scope.declared_by,
        )

    def _shadow_document(
        self, base: Document, draft, attempt: int, request: RepairRequest
    ) -> Document:
        """Apply the compiled candidate to a copy, exactly as ``analyze_transaction`` does.

        Compilation is repeated here (once per attempt) because the shadow copy is not kept
        between attempts: a rejected candidate must not leak into the next try.
        """

        working = deepcopy(base)
        for operation in self._compiled_operations(base, draft, request):
            self.service._apply_operation(working, operation)
        working.revision = base.revision + 1
        return Document.model_validate(working.model_dump(mode="python"))

    def _compiled_operations(self, base: Document, draft, request: RepairRequest) -> list:
        transaction = draft.transaction(
            expected_revision=request.revision, label="self-repair attempt"
        )
        compiled = SemanticTransactionCompiler(self.service).compile(
            request.document_id, transaction
        )
        return list(compiled.transaction.operations) if compiled.transaction else []

    def _evaluate_attempt(
        self,
        *,
        request: RepairRequest,
        scope: RepairScope,
        base_document: Document,
        base_result: ValidationResult,
        protected_pre,
        attempt: int,
        draft,
        postconditions: dict | None,
    ) -> RepairAttemptRecord:
        planner_ref = RepairPlanRef(
            attempt=attempt,
            source=self._source(),
            planner_id=getattr(self.planner, "planner_id", "unknown"),
            operation_count=len(draft.operations),
        )
        evaluate_started = time.perf_counter()
        blocked = locked_plan_targets(base_document, draft.operations)
        if blocked:
            # A plan that writes protected content is refused *before* it is compiled: the locked
            # flag is the drawing's way of saying "a human decides this", and no amount of
            # retrying turns a locked element into a repairable one.
            return RepairAttemptRecord(
                attempt=attempt,
                planner=planner_ref,
                compiled=False,
                outcome="rejected",
                failure_code="policy_violation",
                notes=[f"the plan writes locked content: {', '.join(blocked)}"],
            )
        transaction = draft.transaction(
            expected_revision=request.revision, label="self-repair candidate"
        )
        compiled = SemanticTransactionCompiler(self.service).compile(
            request.document_id, transaction
        )
        if not compiled.assessment.valid or compiled.transaction is None:
            return RepairAttemptRecord(
                attempt=attempt,
                planner=planner_ref,
                compiled=False,
                compile_issue_codes=sorted(
                    {issue.code for issue in compiled.assessment.issues}
                ),
                outcome="rejected",
                failure_code="compile_failed",
                notes=[issue.message for issue in compiled.assessment.issues][:4],
            )
        operations = list(compiled.transaction.operations)
        compile_ms = int((time.perf_counter() - evaluate_started) * 1000)
        planner_ref = planner_ref.model_copy(
            update={
                "plan_hash": _operations_digest(operations),
                "compiled_operation_count": len(operations),
            }
        )
        shadow_started = time.perf_counter()
        try:
            shadow = self._shadow_document(base_document, draft, attempt, request)
        except Exception as exc:  # noqa: BLE001 - a failed candidate is data, not a crash
            return RepairAttemptRecord(
                attempt=attempt,
                planner=planner_ref,
                compiled=True,
                compiled_operation_count=len(operations),
                outcome="rejected",
                failure_code="validation_failed",
                notes=[f"the candidate could not be applied to a shadow copy: {exc}"],
            )
        shadow_result = run_validation(
            shadow,
            self.registry,
            self.profile,
            project_id=request.project_id,
            service=self.service,
            now=request.evaluated_at,
        )
        shadow_ms = int((time.perf_counter() - shadow_started) * 1000)
        self._last_timings = RepairTimings(
            plan_ms=self._last_timings.plan_ms,
            compile_ms=compile_ms,
            shadow_validate_ms=shadow_ms,
        )
        changed = changed_existing_ids(base_document, shadow)
        touched = touched_existing_ids(base_document, shadow)
        created = created_ids(base_document, shadow)
        removed = deleted_ids(base_document, shadow)
        protected_post = protected_hashes(
            shadow,
            self.registry,
            scope,
            rule_bundle_fingerprint=shadow_result.rule_bundle_fingerprint,
            created_ids=created,
        )
        verdict = evaluate_candidate(
            base_result,
            shadow_result,
            request,
            before=base_document,
            after=shadow,
            protected_pre=protected_pre,
            protected_post=protected_post,
            changed_ids=changed,
            touched_ids=touched,
            created_ids=created,
            deleted_ids=removed,
            postconditions=postconditions,
        )
        if verdict.accepted:
            return RepairAttemptRecord(
                attempt=attempt,
                planner=planner_ref,
                compiled=True,
                compiled_operation_count=len(operations),
                shadow_content_hash=shadow_result.content_hash,
                shadow_validation_hash=shadow_result.result_hash,
                touched_existing_ids=touched,
                target_resolved=True,
                outcome="selected",
                failure_code=None,
                notes=[],
            )
        return RepairAttemptRecord(
            attempt=attempt,
            planner=planner_ref,
            compiled=True,
            compiled_operation_count=len(operations),
            shadow_content_hash=shadow_result.content_hash,
            shadow_validation_hash=shadow_result.result_hash,
            touched_existing_ids=touched,
            target_resolved=verdict.target_resolved,
            collateral_regressions=verdict.collateral_regressions,
            locality_violation_ids=verdict.locality_violation_ids,
            protected_region_changed=verdict.protected_region_changed,
            outcome="rejected",
            failure_code=verdict.failure_code,
            notes=verdict.notes,
        )

    def _apply_and_prove(
        self,
        *,
        request: RepairRequest,
        draft,
        scope: RepairScope,
        base_document: Document,
        protected_pre,
        attempts: list[RepairAttemptRecord] | None = None,
        selected_attempt: int | None = None,
    ) -> tuple[RepairApplyProof, RepairFailureCode | None]:
        """The one governed write, plus the undo/redo proof the baseline demands."""

        transaction = draft.transaction(
            expected_revision=request.revision, label="Agent self-repair"
        )
        compiled = SemanticTransactionCompiler(self.service).compile(
            request.document_id, transaction
        )
        if compiled.transaction is None or not compiled.assessment.valid:
            return RepairApplyProof(applied=False, base_revision=request.revision), "compile_failed"
        operations = list(compiled.transaction.operations)
        base_hash = drafting_content_hash(base_document)
        audit = AuditContext(
            actor=REPAIR_SURFACE_ACTOR,
            surface="internal",
            tool_name=REPAIR_TOOL_NAME,
            project_id=request.project_id or None,
            label="Agent self-repair",
            validation_status="valid",
            # §M5-5's binding list, as far as it can be true *before* the write: the validation
            # the repair was planned against, the scope it was frozen into, the attempt count and
            # the plan that was selected. The post-apply hash is bound by ``repair.completed``
            # below, because a record written before the write cannot contain it.
            validation_evidence={
                "validation_hash": request.validation_hash,
                "target_rule_id": request.target.rule_id,
                "target_code": request.target.code,
                "scope_fingerprint": protected_pre.scope_fingerprint,
                "attempt_count": len(attempts),
            },
            metadata={
                "repair_request_hash": request.request_hash(),
                "target_validation_hash": request.validation_hash,
                "plan_hash": _operations_digest(operations),
                "selected_attempt": selected_attempt,
            },
        )
        try:
            applied = self.service.apply_transaction(
                request.document_id,
                TransactionRequest(
                    operations=operations,
                    expected_revision=request.revision,
                    label="Agent self-repair",
                    source="llm",
                ),
                audit=audit,
            )
        except RevisionConflictError:
            return RepairApplyProof(applied=False, base_revision=request.revision), "revision_conflict"
        except Exception:  # noqa: BLE001 - a refused apply is a failure verdict, not a crash
            return RepairApplyProof(applied=False, base_revision=request.revision), "apply_failed"

        repaired = applied.document
        repaired_hash = drafting_content_hash(repaired)
        record = self.service.store.get_audit_record_for_revision(
            request.document_id, repaired.revision
        )
        post_started = time.perf_counter()
        post_result = run_validation(
            repaired,
            self.registry,
            self.profile,
            project_id=request.project_id,
            service=self.service,
            now=request.evaluated_at,
        )
        self._last_timings = self._last_timings.model_copy(
            update={"post_validate_ms": int((time.perf_counter() - post_started) * 1000)}
        )
        protected_post = protected_hashes(
            repaired,
            self.registry,
            scope,
            rule_bundle_fingerprint=post_result.rule_bundle_fingerprint,
            created_ids=created_ids(base_document, repaired),
        )
        proof = RepairApplyProof(
            applied=True,
            base_revision=request.revision,
            result_revision=repaired.revision,
            transaction_hash=_sha(
                f"{base_hash}:{repaired_hash}:{_operations_digest(operations)}"
            ),
            audit_record_id=record.record_id if record is not None else "",
            logical_change_count=1,
            history_entries_added=1,
            post_validation_hash=post_result.result_hash,
            post_protected=protected_post,
        )
        if self.prove_undo:
            proof, failure = self._prove_undo_redo(
                request=request,
                proof=proof,
                base_hash=base_hash,
                repaired_hash=repaired_hash,
            )
            if failure is not None:
                return proof, failure
        return proof, None

    def _prove_undo_redo(
        self,
        *,
        request: RepairRequest,
        proof: RepairApplyProof,
        base_hash: str,
        repaired_hash: str,
    ) -> tuple[RepairApplyProof, RepairFailureCode | None]:
        """Undo must restore the pre-repair drawing; redo must restore the repaired one.

        This is deliberately *executed*, not asserted from the undo stack contents: the claim
        "one logical change, one undo unit" is about behaviour, and M4 already learned what
        happens when a workflow claims a mechanism it only documents.
        """

        audit = AuditContext(
            actor=REPAIR_SURFACE_ACTOR,
            surface="internal",
            tool_name="repair_proof_undo",
            label="Self-repair undo proof",
        )
        try:
            undone = self.service.undo(
                request.document_id, expected_revision=proof.result_revision, audit=audit
            )
        except Exception:  # noqa: BLE001
            return proof, "undo_proof_failed"
        undo_restored = drafting_content_hash(undone) == base_hash
        try:
            redone = self.service.redo(
                request.document_id, expected_revision=undone.revision, audit=audit
            )
        except Exception:  # noqa: BLE001
            return proof.model_copy(update={"undo_restored_base": undo_restored}), "undo_proof_failed"
        redo_restored = drafting_content_hash(redone) == repaired_hash
        updated = proof.model_copy(
            update={
                "undo_restored_base": undo_restored,
                "redo_restored_result": redo_restored,
                "result_revision": redone.revision,
                "history_entries_added": 3,
            }
        )
        if not (undo_restored and redo_restored):
            return updated, "undo_proof_failed"
        return updated, None

    # -- result assembly ------------------------------------------------- #

    def _result(
        self,
        request: RepairRequest,
        *,
        status: RepairStatus,
        failure_code: RepairFailureCode | None,
        reasons: list[str],
        attempts: list[RepairAttemptRecord],
        metrics: list[RepairAttemptMetrics] | None = None,
        protected_pre=None,
        timings: RepairTimings | None = None,
        selected: RepairAttemptRecord | None = None,
        applied: RepairApplyProof | None = None,
    ) -> RepairRunResult:
        identity = self._planner_identity()
        result = RepairRunResult(
            request_hash=request.request_hash(),
            document_id=request.document_id,
            project_id=request.project_id,
            profile_id=request.profile_id,
            profile_version=request.profile_version,
            engine_version=request.engine_version,
            validation_hash=request.validation_hash,
            evaluated_at=request.evaluated_at,
            target=request.target,
            scope=request.scope,
            budgets=request.budgets,
            planner=identity,
            protected_pre=protected_pre or protected_hashes(
                self.service.get_document(request.document_id),
                self.registry,
                request.scope,
                rule_bundle_fingerprint=self.profile.fingerprint,
            ),
            attempts=attempts,
            attempt_metrics=metrics or [],
            timings=timings or RepairTimings(),
            selected_attempt=selected.attempt if selected is not None else None,
            selected_plan_hash=selected.planner.plan_hash if selected is not None else "",
            applied=applied or RepairApplyProof(applied=False, base_revision=request.revision),
            status=status,
            failure_code=failure_code,
            reasons=reasons,
        )
        return result.model_copy(update={"repair_hash": repair_digest(result)})

    def _record_repair_evidence(
        self,
        request: RepairRequest,
        *,
        event_type: str,
        status: str,
        evidence: dict[str, Any],
        error_code: str = "",
        result_revision: int | None = None,
    ) -> None:
        """Append the repair's own audit evidence, bound to the write it describes (§M5-5).

        The governed write already produces a ``revision.created`` record carrying the target
        validation hash, the scope fingerprint, the attempt count and the selected plan. What
        that record *cannot* contain is the outcome — the final validation hash only exists
        after the write — so the run appends one more event that completes the binding. Both
        records are secret-free by construction: they carry hashes, counts and codes, never a
        prompt, a provider key or a repaired payload.
        """

        context = AuditContext(
            actor=REPAIR_SURFACE_ACTOR,
            surface="internal",
            tool_name=REPAIR_TOOL_NAME,
            project_id=request.project_id or None,
            label=f"Agent self-repair evidence: {request.target.code}",
            metadata={"repair_request_hash": request.request_hash()},
        )
        try:
            self.service.audit.record_event(
                event_type,
                context,
                document_id=request.document_id,
                base_revision=request.revision,
                result_revision=result_revision,
                status=status,
                evidence=evidence,
                error_code=error_code,
            )
        except Exception:  # noqa: BLE001 - evidence bookkeeping must not change a verdict
            return

    def _planner_identity(self) -> RepairPlannerIdentity:
        describe = getattr(self.planner, "identity", None)
        if callable(describe):
            return describe()
        return RepairPlannerIdentity(
            planner_id=getattr(self.planner, "planner_id", "unknown"),
            planner_version=PLANNER_VERSION,
        )


def repair_document_finding(
    service: DocumentService,
    profile: EffectiveProfile,
    document_id: str,
    *,
    target_code: str | None = None,
    element_ids: list[str] | None = None,
    planner: RepairPlanner | None = None,
    apply_enabled: bool = True,
    hop: int = 1,
    max_touched_existing_ids: int | None = None,
    validator_id: str = "",
    declared_by: str = "surface_request",
    permits_creation: bool = False,
    max_created_ids: int = 0,
    permits_deletion: bool = False,
    max_deleted_ids: int = 0,
    now: datetime | None = None,
) -> RepairRunResult:
    """Convenience entry point used by the CLI, MCP and REST surfaces.

    It binds the request from a *fresh* canonical validation of the stored document, so every
    surface repairs the same finding M4 would report rather than a client's description of it.

    The knobs a caller *may* set are policy, not planning: which finding, how far the scope may
    reach, and whether the repair is allowed to add equipment. Everything else — the profile,
    the rule bundle, the budgets, the oracle — is decided server-side, which is what makes three
    surfaces and a benchmark case the same repair rather than four similar ones.
    """

    moment = now or datetime.now(UTC)
    document = service.get_document(document_id)
    result = run_validation(
        document, service.symbols, profile, service=service, now=moment
    )
    candidates = [
        issue
        for issue in result.issues
        if not issue.is_waived
        and (target_code is None or issue.code == target_code)
        and (not validator_id or issue.validator_id == validator_id)
        and (not element_ids or set(element_ids) & set(issue.element_ids))
    ]
    if not candidates:
        raise RepairStaleEvidence(
            "no_repairable_finding",
            "the drawing has no unwaived finding matching the requested target",
        )
    issue = candidates[0]
    request = build_repair_request(
        document,
        result,
        issue,
        registry=service.symbols,
        profile=profile,
        hop=hop,
        max_touched_existing_ids=max_touched_existing_ids,
        declared_by=declared_by,
        permits_creation=permits_creation,
        max_created_ids=max_created_ids,
        permits_deletion=permits_deletion,
        max_deleted_ids=max_deleted_ids,
    )
    orchestrator = RepairOrchestrator(
        service, profile, planner=planner, apply_enabled=apply_enabled
    )
    return orchestrator.run(request)


__all__ = [
    "MAX_REPAIR_ATTEMPTS",
    "REPAIR_SURFACE_ACTOR",
    "REPAIR_TOOL_NAME",
    "RepairOrchestrator",
    "RepairStaleEvidence",
    "build_repair_request",
    "repair_document_finding",
]
