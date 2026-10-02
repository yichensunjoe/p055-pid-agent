"""M12-D3: Engineering-link DomainAdapter + tool registry + audit adapter.

Wires EngineeringLinkService into the M10 AgentHarnessRuntime (Charter
§55B(4) freeze, D79-1): the three link mutations are neutral runtime tools
(permission=ask, risk=engineering_change); the runtime owns session /
approval / authorization; the domain adapter executes through the atomic
store commits, so mutation + governance audit + tool-call closure + approval
consumption + session closure land in one transaction (D79-1) with every
frozen invariant re-checked inside the BEGIN IMMEDIATE write (D79-2).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import Field

from .audit import AuditRecorder
from .engineering_links import EngineeringLinkError, EngineeringLinkService
from .models import StrictModel
from .runtime.ports import (
    AuditEvent,
    AuditRecordRef,
    ClosureRequest,
    DocumentContext,
    ExecutionOutcome,
    ToolDefinitionView,
)

TOOL_CREATE_LINK = "create_engineering_link"
TOOL_REPIN_LINK = "repin_engineering_link"
TOOL_DELETE_LINK = "delete_engineering_link"


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CreateLinkIntent(StrictModel):
    project_id: str = Field(min_length=1)
    source_document_id: str = Field(min_length=1)
    source_object_ref: str = Field(min_length=1)
    source_endpoint: str = Field(min_length=1)
    target_document_id: str = Field(min_length=1)
    target_object_ref: str = Field(min_length=1)
    # Server-bound approval-time revisions (D79-2): injected by
    # canonicalize_intent from the current state, never caller-supplied.
    expected_source_revision: int | None = Field(default=None, ge=0)
    expected_target_revision: int | None = Field(default=None, ge=0)


class RepinLinkIntent(StrictModel):
    link_id: str = Field(min_length=1)
    expected_source_revision: int | None = Field(default=None, ge=0)
    expected_target_revision: int | None = Field(default=None, ge=0)


class DeleteLinkIntent(StrictModel):
    link_id: str = Field(min_length=1)


class ProjectLinkToolRegistry:
    """Neutral tool registry for the engineering-link plane (M12-D3): the
    runtime only sees ToolDefinitionView; never extends the P&ID catalogue."""

    _VIEWS = (
        ToolDefinitionView(
            name=TOOL_CREATE_LINK,
            description="Create one cable_endpoint_equipment cross-domain link (M12)",
            permission="ask",
            risk="engineering_change",
            audit_event="tool.project_link.create",
        ),
        ToolDefinitionView(
            name=TOOL_REPIN_LINK,
            description="Re-pin one engineering link to both endpoints' current revisions (M12)",
            permission="ask",
            risk="engineering_change",
            audit_event="tool.project_link.repin",
        ),
        ToolDefinitionView(
            name=TOOL_DELETE_LINK,
            description="Soft-delete one engineering link (M12 has no hard delete)",
            permission="ask",
            risk="engineering_change",
            audit_event="tool.project_link.delete",
        ),
    )

    def require(self, name: str) -> ToolDefinitionView:
        for view in self._VIEWS:
            if view.name == name:
                return view
        raise KeyError(f"unknown project-link tool: {name}")


class ProjectLinkAuditAdapter:
    """AuditPort for the engineering-link plane: runtime-level rejections
    recorded before any domain write, same field-parity semantics as the
    Cable audit adapter."""

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


class ProjectLinkDomainAdapter:
    def __init__(self, service: EngineeringLinkService) -> None:
        self.service = service

    # ------------------------------------------------------------------ reads

    def document_context(self, *, document_id: str) -> DocumentContext:
        # Links bind runtime authorization to the cable source document; the
        # plane revision there is the cable envelope revision.
        view = self.service._cable.load(document_id)  # noqa: SLF001 - same-package adapter
        return DocumentContext(document_id=document_id, revision=view.document.revision)

    def _canonical_intent(self, tool_name: str, intent: Any) -> Any:
        model = {
            TOOL_CREATE_LINK: CreateLinkIntent,
            TOOL_REPIN_LINK: RepinLinkIntent,
            TOOL_DELETE_LINK: DeleteLinkIntent,
        }.get(tool_name)
        if model is None or not isinstance(intent, dict):
            return intent
        return model.model_validate(intent).model_dump(mode="json")

    def canonicalize_intent(self, tool_name: str, intent: Any) -> Any:
        canonical = self._canonical_intent(tool_name, intent)
        if isinstance(canonical, dict):
            canonical = dict(canonical)
            canonical.pop("expected_source_revision", None)
            canonical.pop("expected_target_revision", None)
            if tool_name == TOOL_CREATE_LINK:
                from types import SimpleNamespace

                pins = self.service.resolve_current_pins(SimpleNamespace(**canonical))
                canonical["expected_source_revision"] = pins[0]
                canonical["expected_target_revision"] = pins[1]
            elif tool_name in (TOOL_REPIN_LINK, TOOL_DELETE_LINK):
                row = self.service._require_link_row(str(canonical["link_id"]))  # noqa: SLF001
                pins = self.service._resolve_pins(
                    project_id=str(row["project_id"]),
                    source_document_id=str(row["source_document_id"]),
                    source_object_ref=str(row["source_object_ref"]),
                    source_endpoint=str(row["source_endpoint"]),
                    target_document_id=str(row["target_document_id"]),
                    target_object_ref=str(row["target_object_ref"]),
                )
                if tool_name == TOOL_REPIN_LINK:
                    canonical["expected_source_revision"] = pins[0]
                    canonical["expected_target_revision"] = pins[1]
        return canonical

    def preview_diff_hash(self, *, document_id: str, intent: Any) -> str:
        """Deterministic intent hash; never blocks an approval."""
        try:
            material = {"document_id": document_id, "intent": self._canonicalize(intent)}
            return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
        except Exception:  # pragma: no cover - preview must never block an approval
            return ""

    def _canonicalize(self, intent: Any) -> Any:
        if isinstance(intent, dict) and "source_document_id" in intent:
            return CreateLinkIntent.model_validate(intent).model_dump(mode="json")
        if isinstance(intent, dict) and "link_id" in intent:
            # repin and delete share the {link_id} shape; the tool name in the
            # surrounding authorization disambiguates, the hash need not.
            return RepinLinkIntent.model_validate(intent).model_dump(mode="json")
        return intent

    def approval_evidence(
        self,
        *,
        definition: ToolDefinitionView,
        canonical_intent: Any,
        diff_preview_hash: str,
    ) -> dict[str, Any]:
        return {
            "tool": definition.name,
            "intent": canonical_intent,
            "diff_preview_hash": diff_preview_hash,
        }

    def rejection_evidence(
        self,
        *,
        definition: ToolDefinitionView,
        intent: Any,
        error_code: str,
    ) -> dict[str, Any]:
        return {"tool_permission": definition.permission, "authorized": False}

    def closure_request(self, *, authorized: Any, intent: Any) -> ClosureRequest:
        return ClosureRequest(
            tool_call_id=authorized.record.id,
            consume_approval=authorized.approval is not None,
            close_session=True,
            metadata={},
        )

    # --------------------------------------------------------------- execution

    def _approved_intent(self, authorized: Any, audit_event: AuditEvent, intent: Any) -> dict:
        """D79-2 state binding: re-canonicalize the caller intent and demand
        it hash-match the authorized record. canonicalize_intent enriches the
        server-bound expected revisions from the CURRENT state, so any state
        change since authorization (either endpoint moving revision) changes
        the hash and fails closed here — before any store write."""
        from .runtime.harness import tool_intent_hash

        name = authorized.definition.name
        try:
            canonical = self.canonicalize_intent(name, intent)
        except EngineeringLinkError as exc:
            # State changed since authorization (or the intent was tampered):
            # the harness was already authorized, so close it out honestly.
            self.service.failure_closeout(authorized, audit_event, exc.code)
            raise
        if (
            tool_intent_hash(name, authorized.record.document_id, canonical)
            != authorized.record.intent_hash
        ):
            self.service.failure_closeout(authorized, audit_event, "revision_conflict")
            raise EngineeringLinkError(
                "revision_conflict",
                "endpoint state changed since approval; re-request approval",
            )
        return canonical if isinstance(canonical, dict) else dict(intent)

    def execute(
        self,
        *,
        authorized: Any,
        audit_event: AuditEvent,
        closure: ClosureRequest,
        intent: Any,
    ) -> ExecutionOutcome:
        name = authorized.definition.name
        approved = self._approved_intent(authorized, audit_event, intent)
        if name == TOOL_CREATE_LINK:
            intent_model = CreateLinkIntent.model_validate(approved)
            try:
                self.service.preview_create(intent_model)
            except EngineeringLinkError as exc:
                self.service.failure_closeout(authorized, audit_event, exc.code)
                raise
            return self.service.execute_create(
                authorized=authorized,
                audit_event=audit_event,
                closure=closure,
                intent=intent_model,
            )
        if name == TOOL_REPIN_LINK:
            return self.service.execute_repin(
                authorized=authorized,
                audit_event=audit_event,
                closure=closure,
                intent=RepinLinkIntent.model_validate(approved),
            )
        if name == TOOL_DELETE_LINK:
            return self.service.execute_delete(
                authorized=authorized,
                audit_event=audit_event,
                closure=closure,
                intent=DeleteLinkIntent.model_validate(approved),
            )
        raise KeyError(f"unknown project-link tool: {name}")
