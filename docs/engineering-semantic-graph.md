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

| kind | source | identity basis |
| --- | --- | --- |
| `equipment` | non-valve/non-instrument/non-OPC symbol | tag |
| `valve` | symbol whose capability/category is a valve | tag |
| `instrument` | symbol in an instrument category | tag |
| `line` | aggregation of connectors sharing tag + medium class + diameter | tag |
| `junction` | junction element | label, else element id |
| `off_page_connector` | symbol with capability `opc` | tag |
| `annotation` | text element | element id |
| `graphic` | line/polyline/rectangle/circle | element id |

Each object carries `identity_scope`:

* `tag` — identity derived from the engineering tag (`valve:hv-101`). This is the
  useful case: re-importing a drawing can regenerate every element id, and the
  object identity still holds.
* `element` — no tag exists, so identity degrades to the element id. The graph says
  so instead of pretending the identity is stable.

Duplicate tags inside one document are **never merged**. The second and later
objects get a deterministic `#2`, `#3` suffix (ordered by element id) and an
`IR_DUPLICATE_IDENTITY` error finding is raised. Two valves sharing a tag on a real
drawing is an engineering defect, not an aliasing opportunity.

A pipeline (`line`) groups every connector with the same tag, medium class and
nominal diameter, and attributes the aggregated length. Untagged connectors form
single-connector pipelines keyed by element id.

Signals are classified conservatively:

* `declared_medium` — the connector medium explicitly names a signal/electric/
  pneumatic/impulse medium;
* `instrument_to_instrument` — both endpoints are instrument objects.

A process tap from an instrument onto a line is **not** reclassified as a signal:
the structural rule alone is not allowed to invent signal semantics.

## 4. Topology, finding, tracing

* `edges` — one per connector, with `pipeline_object_id`, source/target object ids,
  ports, medium class and whether flow direction is declared.
* `connectivity_components` — union-find over process objects; annotations and
  graphics are excluded, so components answer "what is physically connected".
* `findings` — `IR_DUPLICATE_IDENTITY`, `IR_SYMBOL_DEFINITION_MISSING`,
  `IR_ENDPOINT_ELEMENT_MISSING`, `IR_OPC_TARGET_MISSING`, `IR_ORPHAN_LINE`,
  `IR_ISOLATED_OBJECT`, `IR_SIGNAL_WITHOUT_INSTRUMENT`.
* `trace_engineering_object(graph, object_id, direction)` — BFS honouring declared
  flow direction; undeclared connections are traversable both ways. Steps are
  engineering objects; the pipelines traversed are reported separately in
  `traversed_pipeline_ids` because a pipeline is an aggregate, not a single node on
  a path. Starting a trace *from* a pipeline enumerates what it connects, with the
  declared source reported upstream and the declared target downstream.

Findings here are graph-integrity findings. Drafting-quality and schedule-rule
findings stay in `engineering_reports.py`; the two layers are complementary and
must not be silently merged.

## 5. The project index

`project_index` (SQLite schema v5) stores, per document: revision, content hash,
graph hash, builder version, headline counts, the serialized graph, build time and
who built it.

Freshness is explicit and two-tiered:

| tier | how | claim |
| --- | --- | --- |
| cheap (`list_entries`, `GET /project/engineering-graph`) | compare stored revision with live revision | `fresh` — a necessary condition, not a proof |
| verified (`get_entry`, `graph`, `POST /project/index/rebuild`) | recompute the content hash and compare | `verified_fresh` — provably current |

Staleness reasons are named (`revision_changed`, `content_hash_changed`,
`builder_version_changed`, `document_deleted`), the project graph raises
`IR_INDEX_STALE` / `IR_INDEX_ORPHAN`, and `graph()` repairs a stale row before
returning it. A builder-version change invalidates every row rather than mixing
graphs derived by different logic.

Cross-document links resolve off-page connectors that name a target document: a
link is `resolved` when the target document exists *and* contains an OPC object with
the same tag in the opposite direction. Otherwise `IR_CROSS_DOC_TARGET_MISSING` or
`IR_CROSS_DOC_TAG_UNMATCHED` is reported. Nothing is inferred from proximity or
drawing order.

The index is a cache, therefore it is **not** part of the audit chain: it is a pure
function of the document, and auditing a recomputable value would dilute the
evidence chain. The explicit rebuild command *is* audited
(`engineering.index.rebuilt`), because "who recomputed the project index and when"
is a real operational fact.

## 6. Surfaces

| surface | binding | notes |
| --- | --- | --- |
| REST | `GET /api/v2/documents/{id}/engineering-graph` | derived live; read-only |
| REST | `GET /api/v2/documents/{id}/engineering-graph/trace?object_id=&direction=` | read-only |
| REST | `GET /api/v2/project/engineering-graph` | cheap freshness |
| REST | `GET /api/v2/documents/{id}/project-index` | verified freshness |
| REST | `POST /api/v2/project/index/rebuild?force=` | audited; cannot change a drawing |
| MCP | `get_engineering_graph`, `trace_engineering_object`, `get_project_engineering_graph`, `rebuild_project_index` | same services |
| CLI | `pid-agent engineering-graph <document_id> [--summary]` | exit 2 when the graph has error findings |
| CLI | `pid-agent project-index rebuild|list|project` | exit 2 when anything stays stale |
| Web UI | right-hand tab 「工程图谱」 (`EngineeringGraphPanel`) | read-only viewer: counts, findings, objects, trace, project index + rebuild button |
| Quality harness | `engineering_graph_contract` | offline, model-free golden invariants |
| Playwright | `frontend/e2e/engineering-graph.spec.ts` | panel + API acceptance |

Every engineering-write surface is unchanged: the graph reads documents through the
existing service/store and adds no write path. `surface_contract.py` enumerates the
one new mutating route so `tests/test_surface_contract.py` keeps proving that no
undiscovered write path exists.

## 7. Invariants (enforced by tests)

1. Object ids are unique inside a document.
2. Identity is deterministic: the same document always produces the same graph hash.
3. Duplicate tags are disambiguated and reported; they are never merged.
4. Every topology edge endpoint is either a known object or an explicit `unbound:*`
   placeholder; every `pipeline_object_id` is a known object.
5. Connectivity components partition the process objects exactly once.
6. Tracing is deterministic, flow-aware, and reports the pipelines it traversed.
7. `content_hash` ignores style and changes with geometry, tags and attributes.
8. The index detects its own staleness and repairs it; rebuilding never modifies a
   document, its revision, or the audit chain.

## 8. Known limits (honest boundaries)

* Untagged objects only have element-scoped identity; renaming/importing them
  changes their object id. This is reported via `identity_scope`, not hidden.
* Signals are classified from medium and endpoints only. Loop numbers, ISA tag
  structure, cause-and-effect matrices and SIS/interlock logic are **not** modelled
  yet.
* Line identity uses tag + medium class + diameter. Two physically separate services
  sharing a tag would be merged; the rule-check layer (`LINE_TAG_MISSING`,
  `TAG_DUPLICATE`) is the current guard, and a future rule engine should own stricter
  line-break/continuation semantics.
* Cross-document resolution is by declared target document + tag. Off-page
  connectors without `target_document_id` are reported, never guessed.
* The index is per SQLite database; multi-user concurrent rebuilds serialize on the
  store lock rather than coordinating a distributed cache.
* The web UI consumes the graph read-only (viewer + trace + project index). The
  Agent context still uses `build_agent_harness_context`, not this graph, so the two
  views of topology are not yet unified — that unification belongs to M3/M4.
* The trace UI has no graph visualisation yet: it lists object steps and traversed
  pipelines as text. A topology/ladder view is future work.
* Nothing in this layer replaces engineering review. Findings are deterministic
  review inputs; approval, issuance and IFC/AFC state remain human decisions.
