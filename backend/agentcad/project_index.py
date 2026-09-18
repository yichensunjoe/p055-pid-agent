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

from pydantic import Field

from .engineering_ir import (
    IR_BUILDER_VERSION,
    EngineeringGraph,
    EngineeringGraphCounts,
    GraphFinding,
    build_engineering_graph,
    document_content_hash,
    graph_fingerprint,
)
from .models import Document, StrictModel
from .store import SQLiteDocumentStore
from .symbols import SymbolRegistry

PROJECT_INDEX_SCHEMA = "pid-agent.project-engineering-index"
PROJECT_INDEX_VERSION = 1

Staleness = Literal["verified_fresh", "fresh", "stale", "missing_document", "builder_outdated"]


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


class CrossDocumentLink(StrictModel):
    source_document_id: str
    source_object_id: str
    source_tag: str = ""
    direction: Literal["in", "out", ""] = ""
    target_document_id: str = ""
    target_document_found: bool = False
    resolved: bool = False
    matching_object_ids: list[str] = Field(default_factory=list)


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
    version: Literal[1] = PROJECT_INDEX_VERSION
    generated_at: datetime
    freshness: Literal["cheap", "verified"] = "cheap"
    document_count: int
    indexed_document_count: int
    stale_document_ids: list[str]
    unindexed_document_ids: list[str]
    totals: EngineeringGraphCounts
    documents: list[ProjectIndexEntry]
    cross_document_links: list[CrossDocumentLink]
    findings: list[GraphFinding]


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
        entry = ProjectIndexEntry(
            document_id=document.id,
            document_name=document.name,
            revision=document.revision,
            content_hash=graph.content_hash,
            graph_hash=graph_fingerprint(graph),
            builder_version=IR_BUILDER_VERSION,
            built_at=datetime.now(UTC),
            built_by=built_by,
            counts=ProjectIndexCounts(
                objects=graph.counts.objects,
                equipment=graph.counts.equipment,
                valves=graph.counts.valves,
                instruments=graph.counts.instruments,
                lines=graph.counts.lines,
                off_page_connectors=graph.counts.off_page_connectors,
                errors=graph.counts.errors,
                warnings=graph.counts.warnings,
            ),
            staleness="verified_fresh",
        )
        self.store.upsert_project_index(
            {
                **entry.model_dump(mode="json", exclude={"counts", "staleness", "stale_reasons"}),
                "object_count": graph.counts.objects,
                "equipment_count": graph.counts.equipment,
                "valve_count": graph.counts.valves,
                "instrument_count": graph.counts.instruments,
                "line_count": graph.counts.lines,
                "off_page_count": graph.counts.off_page_connectors,
                "error_count": graph.counts.errors,
                "warning_count": graph.counts.warnings,
                "graph_json": graph.model_dump_json(by_alias=True),
            }
        )
        return entry

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
        if existing is not None and not force:
            content_hash = document_content_hash(document)
            if (
                int(existing["revision"]) == document.revision
                and str(existing["content_hash"]) == content_hash
                and int(existing["builder_version"]) == IR_BUILDER_VERSION
            ):
                return self._entry_from_row(existing, staleness="verified_fresh")
        return self.build(document, built_by=built_by)

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
            fresh = (
                existing is not None
                and not force
                and int(existing["revision"]) == document.revision
                and str(existing["content_hash"]) == document_content_hash(document)
                and int(existing["builder_version"]) == IR_BUILDER_VERSION
            )
            if fresh:
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
    def _entry_from_row(row: dict[str, Any], *, staleness: Staleness, reasons: list[str] | None = None) -> ProjectIndexEntry:
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
                lines=int(row["line_count"]),
                off_page_connectors=int(row["off_page_count"]),
                errors=int(row["error_count"]),
                warnings=int(row["warning_count"]),
            ),
            staleness=staleness,
            stale_reasons=reasons or [],
        )

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
            if int(row["builder_version"]) != IR_BUILDER_VERSION:
                reasons.append("builder_version_changed")
            # The content hash is not re-derived here: ``list_entries`` is the
            # project-wide listing and must stay cheap. ``get_entry`` verifies.
            entries.append(
                self._entry_from_row(
                    row,
                    staleness="stale" if reasons else "fresh",
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
        if int(row["builder_version"]) != IR_BUILDER_VERSION:
            reasons.append("builder_version_changed")
        return self._entry_from_row(
            row,
            staleness="stale" if reasons else "verified_fresh",
            reasons=reasons,
        )

    def graph(self, document_id: str, *, require_fresh: bool = True) -> EngineeringGraph:
        """Return the document's graph, rebuilding when the cached copy is stale."""

        row = self.store.get_project_index(document_id)
        if row is not None and not require_fresh:
            return EngineeringGraph.model_validate_json(str(row["graph_json"]))
        stored = self.store.get(document_id)
        if stored is None:
            raise KeyError(f"document not found: {document_id}")
        document = stored.document
        content_hash = document_content_hash(document)
        if (
            row is not None
            and int(row["revision"]) == document.revision
            and str(row["content_hash"]) == content_hash
            and int(row["builder_version"]) == IR_BUILDER_VERSION
        ):
            return EngineeringGraph.model_validate_json(str(row["graph_json"]))
        graph = build_engineering_graph(document, self.registry)
        self._persist(document, graph)
        return graph

    def _persist(self, document: Document, graph: EngineeringGraph) -> None:
        self.store.upsert_project_index(
            {
                "document_id": document.id,
                "document_name": document.name,
                "revision": document.revision,
                "content_hash": graph.content_hash,
                "graph_hash": graph_fingerprint(graph),
                "builder_version": IR_BUILDER_VERSION,
                "object_count": graph.counts.objects,
                "equipment_count": graph.counts.equipment,
                "valve_count": graph.counts.valves,
                "instrument_count": graph.counts.instruments,
                "line_count": graph.counts.lines,
                "off_page_count": graph.counts.off_page_connectors,
                "error_count": graph.counts.errors,
                "warning_count": graph.counts.warnings,
                "graph_json": graph.model_dump_json(by_alias=True),
                "built_at": datetime.now(UTC).isoformat(),
                "built_by": "",
            }
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
            document_id: EngineeringGraph.model_validate_json(str(row["graph_json"]))
            for document_id, row in rows.items()
            if document_id in live
        }

        totals = EngineeringGraphCounts()
        for document_id in sorted(graphs):
            counts = graphs[document_id].counts
            totals = EngineeringGraphCounts(
                objects=totals.objects + counts.objects,
                equipment=totals.equipment + counts.equipment,
                valves=totals.valves + counts.valves,
                instruments=totals.instruments + counts.instruments,
                lines=totals.lines + counts.lines,
                junctions=totals.junctions + counts.junctions,
                off_page_connectors=totals.off_page_connectors + counts.off_page_connectors,
                annotations=totals.annotations + counts.annotations,
                graphics=totals.graphics + counts.graphics,
                edges=totals.edges + counts.edges,
                signal_links=totals.signal_links + counts.signal_links,
                errors=totals.errors + counts.errors,
                warnings=totals.warnings + counts.warnings,
                info=totals.info + counts.info,
            )

        links, findings = self._cross_document_links(graphs, live)
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
            indexed_document_count=sum(1 for entry in entries if entry.staleness != "missing_document"),
            stale_document_ids=sorted(
                entry.document_id
                for entry in entries
                if entry.staleness in {"stale", "builder_outdated", "missing_document"}
            ),
            unindexed_document_ids=sorted(set(live) - set(rows)),
            totals=totals,
            documents=entries,
            cross_document_links=links,
            findings=findings,
        )

    def _cross_document_links(
        self,
        graphs: dict[str, EngineeringGraph],
        live: dict[str, int],
    ) -> tuple[list[CrossDocumentLink], list[GraphFinding]]:
        """Resolve off-page connectors across documents by target document + tag."""

        opc_by_document: dict[str, list[Any]] = {}
        for document_id, graph in graphs.items():
            opc_by_document[document_id] = [
                obj for obj in graph.objects if obj.kind == "off_page_connector"
            ]

        links: list[CrossDocumentLink] = []
        findings: list[GraphFinding] = []
        for document_id in sorted(graphs):
            for obj in sorted(opc_by_document[document_id], key=lambda item: item.object_id):
                if not obj.target_document_id:
                    continue  # bare OPC: IR_OPC_TARGET_MISSING already reports it
                target_id = obj.target_document_id
                target_found = target_id in live
                candidates = opc_by_document.get(target_id, [])
                matching = sorted(
                    candidate.object_id
                    for candidate in candidates
                    if obj.tag
                    and candidate.tag
                    and candidate.tag.casefold() == obj.tag.casefold()
                    and candidate.opc_direction != obj.opc_direction
                )
                link = CrossDocumentLink(
                    source_document_id=document_id,
                    source_object_id=obj.object_id,
                    source_tag=obj.tag,
                    direction=obj.opc_direction,
                    target_document_id=target_id,
                    target_document_found=target_found,
                    resolved=bool(matching),
                    matching_object_ids=matching,
                )
                links.append(link)
                if not target_found:
                    findings.append(
                        GraphFinding(
                            severity="warning",
                            code="IR_CROSS_DOC_TARGET_MISSING",
                            message=(
                                f"{obj.tag or obj.object_id} 声明的目标图纸 {target_id} 不存在。"
                            ),
                            object_ids=[obj.object_id],
                            details={"document_id": document_id, "target_document_id": target_id},
                        )
                    )
                elif not matching:
                    findings.append(
                        GraphFinding(
                            severity="warning",
                            code="IR_CROSS_DOC_TAG_UNMATCHED",
                            message=(
                                f"{obj.tag or obj.object_id} 在目标图纸 {target_id} 中找不到"
                                "同标识的反向跨图连接。"
                            ),
                            object_ids=[obj.object_id],
                            details={"document_id": document_id, "target_document_id": target_id},
                        )
                    )
        links.sort(key=lambda link: (link.source_document_id, link.source_object_id))
        return links, findings


__all__ = [
    "PROJECT_INDEX_SCHEMA",
    "PROJECT_INDEX_VERSION",
    "CrossDocumentLink",
    "ProjectEngineeringGraph",
    "ProjectIndexCounts",
    "ProjectIndexEntry",
    "ProjectIndexService",
    "RebuildReport",
    "rebuild_with_evidence",
]
