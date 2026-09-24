from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field

from .agent_semantic_models import (
    AgentTransactionAssessment,
    CompiledSemanticTransaction,
    SemanticTransaction,
)
from .cad_models import CadImportOptions, CadImportResult
from .drafting_models import DraftingPreview, DraftingReport, DraftingRequest
from .engineering_ir import EngineeringGraph, TraceResult
from .layout_models import AutoLayoutPreview, AutoLayoutRequest
from .models import Document, StrictModel, TransactionRequest
from .project_index import EngineeringObjectMatch, ProjectEngineeringGraph, RebuildReport
from .semantic_diff_models import SemanticDiffReport

ToolPermission = Literal["allow", "ask", "deny"]
ToolRisk = Literal["read", "draft_edit", "engineering_change", "critical_change", "release"]
ToolIdempotency = Literal["read_only", "idempotent", "depends_on_revision", "non_idempotent"]
ToolSurface = Literal["agent", "rest", "mcp"]


class DocumentToolInput(StrictModel):
    document_id: str


class HistoryToolInput(DocumentToolInput):
    limit: int = Field(default=100, ge=1, le=5000)


class RepairFindingToolInput(DocumentToolInput):
    """A repair target: which finding, how far the scope may reach, and nothing else.

    Deliberately narrow. A tool that accepted a plan, a waiver, a profile or a success verdict
    would be a second repair implementation with its own opinion about what "repaired" means.
    """

    code: str = ""
    validator_id: str = ""
    element_ids: list[str] = Field(default_factory=list)
    hop: int = Field(default=1, ge=1, le=2)
    permits_creation: bool = False
    as_of: str = ""


class LowLevelTransactionToolInput(DocumentToolInput):
    transaction: TransactionRequest


class SemanticTransactionToolInput(DocumentToolInput):
    transaction: SemanticTransaction


class AutoLayoutToolInput(DocumentToolInput):
    options: AutoLayoutRequest


class DraftingToolInput(DocumentToolInput):
    """Input for the deterministic drafting surfaces.

    ``request`` carries the whole contract (scope, locks, direction, policy), so a
    caller can describe a regional repair, a route-only tidy-up or a full relayout
    through one stable type.
    """

    request: DraftingRequest = Field(default_factory=DraftingRequest)


class CadImportToolInput(StrictModel):
    """Input for the CAD (DWG/DXF) import surfaces.

    The HTTP surface takes the drawing as its request body and passes ``filename``;
    the CLI and MCP surfaces name a file on the host through ``path``. ``options``
    carries the whole import contract (frame, fills, layers, units, limits).
    """

    path: str = ""
    filename: str = ""
    options: CadImportOptions = Field(default_factory=CadImportOptions)


class RevisionToolInput(DocumentToolInput):
    expected_revision: int | None = Field(default=None, ge=0)


class EngineeringTraceToolInput(DocumentToolInput):
    #: Engineering id (``eq_…``), tag key (``equipment:p-101``), tag or element id.
    ref: str
    direction: Literal["upstream", "downstream", "both"] = "both"
    max_depth: int = Field(default=64, ge=1, le=256)


class EngineeringFindToolInput(StrictModel):
    """Input for the project-wide engineering-object lookup."""

    ref: str = Field(
        min_length=1,
        description=(
            "Stable engineering id, tag key, tag or drawing element id to locate across "
            "the project."
        ),
    )
    limit: int = Field(default=50, ge=1, le=500)


class ProjectIndexRebuildToolInput(StrictModel):
    force: bool = False


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
                name="create_document",
                description="Create a new empty engineering document.",
                input_schema=_object_schema("Document name and canvas size."),
                output_schema=Document.model_json_schema(),
                permission="allow",
                risk="draft_edit",
                has_side_effect=True,
                idempotency="non_idempotent",
                audit_event="tool.create_document",
                surfaces=["mcp", "rest"],
                tags=["document", "create", "engineering-change"],
            ),
            ToolDefinition(
                name="delete_document",
                description=(
                    "Delete an entire engineering document at an expected revision. "
                    "The deletion keeps its audit evidence, which outlives the document."
                ),
                input_schema=RevisionToolInput.model_json_schema(),
                output_schema=_object_schema("Deletion confirmation."),
                permission="ask",
                risk="critical_change",
                has_side_effect=True,
                idempotency="non_idempotent",
                audit_event="tool.delete_document",
                surfaces=["rest"],
                tags=["document", "delete", "critical-change"],
            ),
            ToolDefinition(
                name="apply_web_transaction",
                description=(
                    "Apply one atomic edit made by the human editor UI. This is a "
                    "deliberate, human-driven change, so it is allowed without an "
                    "agent approval but is fully attributed and audited."
                ),
                input_schema=LowLevelTransactionToolInput.model_json_schema(),
                output_schema=_object_schema("Applied TransactionResult and provenance."),
                permission="allow",
                risk="draft_edit",
                has_side_effect=True,
                preview_supported=True,
                idempotency="depends_on_revision",
                audit_event="tool.apply_web_transaction",
                surfaces=["rest"],
                tags=["apply", "editor", "engineering-change"],
            ),
            ToolDefinition(
                name="rename_document",
                description="Rename an engineering document at an expected revision.",
                input_schema=RevisionToolInput.model_json_schema(),
                output_schema=Document.model_json_schema(),
                permission="allow",
                risk="draft_edit",
                has_side_effect=True,
                idempotency="non_idempotent",
                audit_event="tool.rename_document",
                surfaces=["rest"],
                tags=["document", "metadata"],
            ),
            ToolDefinition(
                name="move_document_folder",
                description="Move a document to a project folder at an expected revision.",
                input_schema=RevisionToolInput.model_json_schema(),
                output_schema=Document.model_json_schema(),
                permission="allow",
                risk="draft_edit",
                has_side_effect=True,
                idempotency="non_idempotent",
                audit_event="tool.move_document_folder",
                surfaces=["rest"],
                tags=["document", "metadata"],
            ),
            ToolDefinition(
                name="import_document_payload",
                description="Import an exported document payload into the project store.",
                input_schema=_object_schema("Exported document payload plus conflict policy."),
                output_schema=_object_schema("Imported document ids and id remapping."),
                permission="ask",
                risk="draft_edit",
                has_side_effect=True,
                idempotency="non_idempotent",
                audit_event="tool.import_document_payload",
                surfaces=["rest"],
                tags=["import", "project"],
            ),
            ToolDefinition(
                name="import_project_payload",
                description="Import a full project package into the project store.",
                input_schema=_object_schema("Exported project package plus conflict policy."),
                output_schema=_object_schema("Imported document ids and id remapping."),
                permission="ask",
                risk="draft_edit",
                has_side_effect=True,
                idempotency="non_idempotent",
                audit_event="tool.import_project_payload",
                surfaces=["rest"],
                tags=["import", "project"],
            ),
            ToolDefinition(
                name="import_cad_drawing",
                description=(
                    "Import a DWG or DXF drawing as a new document: geometry, layer "
                    "names, text and block provenance are reproduced, and everything "
                    "that could not be reproduced is reported with a code and a count. "
                    "No engineering semantics are inferred. The import cannot modify, "
                    "re-version or delete any existing document."
                ),
                input_schema=CadImportToolInput.model_json_schema(),
                output_schema=CadImportResult.model_json_schema(),
                permission="allow",
                risk="draft_edit",
                has_side_effect=True,
                preview_supported=True,
                idempotency="non_idempotent",
                audit_event="tool.import_cad_drawing",
                surfaces=["mcp", "rest", "agent"],
                tags=["import", "cad", "dwg", "dxf", "drawing", "cli"],
            ),
            ToolDefinition(
                name="update_project_settings",
                description="Update project-level settings (name, standard, revision metadata).",
                input_schema=_object_schema("Project settings document."),
                output_schema=_object_schema("Persisted project settings."),
                permission="allow",
                risk="draft_edit",
                has_side_effect=True,
                idempotency="non_idempotent",
                audit_event="tool.update_project_settings",
                surfaces=["rest"],
                tags=["project", "metadata"],
            ),
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
                name="preview_repair_finding",
                description=(
                    "Plan, compile and judge a self-repair for one canonical finding "
                    "without writing: every attempt, its failure code, the selected plan "
                    "and the success oracle's verdict. Read-only."
                ),
                input_schema=RepairFindingToolInput.model_json_schema(),
                output_schema=_object_schema("Canonical self-repair result (not applied)."),
                permission="allow",
                risk="read",
                audit_event="tool.preview_repair_finding",
                surfaces=["mcp", "rest"],
                tags=["repair", "preview", "validation", "agent"],
            ),
            ToolDefinition(
                name="repair_finding",
                description=(
                    "Repair one canonical finding through the governed write path: one "
                    "audited revision, and only after a shadow candidate passed the success "
                    "oracle. Cannot waive a finding or approve a release."
                ),
                input_schema=RepairFindingToolInput.model_json_schema(),
                output_schema=_object_schema("Canonical self-repair result (one write, or none)."),
                permission="ask",
                risk="engineering_change",
                has_side_effect=True,
                preview_supported=True,
                idempotency="depends_on_revision",
                audit_event="tool.repair_finding",
                surfaces=["mcp", "rest"],
                tags=["repair", "validation", "agent", "engineering-change"],
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
                name="get_engineering_graph",
                description=(
                    "Derive the engineering semantic graph of one drawing: equipment, "
                    "valves, instruments, pipelines, junctions, off-page connectors, "
                    "signals, topology edges and findings. Read-only; the graph is "
                    "derived from the document and never replaces it."
                ),
                input_schema=DocumentToolInput.model_json_schema(),
                output_schema=EngineeringGraph.model_json_schema(),
                permission="allow",
                risk="read",
                audit_event="tool.get_engineering_graph",
                surfaces=["mcp", "rest", "agent"],
                tags=["inspect", "engineering", "semantic", "graph"],
            ),
            ToolDefinition(
                name="trace_engineering_object",
                description=(
                    "Trace upstream/downstream engineering objects from a stable id, tag "
                    "key or tag, honouring declared flow direction and staying inside one "
                    "edge class (signal wiring vs process flow). Read-only."
                ),
                input_schema=EngineeringTraceToolInput.model_json_schema(),
                output_schema=TraceResult.model_json_schema(),
                permission="allow",
                risk="read",
                audit_event="tool.trace_engineering_object",
                surfaces=["mcp", "rest", "agent"],
                tags=["inspect", "topology", "trace"],
            ),
            ToolDefinition(
                name="find_engineering_object",
                description=(
                    "Locate one engineering object across the project's indexed drawings "
                    "by stable engineering id, tag key, tag or element id. Read-only; "
                    "rows written by an older builder version are skipped, not guessed."
                ),
                input_schema=EngineeringFindToolInput.model_json_schema(),
                output_schema={
                    "type": "array",
                    "items": EngineeringObjectMatch.model_json_schema(),
                },
                permission="allow",
                risk="read",
                audit_event="tool.find_engineering_object",
                surfaces=["mcp", "rest", "agent"],
                tags=["inspect", "project", "engineering", "identity"],
            ),
            ToolDefinition(
                name="get_project_engineering_graph",
                description=(
                    "Read the project-wide derived engineering graph: per-document "
                    "counts and freshness plus cross-document off-page connections."
                ),
                input_schema=_object_schema("No input; the project is the scope."),
                output_schema=ProjectEngineeringGraph.model_json_schema(),
                permission="allow",
                risk="read",
                audit_event="tool.get_project_engineering_graph",
                surfaces=["mcp", "rest", "agent"],
                tags=["inspect", "project", "engineering", "graph"],
            ),
            ToolDefinition(
                name="rebuild_project_index",
                description=(
                    "Rebuild the derived project engineering index. Deterministic and "
                    "idempotent: it recomputes cached engineering graphs from the "
                    "current documents and cannot change a drawing."
                ),
                input_schema=ProjectIndexRebuildToolInput.model_json_schema(),
                output_schema=RebuildReport.model_json_schema(),
                permission="allow",
                risk="draft_edit",
                has_side_effect=True,
                idempotency="idempotent",
                audit_event="engineering.index.rebuilt",
                surfaces=["mcp", "rest", "agent"],
                tags=["engineering", "index", "derived", "cache", "cli"],
            ),
            ToolDefinition(
                name="preview_semantic_diff",
                description=(
                    "Preview an engineering-oriented semantic diff for a proposed "
                    "transaction without modifying the document."
                ),
                input_schema=LowLevelTransactionToolInput.model_json_schema(),
                output_schema=SemanticDiffReport.model_json_schema(),
                permission="allow",
                risk="read",
                preview_supported=True,
                audit_event="tool.preview_semantic_diff",
                surfaces=["mcp", "rest", "agent"],
                tags=["inspect", "diff", "engineering", "approval"],
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
                name="apply_compiled_agent_transaction",
                description=(
                    "Apply an already compiled and validated low-level Agent transaction "
                    "through the atomic DocumentService write boundary."
                ),
                input_schema=LowLevelTransactionToolInput.model_json_schema(),
                output_schema=_object_schema("Applied TransactionResult and provenance."),
                permission="ask",
                risk="engineering_change",
                has_side_effect=True,
                preview_supported=True,
                idempotency="depends_on_revision",
                audit_event="tool.apply_compiled_agent_transaction",
                surfaces=["mcp", "rest", "agent"],
                tags=["apply", "compiled", "engineering-change"],
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
                name="get_drafting_report",
                description=(
                    "Measure one drawing against the drafting and drawing rules: "
                    "addressable ports, unbridged crossings, junction degrees, collisions, "
                    "reserved-space intrusions, lock provenance and the deterministic "
                    "pass/fail gate. Read-only; nothing is written and no edit is proposed."
                ),
                input_schema=DraftingToolInput.model_json_schema(),
                output_schema=DraftingReport.model_json_schema(),
                permission="allow",
                risk="read",
                audit_event="tool.get_drafting_report",
                surfaces=["mcp", "rest", "agent"],
                tags=["inspect", "drafting", "quality", "gate", "cli"],
            ),
            ToolDefinition(
                name="preview_deterministic_drafting",
                description=(
                    "Preview a deterministic drafting run (region relayout, port-aware "
                    "routing, annotation placement, crossing bridges, collision "
                    "relaxation) without writing: same content always produces the same "
                    "transaction digest. Locked geometry is never moved and a stage that "
                    "would worsen any hard drawing metric is rolled back."
                ),
                input_schema=DraftingToolInput.model_json_schema(),
                output_schema=DraftingPreview.model_json_schema(),
                permission="allow",
                risk="draft_edit",
                preview_supported=True,
                idempotency="idempotent",
                audit_event="tool.preview_deterministic_drafting",
                surfaces=["mcp", "rest", "agent"],
                tags=["drafting", "layout", "routing", "annotation", "preview", "cli"],
            ),
            ToolDefinition(
                name="apply_deterministic_drafting",
                description=(
                    "Recompute and atomically apply a deterministic drafting run at an "
                    "expected revision through the governed write path."
                ),
                input_schema=DraftingToolInput.model_json_schema(),
                output_schema=_object_schema("Drafting preview and apply result."),
                permission="allow",
                risk="draft_edit",
                has_side_effect=True,
                preview_supported=True,
                idempotency="depends_on_revision",
                audit_event="tool.apply_deterministic_drafting",
                surfaces=["mcp", "agent"],
                tags=["drafting", "layout", "draft-edit"],
            ),
            ToolDefinition(
                name="draw_text_plan",
                description=(
                    "Draw one new P&ID from a single natural-language sentence: code reads the "
                    "sentence into catalogue-backed candidates, System One judges only what a "
                    "lookup cannot decide, the frozen deterministic chain lays the drawing out, "
                    "and the phase-3 materializer commits it through the one governed writer. "
                    "The target document must be empty; an existing drawing is never overwritten. "
                    "A dry_run plans and finalizes without writing."
                ),
                input_schema=_object_schema(
                    "Document id, the sentence, provider config, and the dry_run flag."
                ),
                output_schema=_object_schema(
                    "The committed revision, the planned specification, layout identities, "
                    "and the notes/skipped report."
                ),
                permission="allow",
                risk="engineering_change",
                has_side_effect=True,
                preview_supported=True,
                idempotency="depends_on_revision",
                audit_event="tool.draw_text_plan",
                surfaces=["rest"],
                tags=["draw", "natural-language", "m7", "engineering-change"],
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
