"""Public contract for CAD (DWG/DXF) import.

Charter reference: §7 (one governed write channel), §14 (drawing intake), §21.6
(reviewability). This module holds only the *declared* shapes an adapter or the web
editor exchanges; the decoding and document-building live in ``cad_geometry``,
``cad_convert`` and ``cad_import``.

Two properties are deliberately part of the contract rather than conventions:

* **The report is honest.** Anything the importer could not represent (pattern
  hatches, splines, entities it does not decode) is reported with a code and a count
  instead of disappearing. An import that silently dropped half a sheet would still
  "succeed", so silence is not an option.
* **No engineering meaning is invented.** A CAD import produces geometry, layer
  names, text and block provenance. It never guesses that a circle is a pump or that
  a polyline is a process line, because that is the structural-heuristic-that-creates
  -engineering-semantics failure mode P0 forbids.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .models import StrictModel

CadSourceFormat = Literal["dwg", "dxf"]
CadFillMode = Literal["skip", "solid"]
CadFrame = tuple[float, float, float, float]


class CadImportOptions(StrictModel):
    """Everything a caller may decide about one CAD import.

    Defaults are chosen so that a plain ``import`` reproduces the drawing rather than
    "cleaning it up": the whole sheet is imported, solid fills become filled
    polygons, and text keeps its source height and anchor.
    """

    name: str = Field(default="", max_length=160)
    #: Crop window ``(x0, y0, x1, y1)`` in *source* coordinates. ``None`` imports the
    #: drawing's own extents. Cropping changes the canvas frame, never the geometry
    #: inside it.
    frame: CadFrame | None = None
    #: Solid fills (SOLID/TRACE/3DFACE, and solid HATCH boundaries) become filled
    #: closed polylines. ``skip`` imports their outlines only where an outline entity
    #: already exists, and reports the count it dropped.
    fills: CadFillMode = "solid"
    #: Samples per full turn when a curve has to become a polyline. AgentCAD has no
    #: arc/ellipse primitive, so this is the one place where fidelity is a number.
    curve_segments: int = Field(default=24, ge=8, le=180)
    #: Import only these source layers (exact names). Empty means every layer.
    layers: list[str] = Field(default_factory=list)
    include_text: bool = True
    #: Divides every coordinate and size, so a drawing modelled in millimetres can be
    #: imported at a canvas scale that stays easy to work with. Purely a unit change:
    #: it moves nothing relative to anything else.
    unit_scale: float = Field(default=1.0, gt=0, le=100_000)
    #: The editor draws strokes in screen space (``non-scaling-stroke``), so this is a
    #: visual weight, not a paper width. Large CAD lineweights are scaled down to it.
    stroke_width: float = Field(default=1.8, gt=0, le=20)
    #: Refuse an import that would exceed this many native elements. A guard rail, not
    #: a truncation: a silently truncated drawing would look complete.
    max_elements: int = Field(default=200_000, ge=1, le=5_000_000)
    #: Operations per governed transaction (the store limits one transaction to 1000).
    chunk_size: int = Field(default=1000, ge=1, le=1000)
    #: Copy the source layer colour onto the imported geometry. ``False`` draws
    #: everything in the document's default ink.
    preserve_colors: bool = True


class CadElementCounts(StrictModel):
    layers: int = 0
    lines: int = 0
    polylines: int = 0
    circles: int = 0
    texts: int = 0
    fills: int = 0
    elements: int = 0
    primitives: int = 0
    skipped: int = 0


class CadIssue(StrictModel):
    """One honest admission about what the import could not reproduce."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    count: int = Field(default=1, ge=0)
    detail: dict[str, Any] = Field(default_factory=dict)


class CadConverterInfo(StrictModel):
    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    executable: str = ""
    available: bool = False
    produces: Literal["object-stream", "dxf"] = "dxf"
    version: str = ""
    #: ``verified`` means this project ran it on real drawings; ``unverified`` means it
    #: is selected when present but has not been exercised here.
    evidence: Literal["verified", "unverified"] = "unverified"
    notes: str = ""


class CadCapabilities(StrictModel):
    """What this installation can decode right now, and with which evidence."""

    dxf_import: bool = True
    dwg_import: bool = False
    formats: list[CadSourceFormat] = Field(default_factory=lambda: ["dxf"])
    converters: list[CadConverterInfo] = Field(default_factory=list)
    decode_encoding: list[str] = Field(default_factory=list)
    max_source_bytes: int = 0
    notes: list[str] = Field(default_factory=list)


class CadSourceInfo(StrictModel):
    filename: str = ""
    format: CadSourceFormat = "dxf"
    format_detail: str = ""
    size_bytes: int = Field(default=0, ge=0)
    sha256: str = ""
    encoding: str = ""
    converter: str = ""
    converter_version: str = ""
    converter_command: list[str] = Field(default_factory=list)


class CadImportReport(StrictModel):
    source: CadSourceInfo
    counts: CadElementCounts = Field(default_factory=CadElementCounts)
    #: Bounding box of the *source* geometry before framing.
    source_bounds: CadFrame | None = None
    #: Bounding box of what was actually imported, in source coordinates.
    imported_bounds: CadFrame | None = None
    #: The frame the canvas was built from, in source coordinates.
    frame: CadFrame | None = None
    canvas: dict[str, float] = Field(default_factory=dict)
    layers: list[str] = Field(default_factory=list)
    issues: list[CadIssue] = Field(default_factory=list)
    operations: int = Field(default=0, ge=0)
    revisions: int = Field(default=0, ge=0)
    transactions: int = Field(default=0, ge=0)
    duration_ms: float = Field(default=0.0, ge=0)
    warnings: list[str] = Field(default_factory=list)

    @property
    def artifacts(self) -> list[CadIssue]:
        return [issue for issue in self.issues if issue.count > 0]


class CadImportResult(StrictModel):
    document_id: str
    document_name: str
    revision: int = Field(default=0, ge=0)
    report: CadImportReport


class CadDryRun(StrictModel):
    """A planned import that was never written.

    Used by the CLI/API to answer "what would this do" without creating a document.
    """

    report: CadImportReport
    document_name: str = ""
    operations: int = Field(default=0, ge=0)
    elements: int = Field(default=0, ge=0)


__all__ = [
    "CadCapabilities",
    "CadConverterInfo",
    "CadDryRun",
    "CadElementCounts",
    "CadFillMode",
    "CadFrame",
    "CadImportOptions",
    "CadImportReport",
    "CadImportResult",
    "CadIssue",
    "CadSourceFormat",
    "CadSourceInfo",
]
