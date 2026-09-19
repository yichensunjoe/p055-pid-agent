from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import __version__
from .agent_semantic import analyze_transaction as analyze_low_level
from .agent_semantic_models import SemanticTransaction
from .audit import request_audit_context
from .auto_layout_engine import AutoLayoutEngine
from .config import Settings
from .diagnostics import DiagnosticLogger
from .drafting_engine import DraftingEngine
from .drafting_models import DraftingRequest
from .engineering_ir import build_engineering_graph
from .harness import AgentHarnessService
from .harness_models import (
    AgentSessionCreateRequest,
    ToolApprovalCreateRequest,
    ToolApprovalResolveRequest,
)
from .layout_models import AutoLayoutRequest
from .models import CreateDocumentRequest, TransactionRequest
from .project_index import ProjectIndexService, rebuild_with_evidence
from .revision_diagnostics import emit_revision_diagnostics
from .semantic_compiler_engine import SemanticTransactionCompiler
from .semantic_diff import preview_transaction_semantic_diff
from .service import DocumentService, InvalidOperationError
from .store import SQLiteDocumentStore
from .symbols import SymbolRegistry
from .tool_registry import get_default_tool_registry


def _parse_cad_frame(raw: str) -> tuple[float, float, float, float] | None:
    """Parse an ``x0,y0,x1,y1`` crop window for the CAD import tool."""

    text = (raw or "").strip()
    if not text:
        return None
    parts = [part.strip() for part in text.replace(";", ",").split(",")]
    if len(parts) != 4:
        raise InvalidOperationError("frame must be four comma-separated numbers: x0,y0,x1,y1")
    try:
        x0, y0, x1, y1 = (float(part) for part in parts)
    except ValueError as exc:
        raise InvalidOperationError("frame values must be numbers") from exc
    if not (x1 > x0 and y1 > y0):
        raise InvalidOperationError("frame must satisfy x1 > x0 and y1 > y0")
    return (x0, y0, x1, y1)


#: Read-only tools and the audit *read* event each one records. Named here so the tool
#: layer cannot invent a third kind of event, and so no read tool can look like a write.
_READ_EVENT_TYPES = {
    "validate_document": "validation.completed",
    "assess_release_readiness": "release.readiness.assessed",
}


def _parse_as_of(raw: str):
    """Parse an optional, timezone-aware evaluation time for waiver expiry.

    Parsing is local (MCP receives a string), but the *contract* is the engine's: a naive
    value is refused with the same stable code REST and the CLI use, and an aware value is
    normalized to UTC by the engine before it is hashed (R3 P0-2).
    """

    from datetime import datetime

    from .validation_engine import ValidationTimeError, normalize_evaluation_time

    text = (raw or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidOperationError(f"invalid_as_of: {exc}") from exc
    try:
        return normalize_evaluation_time(moment)
    except ValidationTimeError as exc:
        raise InvalidOperationError(
            f"{exc.code}: as_of must carry a timezone, e.g. 2026-09-19T12:00:00Z"
        ) from exc


def emit_read_diagnostics(
    service: DocumentService,
    document,
    *,
    tool_name: str,
    label: str,
    extra: dict[str, Any],
) -> None:
    """Record one read/tool invocation as audit evidence.

    Validation is a read, so this is deliberately **not** a ``revision.created`` event:
    running a validator must not appear in document history as if the drawing changed.
    The event references the canonical hashes instead, which is what a reviewer needs in
    order to bind "what was checked" to "what was found".
    """

    service.audit.record_event(
        _READ_EVENT_TYPES[tool_name],
        request_audit_context(
            tool_name,
            actor="mcp-agent",
            surface="mcp",
            label=label,
            metadata={"surface": "mcp"},
        ),
        document_id=document.id,
        base_revision=document.revision,
        status="applied",
        evidence=extra,
    )


def build_service(settings: Settings | None = None) -> DocumentService:
    settings = settings or Settings.from_env()
    symbols = SymbolRegistry()
    return DocumentService(SQLiteDocumentStore(settings.database_path), symbols)


def _validate_transaction(
    service: DocumentService,
    document_id: str,
    transaction: TransactionRequest,
) -> dict[str, Any]:
    assessment = analyze_low_level(service, document_id, transaction)
    if not assessment.valid:
        issue = assessment.issues[0]
        raise InvalidOperationError(
            f"{issue.field_path} ({issue.code}): {issue.message}; "
            f"suggestions={issue.suggestions}"
        )
    return {
        "valid": True,
        "document_id": assessment.document_id,
        "current_revision": assessment.current_revision,
        "next_revision": assessment.next_revision,
        "operation_count": assessment.compiled_operation_count,
        "resulting_element_count": assessment.resulting_element_count,
        "affected_element_ids": assessment.affected_element_ids,
        "added_element_ids": assessment.added_element_ids,
        "updated_element_ids": assessment.updated_element_ids,
        "deleted_element_ids": assessment.deleted_element_ids,
        "issues": [],
    }


def _tool_registry_catalog() -> dict[str, Any]:
    """Return the same canonical tool metadata used by REST and internal Agent surfaces."""
    return get_default_tool_registry().catalog()


def _server_info(
    settings: Settings,
    service: DocumentService,
    transport: str,
    diagnostics: DiagnosticLogger,
) -> dict[str, Any]:
    database_path = settings.database_path.expanduser().resolve()
    symbol_paths = [str(path.expanduser().resolve()) for path in service.symbols._search_paths]
    return {
        "service": "P&ID-Agent",
        "version": __version__,
        "transport": transport,
        "database_path": str(database_path),
        "database_instance_id": service.store.database_instance_id,
        "diagnostics": diagnostics.info(),
        "document_count": len(service.list_documents()),
        "symbol_count": len(service.symbols.list()),
        "symbol_paths": symbol_paths,
    }


def _apply_governed(
    harness: AgentHarnessService,
    diagnostics: DiagnosticLogger,
    document_id: str,
    transaction: TransactionRequest,
    *,
    authorized,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply one approved MCP tool call through the single governed write path.

    MCP must not have its own write path: revision, semantic diff, audit record and
    harness close-out (tool call completed, approval consumed, session closed) all
    commit inside one SQLite transaction, exactly as on every other surface.
    """
    result = harness.apply_authorized(
        authorized,
        document_id,
        transaction,
        metadata=metadata,
    )
    emit_revision_diagnostics(
        harness.service,
        result.document,
        action="transaction",
        source="mcp",
        diagnostics=diagnostics,
        label=transaction.label,
        operation_count=len(transaction.operations),
        extra={
            "session_id": authorized.session.id,
            "approval_id": authorized.record.approval_id,
            "tool_call_id": authorized.record.id,
        },
    )
    return result.model_dump(mode="json")


def build_mcp_server(settings: Settings | None = None) -> tuple[Any, str]:
    """Build the MCP server and every tool it publishes, without starting a transport.

    Returning the server, rather than only running it, is what makes this surface
    testable: the cross-surface parity test calls the registered tool functions and
    compares their payloads against REST and the CLI byte for byte, which is only possible
    if a test can build the tools without blocking on stdio (M4 R3 P0-4).
    """

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise SystemExit("Install MCP support with: pip install 'pid-agent[mcp]'" ) from exc

    settings = settings or Settings.from_env()
    service = build_service(settings)
    semantic_compiler = SemanticTransactionCompiler(service)
    layout_engine = AutoLayoutEngine(service)
    drafting_engine = DraftingEngine(service)
    project_index = ProjectIndexService(service.store, service.symbols)
    harness = AgentHarnessService(
        service=service,
        store=service.store,
        registry=get_default_tool_registry(),
    )
    diagnostics_path = settings.diagnostics_path or settings.database_path.with_suffix(
        ".diagnostics.jsonl"
    )
    diagnostics = DiagnosticLogger(diagnostics_path, service_version=__version__)
    transport = os.getenv("PID_AGENT_MCP_TRANSPORT", os.getenv("AGENTCAD_MCP_TRANSPORT", "stdio"))
    mcp = FastMCP("P&ID-Agent")
    diagnostics.emit(
        "mcp.runtime.created",
        transport=transport,
        database_path=settings.database_path,
        database_instance_id=service.store.database_instance_id,
        diagnostics_path=diagnostics_path,
    )

    @mcp.tool()
    def get_server_info() -> dict[str, Any]:
        """Return version, transport, database identity, and diagnostic log information."""
        return _server_info(settings, service, transport, diagnostics)

    @mcp.tool()
    def get_diagnostics(limit: int = 200) -> dict[str, Any]:
        """Read recent redacted diagnostic events without exposing API keys or full prompts."""
        return {"log": diagnostics.info(), "events": diagnostics.recent(limit)}

    @mcp.tool()
    def list_documents() -> list[dict]:
        """List P&ID-Agent documents and their current revisions."""
        return [item.model_dump(mode="json") for item in service.list_documents()]

    @mcp.tool()
    def create_document(
        name: str = "Untitled P&ID", width: float = 1600, height: float = 900
    ) -> dict:
        """Create a new editable P&ID document."""
        document = service.create_document(
            CreateDocumentRequest(name=name, width=width, height=height),
            source="mcp",
            audit=request_audit_context(
                "create_document",
                actor="mcp-agent",
                surface="mcp",
                label=f"Create document: {name}",
                metadata={"surface": "mcp"},
            ),
        )
        return document.model_dump(mode="json")

    @mcp.tool()
    def import_cad_drawing(
        path: str,
        name: str = "",
        frame: str = "",
        include_text: bool = True,
        fills: str = "solid",
        unit_scale: float = 1.0,
        max_elements: int = 200_000,
        dry_run: bool = False,
    ) -> dict:
        """Import a DWG or DXF drawing from disk as a new engineering document.

        Reproduces geometry, layer names, text and block provenance (``cad_block`` per
        element) and reports everything it could not reproduce. It does not infer
        equipment, lines or instruments — that remains a reviewed semantic step. Set
        ``dry_run`` to get the report without creating a document.
        """
        from .cad_import import CadImporter, CadImportError
        from .cad_models import CadImportOptions

        database = Path(settings.database_path)
        importer = CadImporter(
            DocumentService(SQLiteDocumentStore(database), SymbolRegistry()),
            max_source_bytes=settings.max_import_body_bytes,
        )
        options = CadImportOptions(
            name=name,
            frame=_parse_cad_frame(frame),
            include_text=include_text,
            fills=fills if fills in {"skip", "solid"} else "solid",
            unit_scale=unit_scale,
            max_elements=max_elements,
        )
        target = Path(path)
        try:
            if dry_run:
                # Same loader as the real import: a missing/unreadable path must fail
                # with a stable CAD code on both branches, not leak OSError on one.
                plan = importer.dry_run_path(target, options=options)
                return plan.model_dump(mode="json")
            result = importer.import_path(
                target,
                options=options,
                audit=request_audit_context(
                    "import_cad_drawing",
                    actor="mcp-agent",
                    surface="mcp",
                    label=f"Import CAD file: {target.name}",
                    metadata={"surface": "mcp"},
                ),
                source="mcp",
            )
        except CadImportError as exc:
            raise InvalidOperationError(f"{exc.code}: {exc.message}") from exc
        emit_revision_diagnostics(
            service,
            service.get_document(result.document_id),
            action="cad_import",
            source="mcp",
            diagnostics=diagnostics,
            label=f"Import CAD file: {target.name}",
            operation_count=result.report.operations,
            extra={
                "source_file": result.report.source.filename,
                "sha256": result.report.source.sha256,
                "converter": result.report.source.converter,
                "element_count": result.report.counts.elements,
                "issue_codes": [issue.code for issue in result.report.issues],
            },
        )
        return result.model_dump(mode="json")

    @mcp.tool()
    def inspect_validation_profile() -> dict:
        """Read the resolved validation rule bundle for this deployment.

        Returns the profile id/version, the rule-bundle fingerprint, the release policy
        and every effective rule with the layer that decided it. The profile is resolved
        server-side; there is no argument that could change a rule, a severity or a
        waiver from here.
        """
        from .validation_profile import load_profile

        profile = load_profile()
        return {
            "profile_id": profile.profile_id,
            "profile_version": profile.profile_version,
            "source": profile.source,
            "fingerprint": profile.fingerprint,
            "release_policy": profile.release_policy.model_dump(mode="json"),
            "rules": [
                {
                    "rule_id": rule.rule_id,
                    "enabled": rule.enabled,
                    "severity": rule.severity,
                    "threshold": rule.threshold,
                    "rule_source": rule.rule_source,
                }
                for rule in sorted(profile.rules.values(), key=lambda item: item.rule_id)
            ],
        }

    @mcp.tool()
    def validate_document(document_id: str, as_of: str = "") -> dict:
        """Run the canonical engineering validation for one document. Read-only.

        Returns the canonical result: every issue with its stable code, severity,
        object/element locators, expected versus actual, rule source and waiver status,
        bound to the document revision, profile and rule-bundle fingerprints and the
        evaluation time. It cannot accept a rule script, a severity override or a waiver,
        and it never writes a revision. Pass ``as_of`` (ISO-8601, timezone-aware) when
        waiver expiry matters and the verdict must be reproducible.
        """
        from .validation_engine import validate_document as run_canonical_validation
        from .validation_models import canonical_payload
        from .validation_profile import load_profile

        moment = _parse_as_of(as_of)
        profile = load_profile()
        document = service.get_document(document_id)
        result = run_canonical_validation(service, document.id, profile, now=moment)
        emit_read_diagnostics(
            service,
            document,
            tool_name="validate_document",
            label=f"Validate: {document.name}",
            extra={
                "validation_hash": result.result_hash,
                "rule_bundle_fingerprint": result.rule_bundle_fingerprint,
                "counts": result.counts.model_dump(mode="json"),
                "evaluated_at": result.evaluated_at.isoformat(),
            },
        )
        # Same canonical public payload as REST and the CLI: ``schema``, not ``schema_name``.
        return canonical_payload(result)

    @mcp.tool()
    def assess_release_readiness(document_id: str, as_of: str = "") -> dict:
        """Assess whether a document is release-*eligible*. It cannot approve anything.

        Returns ``eligible`` / ``not_eligible`` with the evidence behind it. A required
        validator that did not run, an unwaived severity named by the release policy, or
        a finding whose rule is not registered all fail closed. There is no argument and
        no return field that can produce an approved/signed/IFC/AFC state: formal release
        stays behind the human approval workflow.
        """
        from .release_validator import assess_document_release_readiness
        from .validation_models import canonical_payload
        from .validation_profile import load_profile

        moment = _parse_as_of(as_of)
        profile = load_profile()
        document = service.get_document(document_id)
        readiness = assess_document_release_readiness(service, document.id, profile, now=moment)
        emit_read_diagnostics(
            service,
            document,
            tool_name="assess_release_readiness",
            label=f"Release readiness: {document.name}",
            extra={
                "readiness_hash": readiness.readiness_hash,
                "state": readiness.state,
                "human_approval_required": True,
                "evaluated_at": readiness.evaluated_at.isoformat(),
            },
        )
        return canonical_payload(readiness)

    @mcp.tool()
    def get_scene_summary(document_id: str) -> dict:
        """Read the latest semantic scene summary before planning a modification."""
        return service.scene_summary(document_id)

    @mcp.tool()
    def get_document(document_id: str) -> dict:
        """Read the complete current P&ID-Agent document JSON."""
        return service.get_document(document_id).model_dump(mode="json")

    @mcp.tool()
    def get_document_history(document_id: str, limit: int = 100) -> list[dict]:
        """Read revision history with operation summaries and element-level before/after diffs."""
        service.get_document(document_id)
        return service.store.list_history_detailed(document_id, limit)

    @mcp.tool()
    def get_tool_registry() -> dict[str, Any]:
        """Return canonical tool schemas, permissions, risk and audit metadata."""
        return _tool_registry_catalog()

    @mcp.tool()
    def start_agent_session(
        document_id: str,
        actor: str = "mcp-agent",
        provider: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        """Start a persisted Agent harness session for one document."""
        return harness.create_session(
            AgentSessionCreateRequest(
                document_id=document_id,
                actor=actor,
                provider=provider,
                model=model,
                metadata={"surface": "mcp"},
            )
        ).model_dump(mode="json")

    @mcp.tool()
    def request_agent_tool_approval(
        session_id: str,
        document_id: str,
        tool_name: str,
        intent: dict[str, Any],
        requested_by: str = "mcp-user",
        reason: str = "",
    ) -> dict[str, Any]:
        """Create an approval bound to an exact session/tool/document/intent hash."""
        return harness.request_approval(
            session_id,
            ToolApprovalCreateRequest(
                tool_name=tool_name,
                document_id=document_id,
                intent=intent,
                requested_by=requested_by,
                reason=reason,
            ),
        ).model_dump(mode="json")

    @mcp.tool()
    def resolve_agent_tool_approval(
        approval_id: str,
        approved: bool,
        actor: str = "mcp-user",
        note: str = "",
    ) -> dict[str, Any]:
        """Approve or reject a pending Agent tool request."""
        return harness.resolve_approval(
            approval_id,
            ToolApprovalResolveRequest(approved=approved, actor=actor, note=note),
        ).model_dump(mode="json")

    @mcp.tool()
    def get_agent_session_audit(session_id: str) -> dict[str, Any]:
        """Return persisted approvals and tool-call provenance for one session."""
        return harness.audit(session_id).model_dump(mode="json")

    @mcp.tool()
    def get_audit_trail(
        document_id: str | None = None,
        event_type: str | None = None,
        actor: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Read the append-only audit trail (read-only; it is only ever appended by
        engineering write paths)."""
        return [
            record.model_dump(mode="json")
            for record in service.audit.audit_trail(
                document_id=document_id,
                event_type=event_type,
                actor=actor,
                status=status,
                limit=limit,
            )
        ]

    @mcp.tool()
    def verify_audit_chain() -> dict[str, Any]:
        """Recompute the whole audit hash chain and report any divergence."""
        return service.audit.verify_chain().model_dump(mode="json")

    @mcp.tool()
    def get_revision_evidence(document_id: str, revision: int) -> dict[str, Any]:
        """Return the audit record, semantic diff and validation evidence for one revision."""
        return service.audit.revision_evidence(document_id, revision).model_dump(mode="json")

    @mcp.tool()
    def get_transaction_schema() -> dict[str, Any]:
        """Return the low-level atomic transaction JSON Schema."""
        return TransactionRequest.model_json_schema()

    @mcp.tool()
    def get_agent_transaction_schema() -> dict[str, Any]:
        """Return the safer semantic schema for replace, reconnect, connect and delete actions."""
        return SemanticTransaction.model_json_schema()

    @mcp.tool()
    def analyze_transaction(
        document_id: str,
        transaction: TransactionRequest,
    ) -> dict[str, Any]:
        """Return structured validation issues and repair suggestions without writing."""
        return analyze_low_level(service, document_id, transaction).model_dump(mode="json")

    @mcp.tool()
    def validate_transaction(
        document_id: str,
        transaction: TransactionRequest,
    ) -> dict[str, Any]:
        """Validate a low-level transaction and raise on the first structured issue."""
        return _validate_transaction(service, document_id, transaction)

    @mcp.tool()
    def preview_semantic_diff(
        document_id: str,
        transaction: TransactionRequest,
    ) -> dict[str, Any]:
        """Return an engineering-oriented semantic diff without writing."""
        return preview_transaction_semantic_diff(
            service,
            document_id,
            transaction,
        ).model_dump(mode="json")

    @mcp.tool()
    def compile_agent_transaction(
        document_id: str,
        transaction: SemanticTransaction,
    ) -> dict[str, Any]:
        """Compile a safe semantic transaction to low-level operations without writing."""
        return semantic_compiler.compile(document_id, transaction).model_dump(mode="json")

    @mcp.tool()
    def apply_agent_transaction(
        document_id: str,
        transaction: SemanticTransaction,
        session_id: str,
        approval_id: str,
    ) -> dict[str, Any]:
        """Compile, validate and atomically apply an explicitly approved semantic transaction."""
        authorized = harness.authorize(
            session_id=session_id,
            tool_name="apply_agent_transaction",
            document_id=document_id,
            intent={"transaction": transaction.model_dump(mode="json")},
            approval_id=approval_id,
            base_revision=transaction.expected_revision,
            metadata={"surface": "mcp"},
        )
        compiled = semantic_compiler.compile(document_id, transaction)
        if compiled.transaction is None:
            harness.fail_tool_call(authorized, error_code="semantic_compile_invalid")
            return {"applied": False, "assessment": compiled.assessment.model_dump(mode="json")}
        try:
            result = _apply_governed(
                harness,
                diagnostics,
                document_id,
                compiled.transaction,
                authorized=authorized,
                metadata={"compiled_operation_count": compiled.assessment.compiled_operation_count},
            )
        except Exception as exc:
            harness.fail_tool_call(
                authorized,
                error_code=getattr(exc, "code", type(exc).__name__),
            )
            raise
        return {
            "applied": True,
            "assessment": compiled.assessment.model_dump(mode="json"),
            "result": result,
        }

    @mcp.tool()
    def preview_auto_layout(document_id: str, options: AutoLayoutRequest) -> dict[str, Any]:
        """Preview topology-aware equipment layout and obstacle-avoiding pipe routing without writing."""
        preview = layout_engine.preview(document_id, options)
        diagnostics.emit(
            "layout.preview.completed",
            document_id=document_id,
            revision=preview.current_revision,
            source="mcp",
            operation_count=len(preview.transaction.operations) if preview.transaction else 0,
            moved_element_ids=preview.moved_element_ids,
            rerouted_connector_ids=preview.rerouted_connector_ids,
            overlaps_before=preview.metrics.overlaps_before,
            overlaps_after=preview.metrics.overlaps_after,
            pipe_obstacle_intersections_before=preview.metrics.pipe_obstacle_intersections_before,
            pipe_obstacle_intersections_after=preview.metrics.pipe_obstacle_intersections_after,
        )
        return preview.model_dump(mode="json")

    @mcp.tool()
    def apply_auto_layout(
        document_id: str,
        options: AutoLayoutRequest,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply reversible auto-layout through the Harness allow-policy and audit path."""
        session = harness.ensure_session(
            document_id,
            session_id=session_id,
            actor="mcp-agent",
        )
        authorized = harness.authorize(
            session_id=session.id,
            tool_name="apply_auto_layout",
            document_id=document_id,
            intent={"options": options.model_dump(mode="json")},
            base_revision=options.expected_revision,
            metadata={"surface": "mcp"},
        )
        preview = layout_engine.preview(document_id, options)
        if preview.transaction is None:
            harness.complete_tool_call(
                authorized,
                result_revision=preview.current_revision,
                metadata={"applied": False},
            )
            harness.complete_session(
                session.id,
                end_revision=preview.current_revision,
                status="completed",
            )
            return {"applied": False, "preview": preview.model_dump(mode="json")}
        try:
            result = _apply_governed(
                harness,
                diagnostics,
                document_id,
                preview.transaction,
                authorized=authorized,
                metadata={"applied": True, "options": options.model_dump(mode="json")},
            )
        except Exception as exc:
            harness.fail_tool_call(
                authorized,
                error_code=getattr(exc, "code", type(exc).__name__),
            )
            raise
        return {
            "applied": True,
            "preview": preview.model_dump(mode="json"),
            "result": result,
        }

    @mcp.tool()
    def get_drafting_report(
        document_id: str,
        request: DraftingRequest | None = None,
    ) -> dict[str, Any]:
        """Measure one drawing against the drafting rules without writing anything."""
        report = drafting_engine.report(document_id, request or DraftingRequest())
        diagnostics.emit(
            "drafting.report.completed",
            document_id=document_id,
            revision=report.revision,
            source="mcp",
            scope_kind=report.scope_kind,
            score=report.score,
            gate_passed=report.gate.passed,
            blocker_codes=sorted({finding.code for finding in report.gate.blockers}),
            locked_element_ids=report.locks.locked_element_ids,
        )
        return report.model_dump(mode="json", by_alias=True)

    @mcp.tool()
    def preview_deterministic_drafting(
        document_id: str,
        request: DraftingRequest,
    ) -> dict[str, Any]:
        """Preview a reproducible deterministic drafting run without writing."""
        preview = drafting_engine.preview(document_id, request)
        diagnostics.emit(
            "drafting.preview.completed",
            document_id=document_id,
            revision=preview.current_revision,
            source="mcp",
            operation_count=preview.reproducibility.operation_count,
            settled=preview.settled,
            moved_element_ids=preview.moved_element_ids,
            rerouted_connector_ids=preview.rerouted_connector_ids,
            locked_element_ids=preview.locked_element_ids,
            score_before=preview.metrics.before.score,
            score_after=preview.metrics.after.score,
            regressions=preview.metrics.regressions,
            gate_passed=preview.gate.passed,
            transaction_digest=preview.reproducibility.transaction_digest,
        )
        return preview.model_dump(mode="json", by_alias=True)

    @mcp.tool()
    def apply_deterministic_drafting(
        document_id: str,
        request: DraftingRequest,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply a deterministic drafting run through the Harness allow-policy and audit path."""
        session = harness.ensure_session(
            document_id,
            session_id=session_id,
            actor="mcp-agent",
        )
        authorized = harness.authorize(
            session_id=session.id,
            tool_name="apply_deterministic_drafting",
            document_id=document_id,
            intent={"request": request.model_dump(mode="json")},
            base_revision=request.expected_revision,
            metadata={"surface": "mcp"},
        )
        preview = drafting_engine.preview(document_id, request)
        if preview.transaction is None:
            harness.complete_tool_call(
                authorized,
                result_revision=preview.current_revision,
                metadata={"applied": False},
            )
            harness.complete_session(
                session.id,
                end_revision=preview.current_revision,
                status="completed",
            )
            return {"applied": False, "preview": preview.model_dump(mode="json", by_alias=True)}
        try:
            result = _apply_governed(
                harness,
                diagnostics,
                document_id,
                preview.transaction,
                authorized=authorized,
                metadata={
                    "applied": True,
                    "transaction_digest": preview.reproducibility.transaction_digest,
                    "engine_version": preview.reproducibility.engine_version,
                    "operation_count": preview.reproducibility.operation_count,
                },
            )
        except Exception as exc:
            harness.fail_tool_call(
                authorized,
                error_code=getattr(exc, "code", type(exc).__name__),
            )
            raise
        return {
            "applied": True,
            "preview": preview.model_dump(mode="json", by_alias=True),
            "result": result,
        }

    def _apply_low_level_with_approval(
        document_id: str,
        transaction: TransactionRequest,
        session_id: str,
        approval_id: str,
        *,
        tool_surface: str,
    ) -> dict:
        authorized = harness.authorize(
            session_id=session_id,
            tool_name="apply_compiled_agent_transaction",
            document_id=document_id,
            intent={"transaction": transaction.model_dump(mode="json")},
            approval_id=approval_id,
            base_revision=transaction.expected_revision,
            metadata={"surface": "mcp", "legacy_tool": tool_surface},
        )
        try:
            return _apply_governed(
                harness,
                diagnostics,
                document_id,
                transaction,
                authorized=authorized,
                metadata={"legacy_tool": tool_surface},
            )
        except Exception as exc:
            harness.fail_tool_call(
                authorized,
                error_code=getattr(exc, "code", type(exc).__name__),
            )
            raise

    @mcp.tool()
    def apply_transaction_v2(
        document_id: str,
        transaction: TransactionRequest,
        session_id: str,
        approval_id: str,
    ) -> dict:
        """Apply a low-level transaction only after exact Harness approval."""
        return _apply_low_level_with_approval(
            document_id,
            transaction,
            session_id,
            approval_id,
            tool_surface="apply_transaction_v2",
        )

    @mcp.tool()
    def apply_transaction(
        document_id: str,
        transaction_json: str,
        session_id: str,
        approval_id: str,
    ) -> dict:
        """Legacy JSON transaction entrypoint; still requires exact Harness approval."""
        transaction = TransactionRequest.model_validate(json.loads(transaction_json))
        return _apply_low_level_with_approval(
            document_id,
            transaction,
            session_id,
            approval_id,
            tool_surface="apply_transaction",
        )

    @mcp.tool()
    def get_engineering_graph(document_id: str) -> dict:
        """Read the derived engineering semantic graph (objects, topology, findings)."""
        document = service.get_document(document_id)
        graph = build_engineering_graph(document, service.symbols)
        return graph.model_dump(mode="json", by_alias=True)

    @mcp.tool()
    def trace_engineering_object(
        document_id: str,
        ref: str,
        direction: str = "both",
        max_depth: int = 64,
    ) -> dict:
        """Trace upstream/downstream engineering objects from an id, tag key or tag.

        ``ref`` accepts the stable engineering id (``eq_…``/``ln_…``/``sg_…``), a tag
        key (``equipment:p-101``), a tag (``P-101``) or a drawing element id. Traces
        stay inside one edge class: starting from a signal follows instrument wiring,
        anything else follows process flow.
        """
        if direction not in {"upstream", "downstream", "both"}:
            raise InvalidOperationError(
                f"direction must be upstream, downstream or both, got {direction!r}"
            )
        document = service.get_document(document_id)
        graph = build_engineering_graph(document, service.symbols)
        try:
            result = trace_engineering_object(
                graph, ref, direction=direction, max_depth=max_depth  # type: ignore[arg-type]
            )
        except KeyError as exc:
            raise InvalidOperationError(str(exc)) from exc
        return result.model_dump(mode="json")

    @mcp.tool()
    def find_engineering_object(ref: str, limit: int = 50) -> list[dict]:
        """Locate one engineering object across the project's indexed drawings.

        Answers "which drawing holds ``P-101``" or "which drawing holds
        ``eq_9f3a2b1c4d5e``" without the caller knowing the drawing. Rows left by an
        older builder version are skipped instead of guessed at.
        """
        return [
            match.model_dump(mode="json") for match in project_index.find_objects(ref, limit=limit)
        ]

    @mcp.tool()
    def get_project_engineering_graph() -> dict:
        """Read the project-wide derived engineering graph built from the index."""
        return project_index.project_graph().model_dump(mode="json", by_alias=True)

    @mcp.tool()
    def rebuild_project_index(force: bool = False) -> dict:
        """Rebuild the derived project index; never changes a drawing."""

        report = rebuild_with_evidence(
            project_index,
            service.audit,
            force=force,
            actor="mcp-client",
            surface="mcp",
        )
        return report.model_dump(mode="json", by_alias=True)

    @mcp.tool()
    def list_symbols() -> list[dict]:
        """List allowed company/P&ID symbols, sizes, ports, and descriptions."""
        return [item.model_dump(mode="json") for item in service.symbols.list()]

    return mcp, transport


def main() -> None:
    mcp, transport = build_mcp_server()
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
