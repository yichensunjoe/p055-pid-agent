"""M13-D4: exact intent approval binding + atomic governed multi-domain executor.

Gate-frozen scope (M13-D4 CODE GO): neutral tool apply_project_change_set
(permission=ask, risk=engineering_change); approval binds project_id +
declared base pins + evaluation_as_of + ordered mutations + the D3 canonical
impacted snapshot / derived re-pins + the effective validation-profile
fingerprint; apply re-computes and compares all of it — any drift is a stable
change_set_conflict with zero engineering writes; a change-set row in
'approved' never authorizes by itself (C4: real M10 approval + intent-hash
match + unconsumed + C1 current pins); one BEGIN IMMEDIATE success transaction
carries every P&ID CAS write, every Cable CAS write, every existing-link
re-pin, the approved->applied transition, the in-transaction evidence
(including the snapshot readiness hash that must equal the post-commit D4
canonical hash) and the governance closeout with exactly one applied audit;
failures roll everything back and close out in a separate transaction
(refused + failed tool call + failed session + exactly one rejected audit).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from .audit import AuditRecorder
from .audit_models import AuditRecordDraft
from .cable_service import CableService
from .models import HistoryEntry, StrictModel
from .project_change_set import (
    MUTATION_PID_TRANSACTION,
    ChangeSetError,
    ChangeSetImpactAnalyzer,
    ChangeSetIntent,
    ChangeSetPreviewer,
    _parse_pid_operations,
    canonical_change_set_intent,
    change_set_intent_hash,
    validate_declared_pins,
)
from .runtime.ports import (
    AuditEvent,
    AuditRecordRef,
    ClosureRequest,
    DocumentContext,
    ExecutionOutcome,
    ToolDefinitionView,
)
from .service import DocumentService
from .store import (
    SQLiteDocumentStore,
)
from .validation_profile import load_profile

TOOL_APPLY_CHANGE_SET = "apply_project_change_set"


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


class ApplyChangeSetIntent(StrictModel):
    """Tool-level payload: which staged+approved change set to apply."""

    change_set_id: str = Field(min_length=1)
    intent: ChangeSetIntent


def binding_material(apply_intent: ApplyChangeSetIntent, impacted: dict[str, Any]) -> dict[str, Any]:
    """The exact approval binding (Gate-frozen): project_id + declared base
    pins + evaluation_as_of + ordered mutations + the D3 canonical impacted
    snapshot / derived re-pins + the effective validation-profile fingerprint.
    Change-set id rides along so a approval can never be replayed against a
    different change set row."""
    profile = load_profile()
    return {
        "change_set_id": apply_intent.change_set_id,
        "declared": json.loads(canonical_change_set_intent(apply_intent.intent)),
        "impacted": impacted,
        "profile_fingerprint": profile.fingerprint,
    }


def binding_hash(apply_intent: ApplyChangeSetIntent, impacted: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(binding_material(apply_intent, impacted)).encode("utf-8")).hexdigest()


class ProjectChangeToolRegistry:
    """Neutral registry for the change-set plane: the runtime only sees the
    ToolDefinitionView; never extends the P&ID catalogue."""

    _VIEW = ToolDefinitionView(
        name=TOOL_APPLY_CHANGE_SET,
        description="Atomically apply one approved multi-domain project change set (M13)",
        permission="ask",
        risk="engineering_change",
        audit_event="tool.project_change.apply",
    )

    def require(self, name: str) -> ToolDefinitionView:
        if name != self._VIEW.name:
            raise KeyError(f"unknown project-change tool: {name}")
        return self._VIEW


class ProjectChangeAuditAdapter:
    """AuditPort for the change-set plane, same field-parity semantics as the
    Cable / engineering-link adapters."""

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


@dataclass(frozen=True)
class StagedWrite:
    document_id: str
    domain: str
    base_revision: int
    result_revision: int
    data_json: str


class ChangeSetExecutor:
    """Drives store.commit_project_change_set with staged payloads and the
    two-transaction success/failure discipline. Only the runtime adapter
    calls apply_authorized — there is no direct-write entry point."""

    def __init__(
        self,
        store: SQLiteDocumentStore,
        pid_service: DocumentService,
        cable_service: CableService,
        analyzer: ChangeSetImpactAnalyzer,
        previewer: ChangeSetPreviewer,
    ) -> None:
        self._store = store
        self._pid = pid_service
        self._cable = cable_service
        self._analyzer = analyzer
        self._previewer = previewer

    def apply_authorized(
        self,
        *,
        authorized: Any,
        audit_event: AuditEvent,
        closure: ClosureRequest,
        intent: ApplyChangeSetIntent,
    ) -> ExecutionOutcome:
        store = self._store
        change_set_id = intent.change_set_id
        persisted_call = store.get_tool_call(authorized.record.id)
        if persisted_call is None or str(persisted_call.status) != "running":
            # Replay of an already-terminal execution: refuse WITHOUT
            # touching the completed records (D85-5) — no closeout, no new
            # audit; the refusal is this raised error itself.
            raise ChangeSetError(
                "change_set_not_authorized",
                "this authorization has already been used",
            )
        try:
            row = store.get_change_set(change_set_id)
            if row is None:
                raise ChangeSetError("change_set_not_found", f"change set {change_set_id!r} missing")
            # C4: an approved ROW is a persisted fact, never authorization.
            if str(row["status"]) != "approved":
                raise ChangeSetError(
                    "change_set_not_authorized",
                    f"change set {change_set_id!r} is not approved (C4)",
                )
            if str(row["intent_hash"]) != change_set_intent_hash(intent.intent):
                raise ChangeSetError(
                    "change_set_intent_mismatch",
                    "declared intent does not match the staged change set",
                )
            if authorized.approval is None or str(authorized.approval.status) != "approved":
                raise ChangeSetError(
                    "change_set_not_authorized",
                    "no live M10 approval bound to this execution (C4)",
                )
            # C1: base pins must STILL be current.
            validate_declared_pins(
                store,
                project_id=intent.intent.project_id,
                declared=intent.intent.base_member_pins,
            )
            impact = self._analyzer.analyze(intent.intent)
            material = binding_material(intent, impact.canonical())
            from .runtime.harness import tool_intent_hash

            binding_document_id = self._binding_document_id(intent)
            recomputed = tool_intent_hash(
                TOOL_APPLY_CHANGE_SET, binding_document_id, material
            )
            if recomputed != authorized.record.intent_hash:
                raise ChangeSetError(
                    "change_set_conflict",
                    "impacted / derived re-pins / profile drifted since approval",
                )

            if authorized.record.status != "running":
                # Replay of an already-terminal execution: refuse WITHOUT
                # touching the completed records (D85-5) — no closeout, no
                # new audit; the refusal is this raised error itself.
                raise ChangeSetError(
                    "change_set_not_authorized",
                    "this authorization has already been used",
                )
            staged = self._stage_writes(intent.intent)
            readiness_after = self._previewer._assess_staged(  # noqa: SLF001
                intent.intent,
                self._staged_members(intent.intent, staged),
                impact,
            )
            evidence_base = self._build_evidence(
                intent, impact, staged, readiness_after, authorized
            )
            full_result_pins = dict(intent.intent.base_member_pins)
            for write in staged:
                full_result_pins[write.document_id] = write.result_revision
            history_entries = [
                HistoryEntry(
                    document_id=write.document_id,
                    revision=write.result_revision,
                    source="mcp",
                    action="transaction",
                    label=f"Change set {change_set_id}",
                    operation_count=len(
                        [
                            m
                            for m in intent.intent.mutations
                            if m.document_id == write.document_id
                        ]
                    ),
                )
                for write in staged
                if write.domain == "pid"
            ]
            from .engineering_links import EngineeringLinkService

            link_plane = EngineeringLinkService(
                store, self._pid, self._cable
            )
            audit = self._success_audit(authorized, audit_event, intent, evidence_base)
            store.commit_project_change_set(
                change_set_id=change_set_id,
                project_id=intent.intent.project_id,
                pid_writes=[
                    (w.document_id, w.result_revision, w.data_json)
                    for w in staged
                    if w.domain == "pid"
                ],
                cable_writes=[
                    (w.document_id, w.result_revision, w.data_json)
                    for w in staged
                    if w.domain == "cable"
                ],
                repin_actions=[
                    (a.link_id, a.pinned_source_revision, a.pinned_target_revision)
                    for a in impact.derived_repin_actions
                ],
                declared_pins=dict(intent.intent.base_member_pins),
                member_identity=sorted(
                    (document_id, domain)
                    for document_id, domain, _added in store.list_project_documents(
                        intent.intent.project_id
                    )
                ),
                known_symbol_keys=link_plane._known_symbol_keys,  # noqa: SLF001
                instrument_symbol_keys=link_plane._instrument_symbol_keys,  # noqa: SLF001
                history_entries=history_entries,
                evidence_base=evidence_base,
                result_pins=_canonical(full_result_pins),
                history_limit=self._pid.history_limit,
                audit=audit,
                tool_call=authorized.record.model_copy(
                    update={"status": "completed", "completed_at": datetime.now(UTC)}
                ),
                approval=authorized.approval.model_copy(
                    update={"status": "consumed", "consumed_at": datetime.now(UTC)}
                ),
                session=authorized.session.model_copy(
                    update={"status": "completed", "updated_at": datetime.now(UTC)}
                )
                if closure.close_session
                else authorized.session,
            )
        except Exception:
            # D79-1 discipline: ANY failure of an authorized execution closes
            # the harness out honestly — the engineering transaction already
            # rolled back inside the store commit.
            self._failure_closeout(authorized, audit_event, change_set_id)
            raise
        return ExecutionOutcome(
            document_id=self._binding_document_id(intent),
            base_revision=None,
            result_revision=None,
            payload={
                "change_set_id": change_set_id,
                "result_pins": full_result_pins,
            },
        )

    # ---- staging (in-memory; writes happen only inside the store commit) ----

    def _stage_writes(self, intent: ChangeSetIntent) -> list[StagedWrite]:
        writes = []
        for mutation in intent.mutations:
            base = int(intent.base_member_pins[mutation.document_id])
            if mutation.kind == MUTATION_PID_TRANSACTION:
                document = self._pid.get_document(mutation.document_id)
                operations = _parse_pid_operations(mutation.payload)
                staged = self._pid._stage_mutation(document, operations)  # noqa: SLF001
                staged = staged.model_copy(update={"revision": base + 1})
                writes.append(
                    StagedWrite(
                        document_id=mutation.document_id,
                        domain="pid",
                        base_revision=base,
                        result_revision=base + 1,
                        data_json=staged.model_dump_json(),
                    )
                )
            else:
                from .cable_models import CableDocument, serialize_cable_payload

                staged_doc = CableDocument.model_validate(
                    {**mutation.payload, "revision": base + 1}
                )
                writes.append(
                    StagedWrite(
                        document_id=mutation.document_id,
                        domain="cable",
                        base_revision=base,
                        result_revision=base + 1,
                        data_json=serialize_cable_payload(staged_doc),
                    )
                )
        return writes

    def _staged_members(self, intent: ChangeSetIntent, staged: list[StagedWrite]):
        from .cable_models import CableDocument
        from .cable_service import CableDocumentView

        members = {}
        writes = {w.document_id: w for w in staged}
        for mutation in intent.mutations:
            write = writes[mutation.document_id]
            if write.domain == "pid":
                document = self._pid.get_document(mutation.document_id)
                operations = _parse_pid_operations(mutation.payload)
                staged_doc = self._pid._stage_mutation(document, operations)  # noqa: SLF001
                members[mutation.document_id] = (
                    "pid",
                    staged_doc.model_copy(update={"revision": write.result_revision}),
                )
            else:
                staged_doc = CableDocument.model_validate(
                    {**mutation.payload, "revision": write.result_revision}
                )
                members[mutation.document_id] = (
                    "cable",
                    CableDocumentView(document_id=mutation.document_id, document=staged_doc),
                )
        return members

    # ---- evidence / audits ----

    def _build_evidence(
        self, intent: ApplyChangeSetIntent, impact, staged, readiness_after, authorized
    ) -> dict[str, Any]:
        diffs = {}
        for mutation in intent.intent.mutations:
            if mutation.kind == MUTATION_PID_TRANSACTION:
                document = self._pid.get_document(mutation.document_id)
                operations = _parse_pid_operations(mutation.payload)
                after = self._pid._stage_mutation(document, operations)  # noqa: SLF001
                diffs[mutation.document_id] = ChangeSetPreviewer._pid_diff(document, after)
            else:
                from .cable_models import CableDocument

                view = self._cable.load(mutation.document_id)
                after = CableDocument.model_validate(
                    {**mutation.payload, "revision": view.document.revision}
                )
                diffs[mutation.document_id] = ChangeSetPreviewer._cable_diff(
                    view.document, after
                )
        link_snapshot = [
            {
                "link_id": action.link_id,
                "pinned_source_revision": action.pinned_source_revision,
                "pinned_target_revision": action.pinned_target_revision,
            }
            for action in impact.derived_repin_actions
        ]
        import hashlib as _hashlib

        def _sha(text: str) -> str:
            return _hashlib.sha256(text.encode("utf-8")).hexdigest()

        per_domain = {}
        writes_by_doc = {w.document_id: w for w in staged}
        for document_id, diff in diffs.items():
            write = writes_by_doc[document_id]
            per_domain[document_id] = {
                "semantic_diff_hash": _sha(_canonical(diff)),
                "staged_payload_hash": _sha(write.data_json),
            }
        return {
            "before_pins": dict(intent.intent.base_member_pins),
            "after_pins": {
                **intent.intent.base_member_pins,
                **{w.document_id: w.result_revision for w in staged},
            },
            "per_domain": per_domain,
            "per_domain_diffs": diffs,
            "link_snapshot": link_snapshot,
            "readiness_result_hash": readiness_after.result_hash,
            "evaluation_as_of": intent.intent.evaluation_as_of.isoformat(),
            "intent_hash_declared": change_set_intent_hash(intent.intent),
            "session_id": authorized.session.id,
            "approval_id": authorized.approval.id if authorized.approval else None,
            "tool_call_id": authorized.record.id,
        }

    def _success_audit(self, authorized, audit_event, intent, evidence) -> AuditRecordDraft:
        from .audit import validation_evidence_hash

        return AuditRecordDraft(
            event_type="project_change_set.applied",
            actor=audit_event.actor,
            surface=audit_event.surface,
            tool_name=authorized.definition.name,
            status="applied",
            project_id=intent.intent.project_id,
            session_id=authorized.session.id,
            approval_id=authorized.approval.id if authorized.approval else None,
            tool_call_id=authorized.record.id,
            provider=audit_event.provider,
            model=audit_event.model,
            intent_hash=authorized.record.intent_hash,
            validation_status="valid",
            validation_hash=validation_evidence_hash(audit_event.validation_evidence),
            label=f"Applied change set {intent.change_set_id}",
            evidence=evidence,
        )

    def _failure_closeout(self, authorized, audit_event, change_set_id: str) -> None:
        from .audit import validation_evidence_hash

        now = datetime.now(UTC)
        code = "change_set_refused"
        audit = AuditRecordDraft(
            event_type="project_change_set.rejected",
            actor=audit_event.actor,
            surface=audit_event.surface,
            tool_name=authorized.definition.name,
            status="rejected",
            error_code=code,
            project_id=None,
            session_id=authorized.session.id,
            approval_id=authorized.approval.id if authorized.approval else None,
            tool_call_id=authorized.record.id,
            provider=audit_event.provider,
            model=audit_event.model,
            intent_hash=authorized.record.intent_hash,
            validation_status=audit_event.validation_status,
            validation_hash=validation_evidence_hash(audit_event.validation_evidence),
            label=f"Change set {change_set_id} refused: {code}",
            evidence=dict(audit_event.metadata),
        )
        self._store.change_set_failure_closeout(
            change_set_id=change_set_id,
            tool_call=authorized.record.model_copy(
                update={"status": "failed", "error_code": code, "completed_at": now}
            ),
            session=authorized.session.model_copy(
                update={"status": "failed", "updated_at": now}
            ),
            audit=audit,
        )

    @staticmethod
    def _binding_document_id(intent: ApplyChangeSetIntent) -> str:
        # Session binding anchors to the first mutated document (canonical
        # order); the real authorization guard is the intent-hash binding.
        return sorted(m.document_id for m in intent.intent.mutations)[0]


class ProjectChangeDomainAdapter:
    """Runtime ports for the change-set plane."""

    def __init__(self, executor: ChangeSetExecutor, store: SQLiteDocumentStore) -> None:
        self.executor = executor
        self._store = store

    # ------------------------------------------------------------------ reads

    def document_context(self, *, document_id: str) -> DocumentContext:
        envelope = self._store.get_cable_envelope(document_id)
        if envelope is not None:
            return DocumentContext(document_id=document_id, revision=int(envelope[0]))
        stored = self._store.get(document_id)
        if stored is None:
            raise ChangeSetError("impact_unknown_object", f"document {document_id!r} not found")
        return DocumentContext(document_id=document_id, revision=stored.document.revision)

    def canonicalize_intent(self, tool_name: str, intent: Any) -> Any:
        if tool_name != TOOL_APPLY_CHANGE_SET:
            return intent
        apply_intent = ApplyChangeSetIntent.model_validate(intent)
        impact = self.executor._analyzer.analyze(apply_intent.intent)  # noqa: SLF001
        return binding_material(apply_intent, impact.canonical())

    def preview_diff_hash(self, *, document_id: str, intent: Any) -> str:
        try:
            apply_intent = ApplyChangeSetIntent.model_validate(intent)
            material = self.canonicalize_intent(TOOL_APPLY_CHANGE_SET, apply_intent)
            return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()
        except Exception:  # preview must never block an approval
            return ""

    def approval_evidence(self, *, definition, canonical_intent, diff_preview_hash) -> dict:
        return {
            "tool": definition.name,
            "impacted": canonical_intent.get("impacted", {}),
            "diff_preview_hash": diff_preview_hash,
        }

    def rejection_evidence(self, *, definition, intent, error_code) -> dict:
        return {"tool_permission": definition.permission, "authorized": False}

    def closure_request(self, *, authorized, intent) -> ClosureRequest:
        return ClosureRequest(
            tool_call_id=authorized.record.id,
            consume_approval=authorized.approval is not None,
            close_session=True,
            metadata={},
        )

    # --------------------------------------------------------------- execution

    def execute(self, *, authorized, audit_event, closure, intent) -> ExecutionOutcome:
        # D79-2 discipline: the caller re-passes the tool payload; the
        # executor re-canonicalizes server-derived material from the CURRENT
        # state and hash-compares against the authorized record — any drift
        # (pins, impacted, re-pins, profile) is a stable conflict with zero
        # engineering writes. There is nothing to "merge back" because the
        # approval record stores only the intent_hash, never the material.
        apply_intent = ApplyChangeSetIntent.model_validate(intent)
        return self.executor.apply_authorized(
            authorized=authorized,
            audit_event=audit_event,
            closure=closure,
            intent=apply_intent,
        )
