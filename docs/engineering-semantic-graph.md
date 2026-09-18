# Engineering Semantic Graph (M2)

Charter reference: §6 (Semantic first), §8 (engineering objects, not pixels), §9
(validation before success), §16 (M2). This document describes the derived
engineering layer, its invariants, its surfaces, and — explicitly — what it is not.

## 1. Why this exists

The drawing document stores *geometry*: symbols with ports, connectors with
polylines, junctions, text. That is enough to render and to edit, but an engineer
does not review geometry. They review:

* equipment, valves, instruments and their tags;
* process pipelines, their medium and nominal diameter;
* what is connected to what, and in which direction;
* which instrument signal goes where;
* which drawing continues in which other drawing;
* which of the above is wrong.

Before M2 the repository derived equipment/lines/instruments schedules and rule
findings per drawing (`docs/engineering-reports.md`), but there was no addressable
*engineering object* with a stable identity, no project-wide aggregation, and no way
to answer "what is downstream of this pump" or "which drawings are connected".

M2 adds one derived artifact — the **engineering semantic graph** — and one
persisted, verifiably-derived cache of it: the **project engineering index**.

## 2. Layer position

```
drawing document (geometry, revision, transactions)     ← authoritative
        │  pure function, no writes
        ▼
engineering_ir.build_engineering_graph(document, symbols)   ← derived per drawing
        │  deterministic, hashed
        ▼
project_index.ProjectIndexService                            ← derived cache per project
        │
        ▼
REST · MCP · CLI · quality harness · Agent context
```

The graph never writes. `document_content_hash` hashes exactly the engineering
content the graph depends on (identity, connectivity, attributes, layer/system
membership) and deliberately ignores style, so a restyle does not invalidate the
index while any real change does.

## 3. Engineering objects

### 3.1 Identity is not the tag

Every object carries `engineering_id`, an immutable surrogate id:

```
eq_4f9c1a8b2d60   vl_9b2e77c0a1f3   inst_3c8d5e1f9a24   sg_77b1c0de9f02
ln_02f5a9c7e1b4   jn_6ad4e0b1c739   opc_conn_11a3f5c8e0d9   an_…   gr_…
```

How it was obtained is reported, never guessed (`identity_basis`):

* `declared` — the drawing element declares an authoritative stable id
  (`engineering_id` / `equipment_id` / `asset_id` / `line_id` / `signal_id` /
  `connection_id` in element properties, or the element metadata). This is the
  intended case for a real deliverable: re-importing a drawing may regenerate every
  element handle, and the object identity still holds.
* `element` — nothing was declared, so identity is derived from the drawing element
  handle. The graph says so instead of pretending the identity is stable.

The **tag is an engineering attribute**, not an identity: `tag` and `tag_key`
(`equipment:p-101`) can be edited freely and `engineering_id` does not move.
Renaming `P-101` → `P-201` therefore keeps the same object, the same downstream
trace and the same project-index row; only the attribute changes. `tag_key`
remains available so humans and existing integrations can keep addressing objects
by tag (the trace/find surfaces accept stable id, tag key, tag or element id and
report what the reference resolved to via `resolved_from`).

| kind | source | declares its identity via |
| --- | --- | --- |
| `equipment` | non-valve/non-instrument/non-OPC symbol | `engineering_id` / `equipment_id` / `asset_id` |
| `valve` | symbol whose capability/category is a valve | `engineering_id` / `valve_id` / `asset_id` |
| `instrument` | symbol in an instrument category | `engineering_id` / `instrument_id` / `asset_id` |
| `signal` | connector that carries an instrument signal (first-class object) | `signal_id` / `engineering_id` |
| `line` | aggregation of connectors sharing tag + medium class + diameter | `line_id` / `engineering_id` |
| `junction` | junction element | `engineering_id` |
| `off_page_connector` | symbol with capability `opc` | `connection_id` / `engineering_id` |
| `annotation` | text element | `engineering_id` |
| `graphic` | line/polyline/rectangle/circle | `engineering_id` |

Duplicate *declared* ids are refused: a collision is reported as
`IR_IDENTITY_COLLISION` instead of silently merging two different objects.
Duplicate **tags** are never merged either. The second and later objects get a
deterministic `#2`, `#3` suffix on `tag_key` (ordered by element id) and an
`IR_DUPLICATE_IDENTITY` error finding is raised. Two valves sharing a tag on a real
drawing is an engineering defect, not an aliasing opportunity.

### 3.2 Signals

A connector whose medium declares an instrument signal, or that runs instrument to
instrument, becomes a first-class `signal` object — not a classification attached
to a connector:

| field | meaning |
| --- | --- |
| `signal_id` | stable signal identity (same surrogate rule as every other object) |
| `connector_id` | the drawing element that carries the wiring |
| `signal_type` | `analog` \| `digital` \| `electrical` \| `pneumatic` \| `impulse` \| `unknown` |
| `source_engineering_id` / `target_engineering_id` | the two ends, as engineering objects |
| `instrument_engineering_ids` / `equipment_engineering_ids` | associated instruments and equipment |
| `medium` / `medium_class` | the declared medium that justified the classification |
| `classification` | `declared_medium` or `instrument_to_instrument` |
| `provenance` | which rule fired and on what, so a reviewer can audit the decision |

Signal wiring is kept **out of the process topology**: `edges` carry an
`edge_class` (`process` \| `signal`), connectivity components and flow traversals
are computed over process edges only, and a trace starting from a signal walks
signal edges. A signal can therefore never be mistaken for a process line, and a
process tap from an instrument onto a line is still not reclassified as a signal:
the structural rule alone is not allowed to invent signal semantics.

Signals that look like wiring but have neither a declared medium nor an instrument
endpoint are reported (`IR_SIGNAL_UNNAMED`), and a signal whose ends contain no
instrument is reported (`IR_SIGNAL_WITHOUT_INSTRUMENT`) rather than silently
accepted.

### 3.3 Pipelines and off-page connections

A pipeline (`line`) groups every connector with the same tag, medium class and
nominal diameter, and attributes the aggregated length. Untagged connectors form
single-connector pipelines keyed by element id.

An off-page connector names the drawing it continues into (`target_document_id`)
and the stable identity of the connection it carries (`off_page_connection_id`, a
`opc_conn_…` id used consistently on both ends). The project index resolves the
reciprocal end into one `OffPageConnection` whose `connection_id` is **symmetric and
tag-free** — derived from the two stable object ids — so editing a tag or a line
number on either side does not create a new connection identity. The declared
service/tag is a cross-check and tie-breaker only (`tag_agrees`, `matched_by`), and
the convention fallback (same tag, opposite direction) is labelled as such
(`reciprocal_declaration+service`) instead of being passed off as a declaration.
Two connectors declaring the same connection identity are reported
(`IR_OPC_CONNECTION_ID_DUPLICATE`); an unrestricted off-page connector with no
target is reported (`IR_OPC_TARGET_MISSING`), never guessed.

## 4. Topology, finding, tracing

* `edges` — one per connector, with `edge_class`, `pipeline_object_id` /
  `signal_engineering_id`, source/target object ids, ports, medium class and whether
  flow direction is declared.
* `connectivity_components` — union-find over process objects and process edges only;
  signals, annotations and graphics are excluded, so components answer "what is
  physically connected".
* `findings` — `IR_IDENTITY_COLLISION`, `IR_DUPLICATE_IDENTITY`,
  `IR_SYMBOL_DEFINITION_MISSING`, `IR_ENDPOINT_ELEMENT_MISSING`,
  `IR_OPC_TARGET_MISSING`, `IR_OPC_CONNECTION_ID_DUPLICATE`, `IR_ORPHAN_LINE`,
  `IR_ISOLATED_OBJECT`, `IR_SIGNAL_UNNAMED`, `IR_SIGNAL_WITHOUT_INSTRUMENT`.
* `trace_engineering_object(graph, ref, direction)` — BFS honouring declared flow
  direction; undeclared connections are traversable both ways. `ref` is an
  engineering id, a tag key, a tag or an element id, so a reviewer can start from
  whatever they have on screen; the result reports `origin_engineering_id` and
  `resolved_from` so the reference is never silently reinterpreted. Steps are
  engineering objects; the pipelines traversed are reported separately in
  `traversed_pipeline_ids` because a pipeline is an aggregate, not a single node on
  a path. Starting a trace *from* a pipeline enumerates what it connects, with the
  declared source reported upstream and the declared target downstream.

Findings here are graph-integrity findings. Drafting-quality and schedule-rule
findings stay in `engineering_reports.py`; the two layers are complementary and
must not be silently merged.

## 5. The project index

`project_index` (SQLite schema v6) stores, per document: revision, content hash,
graph hash, builder version, headline counts (including `signal_count`), the
serialized graph, build time and who built it.

Freshness is explicit and two-tiered:

| tier | how | claim |
| --- | --- | --- |
| cheap (`list_entries`, `GET /project/engineering-graph`) | compare stored revision with live revision | `fresh` — a necessary condition, not a proof |
| verified (`get_entry`, `graph`, `POST /project/index/rebuild`) | recompute the content hash and compare | `verified_fresh` — provably current |

Staleness reasons are named (`revision_changed`, `content_hash_changed`,
`builder_version_changed`, `document_deleted`), the project graph raises
`IR_INDEX_STALE` / `IR_INDEX_ORPHAN`, and `graph()` repairs a stale row before
returning it. A builder-version change invalidates every row rather than mixing
graphs derived by different logic — which is why the M2 identity rework (stable
surrogate ids instead of tag-derived ids, first-class signals) bumped
`IR_BUILDER_VERSION` to 2: every row written by the old builder is reported stale
and rebuilt instead of being silently mixed with new-identity graphs. Rows written
by an older builder are skipped by `find_objects` rather than guessed at.

Cross-document links resolve off-page connectors that name a target document, and
the connection identity they are keyed by is derived from the two stable object
ends (`opc_conn_…`), so it survives tag/line-number renames. The declared service
tag is a cross-check (`tag_agrees`): a resolved link whose tags disagree is
reported (`IR_CROSS_DOC_TAG_UNMATCHED`) rather than accepted silently, and an
unmatched or missing target is reported (`IR_CROSS_DOC_TAG_UNMATCHED` /
`IR_CROSS_DOC_TARGET_MISSING`). Nothing is inferred from proximity or drawing
order.

`find_objects(ref)` answers "which drawing holds `eq_…` / `equipment:p-101` /
`P-101`" from the index, without the caller knowing the drawing — the addressable
payoff of stable identity.

The index is a cache, therefore it is **not** part of the audit chain: it is a pure
function of the document, and auditing a recomputable value would dilute the
evidence chain. The explicit rebuild command *is* audited
(`engineering.index.rebuilt`), because "who recomputed the project index and when"
is a real operational fact.

## 6. Surfaces

| surface | binding | notes |
| --- | --- | --- |
| REST | `GET /api/v2/documents/{id}/engineering-graph` | derived live; read-only; `schema=pid-agent.engineering-graph`, `version=2` |
| REST | `GET /api/v2/documents/{id}/engineering-graph/trace?ref=&direction=` | read-only; `ref` = engineering id, tag key, tag or element id |
| REST | `GET /api/v2/project/engineering-objects?ref=&limit=` | locate an object across indexed drawings |
| REST | `GET /api/v2/project/engineering-graph` | cheap freshness |
| REST | `GET /api/v2/documents/{id}/project-index` | verified freshness |
| REST | `POST /api/v2/project/index/rebuild?force=` | audited; cannot change a drawing |
| MCP | `get_engineering_graph`, `trace_engineering_object`, `find_engineering_object`, `get_project_engineering_graph`, `rebuild_project_index` | same services |
| CLI | `pid-agent engineering-graph <document_id> [--summary]` | exit 2 when the graph has error findings |
| CLI | `pid-agent engineering-find <ref> [--limit]` | locate an object by id / tag key / tag |
| CLI | `pid-agent project-index rebuild|list|project` | exit 2 when anything stays stale |
| Web UI | right-hand tab 「工程图谱」 (`EngineeringGraphPanel`) | read-only viewer: counts, findings, objects, trace, project index + rebuild button |
| Quality harness | `engineering_graph_contract` | offline, model-free golden invariants |
| Playwright | `frontend/e2e/engineering-graph.spec.ts` | panel + API acceptance |

Every engineering-write surface is unchanged: the graph reads documents through the
existing service/store and adds no write path. `surface_contract.py` declares the
one new mutating route, and `tests/test_surface_contract.py` checks the declared
contract against the live OpenAPI schema and MCP tool list. Note what that does and
does not claim: it establishes a machine-checked completeness contract over the
REST/MCP surfaces that are actually exposed — it is a strong regression guard, not
a proof that no Python code can ever reach `Store` directly.

## 7. Invariants (enforced by tests)

1. `engineering_id` values are unique inside a document.
2. Identity is deterministic: the same document always produces the same graph hash.
3. **Renaming a tag does not change any `engineering_id`** — nor any downstream
   object, edge, signal, off-page connection id, trace or project-index row. Tag
   edits change attributes only.
4. **A declared id wins over the element handle**: re-importing a drawing with fresh
   element ids but the same declared ids reproduces the same identities, and a
   declared-id collision is reported (`IR_IDENTITY_COLLISION`), never merged.
5. Duplicate tags are disambiguated on `tag_key` and reported; they are never merged.
6. Signals are objects with their own identity, and signal edges never appear in
   process topology or in `connectivity_components`.
7. An off-page connection's `connection_id` is symmetric and derived from the two
   stable object ends only; editing either side's tag or service does not change it.
8. Every topology edge endpoint is either a known object or an explicit `unbound:*`
   placeholder; every `pipeline_object_id` is a known object.
9. Connectivity components partition the process objects exactly once.
10. Tracing is deterministic, flow-aware, and reports the pipelines it traversed; a
    reference is resolved (id / tag key / tag / element) and `resolved_from` says which.
11. `content_hash` ignores style and changes with geometry, tags and attributes.
12. The index detects its own staleness (including a builder-version bump) and
    repairs it; rebuilding never modifies a document, its revision, or the audit chain.

## 8. Known limits (honest boundaries)

* A drawing that declares no stable ids still has only element-scoped identity:
  regenerating its element handles changes the object ids. `identity_basis` says so
  per object, and the honest fix is for the deliverable to carry declared ids.
* Signals are objects, but their *meaning* is classified from medium and endpoints
  only. Loop numbers, ISA tag structure, cause-and-effect matrices and
  SIS/interlock logic are **not** modelled yet, and a signal is not yet validated
  against an instrument index.
* Line identity uses declared line id when present, otherwise the aggregated tag.
  Two physically separate services sharing a tag would be aggregated; the rule-check
  layer (`LINE_TAG_MISSING`, `TAG_DUPLICATE`) is the current guard, and a future rule
  engine should own stricter line-break/continuation semantics.
* Cross-document resolution needs a declared `target_document_id` (or an explicit
  connection identity). Off-page connectors without one are reported, never guessed,
  and the same-tag convention fallback is labelled `reciprocal_declaration+service`
  so a convention match is never mistaken for a declaration.
* The index is per SQLite database; multi-user concurrent rebuilds serialize on the
  store lock rather than coordinating a distributed cache.
* The web UI consumes the graph read-only (viewer + trace + project index). The
  Agent context still uses `build_agent_harness_context`, not this graph, so the two
  views of topology are not yet unified — that unification is follow-up work, not
  part of M2.
* Signals carry identity and structure, not yet *validity*: nothing checks a signal
  against an instrument index, a loop sheet or a cause-and-effect matrix. That
  belongs to the validation layer (Charter Priority 2), together with the rule
  unification of `diagram_quality.py` / `engineering_reports.py` / `engineering_ir.py`.
* The trace UI has no graph visualisation yet: it lists object steps and traversed
  pipelines as text. A topology/ladder view is future work.
* Nothing in this layer replaces engineering review. Findings are deterministic
  review inputs; approval, issuance and IFC/AFC state remain human decisions.
