from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field

from .agent_semantic_models import (
    AgentTransactionAssessment,
    CompiledSemanticTransaction,
    SemanticTransaction,
)
from .layout_models import AutoLayoutPreview, AutoLayoutRequest
from .models import Document, StrictModel, TransactionRequest

ToolPermission = Literal["allow", "ask", "deny"]
ToolRisk = Literal["read", "draft_edit", "engineering_change", "critical_change", "release"]
ToolIdempotency = Literal["read_only", "idempotent", "depends_on_revision", "non_idempotent"]
ToolSurface = Literal["agent", "rest", "mcp"]


class DocumentToolInput(StrictModel):
    document_id: str


class HistoryToolInput(DocumentToolInput):
    limit: int = Field(default=100, ge=1, le=5000)


class LowLevelTransactionToolInput(DocumentToolInput):
    transaction: TransactionRequest


class SemanticTransactionToolInput(DocumentToolInput):
    transaction: SemanticTransaction


class AutoLayoutToolInput(DocumentToolInput):
    options: AutoLayoutRequest


class RevisionToolInput(DocumentToolInput):
    expected_revision: int | None = Field(default=None, ge=0)


class ToolDefinition(StrictModel):
    """Machine-readable contract for one AgentCAD/P&ID harness capability.

    This layer is deliberately metadata-only in the first harness slice. Existing
    service/API/MCP execution paths remain authoritative; later permission and audit
    middleware can consume the same definitions without changing tool identities.
    """

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    description: str = Field(min_length=1)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permission: ToolPermission
    risk: ToolRisk
    has_side_effect: bool = False
    preview_supported: bool = False
    idempotency: ToolIdempotency = "read_only"
    audit_event: str
    surfaces: list[ToolSurface] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    def llm_tool_schema(self) -> dict[str, Any]:
        """Return the compatibility shape used by the existing semantic planner endpoint."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self, definitions: list[ToolDefinition] | None = None):
        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions or []:
            self.register(definition)

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"duplicate tool definition: {definition.name}")
        self._definitions[definition.name] = definition

    def require(self, name: str) -> ToolDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool definition: {name}") from exc

    def list(self) -> list[ToolDefinition]:
        return [self._definitions[name] for name in sorted(self._definitions)]

    def catalog(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "tool_count": len(self._definitions),
            "tools": [item.model_dump(mode="json") for item in self.list()],
        }


def _object_schema(description: str) -> dict[str, Any]:
    return {"type": "object", "description": description}


def _array_schema(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "object"}, "description": description}


@lru_cache(maxsize=1)
def get_default_tool_registry() -> ToolRegistry:
    """Build the canonical registry for capabilities that already exist today.

    The registry intentionally excludes proposed Charter tools that do not yet have
    deterministic implementations. New entries should be added only with tests and
    a real execution path.
    """

    return ToolRegistry(
        [
            ToolDefinition(
                name="get_document",
                description="Read the complete current structured P&ID document.",
                input_schema=DocumentToolInput.model_json_schema(),
                output_schema=Document.model_json_schema(),
                permission="allow",
                risk="read",
                audit_event="tool.get_document",
                surfaces=["mcp", "rest", "agent"],
                tags=["inspect", "document"],
            ),
            ToolDefinition(
                name="get_scene_summary",
                description=(
                    "Read the latest semantic scene summary, including engineering "
                    "elements and connection context, before planning a modification."
                ),
                input_schema=DocumentToolInput.model_json_schema(),
                output_schema=_object_schema("Semantic scene summary."),
                permission="allow",
                risk="read",
                audit_event="tool.get_scene_summary",
                surfaces=["mcp", "rest", "agent"],
                tags=["inspect", "topology"],
            ),
            ToolDefinition(
                name="get_document_history",
                description="Read revision history and element-level change details.",
                input_schema=HistoryToolInput.model_json_schema(),
                output_schema=_array_schema("Revision history entries."),
                permission="allow",
                risk="read",
                audit_event="tool.get_document_history",
                surfaces=["mcp", "rest", "agent"],
                tags=["inspect", "revision", "audit"],
            ),
            ToolDefinition(
                name="analyze_transaction",
                description=(
                    "Analyze a low-level transaction and return structured issues "
                    "without modifying the document."
                ),
                input_schema=LowLevelTransactionToolInput.model_json_schema(),
                output_schema=AgentTransactionAssessment.model_json_schema(),
                permission="allow",
                risk="read",
                audit_event="tool.analyze_transaction",
                surfaces=["mcp", "rest", "agent"],
                tags=["validate", "transaction", "preview"],
            ),
            ToolDefinition(
                name="validate_transaction",
                description=(
                    "Validate a low-level transaction against the current revision "
                    "without writing."
                ),
                input_schema=LowLevelTransactionToolInput.model_json_schema(),
                output_schema=_object_schema("Validated transaction preview summary."),
                permission="allow",
                risk="read",
                audit_event="tool.validate_transaction",
                surfaces=["mcp", "rest"],
                tags=["validate", "transaction", "preview"],
            ),
            ToolDefinition(
                name="plan_pid_agent_semantic_transaction",
                description=(
                    "Use safe high-level P&ID operations for symbol replacement, "
                    "connector reconnection, port-to-port connections, instrument taps "
                    "and connection-aware deletion."
                ),
                input_schema=SemanticTransaction.model_json_schema(),
                output_schema=SemanticTransaction.model_json_schema(),
                permission="allow",
                risk="read",
                audit_event="tool.plan_pid_agent_semantic_transaction",
                surfaces=["agent", "rest"],
                tags=["plan", "pid", "semantic"],
            ),
            ToolDefinition(
                name="compile_agent_transaction",
                description=(
                    "Compile a semantic P&ID transaction into validated low-level "
                    "operations without writing."
                ),
                input_schema=SemanticTransactionToolInput.model_json_schema(),
                output_schema=CompiledSemanticTransaction.model_json_schema(),
                permission="allow",
                risk="read",
                audit_event="tool.compile_agent_transaction",
                surfaces=["mcp", "agent"],
                tags=["compile", "pid", "semantic", "preview"],
            ),
            ToolDefinition(
                name="apply_agent_transaction",
                description=(
                    "Compile, validate and atomically apply a semantic P&ID transaction."
                ),
                input_schema=SemanticTransactionToolInput.model_json_schema(),
                output_schema=_object_schema(
                    "Application result containing assessment and TransactionResult."
                ),
                permission="ask",
                risk="engineering_change",
                has_side_effect=True,
                preview_supported=True,
                idempotency="depends_on_revision",
                audit_event="tool.apply_agent_transaction",
                surfaces=["mcp", "agent"],
                tags=["apply", "pid", "semantic", "engineering-change"],
            ),
            ToolDefinition(
                name="preview_auto_layout",
                description=(
                    "Preview topology-aware equipment layout and obstacle-avoiding "
                    "routing without modifying the document."
                ),
                input_schema=AutoLayoutToolInput.model_json_schema(),
                output_schema=AutoLayoutPreview.model_json_schema(),
                permission="allow",
                risk="draft_edit",
                preview_supported=True,
                audit_event="tool.preview_auto_layout",
                surfaces=["mcp", "rest", "agent"],
                tags=["layout", "routing", "preview"],
            ),
            ToolDefinition(
                name="apply_auto_layout",
                description=(
                    "Apply a previously reproducible topology-aware layout transaction."
                ),
                input_schema=AutoLayoutToolInput.model_json_schema(),
                output_schema=_object_schema("Auto-layout preview and apply result."),
                permission="allow",
                risk="draft_edit",
                has_side_effect=True,
                preview_supported=True,
                idempotency="depends_on_revision",
                audit_event="tool.apply_auto_layout",
                surfaces=["mcp", "rest", "agent"],
                tags=["layout", "routing", "draft-edit"],
            ),
            ToolDefinition(
                name="undo_document",
                description="Undo the latest document edit at an expected revision.",
                input_schema=RevisionToolInput.model_json_schema(),
                output_schema=Document.model_json_schema(),
                permission="allow",
                risk="draft_edit",
                has_side_effect=True,
                idempotency="depends_on_revision",
                audit_event="tool.undo_document",
                surfaces=["rest", "agent"],
                tags=["revision", "rollback"],
            ),
            ToolDefinition(
                name="redo_document",
                description="Redo the latest undone document edit at an expected revision.",
                input_schema=RevisionToolInput.model_json_schema(),
                output_schema=Document.model_json_schema(),
                permission="allow",
                risk="draft_edit",
                has_side_effect=True,
                idempotency="depends_on_revision",
                audit_event="tool.redo_document",
                surfaces=["rest", "agent"],
                tags=["revision", "rollback"],
            ),
        ]
    )
