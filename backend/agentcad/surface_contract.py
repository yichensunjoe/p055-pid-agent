"""Declared contract between every mutating surface and the Tool Registry.

Charter reference: §7 (no surface may bypass the permission gate), §19 (audit),
§21.6 (reviewability). P0 asks the impossible-to-answer question *"is there a
hidden write path?"* to be answered mechanically instead of by reading code.

This module is the single place that says, for every HTTP route and every MCP
tool, what kind of surface it is and — if it mutates the engineering model —
which registry tool governs it and whether it must leave an audit record.

``backend/tests/test_surface_contract.py`` enumerates the live FastAPI OpenAPI
schema and the live MCP tool names and fails if anything is missing, if a mutating
surface points at an unknown tool, or if a side-effecting tool is unreachable.

Deliberate design choices
-------------------------
* The contract is *declarative data*, not behaviour: nothing at import time can
  grant permission. It is a test oracle and a review document.
* Routes are matched by method + path template (openapi style, ``{id}``).
* ``category`` is honest about why a mutating route is not an engineering change:
  ``runtime`` (process/symbol/config state), ``harness_lifecycle`` (session and
  approval bookkeeping), ``agent_runtime`` (planning/provider calls that do not
  write the document) and ``model_acceptance`` (throws away its own scratch DB).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .models import StrictModel

SurfaceCategory = Literal[
    "read",
    "engineering_write",
    "legacy_write",
    "harness_lifecycle",
    "agent_runtime",
    "runtime",
    "model_acceptance",
    "project_metadata",
]

#: Categories that must produce an audit record.
AUDITED_CATEGORIES: frozenset[str] = frozenset(
    {
        "engineering_write",
        "legacy_write",
        "project_metadata",
        "harness_lifecycle",
    }
)


#: Tools that mutate a document with permission='allow' instead of 'ask'.
#: Each entry is an explicit, reviewable policy decision, and the bar for adding one
#: is: the change is reversible at an expected revision, it cannot create/remove/
#: reconnect engineering objects, and the audit record still captures
#: actor/tool/session/revision. Adding an entry here is a visible contract change.
DRAFT_EDIT_EXCEPTIONS: dict[str, str] = {
    "create_document": (
        "Creates a new empty document. It cannot modify, invalidate or leak any "
        "existing engineering content, and the creation itself is audited."
    ),
    "apply_auto_layout": (
        "Deterministic, previewed repositioning and pipe re-routing of existing "
        "elements at an expected revision. It cannot add, remove or reconnect "
        "engineering objects and is reversible with undo."
    ),
    "import_cad_drawing": (
        "Creates one *new* document from a CAD file; it cannot modify, re-version or "
        "delete any existing document, and the source is identified by SHA-256 in both "
        "the document metadata and the audit record. Every element is written through "
        "the ordinary transaction path, so the import inherits the revision check and "
        "is undoable like any other edit. Anything the importer could not reproduce is "
        "reported rather than guessed at."
    ),
    "apply_deterministic_drafting": (
        "The M3 drafting pipeline: it recomputes a preview and applies it inside the "
        "same governed transaction, so it inherits the revision check, the audit record "
        "and the undo history. The engine itself is preview-only and provably cannot "
        "touch connectivity: it emits update operations only, refuses to edit a locked "
        "or out-of-scope element, and reports DRAFT_TOPOLOGY_CHANGED / "
        "DRAFT_OUT_OF_SCOPE_CHANGE / DRAFT_LOCKED_ELEMENT_MOVED as blockers if a "
        "result ever violated that (see tests/test_drafting_engine.py)."
    ),
}


class SurfaceBinding(StrictModel):
    """One declared HTTP or MCP surface."""

    surface: Literal["http", "mcp"]
    name: str = Field(min_length=1)
    methods: list[str] = Field(default_factory=list)
    category: SurfaceCategory
    tool: str | None = None
    has_side_effect: bool = False
    audited: bool = False
    notes: str = ""

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        methods = ",".join(self.methods) or "-"
        return f"{self.surface}:{methods} {self.name}"


def _http(
    method: str,
    path: str,
    category: SurfaceCategory,
    *,
    tool: str | None = None,
    audited: bool = False,
    notes: str = "",
) -> SurfaceBinding:
    return SurfaceBinding(
        surface="http",
        name=path,
        methods=[method],
        category=category,
        tool=tool,
        has_side_effect=True,
        audited=audited,
        notes=notes,
    )


def _read(path: str, method: str = "GET") -> SurfaceBinding:
    return SurfaceBinding(surface="http", name=path, methods=[method], category="read")


LEGACY_V1_TOOL = "apply_compiled_agent_transaction"

#: Every mutating HTTP route in the live application.
HTTP_SURFACE_BINDINGS: tuple[SurfaceBinding, ...] = (
    # --- v2 document lifecycle -------------------------------------------------
    _http(
        "POST",
        "/api/v2/documents",
        "engineering_write",
        tool="create_document",
        audited=True,
    ),
    _http(
        "DELETE",
        "/api/v2/documents/{document_id}",
        "engineering_write",
        tool="delete_document",
        audited=True,
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/transactions",
        "engineering_write",
        tool="apply_web_transaction",
        audited=True,
        notes="Human editor path; server-derived attribution, not the Agent gate.",
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/undo",
        "engineering_write",
        tool="undo_document",
        audited=True,
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/redo",
        "engineering_write",
        tool="redo_document",
        audited=True,
    ),
    _http(
        "PUT",
        "/api/v2/documents/{document_id}/name",
        "engineering_write",
        tool="rename_document",
        audited=True,
    ),
    _http(
        "PUT",
        "/api/v2/documents/{document_id}/folder",
        "engineering_write",
        tool="move_document_folder",
        audited=True,
    ),
    _http(
        "PUT",
        "/api/v2/documents/{document_id}/canvas-grid",
        "read",
        notes=(
            "Returns a projected editor view only. Verified by "
            "test_canvas_grid_endpoint_does_not_create_a_revision."
        ),
    ),
    # --- layout / preflight (no write) ----------------------------------------
    _http(
        "POST",
        "/api/v2/documents/{document_id}/layout/preview",
        "read",
        tool="preview_auto_layout",
        notes="Preview only; the write path is the MCP/REST apply tool.",
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/drafting/report",
        "read",
        tool="get_drafting_report",
        notes="Drafting analysis of one drawing state; no write and no proposed edit.",
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/drafting/preview",
        "read",
        tool="preview_deterministic_drafting",
        notes=(
            "Returns the reproducible transaction; the write path is the ordinary "
            "governed transaction channel, so drafting has no private write path."
        ),
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/transactions/analyze",
        "read",
        tool="analyze_transaction",
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/transactions/validate",
        "read",
        tool="validate_transaction",
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/transactions/semantic-diff",
        "read",
        tool="preview_semantic_diff",
    ),
    # --- governed agent apply --------------------------------------------------
    _http(
        "POST",
        "/api/v2/documents/{document_id}/agent/apply-v2",
        "engineering_write",
        tool="apply_compiled_agent_transaction",
        audited=True,
        notes="Permission 'ask'; approval must match the exact intent hash.",
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/agent/apply",
        "engineering_write",
        tool="apply_compiled_agent_transaction",
        audited=True,
        notes="Legacy preview+apply endpoint; same harness gate as apply-v2.",
    ),
    # --- agent planning / provider runtime -------------------------------------
    _http(
        "POST",
        "/api/v2/documents/{document_id}/agent/plan-v2",
        "agent_runtime",
        notes="Planning only; nothing is written to the document.",
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/agent/plan-v2-stream",
        "agent_runtime",
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/agent/replan",
        "agent_runtime",
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/agent/generate",
        "engineering_write",
        tool="apply_compiled_agent_transaction",
        audited=True,
        notes=(
            "Legacy one-shot generate+apply. Must be reached only through the "
            "harness; see test_legacy_generate_requires_harness_approval."
        ),
    ),
    _http("POST", "/api/v2/agent/provider/models", "runtime", notes="Provider catalog read."),
    _http("POST", "/api/v2/agent/provider/test", "runtime", notes="Provider connectivity probe."),
    # --- harness lifecycle ------------------------------------------------------
    _http("POST", "/api/v2/agent/sessions", "harness_lifecycle", audited=True),
    _http(
        "POST",
        "/api/v2/agent/sessions/{session_id}/approvals",
        "harness_lifecycle",
        audited=True,
    ),
    _http(
        "POST",
        "/api/v2/agent/approvals/{approval_id}/resolve",
        "harness_lifecycle",
        audited=True,
    ),
    # --- derived engineering index ----------------------------------------------
    _http(
        "POST",
        "/api/v2/project/index/rebuild",
        "runtime",
        tool="rebuild_project_index",
        audited=True,
        notes=(
            "Recomputes the derived project engineering graph cache. Deterministic and "
            "idempotent, cannot change any drawing; the audit fact records the rebuild, "
            "not a revision. Verified by test_read_routes_do_not_change_stored_state."
        ),
    ),
    # --- CAD (DWG/DXF) import ---------------------------------------------------
    # ``GET /api/v2/imports/cad/capabilities`` is deliberately absent: the contract
    # enumerates *mutating* methods, and ``test_declared_routes_all_exist`` treats a
    # declared read route as stale. Its read-only nature is asserted directly by
    # ``test_capabilities_route_writes_nothing`` in tests/test_cad_api.py.
    # --- project metadata -------------------------------------------------------
    _http(
        "POST",
        "/api/v2/imports/document",
        "project_metadata",
        tool="import_document_payload",
        audited=True,
    ),
    _http(
        "POST",
        "/api/v2/imports/project-package",
        "project_metadata",
        tool="import_project_payload",
        audited=True,
    ),
    _http(
        "POST",
        "/api/v2/imports/cad",
        "project_metadata",
        tool="import_cad_drawing",
        audited=True,
        notes=(
            "Creates one new document from an uploaded DWG/DXF: geometry, layers and "
            "text only, no engineering semantics. Existing documents cannot be "
            "modified, re-versioned or deleted by this route."
        ),
    ),
    _http(
        "POST",
        "/api/v2/imports/cad/plan",
        "read",
        tool="import_cad_drawing",
        notes=(
            "Dry run: decodes the same body and returns the import report without "
            "creating a document or writing anything."
        ),
    ),
    _http(
        "PUT",
        "/api/v2/project/settings",
        "project_metadata",
        tool="update_project_settings",
        audited=True,
    ),
    # --- process runtime --------------------------------------------------------
    _http("POST", "/api/v2/symbols/reload", "runtime", notes="Reloads the symbol catalog."),
    _http(
        "POST",
        "/api/v2/acceptance/model-matrix",
        "model_acceptance",
        notes="Runs against throwaway scratch databases; never the project store.",
    ),
    # --- legacy v1 compatibility ------------------------------------------------
    *(
        _http(
            method,
            path,
            "legacy_write",
            tool=LEGACY_V1_TOOL,
            audited=True,
            notes="Legacy primitive/layer compatibility write, attributed as legacy_v1.*",
        )
        for method, path in (
            ("POST", "/api/v1/draw/line"),
            ("POST", "/api/v1/draw/circle"),
            ("POST", "/api/v1/draw/rectangle"),
            ("POST", "/api/v1/draw/polyline"),
            ("POST", "/api/v1/draw/text"),
            ("POST", "/api/v1/draw/symbol"),
            ("POST", "/api/v1/draw/batch"),
            ("DELETE", "/api/v1/primitives/{primitive_id}"),
            ("DELETE", "/api/v1/clear"),
            ("POST", "/api/v1/undo"),
            ("POST", "/api/v1/redo"),
            ("POST", "/api/v1/layers"),
            ("DELETE", "/api/v1/layers/{name}"),
            ("PATCH", "/api/v1/layers/{name}/visibility"),
        )
    ),
)


def mcp(
    name: str,
    category: SurfaceCategory,
    *,
    tool: str | None = None,
    audited: bool = False,
    notes: str = "",
) -> SurfaceBinding:
    return SurfaceBinding(
        surface="mcp",
        name=name,
        category=category,
        tool=tool,
        has_side_effect=category
        not in {"read", "agent_runtime"},
        audited=audited,
        notes=notes,
    )


#: Every tool exposed by the MCP server (kept in lockstep with the live server by test).
MCP_SURFACE_BINDINGS: tuple[SurfaceBinding, ...] = (
    mcp("get_server_info", "read"),
    mcp("get_tool_registry", "read"),
    mcp("get_diagnostics", "read"),
    mcp("list_documents", "read"),
    mcp("create_document", "engineering_write", tool="create_document", audited=True),
    mcp("get_scene_summary", "read", tool="get_scene_summary"),
    mcp("get_document", "read", tool="get_document"),
    mcp("get_document_history", "read", tool="get_document_history"),
    mcp("analyze_transaction", "read", tool="analyze_transaction"),
    mcp("validate_transaction", "read", tool="validate_transaction"),
    mcp("preview_semantic_diff", "read", tool="preview_semantic_diff"),
    mcp("compile_agent_transaction", "read", tool="compile_agent_transaction"),
    mcp(
        "apply_agent_transaction",
        "engineering_write",
        tool="apply_agent_transaction",
        audited=True,
    ),
    mcp("preview_auto_layout", "read", tool="preview_auto_layout"),
    mcp("apply_auto_layout", "engineering_write", tool="apply_auto_layout", audited=True),
    mcp("get_drafting_report", "read", tool="get_drafting_report"),
    mcp("preview_deterministic_drafting", "read", tool="preview_deterministic_drafting"),
    mcp(
        "apply_deterministic_drafting",
        "engineering_write",
        tool="apply_deterministic_drafting",
        audited=True,
    ),
    mcp(
        "apply_transaction_v2",
        "engineering_write",
        tool="apply_compiled_agent_transaction",
        audited=True,
    ),
    mcp(
        "apply_transaction",
        "engineering_write",
        tool="apply_compiled_agent_transaction",
        audited=True,
        notes="Legacy JSON entrypoint; still gated by Harness approval.",
    ),
    mcp("start_agent_session", "harness_lifecycle", audited=True),
    mcp("request_agent_tool_approval", "harness_lifecycle", audited=True),
    mcp("resolve_agent_tool_approval", "harness_lifecycle", audited=True),
    mcp("get_agent_session_audit", "read"),
    mcp("get_audit_trail", "read"),
    mcp("verify_audit_chain", "read"),
    mcp("get_revision_evidence", "read"),
    mcp("get_engineering_graph", "read", tool="get_engineering_graph"),
    mcp("trace_engineering_object", "read", tool="trace_engineering_object"),
    mcp("find_engineering_object", "read", tool="find_engineering_object"),
    mcp("get_project_engineering_graph", "read", tool="get_project_engineering_graph"),
    mcp("rebuild_project_index", "runtime", tool="rebuild_project_index", audited=True),
    mcp(
        "import_cad_drawing",
        "engineering_write",
        tool="import_cad_drawing",
        audited=True,
        notes=(
            "Imports a drawing from a path on the server host. Only files that decode "
            "as DXF/DWG are accepted, and the source SHA-256 is recorded."
        ),
    ),
    mcp("list_symbols", "read"),
    mcp("get_transaction_schema", "read"),
    mcp("get_agent_transaction_schema", "read"),
)


def http_binding(method: str, path: str) -> SurfaceBinding | None:
    for binding in HTTP_SURFACE_BINDINGS:
        if binding.name == path and method.upper() in binding.methods:
            return binding
    return None


def mcp_binding(name: str) -> SurfaceBinding | None:
    for binding in MCP_SURFACE_BINDINGS:
        if binding.name == name:
            return binding
    return None


def mutating_bindings() -> list[SurfaceBinding]:
    return [binding for binding in HTTP_SURFACE_BINDINGS if binding.has_side_effect]


__all__ = [
    "AUDITED_CATEGORIES",
    "DRAFT_EDIT_EXCEPTIONS",
    "HTTP_SURFACE_BINDINGS",
    "LEGACY_V1_TOOL",
    "MCP_SURFACE_BINDINGS",
    "SurfaceBinding",
    "SurfaceCategory",
    "http_binding",
    "mcp_binding",
    "mutating_bindings",
]
