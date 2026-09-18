"""Engineering semantic graph surfaces (M2).

Charter reference: §6 (semantic first), §8 (engineering objects, not pixels), §16.

Every route here answers an *engineering* question about derived semantics:

* what engineering objects exist in a drawing, and are they valid?
* what is upstream/downstream of this object?
* what does the whole project look like, and how do drawings connect?

Read routes are strictly read-only: they derive the graph in memory (a few
milliseconds for a 450-element drawing) and never persist anything, so a GET can
never change stored state. The only mutating route is the explicit index rebuild,
which writes the derived cache and records an audit fact.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response

from .engineering_ir import (
    EngineeringGraph,
    TraceResult,
    build_engineering_graph,
    trace_engineering_object,
)
from .project_index import (
    ProjectEngineeringGraph,
    ProjectIndexEntry,
    ProjectIndexService,
    RebuildReport,
    rebuild_with_evidence,
)
from .service import DocumentNotFoundError, DocumentService


def create_engineering_router(
    service: DocumentService,
    project_index: ProjectIndexService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["P&ID-Agent engineering graph"])

    def _document(document_id: str):
        try:
            return service.get_document(document_id)
        except DocumentNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail=f"document not found: {document_id}"
            ) from exc

    def _graph(document_id: str) -> EngineeringGraph:
        return build_engineering_graph(_document(document_id), service.symbols)

    @router.get("/documents/{document_id}/engineering-graph", response_model=EngineeringGraph)
    def engineering_graph(document_id: str) -> Response:
        document = _document(document_id)
        graph = build_engineering_graph(document, service.symbols)
        payload = graph.model_dump_json(by_alias=True)
        return Response(
            payload,
            media_type="application/json",
            headers={
                "X-PID-Agent-Graph-Revision": str(graph.revision),
                "X-PID-Agent-Graph-Objects": str(graph.counts.objects),
                "X-PID-Agent-Graph-Errors": str(graph.counts.errors),
                "X-PID-Agent-Graph-Warnings": str(graph.counts.warnings),
                "X-PID-Agent-Graph-Hash": graph.content_hash,
            },
        )

    @router.get(
        "/documents/{document_id}/engineering-graph/trace",
        response_model=TraceResult,
    )
    def engineering_trace(
        document_id: str,
        object_id: str,
        direction: Literal["upstream", "downstream", "both"] = "both",
        max_depth: Annotated[int, Query(ge=1, le=256)] = 64,
    ) -> TraceResult:
        graph = _graph(document_id)
        try:
            return trace_engineering_object(
                graph, object_id, direction=direction, max_depth=max_depth
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/project/engineering-graph", response_model=ProjectEngineeringGraph)
    def project_engineering_graph() -> ProjectEngineeringGraph:
        """Project-wide graph from the derived index (cheap freshness).

        Rows are reported as ``fresh`` when the stored revision still matches the live
        document. Run ``POST /api/v2/project/index/rebuild`` (or request the document
        graph) for the hash-verified variant.
        """

        return project_index.project_graph()

    @router.get("/documents/{document_id}/project-index", response_model=ProjectIndexEntry)
    def document_index_entry(document_id: str) -> ProjectIndexEntry:
        _document(document_id)
        entry = project_index.get_entry(document_id)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"document has no index entry yet: {document_id}",
            )
        return entry

    @router.post("/project/index/rebuild", response_model=RebuildReport)
    def rebuild_project_index(force: bool = False) -> RebuildReport:
        """Rebuild the derived index and record who asked for it.

        This is a cache rebuild, not an engineering change: it cannot alter a drawing,
        and the audit fact records the *rebuild*, not a revision.
        """

        return rebuild_with_evidence(
            project_index,
            service.audit,
            force=force,
            actor="web-user",
            surface="rest",
        )

    return router


__all__ = ["create_engineering_router"]
