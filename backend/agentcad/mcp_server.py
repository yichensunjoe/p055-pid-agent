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
from .history_diff import build_history_details
from .layout_models import AutoLayoutRequest
from .models import CreateDocumentRequest, TransactionRequest
from .semantic_compiler_engine import SemanticTransactionCompiler
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
    ) -> dict[str, Any]:
        """Compile, validate and atomically apply a semantic transaction."""
        compiled = semantic_compiler.compile(document_id, transaction)
        if compiled.transaction is None:
            return {"applied": False, "assessment": compiled.assessment.model_dump(mode="json")}
        return {
            "applied": True,
            "assessment": compiled.assessment.model_dump(mode="json"),
            "result": _apply_with_history(service, diagnostics, document_id, compiled.transaction),
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
    def apply_auto_layout(document_id: str, options: AutoLayoutRequest) -> dict[str, Any]:
        """Preview and atomically apply topology-aware layout when the generated transaction is non-empty."""
        preview = layout_engine.preview(document_id, options)
        if preview.transaction is None:
            return {"applied": False, "preview": preview.model_dump(mode="json")}
        return {
            "applied": True,
            "preview": preview.model_dump(mode="json"),
            "result": _apply_with_history(service, diagnostics, document_id, preview.transaction),
        }

    @mcp.tool()
    def apply_transaction_v2(
        document_id: str,
        transaction: TransactionRequest,
    ) -> dict:
        """Apply a structured low-level atomic P&ID-Agent transaction."""
        return _apply_with_history(service, diagnostics, document_id, transaction)

    @mcp.tool()
    def apply_transaction(document_id: str, transaction_json: str) -> dict:
        """Legacy string-based transaction tool. Prefer apply_agent_transaction or apply_transaction_v2."""
        transaction = TransactionRequest.model_validate(json.loads(transaction_json))
        return _apply_with_history(service, diagnostics, document_id, transaction)

    @mcp.tool()
    def list_symbols() -> list[dict]:
        """List allowed company/P&ID symbols, sizes, ports, and descriptions."""
        return [item.model_dump(mode="json") for item in service.symbols.list()]

    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
