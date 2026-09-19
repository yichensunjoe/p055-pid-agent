"""Rebuild a DWG/DXF drawing as a native, governed engineering document.

Charter reference: §7 (one governed write channel), §14 (drawing intake), §21.6
(reviewability), P0 ("structural heuristics must not create engineering semantics").

The import is a *reproduction*, not an interpretation. It produces:

* the same geometry, at the same coordinates inside the document frame (lines,
  polylines, circles, sampled arcs/ellipses, solid fills),
* the same layer names, so an engineer recognises their own drawing,
* the same text, with its source height and alignment,
* block provenance on every element that came out of a symbol instance
  (``cad_block``/``cad_handle``), so a later, *reviewed* semantic pass can group them
  into equipment — this importer deliberately does not.

and it produces a **report** that names everything it could not reproduce.

**One import is one governed mutation.** The whole geometry lands through a single
``DocumentService`` call (``create_document_with_operations``): one revision, one history
entry, one audit record and one undo snapshot. An earlier version wrote the layers and
then the elements in batched transactions, which meant a failure part-way through left a
half-imported drawing that looked like a normal completed document, and one undo only
removed the last batch. There is **no chunked write path** today: the operation sequence is
staged in memory and the completed document is persisted once, so nothing partial is ever
persisted — nothing is persisted until the whole document is valid.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audit_models import AuditContext
from .cad_convert import (
    STAGED_SOURCE_NAME,
    CadConversionError,
    available_converters,
    convert_dwg,
    converter_report,
    public_command,
)
from .cad_dwg import decode_object_stream
from .cad_dxf import read_dxf
from .cad_geometry import (
    DEFAULT_INK,
    CadDecodeResult,
    CadGeometryError,
    CadPrimitive,
    bounds_of,
    boxes_intersect,
    dash_for_linetype,
    matrix_is_uniform,
)
from .cad_models import (
    CadCapabilities,
    CadConverterInfo,
    CadDryRun,
    CadElementCounts,
    CadImportOptions,
    CadImportReport,
    CadImportResult,
    CadIssue,
    CadSourceFormat,
    CadSourceInfo,
)
from .models import (
    AddElementOperation,
    AddLayerOperation,
    CreateDocumentRequest,
    HistorySource,
    Layer,
)
from .service import DocumentService, InvalidOperationError

DWG_SIGNATURE = b"AC10"
BINARY_DXF_MARKER = b"AutoCAD Binary DXF"
SUPPORTED_ENCODINGS = ("utf-8", "gbk", "big5", "cp1252", "latin-1")

#: ``TextElement.font_size`` is capped at 500 by the model. CAD text heights are
#: frequently enormous in block-local units, so clamping is reported, never silent.
MAX_FONT_SIZE = 500.0
MIN_FONT_SIZE = 0.5

#: Fill and stroke ink for imported geometry; the app's own drawings use this.
TEXT_STROKE_WIDTH = 1.0


class CadImportError(ValueError):
    """A CAD import that could not be completed, with a machine-readable code."""

    def __init__(self, code: str, message: str, *, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def detect_format(data: bytes) -> tuple[CadSourceFormat, str]:
    """Identify the source format from its content, not its file extension."""

    head = data[:32]
    if head.startswith(DWG_SIGNATURE):
        signature = head[:6].decode("ascii", errors="replace")
        return "dwg", signature
    if head.startswith(BINARY_DXF_MARKER):
        raise CadImportError(
            "binary_dxf_unsupported",
            "binary DXF is not supported; re-save the drawing as ASCII DXF",
        )
    window = data[:8192].decode("latin-1", errors="replace")
    if "SECTION" in window or window.lstrip().startswith("0"):
        return "dxf", ""
    raise CadImportError(
        "unrecognised_format",
        (
            "the file is neither an ASCII DXF stream nor a DWG drawing. Export it as "
            "DXF/DWG from your CAD application and try again."
        ),
    )


def read_source_bytes(path: Path) -> bytes:
    """Read a CAD file from disk, reporting failures as stable CAD error codes.

    Every entry point that takes a path (CLI, MCP, dry run or write) goes through here,
    so an unreadable file is answered with the same ``CadImportError`` code no matter
    which surface asked, instead of leaking ``FileNotFoundError`` from one of them.
    """

    path = Path(path)
    try:
        if path.is_dir():
            raise CadImportError("source_not_a_file", f"not a file: {path}")
        return path.read_bytes()
    except CadImportError:
        raise
    except FileNotFoundError as exc:
        raise CadImportError("source_not_found", f"no such file: {path}") from exc
    except OSError as exc:
        raise CadImportError(
            "source_not_readable",
            f"the file could not be read: {exc.strerror or exc}",
            detail={"path_name": path.name},
        ) from exc


def _slug(value: str, *, limit: int = 48) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", normalized).strip("_")
    return cleaned[:limit]


@dataclass
class _Plan:
    """The result of decoding: everything needed to write, or to explain why not."""

    document_name: str
    canvas_width: float
    canvas_height: float
    layer_operations: list[AddLayerOperation] = field(default_factory=list)
    element_operations: list[AddElementOperation] = field(default_factory=list)
    counts: CadElementCounts = field(default_factory=CadElementCounts)
    issues: list[CadIssue] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    frame: tuple[float, float, float, float] | None = None
    source_bounds: tuple[float, float, float, float] | None = None
    imported_bounds: tuple[float, float, float, float] | None = None
    source: CadSourceInfo = field(default_factory=CadSourceInfo)
    warnings: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    decode_ms: float = 0.0
    logical_mutations: int = 1

    @property
    def operations(self) -> int:
        return len(self.layer_operations) + len(self.element_operations)

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "cad_import": {
                "source_file": self.source.filename,
                "source_format": self.source.format,
                "format_detail": self.source.format_detail,
                "size_bytes": self.source.size_bytes,
                "sha256": self.source.sha256,
                "encoding": self.source.encoding,
                "converter": self.source.converter,
                "converter_version": self.source.converter_version,
                "converter_command": list(self.source.converter_command),
                "frame": list(self.frame) if self.frame else None,
                "canvas": {
                    "width": round(self.canvas_width, 3),
                    "height": round(self.canvas_height, 3),
                },
                "counts": self.counts.model_dump(mode="json"),
                "issues": [
                    {"code": issue.code, "count": issue.count} for issue in self.issues
                ],
                "note": (
                    "geometry-faithful reproduction: layers, geometry and text were "
                    "imported; no engineering semantics (equipment, lines, instruments) "
                    "were inferred"
                ),
            }
        }

    @property
    def audit_provenance(self) -> dict[str, Any]:
        """Server-derived CAD provenance for the audit record (Charter §19).

        The SHA-256 is hashed from the decoded bytes here, never taken from a request,
        and the whole binding (format, decoder key and version, resulting document and
        revision) travels with the same atomic write as the revision itself, so an audit
        record cannot claim a source it did not import.
        """

        return {
            "cad_import": {
                "source_sha256": self.source.sha256,
                "source_format": self.source.format,
                "source_format_detail": self.source.format_detail,
                "source_size_bytes": self.source.size_bytes,
                "source_encoding": self.source.encoding,
                "converter": self.source.converter,
                "converter_version": self.source.converter_version,
                "converter_command": list(self.source.converter_command),
                "operation_count": self.operations,
            }
        }


class CadImporter:
    """Decode CAD files and write them as governed documents."""

    def __init__(
        self,
        service: DocumentService,
        *,
        max_source_bytes: int = 25 * 1024 * 1024,
    ):
        self.service = service
        self.max_source_bytes = max_source_bytes

    # -- capability discovery ----------------------------------------------- #

    def capabilities(self) -> CadCapabilities:
        converters = [
            CadConverterInfo(
                key=item["key"],
                label=item["label"],
                executable=item["executable"],
                available=item["available"],
                produces=item["produces"],
                version=item["version"],
                evidence=item["evidence"],
                notes=item["notes"],
            )
            for item in converter_report()
        ]
        dwg_import = any(item.available for item in converters)
        notes = [
            "DXF is read by this project's own group-code reader; no external tool is "
            "needed and the reader is part of the audited source tree.",
            "DWG needs an installed decoder, invoked as a separate process. No converter "
            "is bundled or redistributed with this project.",
        ]
        if not dwg_import:
            notes.append(
                "No DWG decoder is installed: export the drawing as ASCII DXF, or "
                "install LibreDWG (dwgread/dwg2dxf) or the ODA File Converter."
            )
        return CadCapabilities(
            dxf_import=True,
            dwg_import=dwg_import,
            formats=["dxf", "dwg"] if dwg_import else ["dxf"],
            converters=converters,
            decode_encoding=list(SUPPORTED_ENCODINGS),
            max_source_bytes=self.max_source_bytes,
            notes=notes,
        )

    # -- decoding ----------------------------------------------------------- #

    def decode_bytes(
        self,
        data: bytes,
        *,
        filename: str,
        options: CadImportOptions | None = None,
    ) -> CadDecodeResult:
        """Decode a source file without writing anything."""

        selected = options or CadImportOptions()
        self._validate_source(data)
        fmt, detail = detect_format(data)
        if fmt == "dxf":
            try:
                return read_dxf(data)
            except CadGeometryError as exc:
                raise CadImportError(exc.code, exc.message) from exc
        return self._decode_dwg(data, options=selected, detail=detail)[0]

    def _validate_source(self, data: bytes) -> None:
        """Reject an empty or oversized upload before any decoding is attempted."""

        if not data:
            raise CadImportError("empty_source", "the uploaded file is empty")
        if len(data) > self.max_source_bytes:
            raise CadImportError(
                "source_too_large",
                (
                    f"the source file is {len(data)} bytes, above the configured limit "
                    f"of {self.max_source_bytes} bytes"
                ),
            )

    def _decode_dwg(
        self,
        data: bytes,
        *,
        options: CadImportOptions,
        detail: str,
    ) -> tuple[CadDecodeResult, dict[str, Any]]:
        converters = available_converters()
        if not converters:
            raise CadImportError(
                "no_dwg_converter",
                (
                    "no DWG decoder is installed. Install AutoCAD (its headless Core "
                    "Console is enough), LibreDWG (dwgread/dwg2dxf) or the ODA File "
                    "Converter, or export the drawing as ASCII DXF and import that."
                ),
            )
        with tempfile.TemporaryDirectory(prefix="pid-agent-cad-") as temporary:
            workdir = Path(temporary)
            # Fixed internal name: the operator's filename never becomes a path, and so
            # can never reach the converter's command line or its ``.scr`` script.
            source_path = workdir / STAGED_SOURCE_NAME
            source_path.write_bytes(data)
            try:
                conversion = convert_dwg(source_path, workdir=workdir, converters=converters)
            except CadConversionError as exc:
                raise CadImportError(
                    exc.code,
                    exc.message,
                    detail={
                        "attempts": [
                            {
                                "key": attempt.key,
                                "status": attempt.status,
                                "exit_code": attempt.exit_code,
                                "error": attempt.error,
                                "stderr": attempt.stderr,
                            }
                            for attempt in exc.attempts
                        ]
                    },
                ) from exc
            if conversion.kind == "object-stream":
                if conversion.payload is None:
                    raise CadImportError(
                        "dwg_conversion_failed",
                        "the converter reported success without an object dump",
                    )
                result = decode_object_stream(conversion.payload)
            else:
                if conversion.dxf_bytes is None:
                    raise CadImportError(
                        "dwg_conversion_failed",
                        "the converter reported success without a DXF payload",
                    )
                try:
                    result = read_dxf(conversion.dxf_bytes)
                except CadGeometryError as exc:
                    raise CadImportError(exc.code, exc.message) from exc
        details: dict[str, Any] = {
            "converter": conversion.converter.key,
            "converter_label": conversion.converter.label,
            "converter_version": conversion.version,
            # Reported form: executable basename plus a ``<workdir>`` token, so no local
            # path is disclosed to a client of this instance.
            "command": (
                public_command(conversion.attempts[-1].command, workdir=workdir)
                if conversion.attempts
                else []
            ),
            "attempts": [
                {"key": attempt.key, "status": attempt.status, "duration_ms": attempt.duration_ms}
                for attempt in conversion.attempts
            ],
            "evidence": conversion.converter.evidence,
            "format_detail": detail or result.format_detail,
        }
        return result, details

    # -- planning ----------------------------------------------------------- #

    def plan(
        self,
        data: bytes,
        *,
        filename: str,
        options: CadImportOptions | None = None,
    ) -> _Plan:
        """Decode and translate the file into native operations, writing nothing."""

        selected = options or CadImportOptions()
        self._validate_source(data)
        started = time.perf_counter()
        decode_started = time.perf_counter()
        conversion_details: dict[str, Any] = {}
        fmt, detail = detect_format(data)
        if fmt == "dxf":
            result = self.decode_bytes(data, filename=filename, options=selected)
        else:
            result, conversion_details = self._decode_dwg(
                data, options=selected, detail=detail
            )
        decode_ms = round((time.perf_counter() - decode_started) * 1000, 2)

        plan = self._translate(result, selected, filename=filename, data=data)
        plan.decode_ms = decode_ms
        plan.source.format = fmt
        plan.source.format_detail = str(
            conversion_details.get("format_detail") or detail or result.format_detail
        )
        plan.source.converter = str(conversion_details.get("converter") or "")
        plan.source.converter_version = str(conversion_details.get("converter_version") or "")
        plan.source.converter_command = list(conversion_details.get("command") or [])
        if conversion_details.get("evidence") == "unverified":
            plan.warnings.append(
                "This DWG was decoded with an unverified converter "
                f"({conversion_details.get('converter_label')}). Check the drawing "
                "against the original before relying on it."
            )
        for attempt in conversion_details.get("attempts") or []:
            if attempt.get("status") not in {"success", "skipped"}:
                plan.warnings.append(
                    f"Converter {attempt['key']} did not produce a result: "
                    f"{attempt.get('status')}"
                )
        plan.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        return plan

    def _translate(
        self,
        result: CadDecodeResult,
        options: CadImportOptions,
        *,
        filename: str,
        data: bytes,
    ) -> _Plan:
        primitives = result.primitives
        source_bounds = bounds_of(primitives, options.curve_segments)
        frame = self._resolve_frame(source_bounds, options)
        if frame is None:
            raise CadImportError(
                "no_geometry",
                "the file contains no drawable geometry inside the requested frame",
            )
        scale = options.unit_scale
        plan = _Plan(
            document_name=self._document_name(options, filename),
            canvas_width=(frame[2] - frame[0]) / scale,
            canvas_height=(frame[3] - frame[1]) / scale,
            frame=frame,
            source_bounds=source_bounds,
            source=CadSourceInfo(
                filename=Path(filename).name or "drawing",
                format="dxf",
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                encoding=result.encoding,
            ),
        )
        issues = Counter({code: count for code, count in result.issues.sorted_items()})
        issue_details = dict(result.issues.details)
        issue_messages = dict(result.issues.messages)

        def note(code: str, message: str, count: int = 1, detail: dict[str, int] | None = None) -> None:
            if count <= 0:
                return
            issues[code] += count
            issue_messages.setdefault(code, message)
            if detail:
                bucket = issue_details.setdefault(code, {})
                for key, value in detail.items():
                    bucket[key] = bucket.get(key, 0) + value

        wanted_layers = {(name or "").strip() for name in options.layers if name.strip()}
        kept: list[CadPrimitive] = []
        skipped = Counter()
        for primitive in primitives:
            if wanted_layers and primitive.layer not in wanted_layers:
                skipped["layer_filter"] += 1
                continue
            if primitive.kind == "text" and not options.include_text:
                skipped["text_disabled"] += 1
                continue
            if primitive.kind == "fill" and options.fills == "skip":
                skipped["fills_disabled"] += 1
                continue
            box = primitive.bounds(options.curve_segments)
            if box is None or not boxes_intersect(box, frame):
                skipped["outside_frame"] += 1
                continue
            kept.append(primitive)

        if not kept:
            raise CadImportError(
                "no_geometry",
                "no drawable geometry would be imported from this file",
            )
        if len(kept) > options.max_elements:
            raise CadImportError(
                "too_many_elements",
                (
                    f"the drawing has {len(kept)} drawable primitives, above the "
                    f"configured limit of {options.max_elements}. Import a frame "
                    "(crop) or raise the limit deliberately."
                ),
            )
        if skipped:
            note(
                "CAD_PRIMITIVES_NOT_IMPORTED",
                (
                    "primitives were left out by the import options (layer filter, "
                    "text/fill switch, or outside the requested frame)"
                ),
                sum(skipped.values()),
                detail=dict(skipped),
            )

        approximated_circles = sum(
            1
            for primitive in kept
            if primitive.kind == "circle"
            and not matrix_is_uniform(primitive.transform.matrix)
        )
        note(
            "CAD_CIRCLE_APPROXIMATED",
            (
                "circles placed by a non-uniform block reference are not circles after "
                "the transform (they are ellipses), so they were sampled as closed "
                "polylines"
            ),
            approximated_circles,
        )

        layer_ids = self._assign_layer_ids(kept)
        plan.layers = sorted(layer_ids, key=lambda name: layer_ids[name])
        plan.layer_operations = [
            AddLayerOperation(layer=Layer(id=layer_ids[name], name=name))
            for name in plan.layers
        ]

        origin_x, origin_y = frame[0], frame[3]
        counts = Counter()
        clamped_text = 0
        degenerate = 0
        for index, primitive in enumerate(kept):
            element = self._element(
                primitive,
                index=index,
                layer_id=layer_ids[primitive.layer],
                origin=(origin_x, origin_y),
                scale=scale,
                options=options,
                filename=plan.source.filename,
            )
            if element is None:
                degenerate += 1
                continue
            counts[primitive.kind] += 1
            if primitive.kind == "text" and primitive.height / scale >= MAX_FONT_SIZE:
                clamped_text += 1
            plan.element_operations.append(element)
        note(
            "CAD_TEXT_HEIGHT_CLAMPED",
            (
                f"text taller than the editor's {MAX_FONT_SIZE:g} unit limit was "
                "clamped; its position is unchanged"
            ),
            clamped_text,
        )
        note(
            "CAD_DEGENERATE_GEOMETRY",
            "shapes that would have had no extent after import were skipped",
            degenerate,
        )
        plan.counts = CadElementCounts(
            layers=len(plan.layer_operations),
            lines=counts["line"],
            polylines=counts["polyline"],
            circles=counts["circle"],
            texts=counts["text"],
            fills=counts["fill"],
            elements=len(plan.element_operations),
            primitives=len(primitives),
            skipped=len(primitives) - len(plan.element_operations),
        )
        plan.imported_bounds = bounds_of(kept, options.curve_segments)
        plan.issues = [
            CadIssue(
                code=code,
                message=issue_messages.get(code, code),
                count=count,
                detail=issue_details.get(code, {}),
            )
            for code, count in sorted(issues.items())
            if count > 0
        ]
        return plan

    def _resolve_frame(
        self,
        source_bounds: tuple[float, float, float, float] | None,
        options: CadImportOptions,
    ) -> tuple[float, float, float, float] | None:
        if options.frame is not None:
            x0, y0, x1, y1 = options.frame
            if not (x1 > x0 and y1 > y0):
                raise CadImportError(
                    "invalid_frame",
                    "the frame must be (x0, y0, x1, y1) with x1 > x0 and y1 > y0",
                )
            return (float(x0), float(y0), float(x1), float(y1))
        if source_bounds is None:
            return None
        return (float(source_bounds[0]), float(source_bounds[1]), float(source_bounds[2]), float(source_bounds[3]))

    def _document_name(self, options: CadImportOptions, filename: str) -> str:
        name = (options.name or "").strip()
        if name:
            return name[:160]
        stem = Path(filename).stem.strip() or "CAD import"
        return f"{stem} (CAD 导入)"[:160]

    def _assign_layer_ids(self, primitives: list[CadPrimitive]) -> dict[str, str]:
        """Deterministic, collision-free layer ids.

        Two CAD layers can sanitise to the same ASCII id (a real failure on the first
        version of this importer, where two Chinese layer names collapsed into one).
        """

        names = sorted({(primitive.layer or "0").strip() or "0" for primitive in primitives})
        ids: dict[str, str] = {}
        used: set[str] = {"layer_default"}
        for name in names:
            base = f"layer_cad_{_slug(name) or 'unnamed'}"
            candidate = base
            suffix = 2
            while candidate in used:
                candidate = f"{base}_{suffix}"
                suffix += 1
            used.add(candidate)
            ids[name] = candidate
        return ids

    def _element(
        self,
        primitive: CadPrimitive,
        *,
        index: int,
        layer_id: str,
        origin: tuple[float, float],
        scale: float,
        options: CadImportOptions,
        filename: str,
    ) -> AddElementOperation | None:
        """Translate one primitive into a native element operation."""

        def place(point: tuple[float, float]) -> dict[str, float]:
            return {
                "x": round((point[0] - origin[0]) / scale, 6),
                "y": round((origin[1] - point[1]) / scale, 6),
            }

        ink = primitive.color if options.preserve_colors else DEFAULT_INK
        metadata: dict[str, Any] = {"cad_layer": primitive.layer}
        if primitive.block:
            metadata["cad_block"] = primitive.block
        if primitive.handle:
            metadata["cad_handle"] = primitive.handle
        metadata["cad_source"] = filename
        style: dict[str, Any] = {
            "stroke": ink,
            "fill": "none",
            "stroke_width": options.stroke_width,
            "dash": dash_for_linetype(primitive.linetype),
        }
        element_id = f"el_cad_{index:06d}"
        common: dict[str, Any] = {
            "id": element_id,
            "layer_id": layer_id,
            "metadata": metadata,
            "style": style,
        }

        if primitive.kind == "line":
            if len(primitive.points) < 2:
                return None
            return AddElementOperation(
                element={
                    "type": "line",
                    "start": place(primitive.points[0]),
                    "end": place(primitive.points[1]),
                    **common,
                }
            )
        if primitive.kind == "polyline":
            points = [place(point) for point in primitive.points]
            if len(points) < 2:
                return None
            return AddElementOperation(
                element={
                    "type": "polyline",
                    "points": points,
                    "closed": primitive.closed,
                    **common,
                }
            )
        if primitive.kind == "fill":
            polygon = [place(point) for point in primitive.points]
            if len(polygon) < 3:
                return None
            style["fill"] = ink
            style["stroke"] = ink
            return AddElementOperation(
                element={
                    "type": "polyline",
                    "points": polygon,
                    "closed": True,
                    **common,
                }
            )
        if primitive.kind == "circle":
            if not matrix_is_uniform(primitive.transform.matrix):
                # A non-uniform INSERT turns a circle into an ellipse. A scalar radius
                # cannot express that, so the *fully transformed* curve is sampled
                # instead of keeping a native circle that would be the wrong shape.
                points = [place(point) for point in primitive.curve_points(options.curve_segments)]
                if len(points) < 3:
                    return None
                return AddElementOperation(
                    element={
                        "type": "polyline",
                        "points": points,
                        "closed": True,
                        **common,
                    }
                )
            radius = primitive.radius * primitive.transform.scale / scale
            if radius <= 0:
                return None
            center = primitive.transform.apply(primitive.center or (0.0, 0.0))
            return AddElementOperation(
                element={
                    "type": "circle",
                    "center": place(center),
                    "radius": round(radius, 6),
                    **common,
                }
            )
        if primitive.kind in {"arc", "ellipse"}:
            points = [place(point) for point in primitive.curve_points(options.curve_segments)]
            if len(points) < 2:
                return None
            closed = primitive.kind == "ellipse" and primitive.is_closed_curve()
            return AddElementOperation(
                element={
                    "type": "polyline",
                    "points": points,
                    "closed": closed,
                    **common,
                }
            )
        if primitive.kind == "text":
            if not primitive.points or primitive.height <= 0:
                return None
            height = primitive.height / scale
            font_size = min(max(height, MIN_FONT_SIZE), MAX_FONT_SIZE)
            style["stroke"] = ink
            style["fill"] = ink
            style["stroke_width"] = TEXT_STROKE_WIDTH
            return AddElementOperation(
                element={
                    "type": "text",
                    "position": place(primitive.points[0]),
                    "text": primitive.text,
                    "font_size": round(font_size, 4),
                    "anchor": primitive.anchor if primitive.anchor in {"start", "middle", "end"} else "start",
                    **common,
                }
            )
        return None

    # -- writing ------------------------------------------------------------ #

    def import_bytes(
        self,
        data: bytes,
        *,
        filename: str,
        options: CadImportOptions | None = None,
        audit: AuditContext | None = None,
        source: HistorySource | None = None,
    ) -> CadImportResult:
        """Import a source file as a new document through the governed write path."""

        plan = self.plan(data, filename=filename, options=options)
        return self._write(plan, audit=audit, source=source, options=options or CadImportOptions())

    def import_path(
        self,
        path: Path,
        *,
        options: CadImportOptions | None = None,
        audit: AuditContext | None = None,
        source: HistorySource | None = None,
    ) -> CadImportResult:
        path = Path(path)
        return self.import_bytes(
            read_source_bytes(path),
            filename=path.name,
            options=options,
            audit=audit,
            source=source,
        )

    def dry_run_path(
        self,
        path: Path,
        *,
        options: CadImportOptions | None = None,
    ) -> CadDryRun:
        """The dry run of a file on disk, through the same loader as a real import.

        Sharing ``read_source_bytes`` is the point: a dry run that reported raw
        ``FileNotFoundError`` for a path a real import would report as a stable
        ``CadImportError`` would be answering a different question than it claims to.
        """

        path = Path(path)
        return self.dry_run(read_source_bytes(path), filename=path.name, options=options)

    def dry_run(
        self,
        data: bytes,
        *,
        filename: str,
        options: CadImportOptions | None = None,
    ) -> CadDryRun:
        plan = self.plan(data, filename=filename, options=options)
        return CadDryRun(
            report=self._report(plan, logical_mutations=0, operations=0, revisions=0),
            document_name=plan.document_name,
            operations=plan.operations,
            elements=plan.counts.elements,
        )

    def _write(
        self,
        plan: _Plan,
        *,
        audit: AuditContext | None,
        source: HistorySource | None,
        options: CadImportOptions,
    ) -> CadImportResult:
        """Write the whole plan as one logical governed mutation.

        One ``DocumentService`` call, therefore one revision, one history entry, one
        audit record and one undo snapshot. The alternative (a document, then batched
        transactions) has two failure modes a reviewer would call data loss: a failed
        batch leaves a document that looks finished but is not, and one undo removes
        only the last batch of a large drawing.
        """

        operations: list[Any] = [*plan.layer_operations, *plan.element_operations]
        if not operations:
            raise CadImportError("no_geometry", "there is nothing to import")
        try:
            result = self.service.create_document_with_operations(
                CreateDocumentRequest(
                    name=plan.document_name,
                    width=max(1.0, plan.canvas_width),
                    height=max(1.0, plan.canvas_height),
                    metadata=plan.metadata,
                ),
                operations=operations,
                label=f"Import CAD file {plan.source.filename}",
                source=source or "web",
                audit=audit,
                provenance=plan.audit_provenance,
            )
        except InvalidOperationError as exc:
            # The service validates the finished document and refuses the whole write. A
            # rejected import is a caller-facing outcome with a stable code, not a 500.
            raise CadImportError("import_rejected", str(exc)) from exc
        return CadImportResult(
            document_id=result.document.id,
            document_name=result.document.name,
            revision=result.document.revision,
            report=self._report(
                plan,
                logical_mutations=1,
                operations=len(operations),
                revisions=1,
            ),
        )

    def _report(
        self,
        plan: _Plan,
        *,
        logical_mutations: int,
        operations: int,
        revisions: int,
    ) -> CadImportReport:
        return CadImportReport(
            source=plan.source,
            counts=plan.counts,
            source_bounds=plan.source_bounds,
            imported_bounds=plan.imported_bounds,
            frame=plan.frame,
            canvas={
                "width": round(plan.canvas_width, 3),
                "height": round(plan.canvas_height, 3),
            },
            layers=plan.layers,
            issues=plan.issues,
            operations=operations,
            revisions=revisions,
            logical_mutations=logical_mutations,
            duration_ms=plan.duration_ms,
            warnings=plan.warnings,
        )


__all__ = [
    "MAX_FONT_SIZE",
    "SUPPORTED_ENCODINGS",
    "CadImportError",
    "CadImporter",
    "detect_format",
    "read_source_bytes",
]
