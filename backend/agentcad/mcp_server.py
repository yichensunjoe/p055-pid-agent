from __future__ import annotations

import json
import os
from typing import Any

from . import __version__
from .agent_semantic import analyze_transaction as analyze_low_level
from .agent_semantic_models import SemanticTransaction
from .auto_layout_engine import AutoLayoutEngine
from .config import Settings
from .diagnostics import DiagnosticLogger
from .harness import AgentHarnessService
from .harness_models import (
    AgentSessionCreateRequest,
    ToolApprovalCreateRequest,
    ToolApprovalResolveRequest,
)
from .history_diff import build_history_details
from .layout_models import AutoLayoutRequest
from .models import CreateDocumentRequest, TransactionRequest
from .semantic_compiler_engine import SemanticTransactionCompiler
from .semantic_diff import build_semantic_diff, preview_transaction_semantic_diff
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


def _apply_with_history(
    service: DocumentService,
    diagnostics: DiagnosticLogger,
    document_id: str,
    transaction: TransactionRequest,
) -> dict[str, Any]:
    before = service.get_document(document_id)
    result = service.apply_transaction(document_id, transaction, source="mcp")
    details = build_history_details(
        before,
        result.document,
        transaction.operations,
        action="transaction",
    )
    details["semantic_diff"] = build_semantic_diff(
        before,
        result.document,
        details,
        service.symbols,
    ).model_dump(mode="json")
    persisted = service.store.update_history_details(
        document_id,
        result.document.revision,
        details,
    )
    diagnostics.emit(
        "document.revision.created",
        document_id=document_id,
        base_revision=before.revision,
        revision=result.document.revision,
        source="mcp",
        action="transaction",
        label=transaction.label,
        operation_count=len(transaction.operations),
        affected_element_ids=details["affected_element_ids"],
        added_element_ids=details["added_element_ids"],
        updated_element_ids=details["updated_element_ids"],
        deleted_element_ids=details["deleted_element_ids"],
        history_details_persisted=persisted,
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
            CreateDocumentRequest(name=name, width=width, height=height), source="mcp"
        )
        diagnostics.emit(
            "document.created",
            document_id=document.id,
            revision=document.revision,
            name=document.name,
            width=document.canvas.width,
            height=document.canvas.height,
            source="mcp",
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
            result = _apply_with_history(service, diagnostics, document_id, compiled.transaction)
        except Exception as exc:
            harness.fail_tool_call(
                authorized,
                error_code=getattr(exc, "code", type(exc).__name__),
            )
            raise
        revision = result["document"]["revision"]
        harness.complete_tool_call(
            authorized,
            result_revision=revision,
            metadata={"compiled_operation_count": compiled.assessment.compiled_operation_count},
        )
        harness.complete_session(session_id, end_revision=revision, status="completed")
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
            result = _apply_with_history(service, diagnostics, document_id, preview.transaction)
        except Exception as exc:
            harness.fail_tool_call(
                authorized,
                error_code=getattr(exc, "code", type(exc).__name__),
            )
            raise
        revision = result["document"]["revision"]
        harness.complete_tool_call(
            authorized,
            result_revision=revision,
            metadata={"applied": True},
        )
        harness.complete_session(session.id, end_revision=revision, status="completed")
        return {
            "applied": True,
            "preview": preview.model_dump(mode="json"),
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
            result = _apply_with_history(service, diagnostics, document_id, transaction)
        except Exception as exc:
            harness.fail_tool_call(
                authorized,
                error_code=getattr(exc, "code", type(exc).__name__),
            )
            raise
        revision = result["document"]["revision"]
        harness.complete_tool_call(
            authorized,
            result_revision=revision,
            metadata={"applied_operations": len(transaction.operations)},
        )
        harness.complete_session(session_id, end_revision=revision, status="completed")
        return result

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
    def list_symbols() -> list[dict]:
        """List allowed company/P&ID symbols, sizes, ports, and descriptions."""
        return [item.model_dump(mode="json") for item in service.symbols.list()]

    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
