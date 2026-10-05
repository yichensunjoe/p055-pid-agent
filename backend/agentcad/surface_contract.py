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
    # M9-WS1: review/approval governance writes. Like engineering_write these are
    # audited and CAS-guarded, but they deliberately never move an engineering
    # revision or any engineering digest.
    "governance_write",
]

#: Categories that must produce an audit record.
AUDITED_CATEGORIES: frozenset[str] = frozenset(
    {
        "engineering_write",
        "legacy_write",
        "project_metadata",
        "harness_lifecycle",
        # M9-WS1 Round-2 Gate freeze: governance writes (review threads, comments,
        # resolve/reopen, approval request/decide) always carry an audit fact —
        # and, since the unified reconcile, so does every approval.invalidated
        # they trigger. The bootstrap endpoint is deliberately NOT here: it is
        # runtime (hands out a token, writes no governance state, audits nothing;
        # the token never enters the audit chain).
        "governance_write",
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
    # --- M9-WS1 review workflow (governance plane: never an engineering write) ---
    _http(
        "POST",
        "/api/v2/review/operator-session",
        "runtime",
        tool="bootstrap_operator_session",
        notes="Loopback-only; local deployments with an operator identity only. "
        "Hands out the per-process operator token and writes no governance state, "
        "so it is runtime, not governance_write, and audits nothing — the token "
        "never enters the audit chain. Verified by "
        "test_review_operator_session_refuses_non_loopback and "
        "test_valid_operator_token_from_non_loopback_peer_is_refused.",
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/review/threads",
        "governance_write",
        tool="create_review_thread",
        audited=True,
        notes="Governance CAS on expected_governance_seq; never moves the engineering "
        "revision. A new open thread invalidates every live approval "
        "(review_digest_changed) with an approval.invalidated audit in the same "
        "transaction. Verified by test_review_governance_never_moves_engineering_revision.",
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/review/threads/{thread_id}/comments",
        "governance_write",
        tool="add_review_comment",
        audited=True,
        notes="Any comment after an approval invalidates it (review_digest_changed) "
        "via the unified reconcile, atomically with this mutation's audit.",
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/review/threads/{thread_id}/resolve",
        "governance_write",
        tool="resolve_review_thread",
        audited=True,
        notes="Operator token required. Verified by test_review_agent_cannot_resolve.",
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/review/threads/{thread_id}/reopen",
        "governance_write",
        tool="reopen_review_thread",
        audited=True,
        notes="Operator token required; invalidates live approvals.",
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/approval/request",
        "governance_write",
        tool="request_engineering_approval",
        audited=True,
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/approval/{approval_id}/decide",
        "governance_write",
        tool="decide_engineering_approval",
        audited=True,
        notes="Operator token required; decision-time fresh readiness gate. "
        "Verified by test_review_approval_roundtrip_blocks_open_threads.",
    ),
    # --- M9-WS2 release + evidence package (consumes WS1 approval facts) ---
    _http(
        "POST",
        "/api/v2/documents/{document_id}/releases",
        "governance_write",
        tool="release_document",
        audited=True,
        notes="M9-WS2 one-shot formal release. Frozen guard order: actor trust → "
        "open threads → approval live → one-live-release-per-binding → fresh "
        "readiness → package build; Phase B is the single atomic commit_release "
        "primitive (revision recheck + governance CAS + state + BLOB row + "
        "release.released audit in one transaction). F1/F2/F3 record exactly one "
        "audit-only release.denied; F4 untrusted and F6/F7 conflicts record none. "
        "Verified by test_m9_release_workflow.",
    ),
    _read("/api/v2/documents/{document_id}/releases/{release_id}/evidence.zip"),
    # --- M11-D4: minimal read-only Cable surface (Gate-frozen, 3 GETs) ---
    _read("/api/v2/cable/documents"),
    _read("/api/v2/cable/documents/{document_id}"),
    _read("/api/v2/cable/documents/{document_id}/export.zip"),
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
    # --- engineering self-repair (M5) ----------------------------------------- #
    # One finding, one governed write at most, and no private write path: the preview
    # route is a read, and the apply route goes through the same orchestrator the CLI, the
    # MCP tools and the benchmark use. ``repair_finding`` is declared for it because the
    # route and the tool are one operation reached two ways, not two implementations.
    _http(
        "POST",
        "/api/v2/documents/{document_id}/repair/preview",
        "read",
        tool="preview_repair_finding",
        notes=(
            "Plans, compiles and judges a candidate on a shadow copy; nothing is written. "
            "Verified by test_repair_surfaces.py."
        ),
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/repair",
        "engineering_write",
        tool="repair_finding",
        audited=True,
        notes=(
            "One repair, one audited revision, and only after a shadow candidate passed the "
            "success oracle; a refused candidate leaves the drawing byte-identical."
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
    # --- TypeSafe: a second credential, and a plan that is judged rather than generated ---------
    _http(
        "POST",
        "/api/v2/provider/typesafe/verify",
        "runtime",
        notes="TypeSafe credential probe: one judgment, nothing written to any document.",
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/agent/typesafe-plan",
        "agent_runtime",
        notes=(
            "Planning only, and judged rather than generated: candidates come from the document "
            "and the symbol catalogue, System One chooses among them, and the result goes through "
            "the same compiler assessment and apply-v2 harness gate as plan-v2."
        ),
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/agent/text-plan",
        "engineering_write",
        tool="draw_text_plan",
        audited=True,
        notes=(
            "One sentence -> DiagramSpec -> the frozen deterministic chain -> the phase-3 "
            "materializer's one governed write. Preflight refuses a non-empty target, so the "
            "route can create a drawing but never overwrite one; dry_run previews without "
            "writing. Verified by test_text_plan_surface.py."
        ),
    ),
    _http(
        "POST",
        "/api/v2/documents/{document_id}/agent/text-edit",
        "engineering_write",
        tool="edit_text_plan",
        audited=True,
        notes=(
            "One sentence edits the stored semantic spec (never the pixels), the frozen "
            "chain recomputes the whole drawing, and one existing-writer transaction "
            "replaces the prior materialization exactly. Stale revision or digest is "
            "refused, foreign/human drift is refused before the write, and the new spec "
            "row commits in the same transaction as the revision. Verified by "
            "test_text_edit_surface.py."
        ),
    ),
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
    # --- M13-D5: governed change-set surface ------------------------------------
    # Stage persists a change-set governance row via the sealed D3 analyzer +
    # zero-write shadow previewer — never moves an engineering revision or
    # digest; it records one governance-plane audit fact. Apply routes ONLY
    # into the sealed M10/D4 flow (session -> approval -> authorization ->
    # atomic executor) and inherits its exactly-one applied/rejected audit;
    # shared deployments fail closed (approval_not_self_served).
    _http(
        "POST",
        "/api/v2/projects/{project_id}/change-sets",
        "governance_write",
        audited=True,
        notes=(
            "Stages a governed change set: D3 impact analysis + zero-write shadow "
            "preview persisted via D2 primitives; zero engineering writes. "
            "Verified by test_stage_runs_shadow_preview_with_zero_engineering_writes."
        ),
    ),
    _http(
        "POST",
        "/api/v2/projects/{project_id}/change-sets/{change_set_id}/apply",
        "governance_write",
        audited=True,
        notes=(
            "Applies via the sealed M10/D4 governed runtime only — no copied domain "
            "logic. Local mode may auto-resolve the operator approval; shared mode "
            "consumes only a human-resolved approval from the approval-requests "
            "endpoint (approval_not_self_served otherwise). Verified by "
            "test_apply_fail_closed_in_shared_mode and "
            "test_shared_full_chain_with_human_approval."
        ),
    ),
    # D86-1: the project-change approval factory. Creates the M10 session +
    # approval rows bound to the SEALED project-change runtime (the generic
    # /agent/sessions/{id}/approvals surface cannot: its registry does not
    # know apply_project_change_set). A human resolves via the existing
    # /agent/approvals/{id}/resolve route; apply consumes the exact binding.
    _http(
        "POST",
        "/api/v2/projects/{project_id}/change-sets/{change_set_id}/approval-requests",
        "harness_lifecycle",
        audited=True,
        notes=(
            "Session/approval bookkeeping only — no engineering write. The "
            "approval intent hash binds the exact stored change-set intent. "
            "Verified by test_shared_full_chain_with_human_approval."
        ),
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
        has_side_effect=category not in {"read", "agent_runtime"},
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
    # M4 validation surfaces. All three are reads: validation never writes a revision,
    # and readiness is evidence rather than an approval. Any invocation audit they record
    # is a read/tool event (``validation.completed`` / ``release.readiness.assessed``),
    # never ``revision.created``.
    mcp("inspect_validation_profile", "read", tool="inspect_validation_profile"),
    mcp("validate_document", "read", tool="validate_document"),
    mcp("assess_release_readiness", "read", tool="assess_release_readiness"),
    # M5 self-repair. The preview is a read; the apply is one governed write and records
    # ``repair.completed`` / ``repair.refused`` evidence bound to the revision it describes.
    mcp("preview_repair_finding", "read", tool="preview_repair_finding"),
    mcp(
        "repair_finding",
        "engineering_write",
        tool="repair_finding",
        audited=True,
        notes=(
            "Repairs one canonical finding through the governed path. Declines, policy "
            "refusals and oracle failures write nothing."
        ),
    ),
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
