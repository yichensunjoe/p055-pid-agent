from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from agentcad.agent_semantic_models import SemanticTransaction
from agentcad.config import Settings
from agentcad.main import create_app
from agentcad.mcp_server import _tool_registry_catalog
from agentcad.tool_registry import ToolDefinition, ToolRegistry, get_default_tool_registry


def _definition(name: str = "sample_tool") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Sample deterministic tool.",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permission="allow",
        risk="read",
        audit_event=f"tool.{name}",
        surfaces=["agent"],
    )


def test_tool_registry_rejects_duplicate_names():
    definition = _definition()
    registry = ToolRegistry([definition])

    with pytest.raises(ValueError, match="duplicate tool definition"):
        registry.register(definition)


def test_default_tool_registry_encodes_permission_and_schema_contracts():
    registry = get_default_tool_registry()
    names = [item.name for item in registry.list()]

    assert names == sorted(names)
    assert len(names) >= 10
    assert len(names) == len(set(names))

    planner_tool = registry.require("plan_pid_agent_semantic_transaction")
    assert planner_tool.input_schema == SemanticTransaction.model_json_schema()
    assert planner_tool.permission == "allow"
    assert planner_tool.has_side_effect is False

    engineering_change = registry.require("apply_agent_transaction")
    assert engineering_change.permission == "ask"
    assert engineering_change.risk == "engineering_change"
    assert engineering_change.has_side_effect is True
    assert engineering_change.preview_supported is True
    assert engineering_change.idempotency == "depends_on_revision"

    draft_edit = registry.require("apply_auto_layout")
    assert draft_edit.permission == "allow"
    assert draft_edit.risk == "draft_edit"
    assert draft_edit.has_side_effect is True


def test_mcp_catalog_uses_same_canonical_registry():
    assert _tool_registry_catalog() == get_default_tool_registry().catalog()


def test_rest_catalog_and_semantic_schema_share_one_definition(tmp_path: Path):
    app = create_app(
        Settings(
            database_path=tmp_path / "tool-registry.db",
            cors_origins=["http://localhost:5173"],
            frontend_dist=tmp_path / "missing-dist",
        )
    )
    client = TestClient(app)

    catalog_response = client.get("/api/v2/agent/tools")
    assert catalog_response.status_code == 200
    catalog = catalog_response.json()
    assert catalog["schema_version"] == 1
    assert catalog["tool_count"] == len(catalog["tools"])

    by_name = {item["name"]: item for item in catalog["tools"]}
    canonical = by_name["plan_pid_agent_semantic_transaction"]

    semantic_response = client.get("/api/v2/agent/semantic-tool-schema")
    assert semantic_response.status_code == 200
    semantic = semantic_response.json()

    assert semantic["name"] == canonical["name"]
    assert semantic["description"] == canonical["description"]
    assert semantic["input_schema"] == canonical["input_schema"]


def test_unknown_tool_name_is_explicit():
    with pytest.raises(KeyError, match="unknown tool definition"):
        get_default_tool_registry().require("not_a_real_tool")
