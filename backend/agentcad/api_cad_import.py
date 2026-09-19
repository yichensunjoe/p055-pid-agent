"""CAD (DWG/DXF) import surfaces.

Charter reference: §7 (one governed write channel), §14 (drawing intake), §19 (audit),
§21.6 (reviewability).

Three routes, deliberately separated by what they do:

* ``GET  /imports/cad/capabilities`` — what this installation can decode, and with
  which converter evidence. Read-only: it answers "can I even import my DWG here?"
  before a user uploads a 900 KB file to find out.
* ``POST /imports/cad/plan`` — decode and translate, write nothing, return the report
  (counts, layers, frame, every issue). This is the dry run, and it is a *read* route.
* ``POST /imports/cad`` — the write. The file is the request body; the import creates
  one new document through ``DocumentService`` and applies the whole geometry as a
  single governed mutation, so it lands with one audit record, one revision history
  entry and one undo step exactly like any other edit. It cannot modify, re-version or
  delete any existing document.

The body is the raw file rather than a multipart form: this project has no multipart
dependency, and a browser can send a ``Blob`` directly. ``/api/v2/imports/*`` already
carries the configured maximum import body size in the request boundary.
"""

from __future__ import annotations

from time import perf_counter

from fastapi import APIRouter, HTTPException, Query, Request

from .audit import request_audit_context
from .cad_import import CadImporter, CadImportError
from .cad_models import (
    CadCapabilities,
    CadDryRun,
    CadFillMode,
    CadImportOptions,
    CadImportResult,
)
from .diagnostics import DiagnosticLogger
from .service import DocumentService

#: A frame arrives as ``x0,y0,x1,y1`` because it is a query parameter; the geometry
#: module keeps its own tuple type so nothing downstream deals with string parsing.
FRAME_HELP = "Crop window in source coordinates as 'x0,y0,x1,y1'."


def _parse_frame(raw: str) -> tuple[float, float, float, float] | None:
    text = (raw or "").strip()
    if not text:
        return None
    parts = [part.strip() for part in text.replace(";", ",").split(",")]
    if len(parts) != 4:
        raise CadImportError(
            "invalid_frame",
            "frame must have four comma-separated numbers: x0,y0,x1,y1",
        )
    try:
        x0, y0, x1, y1 = (float(part) for part in parts)
    except ValueError as exc:
        raise CadImportError("invalid_frame", "frame values must be numbers") from exc
    if not (x1 > x0 and y1 > y0):
        raise CadImportError(
            "invalid_frame", "frame must satisfy x1 > x0 and y1 > y0"
        )
    return (x0, y0, x1, y1)


def _parse_layers(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").replace(";", ",").split(",") if part.strip()]


def create_cad_import_router(
    service: DocumentService,
    diagnostics: DiagnosticLogger | None = None,
    *,
    max_source_bytes: int = 25 * 1024 * 1024,
) -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["P&ID-Agent CAD import"])
    importer = CadImporter(service, max_source_bytes=max_source_bytes)

    def _options(
        *,
        name: str,
        frame: str,
        fills: CadFillMode,
        layers: str,
        include_text: bool,
        curve_segments: int,
        stroke_width: float,
        unit_scale: float,
        preserve_colors: bool,
        max_elements: int,
    ) -> CadImportOptions:
        try:
            return CadImportOptions(
                name=name,
                frame=_parse_frame(frame),
                fills=fills,
                layers=_parse_layers(layers),
                include_text=include_text,
                curve_segments=curve_segments,
                stroke_width=stroke_width,
                unit_scale=unit_scale,
                preserve_colors=preserve_colors,
                max_elements=max_elements,
            )
        except CadImportError:
            raise
        except ValueError as exc:  # pydantic validation of a query parameter
            raise CadImportError("invalid_options", str(exc)) from exc

    def _error(exc: CadImportError) -> HTTPException:
        if exc.code in {"source_not_found", "source_not_a_file"}:
            status = 404
        elif exc.code in {"source_too_large"}:
            status = 413
        elif exc.code in {
            "no_dwg_converter",
            "dwg_conversion_failed",
            "binary_dxf_unsupported",
            "unrecognised_format",
            "no_geometry",
            "dxf_not_recognised",
            "object_stream_missing",
            "no_model_space",
            "source_not_readable",
            "invalid_options",
        }:
            status = 422
        else:
            status = 400
        return HTTPException(
            status_code=status,
            detail={
                "error": exc.code,
                "message": exc.message,
                "retryable": False,
                **({"detail": exc.detail} if exc.detail else {}),
            },
        )

    @router.get("/imports/cad/capabilities", response_model=CadCapabilities)
    def cad_capabilities() -> CadCapabilities:
        """What this installation can decode, with converter evidence."""

        return importer.capabilities()

    @router.post("/imports/cad/plan", response_model=CadDryRun)
    async def cad_plan(
        request: Request,
        filename: str = Query(default="drawing", min_length=1, max_length=200),
        name: str = Query(default="", max_length=160),
        frame: str = Query(default="", description=FRAME_HELP),
        fills: CadFillMode = "solid",
        layers: str = Query(default=""),
        include_text: bool = True,
        curve_segments: int = Query(default=24, ge=8, le=180),
        stroke_width: float = Query(default=1.8, gt=0, le=20),
        unit_scale: float = Query(default=1.0, gt=0, le=100_000),
        preserve_colors: bool = True,
        max_elements: int = Query(default=200_000, ge=1, le=5_000_000),
    ) -> CadDryRun:
        """Decode a CAD file and report what an import would do. Nothing is written."""

        data = await request.body()
        try:
            options = _options(
                name=name,
                frame=frame,
                fills=fills,
                layers=layers,
                include_text=include_text,
                curve_segments=curve_segments,
                stroke_width=stroke_width,
                unit_scale=unit_scale,
                preserve_colors=preserve_colors,
                max_elements=max_elements,
            )
            result = importer.dry_run(data, filename=filename, options=options)
        except CadImportError as exc:
            raise _error(exc) from exc
        if diagnostics is not None:
            diagnostics.emit(
                "cad.import.planned",
                filename=filename,
                size_bytes=len(data),
                elements=result.elements,
                operations=result.operations,
                duration_ms=result.report.duration_ms,
                issue_codes=[issue.code for issue in result.report.issues],
            )
        return result

    @router.post("/imports/cad", response_model=CadImportResult, status_code=201)
    async def cad_import(
        request: Request,
        filename: str = Query(default="drawing", min_length=1, max_length=200),
        name: str = Query(default="", max_length=160),
        frame: str = Query(default="", description=FRAME_HELP),
        fills: CadFillMode = "solid",
        layers: str = Query(default=""),
        include_text: bool = True,
        curve_segments: int = Query(default=24, ge=8, le=180),
        stroke_width: float = Query(default=1.8, gt=0, le=20),
        unit_scale: float = Query(default=1.0, gt=0, le=100_000),
        preserve_colors: bool = True,
        max_elements: int = Query(default=200_000, ge=1, le=5_000_000),
    ) -> CadImportResult:
        """Import a DWG/DXF file as a new document.

        The response carries the created document id plus the import report: counts,
        layers, frame and every artifact the importer could not reproduce.
        """

        data = await request.body()
        started = perf_counter()
        try:
            options = _options(
                name=name,
                frame=frame,
                fills=fills,
                layers=layers,
                include_text=include_text,
                curve_segments=curve_segments,
                stroke_width=stroke_width,
                unit_scale=unit_scale,
                preserve_colors=preserve_colors,
                max_elements=max_elements,
            )
            result = importer.import_bytes(
                data,
                filename=filename,
                options=options,
                audit=request_audit_context(
                    "import_cad_drawing",
                    label=f"Import CAD file {filename}",
                ),
                source="web",
            )
        except CadImportError as exc:
            if diagnostics is not None:
                diagnostics.emit(
                    "cad.import.failed",
                    filename=filename,
                    size_bytes=len(data),
                    error=exc.code,
                    message=exc.message,
                    duration_ms=round((perf_counter() - started) * 1000, 2),
                )
            raise _error(exc) from exc
        if diagnostics is not None:
            diagnostics.emit(
                "cad.import.completed",
                filename=filename,
                document_id=result.document_id,
                revision=result.revision,
                source_format=result.report.source.format,
                source_format_detail=result.report.source.format_detail,
                converter=result.report.source.converter,
                size_bytes=len(data),
                sha256=result.report.source.sha256,
                elements=result.report.counts.elements,
                primitives=result.report.counts.primitives,
                skipped=result.report.counts.skipped,
                layers=len(result.report.layers),
                logical_mutations=result.report.logical_mutations,
                operations=result.report.operations,
                issue_codes=[issue.code for issue in result.report.issues],
                duration_ms=round((perf_counter() - started) * 1000, 2),
            )
        return result

    return router


__all__ = ["create_cad_import_router"]
