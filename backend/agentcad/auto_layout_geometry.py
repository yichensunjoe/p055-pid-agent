"""M7-2 phase 2B step 3: real symbol geometry, orthogonal routing and annotation placement.

Step 2 placed nodes from the engine's own rules and said so. This step replaces those assumed
sizes with the frozen symbol geometry and then has to live with the consequences: a drawing whose
ranks were spaced for a 120x80 placeholder may not survive a 90x140 vessel in the same lane.

Three things happen here, in the order the contract fixes:

1. **Materialize.** Every node is bound to a frozen symbol fact, its instance size becomes the
   symbol's intrinsic size, and the step-2 placement is *re-validated*. If the real sizes still
   fit, the step-2 solution stands -- it was a correct initial solution, not a draft to discard.
   If they do not, the placement is recomputed deterministically from the real sizes, and the
   result is verified against the separately named materialized clearance policy. A reflow that
   still fails is a hard failure, because the alternative is a drawing that looks finished and
   overlaps itself.
2. **Bind, then route.** Endpoint bindings are resolved first (see ``m7_endpoint_binding``) and
   the router consumes bindings, never ports. Every semantic connection produces exactly one
   route; a route may add waypoints and may not add or remove a connection. A route never crosses
   the bounds of a node it does not serve, and a connection that cannot be routed orthogonally is
   a hard diagnostic -- drawing a straight line through equipment is not a fallback, it is the
   defect the rule exists to prevent.
3. **Annotate.** Labels are placed under the nodes they belong to, de-conflicted by pushing down
   in a fixed order. The text extent comes from the existing pure rule in ``annotation_layout``:
   no browser metrics, no installed fonts, no environment dependence, so the machine the layout
   ran on does not leak into the drawing's identity.

Everything here is a pure function of the plan, the frozen snapshot and the declared rules. No
clock, no service, no registry lookup after the snapshot was taken.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .annotation_layout import text_bounds
from .auto_layout_semantic import (
    STEP_2,
    STEP_3,
    SemanticLayoutPlan,
    SemanticTopologyIngressError,
    SpacingPolicy,
    _quantize,
    system_rank_lanes,
)
from .diagram_quality import definition_port_side
from .m7_endpoint_binding import binding_index, resolve_endpoint_bindings
from .m7_layout_contract import LAYOUT_COORDINATE_DECIMALS, ROUTING_IS_ORTHOGONAL_ONLY
from .m7_symbol_geometry import (
    SymbolGeometryFact,
    SymbolGeometrySnapshot,
    require_node_symbol,
)
from .models import Point, TextElement

#: Declared engine numbers, all under ``LAYOUT_RULES_VERSION``. Changing any of them is a rules
#: change and must bump that version -- which is why none of them is a hidden default in a
#: function body.
#:
#: The catalogue's nominal size is used as the instance size in v1. Scaling an instance away from
#: its intrinsic size is a real feature with real typography consequences, and inventing a scale
#: factor now would put a number nobody chose into every drawing.
INSTANCE_SCALE_FACTOR = 1.0
#: How far a route leaves a port before it is allowed to turn.
ROUTE_STUB_LENGTH = 24.0
#: Where a label sits relative to the node it belongs to.
ANNOTATION_LABEL_GAP = 10.0
#: The space kept between two labels that would otherwise touch, and the font size of a label.
ANNOTATION_CLEARANCE = 6.0
ANNOTATION_FONT_SIZE = 12.0
#: De-confliction is bounded: a label that cannot find room within this many pushes is a
#: diagnostic, not a loop that runs until the request times out.
ANNOTATION_PLACEMENT_ATTEMPTS = 32


class StepThreeError(SemanticTopologyIngressError):
    """A step-3 refusal that is not specific to ports or symbol geometry."""

    code = "step_three_error"


class PlacementReflowFailedError(StepThreeError):
    """Real geometry broke the placement, and the deterministic reflow could not repair it."""

    code = "placement_reflow_failed"


class UnknownClearanceClassError(StepThreeError):
    """A density class with no materialized clearance. Hard failure, never a fallback."""

    code = "unknown_clearance_class"


class UnroutableEdgeError(StepThreeError):
    """A connection that cannot be routed orthogonally without crossing protected node bounds."""

    code = "no_orthogonal_route"


class AnnotationPlacementError(StepThreeError):
    """A label that could not be placed clear of the nodes and labels already there."""

    code = "annotation_placement_failed"


@dataclass(frozen=True)
class MaterializedClearance:
    """The clearance a *materialized* placement must keep, keyed by the density class.

    Deliberately not the spacing policy. ``rank_gap`` spaces *origins* on the step-2 lattice;
    this separates the real symbol bounds the drawing now has. A 120-wide node on a 150 origin
    spacing has a clearance of 30, and that violates neither rule -- which is exactly why the
    two rules are named separately instead of one of them borrowing the other's vocabulary.
    """

    minimum_node_clearance: float
    minimum_system_clearance: float

    def to_projection(self) -> dict[str, float]:
        return {
            "minimum_node_clearance": self.minimum_node_clearance,
            "minimum_system_clearance": self.minimum_system_clearance,
        }


#: The numbers the clearance policy maps a density class to. Like the spacing policy, they are
#: engine facts under ``LAYOUT_RULES_VERSION``: a change here is a layout-rules change, not a
#: separate version axis (§13).
MATERIALIZED_CLEARANCE_POLICY: tuple[tuple[str, MaterializedClearance], ...] = (
    (
        "compact",
        MaterializedClearance(minimum_node_clearance=30.0, minimum_system_clearance=60.0),
    ),
    (
        "comfortable",
        MaterializedClearance(minimum_node_clearance=50.0, minimum_system_clearance=100.0),
    ),
)


def materialized_clearance(density: str) -> MaterializedClearance:
    """The clearance policy a density class names. An unknown class is a failure, not a default."""

    for name, clearance in MATERIALIZED_CLEARANCE_POLICY:
        if name == density:
            return clearance
    raise UnknownClearanceClassError(
        f"no materialized clearance is declared for density {density!r}: defaulting would choose "
        "a separation nobody asked for"
    )


@dataclass(frozen=True)
class _PlanNode:
    """The three facts step 3 needs about a node, without holding the whole topology."""

    kind: str
    engineering_id: str
    symbol_key: str


@dataclass(frozen=True)
class Rect:
    """An axis-aligned box. ``touches`` is deliberately strict: sharing an edge is not crossing."""

    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def contains_point(self, point: tuple[float, float]) -> bool:
        return self.x <= point[0] <= self.right and self.y <= point[1] <= self.bottom

    def crosses(self, other: Rect) -> bool:
        return (
            min(self.right, other.right) - max(self.x, other.x) > 1e-9
            and min(self.bottom, other.bottom) - max(self.y, other.y) > 1e-9
        )

    def expanded(self, margin: float) -> Rect:
        return Rect(self.x - margin, self.y - margin, self.width + margin * 2, self.height + margin * 2)


def _rects_from_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Rect]:
    return {
        row["engineering_id"]: Rect(row["x"], row["y"], row["width"], row["height"])
        for row in rows
    }


def _axis_aligned(points: Sequence[tuple[float, float]]) -> bool:
    for start, end in zip(points, points[1:], strict=False):
        if abs(start[0] - end[0]) > 1e-9 and abs(start[1] - end[1]) > 1e-9:
            return False
    return True


def _segment_crosses_rect(
    start: tuple[float, float], end: tuple[float, float], rect: Rect
) -> bool:
    """Whether an axis-aligned segment passes through a rectangle's interior."""

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
    return False  # non-orthogonal segments are refused before they are tested


def _simplify(points: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """Drop duplicate and collinear points, so two equal routes are equal point for point."""

    unique: list[tuple[float, float]] = []
    for point in points:
        quantized = (_quantize(point[0]), _quantize(point[1]))
        if not unique or unique[-1] != quantized:
            unique.append(quantized)
    simplified: list[tuple[float, float]] = []
    for index, point in enumerate(unique):
        if index in (0, len(unique) - 1):
            simplified.append(point)
            continue
        previous, following = unique[index - 1], unique[index + 1]
        same_x = previous[0] == point[0] == following[0]
        same_y = previous[1] == point[1] == following[1]
        if not (same_x or same_y):
            simplified.append(point)
    return tuple(simplified)


def _placement_decomposition(plan: SemanticLayoutPlan) -> dict[str, tuple[int, int, str]]:
    """``node_id -> (rank, lane, system_id)`` for every node the plan places."""

    system_of = {
        node_id: system_id
        for system_id, node_ids in plan.node_ids_by_system
        for node_id in node_ids
    }
    rows: dict[str, tuple[int, int, str]] = {}
    for node_id, _kind in plan.node_kinds:
        system_id = system_of.get(node_id, "")
        rows[node_id] = (0, 0, system_id)
    for system_id, node_ids in plan.node_ids_by_system:
        intra = tuple(
            (source, target)
            for source, target in plan.flow_edges
            if system_of.get(source) == system_id and system_of.get(target) == system_id
        )
        for node_id, rank, lane in system_rank_lanes(tuple(node_ids), intra):
            rows[node_id] = (rank, lane, system_id)
    return rows


def overlap_problems(rows: Sequence[dict[str, Any]]) -> list[str]:
    """Whether two nodes now share space -- the failure real geometry can introduce.

    Step 2 spaced origins by the density policy for *assumed* sizes, and that separation is
    inherited here rather than re-derived: re-checking it with the real sizes would report a
    shortfall on every drawing, since origin spacing and clear spacing are different quantities.
    What real geometry genuinely adds is the possibility that two nodes now overlap, and that is
    what this answers.
    """

    problems: list[str] = []
    rects = _rects_from_rows(rows)
    ids = sorted(rects)
    for index, first in enumerate(ids):
        for second in ids[index + 1 :]:
            if rects[first].crosses(rects[second]):
                problems.append(f"nodes {first!r} and {second!r} overlap")
    return problems


def _axis_gap(first: Rect, second: Rect, axis: str) -> float:
    """The signed gap between two boxes along one axis (negative when they overlap on it)."""

    if axis == "x":
        return max(first.x, second.x) - min(first.right, second.right)
    return max(first.y, second.y) - min(first.bottom, second.bottom)


def _envelope(boxes: Sequence[Rect]) -> Rect:
    x = min(box.x for box in boxes)
    y = min(box.y for box in boxes)
    return Rect(
        x,
        y,
        max(box.right for box in boxes) - x,
        max(box.bottom for box in boxes) - y,
    )


def placement_problems(
    rows: Sequence[dict[str, Any]],
    plan: SemanticLayoutPlan,
    clearance: MaterializedClearance | None = None,
) -> list[str]:
    """The full check: no overlap, and the *materialized clearance* between neighbours.

    Used to verify a *reflowed* placement. The rule is the separately named clearance policy --
    `minimum_node_clearance` / `minimum_system_clearance` -- and not the density class's
    `rank_gap` / `node_gap`: those space origins on the step-2 lattice, and asking for them here
    would redefine a rule this project has already signed off on. A placement that has not been
    reflowed is only required not to overlap, because that is the question real geometry asks of
    an inherited solution.
    """

    problems = overlap_problems(rows)
    policy = clearance if clearance is not None else materialized_clearance(plan.intent.density)
    rects = _rects_from_rows(rows)
    ids = sorted(rects)
    decomposition = _placement_decomposition(plan)
    horizontal = plan.intent.primary_flow_direction == "left_to_right"
    for index, first in enumerate(ids):
        for second in ids[index + 1 :]:
            first_rank, first_lane, first_system = decomposition[first]
            second_rank, second_lane, second_system = decomposition[second]
            if first_system != second_system:
                continue
            a, b = rects[first], rects[second]
            if first_lane == second_lane and abs(first_rank - second_rank) == 1:
                gap = _axis_gap(a, b, "x" if horizontal else "y")
                if gap < policy.minimum_node_clearance - 1e-9:
                    problems.append(
                        f"rank clearance between {first!r} and {second!r} is {gap:g}, below the "
                        f"materialized node clearance {policy.minimum_node_clearance:g}"
                    )
            if first_rank == second_rank and abs(first_lane - second_lane) == 1:
                gap = _axis_gap(a, b, "y" if horizontal else "x")
                if gap < policy.minimum_node_clearance - 1e-9:
                    problems.append(
                        f"lane clearance between {first!r} and {second!r} is {gap:g}, below the "
                        f"materialized node clearance {policy.minimum_node_clearance:g}"
                    )

    # Different systems are separated by the grouping class: `flat` chains them along the flow
    # axis, a grouped layout stacks them across it. Whichever axis does the separating is the one
    # the system clearance is measured on -- measuring the other would compare two boxes that are
    # deliberately side by side.
    boxes_by_system: dict[str, list[Rect]] = {}
    for node_id, box in rects.items():
        boxes_by_system.setdefault(decomposition[node_id][2], []).append(box)
    if len(boxes_by_system) > 1:
        axis = "x" if (plan.intent.grouping == "flat") == horizontal else "y"
        envelopes = {name: _envelope(boxes) for name, boxes in boxes_by_system.items()}
        names = sorted(envelopes)
        for index, first in enumerate(names):
            for second in names[index + 1 :]:
                gap = _axis_gap(envelopes[first], envelopes[second], axis)
                if gap < policy.minimum_system_clearance - 1e-9:
                    problems.append(
                        f"system clearance between {first!r} and {second!r} is {gap:g}, below the "
                        f"materialized system clearance {policy.minimum_system_clearance:g}"
                    )
    return problems


def _real_sizes(
    plan: SemanticLayoutPlan, snapshot: SymbolGeometrySnapshot
) -> tuple[dict[str, Rect], dict[str, SymbolGeometryFact]]:
    """The instance size of every placed node, from its frozen symbol geometry."""

    facts: dict[str, SymbolGeometryFact] = {}
    sizes: dict[str, Rect] = {}
    placed_by_id = {row["engineering_id"]: row for row in plan.placement}
    for node_id, kind in plan.node_kinds:
        node = plan_node(plan, node_id, kind)
        fact = require_node_symbol(node, snapshot)
        facts[node_id] = fact
        placed = placed_by_id[node_id]
        sizes[node_id] = Rect(
            placed["x"],
            placed["y"],
            _quantize(fact.intrinsic_width * INSTANCE_SCALE_FACTOR),
            _quantize(fact.intrinsic_height * INSTANCE_SCALE_FACTOR),
        )
    return sizes, facts


def _place_with_real_sizes(
    plan: SemanticLayoutPlan, sizes: Mapping[str, Rect], policy: SpacingPolicy
) -> list[dict[str, Any]]:
    """Step 2's algorithm, asked to leave room for the sizes the symbols really have.

    A rank is as wide as its widest node and a lane as tall as its tallest, so the declared gaps
    are gaps between *nodes* rather than between origins. That is the whole difference between
    the initial solution and the reflow, and it is why the reflow is a recomputation rather than
    a nudge.
    """

    kinds = dict(plan.node_kinds)
    system_of = {
        node_id: system_id
        for system_id, node_ids in plan.node_ids_by_system
        for node_id in node_ids
    }
    horizontal = plan.intent.primary_flow_direction == "left_to_right"
    honoured_grouping = plan.intent.grouping

    rows: list[dict[str, Any]] = []
    cursor = 0.0
    for system_id in plan.systems_in_reading_order:
        node_ids = next(
            (nodes for candidate, nodes in plan.node_ids_by_system if candidate == system_id),
            (),
        )
        if not node_ids:
            continue
        intra = tuple(
            (source, target)
            for source, target in plan.flow_edges
            if system_of.get(source) == system_id and system_of.get(target) == system_id
        )
        decomposition = system_rank_lanes(tuple(node_ids), intra)
        rank_flow: dict[int, float] = {}
        lane_cross: dict[int, float] = {}
        flow_cursor = 0.0
        per_rank: dict[int, float] = {}
        for node_id, rank, _lane in decomposition:
            flow_extent = sizes[node_id].width if horizontal else sizes[node_id].height
            per_rank[rank] = max(per_rank.get(rank, 0.0), flow_extent)
        for rank in sorted(per_rank):
            rank_flow[rank] = _quantize(flow_cursor)
            flow_cursor += per_rank[rank] + policy.rank_gap
        per_lane: dict[int, float] = {}
        for node_id, _rank, lane in decomposition:
            cross_extent = sizes[node_id].height if horizontal else sizes[node_id].width
            per_lane[lane] = max(per_lane.get(lane, 0.0), cross_extent)
        cross_cursor = 0.0
        for lane in sorted(per_lane):
            lane_cross[lane] = _quantize(cross_cursor)
            cross_cursor += per_lane[lane] + policy.node_gap
        system_flow_extent = flow_cursor - policy.rank_gap if per_rank else 0.0
        system_cross_extent = cross_cursor - policy.node_gap if per_lane else 0.0
        if honoured_grouping == "flat":
            block_flow, block_cross = cursor, 0.0
            advance = system_flow_extent + policy.component_gap
        else:
            block_flow, block_cross = 0.0, cursor
            advance = system_cross_extent + policy.system_gap
        for node_id, rank, lane in decomposition:
            flow = block_flow + rank_flow[rank]
            cross = block_cross + lane_cross[lane]
            x, y = (flow, cross) if horizontal else (cross, flow)
            size = sizes[node_id]
            rows.append(
                {
                    "placement_kind": kinds[node_id],
                    "engineering_id": node_id,
                    "x": _quantize(x),
                    "y": _quantize(y),
                    "width": size.width,
                    "height": size.height,
                }
            )
        cursor += advance
    rows.sort(key=lambda row: (row["placement_kind"], row["engineering_id"]))
    return rows


def _with_real_sizes(
    rows: Sequence[dict[str, Any]], sizes: Mapping[str, Rect]
) -> list[dict[str, Any]]:
    """The step-2 coordinates, carrying the real sizes: what the initial solution actually is."""

    resized: list[dict[str, Any]] = []
    for row in rows:
        size = sizes[row["engineering_id"]]
        resized.append(
            {
                "placement_kind": row["placement_kind"],
                "engineering_id": row["engineering_id"],
                "x": row["x"],
                "y": row["y"],
                "width": size.width,
                "height": size.height,
            }
        )
    return resized


def materialize_semantic_layout(
    plan: SemanticLayoutPlan, snapshot: SymbolGeometrySnapshot
) -> SemanticLayoutPlan:
    """Step 3a: bind every node to frozen geometry, then keep or reflow the placement.

    The step-2 result is treated as an initial solution rather than a frame the real sizes must
    fit into: it is kept whenever the real sizes still do not overlap, and recomputed when they
    do. Either way the result is verified -- an inherited placement against overlap, a reflowed
    one against the materialized clearance policy -- and ``placement_reflowed`` records which of
    the two happened, because a drawing that moved silently is a drawing nobody can explain.
    """

    if plan.produced_at_step != STEP_2:
        raise StepThreeError(
            f"step 3 materializes the step-2 plan, not one produced at {plan.produced_at_step!r}: "
            "materializing an unplaced plan would place it while claiming to refine a placement"
        )
    policy = plan.spacing
    if policy is None:
        raise StepThreeError("the step-2 plan carries no spacing policy, so nothing can be checked")

    sizes, _facts = _real_sizes(plan, snapshot)
    initial = _with_real_sizes(plan.placement, sizes)
    problems = overlap_problems(initial)
    reflowed = False
    if problems:
        reflowed = True
        rows = _place_with_real_sizes(plan, sizes, policy)
        remaining = placement_problems(rows, plan, materialized_clearance(plan.intent.density))
        if remaining:
            raise PlacementReflowFailedError(
                "the deterministic reflow still leaves the drawing invalid: "
                + "; ".join(remaining)
            )
    else:
        rows = initial

    for row in rows:
        if row["width"] <= 0 or row["height"] <= 0:
            raise PlacementReflowFailedError(
                f"node {row['engineering_id']!r} materialized a non-positive size "
                f"({row['width']}, {row['height']}): a node with no area cannot be drawn"
            )
        for coordinate in (row["x"], row["y"], row["width"], row["height"]):
            if coordinate != coordinate or coordinate in (float("inf"), float("-inf")):
                raise PlacementReflowFailedError(
                    f"node {row['engineering_id']!r} materialized a non-finite coordinate"
                )
            if round(coordinate, LAYOUT_COORDINATE_DECIMALS) != coordinate:
                raise PlacementReflowFailedError(
                    f"node {row['engineering_id']!r} carries {coordinate!r}, which is finer than "
                    "the declared coordinate quantum"
                )

    nodes = tuple(plan_node(plan, node_id, kind) for node_id, kind in plan.node_kinds)
    bindings = resolve_endpoint_bindings(
        connections=plan.connections, nodes=nodes, snapshot=snapshot
    )
    return replace(
        plan,
        placement=tuple(rows),
        produced_at_step=STEP_3,
        symbol_geometry_catalog_digest=snapshot.digest,
        symbol_geometry_closure=snapshot.closure,
        placement_reflowed=reflowed,
        endpoint_bindings=bindings,
    )


def _instance_anchor(rect: Rect, fact: SymbolGeometryFact, port_id: str) -> tuple[float, float]:
    port = fact.port(port_id)
    if port is None:  # pragma: no cover - the binding step already proved it exists
        raise UnroutableEdgeError(
            f"symbol {fact.symbol_key!r} does not define port {port_id!r}",
        )
    return (_quantize(rect.x + port.normalized_x * rect.width), _quantize(rect.y + port.normalized_y * rect.height))


def _outward_normal(
    rect: Rect, fact: SymbolGeometryFact, port_id: str
) -> tuple[float, float]:
    """Which way the route leaves the port, from the anchor's own side of the symbol.

    The side is read with the same rule the drafting layer uses, so "where does this port point"
    has one answer in this codebase rather than two.
    """

    anchor = _instance_anchor(rect, fact, port_id)
    side = definition_port_side(rect.width, rect.height, anchor[0] - rect.x, anchor[1] - rect.y)
    if side == "interior":
        # An anchor in the middle of a symbol has no side to leave through; for a route that
        # means "go towards the other end", which the candidate ordering then resolves.
        return (0.0, 0.0)
    return {
        "left": (-1.0, 0.0),
        "right": (1.0, 0.0),
        "top": (0.0, -1.0),
        "bottom": (0.0, 1.0),
    }[side]


def _route_candidates(
    start: tuple[float, float],
    end: tuple[float, float],
    start_stub: tuple[float, float],
    end_stub: tuple[float, float],
) -> tuple[tuple[tuple[float, float], ...], ...]:
    """The orthogonal shapes a route may take, in a fixed order.

    Manhattan with a bend in each direction covers the ordinary case, and the two corridor
    candidates cover the case where the straight alternatives are blocked. The order is part of
    the rule: the same plan must pick the same shape every time.
    """

    candidates = [
        (start, start_stub, (end_stub[0], start_stub[1]), end_stub, end),
        (start, start_stub, (start_stub[0], end_stub[1]), end_stub, end),
    ]
    mid_x = _quantize((start_stub[0] + end_stub[0]) / 2)
    mid_y = _quantize((start_stub[1] + end_stub[1]) / 2)
    candidates.append(
        (start, start_stub, (mid_x, start_stub[1]), (mid_x, end_stub[1]), end_stub, end)
    )
    candidates.append(
        (start, start_stub, (start_stub[0], mid_y), (end_stub[0], mid_y), end_stub, end)
    )
    return tuple(candidates)


def route_semantic_layout(
    plan: SemanticLayoutPlan, snapshot: SymbolGeometrySnapshot
) -> SemanticLayoutPlan:
    """Step 3b: one orthogonal route per semantic connection, around everything it does not serve.

    The router reads the resolved bindings, so it never guesses a port; the endpoints come from
    the frozen port anchors on the materialized instance. A connection with no collision-free
    orthogonal shape is a hard diagnostic: the alternative would be a drawing that looks
    complete while a pipe passes through a vessel.
    """

    if plan.produced_at_step != STEP_3:
        raise StepThreeError(
            f"routing takes the materialized plan, not one produced at {plan.produced_at_step!r}"
        )
    if not plan.endpoint_bindings:
        raise StepThreeError(
            "routing needs resolved endpoint bindings: without them it would have to guess ports"
        )
    _require_matching_snapshot(plan, snapshot)
    rects = _rects_from_rows(plan.placement)
    bindings = binding_index(plan.endpoint_bindings)
    facts_by_symbol = {fact.symbol_key: fact for fact in snapshot.facts}

    rows: list[dict[str, Any]] = []
    for connection in plan.connections:
        source_binding = bindings.get((connection.connection_id, "source"))
        target_binding = bindings.get((connection.connection_id, "target"))
        if source_binding is None or target_binding is None:
            raise StepThreeError(
                f"connection {connection.connection_id!r} is missing an endpoint binding: "
                "routing may not invent one"
            )
        source_rect = rects[source_binding.node_id]
        target_rect = rects[target_binding.node_id]
        source_fact = facts_by_symbol[source_binding.symbol_key]
        target_fact = facts_by_symbol[target_binding.symbol_key]
        start = _instance_anchor(source_rect, source_fact, source_binding.port_id)
        end = _instance_anchor(target_rect, target_fact, target_binding.port_id)
        source_normal = _outward_normal(source_rect, source_fact, source_binding.port_id)
        target_normal = _outward_normal(target_rect, target_fact, target_binding.port_id)
        start_stub = (
            _quantize(start[0] + source_normal[0] * ROUTE_STUB_LENGTH),
            _quantize(start[1] + source_normal[1] * ROUTE_STUB_LENGTH),
        )
        end_stub = (
            _quantize(end[0] + target_normal[0] * ROUTE_STUB_LENGTH),
            _quantize(end[1] + target_normal[1] * ROUTE_STUB_LENGTH),
        )
        served = {source_binding.node_id, target_binding.node_id}
        obstacles = [
            rect for node_id, rect in rects.items() if node_id not in served
        ]
        route = _first_clear_route(
            start,
            end,
            start_stub,
            end_stub,
            [*obstacles, *[rects[node_id] for node_id in sorted(served)]],
            served=[rects[node_id] for node_id in sorted(served)],
        )
        if route is None:
            raise UnroutableEdgeError(
                f"connection {connection.connection_id!r} cannot be routed orthogonally from "
                f"{source_binding.node_id!r}.{source_binding.port_id} to "
                f"{target_binding.node_id!r}.{target_binding.port_id} without crossing "
                f"{len(obstacles)} other node bounds"
            )
        rows.append(
            {
                "placement_kind": "routing",
                "engineering_id": connection.connection_id,
                "x": route[0][0],
                "y": route[0][1],
                "width": 0.0,
                "height": 0.0,
                "ordered_waypoints": [[x, y] for x, y in route[1:]],
            }
        )
    rows.sort(key=lambda row: (row["placement_kind"], row["engineering_id"]))
    if len(rows) != len(plan.connections):
        raise StepThreeError(
            f"routing produced {len(rows)} routes for {len(plan.connections)} connections: "
            "every semantic connection is routed exactly once"
        )
    return replace(plan, routing=tuple(rows))


def plan_node(plan: SemanticLayoutPlan, node_id: str, kind: str) -> _PlanNode:
    """The node facts the symbol binding needs, read off the plan rather than off a document."""

    return _PlanNode(
        kind=kind,
        engineering_id=node_id,
        symbol_key=dict(plan.node_symbols).get(node_id, ""),
    )


def _require_matching_snapshot(
    plan: SemanticLayoutPlan, snapshot: SymbolGeometrySnapshot
) -> None:
    """The snapshot a plan was materialized from, or a hard failure.

    Routing against a *different* snapshot would anchor routes on geometry the placement was
    never validated against, and the resulting drawing would carry a digest for a catalogue it
    did not use.
    """

    if not plan.symbol_geometry_catalog_digest:
        raise StepThreeError(
            "the plan was not materialized against any frozen symbol geometry, so there is nothing "
            "to anchor a route on"
        )
    if plan.symbol_geometry_catalog_digest != snapshot.digest:
        raise StepThreeError(
            "the plan was materialized against symbol geometry "
            f"{plan.symbol_geometry_catalog_digest[:12]} but routing was handed "
            f"{snapshot.digest[:12]}: the drawing would be anchored on a catalogue it was never "
            "validated against"
        )
    return None


def _first_clear_route(
    start: tuple[float, float],
    end: tuple[float, float],
    start_stub: tuple[float, float],
    end_stub: tuple[float, float],
    obstacles: Sequence[Rect],
    served: Sequence[Rect] = (),
) -> tuple[tuple[float, float], ...] | None:
    """The first candidate shape that clears everything it must, in the declared order.

    Two passes, because the rule and good draughtsmanship differ by exactly one allowance: the
    rule says a route may not cross a node it does not serve, so crossing its *own* node is
    permitted. A route that stays outside both ends is nevertheless the better drawing, so the
    candidates are first tried against every node and only then against the rule's own set. Both
    passes are deterministic; the second only makes reachable what the first could not shape
    nicely.
    """

    for candidate in _route_candidates(start, end, start_stub, end_stub):
        simple = _simplify(candidate)
        if ROUTING_IS_ORTHOGONAL_ONLY and not _axis_aligned(simple):
            continue
        if any(
            _segment_crosses_rect(a, b, obstacle)
            for a, b in zip(simple, simple[1:], strict=False)
            for obstacle in obstacles
        ):
            continue
        return simple
    for candidate in _route_candidates(start, end, start_stub, end_stub):
        simple = _simplify(candidate)
        if ROUTING_IS_ORTHOGONAL_ONLY and not _axis_aligned(simple):
            continue
        if any(
            _segment_crosses_rect(a, b, obstacle)
            for a, b in zip(simple, simple[1:], strict=False)
            for obstacle in obstacles
            if obstacle not in served
        ):
            continue
        return simple
    return None


def annotate_semantic_layout(
    plan: SemanticLayoutPlan, labels: Mapping[str, str]
) -> SemanticLayoutPlan:
    """Step 3c: place the label of every node that has one, deterministically.

    Labels are placed in the canonical node order and pushed straight down until they clear both
    the nodes and the labels already placed. The extent comes from the existing pure text rule, so
    the same label produces the same box on every machine -- and the label *text* stays in the
    document rather than in the layout projection: a layout digest identifies where things are,
    not what they say.
    """

    if plan.produced_at_step != STEP_3:
        raise StepThreeError(
            f"annotation placement takes the materialized plan, not one produced at "
            f"{plan.produced_at_step!r}"
        )
    rects = _rects_from_rows(plan.placement)
    obstacles = list(rects.values())
    placed: list[Rect] = []
    rows: list[dict[str, Any]] = []
    for row in sorted(plan.placement, key=lambda item: (item["placement_kind"], item["engineering_id"])):
        node_id = row["engineering_id"]
        text = str(labels.get(node_id, "") or "").strip()
        if not text:
            continue
        extent = text_bounds(
            TextElement(
                id=node_id,
                position=Point(x=0.0, y=0.0),
                text=text,
                font_size=ANNOTATION_FONT_SIZE,
            )
        )
        width = _quantize(extent.x2 - extent.x1)
        height = _quantize(extent.y2 - extent.y1)
        node = rects[node_id]
        x = _quantize(node.x + (node.width - width) / 2)
        y = _quantize(node.bottom + ANNOTATION_LABEL_GAP)
        candidate = Rect(x, y, width, height)
        attempts = 0
        while attempts < ANNOTATION_PLACEMENT_ATTEMPTS:
            blocked = any(candidate.crosses(obstacle) for obstacle in obstacles) or any(
                candidate.crosses(other.expanded(ANNOTATION_CLEARANCE)) for other in placed
            )
            if not blocked:
                break
            y = _quantize(y + height + ANNOTATION_CLEARANCE)
            candidate = Rect(x, y, width, height)
            attempts += 1
        else:
            raise AnnotationPlacementError(
                f"the label of node {node_id!r} found no clear space in "
                f"{ANNOTATION_PLACEMENT_ATTEMPTS} pushes: an unbounded search is not a placement "
                "rule"
            )
        placed.append(candidate)
        rows.append(
            {
                "placement_kind": "annotation",
                "engineering_id": node_id,
                "x": candidate.x,
                "y": candidate.y,
                "width": candidate.width,
                "height": candidate.height,
                "ordered_waypoints": [],
            }
        )
    rows.sort(key=lambda item: (item["placement_kind"], item["engineering_id"]))
    return replace(plan, annotations=tuple(rows))


class SemanticLayoutGeometry:
    """Step 3, mixed into the one layout authority for the same reason step 1 is.

    A separate placer or router beside the engine would be a second geometry authority; the
    engine already knows how to place things, and this gives it one more declared stage rather
    than a competitor.
    """

    def layout_semantic_geometry(
        self,
        plan: SemanticLayoutPlan,
        snapshot: SymbolGeometrySnapshot,
        labels: Mapping[str, str] | None = None,
    ) -> SemanticLayoutPlan:
        """Materialize, bind, route and annotate a step-2 plan."""

        materialized = materialize_semantic_layout(plan, snapshot)
        routed = route_semantic_layout(materialized, snapshot)
        return annotate_semantic_layout(routed, labels or {})
