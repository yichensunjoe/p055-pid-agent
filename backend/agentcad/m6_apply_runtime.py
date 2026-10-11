"""M6-2B-D4: the sealed governed-apply runtime for confirmed M6 findings.

The shape follows the M13 change-set precedent exactly: a dedicated tool registry (never the
default P&ID one), a domain adapter implementing the runtime ports, an audit adapter, and an
executor that drives the store's atomic commit. What the Gate's D4 freeze adds on top:

* **The binding material is server-derived, never client-supplied.** The approval binds
  ``patch_id`` + ``canonical_digest`` + the source triple (document/revision/content_hash) +
  the target pair (document/baseline revision) + the finding/candidate identity chain. At
  apply time the executor re-derives it from the stored rows and hash-compares against the
  authorized record — drift is a stable conflict with zero engineering writes.
* **The final checks run inside the store's write transaction** (D87-3 L1/L3): identity
  chain, source pin, semantic baseline, duplicate-apply guard, document CAS, ledger row,
  audit, harness close-out — one BEGIN IMMEDIATE, or nothing.
* **Undo is a compensation transaction** (the contract's undo mechanism): a revert is a new
  apply record through the same sealed flow, and the original record transitions
  applied->reverted in the same transaction. The candidate is never marked reverted.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import Field, TypeAdapter

from .audit import AuditRecorder
from .audit_models import AuditRecordDraft
from .m6_candidate_core import canonical_digest
from .m6_candidate_models import SemanticCandidate
from .m6_ingestion_contract import identity_prefix
from .models import (
    AddElementOperation,
    DeleteElementOperation,
    Document,
    HistoryEntry,
    Operation,
    StrictModel,
    UpdateElementOperation,
    utc_now,
)
from .runtime.ports import (
    AuditEvent,
    AuditRecordRef,
    ClosureRequest,
    DocumentContext,
    ExecutionOutcome,
    ToolDefinitionView,
)
from .service import DocumentService, InvalidOperationError
from .store import SQLiteDocumentStore
from .symbols import SymbolRegistry

TOOL_APPLY_M6_FINDING = "apply_m6_confirmed_finding"
TOOL_REVERT_M6_APPLY = "revert_m6_apply"

#: Parse the persisted operations payload back into the compiler's operation union.
_OPERATION_ADAPTER: TypeAdapter[Any] = TypeAdapter(Operation)


def parse_operations(payload: Any) -> list[Any]:
    """Parse a persisted operations payload back into operation models (the compiler's union)."""

    return [_OPERATION_ADAPTER.validate_python(raw) for raw in payload]


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class M6ApplyError(RuntimeError):
    """A governed-apply refusal. The code is the stable contract."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ApplyM6PatchIntent(StrictModel):
    """Tool-level payload for an apply: which persisted patch to land."""

    patch_id: str = Field(min_length=1)


class RevertM6ApplyIntent(StrictModel):
    """Tool-level payload for a compensation revert: which applied record to compensate."""

    apply_id: str = Field(min_length=1)


def apply_binding_material(
    patch_row: dict[str, Any], candidate: SemanticCandidate
) -> dict[str, Any]:
    """The exact approval binding for an apply, derived from stored rows only."""

    return {
        "patch_id": str(patch_row["patch_id"]),
        "canonical_digest": str(patch_row["canonical_digest"]),
        "source": {
            "document_id": candidate.artifact.source_document_id,
            "revision": candidate.artifact.source_revision,
            "content_hash": candidate.artifact.content_hash,
        },
        "target": {
            "document_id": str(patch_row["target_document_id"]),
            "baseline_revision": int(patch_row["baseline_revision"]),
        },
        "finding_ids": json.loads(str(patch_row["finding_ids_json"])),
        "candidate_id": candidate.candidate_id,
    }


def revert_binding_material(
    apply_row: dict[str, Any],
    candidate: SemanticCandidate,
    revert_patch: str,
) -> dict[str, Any]:
    """The exact approval binding for a compensation revert."""

    return {
        "revert_of": str(apply_row["apply_id"]),
        "patch_id": str(apply_row["patch_id"]),
        "candidate_id": candidate.candidate_id,
        "source": {
            "document_id": candidate.artifact.source_document_id,
            "revision": candidate.artifact.source_revision,
            "content_hash": candidate.artifact.content_hash,
        },
        "target": {
            "document_id": str(apply_row["target_document_id"]),
            "result_revision": int(apply_row["result_revision"]),
        },
        "revert_patch_id": revert_patch,
    }


def apply_record_id(*, patch_id: str, candidate_id: str, intent_hash: str) -> str:
    """The ledger row identity: content-derived, full width, from the declared prefix."""

    return identity_prefix("apply_id") + canonical_digest(
        {"patch_id": patch_id, "candidate_id": candidate_id, "intent_hash": intent_hash}
    )


def compensation_patch_identity(
    original_patch_row: dict[str, Any],
    finding_facts: dict[str, Any],
    inverse_operations: list[dict[str, Any]],
    *,
    baseline_revision: int,
) -> tuple[str, str]:
    """The compensation patch identity AND its canonical digest.

    The identity must re-derive exactly the way the store's L1 cross-check rebuilds it —
    the same payload shape the compiler produces (compiler/intent/facts/operations/baseline),
    with intent "revert" — so a compensation row passes the same recomputation proof as any
    compiler-produced patch. Returns ``(patch_id, canonical_digest)``.
    """

    digest = canonical_digest(
        {
            "compiler": str(original_patch_row["compiler_name"]),
            "compiler_version": str(original_patch_row["compiler_version"]),
            "compiler_rules": str(original_patch_row["compiler_rules"]),
            "intent": "revert",
            "finding_facts": finding_facts,
            "operations": inverse_operations,
            "baseline_revision": baseline_revision,
        }
    )
    return identity_prefix("patch_id") + digest, digest


def _compensation_patch_for(
    store: SQLiteDocumentStore, original_apply_row: dict[str, Any]
) -> tuple[str, str, dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Compute the compensation patch identity for one applied record (read-only).

    The inverse operations come from the apply's audit evidence (computed when the
    pre-apply state was in hand); the digest shares the compiler's payload shape so the
    store's L1 recomputation proof holds for compensation rows too. Returns the patch id,
    its digest, the original patch row, the inverse operations, and the apply evidence
    block (which carries the semantic key + post-apply digest the revert rechecks).
    """

    original_patch = store.get_structured_patch(str(original_apply_row["patch_id"]))
    if original_patch is None:
        raise M6ApplyError("the original patch row is missing", code="patch_not_found")
    apply_audit = store.get_audit_record(str(original_apply_row["audit_record_id"]))
    apply_evidence = apply_audit.evidence.get("m6", {}) if apply_audit is not None else {}
    inverse = apply_evidence.get("inverse_operations", [])
    if not inverse:
        raise M6ApplyError(
            f"apply record {original_apply_row['apply_id']} carries no inverse operations",
            code="revert_not_compilable",
        )
    finding_ids = json.loads(str(original_patch["finding_ids_json"]))
    finding = store.get_confirmed_finding(str(finding_ids[0]))
    if finding is None:
        raise M6ApplyError(
            "the original patch's finding is missing", code="apply_identity_chain_mismatch"
        )
    from .m6_candidate_core import finding_facts_payload

    baseline_revision = int(original_apply_row["result_revision"])
    patch_id, digest = compensation_patch_identity(
        original_patch,
        finding_facts_payload(finding),
        inverse,
        baseline_revision=baseline_revision,
    )
    return patch_id, digest, original_patch, inverse, apply_evidence


def inverse_operations(
    document: Document, operations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The inverse of one applied patch, computed while the pre-apply state is in hand.

    creation -> delete the created element; metadata_enrichment -> restore the value the
    target carried before the apply. Anything else is refused rather than guessed.
    """

    inverse: list[dict[str, Any]] = []
    elements = {element.id: element for element in document.elements}
    for raw in operations:
        op = _OPERATION_ADAPTER.validate_python(raw)
        if isinstance(op, AddElementOperation):
            inverse.append(
                DeleteElementOperation(element_id=op.element.id).model_dump(mode="json")
            )
            continue
        if isinstance(op, UpdateElementOperation):
            element = elements.get(op.element_id)
            prior: dict[str, Any] = {}
            for key in op.patch:
                prior[key] = (
                    element.properties.get(key, "")
                    if key != "label"
                    else (getattr(element, "label", "") if element is not None else "")
                )
            inverse.append(
                UpdateElementOperation(element_id=op.element_id, patch=prior).model_dump(
                    mode="json"
                )
            )
            continue
        raise M6ApplyError(
            f"no inverse is defined for operation {type(op).__name__}; the revert is "
            "refused rather than guessed",
            code="revert_not_compilable",
        )
    return inverse


class M6ToolRegistry:
    """The neutral governance view of the M6 apply plane (never the default registry)."""

    _APPLY = ToolDefinitionView(
        name=TOOL_APPLY_M6_FINDING,
        description="Apply one persisted M6 patch to its target document (M6-2B-D4)",
        permission="ask",
        risk="engineering_change",
        audit_event="tool.m6_apply.apply",
    )
    _REVERT = ToolDefinitionView(
        name=TOOL_REVERT_M6_APPLY,
        description="Compensate one applied M6 record with a governed inverse write",
        permission="ask",
        risk="engineering_change",
        audit_event="tool.m6_apply.revert",
    )

    def require(self, name: str) -> ToolDefinitionView:
        if name == self._APPLY.name:
            return self._APPLY
        if name == self._REVERT.name:
            return self._REVERT
        raise KeyError(f"unknown M6 apply tool: {name}")


class M6AuditAdapter:
    """AuditPort for the M6 apply plane (same field-parity shape as M13's adapter)."""

    def __init__(self, recorder: AuditRecorder) -> None:
        self._recorder = recorder

    def record(self, event: AuditEvent) -> AuditRecordRef:
        from .audit import request_audit_context

        context = request_audit_context(
            event.tool_name or event.event_type,
            actor=event.actor,
            surface=event.surface,
            label=event.label,
            session_id=event.session_id,
            approval_id=event.approval_id,
            tool_call_id=event.tool_call_id,
            provider=event.provider,
            model=event.model,
            intent_hash=event.intent_hash,
            metadata=event.metadata,
        )
        record = self._recorder.record_rejection(
            context,
            event_type=event.event_type,
            error_code=event.error_code,
            document_id=event.document_id,
            base_revision=event.base_revision,
            status=event.status,
            evidence=event.evidence,
        )
        return AuditRecordRef(
            record_id=record.record_id, ordinal=record.ordinal, record_hash=record.record_hash
        )


class M6ApplyExecutor:
    """Drives the store's atomic M6 commits. Only the domain adapter calls these — there
    is no direct-write entry point."""

    def __init__(
        self,
        store: SQLiteDocumentStore,
        service: DocumentService,
        registry: SymbolRegistry,
    ) -> None:
        self._store = store
        self._service = service
        self._registry = registry

    # ------------------------------------------------------------- apply

    def apply_authorized(
        self,
        *,
        authorized: Any,
        audit_event: AuditEvent,
        closure: ClosureRequest,
        intent: ApplyM6PatchIntent,
    ) -> ExecutionOutcome:
        self._require_running_call(authorized)
        try:
            return self._apply(authorized, audit_event, closure, intent)
        except sqlite3.Error as exc:
            # The atomic commit rolled back (zero dirty writes); the refusal is a stable code.
            self._failure_closeout(authorized, audit_event, intent.patch_id)
            raise M6ApplyError(
                f"the atomic apply was rolled back: {exc}", code="apply_commit_failed"
            ) from exc
        except Exception:
            self._failure_closeout(authorized, audit_event, intent.patch_id)
            raise

    def _apply(
        self,
        authorized: Any,
        audit_event: AuditEvent,
        closure: ClosureRequest,
        intent: ApplyM6PatchIntent,
    ) -> ExecutionOutcome:
        store = self._store
        patch_row = store.get_structured_patch(intent.patch_id)
        if patch_row is None:
            raise M6ApplyError(
                f"no persisted patch {intent.patch_id!r}", code="patch_not_found"
            )
        candidate = self._candidate_for(patch_row)
        # N7: a patch with an applied record may never apply twice.
        for record in store.list_apply_records(patch_id=intent.patch_id):
            if record["status"] == "applied":
                raise M6ApplyError(
                    f"patch {intent.patch_id} already applied as {record['apply_id']}",
                    code="patch_already_applied",
                )
        # The approval binds the server-derived material; recompute and compare.
        material = apply_binding_material(patch_row, candidate)
        from .runtime.harness import tool_intent_hash

        target_id = str(patch_row["target_document_id"])
        if (
            tool_intent_hash(TOOL_APPLY_M6_FINDING, target_id, material)
            != authorized.record.intent_hash
        ):
            raise M6ApplyError(
                "the stored patch/finding/candidate chain drifted from the approved intent",
                code="m6_apply_intent_mismatch",
            )

        finding_ids = json.loads(str(patch_row["finding_ids_json"]))
        finding = store.get_confirmed_finding(finding_ids[0])
        if finding is None:
            raise M6ApplyError(
                f"patch {intent.patch_id} cites missing finding {finding_ids[0]}",
                code="apply_identity_chain_mismatch",
            )

        stored = store.get(target_id)
        if stored is None:
            raise M6ApplyError(
                f"target document {target_id!r} does not exist", code="target_not_found"
            )
        operations = parse_operations(json.loads(str(patch_row["operations_json"])))
        # D4 conflict recheck on the REAL created identity (never D3's region/absent
        # convention): a creation whose derived element id already exists in the target is a
        # moved baseline, refused before staging so the failure is a conflict, not a
        # generic operation error. Zero engineering write on refusal.
        existing_ids = {element.id for element in stored.document.elements}
        for operation in operations:
            if isinstance(operation, AddElementOperation) and operation.element.id in existing_ids:
                raise M6ApplyError(
                    f"target already carries created identity {operation.element.id!r}",
                    code="semantic_baseline_moved",
                )
        staged = self._stage(stored.document, operations)
        # B' precommit: re-validate exactly the staged document that would be committed.
        try:
            Document.model_validate(staged.model_dump(mode="python"))
        except Exception as exc:
            raise M6ApplyError(
                f"the staged target failed precommit validation: {exc}",
                code="precommit_validation_failed",
            ) from exc

        inverse = inverse_operations(stored.document, operations)
        post_value_digest = self._post_value_digest(finding, staged)
        apply_id = apply_record_id(
            patch_id=intent.patch_id,
            candidate_id=candidate.candidate_id,
            intent_hash=authorized.record.intent_hash,
        )
        audit = self._success_audit(
            authorized,
            audit_event,
            label=f"Apply M6 patch {intent.patch_id}",
            evidence=self._apply_evidence(
                patch_row,
                candidate,
                finding_ids,
                apply_id,
                inverse,
                post_value_digest,
                authorized,
            ),
            base_revision=stored.document.revision,
            result_revision=staged.revision,
            target_id=target_id,
        )
        now = utc_now()
        store.commit_m6_apply(
            document=staged,
            expected_revision=stored.document.revision,
            history=HistoryEntry(
                document_id=target_id,
                revision=staged.revision,
                timestamp=now,
                source="mcp",
                action="transaction",
                label=f"Apply M6 patch {intent.patch_id}",
                operation_count=len(operations),
            ),
            history_details={"m6": {"patch_id": intent.patch_id, "apply_id": apply_id}},
            audit=audit,
            tool_call=authorized.record.model_copy(
                update={
                    "status": "completed",
                    "result_revision": staged.revision,
                    "completed_at": now,
                }
            ),
            approval=authorized.approval.model_copy(
                update={"status": "consumed", "consumed_at": now}
            )
            if authorized.approval is not None
            else None,
            session=authorized.session.model_copy(
                update={"status": "completed", "updated_at": now}
            )
            if closure.close_session
            else authorized.session,
            symbols=self._registry,
            apply_id=apply_id,
            patch_id=intent.patch_id,
            candidate_id=candidate.candidate_id,
            baseline_revision=int(patch_row["baseline_revision"]),
            expected_semantic_identity=finding.baseline.comparison_identity,
            expected_semantic_path=finding.baseline.comparison_path,
            expected_baseline_digest=finding.baseline.value_digest,
            history_limit=self._service.history_limit,
        )
        return ExecutionOutcome(
            document_id=target_id,
            base_revision=stored.document.revision,
            result_revision=staged.revision,
            payload={"apply_id": apply_id, "patch_id": intent.patch_id},
        )

    # ------------------------------------------------------------- revert

    def revert_authorized(
        self,
        *,
        authorized: Any,
        audit_event: AuditEvent,
        closure: ClosureRequest,
        intent: RevertM6ApplyIntent,
    ) -> ExecutionOutcome:
        self._require_running_call(authorized)
        try:
            return self._revert(authorized, audit_event, closure, intent)
        except sqlite3.Error as exc:
            # Same discipline as apply_authorized: the atomic commit rolled back (zero
            # dirty writes, the original record still applied) and the caller sees a
            # stable refusal code, never a raw sqlite error.
            self._failure_closeout(authorized, audit_event, intent.apply_id)
            raise M6ApplyError(
                f"the atomic revert was rolled back: {exc}", code="revert_commit_failed"
            ) from exc
        except Exception:
            self._failure_closeout(authorized, audit_event, intent.apply_id)
            raise

    def _revert(
        self,
        authorized: Any,
        audit_event: AuditEvent,
        closure: ClosureRequest,
        intent: RevertM6ApplyIntent,
    ) -> ExecutionOutcome:
        store = self._store
        original = store.get_apply_record(intent.apply_id)
        if original is None:
            raise M6ApplyError(
                f"no apply record {intent.apply_id!r}", code="apply_record_not_found"
            )
        if str(original["status"]) != "applied":
            raise M6ApplyError(
                f"apply record {intent.apply_id} is {original['status']!r}; only an applied "
                "record can be reverted",
                code="apply_record_not_applied",
            )
        candidate = store.get_semantic_candidate(str(original["candidate_id"]))
        if candidate is None:
            raise M6ApplyError(
                "the apply record's candidate is missing", code="apply_identity_chain_mismatch"
            )
        # The inverse operations were computed at apply time, while the pre-apply state was
        # in hand, and recorded in the apply audit evidence.
        revert_patch, revert_digest, original_patch, inverse, apply_evidence = (
            _compensation_patch_for(store, original)
        )
        material = revert_binding_material(original, candidate, revert_patch)
        from .runtime.harness import tool_intent_hash

        target_id = str(original["target_document_id"])
        if (
            tool_intent_hash(TOOL_REVERT_M6_APPLY, target_id, material)
            != authorized.record.intent_hash
        ):
            raise M6ApplyError(
                "the revert binding drifted from the approved intent",
                code="m6_apply_intent_mismatch",
            )
        original_patch = store.get_structured_patch(str(original["patch_id"]))
        if original_patch is None:
            raise M6ApplyError("the original patch row is missing", code="patch_not_found")
        # The compensation patch row (idempotent by identity).
        store.insert_structured_patch(
            patch_id=revert_patch,
            finding_ids=json.loads(str(original_patch["finding_ids_json"])),
            intent="revert",
            policy_disposition=str(original_patch["policy_disposition"]),
            operations_json=_canonical(inverse),
            compiler_name=str(original_patch["compiler_name"]),
            compiler_version=str(original_patch["compiler_version"]),
            compiler_rules=str(original_patch["compiler_rules"]),
            baseline_revision=int(original["result_revision"]),
            canonical_digest=revert_digest,
            source_document_id=str(original["source_document_id"]),
            target_document_id=target_id,
        )
        stored = store.get(target_id)
        if stored is None:
            raise M6ApplyError(
                f"target document {target_id!r} does not exist", code="target_not_found"
            )
        operations = parse_operations(inverse)
        staged = self._stage(stored.document, operations)
        try:
            Document.model_validate(staged.model_dump(mode="python"))
        except Exception as exc:
            raise M6ApplyError(
                f"the staged revert failed precommit validation: {exc}",
                code="precommit_validation_failed",
            ) from exc

        revert_id = apply_record_id(
            patch_id=revert_patch,
            candidate_id=candidate.candidate_id,
            intent_hash=authorized.record.intent_hash,
        )
        audit = self._success_audit(
            authorized,
            audit_event,
            label=f"Revert M6 apply {intent.apply_id}",
            evidence={
                "m6": {
                    "intent": "revert",
                    "revert_of": intent.apply_id,
                    "revert_patch_id": revert_patch,
                    "original_patch_id": str(original["patch_id"]),
                    "candidate_id": candidate.candidate_id,
                    "revert_apply_id": revert_id,
                }
            },
            base_revision=stored.document.revision,
            result_revision=staged.revision,
            target_id=target_id,
        )
        now = utc_now()
        store.commit_m6_revert(
            document=staged,
            expected_revision=stored.document.revision,
            history=HistoryEntry(
                document_id=target_id,
                revision=staged.revision,
                timestamp=now,
                source="mcp",
                action="transaction",
                label=f"Revert M6 apply {intent.apply_id}",
                operation_count=len(operations),
            ),
            history_details={"m6": {"revert_of": intent.apply_id, "revert_apply_id": revert_id}},
            audit=audit,
            tool_call=authorized.record.model_copy(
                update={
                    "status": "completed",
                    "result_revision": staged.revision,
                    "completed_at": now,
                }
            ),
            approval=authorized.approval.model_copy(
                update={"status": "consumed", "consumed_at": now}
            )
            if authorized.approval is not None
            else None,
            session=authorized.session.model_copy(
                update={"status": "completed", "updated_at": now}
            )
            if closure.close_session
            else authorized.session,
            symbols=self._registry,
            revert_apply_id=revert_id,
            revert_patch_id=revert_patch,
            original_apply_id=intent.apply_id,
            candidate_id=candidate.candidate_id,
            base_revision=int(original["result_revision"]),
            expected_semantic_identity=str(apply_evidence.get("semantic_identity", "")),
            expected_semantic_path=str(apply_evidence.get("semantic_path", "existence")),
            expected_post_digest=str(apply_evidence.get("post_value_digest", "")),
            history_limit=self._service.history_limit,
        )
        return ExecutionOutcome(
            document_id=target_id,
            base_revision=stored.document.revision,
            result_revision=staged.revision,
            payload={"revert_apply_id": revert_id, "reverted": intent.apply_id},
        )

    # ------------------------------------------------------------- shared plumbing

    def _require_running_call(self, authorized: Any) -> None:
        persisted_call = self._store.get_tool_call(authorized.record.id)
        if persisted_call is None or str(persisted_call.status) != "running":
            # Replay of an already-terminal execution: refuse without touching the
            # completed records (M13 D85-5 discipline).
            raise M6ApplyError(
                "this authorization has already been used", code="m6_apply_not_authorized"
            )

    def _candidate_for(self, patch_row: dict[str, Any]) -> SemanticCandidate:
        finding_ids = json.loads(str(patch_row["finding_ids_json"]))
        finding = self._store.get_confirmed_finding(str(finding_ids[0]))
        if finding is None:
            raise M6ApplyError(
                f"patch cites missing finding {finding_ids[0]}",
                code="apply_identity_chain_mismatch",
            )
        candidate = self._store.get_semantic_candidate(finding.candidate_id)
        if candidate is None:
            raise M6ApplyError(
                f"finding {finding.finding_id} cites missing candidate",
                code="apply_identity_chain_mismatch",
            )
        return candidate

    def _stage(self, document: Document, operations: list[Any]) -> Document:
        try:
            return self._service._stage_mutation(document, operations)  # noqa: SLF001
        except InvalidOperationError as exc:
            raise M6ApplyError(str(exc), code="invalid_operation") from exc

    def _post_value_digest(self, finding: Any, staged: Document) -> str:
        """The semantic value digest of the *staged* result at the comparison key."""

        from .engineering_ir import build_engineering_graph
        from .m6_candidate_core import semantic_value, value_digest

        path = finding.baseline.comparison_path
        return value_digest(
            path,
            semantic_value(
                staged,
                build_engineering_graph(staged, self._registry),
                finding.baseline.comparison_identity,
                path,
            ),
        )

    def _apply_evidence(
        self,
        patch_row: dict[str, Any],
        candidate: SemanticCandidate,
        finding_ids: list[str],
        apply_id: str,
        inverse: list[dict[str, Any]],
        post_value_digest: str,
        authorized: Any,
    ) -> dict[str, Any]:
        finding = self._store.get_confirmed_finding(str(finding_ids[0]))
        return {
            "m6": {
                "patch_id": str(patch_row["patch_id"]),
                "apply_id": apply_id,
                "candidate_id": candidate.candidate_id,
                "finding_ids": finding_ids,
                "source_document_id": candidate.artifact.source_document_id,
                "source_revision": candidate.artifact.source_revision,
                "source_content_hash": candidate.artifact.content_hash,
                "target_document_id": str(patch_row["target_document_id"]),
                "baseline_revision": int(patch_row["baseline_revision"]),
                "semantic_identity": finding.baseline.comparison_identity if finding else "",
                "semantic_path": finding.baseline.comparison_path if finding else "existence",
                "post_value_digest": post_value_digest,
                "inverse_operations": inverse,
                "session_id": authorized.session.id,
                "approval_id": authorized.approval.id if authorized.approval else "",
                "tool_call_id": authorized.record.id,
            }
        }

    def _success_audit(
        self,
        authorized: Any,
        audit_event: AuditEvent,
        *,
        label: str,
        evidence: dict[str, Any],
        base_revision: int,
        result_revision: int,
        target_id: str,
    ) -> AuditRecordDraft:
        return AuditRecordDraft(
            event_type="revision.created",
            actor=audit_event.actor,
            surface=audit_event.surface,
            tool_name=authorized.definition.name,
            status="applied",
            document_id=target_id,
            base_revision=base_revision,
            result_revision=result_revision,
            session_id=authorized.session.id,
            approval_id=authorized.approval.id if authorized.approval else None,
            tool_call_id=authorized.record.id,
            provider=audit_event.provider,
            model=audit_event.model,
            intent_hash=authorized.record.intent_hash,
            label=label,
            evidence=evidence,
        )

    def _failure_closeout(
        self, authorized: Any, audit_event: AuditEvent, ref: str
    ) -> None:
        now = utc_now()
        audit = AuditRecordDraft(
            event_type="m6.apply.rejected",
            actor=audit_event.actor,
            surface=audit_event.surface,
            tool_name=authorized.definition.name,
            status="rejected",
            error_code="m6_apply_refused",
            document_id=None,
            session_id=authorized.session.id,
            approval_id=authorized.approval.id if authorized.approval else None,
            tool_call_id=authorized.record.id,
            intent_hash=authorized.record.intent_hash,
            label=f"M6 apply {ref} refused",
            evidence=dict(audit_event.metadata),
        )
        self._store.m6_apply_failure_closeout(
            tool_call=authorized.record.model_copy(
                update={"status": "failed", "error_code": "m6_apply_refused", "completed_at": now}
            ),
            session=authorized.session.model_copy(
                update={"status": "failed", "updated_at": now}
            ),
            audit=audit,
        )


class M6DomainAdapter:
    """Runtime ports for the M6 apply plane."""

    def __init__(self, executor: M6ApplyExecutor, store: SQLiteDocumentStore) -> None:
        self.executor = executor
        self._store = store

    def document_context(self, *, document_id: str) -> DocumentContext:
        stored = self._store.get(document_id)
        if stored is None:
            raise M6ApplyError(
                f"document {document_id!r} not found", code="target_not_found"
            )
        return DocumentContext(document_id=document_id, revision=stored.document.revision)

    def canonicalize_intent(self, tool_name: str, intent: Any) -> Any:
        if tool_name == TOOL_APPLY_M6_FINDING:
            parsed = ApplyM6PatchIntent.model_validate(intent)
            patch_row = self._store.get_structured_patch(parsed.patch_id)
            if patch_row is None:
                raise M6ApplyError(
                    f"no persisted patch {parsed.patch_id!r}", code="patch_not_found"
                )
            candidate = self.executor._candidate_for(patch_row)  # noqa: SLF001
            return apply_binding_material(patch_row, candidate)
        if tool_name == TOOL_REVERT_M6_APPLY:
            parsed = RevertM6ApplyIntent.model_validate(intent)
            apply_row = self._store.get_apply_record(parsed.apply_id)
            if apply_row is None:
                raise M6ApplyError(
                    f"no apply record {parsed.apply_id!r}", code="apply_record_not_found"
                )
            candidate = self._store.get_semantic_candidate(str(apply_row["candidate_id"]))
            if candidate is None:
                raise M6ApplyError(
                    "the apply record's candidate is missing",
                    code="apply_identity_chain_mismatch",
                )
            revert_patch, _digest, _patch_row, _inverse, _evidence = _compensation_patch_for(
                self._store, apply_row
            )
            return revert_binding_material(apply_row, candidate, revert_patch)
        return intent

    def preview_diff_hash(self, *, document_id: str, intent: Any) -> str:
        try:
            material = self.canonicalize_intent(TOOL_APPLY_M6_FINDING, intent)
            return canonical_digest(material)
        except Exception:  # preview must never block an approval
            return ""

    def approval_evidence(
        self, *, definition: ToolDefinitionView, canonical_intent: Any, diff_preview_hash: str
    ) -> dict[str, Any]:
        return {
            "tool": definition.name,
            "binding": canonical_intent,
            "diff_preview_hash": diff_preview_hash,
        }

    def rejection_evidence(
        self, *, definition: ToolDefinitionView, intent: Any, error_code: str
    ) -> dict[str, Any]:
        return {"tool_permission": definition.permission, "authorized": False}

    def closure_request(self, *, authorized: Any, intent: Any) -> ClosureRequest:
        return ClosureRequest(
            tool_call_id=authorized.record.id,
            consume_approval=authorized.approval is not None,
            close_session=True,
            metadata={},
        )

    def execute(
        self,
        *,
        authorized: Any,
        audit_event: AuditEvent,
        closure: ClosureRequest,
        intent: Any,
    ) -> ExecutionOutcome:
        if authorized.definition.name == TOOL_REVERT_M6_APPLY:
            return self.executor.revert_authorized(
                authorized=authorized,
                audit_event=audit_event,
                closure=closure,
                intent=RevertM6ApplyIntent.model_validate(intent),
            )
        return self.executor.apply_authorized(
            authorized=authorized,
            audit_event=audit_event,
            closure=closure,
            intent=ApplyM6PatchIntent.model_validate(intent),
        )


__all__ = [
    "TOOL_APPLY_M6_FINDING",
    "TOOL_REVERT_M6_APPLY",
    "ApplyM6PatchIntent",
    "M6ApplyError",
    "M6ApplyExecutor",
    "M6AuditAdapter",
    "M6DomainAdapter",
    "M6ToolRegistry",
    "RevertM6ApplyIntent",
    "apply_binding_material",
    "apply_record_id",
    "compensation_patch_identity",
    "inverse_operations",
    "parse_operations",
    "revert_binding_material",
]
