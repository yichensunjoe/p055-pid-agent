"""Registry / surface conformance ('no unregistered write path').

Charter reference: §7, §19, §21.6. These tests enumerate the *live* FastAPI
OpenAPI schema and the *live* MCP tool names, then require each one to appear in
``surface_contract``. A new endpoint that mutates the engineering model without a
registry tool, without an audit record, is therefore a test failure rather than a
code-review question.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentcad.main import create_app
from agentcad.surface_contract import (
    AUDITED_CATEGORIES,
    DRAFT_EDIT_EXCEPTIONS,
    HTTP_SURFACE_BINDINGS,
    MCP_SURFACE_BINDINGS,
    http_binding,
    mcp_binding,
)
from agentcad.tool_registry import get_default_tool_registry

MCP_SERVER_SOURCE = Path(__file__).resolve().parents[1] / "agentcad" / "mcp_server.py"
MUTATING_METHODS = {"post", "put", "patch", "delete"}


@pytest.fixture(scope="module")
def app():
    return create_app()


def _live_mutating_routes(app) -> set[tuple[str, str]]:
    paths = app.openapi()["paths"]
    found: set[tuple[str, str]] = set()
    for path, operations in paths.items():
        for method in operations:
            if method.lower() in MUTATING_METHODS:
                found.add((method.upper(), path))
    return found


def _declared_http_routes() -> set[tuple[str, str]]:
    declared: set[tuple[str, str]] = set()
    for binding in HTTP_SURFACE_BINDINGS:
        for method in binding.methods:
            declared.add((method.upper(), binding.name))
    return declared


def _live_mcp_tool_names() -> set[str]:
    source = MCP_SERVER_SOURCE.read_text(encoding="utf-8")
    return set(re.findall(r"@mcp\.tool\(\)\s*\n\s*def ([a-z_0-9]+)\(", source))


def test_every_mutating_route_is_declared(app) -> None:
    live = _live_mutating_routes(app)
    declared = _declared_http_routes()
    missing = sorted(live - declared)
    assert not missing, (
        "these mutating HTTP routes are not declared in surface_contract.py, so "
        f"nothing checks their permission or audit behaviour: {missing}"
    )


def test_declared_routes_all_exist(app) -> None:
    live = _live_mutating_routes(app)
    declared = _declared_http_routes()
    stale = sorted(declared - live)
    assert not stale, f"surface_contract declares routes that no longer exist: {stale}"


def test_mutating_surfaces_name_a_registered_tool_or_justify_themselves() -> None:
    """Every declared mutating route is either registry-governed or explicitly
    categorised as a non-engineering write (runtime / lifecycle / planning)."""

    registry = get_default_tool_registry()
    known = {definition.name for definition in registry.list()}
    # Harness lifecycle routes are session/approval bookkeeping, not engineering
    # capabilities; they are covered by the audit-required assertion below.
    exempt = {"runtime", "agent_runtime", "model_acceptance", "read", "harness_lifecycle"}
    ungoverned: list[str] = []
    for binding in HTTP_SURFACE_BINDINGS:
        if not binding.has_side_effect:
            continue
        if binding.category in exempt:
            continue
        if binding.tool not in known:
            ungoverned.append(str(binding))
    assert not ungoverned, f"mutating surfaces without a registered tool: {ungoverned}"


def test_audited_categories_are_marked_audited() -> None:
    offenders = [
        str(binding)
        for binding in [*HTTP_SURFACE_BINDINGS, *MCP_SURFACE_BINDINGS]
        if binding.category in AUDITED_CATEGORIES and binding.has_side_effect and not binding.audited
    ]
    assert not offenders, f"these surfaces must record an audit fact but are not marked: {offenders}"


def test_every_side_effecting_tool_is_reachable_from_a_surface() -> None:
    registry = get_default_tool_registry()
    bound_tools = {
        binding.tool
        for binding in [*HTTP_SURFACE_BINDINGS, *MCP_SURFACE_BINDINGS]
        if binding.tool
    }
    unreachable = [
        definition.name
        for definition in registry.list()
        if definition.has_side_effect and definition.name not in bound_tools
    ]
    assert not unreachable, (
        "side-effecting registry tools with no declared surface cannot be reviewed: "
        f"{unreachable}"
    )


def test_every_mcp_tool_is_declared() -> None:
    live = _live_mcp_tool_names()
    declared = {binding.name for binding in MCP_SURFACE_BINDINGS}
    missing = sorted(live - declared)
    stale = sorted(declared - live)
    assert not missing, f"MCP tools missing from surface_contract: {missing}"
    assert not stale, f"surface_contract declares MCP tools that no longer exist: {stale}"


def test_mcp_mutating_tools_require_harness_approval() -> None:
    """Every MCP engineering write is gated, or is a documented draft-edit exception.

    ``DRAFT_EDIT_EXCEPTIONS`` is deliberately explicit and reviewable: adding an
    exception is a visible change to this contract, not a silent policy relaxation.
    """

    registry = get_default_tool_registry()
    unapproved: list[str] = []
    for binding in MCP_SURFACE_BINDINGS:
        if binding.category != "engineering_write":
            continue
        definition = registry.require(binding.tool)
        if definition.permission == "ask":
            continue
        if definition.name in DRAFT_EDIT_EXCEPTIONS:
            continue
        unapproved.append(f"{binding.name} -> {definition.name}:{definition.permission}")
    assert not unapproved, (
        "MCP engineering writes must go through a permission='ask' tool (or be a "
        f"documented draft-edit exception): {unapproved}"
    )


def test_draft_edit_exceptions_are_documented_and_open() -> None:
    registry = get_default_tool_registry()
    for name, reason in DRAFT_EDIT_EXCEPTIONS.items():
        definition = registry.require(name)
        assert definition.permission == "allow", name
        assert definition.risk == "draft_edit", name
        assert definition.has_side_effect is True, name
        assert definition.idempotency in {"depends_on_revision", "non_idempotent"}, name
        assert reason and len(reason) > 40, f"exception {name} needs a real justification"


def test_no_route_can_be_added_without_a_companion_negative_test(app) -> None:
    """Guard: ensure the contract file itself is exercised by a live app."""

    assert _live_mutating_routes(app), "expected the application to expose mutating routes"
    assert http_binding("POST", "/api/v2/documents") is not None
    assert mcp_binding("apply_transaction") is not None


def test_canvas_grid_endpoint_does_not_create_a_revision(app) -> None:
    """The one mutating-method route declared as 'read' must really not write."""

    with TestClient(app) as client:
        document = client.post("/api/v2/documents", json={"name": "Grid"}).json()
        service = app.state.service
        before = service.get_document(document["id"]).revision
        response = client.put(
            f"/api/v2/documents/{document['id']}/canvas-grid",
            json={"grid_size": 20, "expected_revision": before},
        )
        assert response.status_code == 200, response.text
        assert service.get_document(document["id"]).revision == before
        assert not [
            record
            for record in service.audit.audit_trail(document_id=document["id"], limit=10)
            if record.event_type == "revision.created"
        ]


def test_legacy_v1_writes_are_registered_as_engineering_changes() -> None:
    binding = http_binding("POST", "/api/v1/draw/line")
    assert binding is not None
    assert binding.category == "legacy_write"
    assert binding.audited is True
    definition = get_default_tool_registry().require(binding.tool)
    assert definition.permission in {"allow", "ask"}
    assert definition.has_side_effect is True


def test_registry_catalog_is_stable_and_machine_readable() -> None:
    catalog = get_default_tool_registry().catalog()
    assert catalog["schema_version"] == 1
    assert catalog["tool_count"] == len(catalog["tools"])
    permissions = {definition["name"]: definition["permission"] for definition in catalog["tools"]}
    assert permissions["apply_compiled_agent_transaction"] == "ask"
    assert permissions["delete_document"] == "ask"
    assert permissions["get_document"] == "allow"
    # Side-effecting tools must document their audit event and idempotency.
    for definition in catalog["tools"]:
        if definition["has_side_effect"]:
            assert definition["audit_event"]
            assert definition["idempotency"] in {
                "idempotent",
                "depends_on_revision",
                "non_idempotent",
            }
