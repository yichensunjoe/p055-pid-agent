"""M7-2 phase 2B step 3: the frozen symbol geometry a layout is placed against.

Step 2 placed nodes from the engine's own rules, and said so. Real bounds arrive here, which
creates a new way for a drawing to move between two runs: the catalogue itself. If a symbol's
bounding geometry changes and the engine is unchanged, the same topology under the same rules
can legitimately produce a different drawing -- so *which* symbol geometry was used has to be
part of the layout's identity. That is what :class:`SymbolGeometrySnapshot` is: an immutable,
digestible record of the geometry facts this drawing depends on.

The line this module must not cross:

* the **catalogue** owns what a symbol *is* -- its renderer geometry, its intrinsic size, its
  scale constraint, and where its ports sit;
* the **layout engine** owns what an instance *is* -- how large it is drawn, where it sits, which
  rank and band it belongs to, and how the route reaches it.

So the snapshot carries no ``x``, no ``y``, no rank, no canvas, no route. A snapshot that carried
a position would be a second placement authority, which is the defect this milestone exists to
remove, wearing a third hat.

Two choices worth stating because they are choices:

* **The digest covers this layout's closure, not the catalogue.** Placing a drawing that uses
  seventeen symbols must not change identity because someone added a valve definition somewhere
  else. Closure is complete in the other direction: every symbol key the plan needs is in it, and
  a key the catalogue does not define is a hard failure *before* routing rather than a quiet
  fall back to step 2's rule sizes.
* **Port anchors are normalized.** The engine may draw an instance larger or smaller than the
  catalogue's nominal size, so an absolute anchor would only be correct at one size. A hidden
  (suppressed) catalogue key is accepted here: whether a symbol is *visible to the model* is a
  catalogue-audit question, while whether its geometry *exists* is what this step needs, and
  refusing a key the topology already names would only hide the drawing behind a visibility
  question.

An out-of-bounds port anchor is deliberately not judged here: the contract does not claim anchors
must sit inside the bounds, and inventing that rule in the runtime would be a second source of
truth about geometry. What this module does guarantee is that every anchor is finite and
quantized to the declared coordinate quantum.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .m7_layout_contract import (
    ANNOTATION_TEXT_EXTENT_CHARACTER_FACTOR,
    EQUIPMENT_SYMBOL_CATEGORIES_ARE_THE_REST,
    INSTRUMENT_SYMBOL_CATEGORY,
    LAYOUT_COORDINATE_DECIMALS,
    SYMBOL_GEOMETRY_CATALOG_DIGEST_VERSION,
    SYMBOL_GEOMETRY_FACT_EXCLUSIONS,
    SYMBOL_GEOMETRY_FACT_FIELDS,
    SYMBOL_GEOMETRY_PORT_FIELDS,
    SYMBOL_KEY_FIELD,
    SYMBOL_SCALE_CONSTRAINT_DEFAULT,
    SYMBOL_SCALE_CONSTRAINT_METADATA_KEY,
    SYMBOL_SCALE_CONSTRAINTS,
    SYMBOL_SHAPE_KINDS,
)
from .symbols import SymbolRegistry


class SymbolGeometryError(ValueError):
    """The catalogue cannot answer a question the layout asked of it."""

    code = "symbol_geometry_error"

    def __init__(self, message: str, *, symbol_key: str = "") -> None:
        self.symbol_key = symbol_key
        super().__init__(message)


class MissingSymbolGeometryError(SymbolGeometryError):
    """A symbol key the frozen catalogue does not define.

    Hard failure, not a fallback. Falling back to step 2's rule sizes would draw the node as
    *something* while the drawing claims to be made of catalogue symbols -- an invisible
    substitution, which is the failure the grouping-intent ruling rejected one step earlier.
    """

    code = "symbol_geometry_missing"


class UnknownSymbolScaleConstraintError(SymbolGeometryError):
    """A declared scale constraint outside the vocabulary. Defaulting would invent a rule."""

    code = "unknown_symbol_scale_constraint"


class DuplicateSymbolPortError(SymbolGeometryError):
    """Two ports with one id: an anchor reference would be ambiguous, not merely imprecise."""

    code = "duplicate_symbol_port_id"


class EntitySymbolResolutionError(SymbolGeometryError):
    """An entity whose ``symbol_key`` is missing, or whose symbol it may not use."""

    code = "entity_symbol_key_unresolved"


class EntityKindIncompatibleError(SymbolGeometryError):
    """An instrument bound to a non-instrument symbol (or the other way round)."""

    code = "entity_kind_incompatible"


class SymbolNotRenderableError(SymbolGeometryError):
    """A symbol the catalogue defines without any renderer geometry to draw."""

    code = "symbol_not_renderable"


class SymbolShapeOutOfBoundsError(SymbolGeometryError):
    """A shape whose unstroked geometry leaves the symbol's own declared box.

    The canvas inflates the declared box by the stroke envelope, and containment is what makes
    that envelope *safe* -- nothing the renderer draws can leave it. A shape that overflows breaks
    the proof, so it is refused where the geometry freezes, with the symbol and the shape named,
    because a catalogue defect nobody can locate is a catalogue defect that stays.
    """

    code = "symbol_shape_outside_intrinsic_box"

    def __init__(self, message: str, *, symbol_key: str = "", shape_index: int = -1) -> None:
        self.shape_index = shape_index
        super().__init__(message, symbol_key=symbol_key)


def unstroked_shape_bounds(shape: Any) -> tuple[float, float, float, float]:
    """``(min_x, min_y, max_x, max_y)`` of one catalogue shape, ignoring its stroke.

    Curve control points are included rather than sampled: a Bezier stays inside the convex hull
    of its control points, so the hull is a conservative bound, while sampling would make the
    check depend on a step size -- and a step size is a decision nobody declared.
    """

    kind = str(shape.get("type") if isinstance(shape, dict) else "")
    if kind not in SYMBOL_SHAPE_KINDS:
        raise SymbolShapeOutOfBoundsError(
            f"shape kind {kind!r} is not one of {list(SYMBOL_SHAPE_KINDS)}: its bounds are "
            "unknown, and an unknown extent cannot be proven to fit the symbol's box",
            shape_index=-1,
        )
    if kind == "line":
        xs = (float(shape["x1"]), float(shape["x2"]))
        ys = (float(shape["y1"]), float(shape["y2"]))
    elif kind == "polyline":
        points = [(float(point[0]), float(point[1])) for point in shape["points"]]
        xs = tuple(point[0] for point in points)
        ys = tuple(point[1] for point in points)
    elif kind == "rect":
        xs = (float(shape["x"]), float(shape["x"]) + float(shape["width"]))
        ys = (float(shape["y"]), float(shape["y"]) + float(shape["height"]))
    elif kind == "circle":
        cx, cy, radius = float(shape["cx"]), float(shape["cy"]), float(shape["r"])
        xs = (cx - radius, cx + radius)
        ys = (cy - radius, cy + radius)
    elif kind == "path":
        points = _path_points(str(shape["d"]))
        xs = tuple(point[0] for point in points)
        ys = tuple(point[1] for point in points)
    else:  # text
        font_size = float(shape.get("font_size", 12.0))
        width = len(str(shape.get("text", ""))) * font_size * ANNOTATION_TEXT_EXTENT_CHARACTER_FACTOR
        xs = (float(shape["x"]) - width, float(shape["x"]) + width)
        ys = (float(shape["y"]) - font_size, float(shape["y"]) + font_size * 0.3)
    return (min(xs), min(ys), max(xs), max(ys))


def arc_extent_corners(
    *,
    start: tuple[float, float],
    end: tuple[float, float],
    radius_x: float,
    radius_y: float,
    rotation: float,
    large_arc: bool,
    sweep: bool,
) -> tuple[tuple[float, float], ...]:
    """Corners that bound an SVG arc, from the arc's own centre.

    The endpoint parameterisation is the SVG specification's (F.6.5): the centre is recovered
    from the endpoints, the radii and the two flags, radii are scaled up when they are too small
    to span the chord, and the arc then lies inside the ellipse that centre and radii describe.
    A degenerate radius is a straight line to the endpoint, which the endpoints already bound.
    """

    if radius_x == 0.0 or radius_y == 0.0:
        return (start, end)
    cos_rotation, sin_rotation = math.cos(rotation), math.sin(rotation)
    half_dx = (start[0] - end[0]) / 2
    half_dy = (start[1] - end[1]) / 2
    x1 = cos_rotation * half_dx + sin_rotation * half_dy
    y1 = -sin_rotation * half_dx + cos_rotation * half_dy
    scale = (x1 / radius_x) ** 2 + (y1 / radius_y) ** 2
    if scale > 1.0:
        factor = math.sqrt(scale)
        radius_x *= factor
        radius_y *= factor
    denominator = (radius_x * y1) ** 2 + (radius_y * x1) ** 2
    numerator = (radius_x * radius_y) ** 2 - denominator
    coefficient = math.sqrt(max(0.0, numerator / denominator)) if denominator else 0.0
    if large_arc == sweep:
        coefficient = -coefficient
    centre_x = (
        cos_rotation * coefficient * radius_x * y1 / radius_y
        - sin_rotation * coefficient * radius_y * x1 / radius_x
        + (start[0] + end[0]) / 2
    )
    centre_y = (
        sin_rotation * coefficient * radius_x * y1 / radius_y
        + cos_rotation * coefficient * radius_y * x1 / radius_x
        + (start[1] + end[1]) / 2
    )
    # The axis-aligned extent of a rotated ellipse, which contains the whole arc and therefore
    # also the part of it that is actually drawn.
    half_x = math.sqrt((radius_x * cos_rotation) ** 2 + (radius_y * sin_rotation) ** 2)
    half_y = math.sqrt((radius_x * sin_rotation) ** 2 + (radius_y * cos_rotation) ** 2)
    return (
        (centre_x - half_x, centre_y - half_y),
        (centre_x + half_x, centre_y + half_y),
        start,
        end,
    )


#: The SVG path commands the catalogue may use, with how many numbers each takes. Relative
#: commands are resolved so that a relative path is measured where it actually draws.
_PATH_COMMAND_ARITY: dict[str, int] = {
    "M": 2,
    "L": 2,
    "H": 1,
    "V": 1,
    "C": 6,
    "S": 4,
    "Q": 4,
    "T": 2,
    "A": 7,
    "Z": 0,
}


def _path_points(described: str) -> tuple[tuple[float, float], ...]:
    """Every point a path's ``d`` touches, control points included."""

    tokens = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|-?\d*\.?\d+(?:[eE]-?\d+)?", described)
    points: list[tuple[float, float]] = []
    current = (0.0, 0.0)
    subpath_start = (0.0, 0.0)
    command = ""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.isalpha():
            command = token
            index += 1
            if command.upper() == "Z":
                current = subpath_start
                points.append(current)
                continue
        if not command:
            raise SymbolShapeOutOfBoundsError(
                f"path {described!r} starts with a number rather than a command",
                shape_index=-1,
            )
        arity = _PATH_COMMAND_ARITY[command.upper()]
        if index + arity > len(tokens):
            raise SymbolShapeOutOfBoundsError(
                f"path {described!r} ends mid-command for {command!r}",
                shape_index=-1,
            )
        numbers = [float(value) for value in tokens[index : index + arity]]
        index += arity
        relative = command.islower()
        upper = command.upper()
        if upper in ("M", "L", "T"):
            point = (numbers[0], numbers[1])
        elif upper == "H":
            point = (numbers[0], current[1])
        elif upper == "V":
            point = (current[0], numbers[0])
        elif upper == "A":
            # An arc bulges beyond its endpoints, and the two possible arcs are not symmetric
            # about them: the extent has to come from the arc's own centre. (Inflating the
            # endpoints by the radii instead would flag two real catalogue symbols -- a buffer
            # tank and a column -- that draw perfectly well inside their boxes.)
            rx, ry = abs(numbers[0]), abs(numbers[1])
            rotation = math.radians(numbers[2])
            large_arc, sweep = numbers[3] != 0.0, numbers[4] != 0.0
            endpoint = (numbers[5], numbers[6])
            if relative:
                endpoint = (current[0] + endpoint[0], current[1] + endpoint[1])
            for point in arc_extent_corners(
                start=current,
                end=endpoint,
                radius_x=rx,
                radius_y=ry,
                rotation=rotation,
                large_arc=large_arc,
                sweep=sweep,
            ):
                points.append(point)
            current = endpoint
            points.append(current)
            continue
        else:  # cubic / quadratic: the endpoint is the last pair, the rest are controls
            point = (numbers[-2], numbers[-1])
            for offset in range(0, arity - 2, 2):
                control = (numbers[offset], numbers[offset + 1])
                if relative:
                    control = (current[0] + control[0], current[1] + control[1])
                points.append(control)
        if relative:
            point = (current[0] + point[0], current[1] + point[1])
        current = point
        points.append(current)
        if upper == "M":
            subpath_start = current
    if not points:  # pragma: no cover - a path with no geometry cannot be drawn
        raise SymbolShapeOutOfBoundsError(
            f"path {described!r} has no geometry", shape_index=-1
        )
    return tuple(points)


def symbol_shape_overflow(symbol: Any) -> tuple[str, ...]:
    """Every shape of a symbol that leaves its declared box, described for a human."""

    problems: list[str] = []
    width = float(symbol.width)
    height = float(symbol.height)
    for index, shape in enumerate(symbol.shapes):
        kind = str(shape.get("type") if isinstance(shape, dict) else "")
        try:
            min_x, min_y, max_x, max_y = unstroked_shape_bounds(shape)
        except SymbolShapeOutOfBoundsError as exc:
            problems.append(f"shape {index} ({kind}): {exc}")
            continue
        if min_x < -1e-9 or min_y < -1e-9 or max_x > width + 1e-9 or max_y > height + 1e-9:
            problems.append(
                f"shape {index} ({kind}) spans ({min_x:g}, {min_y:g})..({max_x:g}, {max_y:g}), "
                f"outside the declared box (0, 0)..({width:g}, {height:g})"
            )
    return tuple(problems)


def require_shapes_fit_the_intrinsic_box(symbol: Any) -> None:
    """The invariant the presentation-envelope rule rests on, enforced where geometry freezes."""

    problems = symbol_shape_overflow(symbol)
    if problems:
        raise SymbolShapeOutOfBoundsError(
            f"symbol {symbol.key!r} draws outside its own declared box, so inflating that box "
            "cannot be proved to contain the drawing: " + "; ".join(problems),
            symbol_key=symbol.key,
            shape_index=0,
        )


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _quantize(value: float) -> float:
    rounded = round(value, LAYOUT_COORDINATE_DECIMALS)
    return 0.0 if rounded == 0 else rounded


def _finite(value: float, *, symbol_key: str, what: str) -> float:
    if value != value or value in (float("inf"), float("-inf")):
        raise SymbolGeometryError(
            f"symbol {symbol_key!r} declares a non-finite {what}: a coordinate that is not a "
            "number cannot be placed deterministically",
            symbol_key=symbol_key,
        )
    return value


@dataclass(frozen=True)
class SymbolPortGeometry:
    """One port, as the catalogue states it: identity, semantics, and a normalized anchor."""

    port_id: str
    direction: str
    medium: str
    normalized_x: float
    normalized_y: float

    def to_projection(self) -> dict[str, Any]:
        return {
            "port_id": self.port_id,
            "direction": self.direction,
            "medium": self.medium,
            "normalized_x": self.normalized_x,
            "normalized_y": self.normalized_y,
        }

    def anchor(self, width: float, height: float) -> tuple[float, float]:
        """The port's absolute position on an instance of the given size.

        The engine owns the instance size and asks the fact where the port lands; the fact never
        asks the engine where the instance is.
        """

        return (_quantize(self.normalized_x * width), _quantize(self.normalized_y * height))


@dataclass(frozen=True)
class SymbolGeometryFact:
    """Everything the renderer needs to draw a symbol, and nothing about a drawn instance."""

    symbol_key: str
    renderer_geometry_identity: str
    #: Whether the catalogue declares any renderer geometry at all. A catalogue entry without
    #: shapes is a placeable-looking key that cannot be drawn, so the fact says so rather than
    #: leaving the caller to discover it while routing.
    renderer_supported: bool
    intrinsic_width: float
    intrinsic_height: float
    scale_constraint: str
    #: The placement kinds this symbol may serve, derived from its catalogue category by the
    #: declared compatibility rule. Carried as a fact so a node can be checked against the
    #: *frozen* catalogue it was placed from, not against whatever the registry says later.
    entity_kinds: tuple[str, ...] = ()
    ports: tuple[SymbolPortGeometry, ...] = ()

    @property
    def aspect_ratio(self) -> float:
        return self.intrinsic_width / self.intrinsic_height

    def port(self, port_id: str) -> SymbolPortGeometry | None:
        for candidate in self.ports:
            if candidate.port_id == port_id:
                return candidate
        return None

    def ports_with_direction(self, direction: str) -> tuple[SymbolPortGeometry, ...]:
        """Every port whose declared direction matches, in port id order.

        Sorted because the caller may pick the first of these as the anchor for a connection that
        named no port, and "the first one the catalogue happened to list" is not a rule.
        """

        return tuple(
            sorted(
                (port for port in self.ports if port.direction == direction),
                key=lambda port: port.port_id,
            )
        )

    def serves(self, kind: str) -> bool:
        return kind in self.entity_kinds

    def to_projection(self) -> dict[str, Any]:
        return {
            "symbol_key": self.symbol_key,
            "renderer_geometry_identity": self.renderer_geometry_identity,
            "renderer_supported": self.renderer_supported,
            "intrinsic_width": self.intrinsic_width,
            "intrinsic_height": self.intrinsic_height,
            "scale_constraint": self.scale_constraint,
            "entity_kinds": list(self.entity_kinds),
            "ports": [port.to_projection() for port in self.ports],
        }


@dataclass(frozen=True)
class SymbolGeometrySnapshot:
    """The immutable geometry closure one layout depends on.

    Frozen at construction, so nothing can consult the live registry halfway through routing and
    get a different answer than the digest promised. That is the whole point of freezing rather
    than querying: identity that can change while it is being used is not identity.
    """

    facts: tuple[SymbolGeometryFact, ...]
    digest_version: str = SYMBOL_GEOMETRY_CATALOG_DIGEST_VERSION

    @property
    def closure(self) -> tuple[str, ...]:
        return tuple(fact.symbol_key for fact in self.facts)

    def fact(self, symbol_key: str) -> SymbolGeometryFact | None:
        for candidate in self.facts:
            if candidate.symbol_key == symbol_key:
                return candidate
        return None

    def require(self, symbol_key: str) -> SymbolGeometryFact:
        """The fact for a key, or the refusal that says which symbol geometry is missing."""

        found = self.fact(symbol_key)
        if found is None:
            raise MissingSymbolGeometryError(
                f"the layout depends on symbol {symbol_key!r}, which the frozen snapshot "
                f"({self.digest_version}) does not cover; it was frozen over {list(self.closure)}",
                symbol_key=symbol_key,
            )
        return found

    def to_projection(self) -> dict[str, Any]:
        return {
            "digest_version": self.digest_version,
            "closure": list(self.closure),
            "facts": [fact.to_projection() for fact in self.facts],
        }

    @property
    def digest(self) -> str:
        return symbol_geometry_catalog_digest(self.facts, self.digest_version)


def symbol_geometry_catalog_digest(
    facts: Iterable[SymbolGeometryFact],
    digest_version: str = SYMBOL_GEOMETRY_CATALOG_DIGEST_VERSION,
) -> str:
    """The closure's identity: same facts, same digest, whatever order they were read in."""

    rows = sorted((fact.to_projection() for fact in facts), key=lambda row: row["symbol_key"])
    return _digest(
        {
            "symbol_geometry_catalog_digest_version": digest_version,
            "closure": [row["symbol_key"] for row in rows],
            "facts": rows,
        }
    )


def _scale_constraint(symbol: Any) -> str:
    declared = symbol.metadata.get(SYMBOL_SCALE_CONSTRAINT_METADATA_KEY)
    if declared is None:
        return SYMBOL_SCALE_CONSTRAINT_DEFAULT
    if declared not in SYMBOL_SCALE_CONSTRAINTS:
        raise UnknownSymbolScaleConstraintError(
            f"symbol {symbol.key!r} declares scale constraint {declared!r}, which is not one of "
            f"{list(SYMBOL_SCALE_CONSTRAINTS)}: an unrecognised constraint is a hard failure "
            "rather than a default",
            symbol_key=symbol.key,
        )
    return str(declared)


def _entity_kinds(category: str) -> tuple[str, ...]:
    """The placement kinds a catalogue category may serve, by the declared compatibility rule."""

    if category == INSTRUMENT_SYMBOL_CATEGORY:
        return ("instrument",)
    if EQUIPMENT_SYMBOL_CATEGORIES_ARE_THE_REST:
        return ("equipment",)
    return ()


def _port_geometry(symbol: Any) -> tuple[SymbolPortGeometry, ...]:
    seen: set[str] = set()
    ports: list[SymbolPortGeometry] = []
    for port in symbol.ports:
        if port.id in seen:
            raise DuplicateSymbolPortError(
                f"symbol {symbol.key!r} defines port {port.id!r} more than once: an anchor "
                "reference would be ambiguous rather than merely imprecise",
                symbol_key=symbol.key,
            )
        seen.add(port.id)
        width = _finite(float(symbol.width), symbol_key=symbol.key, what="width")
        height = _finite(float(symbol.height), symbol_key=symbol.key, what="height")
        if width <= 0 or height <= 0:
            raise SymbolGeometryError(
                f"symbol {symbol.key!r} declares a non-positive intrinsic size "
                f"({width}, {height})",
                symbol_key=symbol.key,
            )
        ports.append(
            SymbolPortGeometry(
                port_id=port.id,
                direction=port.direction,
                medium=port.medium,
                normalized_x=_quantize(
                    _finite(float(port.x), symbol_key=symbol.key, what=f"port {port.id!r} x")
                    / width
                ),
                normalized_y=_quantize(
                    _finite(float(port.y), symbol_key=symbol.key, what=f"port {port.id!r} y")
                    / height
                ),
            )
        )
    return tuple(sorted(ports, key=lambda item: item.port_id))


def freeze_symbol_geometry(
    symbol_keys: Iterable[str], *, registry: SymbolRegistry | None = None
) -> SymbolGeometrySnapshot:
    """Read the catalogue once and freeze the facts for exactly these keys.

    The closure is the input, not the catalogue: two drawings that use different symbols get
    different digests, and an edit to a symbol neither of them uses changes neither digest.
    """

    keys = sorted({key for key in symbol_keys if key})
    catalog = registry if registry is not None else SymbolRegistry()
    facts: list[SymbolGeometryFact] = []
    for symbol_key in keys:
        if not catalog.exists(symbol_key):
            raise MissingSymbolGeometryError(
                f"the layout depends on symbol {symbol_key!r}, which the catalogue does not "
                "define; there is no geometry to place against and no rule size to fall back to",
                symbol_key=symbol_key,
            )
        symbol = catalog.get(symbol_key)
        if symbol.shapes:
            require_shapes_fit_the_intrinsic_box(symbol)
        facts.append(
            SymbolGeometryFact(
                symbol_key=symbol.key,
                renderer_geometry_identity=_digest(symbol.shapes),
                renderer_supported=bool(symbol.shapes),
                intrinsic_width=_quantize(
                    _finite(float(symbol.width), symbol_key=symbol.key, what="width")
                ),
                intrinsic_height=_quantize(
                    _finite(float(symbol.height), symbol_key=symbol.key, what="height")
                ),
                scale_constraint=_scale_constraint(symbol),
                entity_kinds=_entity_kinds(str(symbol.category)),
                ports=_port_geometry(symbol),
            )
        )
    return SymbolGeometrySnapshot(facts=tuple(facts))


def symbol_key_field() -> str:
    """The declared field that carries a node's symbol key. Read from the contract, not assumed."""

    return SYMBOL_KEY_FIELD


def declared_symbol_key(node: Any) -> str:
    """The catalogue key a node names, or the refusal that says why it does not name one.

    The field is explicit, so a missing value is a specification defect rather than an
    opportunity to infer: inferring from the engineering class would place the node as whatever
    that class usually looks like, which is not the same statement as "draw it this way".
    """

    value = str(getattr(node, SYMBOL_KEY_FIELD, "") or "").strip()
    if not value:
        raise EntitySymbolResolutionError(
            f"node {node.engineering_id!r} of kind {node.kind!r} declares no {SYMBOL_KEY_FIELD!r}: "
            "without it there is no symbol to place, and inferring one from the engineering "
            "class would draw a different statement than the one that was made",
        )
    return value


def require_node_symbol(node: Any, snapshot: SymbolGeometrySnapshot) -> SymbolGeometryFact:
    """The frozen fact a node is to be drawn from, with all three requirements checked.

    Every requirement comes from the snapshot rather than the live registry: a node must be
    checked against the catalogue it was frozen from, or "the drawing is reproducible" would
    depend on when it was re-run.
    """

    symbol_key = declared_symbol_key(node)
    fact = snapshot.require(symbol_key)
    if not fact.renderer_supported:
        raise SymbolNotRenderableError(
            f"node {node.engineering_id!r} is bound to symbol {symbol_key!r}, which the catalogue "
            "defines without any renderer geometry: there is nothing to place",
            symbol_key=symbol_key,
        )
    if not fact.serves(node.kind):
        raise EntityKindIncompatibleError(
            f"node {node.engineering_id!r} of kind {node.kind!r} is bound to symbol "
            f"{symbol_key!r}, which serves {list(fact.entity_kinds)}: drawing an instrument as "
            "a vessel is a wrong drawing even though both are renderable symbols",
            symbol_key=symbol_key,
        )
    return fact


def symbol_closure_for_kinds(nodes: Iterable[Any]) -> tuple[str, ...]:
    """The closure a set of nodes implies: exactly the keys they name, sorted and deduplicated."""

    return tuple(sorted({declared_symbol_key(node) for node in nodes}))


#: The names this module promises not to be about. Kept as data so the test that walks the
#: snapshot projection can assert it, rather than trusting the docstring.
SNAPSHOT_EXCLUDED_NAMES: tuple[str, ...] = tuple(SYMBOL_GEOMETRY_FACT_EXCLUSIONS)
SNAPSHOT_FACT_FIELDS: tuple[str, ...] = tuple(SYMBOL_GEOMETRY_FACT_FIELDS)
SNAPSHOT_PORT_FIELDS: tuple[str, ...] = tuple(SYMBOL_GEOMETRY_PORT_FIELDS)

#: Recomputed here from the contract, so an accidental second copy of the constant cannot drift.
SNAPSHOT_DEFAULT_SCALE_CONSTRAINT = SYMBOL_SCALE_CONSTRAINT_DEFAULT
