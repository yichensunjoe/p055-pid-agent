# Agent Harness Sessions and Permission / Approval Gate

> Charter mapping: Priority 0 / T0.2 Agent Session + T0.3 Permission / Approval  
> Status: first enforced implementation

## 1. Purpose

The Harness now has a persisted execution identity and an enforced permission boundary.

Before this change, P&ID-Agent already had deterministic transactions and semantic compilation, but an Agent-facing caller could still reach a mutating endpoint directly. The Tool Registry described risk and permission metadata, yet that metadata did not stop execution.

This layer changes that.

For Agent-originated engineering changes, the target execution chain is now:

~~~text
Plan / Replan
    |
    v
Agent Session
    |
    v
Tool Registry policy
    |
    +--> allow -> execute
    |
    +--> ask -> exact Approval required
    |
    +--> deny -> reject
    |
    v
Tool Call provenance
    |
    v
existing compiler / TransactionRequest
    |
    v
DocumentService atomic write boundary
~~~

DocumentService remains the engineering write authority. The Harness does not replace it.

## 2. Persistent data

Database schema v3 adds three tables.

### agent_sessions

One row represents one Agent work session against one document.

Important fields:

- session id;
- document id;
- actor;
- optional project id;
- provider;
- model;
- start revision;
- end revision;
- status;
- created/updated timestamps;
- metadata.

Current statuses:

- active;
- completed;
- failed;
- cancelled.

The first implementation automatically completes a successful apply session. More lifecycle hooks can be added later for explicit cancellation and planner failures.

### agent_approvals

One approval is bound to:

- one session;
- one tool;
- one document;
- one exact intent hash.

It also records:

- requested by;
- resolved by;
- reason;
- note;
- requested/resolved/consumed timestamps;
- status.

Statuses:

- pending;
- approved;
- rejected;
- consumed.

An approval is one-time-use. A successful tool call consumes it.

### agent_tool_calls

Each gated execution records:

- session;
- tool;
- document;
- registry permission;
- registry risk;
- approval id;
- exact intent hash;
- base revision;
- result revision;
- status;
- error code;
- timestamps;
- metadata.

This is the first durable provenance layer above document revision history.

## 3. Exact intent binding

Approval is deliberately stronger than “approve this tool”.

The approved object is:

~~~text
session
+ tool name
+ document id
+ exact normalized intent
~~~

A SHA-256 hash is calculated over that canonical payload.

Therefore an approval cannot legitimately be reused to:

- execute a different transaction;
- change a parameter after approval;
- target another document;
- switch to another tool;
- use another Agent session.

The approval is consumed after successful execution, which also prevents replay.

## 4. Permission semantics

Tool Registry remains the source of policy.

### allow

The Harness may execute without a separate approval.

Example:

- reversible deterministic auto-layout.

The call is still recorded.

### ask

Execution is blocked until an explicit approval exists and matches the exact intent.

Examples:

- compiled Agent engineering transaction;
- semantic engineering transaction.

### deny

The call is rejected regardless of approval.

This is intended for capabilities such as autonomous formal release.

The first test suite contains a synthetic release tool to lock this behavior.

## 5. Human editing vs Agent editing

The gate is intentionally applied to Agent tool execution, not to every human editor interaction.

A human engineer using normal editor controls may still create a normal web TransactionRequest.

Agent-originated writes must go through the Harness.

This distinction prevents the Harness from becoming an unnecessary wrapper around every UI click while still stopping an autonomous Agent from bypassing engineering governance.

## 6. Semantic Agent lifecycle

The semantic Agent flow now behaves as follows.

### Planning

`plan-v2` and `plan-v2-stream` create an Agent Session.

The returned `SemanticAgentPlanResult` contains `session_id`.

### Replanning

`replan` continues the same session when `session_id` is supplied.

### Apply

`apply-v2` requires:

- session_id;
- approval_id;
- plan metadata;
- compiled transaction.

The Harness verifies the approval against the exact compiled transaction before DocumentService is invoked.

On success:

1. DocumentService writes the atomic transaction.
2. Tool Call is marked completed.
3. Approval becomes consumed.
4. Session end revision is stored.
5. Session becomes completed.

On a service failure the Tool Call becomes failed and the transaction remains governed by the existing atomic/revision rules.

## 7. Browser behavior

The browser no longer treats a valid Agent plan as authorization to modify engineering state.

### Manual Agent mode

Clicking the explicit Apply action performs:

1. create/use Agent session;
2. create exact approval request;
3. record the user's explicit approval;
4. call approved apply;
5. update the editor only after the backend succeeds.

### Automatic Agent mode

Automatic planning and repair can continue without interruption.

Once a valid engineering change is ready, it stops at the Approval Gate.

All engineering changes now require a human confirmation before apply.

Delete/clear-style operations are still highlighted as higher risk, but the distinction no longer creates an unsafe auto-apply path for other engineering changes.

## 8. MCP behavior

MCP exposes:

- start_agent_session;
- request_agent_tool_approval;
- resolve_agent_tool_approval;
- get_agent_session_audit.

Mutating MCP tools now obey Harness policy.

### Semantic apply

`apply_agent_transaction` requires session + approval.

### Low-level transaction apply

Both:

- `apply_transaction_v2`;
- legacy `apply_transaction`

also require session + approval.

This closes a critical bypass: a model cannot avoid the semantic tool permission gate by falling back to the low-level MCP transaction API.

### Auto-layout

`apply_auto_layout` is registry policy `allow`.

It still creates or reuses a session and records a Tool Call, but does not require approval because it is classified as reversible draft editing.

## 9. Legacy REST behavior

Legacy Agent apply remains available for compatibility, but it is no longer an ungated write path.

`POST /api/v2/documents/{document_id}/agent/apply`

requires an active session and an exact approval when the Harness is enabled.

Direct non-dry-run `/agent/generate` is blocked by the Harness. Clients should:

1. plan/dry-run;
2. review;
3. request approval;
4. approve;
5. apply.

## 10. REST Harness API

### Create session

~~~text
POST /api/v2/agent/sessions
~~~

### Read session

~~~text
GET /api/v2/agent/sessions/{session_id}
~~~

### Read complete audit

~~~text
GET /api/v2/agent/sessions/{session_id}/audit
~~~

### Request approval

~~~text
POST /api/v2/agent/sessions/{session_id}/approvals
~~~

### Resolve approval

~~~text
POST /api/v2/agent/approvals/{approval_id}/resolve
~~~

## 11. Invariants

The following are now testable invariants:

1. an `ask` tool cannot execute without approval;
2. an approved intent cannot be replaced with a different intent;
3. approval cannot cross sessions, documents or tools;
4. a consumed approval cannot be reused;
5. `allow` tools can execute without approval but are still audited;
6. `deny` tools remain blocked;
7. the document revision does not change when permission is rejected;
8. database migration preserves existing project data while adding Harness tables;
9. legacy Agent/MCP write paths cannot bypass the gate;
10. DocumentService remains the atomic revision boundary.

## 12. What this layer does not yet solve

This is not the final engineering approval system.

Still future work:

- organization roles and authenticated engineer identities;
- discipline-specific approval authority;
- configurable company/project policies;
- approval expiry;
- approval delegation;
- cryptographic signatures;
- formal IFC/AFC release workflow;
- multi-reviewer approval chains;
- safety-class-specific policy;
- session cancellation/recovery UX;
- immutable external audit storage.

Those belong to later Charter milestones.

## 13. Next step

With T0.1–T0.3 in place, the next Priority 0 work is:

### T0.4 Semantic Diff

Convert current element-level history details into a stable engineering-oriented diff contract that can become the review object for approvals.

Expected direction:

~~~text
Approval
  should review
    ->
Semantic Engineering Diff
  not
    ->
raw JSON operations
~~~

Examples:

~~~text
+ valve XV-104 on L-1002
~ L-1002 route changed
~ P-101A discharge connection updated
- instrument PI-103
~~~

After that, T0.5 will deepen Audit / Provenance so planning, tool calls, validation, approvals and document revisions form one traceable engineering change chain.
