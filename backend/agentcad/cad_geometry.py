"""Shared geometry layer for CAD import.

Charter reference: §14 (drawing intake), §21.6 (reviewability), P0 ("structural
heuristics must not create engineering semantics").

Both source formats — a DXF stream (``cad_dxf``) and a DWG object dump (``cad_dwg``) —
decode into the same :class:`CadPrimitive` list, so the document builder in
``cad_import`` has exactly one geometry contract to honour. This module is deliberately
independent of any CAD library: the DWG byte decoder is an external tool invoked by
``cad_convert``, and the DXF group-code reader in ``cad_dxf`` is ours.

Two conventions matter for fidelity, and both are carried explicitly rather than
folded into numbers:

* **Curves keep their local definition plus an affine transform.** A block instance can
  be mirrored (negative scale) or rotated, and a scalar radius cannot express that, so
  :class:`CadPrimitive` stores the circle/arc/ellipse in *local* coordinates and samples
  it through :class:`Affine` when a world point is needed.
* **Nothing is invented.** An entity the decoder does not understand is counted and
  reported through :class:`CadIssueCollector`; it is never guessed at.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Literal


class CadGeometryError(ValueError):
    """A source file that could not be decoded, with a machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

CadPrimitiveKind = Literal["line", "polyline", "circle", "arc", "ellipse", "text", "fill"]

Matrix = tuple[float, float, float, float]
IDENTITY_MATRIX: Matrix = (1.0, 0.0, 0.0, 1.0)
Point = tuple[float, float]

#: Default ink for imported geometry. ACI 7 is "white on black / black on white"; the
#: editor draws on white, and its own drawings use this value, so imports match it.
DEFAULT_INK = "#111827"

#: A small but standard ACI palette. Only used when the source carries no true colour.
ACI_PALETTE: dict[int, str] = {
    1: "#ff0000",
    2: "#ffff00",
    3: "#00ff00",
    4: "#00ffff",
    5: "#0000ff",
    6: "#ff00ff",
    7: DEFAULT_INK,
    8: "#808080",
    9: "#c0c0c0",
    30: "#ff7f00",
    40: "#ffbf00",
    50: "#bfff00",
    90: "#00ff7f",
    130: "#007fff",
    150: "#0000ff",
    170: "#7f00ff",
    200: "#ff00bf",
    230: "#ff003f",
    250: "#404040",
    251: "#505050",
    252: "#696969",
    253: "#828282",
    254: "#bebebe",
    255: "#ffffff",
}

#: Linetype names are conventions, not a spec we can read here; these are the shapes
#: engineering drawings actually use. The length unit is the drawing unit.
DASH_PATTERNS: dict[str, list[float]] = {
    "CENTER": [24.0, 6.0, 6.0, 6.0],
    "DASHDOT": [12.0, 6.0, 2.0, 6.0],
    "DASHED": [12.0, 6.0],
    "HIDDEN": [6.0, 4.0],
    "PHANTOM": [24.0, 6.0, 6.0, 6.0, 6.0, 6.0],
    "DOT": [2.0, 4.0],
}


def matrix_mul(outer: Matrix, inner: Matrix) -> Matrix:
    """Return ``outer · inner`` for row-major 2x2 matrices with ``p' = M p``."""

    a1, b1, c1, d1 = outer
    a2, b2, c2, d2 = inner
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
    )


def matrix_apply(matrix: Matrix, point: Point) -> Point:
    a, b, c, d = matrix
    return (a * point[0] + c * point[1], b * point[0] + d * point[1])


def matrix_scale(matrix: Matrix) -> float:
    """Uniform scale factor of a matrix, used for radii and text heights."""

    return math.sqrt(abs(matrix[0] * matrix[3] - matrix[1] * matrix[2]))


def matrix_is_uniform(matrix: Matrix, tolerance: float = 1e-9) -> bool:
    """True when the matrix scales both axes equally (so a circle stays a circle)."""

    a, b, c, d = matrix
    sx = math.hypot(a, b)
    sy = math.hypot(c, d)
    return abs(sx - sy) <= tolerance * max(1.0, sx, sy)


_MTEXT_CODE = re.compile(r"\\[A-Za-z][^;\\{}]*;")


def clean_text(value: str) -> str:
    """Turn a CAD text payload into the characters an engineer sees.

    Both formats ship formatting inside the string: ``%%d``/``%%p``/``%%c`` symbols in
    TEXT, and MTEXT runs like ``{\\fSimSun|b0;...}`` with ``\\P`` paragraph breaks. The
    editor stores plain text, so the codes are resolved instead of rendered as noise.
    """

    global _MTEXT_CODE
    if not value:
        return ""
    text = value
    text = text.replace("\\P", "\n").replace("\\p", "\n")
    text = text.replace("%%d", "°").replace("%%D", "°")
    text = text.replace("%%p", "±").replace("%%P", "±")
    text = text.replace("%%c", "⌀").replace("%%C", "⌀")
    text = _MTEXT_CODE.sub("", text)
    text = text.replace("{", "").replace("}", "")
    text = " ".join(part.strip() for part in text.splitlines() if part.strip())
    return text.strip()


def plain_true_color(value: object) -> str | None:
    """A true colour carried as a zero-flagged ``rrggbb`` payload, or ``None``.

    LibreDWG writes the raw DWG colour word: a plain six-digit triplet when the colour
    is a true colour, and a flag byte in front of it (``c3000007``) when the value also
    encodes an index. Only the unflagged, non-black shape is trusted here — measured on
    a production sheet, every flagged payload accompanied an ACI index that is the
    better answer, and entity colours are frequently written as ``000000`` placeholders.
    """

    if not isinstance(value, str):
        return None
    digits = value.lower()
    if len(digits) != 6 or any(character not in "0123456789abcdef" for character in digits):
        return None
    return None if digits == "000000" else "#" + digits


def resolve_dwg_color(
    index: object,
    rgb: object,
    *,
    inherited: str,
    layer_ink: str,
) -> str:
    """Resolve a DWG ``(ACI index, raw colour)`` pair into the ink to draw with.

    Index 0 is BYBLOCK (inherit the block instance's colour), 256/absent is BYLAYER
    (take the layer's colour), 7 is the drawing's own ink, and 1..255 are the ACI
    palette. A true colour is used only where there is no usable index.
    """

    if isinstance(index, int) and not isinstance(index, bool):
        if index == 0:
            return inherited
        if index == 7:
            return DEFAULT_INK
        if 1 <= index <= 255:
            return ACI_PALETTE.get(index, layer_ink)
    true = plain_true_color(rgb)
    if true:
        return true
    return layer_ink


def aci_color(index: object) -> str | None:
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    if index in (0, 256):
        return None
    return ACI_PALETTE.get(index)


def dash_for_linetype(name: str) -> list[float]:
    """Map a linetype name onto a dash pattern; unknown names stay solid."""

    cleaned = "".join(character for character in (name or "").upper() if character.isalnum())
    if not cleaned or cleaned in {"CONTINUOUS", "SOLID", "BYLAYER", "BYBLOCK"}:
        return []
    # Longest match first so DASHDOT2 does not collapse into DASH.
    for key in sorted(DASH_PATTERNS, key=len, reverse=True):
        if key in cleaned:
            return list(DASH_PATTERNS[key])
    return []


@dataclass(frozen=True)
class Affine:
    """A 2D affine transform: ``p' = matrix · p + translation``."""

    matrix: Matrix = IDENTITY_MATRIX
    translation: Point = (0.0, 0.0)

    def apply(self, point: Point) -> Point:
        x, y = matrix_apply(self.matrix, point)
        return (x + self.translation[0], y + self.translation[1])

    def compose(self, inner: Affine) -> Affine:
        """Return ``self ∘ inner``: ``inner`` is applied first."""

        translated = matrix_apply(self.matrix, inner.translation)
        return Affine(
            matrix=matrix_mul(self.matrix, inner.matrix),
            translation=(
                translated[0] + self.translation[0],
                translated[1] + self.translation[1],
            ),
        )

    @property
    def scale(self) -> float:
        return matrix_scale(self.matrix)

    def is_identity(self) -> bool:
        return self.matrix == IDENTITY_MATRIX and self.translation == (0.0, 0.0)


IDENTITY = Affine()


def ocs_transform(extrusion: tuple[float, float, float] | None) -> Affine:
    """Transform from a DXF object coordinate system into world coordinates.

    The arbitrary axis algorithm is the documented DXF rule for entities that are not
    drawn in the world XY plane; the common case that matters for 2D drawings is
    ``(0, 0, -1)``, which mirrors the entity on the X axis (mirrored block inserts use
    it). Implemented from the published rule, not from an implementation.
    """

    if not extrusion:
        return IDENTITY
    ax_ = float(extrusion[0])
    ay_ = float(extrusion[1])
    az_ = float(extrusion[2])
    length = math.sqrt(ax_ * ax_ + ay_ * ay_ + az_ * az_)
    if length == 0.0:
        return IDENTITY
    ax_, ay_, az_ = ax_ / length, ay_ / length, az_ / length
    if abs(ax_ - 0.0) < 1e-12 and abs(ay_ - 0.0) < 1e-12:
        if abs(az_ - 1.0) < 1e-12:
            return IDENTITY
        # Arm A: extrusion parallel to the world Z axis -> mirror on X.
        return Affine(matrix=(-1.0, 0.0, 0.0, 1.0))
    if abs(ax_ * 0.0 + ay_ * 1.0 + az_ * 0.0) < 1.0 / 64.0:
        # Arm B: |Az × Wy| is small, so derive Ax from Wz × Az.
        ax_x, ax_y = -ay_, ax_
    else:
        # Arm C: Ax = Wy × Az = (-Az.z, 0, Az.x) normalised.
        ax_x, ax_y = -az_, 0.0
    norm = math.sqrt(ax_x * ax_x + ax_y * ax_y)
    if norm == 0.0:
        return IDENTITY
    ax_x, ax_y = ax_x / norm, ax_y / norm
    # Ay = Az × Ax, also orthogonal to the Z axis by construction.
    ay_x = ay_ * 0.0 - az_ * ax_y
    ay_y = az_ * ax_x - ax_ * 0.0
    return Affine(matrix=(ax_x, ay_x, ax_y, ay_y))


@dataclass(frozen=True)
class CadPrimitive:
    """One drawable shape in source coordinates, ready to become native geometry."""

    kind: CadPrimitiveKind
    layer: str = "0"
    color: str = DEFAULT_INK
    linetype: str = ""
    #: World coordinates for lines, polylines and fills; the anchor for text.
    points: tuple[Point, ...] = ()
    #: Local coordinates plus a transform for circles, arcs and ellipses.
    center: Point | None = None
    radius: float = 0.0
    start_angle: float = 0.0
    end_angle: float = 0.0
    major_axis_angle: float = 0.0
    axis_ratio: float = 1.0
    start_param: float = 0.0
    end_param: float = math.tau
    transform: Affine = IDENTITY
    closed: bool = False
    text: str = ""
    height: float = 0.0
    anchor: str = "start"
    block: str = ""
    handle: str = ""

    # -- geometry ----------------------------------------------------------- #

    def curve_points(self, curve_segments: int = 24) -> list[Point]:
        """Sample a curve into world-space points (agentcad has no arc primitive)."""

        if self.center is None:
            return list(self.points)
        cx, cy = self.center
        segments = max(8, curve_segments)
        if self.kind == "circle":
            count = max(segments, 48)
            local = [
                (
                    cx + self.radius * math.cos(math.tau * index / count),
                    cy + self.radius * math.sin(math.tau * index / count),
                )
                for index in range(count + 1)
            ]
        elif self.kind == "arc":
            start, end = self.start_angle, self.end_angle
            while end <= start:
                end += math.tau
            count = max(6, int(segments * (end - start) / math.pi) + 2)
            local = [
                (
                    cx + self.radius * math.cos(start + (end - start) * index / count),
                    cy + self.radius * math.sin(start + (end - start) * index / count),
                )
                for index in range(count + 1)
            ]
        elif self.kind == "ellipse":
            start, end = self.start_param, self.end_param
            while end <= start:
                end += math.tau
            count = max(8, int(segments * (end - start) / math.pi) + 2)
            cos_axis, sin_axis = math.cos(self.major_axis_angle), math.sin(
                self.major_axis_angle
            )
            local = []
            for index in range(count + 1):
                parameter = start + (end - start) * index / count
                ux = self.radius * math.cos(parameter)
                uy = self.radius * self.axis_ratio * math.sin(parameter)
                local.append(
                    (cx + ux * cos_axis - uy * sin_axis, cy + ux * sin_axis + uy * cos_axis)
                )
        else:
            return list(self.points)
        return [self.transform.apply(point) for point in local]

    def is_closed_curve(self) -> bool:
        if self.kind == "circle":
            return True
        if self.kind != "ellipse":
            return False
        return abs((self.end_param - self.start_param) % math.tau) < 1e-9

    def bounds(self, curve_segments: int = 24) -> tuple[float, float, float, float] | None:
        if self.kind == "text":
            if not self.points:
                return None
            height = max(self.height * abs(self.transform.scale), 1e-6)
            x, y = self.points[0]
            return (x, y - height, x + height, y + height * 0.2)
        points = self.curve_points(curve_segments)
        if not points:
            return None
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class CadIssueCollector:
    """Accumulates "what could not be reproduced" instead of dropping it silently."""

    codes: dict[str, int] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)
    details: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(
        self,
        code: str,
        message: str,
        *,
        count: int = 1,
        detail: dict[str, int] | None = None,
    ) -> None:
        if count <= 0:
            return
        self.codes[code] = self.codes.get(code, 0) + count
        self.messages.setdefault(code, message)
        if detail:
            bucket = self.details.setdefault(code, {})
            for key, value in detail.items():
                bucket[key] = bucket.get(key, 0) + value

    def extend(self, other: CadIssueCollector) -> None:
        for code, count in other.codes.items():
            self.add(code, other.messages.get(code, code), count=count)
        for code, detail in other.details.items():
            bucket = self.details.setdefault(code, {})
            for key, value in detail.items():
                bucket[key] = bucket.get(key, 0) + value

    def sorted_items(self) -> list[tuple[str, int]]:
        return sorted(self.codes.items())

    def empty(self) -> bool:
        return not self.codes


@dataclass
class CadDecodeResult:
    """What one decoder produced, plus every admission it has to make about it."""

    primitives: list[CadPrimitive] = field(default_factory=list)
    #: Source layer name -> the ink that layer draws with.
    layers: dict[str, str] = field(default_factory=dict)
    issues: CadIssueCollector = field(default_factory=CadIssueCollector)
    #: Entity type -> how many were seen (decoded or not).
    entity_kinds: dict[str, int] = field(default_factory=dict)
    extents: tuple[float, float, float, float] | None = None
    encoding: str = ""
    format_detail: str = ""
    paper_space_skipped: int = 0

    def note(self, kind: str, count: int = 1) -> None:
        if count <= 0:
            return
        self.entity_kinds[kind] = self.entity_kinds.get(kind, 0) + count


def fill_polygon(corners: list[Point], *, swapped: bool) -> list[Point]:
    """Order the corners of a SOLID/TRACE/3DFACE into a drawable polygon.

    A SOLID stores its third and fourth corners in crossed order, so the outline is
    1-2-4-3; a 3DFACE is already in order and may repeat its last corner for a
    triangle. Getting this wrong turns every arrowhead into a bow tie.
    """

    points = list(corners)[:4]
    if swapped and len(points) == 4:
        points = [points[0], points[1], points[3], points[2]]
    deduped: list[Point] = []
    for point in points:
        if not deduped or deduped[-1] != point:
            deduped.append(point)
    if len(deduped) >= 2 and deduped[0] == deduped[-1]:
        deduped.pop()
    return deduped


def bounds_of(
    primitives: list[CadPrimitive], curve_segments: int = 24
) -> tuple[float, float, float, float] | None:
    boxes = [box for box in (item.bounds(curve_segments) for item in primitives) if box]
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def bounds_contain(bounds: tuple[float, float, float, float], point: Point) -> bool:
    return bounds[0] <= point[0] <= bounds[2] and bounds[1] <= point[1] <= bounds[3]


def boxes_intersect(
    first: tuple[float, float, float, float], second: tuple[float, float, float, float]
) -> bool:
    return not (
        first[2] < second[0] or second[2] < first[0] or first[3] < second[1] or second[3] < first[1]
    )


def bulge_to_arc(
    start: Point,
    end: Point,
    bulge: float,
    curve_segments: int = 24,
) -> list[Point]:
    """Interpolate a polyline bulge (the DXF tangent-of-a-quarter-angle arc).

    The centre is placed on the right of the chord by ``r·cos(theta/2)`` with a signed
    radius, and the sweep runs clockwise for a positive bulge, which is what makes the
    result bulge to the left of ``start -> end`` (the documented convention).
    """

    if bulge == 0.0:
        return [start, end]
    theta = 4.0 * math.atan(bulge)
    dx, dy = end[0] - start[0], end[1] - start[1]
    chord = math.hypot(dx, dy)
    if chord == 0.0 or abs(math.sin(theta / 2.0)) < 1e-12:
        return [start, end]
    radius = chord / (2.0 * math.sin(theta / 2.0))
    # Left-hand normal of the chord.
    normal = (-dy / chord, dx / chord)
    offset = radius * math.cos(theta / 2.0)
    mid = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
    centre = (mid[0] - normal[0] * offset, mid[1] - normal[1] * offset)
    start_angle = math.atan2(start[1] - centre[1], start[0] - centre[0])
    sweep = -theta
    count = max(4, int(curve_segments * abs(sweep) / math.pi) + 2)
    points = [
        (
            centre[0] + abs(radius) * math.cos(start_angle + sweep * index / count),
            centre[1] + abs(radius) * math.sin(start_angle + sweep * index / count),
        )
        for index in range(count + 1)
    ]
    points[0] = start
    points[-1] = end
    return points


__all__ = [
    "ACI_PALETTE",
    "DEFAULT_INK",
    "IDENTITY",
    "IDENTITY_MATRIX",
    "Affine",
    "CadDecodeResult",
    "CadGeometryError",
    "CadIssueCollector",
    "CadPrimitive",
    "CadPrimitiveKind",
    "Matrix",
    "Point",
    "aci_color",
    "bounds_contain",
    "bounds_of",
    "boxes_intersect",
    "bulge_to_arc",
    "clean_text",
    "dash_for_linetype",
    "fill_polygon",
    "matrix_apply",
    "matrix_is_uniform",
    "matrix_mul",
    "matrix_scale",
    "ocs_transform",
    "plain_true_color",
    "resolve_dwg_color",
]
