from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import cos, radians, sin
from typing import Any, Literal

from .annotation_layout import measure_annotation_quality
from .diagram_quality_models import (
    DiagramQualityIssue,
    DiagramQualityMetrics,
    DiagramQualityReport,
)
from .models import ConnectorElement, Document, Point, SymbolElement
from .symbols import SymbolRegistry

EPSILON = 1e-6
DRAFTING_CONTRACT_VERSION = 2


@dataclass(frozen=True)
class _Rect:
    x1: float
    y1: float
    x2: float
    y2: float
    element_id: str

    def expanded(self, margin: float) -> _Rect:
        return _Rect(
            self.x1 - margin,
            self.y1 - margin,
            self.x2 + margin,
            self.y2 + margin,
            self.element_id,
        )


@dataclass(frozen=True)
class _Segment:
    first: Point
    second: Point
    connector_id: str = ""

    @property
    def horizontal(self) -> bool:
        return abs(self.first.y - self.second.y) <= EPSILON

    @property
    def vertical(self) -> bool:
        return abs(self.first.x - self.second.x) <= EPSILON

    @property
    def length(self) -> float:
        return abs(self.second.x - self.first.x) + abs(self.second.y - self.first.y)


def drafting_contract(document: Document) -> dict[str, Any]:
    grid = document.canvas.grid_size
    return {
        "schema": "pid-agent.drafting-contract",
        "version": DRAFTING_CONTRACT_VERSION,
        "canvas": {
            "width": document.canvas.width,
            "height": document.canvas.height,
            "grid_size": grid,
            "safe_margin": max(40.0, grid * 2),
        },
        "layout": {
            "reading_direction": "left_to_right",
            "workflow": [
                "resolve_process_topology_and_exact_symbol_types",
                "place_major_equipment_and_main_process_skeleton",
                "align_connected_port_coordinates",
                "add_secondary_process_and_utility_lanes",
                "add_instrument_branches_and_annotations",
                "run_drafting_quality_check",
            ],
            "main_process_priority": True,
            "uniform_density": True,
            "minimum_equipment_gap": max(80.0, grid * 4),
        },
        "routing": {
            "orthogonal_only": True,
            "prefer_automatic_routing": True,
            "omit_waypoints_when_ports_are_axis_aligned": True,
            "minimum_leg_length": grid,
            "maximum_preferred_bends": 3,
            "primary_lines_are_direct_and_uninterrupted": True,
            "secondary_or_later_line_uses_jump_at_geometric_crossing": True,
            "crossing_is_not_a_connection_without_junction": True,
        },
        "ports_and_flow": {
            "connect_out_or_bidirectional_to_in_or_bidirectional": True,
            "pipe_must_leave_source_through_port_outward_side": True,
            "pipe_must_approach_target_from_port_outward_side": True,
            "align_real_port_points_not_symbol_top_left_coordinates": True,
            "flow_direction_must_match_source_target_semantics": True,
        },
        "acceptance": {
            "blocking_issue_codes": [
                "NON_ORTHOGONAL_SEGMENT",
                "MICRO_SEGMENT",
                "UNNECESSARY_BEND",
                "NODE_OVERLAP",
                "PIPE_THROUGH_EQUIPMENT",
                "UNBRIDGED_CROSSING",
                "PORT_DIRECTION_MISMATCH",
                "PORT_EXIT_MISMATCH",
                "PORT_FACING_MISMATCH",
                "CONNECTOR_OUT_OF_BOUNDS",
                "DUPLICATE_LABEL",
                "ANNOTATION_OVERLAP",
                "QUALITY_SCORE_BELOW_TARGET",
            ],
            "target_score": 95,
        },
    }


def drafting_prompt_contract() -> str:
    return """P&ID DRAFTING CONTRACT (hard requirements, not suggestions)
1. Think in this order: process topology → exact equipment/valve type → main-flow skeleton → secondary/utility lanes → instruments → annotations. Never start by scattering symbols.
2. Read the process from left to right. Put feed/IN boundaries on the left and product/OUT boundaries on the right. Keep the primary process stream visually dominant, direct and uninterrupted.
3. Select the exact catalog symbol named by the user. Never substitute a generic ball valve for a gate, globe, check, control, butterfly, needle, relief, or other specifically requested valve. A check valve must follow its allowed inlet-to-outlet direction.
4. Connect real ports with connect_ports. For forward flow, source must be an out/bidirectional port and target must be an in/bidirectional port; reverse flow reverses that rule. Do not swap inlet/outlet just to make coordinates convenient.
5. Align real port coordinates, not symbol position.y values. For a horizontal train, calculate each top-left position so the connected outlet and inlet have exactly the same y coordinate.
6. Prefer automatic orthogonal routing and omit waypoints when ports already share x or y. Waypoints are exceptional hints for genuine obstacles or reserved lanes; never add pixel-sized jogs, decorative bends, diagonal segments, or a detour that leaves a port on the wrong side.
7. A pipe must leave the source in the outward direction of its port and approach the target from outside that target port. Use the port on the side facing the intended connection. If a pump discharge is on top, leave vertically before entering the pipe highway.
8. A geometric crossing is not a connection. Use a junction only for an intentional branch/merge. At an unavoidable crossing, keep the earlier/primary line straight and set crossing_style='jump' on the later/secondary line.
9. Reserve whitespace before adding details. Avoid symbol overlap, pipes through equipment, crowded labels, duplicate tags and text placed on top of lines. Keep equipment spacing and visual density consistent.
10. Keep instrument impulse branches free of process-flow arrows unless the user explicitly requests them. Keep equipment tag and equipment name as separate annotations without repeating the tag inside the name.
11. Before returning JSON, audit every connector: correct symbol type, correct in/out ports, declared flow direction, axis-aligned segments, no needless bend, no sub-grid leg, no unbridged crossing, and no equipment intersection. Return JSON only."""


def definition_port_side(symbol_width: float, symbol_height: float, x: float, y: float) -> str:
    distances = {
        "left": abs(x),
        "right": abs(symbol_width - x),
        "top": abs(y),
        "bottom": abs(symbol_height - y),
    }
    side, distance = min(distances.items(), key=lambda item: (item[1], item[0]))
    tolerance = max(1.0, min(symbol_width, symbol_height) * 0.08)
    return side if distance <= tolerance else "interior"


def port_outward_normal(
    element: SymbolElement,
    port_id: str,
    registry: SymbolRegistry,
) -> tuple[int, int] | None:
    try:
        definition = registry.get(element.symbol_key)
    except KeyError:
        return None
    port = next((item for item in definition.ports if item.id == port_id), None)
    if port is None:
        return None
    side = definition_port_side(definition.width, definition.height, port.x, port.y)
    base = {
        "left": (-1.0, 0.0),
        "right": (1.0, 0.0),
        "top": (0.0, -1.0),
        "bottom": (0.0, 1.0),
    }.get(side)
    if base is None:
        return None
    angle = radians(element.rotation)
    rotated_x = base[0] * cos(angle) - base[1] * sin(angle)
    rotated_y = base[0] * sin(angle) + base[1] * cos(angle)
    if abs(rotated_x) >= abs(rotated_y):
        return (1 if rotated_x >= 0 else -1, 0)
    return (0, 1 if rotated_y >= 0 else -1)


def _element_rect(element) -> _Rect | None:
    if element.type == "symbol":
        angle = element.rotation % 360
        if angle <= EPSILON or abs(angle - 180) <= EPSILON:
            return _Rect(
                element.position.x,
                element.position.y,
                element.position.x + element.width,
                element.position.y + element.height,
                element.id,
            )
        theta = radians(angle)
        center_x = element.position.x + element.width / 2
        center_y = element.position.y + element.height / 2
        half_w = element.width / 2
        half_h = element.height / 2
        xs: list[float] = []
        ys: list[float] = []
        for px, py in ((half_w, half_h), (-half_w, half_h), (half_w, -half_h), (-half_w, -half_h)):
            xs.append(center_x + px * cos(theta) - py * sin(theta))
            ys.append(center_y + px * sin(theta) + py * cos(theta))
        return _Rect(min(xs), min(ys), max(xs), max(ys), element.id)
    if element.type == "junction":
        return _Rect(
            element.position.x - element.radius,
            element.position.y - element.radius,
            element.position.x + element.radius,
            element.position.y + element.radius,
            element.id,
        )
    return None


def _same_point(first: Point, second: Point) -> bool:
    return abs(first.x - second.x) <= EPSILON and abs(first.y - second.y) <= EPSILON


def _simplify(points: list[Point]) -> list[Point]:
    deduped: list[Point] = []
    for point in points:
        if not deduped or not _same_point(deduped[-1], point):
            deduped.append(Point.model_validate(point.model_dump(mode="python")))
    if len(deduped) < 3:
        return deduped
    result: list[Point] = [deduped[0]]
    for index in range(1, len(deduped) - 1):
        previous = result[-1]
        current = deduped[index]
        following = deduped[index + 1]
        if (
            abs(previous.x - current.x) <= EPSILON
            and abs(current.x - following.x) <= EPSILON
        ) or (
            abs(previous.y - current.y) <= EPSILON
            and abs(current.y - following.y) <= EPSILON
        ):
            continue
        result.append(current)
    result.append(deduped[-1])
    return result


def _segments(points: list[Point], connector_id: str = "") -> list[_Segment]:
    return [
        _Segment(first, second, connector_id)
        for first, second in zip(points, points[1:], strict=False)
        if not _same_point(first, second)
    ]


def _interval_overlap(a1: float, a2: float, b1: float, b2: float) -> float:
    return max(0.0, min(max(a1, a2), max(b1, b2)) - max(min(a1, a2), min(b1, b2)))


def _segment_intersects_rect(segment: _Segment, rect: _Rect) -> bool:
    if segment.horizontal:
        if not rect.y1 + EPSILON < segment.first.y < rect.y2 - EPSILON:
            return False
        return _interval_overlap(segment.first.x, segment.second.x, rect.x1, rect.x2) > EPSILON
    if segment.vertical:
        if not rect.x1 + EPSILON < segment.first.x < rect.x2 - EPSILON:
            return False
        return _interval_overlap(segment.first.y, segment.second.y, rect.y1, rect.y2) > EPSILON
    # 非正交段：Liang-Barsky 线段-矩形裁剪。要求相交参数区间有正长度，
    # 与水平/垂直段的“严格穿过”语义一致（端点贴边不算穿过）。
    dx = segment.second.x - segment.first.x
    dy = segment.second.y - segment.first.y
    p = (-dx, dx, -dy, dy)
    q = (
        segment.first.x - rect.x1,
        rect.x2 - segment.first.x,
        segment.first.y - rect.y1,
        rect.y2 - segment.first.y,
    )
    t0 = 0.0
    t1 = 1.0
    for pi, qi in zip(p, q, strict=True):
        if abs(pi) <= EPSILON:
            if qi < -EPSILON:
                return False
            continue
        ratio = qi / pi
        if pi < 0:
            if ratio > t1:
                return False
            if ratio > t0:
                t0 = ratio
        else:
            if ratio < t0:
                return False
            if ratio < t1:
                t1 = ratio
    return t1 - t0 > EPSILON


def _rects_overlap(first: _Rect, second: _Rect) -> bool:
    return not (
        first.x2 <= second.x1 + EPSILON
        or second.x2 <= first.x1 + EPSILON
        or first.y2 <= second.y1 + EPSILON
        or second.y2 <= first.y1 + EPSILON
    )


def _crossing(first: _Segment, second: _Segment) -> Point | None:
    if first.horizontal and second.vertical:
        horizontal, vertical = first, second
    elif first.vertical and second.horizontal:
        horizontal, vertical = second, first
    else:
        return None
    x = vertical.first.x
    y = horizontal.first.y
    if not (
        min(horizontal.first.x, horizontal.second.x) + EPSILON
        < x
        < max(horizontal.first.x, horizontal.second.x) - EPSILON
        and min(vertical.first.y, vertical.second.y) + EPSILON
        < y
        < max(vertical.first.y, vertical.second.y) - EPSILON
    ):
        return None
    return Point(x=x, y=y)


def _shares_bound_endpoint(first: ConnectorElement, second: ConnectorElement) -> bool:
    first_ids = {
        endpoint.element_id
        for endpoint in (first.source, first.target)
        if endpoint is not None and endpoint.element_id is not None
    }
    second_ids = {
        endpoint.element_id
        for endpoint in (second.source, second.target)
        if endpoint is not None and endpoint.element_id is not None
    }
    return bool(first_ids & second_ids)


def _bisect_first(items: list[tuple[float, float, float]], value: float) -> int:
    low = 0
    high = len(items)
    while low < high:
        mid = (low + high) // 2
        if items[mid][0] < value:
            low = mid + 1
        else:
            high = mid
    return low


def _build_segment_indexes(
    connector: ConnectorElement,
    existing: list[ConnectorElement],
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """把既有连接段建成按主轴排序的一维索引，供交叉计数快速过滤。

    返回 (vertical_index, horizontal_index)，每项为 (主轴坐标, 区间起, 区间止)。
    与 connector 共享端点元素的连接不参与计数（语义与 _crossing 调用处的
    _shares_bound_endpoint 过滤一致）。
    """
    shared_ids = {
        endpoint.element_id
        for endpoint in (connector.source, connector.target)
        if endpoint is not None and endpoint.element_id is not None
    }
    vertical_index: list[tuple[float, float, float]] = []
    horizontal_index: list[tuple[float, float, float]] = []
    for other in existing:
        other_ids = {
            endpoint.element_id
            for endpoint in (other.source, other.target)
            if endpoint is not None and endpoint.element_id is not None
        }
        if shared_ids & other_ids:
            continue
        for segment in _segments(other.points):
            if segment.horizontal:
                horizontal_index.append(
                    (
                        segment.first.y,
                        min(segment.first.x, segment.second.x),
                        max(segment.first.x, segment.second.x),
                    )
                )
            elif segment.vertical:
                vertical_index.append(
                    (
                        segment.first.x,
                        min(segment.first.y, segment.second.y),
                        max(segment.first.y, segment.second.y),
                    )
                )
    vertical_index.sort(key=lambda item: item[0])
    horizontal_index.sort(key=lambda item: item[0])
    return vertical_index, horizontal_index


def _count_crossings(
    segments: list[_Segment],
    vertical_index: list[tuple[float, float, float]],
    horizontal_index: list[tuple[float, float, float]],
) -> int:
    """统计候选段与既有段严格内点交叉的次数（语义同 _crossing）。"""
    count = 0
    for segment in segments:
        if segment.horizontal:
            x1 = min(segment.first.x, segment.second.x) + EPSILON
            x2 = max(segment.first.x, segment.second.x) - EPSILON
            y = segment.first.y
            for x, y1, y2 in vertical_index[_bisect_first(vertical_index, x1):]:
                if x > x2:
                    break
                if y1 + EPSILON < y < y2 - EPSILON:
                    count += 1
        elif segment.vertical:
            y1 = min(segment.first.y, segment.second.y) + EPSILON
            y2 = max(segment.first.y, segment.second.y) - EPSILON
            x = segment.first.x
            for y, x1, x2 in horizontal_index[_bisect_first(horizontal_index, y1):]:
                if y > y2:
                    break
                if x1 + EPSILON < x < x2 - EPSILON:
                    count += 1
    return count


def _port_direction(
    document: Document,
    element_id: str | None,
    port_id: str | None,
    registry: SymbolRegistry,
    element_map: dict[str, Any] | None = None,
) -> str | None:
    if element_id is None or port_id is None:
        return None
    if element_map is None:
        element_map = {item.id: item for item in document.elements}
    element = element_map.get(element_id)
    if element is None or element.type != "symbol":
        return "bidirectional" if element is not None and element.type == "junction" else None
    try:
        definition = registry.get(element.symbol_key)
    except KeyError:
        return None
    port = next((item for item in definition.ports if item.id == port_id), None)
    return port.direction if port is not None else None


def infer_flow_direction(
    document: Document,
    connector: ConnectorElement,
    registry: SymbolRegistry,
    element_map: dict[str, Any] | None = None,
) -> Literal["forward", "reverse", "none"]:
    source_direction = _port_direction(
        document,
        connector.source.element_id if connector.source else None,
        connector.source.port_id if connector.source else None,
        registry,
        element_map=element_map,
    )
    target_direction = _port_direction(
        document,
        connector.target.element_id if connector.target else None,
        connector.target.port_id if connector.target else None,
        registry,
        element_map=element_map,
    )
    forward = source_direction in {"out", "bidirectional"} and target_direction in {
        "in",
        "bidirectional",
    }
    reverse = source_direction in {"in", "bidirectional"} and target_direction in {
        "out",
        "bidirectional",
    }
    if forward and not reverse:
        return "forward"
    if reverse and not forward:
        return "reverse"
    return "none"


def _middle_route_candidates(first: Point, second: Point, lanes_x: list[float], lanes_y: list[float]):
    if abs(first.x - second.x) <= EPSILON or abs(first.y - second.y) <= EPSILON:
        yield [first, second]
    yield [first, Point(x=second.x, y=first.y), second]
    yield [first, Point(x=first.x, y=second.y), second]
    for x in lanes_x:
        yield [first, Point(x=x, y=first.y), Point(x=x, y=second.y), second]
    for y in lanes_y:
        yield [first, Point(x=first.x, y=y), Point(x=second.x, y=y), second]


def _normal_mismatches(
    points: list[Point],
    source_normal: tuple[int, int] | None,
    target_normal: tuple[int, int] | None,
) -> int:
    segments = _segments(points)
    if not segments:
        return 0
    mismatches = 0
    if source_normal is not None:
        first = segments[0]
        dx = first.second.x - first.first.x
        dy = first.second.y - first.first.y
        if dx * source_normal[0] + dy * source_normal[1] <= EPSILON:
            mismatches += 1
    if target_normal is not None:
        last = segments[-1]
        dx = last.second.x - last.first.x
        dy = last.second.y - last.first.y
        if dx * target_normal[0] + dy * target_normal[1] >= -EPSILON:
            mismatches += 1
    return mismatches


def route_connector_points(
    document: Document,
    connector: ConnectorElement,
    registry: SymbolRegistry,
    avoid_rects: Iterable[tuple[float, float, float, float]] | None = None,
) -> list[Point]:
    """Route one connector, keeping it out of the given reserved rectangles.

    ``avoid_rects`` are declared areas a route must not intrude into — reserved
    drawing space such as a legend or title block (Charter §15). They are treated
    exactly like an element obstacle: they penalise a candidate's score and they
    contribute the lanes a detour can use, so a route is actually planned *around*
    them rather than merely rejected afterwards.
    """
    if connector.source is None or connector.target is None:
        return _simplify(connector.points)
    start = connector.source.point
    end = connector.target.point
    element_map = {element.id: element for element in document.elements}
    source_element = element_map.get(connector.source.element_id or "")
    target_element = element_map.get(connector.target.element_id or "")
    source_normal = (
        port_outward_normal(source_element, connector.source.port_id or "", registry)
        if source_element is not None and source_element.type == "symbol"
        else None
    )
    target_normal = (
        port_outward_normal(target_element, connector.target.port_id or "", registry)
        if target_element is not None and target_element.type == "symbol"
        else None
    )
    grid = max(5.0, document.canvas.grid_size)
    stub = max(20.0, grid)
    start_stub = (
        Point(x=start.x + source_normal[0] * stub, y=start.y + source_normal[1] * stub)
        if source_normal is not None
        else start
    )
    end_stub = (
        Point(x=end.x + target_normal[0] * stub, y=end.y + target_normal[1] * stub)
        if target_normal is not None
        else end
    )
    excluded = {
        connector.source.element_id,
        connector.target.element_id,
    }
    obstacles = [
        rect.expanded(max(8.0, grid / 2))
        for element in document.elements
        if element.id not in excluded and (rect := _element_rect(element)) is not None
    ]
    obstacles.extend(
        _Rect(float(x1), float(y1), float(x2), float(y2), "reserved").expanded(
            max(8.0, grid / 2)
        )
        for x1, y1, x2, y2 in (avoid_rects or ())
    )
    lane_margin = max(40.0, grid * 2)
    lanes_x = sorted(
        {
            (start_stub.x + end_stub.x) / 2,
            min(start_stub.x, end_stub.x) - lane_margin,
            max(start_stub.x, end_stub.x) + lane_margin,
            *(rect.x1 - lane_margin for rect in obstacles),
            *(rect.x2 + lane_margin for rect in obstacles),
        }
    )
    lanes_y = sorted(
        {
            (start_stub.y + end_stub.y) / 2,
            min(start_stub.y, end_stub.y) - lane_margin,
            max(start_stub.y, end_stub.y) + lane_margin,
            *(rect.y1 - lane_margin for rect in obstacles),
            *(rect.y2 + lane_margin for rect in obstacles),
        }
    )
    existing = [
        element
        for element in document.elements
        if element.type == "connector" and element.id != connector.id
    ]
    vertical_index, horizontal_index = _build_segment_indexes(connector, existing)
    candidates: list[list[Point]] = []
    if abs(start.x - end.x) <= EPSILON or abs(start.y - end.y) <= EPSILON:
        candidates.append([start, end])
    for middle in _middle_route_candidates(start_stub, end_stub, lanes_x, lanes_y):
        candidates.append(_simplify([start, start_stub, *middle[1:-1], end_stub, end]))

    unique: dict[tuple[tuple[float, float], ...], list[Point]] = {}
    for candidate in candidates:
        candidate = _simplify(candidate)
        if len(candidate) < 2:
            continue
        if any(not (segment.horizontal or segment.vertical) for segment in _segments(candidate)):
            continue
        key = tuple((round(point.x, 6), round(point.y, 6)) for point in candidate)
        unique[key] = candidate

    def score(points: list[Point]):
        segments = _segments(points)
        obstacle_hits = sum(
            1
            for segment in segments
            if any(_segment_intersects_rect(segment, rect) for rect in obstacles)
        )
        crossings = _count_crossings(segments, vertical_index, horizontal_index)
        micro = sum(0 < segment.length < grid - EPSILON for segment in segments)
        preferred_leg = max(40.0, grid * 2)
        cramped_leg_penalty = round(
            sum(max(0.0, preferred_leg - segment.length) for segment in segments),
            6,
        )
        bends = max(0, len(points) - 2)
        length = sum(segment.length for segment in segments)
        mismatch = _normal_mismatches(points, source_normal, target_normal)
        out_of_bounds = sum(
            point.x < -EPSILON
            or point.y < -EPSILON
            or point.x > document.canvas.width + EPSILON
            or point.y > document.canvas.height + EPSILON
            for point in points
        )
        return (
            mismatch,
            out_of_bounds,
            obstacle_hits,
            crossings,
            micro,
            cramped_leg_penalty,
            bends,
            round(length, 6),
            tuple((point.x, point.y) for point in points),
        )

    return min(unique.values(), key=score) if unique else _simplify(connector.points)


def connector_crosses_existing(
    document: Document,
    connector: ConnectorElement,
    connector_ids: set[str] | None = None,
) -> bool:
    for other in document.elements:
        if other.type != "connector" or other.id == connector.id:
            continue
        if connector_ids is not None and other.id not in connector_ids:
            continue
        if _shares_bound_endpoint(connector, other):
            continue
        if any(
            _crossing(first, second) is not None
            for first in _segments(connector.points)
            for second in _segments(other.points)
        ):
            return True
    return False


def analyze_diagram_quality(
    document: Document,
    registry: SymbolRegistry,
) -> DiagramQualityReport:
    issues: list[DiagramQualityIssue] = []
    symbols = [element for element in document.elements if element.type == "symbol"]
    connectors = [element for element in document.elements if element.type == "connector"]
    rects = [
        rect
        for element in document.elements
        if (rect := _element_rect(element)) is not None
    ]
    grid = max(5.0, document.canvas.grid_size)
    node_overlaps = 0
    crowded_pairs = 0
    for index, first in enumerate(rects):
        for second in rects[index + 1 :]:
            if _rects_overlap(first, second):
                node_overlaps += 1
                issues.append(
                    DiagramQualityIssue(
                        severity="error",
                        code="NODE_OVERLAP",
                        message=f"symbols/nodes overlap: {first.element_id}, {second.element_id}",
                        element_ids=[first.element_id, second.element_id],
                    )
                )
            elif _rects_overlap(first.expanded(grid), second.expanded(grid)):
                crowded_pairs += 1

    non_orthogonal = 0
    micro_segments = 0
    unnecessary_bends = 0
    excessive_bends = 0
    total_bends = 0
    obstacle_hits = 0
    port_direction_mismatches = 0
    port_exit_mismatches = 0
    port_facing_mismatches = 0
    backward_flow = 0
    out_of_bounds_connector_points = 0
    element_map = {element.id: element for element in document.elements}
    all_rects = {rect.element_id: rect for rect in rects}
    for connector in connectors:
        instrument_branch = connector.metadata.get("connection_role") == "instrument_branch"
        points = _simplify(connector.points)
        segments = _segments(points, connector.id)
        outside_count = sum(
            point.x < -EPSILON
            or point.y < -EPSILON
            or point.x > document.canvas.width + EPSILON
            or point.y > document.canvas.height + EPSILON
            for point in points
        )
        if outside_count:
            out_of_bounds_connector_points += outside_count
            issues.append(
                DiagramQualityIssue(
                    severity="error",
                    code="CONNECTOR_OUT_OF_BOUNDS",
                    message=f"connector {connector.id} leaves the drawing canvas",
                    element_ids=[connector.id],
                    details={"point_count": outside_count},
                )
            )
        bends = max(0, len(points) - 2)
        total_bends += bends
        diagonal_count = sum(not (segment.horizontal or segment.vertical) for segment in segments)
        if diagonal_count:
            non_orthogonal += diagonal_count
            issues.append(
                DiagramQualityIssue(
                    severity="error",
                    code="NON_ORTHOGONAL_SEGMENT",
                    message=f"connector {connector.id} contains a diagonal segment",
                    element_ids=[connector.id],
                    details={"segment_count": diagonal_count},
                )
            )
        short_count = sum(0 < segment.length < grid - EPSILON for segment in segments)
        if short_count:
            micro_segments += short_count
            issues.append(
                DiagramQualityIssue(
                    severity="error",
                    code="MICRO_SEGMENT",
                    message=f"connector {connector.id} contains sub-grid route legs",
                    element_ids=[connector.id],
                    details={"count": short_count, "minimum_leg_length": grid},
                )
            )
        direct_source_normal = None
        direct_target_normal = None
        if connector.source and connector.source.element_id and connector.source.port_id:
            source_element = element_map.get(connector.source.element_id)
            if source_element is not None and source_element.type == "symbol":
                direct_source_normal = port_outward_normal(
                    source_element,
                    connector.source.port_id,
                    registry,
                )
        if connector.target and connector.target.element_id and connector.target.port_id:
            target_element = element_map.get(connector.target.element_id)
            if target_element is not None and target_element.type == "symbol":
                direct_target_normal = port_outward_normal(
                    target_element,
                    connector.target.port_id,
                    registry,
                )
        direct_route_is_valid = (
            _normal_mismatches(
                [connector.points[0], connector.points[-1]],
                direct_source_normal,
                direct_target_normal,
            )
            == 0
        )
        direct_obstacles = [
            rect.expanded(4.0)
            for key, rect in all_rects.items()
            if key
            not in {
                connector.source.element_id if connector.source else None,
                connector.target.element_id if connector.target else None,
            }
        ]
        direct_route_is_clear = not any(
            _segment_intersects_rect(
                _Segment(connector.points[0], connector.points[-1], connector.id),
                rect,
            )
            for rect in direct_obstacles
        )
        if len(points) > 2 and (
            abs(points[0].x - points[-1].x) <= EPSILON
            or abs(points[0].y - points[-1].y) <= EPSILON
        ) and direct_route_is_valid and direct_route_is_clear:
            unnecessary_bends += 1
            issues.append(
                DiagramQualityIssue(
                    severity="error",
                    code="UNNECESSARY_BEND",
                    message=f"connector {connector.id} bends although its endpoints are aligned",
                    element_ids=[connector.id],
                )
            )
        if bends > 3:
            excessive_bends += 1
            issues.append(
                DiagramQualityIssue(
                    severity="warning",
                    code="EXCESSIVE_BENDS",
                    message=f"connector {connector.id} has {bends} bends",
                    element_ids=[connector.id],
                    details={"bend_count": bends},
                )
            )
        excluded = {
            connector.source.element_id if connector.source else None,
            connector.target.element_id if connector.target else None,
        }
        obstacles = [rect.expanded(4.0) for key, rect in all_rects.items() if key not in excluded]
        hits = sum(
            1
            for segment in segments
            if any(_segment_intersects_rect(segment, rect) for rect in obstacles)
        )
        if hits:
            obstacle_hits += hits
            issues.append(
                DiagramQualityIssue(
                    severity="error",
                    code="PIPE_THROUGH_EQUIPMENT",
                    message=f"connector {connector.id} passes through equipment or another node",
                    element_ids=[connector.id],
                    details={"intersection_count": hits},
                )
            )
        source_direction = _port_direction(
            document,
            connector.source.element_id if connector.source else None,
            connector.source.port_id if connector.source else None,
            registry,
            element_map=element_map,
        )
        target_direction = _port_direction(
            document,
            connector.target.element_id if connector.target else None,
            connector.target.port_id if connector.target else None,
            registry,
            element_map=element_map,
        )
        inferred = infer_flow_direction(document, connector, registry, element_map=element_map)
        incompatible_ports = (
            source_direction in {"in", "out"}
            and target_direction in {"in", "out"}
            and inferred == "none"
        )
        if (incompatible_ports and not instrument_branch) or (
            connector.flow_direction != "none"
            and inferred != "none"
            and connector.flow_direction != inferred
        ):
            port_direction_mismatches += 1
            issues.append(
                DiagramQualityIssue(
                    severity="error",
                    code="PORT_DIRECTION_MISMATCH",
                    message=(
                        f"connector {connector.id} flow {connector.flow_direction} contradicts "
                        f"port directions {source_direction}→{target_direction}"
                    ),
                    element_ids=[connector.id],
                    details={"inferred_flow_direction": inferred},
                )
            )
        source_normal = None
        target_normal = None
        if connector.source and connector.source.element_id and connector.source.port_id:
            source_element = element_map.get(connector.source.element_id)
            if source_element is not None and source_element.type == "symbol":
                source_normal = port_outward_normal(source_element, connector.source.port_id, registry)
        if connector.target and connector.target.element_id and connector.target.port_id:
            target_element = element_map.get(connector.target.element_id)
            if target_element is not None and target_element.type == "symbol":
                target_normal = port_outward_normal(target_element, connector.target.port_id, registry)
        exit_count = _normal_mismatches(points, source_normal, target_normal)
        if exit_count:
            port_exit_mismatches += exit_count
            issues.append(
                DiagramQualityIssue(
                    severity="error",
                    code="PORT_EXIT_MISMATCH",
                    message=f"connector {connector.id} leaves or approaches a port from the wrong side",
                    element_ids=[connector.id],
                    details={"endpoint_count": exit_count},
                )
            )
        dx = connector.points[-1].x - connector.points[0].x
        dy = connector.points[-1].y - connector.points[0].y
        facing = 0
        if abs(dx) >= abs(dy) * 1.5 and abs(dx) >= grid * 2:
            direction_x = 1 if dx > 0 else -1
            if source_normal is not None and source_normal[1] == 0 and source_normal[0] != direction_x:
                facing += 1
            if target_normal is not None and target_normal[1] == 0 and target_normal[0] != -direction_x:
                facing += 1
        if facing and not instrument_branch:
            port_facing_mismatches += facing
            issues.append(
                DiagramQualityIssue(
                    severity="error",
                    code="PORT_FACING_MISMATCH",
                    message=f"connector {connector.id} uses a horizontal port facing away from the other endpoint",
                    element_ids=[connector.id],
                    details={"endpoint_count": facing},
                )
            )
        normalized_medium = connector.medium.strip().casefold().replace("-", "_")
        utility_or_relief = any(
            token in normalized_medium
            for token in (
                "cooling",
                "utility",
                "instrument_air",
                "steam",
                "drain",
                "vent",
                "relief",
            )
        )
        if (
            connector.flow_direction in {"forward", "reverse"}
            and not utility_or_relief
            and not instrument_branch
        ):
            upstream = connector.source if connector.flow_direction == "forward" else connector.target
            downstream = connector.target if connector.flow_direction == "forward" else connector.source
            if upstream and downstream and upstream.element_id and downstream.element_id:
                upstream_element = element_map.get(upstream.element_id)
                downstream_element = element_map.get(downstream.element_id)
                if (
                    upstream_element is not None
                    and downstream_element is not None
                    and upstream_element.type == "symbol"
                    and downstream_element.type == "symbol"
                    and downstream.point.x < upstream.point.x - grid * 2
                ):
                    backward_flow += 1

    geometric_crossings = 0
    unbridged_crossings = 0
    seen_crossings: set[tuple[str, str, float, float]] = set()
    for index, first in enumerate(connectors):
        for second in connectors[index + 1 :]:
            if _shares_bound_endpoint(first, second):
                continue
            for first_segment in _segments(first.points, first.id):
                for second_segment in _segments(second.points, second.id):
                    point = _crossing(first_segment, second_segment)
                    if point is None:
                        continue
                    key = (
                        min(first.id, second.id),
                        max(first.id, second.id),
                        round(point.x, 6),
                        round(point.y, 6),
                    )
                    if key in seen_crossings:
                        continue
                    seen_crossings.add(key)
                    geometric_crossings += 1
                    if first.crossing_style != "jump" and second.crossing_style != "jump":
                        unbridged_crossings += 1
                        issues.append(
                            DiagramQualityIssue(
                                severity="error",
                                code="UNBRIDGED_CROSSING",
                                message=f"connectors {first.id} and {second.id} cross without a jump bridge",
                                element_ids=[first.id, second.id],
                                details={"point": point.model_dump(mode="json")},
                            )
                        )

    out_of_bounds = sum(
        rect.x1 < -EPSILON
        or rect.y1 < -EPSILON
        or rect.x2 > document.canvas.width + EPSILON
        or rect.y2 > document.canvas.height + EPSILON
        for rect in rects
        if element_map[rect.element_id].type == "symbol"
    )
    if out_of_bounds:
        issues.append(
            DiagramQualityIssue(
                severity="warning",
                code="SYMBOL_OUT_OF_BOUNDS",
                message=f"{out_of_bounds} symbol(s) extend outside the canvas",
                details={"count": out_of_bounds},
            )
        )
    annotations = measure_annotation_quality(document, registry)
    if annotations.duplicate_label_count:
        issues.append(
            DiagramQualityIssue(
                severity="error",
                code="DUPLICATE_LABEL",
                message="duplicate visible engineering labels remain after layout",
                details={"count": annotations.duplicate_label_count},
            )
        )
    annotation_overlap_count = (
        annotations.text_text_overlaps
        + annotations.text_symbol_overlaps
        + annotations.text_connector_intersections
    )
    if annotation_overlap_count:
        issues.append(
            DiagramQualityIssue(
                severity="error",
                code="ANNOTATION_OVERLAP",
                message="annotation text overlaps another label, symbol, or connector",
                details={
                    "text_text": annotations.text_text_overlaps,
                    "text_symbol": annotations.text_symbol_overlaps,
                    "text_connector": annotations.text_connector_intersections,
                },
            )
        )

    metrics = DiagramQualityMetrics(
        symbol_count=len(symbols),
        connector_count=len(connectors),
        total_bends=total_bends,
        non_orthogonal_segments=non_orthogonal,
        micro_segments=micro_segments,
        unnecessary_bends=unnecessary_bends,
        excessive_bend_connectors=excessive_bends,
        node_overlaps=node_overlaps,
        crowded_node_pairs=crowded_pairs,
        pipe_obstacle_intersections=obstacle_hits,
        geometric_crossings=geometric_crossings,
        unbridged_crossings=unbridged_crossings,
        port_direction_mismatches=port_direction_mismatches,
        port_exit_mismatches=port_exit_mismatches,
        port_facing_mismatches=port_facing_mismatches,
        backward_flow_connectors=backward_flow,
        out_of_bounds_symbols=out_of_bounds,
        out_of_bounds_connector_points=out_of_bounds_connector_points,
        duplicate_label_count=annotations.duplicate_label_count,
        text_text_overlaps=annotations.text_text_overlaps,
        text_symbol_overlaps=annotations.text_symbol_overlaps,
        text_connector_intersections=annotations.text_connector_intersections,
    )
    penalty = (
        non_orthogonal * 15
        + micro_segments * 5
        + unnecessary_bends * 5
        + excessive_bends * 0.25
        + node_overlaps * 12
        + obstacle_hits * 10
        + unbridged_crossings * 8
        + port_direction_mismatches * 15
        + port_exit_mismatches * 8
        + port_facing_mismatches * 8
        + backward_flow * 0.25
        + out_of_bounds * 10
        + out_of_bounds_connector_points * 10
        + annotations.duplicate_label_count * 8
        + annotation_overlap_count * 6
        + min(crowded_pairs, 10) * 0.25
    )
    score = max(0.0, round(100.0 - penalty, 2))
    if score < 95:
        issues.append(
            DiagramQualityIssue(
                severity="error",
                code="QUALITY_SCORE_BELOW_TARGET",
                message=f"diagram drafting-quality score {score:g} is below the required 95",
                details={"score": score, "target_score": 95},
            )
        )
    issues.sort(key=lambda item: (item.severity != "error", item.code, item.element_ids))
    return DiagramQualityReport(
        passed=not any(issue.severity == "error" for issue in issues),
        score=score,
        metrics=metrics,
        issues=issues,
    )
