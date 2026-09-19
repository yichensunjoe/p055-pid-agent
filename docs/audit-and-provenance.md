# Audit and Provenance (Priority 0 / T0.5)

Charter reference: §19 (Audit & Provenance), §21.6 (Reviewability), §7 (Permission
boundaries). This document describes what the repository can *prove* about a
change, what it deliberately cannot, and how a reviewer checks it.

## 1. Why this exists

A P&ID revision that cannot be attributed is not an engineering deliverable. Before
T0.5 the repository could answer "what changed" (raw diff + semantic diff) but not:

* who or which model made the change, through which tool, in which session;
* which exact intent was approved before the change landed;
* whether the approval and the change were the same thing;
* whether anything was altered after the fact;
* what validation authorised the write.

## 2. The four evidence objects

| Object | Storage | Purpose |
| --- | --- | --- |
| Revision history | `document_history` | Display-level timeline (source, action, label). Unchanged from before T0.5. |
| Revision diff | `document_history.details_json` | Raw diff + engineering semantic diff for that exact revision. |
| Audit record | `audit_records` | Append-only, hash-chained fact describing one event (created / updated / deleted / rejected / failed). |
| Harness evidence | `agent_sessions`, `agent_approvals`, `agent_tool_calls` | Session, exact approved intent hash, tool call outcome. |

`RevisionEvidence` (`GET /api/v2/documents/{id}/history/{revision}/evidence`)
joins all four and re-computes the stored hashes, so a reviewer does not have to
trust the stored values.

## 3. Atomicity

One revision, its diff, its audit record and the harness close-out (tool call
completed, approval consumed, session completed) commit inside **one** SQLite
transaction (`SQLiteDocumentStore.save`, `BEGIN IMMEDIATE`). Consequences:

* a crash cannot leave a revision without evidence;
* an approval cannot be consumed without the revision it approved;
* a refused call cannot vanish (the rejection is written before the raise).

Every surface uses this path: REST v2, the Agent/REST harness, MCP tools, the
legacy `/api/v1` compatibility router, the documents router (rename / folder
move) and the in-process model-acceptance harness. Adapters never write documents
through the store directly.

## 4. Attribution is server-derived

`AuditContext` is built by the adapter that owns the entry point
(`request_audit_context`), never from the request body. `TransactionRequest.source`
is a UI hint only: it cannot change `actor`, `surface` or `tool_name` in the audit
record. The legacy v1 router records `actor="legacy-v1-client"`,
`surface="rest"`, `tool_name="legacy_v1.<endpoint>"` so a v1 edit is never
mis-attributed to `internal/system`.

`provenance.actor`, `surface`, `tool_name`, `session_id`, `approval_id`,
`tool_call_id`, `provider`, `model` and the ambient `request_id` are copied into
`evidence.attribution` for a single flat read.

## 5. Hash chain

* Each record stores `prev_hash` (the previous record's `record_hash`, or
  `GENESIS_HASH`) and `record_hash = sha256(canonical(record fields) + prev_hash)`.
* `GET /api/v2/audit/verify` recomputes the whole chain and returns the first
  divergence (`broken_link`, `ordinal_gap`, `hash_mismatch`).

### Honest boundaries

1. The chain is **tamper-evident, not tamper-proof**. An operator with write
   access to the SQLite file can rewrite the whole chain. Defeating that requires
   an external anchor (signed exports, append-only storage, or a second system).
2. A truncated **tail** leaves a locally valid chain — nothing inside the file can
   prove that newer records ever existed. This is why
   `GET /api/v2/audit/export` publishes `chain_head_hash`, `chain_head_ordinal`
   and `chain_length`: comparing two packages detects tail truncation. This is
   covered by `test_chain_cannot_detect_truncated_tail_without_external_anchor`.
3. Nothing is back-filled. A database migrated from schema v3 starts an empty
   chain because the past cannot be attested retroactively.

## 6. Diff binding (approval ↔ outcome)

The Harness approves an exact `intent_hash` over session + tool + document +
canonical intent, and additionally stores the hash of the *previewed semantic
diff* (`diff_preview_hash`) when the approval is requested. The landed revision
stores its own `diff_hash`. `diff_binding` reports `match`, `mismatch` or
`not_recorded`.

This is a **comparison, not a gate**. The hard gate remains the intent hash; the
diff binding exists so a reviewer can tell whether "what I reviewed" is "what
landed". Any non-`match` value is a review finding, not a silent pass.

## 7. Privacy

Audit records store hashes, counts, identifiers and stable codes only:

* no prompts, no model context, no document bodies, no credentials;
* changed-entity id lists are bounded (200) and flagged when truncated;
* the operational diagnostics log stays redacted and separate; it is derived from
  the committed audit record (`revision_diagnostics.emit_revision_diagnostics`),
  so the diagnostics log can never claim something the evidence chain lacks.

## 8. Surfaces

| Surface | Read | Verify | Export |
| --- | --- | --- | --- |
| REST | `GET /api/v2/audit/records`, `GET /api/v2/documents/{id}/audit`, `GET /api/v2/documents/{id}/history/{revision}/evidence` | `GET /api/v2/audit/verify` | `GET /api/v2/audit/export` |
| MCP | `get_audit_trail`, `get_revision_evidence`, `get_agent_session_audit` | `verify_audit_chain` | via REST export |
| CLI | `pid-agent audit trail`, `pid-agent audit evidence` | `pid-agent audit verify` | `pid-agent audit export --output file.json` |

All audit surfaces are read-only. The chain is appended only by engineering write
paths.

## 9. Tests

## Engineering validation evidence (M4)

A validation result is bound to the exact document id/revision/content hash, the profile id/version,
the **complete** rule-bundle fingerprint (including waiver scope), the engine and validator versions,
the symbol-registry fingerprint, and the explicit evaluation time the run used. A release-readiness
result additionally binds the validation hash and the release-validator version/policy, plus its own
readiness hash.

Two rules follow from that:

* **Validation is a read.** Running it does not create a document revision or a history entry, and
  `revision.created` is never emitted for it. If a deployment records read/tool invocations, the
  event is `validation.completed` / `release.readiness.assessed` and it stores or references the
  canonical result/readiness hash. It must not be confused with `revision.created`, and it is not
  human approval evidence.
* **Approval fields do not exist here.** No validation or readiness payload contains `approved`,
  `ifc`, `afc`, `signature`, `signed` or a release state; readiness carries
  `human_approval_required: true` instead. Formal state transitions stay behind the human Approval
  Gate.

The M4-0 CAD rule still applies to the same records: the source SHA-256 and decoder evidence are
**server-derived** and live in the audit record as well as in document provenance, and caller-supplied
`extra_evidence` may only *add* evidence in its own namespace (`cad_import`) — it cannot overwrite
reserved evidence keys such as `change_count`, `validation`, `action` or `attribution`
(`RESERVED_EVIDENCE_KEYS`; a collision raises `ReservedEvidenceError` before anything is written).

`backend/tests/test_audit_provenance.py` covers the reserved-key guard alongside its existing cases.

`backend/tests/test_audit_provenance.py` covers: chained writes, same-transaction
diff+audit, failed writes leaving no revision, undo/redo, tamper detection,
deletion detection, tail-truncation limitation, evidence re-hashing, bounded
evidence content, REST/v1/Harness/documents attribution, client forgery attempts,
refused approvals and intent mismatch, diagnostics derivation, store reopen, and
schema/backup-restore behaviour.
