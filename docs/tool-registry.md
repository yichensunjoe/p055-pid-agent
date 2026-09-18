# Agent Tool Registry

> Charter mapping: Priority 0 / T0.1 Tool Registry  
> Status: first implementation slice

## Purpose

P&ID-Agent previously exposed Agent capabilities in several places:

- REST routes;
- MCP decorators;
- the semantic planner tool schema;
- direct low-level TransactionRequest schemas.

Those surfaces worked, but there was no single machine-readable contract describing which capabilities are Agent tools, what they accept and return, whether they mutate engineering state, what permission they require, or which audit event should represent the call.

`backend/agentcad/tool_registry.py` is now the canonical metadata layer for implemented harness tools.

The first slice is intentionally metadata-only. It does **not** replace DocumentService, SemanticTransactionCompiler, AutoLayoutEngine or the existing atomic transaction boundary.

The execution chain remains:

~~~text
Agent / REST / MCP
        |
        v
Tool definition / schema
        |
        v
existing deterministic compiler or service
        |
        v
TransactionRequest
        |
        v
DocumentService
~~~

This follows PROJECT_CHARTER.md: add the harness above the existing transaction engine instead of performing a large rewrite.

## Tool contract

Each `ToolDefinition` contains:

- `name`: stable machine identity;
- `description`: Agent-facing purpose;
- `input_schema`: JSON Schema;
- `output_schema`: JSON Schema;
- `permission`: `allow | ask | deny`;
- `risk`: `read | draft_edit | engineering_change | critical_change | release`;
- `has_side_effect`;
- `preview_supported`;
- `idempotency`;
- `audit_event`;
- `surfaces`: Agent / REST / MCP availability metadata;
- `tags`.

A duplicate name is rejected by the registry.

## Current registered capabilities

The initial registry deliberately contains only capabilities already backed by working code:

- `get_document`;
- `get_scene_summary`;
- `get_document_history`;
- `analyze_transaction`;
- `validate_transaction`;
- `plan_pid_agent_semantic_transaction`;
- `compile_agent_transaction`;
- `apply_agent_transaction`;
- `preview_auto_layout`;
- `apply_auto_layout`;
- `undo_document`;
- `redo_document`.

Proposed Charter tools must not be added to the registry until a real deterministic execution path and tests exist.

## Permission semantics in this slice

Permissions are currently descriptive metadata. Enforcement is the next Priority 0 task.

Current policy:

- read/analysis/compile/preview tools: `allow`;
- auto-layout/undo/redo: `allow`, risk `draft_edit`, because they are revision-aware and reversible;
- `apply_agent_transaction`: `ask`, risk `engineering_change`, because the semantic transaction can alter connectivity or delete engineering objects.

Future work will move permission decisions from metadata into an execution gate.

## REST

~~~text
GET /api/v2/agent/tools
~~~

returns the full canonical catalog.

The existing endpoint:

~~~text
GET /api/v2/agent/semantic-tool-schema
~~~

now derives its compatibility payload from the same registry rather than maintaining a separate hard-coded schema description.

## MCP

The MCP server now exposes:

~~~text
get_tool_registry
~~~

which returns the same catalog used by REST.

Existing MCP execution tools remain unchanged.

## Test invariants

`backend/tests/test_tool_registry.py` protects these invariants:

1. duplicate tool names are rejected;
2. catalog names are stable and unique;
3. engineering-changing semantic apply is marked `ask`;
4. reversible auto-layout remains a `draft_edit`;
5. MCP reads the canonical catalog;
6. REST and semantic planner schemas are derived from the same definition;
7. unknown tools fail explicitly.

## Next step

The next Charter task is T0.2/T0.3 groundwork:

1. introduce an Agent Session identity;
2. add a permission decision object;
3. route mutating tool execution through one permission gate;
4. emit registry-defined audit events;
5. persist session/tool-call provenance;
6. keep DocumentService as the atomic engineering write boundary.

The registry itself should remain independent from model providers and UI state.
