"""Audit / provenance recorder (Charter §19, Priority 0 / T0.5).

Responsibilities
----------------
1. Build the history details (raw diff + engineering semantic diff) for one change
   so the store can persist document revision, history details and audit record in a
   single transaction.
2. Bind the exact approved intent (``intent_hash``, enforced by the Harness) to the
   semantic diff that actually landed (``diff_hash``) and to the validation evidence
   that authorised the write.
3. Verify the append-only hash chain and produce a review evidence package.

Honest boundaries (also stated in ``docs/audit-and-provenance.md``):
* the chain is *tamper-evident*, not tamper-proof against an operator who holds
  write access to the SQLite file;
* ``diff_binding`` reports a comparison, it does not replace the hard intent gate;
* only hashes, counts, identifiers and stable codes are stored, never prompts,
  context, document bodies or credentials.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .audit_hash import (
    GENESIS_HASH,
    audit_chain_metadata,
    compute_record_hash,
    payload_hash,
)
from .audit_models import (
    AuditContext,
    AuditDivergence,
    AuditRecord,
    AuditRecordDraft,
    AuditStatus,
    AuditVerification,
    ProvenanceBundle,
    ProvenanceState,
    RevisionEvidence,
)
from .history_diff import build_history_details
from .models import Document, TransactionRequest
from .semantic_diff import build_semantic_diff
from .semantic_diff_models import SemanticDiffReport

_MAX_EVIDENCE_ENTITY_IDS = 200


def new_audit_record_id() -> str:
    return f"audit_{uuid4().hex}"


def semantic_diff_hash(report: SemanticDiffReport | dict[str, Any]) -> str:
    payload = report.model_dump(mode="json") if isinstance(report, SemanticDiffReport) else report
    return payload_hash(payload)


def validation_evidence_hash(evidence: dict[str, Any] | None) -> str:
    if not evidence:
        return ""
    return payload_hash({"validation": evidence})


def diff_binding_verdict(diff_hash: str, diff_preview_hash: str) -> str:
    if not diff_hash or not diff_preview_hash:
        return "not_recorded"
    return "matched" if diff_hash == diff_preview_hash else "mismatched"


def _semantic_diff_summary(report: SemanticDiffReport) -> dict[str, Any]:
    kinds: dict[str, int] = {}
    risk_hints: dict[str, int] = {}
    for change in report.changes:
        kinds[change.entity_kind] = kinds.get(change.entity_kind, 0) + 1
        risk_hints[change.risk_hint] = risk_hints.get(change.risk_hint, 0) + 1
    return {
        "schema": report.schema,
        "version": report.version,
        "change_count": report.change_count,
        "engineering_change_count": report.engineering_change_count,
        "critical_change_count": report.critical_change_count,
        "draft_edit_count": report.draft_edit_count,
        "truncated": report.truncated,
        "entity_kinds": dict(sorted(kinds.items())),
        "risk_hints": dict(sorted(risk_hints.items())),
        "display_summaries": [change.summary for change in report.changes[:20]],
    }


def request_audit_context(
    tool_name: str,
    *,
    actor: str = "web-user",
    surface: str = "rest",
    label: str = "",
    session_id: str | None = None,
    approval_id: str | None = None,
    tool_call_id: str | None = None,
    provider: str = "",
    model: str = "",
    intent_hash: str = "",
    diff_preview_hash: str = "",
    validation_status: str = "not_run",
    validation_evidence: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditContext:
    """Build a server-derived audit context for one adapter call.

    ``actor``/``surface`` come from the adapter that owns the entry point, never from
    the request body, and the ambient request id is attached for correlated review.
    """
    from .request_context import current_request_context

    request_context = current_request_context()
    ambient = request_context.as_metadata() if request_context else {}
    return AuditContext(
        actor=actor,
        surface=surface,  # type: ignore[arg-type]
        tool_name=tool_name,
        session_id=session_id,
        approval_id=approval_id,
        tool_call_id=tool_call_id,
        provider=provider,
        model=model,
        request_id=request_context.request_id if request_context else "",
        intent_hash=intent_hash,
        diff_preview_hash=diff_preview_hash,
        validation_status=validation_status,  # type: ignore[arg-type]
        validation_evidence=validation_evidence or {},
        label=label,
        metadata={**ambient, **(metadata or {})},
    )


class AuditRecorder:
    """Single entry point for building and persisting provenance."""

    def __init__(self, store, symbols=None, service=None) -> None:
        self.store = store
        self.symbols = symbols
        self.service = service

    # ------------------------------------------------------------------ building

    def build_revision_provenance(
        self,
        *,
        before: Document,
        after: Document,
        operations: Iterable[Any] | None,
        request: TransactionRequest | None,
        action: str,
        source: str,
        context: AuditContext,
        status: AuditStatus = "applied",
        extra_evidence: dict[str, Any] | None = None,
    ) -> ProvenanceBundle:
        operation_list = list(operations) if operations is not None else None
        details = build_history_details(before, after, operation_list, action=action)
        semantic_diff = build_semantic_diff(before, after, details, self.symbols)
        details["semantic_diff"] = semantic_diff.model_dump(mode="json")
        affinity = self._diff_and_hashes(semantic_diff, context)
        evidence = {
            "action": action,
            "history_source": source,
            "attribution": context.summary(),
            "semantic_diff": _semantic_diff_summary(semantic_diff),
            "element_count_before": len(before.elements),
            "element_count_after": len(after.elements),
            "change_count": details.get("change_count", 0),
            "diff_truncated": bool(details.get("diff_truncated", False)),
            "changed_entity_ids": list(details.get("affected_element_ids", []))[
                :_MAX_EVIDENCE_ENTITY_IDS
            ],
            "changed_entity_ids_truncated": len(details.get("affected_element_ids", []))
            > _MAX_EVIDENCE_ENTITY_IDS,
            "added_element_ids": list(details.get("added_element_ids", []))[
                :_MAX_EVIDENCE_ENTITY_IDS
            ],
            "updated_element_ids": list(details.get("updated_element_ids", []))[
                :_MAX_EVIDENCE_ENTITY_IDS
            ],
            "deleted_element_ids": list(details.get("deleted_element_ids", []))[
                :_MAX_EVIDENCE_ENTITY_IDS
            ],
            "operation_count": len(operation_list or []),
            "validation": self._bounded_validation(context),
            "metadata": context.metadata,
        }
        if extra_evidence:
            # Caller-supplied *server-derived* evidence (today: CAD source binding).
            # Merged last so it cannot be silently shadowed by the generic keys above.
            evidence.update(extra_evidence)
        draft = AuditRecordDraft(
            event_type="revision.created",
            actor=context.actor,
            surface=context.surface,
            tool_name=context.tool_name,
            status=status,
            document_id=after.id,
            project_id=context.project_id,
            base_revision=before.revision,
            result_revision=after.revision if status == "applied" else None,
            session_id=context.session_id,
            approval_id=context.approval_id,
            tool_call_id=context.tool_call_id,
            provider=context.provider,
            model=context.model,
            label=context.label or (request.label if request else action),
            intent_hash=context.intent_hash,
            diff_hash=affinity["diff_hash"],
            diff_preview_hash=context.diff_preview_hash,
            diff_binding=affinity["diff_binding"],  # type: ignore[arg-type]
            validation_status=context.validation_status,
            validation_hash=affinity["validation_hash"],
            evidence=evidence,
        )
        return ProvenanceBundle(history_details=details, audit=draft)

    def diff_hash_for_transaction(
        self,
        *,
        before: Document,
        request: TransactionRequest,
    ) -> str:
        """Preview-time semantic diff hash, bound to an approval request.

        Uses the same deterministic preview path as the review UI so an approver can
        compare "what I approved" with "what landed".
        """
        from .semantic_diff import preview_transaction_semantic_diff

        if self.service is None:
            return ""
        report = preview_transaction_semantic_diff(self.service, before.id, request)
        return semantic_diff_hash(report)

    def build_event(
        self,
        event_type: str,
        context: AuditContext,
        *,
        status: str = "applied",
        document_id: str | None = None,
        base_revision: int | None = None,
        result_revision: int | None = None,
        evidence: dict[str, Any] | None = None,
        error_code: str = "",
    ) -> AuditRecordDraft:
        payload = {
            "attribution": context.summary(),
            "validation": self._bounded_validation(context),
            "metadata": context.metadata,
            **(evidence or {}),
        }
        return AuditRecordDraft(
            event_type=event_type,  # type: ignore[arg-type]
            actor=context.actor,
            surface=context.surface,
            tool_name=context.tool_name,
            status=status,  # type: ignore[arg-type]
            document_id=document_id,
            project_id=context.project_id,
            base_revision=base_revision,
            result_revision=result_revision,
            session_id=context.session_id,
            approval_id=context.approval_id,
            tool_call_id=context.tool_call_id,
            provider=context.provider,
            model=context.model,
            label=context.label,
            intent_hash=context.intent_hash,
            validation_status=context.validation_status,
            validation_hash=validation_evidence_hash(context.validation_evidence),
            evidence=payload,
            error_code=error_code,
        )

    def record_event(
        self,
        event_type: str,
        context: AuditContext,
        **kwargs: Any,
    ) -> AuditRecord:
        return self.store.record_audit_event(self.build_event(event_type, context, **kwargs))

    def finalize_state(
        self,
        state: ProvenanceState | None,
        *,
        result_revision: int | None,
    ) -> ProvenanceBundle:
        """Resolve tool call completion / approval consumption / session closure.

        Called by ``DocumentService`` immediately before the atomic write so these
        updates land in the same transaction as the revision they describe.
        """
        if state is None or state.tool_call_id is None:
            return ProvenanceBundle()
        record = self.store.get_tool_call(state.tool_call_id)
        if record is None:  # pragma: no cover - defensive, ids always come from authorize
            return ProvenanceBundle()
        now = datetime.now(UTC)
        tool_call = record.model_copy(
            update={
                "status": "completed",
                "result_revision": result_revision,
                "completed_at": now,
                "metadata": {**record.metadata, **state.tool_call_metadata},
            }
        )
        approval = None
        if record.approval_id and state.consume_approval:
            current_approval = self.store.get_tool_approval(record.approval_id)
            if current_approval is not None:
                approval = current_approval.model_copy(
                    update={"status": "consumed", "consumed_at": now}
                )
        session = None
        if state.close_session:
            current_session = self.store.get_agent_session(record.session_id)
            if current_session is not None:
                session = current_session.model_copy(
                    update={
                        "status": "completed",
                        "end_revision": result_revision,
                        "updated_at": now,
                    }
                )
        return ProvenanceBundle(tool_call=tool_call, approval=approval, session=session)

    def record_failure(
        self,
        *,
        context: AuditContext,
        event_type: str,
        error_code: str,
        document_id: str | None = None,
        base_revision: int | None = None,
        status: str = "failed",
        evidence: dict[str, Any] | None = None,
        state: ProvenanceState | None = None,
    ) -> AuditRecord:
        """Record a rejected/failed engineering attempt and close its harness state.

        There is no document write in this path, so the audit row and the harness
        state updates are still committed together, in one transaction.
        """
        draft = self.build_event(
            event_type,
            context,
            status=status,
            error_code=error_code,
            document_id=document_id,
            base_revision=base_revision,
            evidence=evidence,
        )
        tool_call = None
        session = None
        if state is not None and state.tool_call_id:
            record = self.store.get_tool_call(state.tool_call_id)
            if record is not None:
                now = datetime.now(UTC)
                tool_call = record.model_copy(
                    update={
                        "status": "failed",
                        "error_code": error_code,
                        "completed_at": now,
                        "metadata": {**record.metadata, **state.tool_call_metadata},
                    }
                )
                if state.close_session:
                    current_session = self.store.get_agent_session(record.session_id)
                    if current_session is not None:
                        session = current_session.model_copy(
                            update={
                                "status": "failed",
                                "end_revision": None,
                                "updated_at": now,
                            }
                        )
        return self.store.record_audit_event(draft, tool_call=tool_call, session=session)

    def record_rejection(
        self,
        context: AuditContext,
        *,
        event_type: str,
        error_code: str,
        document_id: str | None = None,
        base_revision: int | None = None,
        status: str = "rejected",
        evidence: dict[str, Any] | None = None,
    ) -> AuditRecord:
        return self.record_event(
            event_type,
            context,
            status=status,
            error_code=error_code,
            document_id=document_id,
            base_revision=base_revision,
            evidence=evidence,
        )

    # ------------------------------------------------------------------ reading

    def audit_trail(
        self,
        *,
        document_id: str | None = None,
        event_type: str | None = None,
        actor: str | None = None,
        status: str | None = None,
        tool_name: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 200,
    ) -> list[AuditRecord]:
        return self.store.list_audit_records(
            document_id=document_id,
            event_type=event_type,
            actor=actor,
            status=status,
            tool_name=tool_name,
            since=since,
            until=until,
            limit=limit,
        )

    def verify_chain(self, *, limit: int | None = None) -> AuditVerification:
        """Recompute the whole chain and report the first divergence."""
        records = self.store.all_audit_records()
        if limit is not None:
            records = records[: max(1, limit)]
        prev_hash = GENESIS_HASH
        expected_ordinal = 1
        for record in records:
            if record.ordinal != expected_ordinal:
                return AuditVerification(
                    ok=False,
                    record_count=len(records),
                    database_instance_id=self._instance_id(),
                    first_divergence=AuditDivergence(
                        ordinal=record.ordinal,
                        record_id=record.record_id,
                        reason="ordinal_gap",
                        expected_hash=prev_hash,
                        actual_hash=record.prev_hash,
                    ),
                )
            if record.prev_hash != prev_hash:
                return AuditVerification(
                    ok=False,
                    record_count=len(records),
                    database_instance_id=self._instance_id(),
                    first_divergence=AuditDivergence(
                        ordinal=record.ordinal,
                        record_id=record.record_id,
                        reason="broken_link",
                        expected_hash=prev_hash,
                        actual_hash=record.prev_hash,
                    ),
                )
            recomputed = compute_record_hash(record, prev_hash)
            if recomputed != record.record_hash:
                return AuditVerification(
                    ok=False,
                    record_count=len(records),
                    database_instance_id=self._instance_id(),
                    first_divergence=AuditDivergence(
                        ordinal=record.ordinal,
                        record_id=record.record_id,
                        reason="hash_mismatch",
                        expected_hash=recomputed,
                        actual_hash=record.record_hash,
                    ),
                )
            prev_hash = record.record_hash
            expected_ordinal += 1
        return AuditVerification(
            ok=True,
            record_count=len(records),
            database_instance_id=self._instance_id(),
        )

    def revision_evidence(self, document_id: str, revision: int) -> RevisionEvidence:
        """Rebuild the review bundle for one revision and re-check its hashes."""
        record = self.store.get_audit_record_for_revision(document_id, revision)
        history = self.store.get_history_revision_detailed(document_id, revision)
        details = dict(history.get("details", {})) if history else {}
        raw_diff = details.pop("semantic_diff", None)
        semantic_payload = raw_diff if isinstance(raw_diff, dict) else None
        recomputed_diff_hash = payload_hash(semantic_payload) if semantic_payload else ""
        validation = {}
        if record is not None:
            validation = record.evidence.get("validation", {}) if isinstance(record.evidence, dict) else {}
        recomputed_validation_hash = validation_evidence_hash(validation)
        stored_diff_hash = record.diff_hash if record else ""
        stored_validation_hash = record.validation_hash if record else ""
        return RevisionEvidence(
            document_id=document_id,
            revision=revision,
            audit_record=record,
            history_details=details,
            semantic_diff=semantic_payload,
            session=self._safe_session(record.session_id if record else None),
            approval=self._safe_approval(record.approval_id if record else None),
            tool_call=self._safe_tool_call(record.tool_call_id if record else None),
            recomputed_diff_hash=recomputed_diff_hash,
            recomputed_validation_hash=recomputed_validation_hash,
            diff_hash_matches=(
                None
                if not stored_diff_hash and not recomputed_diff_hash
                else stored_diff_hash == recomputed_diff_hash
            ),
            validation_hash_matches=(
                None
                if not stored_validation_hash and not recomputed_validation_hash
                else stored_validation_hash == recomputed_validation_hash
            ),
            chain_verified=self.verify_chain().ok,
        )

    def export_package(
        self,
        *,
        document_id: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Machine-readable evidence package for an external reviewer."""
        verification = self.verify_chain()
        all_records = self.store.all_audit_records()
        records = self.audit_trail(document_id=document_id, limit=limit)
        revisions: dict[str, list[int]] = {}
        for record in records:
            if record.result_revision is None or record.document_id is None:
                continue
            revisions.setdefault(record.document_id, []).append(record.result_revision)
        history_index: list[dict[str, Any]] = []
        for revision_document_id, revision_numbers in sorted(revisions.items()):
            for revision in sorted(set(revision_numbers)):
                evidence = self.revision_evidence(revision_document_id, revision)
                history_index.append(
                    {
                        "document_id": revision_document_id,
                        "revision": revision,
                        "audit_record_id": evidence.audit_record.record_id
                        if evidence.audit_record
                        else None,
                        "diff_hash": evidence.audit_record.diff_hash if evidence.audit_record else "",
                        "diff_hash_matches": evidence.diff_hash_matches,
                        "validation_hash_matches": evidence.validation_hash_matches,
                    }
                )
        return {
            "schema": "pid-agent.audit-evidence-package",
            "version": 1,
            "generated_at": datetime.now().astimezone().isoformat(),
            "database": {
                "instance_id": self._instance_id(),
                "schema_version": self.store.schema_version,
            },
            "scope": {"document_id": document_id, "record_limit": limit},
            "chain": audit_chain_metadata(),
            # Anchor: a hash chain alone cannot prove that its newest records still
            # exist. Publishing the head hash and the full chain length lets a later
            # reviewer detect tail truncation by comparing two packages.
            "chain_head_hash": all_records[-1].record_hash if all_records else GENESIS_HASH,
            "chain_length": len(all_records),
            "chain_head_ordinal": all_records[-1].ordinal if all_records else 0,
            "verification": verification.model_dump(mode="json"),
            "records": [record.model_dump(mode="json") for record in records],
            "revisions": history_index,
            "privacy": {
                "prompts_recorded": False,
                "model_context_recorded": False,
                "document_bodies_recorded": False,
                "credentials_recorded": False,
                "fields_stored": ["hashes", "counts", "identifiers", "stable_codes"],
            },
        }

    # ------------------------------------------------------------------ helpers

    def _diff_and_hashes(
        self,
        semantic_diff: SemanticDiffReport,
        context: AuditContext,
    ) -> dict[str, str]:
        diff_hash = semantic_diff_hash(semantic_diff)
        return {
            "diff_hash": diff_hash,
            "diff_binding": diff_binding_verdict(diff_hash, context.diff_preview_hash),
            "validation_hash": validation_evidence_hash(context.validation_evidence),
        }

    def _bounded_validation(self, context: AuditContext) -> dict[str, Any]:
        if not context.validation_evidence:
            return {}
        bounded = dict(context.validation_evidence)
        issues = bounded.get("issue_codes")
        if isinstance(issues, list) and len(issues) > 50:
            bounded["issue_codes"] = issues[:50]
            bounded["issue_codes_truncated"] = True
        return bounded

    def _instance_id(self) -> str:
        try:
            return self.store.database_instance_id
        except Exception:  # pragma: no cover - defensive, store may be unavailable
            return ""

    def _safe_session(self, session_id: str | None):
        if not session_id:
            return None
        try:
            return self.store.get_agent_session(session_id)
        except Exception:  # pragma: no cover - defensive
            return None

    def _safe_approval(self, approval_id: str | None):
        if not approval_id:
            return None
        try:
            return self.store.get_tool_approval(approval_id)
        except Exception:  # pragma: no cover - defensive
            return None

    def _safe_tool_call(self, tool_call_id: str | None):
        if not tool_call_id:
            return None
        try:
            return self.store.get_tool_call(tool_call_id)
        except Exception:  # pragma: no cover - defensive
            return None


__all__ = [
    "AuditRecorder",
    "diff_binding_verdict",
    "new_audit_record_id",
    "request_audit_context",
    "semantic_diff_hash",
    "validation_evidence_hash",
]
