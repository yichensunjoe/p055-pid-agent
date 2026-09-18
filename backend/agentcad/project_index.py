"""Persistent, verifiably-derived project engineering index (M2).

Charter reference: §6, §8, §9 (validation before success), §16 (M2 semantic graph).
``engineering_ir`` derives the graph of *one* document; this module keeps that graph
for *every* document in one place so project-level questions can be answered:

    "which drawings exist, what is in them, what is stale, how do off-page
     connectors tie the drawings together, and where are the errors?"

Declared design constraints
---------------------------
* **Derived cache, never truth.** Every row stores the document revision *and* the
  content hash it was built from, plus the builder version. Nothing reads the index
  for engineering decisions without a freshness check.
* **Two freshness paths, both honest.**
  - Cheap: compare the stored revision with the live document revision. A mismatch
    proves staleness; a match proves nothing more.
  - Verified: ``require_fresh`` rebuilds the graph and compares the content hash, so
    it cannot return a graph that does not match the current document.
  ``ProjectIndexEntry.staleness`` therefore reports ``fresh`` (revision matched) or
  ``verified_fresh`` (hash matched) and never pretends to know more than it checked.
* **Cross-drawing connection identity is derived from objects, not tags.** A
  connection between two drawings is identified by the *stable engineering ids* of
  its two ends (``opc_conn_…``, symmetric in the pair), so renaming a line number
  never changes which connection it is. Reciprocal declarations
  (``target_document_id`` on both ends) are the primary matching evidence, and a
  differing service is a warning rather than a rejection. The only other accepted
  evidence is an explicit convention fallback: **same normalised service, opposite
  direction, exactly one candidate**. A lone reverse connector whose service does
  not match is *never* paired — a fabricated cross-drawing connection is worse than
  an admitted unresolved one (Charter §6: heuristics may not invent engineering
  semantics).
* **Deterministic.** Building the same document always produces the same graph hash,
  so the index can be rebuilt at any time without changing the answer.
* **Not audited.** The index changes no engineering model; auditing a recomputable
  cache would dilute the evidence chain (see ``docs/audit-and-provenance.md``).
  Explicit rebuilds are still reported in diagnostics and in the project graph.
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal

from pydantic import Field, ValidationError

from .engineering_ir import (
    IR_BUILDER_VERSION,
    EngineeringGraph,
    EngineeringGraphCounts,
    EngineeringObject,
    GraphFinding,
    build_engineering_graph,
    document_content_hash,
    graph_fingerprint,
    off_page_connection_open_id,
    off_page_connection_pair_id,
    resolve_object,
)
from .models import Document, StrictModel
from .store import SQLiteDocumentStore
from .symbols import SymbolRegistry

PROJECT_INDEX_SCHEMA = "pid-agent.project-engineering-index"
PROJECT_INDEX_VERSION = 2

Staleness = Literal["verified_fresh", "fresh", "stale", "missing_document", "builder_outdated"]
ConnectionMatch = Literal[
    "reciprocal_declaration",
    "reciprocal_declaration+service",
    "service_convention",
    "ambiguous",
    "unresolved",
]


class ProjectIndexCounts(StrictModel):
    """The subset of graph counts persisted per row.

    Deliberately narrower than ``EngineeringGraphCounts``: a stored 0 must mean
    "zero", never "not stored". Full counts are always available from the graph
    itself (``ProjectIndexService.graph``).
    """

    objects: int = 0
    equipment: int = 0
    valves: int = 0
    instruments: int = 0
    signals: int = 0
    lines: int = 0
    off_page_connectors: int = 0
    errors: int = 0
    warnings: int = 0


class ProjectIndexEntry(StrictModel):
    document_id: str
    document_name: str
    revision: int
    content_hash: str
    graph_hash: str
    builder_version: int
    built_at: datetime
    built_by: str = ""
    counts: ProjectIndexCounts
    staleness: Staleness = "fresh"
    stale_reasons: list[str] = Field(default_factory=list)


class OffPageConnection(StrictModel):
    """One cross-drawing connection, identified by its two stable object ends.

    ``connection_id`` is symmetric (source/target order does not matter) and derived
    only from engineering ids, so it stays valid when a tag, line number or service
    name is edited on either side.
    """

    connection_id: str
    source_document_id: str
    source_engineering_id: str
    source_tag: str = ""
    source_connection_endpoint_id: str = ""
    target_document_id: str = ""
    target_engineering_id: str = ""
    target_tag: str = ""
    target_connection_endpoint_id: str = ""
    direction: Literal["in", "out", ""] = ""
    #: Declared service/line identifier: a cross-check, never the identity.
    service: str = ""
    line_engineering_ids: list[str] = Field(default_factory=list)
    resolved: bool = False
    matched_by: ConnectionMatch = "unresolved"
    tag_agrees: bool = False
    #: Whether the declared target drawing exists in this project.
    target_document_found: bool = False
    #: Drawings whose connectors declared this connection (one or both ends).
    declared_by_document_ids: list[str] = Field(default_factory=list)


class EngineeringObjectMatch(StrictModel):
    """Where one engineering object reference was found across the project."""

    document_id: str
    document_name: str
    revision: int
    engineering_id: str
    kind: str
    tag: str = ""
    tag_key: str = ""
    identity_basis: str = "element"
    resolved_from: str = ""


class RebuildReport(StrictModel):
    schema_name: Literal["pid-agent.project-index-rebuild"] = Field(
        default="pid-agent.project-index-rebuild", alias="schema"
    )
    version: Literal[1] = 1
    rebuilt: list[str]
    unchanged: list[str]
    removed: list[str]
    stale_after: list[str]
    duration_ms: float
    built_by: str = ""


class ProjectEngineeringGraph(StrictModel):
    schema_name: Literal["pid-agent.project-engineering-graph"] = Field(
        default=PROJECT_INDEX_SCHEMA, alias="schema"
    )
    version: Literal[2] = PROJECT_INDEX_VERSION
    generated_at: datetime
    freshness: Literal["cheap", "verified"] = "cheap"
    document_count: int
    indexed_document_count: int
    stale_document_ids: list[str]
    unindexed_document_ids: list[str]
    totals: EngineeringGraphCounts
    documents: list[ProjectIndexEntry]
    off_page_connections: list[OffPageConnection]
    findings: list[GraphFinding]


def _same_service(first: str, second: str) -> bool:
    """Whether two connectors declare the same service/line identifier.

    Conservative on purpose: only surrounding whitespace and case are normalised, so
    ``PL-1001`` and `` pl-1001 `` agree while ``PL-1001`` and ``PL1001`` stay
    different services. Two OPCs may only be paired by convention when this holds.
    """

    left = first.strip().casefold()
    right = second.strip().casefold()
    return bool(left) and left == right


def rebuild_with_evidence(
    project_index: ProjectIndexService,
    audit_service: Any,
    *,
    force: bool,
    actor: str,
    surface: str,
) -> RebuildReport:
    """Rebuild the derived index and record one audit fact about the rebuild.

    Shared by every surface (REST, MCP, CLI) so the evidence cannot drift between
    them. The record is explicitly about the *rebuild*: it carries no revision and
    cannot be mistaken for an engineering change.
    """

    from .audit import request_audit_context

    report = project_index.rebuild_all(force=force, built_by=actor)
    audit_service.record_event(
        "engineering.index.rebuilt",
        request_audit_context(
            "rebuild_project_index",
            actor=actor,
            surface=surface,
            label="Rebuild derived project engineering index",
            metadata={"force": force},
        ),
        evidence={
            "rebuilt_count": len(report.rebuilt),
            "unchanged_count": len(report.unchanged),
            "removed_count": len(report.removed),
            "stale_after": len(report.stale_after),
            "duration_ms": report.duration_ms,
            "builder_version": IR_BUILDER_VERSION,
            "rebuilt_document_ids": report.rebuilt[:200],
        },
    )
    return report


class ProjectIndexService:
    """Builds, verifies and queries the derived project engineering index."""

    def __init__(self, store: SQLiteDocumentStore, registry: SymbolRegistry):
        self.store = store
        self.registry = registry

    # ------------------------------------------------------------------ building

    def build(self, document: Document, *, built_by: str = "") -> ProjectIndexEntry:
        """Derive the graph for one document and persist the index row."""

        graph = build_engineering_graph(document, self.registry)
        entry = self._entry_for(document, graph, built_by=built_by)
        self.store.upsert_project_index(self._row_for(document, graph, built_by=built_by))
        return entry

    def _entry_for(
        self, document: Document, graph: EngineeringGraph, *, built_by: str
    ) -> ProjectIndexEntry:
        return ProjectIndexEntry(
            document_id=document.id,
            document_name=document.name,
            revision=document.revision,
            content_hash=graph.content_hash,
            graph_hash=graph_fingerprint(graph),
            builder_version=IR_BUILDER_VERSION,
            built_at=datetime.now(UTC),
            built_by=built_by,
            counts=self._counts_for(graph),
            staleness="verified_fresh",
        )

    @staticmethod
    def _counts_for(graph: EngineeringGraph) -> ProjectIndexCounts:
        return ProjectIndexCounts(
            objects=graph.counts.objects,
            equipment=graph.counts.equipment,
            valves=graph.counts.valves,
            instruments=graph.counts.instruments,
            signals=graph.counts.signals,
            lines=graph.counts.lines,
            off_page_connectors=graph.counts.off_page_connectors,
            errors=graph.counts.errors,
            warnings=graph.counts.warnings,
        )

    @staticmethod
    def _row_for(document: Document, graph: EngineeringGraph, *, built_by: str) -> dict[str, Any]:
        counts = graph.counts
        return {
            "document_id": document.id,
            "document_name": document.name,
            "revision": document.revision,
            "content_hash": graph.content_hash,
            "graph_hash": graph_fingerprint(graph),
            "builder_version": IR_BUILDER_VERSION,
            "object_count": counts.objects,
            "equipment_count": counts.equipment,
            "valve_count": counts.valves,
            "instrument_count": counts.instruments,
            "signal_count": counts.signals,
            "line_count": counts.lines,
            "off_page_count": counts.off_page_connectors,
            "error_count": counts.errors,
            "warning_count": counts.warnings,
            "graph_json": graph.model_dump_json(by_alias=True),
            "built_at": datetime.now(UTC).isoformat(),
            "built_by": built_by,
        }

    def rebuild_document(
        self,
        document_id: str,
        *,
        force: bool = False,
        built_by: str = "",
    ) -> ProjectIndexEntry:
        """Rebuild one document's index row; skip the write when nothing changed."""

        stored = self.store.get(document_id)
        if stored is None:
            raise KeyError(f"document not found: {document_id}")
        document = stored.document
        existing = self.store.get_project_index(document_id)
        if existing is not None and not force and self._row_is_current(existing, document):
            return self._entry_from_row(existing, staleness="verified_fresh")
        return self.build(document, built_by=built_by)

    def _row_is_current(self, row: dict[str, Any], document: Document) -> bool:
        """True when the stored row was built from this exact engineering content."""

        return (
            int(row["revision"]) == document.revision
            and str(row["content_hash"]) == document_content_hash(document)
            and int(row["builder_version"]) == IR_BUILDER_VERSION
        )

    def rebuild_all(self, *, force: bool = False, built_by: str = "") -> RebuildReport:
        """Rebuild every document deterministically (document id order)."""

        started = perf_counter()
        rebuilt: list[str] = []
        unchanged: list[str] = []
        for summary in sorted(self.store.list(), key=lambda item: item.id):
            stored = self.store.get(summary.id)
            if stored is None:  # pragma: no cover - defensive
                continue
            document = stored.document
            existing = self.store.get_project_index(document.id)
            if existing is not None and not force and self._row_is_current(existing, document):
                unchanged.append(document.id)
                continue
            self.build(document, built_by=built_by)
            rebuilt.append(document.id)
        removed = self.store.prune_project_index()
        stale_after = [
            entry.document_id
            for entry in self.list_entries()
            if entry.staleness in {"stale", "missing_document", "builder_outdated"}
        ]
        return RebuildReport(
            rebuilt=sorted(rebuilt),
            unchanged=sorted(unchanged),
            removed=removed,
            stale_after=stale_after,
            duration_ms=round((perf_counter() - started) * 1000, 3),
            built_by=built_by,
        )

    # ------------------------------------------------------------------- reading

    @staticmethod
    def _entry_from_row(
        row: dict[str, Any], *, staleness: Staleness, reasons: list[str] | None = None
    ) -> ProjectIndexEntry:
        return ProjectIndexEntry(
            document_id=str(row["document_id"]),
            document_name=str(row["document_name"]),
            revision=int(row["revision"]),
            content_hash=str(row["content_hash"]),
            graph_hash=str(row["graph_hash"]),
            builder_version=int(row["builder_version"]),
            built_at=datetime.fromisoformat(str(row["built_at"])),
            built_by=str(row.get("built_by", "") or ""),
            counts=ProjectIndexCounts(
                objects=int(row["object_count"]),
                equipment=int(row["equipment_count"]),
                valves=int(row["valve_count"]),
                instruments=int(row["instrument_count"]),
                signals=int(row.get("signal_count", 0) or 0),
                lines=int(row["line_count"]),
                off_page_connectors=int(row["off_page_count"]),
                errors=int(row["error_count"]),
                warnings=int(row["warning_count"]),
            ),
            staleness=staleness,
            stale_reasons=reasons or [],
        )

    @staticmethod
    def _cached_graph(row: dict[str, Any]) -> EngineeringGraph | None:
        """Read a cached graph, or ``None`` when it is unreadable/outdated.

        A row written by an older builder (or an older schema) is *not* an error: it is
        simply not evidence about the current document, so callers fall back to
        rebuilding. This is what makes the identity rework backward compatible with
        drawings whose cached graph predates it.
        """

        if int(row["builder_version"]) != IR_BUILDER_VERSION:
            return None
        try:
            return EngineeringGraph.model_validate_json(str(row["graph_json"]))
        except ValidationError:  # pragma: no cover - defensive
            return None

    def _live_revisions(self) -> dict[str, int]:
        return {summary.id: summary.revision for summary in self.store.list()}

    def list_entries(self) -> list[ProjectIndexEntry]:
        """Index rows with *cheap* freshness: revision comparison only.

        A row whose stored revision matches the live document revision is reported
        ``fresh`` — that is a necessary condition, not a proof, so callers that need
        a proof must use :meth:`require_fresh`.
        """

        live = self._live_revisions()
        entries: list[ProjectIndexEntry] = []
        for row in self.store.list_project_index():
            document_id = str(row["document_id"])
            if document_id not in live:
                entries.append(
                    self._entry_from_row(
                        row, staleness="missing_document", reasons=["document_deleted"]
                    )
                )
                continue
            reasons: list[str] = []
            if int(row["revision"]) != live[document_id]:
                reasons.append("revision_changed")
            builder_changed = int(row["builder_version"]) != IR_BUILDER_VERSION
            if builder_changed:
                reasons.append("builder_version_changed")
            # The content hash is not re-derived here: ``list_entries`` is the
            # project-wide listing and must stay cheap. ``get_entry`` verifies.
            entries.append(
                self._entry_from_row(
                    row,
                    staleness=(
                        "builder_outdated"
                        if builder_changed and reasons == ["builder_version_changed"]
                        else "stale"
                        if reasons
                        else "fresh"
                    ),
                    reasons=reasons,
                )
            )
        return entries

    def get_entry(self, document_id: str) -> ProjectIndexEntry | None:
        """Return one row with *verified* freshness.

        Unlike :meth:`list_entries` (which is project-wide and therefore only compares
        revisions), a single-document query can afford to verify: the live document is
        read and its content hash compared with the stored one. ``verified_fresh``
        therefore means the row provably matches the current engineering content.
        """

        row = self.store.get_project_index(document_id)
        if row is None:
            return None
        stored = self.store.get(document_id)
        if stored is None:
            return self._entry_from_row(
                row, staleness="missing_document", reasons=["document_deleted"]
            )
        document = stored.document
        reasons: list[str] = []
        if int(row["revision"]) != document.revision:
            reasons.append("revision_changed")
        if str(row["content_hash"]) != document_content_hash(document):
            reasons.append("content_hash_changed")
        builder_changed = int(row["builder_version"]) != IR_BUILDER_VERSION
        if builder_changed:
            reasons.append("builder_version_changed")
        return self._entry_from_row(
            row,
            staleness=(
                "builder_outdated"
                if builder_changed and reasons == ["builder_version_changed"]
                else "stale"
                if reasons
                else "verified_fresh"
            ),
            reasons=reasons,
        )

    def graph(self, document_id: str, *, require_fresh: bool = True) -> EngineeringGraph:
        """Return the document's graph, rebuilding when the cached copy cannot be trusted.

        ``require_fresh=False`` still refuses to hand back a graph written by a
        different builder version: an outdated cache is not an answer, it is a stale
        copy, so it is rebuilt instead of returned.
        """

        row = self.store.get_project_index(document_id)
        if row is not None:
            cached = self._cached_graph(row)
            if cached is not None and not require_fresh:
                return cached
            stored = self.store.get(document_id)
            if stored is None:
                raise KeyError(f"document not found: {document_id}")
            if cached is not None and self._row_is_current(row, stored.document):
                return cached
        stored = self.store.get(document_id)
        if stored is None:
            raise KeyError(f"document not found: {document_id}")
        document = stored.document
        graph = build_engineering_graph(document, self.registry)
        self.store.upsert_project_index(self._row_for(document, graph, built_by=""))
        return graph

    # --------------------------------------------------- identity-level queries

    def find_objects(self, ref: str, *, limit: int = 50) -> list[EngineeringObjectMatch]:
        """Find one engineering object across the project by id, tag key, tag or element.

        This is the index-backed counterpart to the per-document graph lookup: it
        answers "which drawing holds ``P-101``" or "which drawing holds
        ``eq_9f3a2b1c4d5e``" without the caller knowing the document.
        """

        needle = ref.strip()
        if not needle:
            return []
        matches: list[EngineeringObjectMatch] = []
        for row in self.store.list_project_index():
            graph = self._cached_graph(row)
            if graph is None:
                continue
            record = resolve_object(graph, needle)
            if record is None:
                continue
            matches.append(self._match_for(row, record, needle))
            if len(matches) >= limit:
                break
        return sorted(matches, key=lambda item: (item.document_id, item.engineering_id))

    @staticmethod
    def _match_for(
        row: dict[str, Any], record: EngineeringObject, needle: str
    ) -> EngineeringObjectMatch:
        return EngineeringObjectMatch(
            document_id=str(row["document_id"]),
            document_name=str(row["document_name"]),
            revision=int(row["revision"]),
            engineering_id=record.engineering_id,
            kind=record.kind,
            tag=record.tag,
            tag_key=record.tag_key,
            identity_basis=record.identity_basis,
            resolved_from="" if needle == record.engineering_id else needle,
        )

    # -------------------------------------------------- project-wide aggregation

    def project_graph(self, *, refresh_stale: bool = False) -> ProjectEngineeringGraph:
        """Aggregate every indexed document into one project-level engineering graph.

        ``refresh_stale=False`` uses the stored rows (cheap, explicitly flagged as
        ``freshness="cheap"``). ``refresh_stale=True`` verifies each document's
        content hash and rebuilds any row that no longer matches.
        """

        if refresh_stale:
            # ``rebuild_all`` verifies each document's content hash and only rebuilds
            # the rows that no longer match, so this is a verification pass.
            self.rebuild_all()
        entries = self.list_entries()
        live = self._live_revisions()
        rows = {str(row["document_id"]): row for row in self.store.list_project_index()}
        graphs = {
            document_id: cached
            for document_id, row in rows.items()
            if document_id in live and (cached := self._cached_graph(row)) is not None
        }

        totals = EngineeringGraphCounts()
        for document_id in sorted(graphs):
            counts = graphs[document_id].counts
            totals = EngineeringGraphCounts(
                objects=totals.objects + counts.objects,
                equipment=totals.equipment + counts.equipment,
                valves=totals.valves + counts.valves,
                instruments=totals.instruments + counts.instruments,
                signals=totals.signals + counts.signals,
                lines=totals.lines + counts.lines,
                junctions=totals.junctions + counts.junctions,
                off_page_connectors=totals.off_page_connectors + counts.off_page_connectors,
                annotations=totals.annotations + counts.annotations,
                graphics=totals.graphics + counts.graphics,
                edges=totals.edges + counts.edges,
                process_edges=totals.process_edges + counts.process_edges,
                signal_edges=totals.signal_edges + counts.signal_edges,
                errors=totals.errors + counts.errors,
                warnings=totals.warnings + counts.warnings,
                info=totals.info + counts.info,
            )

        connections, findings = self._off_page_connections(graphs, live)
        for entry in entries:
            if entry.staleness in {"stale", "builder_outdated"}:
                findings.append(
                    GraphFinding(
                        severity="warning",
                        code="IR_INDEX_STALE",
                        message=(
                            f"图纸 {entry.document_name}（r{entry.revision}）的工程索引已过期："
                            f"{', '.join(entry.stale_reasons) or 'unknown'}。"
                        ),
                        details={"document_id": entry.document_id, "reasons": entry.stale_reasons},
                    )
                )
            elif entry.staleness == "missing_document":
                findings.append(
                    GraphFinding(
                        severity="warning",
                        code="IR_INDEX_ORPHAN",
                        message=f"索引中存在已不存在的图纸 {entry.document_id}。",
                        details={"document_id": entry.document_id},
                    )
                )
        findings.sort(key=lambda item: (item.severity, item.code, item.message))

        return ProjectEngineeringGraph(
            generated_at=datetime.now(UTC),
            freshness="verified" if refresh_stale else "cheap",
            document_count=len(live),
            indexed_document_count=sum(
                1 for entry in entries if entry.staleness != "missing_document"
            ),
            stale_document_ids=sorted(
                entry.document_id
                for entry in entries
                if entry.staleness in {"stale", "builder_outdated", "missing_document"}
            ),
            unindexed_document_ids=sorted(set(live) - set(rows)),
            totals=totals,
            documents=entries,
            off_page_connections=connections,
            findings=findings,
        )

    def _opc_objects(
        self, graphs: dict[str, EngineeringGraph]
    ) -> dict[str, list[EngineeringObject]]:
        return {
            document_id: [obj for obj in graph.objects if obj.kind == "off_page_connector"]
            for document_id, graph in graphs.items()
        }

    @staticmethod
    def _lines_for(graph: EngineeringGraph, engineering_id: str) -> list[str]:
        """Process lines carried by an off-page connector, via its process edges."""

        return sorted(
            {
                edge.pipeline_engineering_id
                for edge in graph.edges
                if edge.edge_class == "process"
                and edge.pipeline_engineering_id
                and engineering_id
                in {edge.source_engineering_id, edge.target_engineering_id}
            }
        )

    def _off_page_connections(
        self,
        graphs: dict[str, EngineeringGraph],
        live: dict[str, int],
    ) -> tuple[list[OffPageConnection], list[GraphFinding]]:
        """Resolve cross-drawing connections into stable, tag-free identities.

        Each declared end is resolved against the target drawing. A reciprocal
        declaration (the candidate names this drawing) is the strong evidence and is
        accepted even when the two services disagree (reported as
        ``IR_CROSS_DOC_TAG_MISMATCH``). Without one, the only accepted evidence is a
        same-normalised-service/opposite-direction convention match, and it is
        labelled ``service_convention``; a unique reverse connector with a different
        service stays unresolved instead of being guessed. The result is deduplicated
        by ``connection_id`` so a mutually declared connection appears exactly once
        with both ends filled in.
        """

        opc_by_document = self._opc_objects(graphs)
        findings: list[GraphFinding] = []
        declared: list[dict[str, Any]] = []
        for document_id in sorted(graphs):
            for obj in sorted(opc_by_document[document_id], key=lambda item: item.engineering_id):
                if not obj.target_document_id:
                    continue  # bare OPC: IR_OPC_TARGET_MISSING already reports it
                declared.append(
                    {
                        "document_id": document_id,
                        "object": obj,
                        "target_document_id": obj.target_document_id,
                        "target_found": obj.target_document_id in live,
                        "line_ids": self._lines_for(graphs[document_id], obj.engineering_id),
                    }
                )

        connections: dict[str, OffPageConnection] = {}
        for side in declared:
            connection = self._resolve_connection(side, opc_by_document)
            existing = connections.get(connection.connection_id)
            if existing is None:
                connections[connection.connection_id] = connection
                continue
            # The other end declared the same connection: merge, preferring the
            # "out" side as source so the record reads in process direction.
            merged = self._merge_connection(existing, connection)
            connections[connection.connection_id] = merged

        for connection in sorted(connections.values(), key=lambda item: item.connection_id):
            if not connection.resolved:
                label = connection.source_tag or connection.source_engineering_id
                details = {
                    "document_id": connection.source_document_id,
                    "target_document_id": connection.target_document_id,
                    "connection_id": connection.connection_id,
                }
                if not connection.target_document_found:
                    findings.append(
                        GraphFinding(
                            severity="warning",
                            code="IR_CROSS_DOC_TARGET_MISSING",
                            message=f"{label} 声明的目标图纸 {connection.target_document_id} 不存在。",
                            object_ids=[connection.source_engineering_id],
                            details=details,
                        )
                    )
                elif connection.matched_by == "ambiguous":
                    findings.append(
                        GraphFinding(
                            severity="warning",
                            code="IR_CROSS_DOC_AMBIGUOUS",
                            message=(
                                f"{label} 在目标图纸 {connection.target_document_id} 中有多个候选反向"
                                "连接，无法唯一确定，需要双方显式声明或补齐标识。"
                            ),
                            object_ids=[connection.source_engineering_id],
                            details=details,
                        )
                    )
                else:
                    findings.append(
                        GraphFinding(
                            severity="warning",
                            code="IR_CROSS_DOC_UNRESOLVED",
                            message=(
                                f"{label} 在目标图纸 {connection.target_document_id} 中找不到可对应的"
                                "反向连接（既无相互声明，也无同标识约定匹配）。"
                            ),
                            object_ids=[connection.source_engineering_id],
                            details=details,
                        )
                    )
            elif connection.matched_by == "service_convention":
                findings.append(
                    GraphFinding(
                        severity="info",
                        code="IR_CROSS_DOC_CONVENTION_MATCH",
                        message=(
                            f"{connection.source_tag or connection.source_engineering_id} 与"
                            f"{connection.target_document_id} 的连接是按同标识约定匹配的，"
                            "建议改为双方显式声明目标图纸。"
                        ),
                        object_ids=[
                            connection.source_engineering_id,
                            connection.target_engineering_id,
                        ],
                        details={
                            "connection_id": connection.connection_id,
                            "matched_by": connection.matched_by,
                        },
                    )
                )
            elif not connection.tag_agrees and connection.source_tag and connection.target_tag:
                findings.append(
                    GraphFinding(
                        severity="warning",
                        code="IR_CROSS_DOC_TAG_MISMATCH",
                        message=(
                            f"跨图连接 {connection.connection_id} 两端标识不一致："
                            f"{connection.source_tag} ↔ {connection.target_tag}。"
                        ),
                        object_ids=[
                            connection.source_engineering_id,
                            connection.target_engineering_id,
                        ],
                        details={"connection_id": connection.connection_id},
                    )
                )
        return sorted(connections.values(), key=lambda item: item.connection_id), findings

    def _resolve_connection(
        self,
        side: dict[str, Any],
        opc_by_document: dict[str, list[EngineeringObject]],
    ) -> OffPageConnection:
        obj: EngineeringObject = side["object"]
        target_document_id: str = side["target_document_id"]
        candidates = opc_by_document.get(target_document_id, [])
        # Direction-less ends stay eligible in either orientation, matching the way
        # the drawing itself treats an unspecified OPC direction.
        opposite = [
            candidate
            for candidate in candidates
            if candidate.opc_direction != obj.opc_direction
        ]
        # A declaration is the strong evidence and needs no tag: the candidate names
        # this drawing as where it continues.
        reciprocal = [
            candidate
            for candidate in opposite
            if candidate.target_document_id == side["document_id"]
        ]
        matched: EngineeringObject | None = None
        matched_by: ConnectionMatch = "unresolved"
        if len(reciprocal) == 1:
            matched = reciprocal[0]
            matched_by = "reciprocal_declaration"
        elif reciprocal:
            service_matches = [
                candidate for candidate in reciprocal if _same_service(obj.tag, candidate.tag)
            ]
            if len(service_matches) == 1:
                matched = service_matches[0]
                matched_by = "reciprocal_declaration+service"
            else:
                matched_by = "ambiguous"
        else:
            # Convention fallback: same service, opposite direction, exactly one
            # candidate. Anything else is reported as unresolved rather than guessed.
            service_matches = [
                candidate for candidate in opposite if _same_service(obj.tag, candidate.tag)
            ]
            if len(service_matches) == 1:
                matched = service_matches[0]
                matched_by = "service_convention"
            elif service_matches:
                matched_by = "ambiguous"

        tag_agrees = _same_service(obj.tag, matched.tag) if matched else False
        return OffPageConnection(
            connection_id=(
                off_page_connection_pair_id(obj.engineering_id, matched.engineering_id)
                if matched is not None
                else off_page_connection_open_id(obj.engineering_id, target_document_id)
            ),
            source_document_id=side["document_id"],
            source_engineering_id=obj.engineering_id,
            source_tag=obj.tag,
            source_connection_endpoint_id=obj.off_page_connection_id,
            target_document_id=target_document_id,
            target_engineering_id=matched.engineering_id if matched else "",
            target_tag=matched.tag if matched else "",
            target_connection_endpoint_id=matched.off_page_connection_id if matched else "",
            direction=obj.opc_direction,
            service=obj.tag,
            line_engineering_ids=sorted(set(side["line_ids"])),
            resolved=matched is not None,
            matched_by=matched_by,
            tag_agrees=tag_agrees,
            target_document_found=bool(side["target_found"]),
            # Only a reciprocal declaration means the other drawing declared this
            # connection too; a convention match must not credit it with a
            # declaration it never made.
            declared_by_document_ids=sorted(
                {side["document_id"], target_document_id}
                if matched_by in {"reciprocal_declaration", "reciprocal_declaration+service"}
                else {side["document_id"]}
            ),
        )

    @staticmethod
    def _merge_connection(first: OffPageConnection, second: OffPageConnection) -> OffPageConnection:
        """Merge the two ends of one mutually declared connection into one record.

        The end that reads as process *out* becomes the source; when both ends are
        direction-less the order falls back to engineering id so the result is stable
        regardless of which drawing was visited first.
        """

        source, target = first, second
        if first.direction == "out" and second.direction != "out":
            source, target = first, second
        elif second.direction == "out" and first.direction != "out":
            source, target = second, first
        elif (first.source_document_id, first.source_engineering_id) > (
            second.source_document_id,
            second.source_engineering_id,
        ):
            source, target = second, first
        resolution = first if first.resolved else second
        return OffPageConnection(
            connection_id=first.connection_id,
            source_document_id=source.source_document_id,
            source_engineering_id=source.source_engineering_id,
            source_tag=source.source_tag,
            source_connection_endpoint_id=source.source_connection_endpoint_id,
            target_document_id=target.source_document_id or target.target_document_id,
            target_engineering_id=target.source_engineering_id,
            target_tag=target.source_tag,
            target_connection_endpoint_id=target.source_connection_endpoint_id,
            direction=source.direction,
            service=source.service or target.service,
            line_engineering_ids=sorted(
                set(source.line_engineering_ids) | set(target.line_engineering_ids)
            ),
            resolved=True,
            matched_by=resolution.matched_by,
            tag_agrees=_same_service(source.source_tag, target.source_tag),
            target_document_found=True,
            declared_by_document_ids=sorted(
                set(source.declared_by_document_ids) | set(target.declared_by_document_ids)
            ),
        )


__all__ = [
    "PROJECT_INDEX_SCHEMA",
    "PROJECT_INDEX_VERSION",
    "ConnectionMatch",
    "EngineeringObjectMatch",
    "OffPageConnection",
    "ProjectEngineeringGraph",
    "ProjectIndexCounts",
    "ProjectIndexEntry",
    "ProjectIndexService",
    "RebuildReport",
    "rebuild_with_evidence",
]
