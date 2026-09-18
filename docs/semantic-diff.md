# Semantic Engineering Diff

> Charter mapping: Priority 0 / T0.4 Semantic Diff  
> Status: first production-oriented contract

## 1. Purpose

A raw transaction is useful to machines but is a poor engineering review object.

The Semantic Diff layer converts document-level before/after changes into a stable, engineering-oriented description such as:

~~~text
+ valve XV-101
+ pipeline L-1002
~ pipeline L-1002: nominal_diameter DN80 -> DN100
~ valve XV-101: layout changed
- safety valve PSV-101
~~~

The goal is to make future Approval, Audit, revision review and Agent repair workflows reason about engineering entities rather than raw JSON patches.

## 2. Contract

The canonical response schema is `SemanticDiffReport`:

- schema: `pid-agent.semantic-diff`;
- version;
- document id;
- base revision;
- result revision;
- total change count;
- engineering / critical / draft-edit counts;
- truncation flag;
- typed change list.

Each `SemanticChange` includes:

- engineering entity kind;
- stable entity id;
- human-readable display name;
- add/delete/update action;
- semantic change types;
- changed fields;
- field-level before/after deltas;
- risk hint;
- human-readable summary;
- symbol key when applicable.

## 3. Entity classification

The first implementation recognizes:

- equipment;
- valve;
- instrument;
- pipeline;
- junction;
- annotation;
- generic graphic;
- layer;
- system;
- generic symbol.

Symbol classification uses both the stable symbol key and the Symbol Registry category.

This is deliberately deterministic. The LLM does not decide entity type for a diff that can be derived from the structured document.

## 4. Change classification

Examples:

### Layout-only change

~~~text
position / geometry / route-only presentation change
-> draft_edit
-> layout_changed / rerouted
~~~

A pipeline reroute by itself is currently treated as draft-edit unless engineering properties/connectivity also change.

### Engineering property change

Examples:

- nominal diameter;
- medium/service;
- process tag;
- flow direction;
- symbol type;
- engineering properties;
- source/target connectivity.

These receive an `engineering_change` risk hint.

### Critical hint

The first implementation marks known safety-relief valve deletion/update as `critical_change`.

This is intentionally narrow.

**risk_hint is not an engineering safety verdict.** It is a deterministic review hint. Future project Rule Engine / safety rules remain authoritative and may escalate or refine risk.

## 5. Preview before write

REST:

~~~text
POST /api/v2/documents/{document_id}/transactions/semantic-diff
~~~

Input: a normal `TransactionRequest`.

Behavior:

1. verifies expected revision;
2. clones the current structured document;
3. applies operations in memory;
4. validates the resulting document;
5. builds the Semantic Diff;
6. returns without writing.

The real document revision remains unchanged.

MCP exposes the corresponding `preview_semantic_diff` tool.

## 6. Revision persistence

Every REST/MCP transaction that records revision details now also persists its semantic diff inside history details.

REST:

~~~text
GET /api/v2/documents/{document_id}/history/{revision}/semantic-diff
~~~

For new revisions the stored diff is returned directly.

For older compatible history entries without a stored semantic diff, the server can derive a best-effort semantic diff from existing before/after change snapshots.

## 7. Relationship to history_diff.py

`history_diff.py` remains the low-level deterministic before/after snapshot mechanism.

The layering is:

~~~text
Document before / after
        |
        v
history_diff.py
  stable element/group snapshots
        |
        v
semantic_diff.py
  engineering interpretation
        |
        v
Approval / Audit / UI / Agent repair
~~~

The lower-level history format is not removed because it remains valuable for exact forensic inspection.

## 8. Truncation

Existing history snapshots intentionally cap detailed before/after records.

Semantic Diff therefore carries a `truncated` flag and preserves the full known `change_count` even when only a subset of detailed changes is available.

Future large-project audit storage may remove this limitation by storing dedicated diff rows or immutable event records.

## 9. Current guarantees

The tests verify that:

1. preview does not modify the document;
2. valves and pipelines are classified as engineering entities;
3. layout-only symbol movement is a draft edit;
4. line-size changes are engineering changes;
5. routing + engineering property changes can be represented together;
6. committed revision history stores Semantic Diff;
7. historical diff can be queried by revision;
8. known safety-relief deletion receives a critical risk hint.

## 10. Next integration

The next step is T0.5 Audit / Provenance.

The important integration target is:

~~~text
Approval Request
   |
   +--> exact intent hash
   +--> semantic diff / diff hash
   +--> validation evidence
   +--> actor
   +--> model/provider/session
   |
   v
Approved Tool Call
   |
   v
Document Revision
~~~

The user should ultimately approve an engineering change summary, not opaque JSON.
