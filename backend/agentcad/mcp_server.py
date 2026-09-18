from __future__ import annotations

import json
import os
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


def main() -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise SystemExit("Install MCP support with: pip install 'pid-agent[mcp]'" ) from exc

    settings = Settings.from_env()
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

    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
