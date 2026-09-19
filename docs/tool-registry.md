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

The registry began as a metadata-only slice. T0.2/T0.3 now consume its permission/risk metadata through the persisted Agent Harness. It still does **not** replace DocumentService, SemanticTransactionCompiler, AutoLayoutEngine or the existing atomic transaction boundary.

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

The registry contains only capabilities already backed by working code. Printed from the live
catalog (`get_default_tool_registry().catalog()`, 31 tools at this commit):

| Capability | Permission | Risk |
|---|---|---|
| `get_document`, `get_scene_summary`, `get_document_history` | `allow` | `read` |
| `analyze_transaction`, `validate_transaction`, `preview_semantic_diff` | `allow` | `read` |
| `plan_pid_agent_semantic_transaction`, `compile_agent_transaction` | `allow` | `read` |
| `get_engineering_graph`, `trace_engineering_object`, `get_project_engineering_graph`, `find_engineering_object` | `allow` | `read` |
| `get_drafting_report`, `preview_deterministic_drafting` | `allow` | `read` / `draft_edit` |
| `preview_auto_layout`, `apply_auto_layout`, `apply_deterministic_drafting`, `apply_web_transaction` | `allow` | `draft_edit` |
| `create_document`, `rename_document`, `move_document_folder`, `update_project_settings`, `rebuild_project_index`, `undo_document`, `redo_document` | `allow` | `draft_edit` |
| `import_document_payload`, `import_project_payload` | `ask` | `draft_edit` |
| `import_cad_drawing` | `allow` | `draft_edit` |
| `apply_agent_transaction`, `apply_compiled_agent_transaction` | `ask` | `engineering_change` |
| `delete_document` | `ask` | `critical_change` |

Proposed Charter tools must not be added to the registry until a real deterministic execution path and tests exist.

### M3 additions

The deterministic drafting engine adds three capabilities. Note the deliberate split:

* `get_drafting_report` — `allow` / `read`. Measures a drawing (ports, crossings, junctions,
  collisions, reserved-space intrusions, lock provenance, gate). Writes nothing.
* `preview_deterministic_drafting` — `allow` / `draft_edit`, `preview_supported`. Returns a
  reproducible `TransactionRequest` and writes nothing.
* `apply_deterministic_drafting` — `allow` / `draft_edit`, audited. Runs the pipeline and applies
  the result **through the same governed transaction channel**, so it inherits the revision check,
  the audit record, undo and the Harness allow-policy like any other edit.

`surface_contract.py` registers both drafting REST routes as `read`. That is the machine-checkable
statement that drafting has no private write path: a new unregistered write route fails
`tests/test_surface_contract.py`.

### CAD (DWG/DXF) import

* `import_cad_drawing` — `allow` / `draft_edit`, audited. One tool covers REST, MCP and the CLI;
  it **creates a new document** and cannot modify, re-version or delete an existing one.

`surface_contract.py` registers `POST /api/v2/imports/cad` as an audited `project_metadata` write
with that reason written down (`DRAFT_EDIT_EXCEPTIONS`), and `POST /api/v2/imports/cad/plan` as
`read` — the dry run is machine-checkably incapable of writing. The capability probe
(`GET /api/v2/imports/cad/capabilities`) is deliberately **not** in the contract, because the
contract enumerates mutating methods and any declared read route becomes a staleness check; its
read-only nature is asserted directly by `tests/test_cad_api.py::test_capabilities_route_writes_nothing`.

### M4 engineering validation (read-only)

* `validate_document` — **`read`**, not audited as a revision. Inputs: an existing document id and
  an optional timezone-aware `as_of`. Output: the canonical `ValidationResult` (stable codes,
  `info|warning|error|blocker` severities, object/element locators, expected vs actual, rule source,
  waiver status, effective threshold, and the document/profile/rule-bundle/registry fingerprints
  plus the evaluation time). It **cannot** accept a client-supplied rule script, severity override,
  waiver or approval state, and it never writes a revision.
* `assess_release_readiness` — **`read`**. Runs canonical validation and returns
  `ReleaseReadiness(eligible|not_eligible)` with the evidence behind it. Missing required-validator
  evidence, an unwaived severity named by the release policy, or a finding whose rule is not
  registered in the catalog all **fail closed**. This tool cannot approve, sign, issue IFC/AFC or
  change release state; human approval remains a separate gate.
* `inspect_validation_profile` — **`read`**. The resolved rule bundle: profile id/version,
  fingerprint, release policy, and every effective rule with the layer (`built-in` / `standard` /
  `company` / `project` / `release-phase`) that decided it.

The same three capabilities are exposed as

~~~text
GET /api/v2/validation/profile
GET /api/v2/validation/documents/{document_id}
GET /api/v2/validation/documents/{document_id}/release-readiness
MCP  inspect_validation_profile / validate_document / assess_release_readiness
CLI  pid-agent validate <document_id> / pid-agent release-readiness <document_id>
~~~

REST, MCP, CLI and the UI are adapters over **one** engine and **one** profile resolver: the same
input must produce the same canonical payload, and no surface may implement independent validation
policy. Concretely: every non-summary REST/CLI/MCP output is serialised by one helper into the
**same canonical public JSON**, using the **same public field names** on every surface. `schema` is
a canonical field — `schema_name` is an internal name and must never appear in a machine-facing
payload. This is asserted by a full-payload equality test (`tests/test_validation_parity.py`), not by
convention. Validation/readiness are reads; if the deployment records the invocation
(`GET ...?audit=true`, `--audit`, or the MCP tool call) it is recorded as read/tool evidence
(`validation.completed` / `release.readiness.assessed`) that references the canonical result or
readiness hash — never as `revision.created` and never as an approval. See
[`m4-engineering-validation.md`](m4-engineering-validation.md).

## Permission semantics in this slice

Permissions are now enforced for Agent-originated mutating paths by `AgentHarnessService`. See [`agent-harness-sessions-permissions.md`](agent-harness-sessions-permissions.md).

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

The registry now covers T0.1–T0.5, M2, M3 and the CAD (DWG/DXF) import slice. The next Charter task is **Priority 2 — Validator
Framework** (configurable, versionable rules with stable issue codes and project standard
references; see `PROJECT_CHARTER.md` §33). Note the milestone numbering: Charter M3 is the
Deterministic Drafting Engine (shipped), M4 is the Engineering Validation System;
the Validator Framework is a **Priority 2 implementation priority**, not a milestone rename.

The registry should keep its earlier promises while it grows: emit registry-defined audit events,
persist session/tool-call provenance, and keep DocumentService as the atomic engineering write
boundary. The registry itself should remain independent from model providers and UI state.
