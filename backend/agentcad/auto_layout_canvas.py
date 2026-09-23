"""M7-2 phase 2B step 4: the content envelope, the derived canvas, and the final geometry checks.

The canvas is the tail of the derivation chain. Everything before this step decided *what* is
drawn and where; this step asks how much room that needs, and the answer is a measurement rather
than an instruction. That ordering is the whole point: a canvas handed to the engine is a second
geometry authority -- and, as the default ``1600x900`` showed, a coordinate authority nobody
chose and everybody inherited.

Three things happen here:

1. **Measure the content.** The envelope covers what the layout actually drew: the materialized
   node bounds, every routed waypoint, and every annotation box. The list is closed and declared
   in the contract, because "the canvas fits the drawing" is only checkable once "the drawing"
   is enumerated -- a bounds computed over a subset is a canvas that crops.
2. **Derive the canvas.** The margin is the density class's declared policy, and the aspect class
   plus orientation decide which axis has to grow to reach the class's ratio. Growth only:
   reaching a ratio by shrinking would crop, which is the one thing a derived canvas exists to
   prevent.
3. **Verify.** Two checks, both about the drawing rather than about the code. Nothing may stick
   out of the canvas, and no route may pass through a node's interior except the single segment
   at each end that leaves or enters its own endpoint. That second check is the step-3 ruling
   implemented where it can see the whole drawing, instead of inside the router's candidate
   ordering where the router is both judge and defendant.

Pure functions of the plan and the declared rules: no clock, no service, no measurement of the
machine it runs on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from math import ceil, floor
from typing import Any

from .auto_layout_geometry import Rect
from .auto_layout_semantic import (
    STEP_3,
    STEP_4,
    SemanticLayoutPlan,
    SemanticTopologyIngressError,
    _quantize,
)
from .m7_endpoint_binding import binding_index
from .m7_layout_contract import (
    ASPECT_CLASS_TARGET_RATIOS,
    LAYOUT_COORDINATE_DECIMALS,
    MARGIN_IS_PRESERVED_IN_THE_DERIVED_CANVAS,
    ROUTE_SEGMENTS_BEYOND_THE_ESCAPE_SEGMENT_MUST_STAY_OUTSIDE_PROTECTED_NODE_INTERIORS,
)


class StepFourError(SemanticTopologyIngressError):
    """A step-4 refusal: something about the canvas or the final geometry is not admissible."""

    code = "step_four_error"


class UnknownCanvasMarginClassError(StepFourError):
    """A density class with no declared margin. Hard failure, never a default."""

    code = "unknown_canvas_margin_class"


class UnknownAspectClassError(StepFourError):
    """A preferred aspect class with no declared ratio. Hard failure, never a guess."""

    code = "unknown_aspect_class"


class UnknownOrientationError(StepFourError):
    """An orientation the canvas cannot enforce. Hard failure, never a silent landscape."""

    code = "unknown_orientation"


class CanvasClippingError(StepFourError):
    """Presentation geometry that the derived canvas would crop."""

    code = "canvas_clips_content"


class RouteCrossesProtectedNodeError(StepFourError):
    """A route passing through a node's interior beyond the segment that enters or leaves it."""

    code = "route_crosses_protected_node"


class UnknownPresentationKindError(StepFourError):
    """A presentation kind with no declared stroke envelope. Hard failure, never a zero."""

    code = "unknown_presentation_kind"


#: Declared engine numbers under ``LAYOUT_RULES_VERSION``, keyed by the density class the caller
#: asked for -- the same shape as the spacing policy, and for the same reason: a margin nobody
#: declared is a margin that drifts with the grid, the host or the export format.
CANVAS_MARGIN_POLICY: tuple[tuple[str, float], ...] = (
    ("compact", 32.0),
    ("comfortable", 48.0),
)

#: The ratio each aspect class names. Read from the contract so that "which ratio does `wide`
#: mean" has one answer; `extra_wide` -- roughly 12:1 -- is the class the reference plant drawing
#: needs.
ASPECT_CLASS_RATIO_POLICY: tuple[tuple[str, float], ...] = tuple(ASPECT_CLASS_TARGET_RATIOS)

#: How many segments at each end of a route are the escape/entry segment. One, because that is
#: where the port anchor and its stub live; a second segment would already be traversing the
#: symbol rather than leaving it.
ROUTE_ENDPOINT_ESCAPE_SEGMENTS = 1


@dataclass(frozen=True)
class PresentationStrokeEnvelope:
    """How far a stroked path reaches beyond its own geometry, by kind.

    ``half_stroke`` is ``stroke_width / 2``; ``cap_join_allowance`` covers what a cap or a miter
    join adds on top of that. Both are declared numbers rather than renderer measurements: the
    same drawing has to measure the same on every machine, and a canvas derived from a browser's
    idea of a stroke would put the machine into the drawing's identity.
    """

    stroke_width: float
    cap_join_allowance: float

    @property
    def reach(self) -> float:
        return self.stroke_width / 2 + self.cap_join_allowance

    def to_projection(self) -> dict[str, float]:
        return {
            "stroke_width": self.stroke_width,
            "cap_join_allowance": self.cap_join_allowance,
            "reach": self.reach,
        }


#: Declared engine numbers under ``LAYOUT_RULES_VERSION``, one entry per presentation kind. The
#: widths match the document's own default style (``stroke_width = 1.5`` for outlines and
#: connectors); the allowances are conservative envelopes rather than renderer queries.
PRESENTATION_STROKE_POLICY: tuple[tuple[str, PresentationStrokeEnvelope], ...] = (
    ("symbol_outline", PresentationStrokeEnvelope(stroke_width=1.5, cap_join_allowance=0.75)),
    ("connector", PresentationStrokeEnvelope(stroke_width=1.5, cap_join_allowance=1.0)),
    ("leader_line", PresentationStrokeEnvelope(stroke_width=1.0, cap_join_allowance=0.5)),
    ("annotation_text", PresentationStrokeEnvelope(stroke_width=0.0, cap_join_allowance=0.0)),
)


def presentation_stroke_envelope(kind: str) -> PresentationStrokeEnvelope:
    """The declared envelope for a presentation kind. Unknown is a failure, not a zero."""

    for name, envelope in PRESENTATION_STROKE_POLICY:
        if name == kind:
            return envelope
    raise UnknownPresentationKindError(
        f"no stroke envelope is declared for presentation kind {kind!r}: assuming it has no "
        "stroke would fit the canvas to a centerline"
    )


def rendered_extent(kind: str, box: Rect) -> Rect:
    """A kind's rendered extent: its geometry inflated by the declared stroke envelope."""

    return box.expanded(presentation_stroke_envelope(kind).reach)


def canvas_margin(density: str) -> float:
    """The declared margin for a density class. Unknown is a failure, not a default (§13)."""

    for name, margin in CANVAS_MARGIN_POLICY:
        if name == density:
            return margin
    raise UnknownCanvasMarginClassError(
        f"no canvas margin is declared for density {density!r}: applying a default would put a "
        "margin in the drawing that nobody chose"
    )


def aspect_class_ratio(preferred_aspect_class: str) -> float:
    for name, ratio in ASPECT_CLASS_RATIO_POLICY:
        if name == preferred_aspect_class:
            return ratio
    raise UnknownAspectClassError(
        f"no target ratio is declared for aspect class {preferred_aspect_class!r}"
    )


@dataclass(frozen=True)
class PresentationBoxes:
    """Everything the canvas has to contain, as rendered extents.

    A route is kept as its centerline points plus the stroke reach that applies to them: the
    centerline is what the router produced and the reach is what the renderer adds, and a check
    that can see only the first is the check the gate caught.
    """

    nodes: tuple[tuple[str, Rect], ...]
    routes: tuple[tuple[str, tuple[tuple[float, float], ...], float], ...]
    annotations: tuple[tuple[str, Rect], ...]

    def boxes(self) -> tuple[Rect, ...]:
        return (
            *(box for _name, box in self.nodes),
            *(
                _point_box(point).expanded(reach)
                for _name, points, reach in self.routes
                for point in points
            ),
            *(box for _name, box in self.annotations),
        )


def _point_box(point: tuple[float, float]) -> Rect:
    """One waypoint as a point box, before the kind's stroke envelope is applied."""

    return Rect(point[0], point[1], 0.0, 0.0)


def _route_points(row: Mapping[str, Any]) -> tuple[tuple[float, float], ...]:
    start = (row["x"], row["y"])
    rest = tuple((point[0], point[1]) for point in row.get("ordered_waypoints", ()))
    return (start, *rest)


def presentation_boxes(plan: SemanticLayoutPlan) -> PresentationBoxes:
    """Read the drawing back out of the plan as *rendered* extents.

    A symbol's declared box is not its drawn box -- catalogue shapes reach the box edges, so a
    stroked outline lands half a stroke outside it -- and a connector's waypoints are its
    centerline, not its stroke. Every kind therefore contributes its geometry inflated by the
    envelope the stroke policy declares for it, which is what makes "all presentation geometry
    fits the canvas" a claim about the same picture the renderer draws.
    """

    symbols = presentation_stroke_envelope("symbol_outline").reach
    connectors = presentation_stroke_envelope("connector").reach
    annotations = presentation_stroke_envelope("annotation_text").reach
    return PresentationBoxes(
        nodes=tuple(
            (
                row["engineering_id"],
                Rect(row["x"], row["y"], row["width"], row["height"]).expanded(symbols),
            )
            for row in plan.placement
        ),
        routes=tuple(
            (
                row["engineering_id"],
                tuple(
                    (point[0], point[1]) for point in _route_points(row)
                ),
                connectors,
            )
            for row in plan.routing
        ),
        annotations=tuple(
            (
                row["engineering_id"],
                Rect(row["x"], row["y"], row["width"], row["height"]).expanded(annotations),
            )
            for row in plan.annotations
        ),
    )


def content_bounds_of(plan: SemanticLayoutPlan) -> Rect:
    """The envelope of everything the layout drew -- nodes, routes and annotations together."""

    boxes = presentation_boxes(plan).boxes()
    if not boxes:
        raise StepFourError(
            "there is no content to measure: a canvas derived from an empty drawing would be a "
            "canvas with nothing in it"
        )
    x = min(box.x for box in boxes)
    y = min(box.y for box in boxes)
    return Rect(
        _quantize(x),
        _quantize(y),
        _quantize(max(box.right for box in boxes) - x),
        _quantize(max(box.bottom for box in boxes) - y),
    )


def _floor_to_quantum(value: float) -> float:
    """Round a coordinate *outwards*, so quantizing the canvas can never pull an edge in."""

    scale = 10**LAYOUT_COORDINATE_DECIMALS
    return _quantize(floor(value * scale + 1e-9) / scale)


def _ceil_to_quantum(value: float) -> float:
    scale = 10**LAYOUT_COORDINATE_DECIMALS
    return _quantize(ceil(value * scale - 1e-9) / scale)


def derive_canvas_bounds(
    content: Rect,
    margin: float,
    orientation: str,
    preferred_aspect_class: str,
) -> Rect:
    """The canvas: the content plus the declared margin, grown to the class's ratio.

    The growth is one-directional on purpose. ``landscape`` means width/height is the class ratio
    and ``portrait`` means height/width is; whichever axis is short of that gets extended. Both
    axes therefore still cover the content plus its margin, which is what makes "the canvas never
    crops" a property of the derivation rather than a hope.
    """

    ratio = aspect_class_ratio(preferred_aspect_class)
    minimum_width = content.width + margin * 2
    minimum_height = content.height + margin * 2
    if orientation == "landscape":
        width = max(minimum_width, minimum_height * ratio)
        height = width / ratio
    elif orientation == "portrait":
        height = max(minimum_height, minimum_width * ratio)
        width = height / ratio
    else:
        raise UnknownOrientationError(
            f"the canvas cannot enforce orientation {orientation!r}: landscape and portrait are "
            "the two the intent declares"
        )

    x = _floor_to_quantum(content.x - margin)
    y = _floor_to_quantum(content.y - margin)
    right = _ceil_to_quantum(max(content.right + margin, x + width))
    bottom = _ceil_to_quantum(max(content.bottom + margin, y + height))
    return Rect(x, y, _quantize(right - x), _quantize(bottom - y))


def _inside(box: Rect, canvas: Rect) -> bool:
    return (
        box.x >= canvas.x - 1e-9
        and box.y >= canvas.y - 1e-9
        and box.right <= canvas.right + 1e-9
        and box.bottom <= canvas.bottom + 1e-9
    )


def clipping_problems(plan: SemanticLayoutPlan, canvas: Rect) -> list[str]:
    """Every piece of presentation geometry that the canvas would cut off, named individually."""

    problems: list[str] = []
    boxes = presentation_boxes(plan)
    for name, box in boxes.nodes:
        if not _inside(box, canvas):
            problems.append(
                f"node {name!r} rendered bounds {box} are outside the canvas {canvas}"
            )
    for name, points, reach in boxes.routes:
        for point in points:
            rendered = _point_box(point).expanded(reach)
            if not _inside(rendered, canvas):
                problems.append(
                    f"route {name!r} waypoint {point} has rendered bounds {rendered} outside "
                    f"the canvas {canvas}"
                )
    for name, box in boxes.annotations:
        if not _inside(box, canvas):
            problems.append(
                f"annotation {name!r} rendered bounds {box} are outside the canvas {canvas}"
            )
    return problems


def margin_is_preserved(content: Rect, canvas: Rect, margin: float) -> bool:
    """Whether the canvas still keeps the declared margin on all four sides."""

    return (
        canvas.x <= content.x - margin + 1e-9
        and canvas.y <= content.y - margin + 1e-9
        and canvas.right >= content.right + margin - 1e-9
        and canvas.bottom >= content.bottom + margin - 1e-9
    )


def _segment_crosses_rect_interior(
    start: tuple[float, float], end: tuple[float, float], rect: Rect
) -> bool:
    """Whether a segment passes through a box's interior. Sharing an edge is not crossing."""

    if abs(start[1] - end[1]) <= 1e-9:  # horizontal
        y = start[1]
        if not (rect.y < y < rect.bottom):
            return False
        low, high = sorted((start[0], end[0]))
        return min(high, rect.right) - max(low, rect.x) > 1e-9
    if abs(start[0] - end[0]) <= 1e-9:  # vertical
        x = start[0]
        if not (rect.x < x < rect.right):
            return False
        low, high = sorted((start[1], end[1]))
        return min(high, rect.bottom) - max(low, rect.y) > 1e-9
    return True  # non-orthogonal: not a segment the engine may produce


def route_node_intersection_problems(plan: SemanticLayoutPlan) -> list[str]:
    """The narrowed crossing rule: only the escape/entry segment may enter its endpoint node.

    §12 permits a route to cross the node it serves, because a port anchor inside a symbol can
    only be reached that way. That permission is exactly one segment wide at each end: the first
    segment leaves the source anchor, the last enters the target anchor, and every other segment
    -- against its own endpoints or against any other node -- stays outside node interiors.
    """

    problems: list[str] = []
    if not ROUTE_SEGMENTS_BEYOND_THE_ESCAPE_SEGMENT_MUST_STAY_OUTSIDE_PROTECTED_NODE_INTERIORS:
        problems.append("the crossing rule must stay in force, not be configured away")
    bindings = binding_index(plan.endpoint_bindings)
    rects = {
        row["engineering_id"]: Rect(row["x"], row["y"], row["width"], row["height"])
        for row in plan.placement
    }
    for row in plan.routing:
        connection_id = row["engineering_id"]
        source = bindings.get((connection_id, "source"))
        target = bindings.get((connection_id, "target"))
        if source is None or target is None:
            problems.append(
                f"route {connection_id!r} has no resolved endpoint binding, so nobody can say "
                "which nodes it ends on"
            )
            continue
        points = _route_points(row)
        if len(points) < 2:
            problems.append(f"route {connection_id!r} has fewer than two points")
            continue
        segments = list(zip(points, points[1:], strict=False))
        escape_count = min(ROUTE_ENDPOINT_ESCAPE_SEGMENTS, len(segments))
        source_escape = set(range(escape_count))
        target_escape = set(range(len(segments) - escape_count, len(segments)))
        for index, (start, end) in enumerate(segments):
            for node_id, box in rects.items():
                if not _segment_crosses_rect_interior(start, end, box):
                    continue
                if node_id == source.node_id and index in source_escape:
                    continue
                if node_id == target.node_id and index in target_escape:
                    continue
                problems.append(
                    f"route {connection_id!r} segment {index} ({start} -> {end}) crosses node "
                    f"{node_id!r}, which it neither enters nor leaves at that segment"
                )
    return problems


def derive_semantic_canvas(plan: SemanticLayoutPlan) -> SemanticLayoutPlan:
    """Step 4: measure the content, derive the canvas, and verify the drawing against both."""

    if plan.produced_at_step != STEP_3:
        raise StepFourError(
            "step 4 derives the canvas from the routed drawing, not from one produced at "
            f"{plan.produced_at_step!r}: measuring a drawing that has no routes would give a "
            "canvas too small for them"
        )
    content = content_bounds_of(plan)
    margin = canvas_margin(plan.intent.density)
    canvas = derive_canvas_bounds(
        content,
        margin,
        plan.intent.orientation,
        plan.intent.preferred_aspect_class,
    )
    # The clipping check comes first on purpose. It is a property of the *drawing* and holds for
    # whatever canvas is in front of it -- including a canvas that came from somewhere else. The
    # margin check is a property of the *derivation*; reporting "this canvas would clip the
    # drawing" before "this canvas was not derived as declared" points at the thing a reader can
    # see, and it keeps a stroke fixture from being answered by a margin complaint.
    problems = clipping_problems(plan, canvas)
    if problems:
        raise CanvasClippingError("the derived canvas would clip the drawing: " + "; ".join(problems))
    if MARGIN_IS_PRESERVED_IN_THE_DERIVED_CANVAS and not margin_is_preserved(
        content, canvas, margin
    ):
        raise StepFourError(
            f"the aspect enforcement spent the declared margin: content {content}, canvas "
            f"{canvas}, margin {margin}"
        )
    crossing = route_node_intersection_problems(plan)
    if crossing:
        raise RouteCrossesProtectedNodeError(
            "the final geometry validation rejected the routing: " + "; ".join(crossing)
        )
    return replace(
        plan,
        content_bounds=_bounds_projection(content),
        canvas_bounds=_bounds_projection(canvas),
        produced_at_step=STEP_4,
    )


def _bounds_projection(bounds: Rect) -> dict[str, float]:
    """The envelope as the plan carries it: the same four numbers, quantized like any other."""

    return {
        "x": _quantize(bounds.x),
        "y": _quantize(bounds.y),
        "width": _quantize(bounds.width),
        "height": _quantize(bounds.height),
    }


def bounds_rect(bounds: Mapping[str, float]) -> Rect:
    """A bounds projection back to a rectangle, so callers compare boxes rather than dicts."""

    return Rect(
        float(bounds["x"]),
        float(bounds["y"]),
        float(bounds["width"]),
        float(bounds["height"]),
    )


class SemanticLayoutCanvas:
    """Step 4, mixed into the one layout authority for the same reason steps 1 and 3 are.

    A canvas calculated beside the engine would be a second opinion about the same drawing -- and
    the derived canvas is precisely the number the old default got wrong.
    """

    def layout_semantic_canvas(self, plan: SemanticLayoutPlan) -> SemanticLayoutPlan:
        """Derive the content envelope and the canvas, and verify nothing is clipped."""

        return derive_semantic_canvas(plan)


__all__ = [
    "ASPECT_CLASS_RATIO_POLICY",
    "CANVAS_MARGIN_POLICY",
    "PRESENTATION_STROKE_POLICY",
    "ROUTE_ENDPOINT_ESCAPE_SEGMENTS",
    "CanvasClippingError",
    "PresentationBoxes",
    "PresentationStrokeEnvelope",
    "RouteCrossesProtectedNodeError",
    "SemanticLayoutCanvas",
    "StepFourError",
    "UnknownAspectClassError",
    "UnknownCanvasMarginClassError",
    "UnknownOrientationError",
    "UnknownPresentationKindError",
    "aspect_class_ratio",
    "bounds_rect",
    "canvas_margin",
    "clipping_problems",
    "content_bounds_of",
    "derive_canvas_bounds",
    "derive_semantic_canvas",
    "margin_is_preserved",
    "presentation_boxes",
    "presentation_stroke_envelope",
    "rendered_extent",
    "route_node_intersection_problems",
]
