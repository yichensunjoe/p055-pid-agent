"""Deterministic geometry and drafting semantics for the M3 drafting engine.

Charter reference: §15 (crossing vs junction, port-aware routing, collision and legend
avoidance), §21.1 (no illegal dangling reference), §21.3 (graphical quality), §48.

Design notes
------------
* **One geometry truth per concern.** Drawing rules (orthogonality, micro segments,
  pipe-through-equipment, text overlaps, score) already exist in ``diagram_quality`` and
  ``annotation_layout``; this module *imports* them instead of re-deriving them, and its
  crossing/text primitives are pinned to those implementations by agreement tests. Only
  drafting-specific questions (junction degree, bridged crossings, reserved drawing
  space, port resolution) are answered here.
* **Port coordinates have exactly one implementation.** :func:`symbol_port_point` is the
  rotation/scale-aware port mapping, and ``DocumentService._symbol_port_point`` delegates
  to it so a port can never mean two different points.
* **Canonical order everywhere.** :func:`canonical_document` sorts elements by id before
  any engine pass runs, and digests are computed over canonical projections only. That is
  what makes a drafting result reproducible across runs and independent of the order in
  which elements happen to be stored (Charter §48 "stable, reproducible results").
* **Functions here are pure.** No function in this module writes a document; every one of
  them takes a snapshot and returns derived data.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import cos, radians, sin
from typing import Any, Literal

from .annotation_layout import measure_annotation_quality, text_bounds
from .diagram_quality import (
    analyze_diagram_quality,
    definition_port_side,
    port_outward_normal,
)
from .drafting_models import (
    DraftingCrossing,
    DraftingFinding,
    DraftingJunction,
    DraftingPolicy,
    DraftingRequest,
    DraftingSnapshot,
    ResolvedPort,
)
from .layout_models import (
    RegionBox,
    declared_lock_regions,
    declared_reserved_regions,
    element_is_locked,
)
from .models import (
    ConnectorElement,
    Document,
    Element,
    Operation,
    Point,
    SymbolElement,
    SymbolPort,
    TextElement,
)
from .symbols import SymbolRegistry

EPSILON = 1e-6


# --------------------------------------------------------------------------- #
# Geometry primitives
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Rect:
    x1: float
    y1: float
    x2: float
    y2: float
    element_id: str = ""

    @property
    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    def expanded(self, margin: float) -> Rect:
        return Rect(
            self.x1 - margin,
            self.y1 - margin,
            self.x2 + margin,
            self.y2 + margin,
            self.element_id,
        )


@dataclass(frozen=True)
class Segment:
    start: Point
    end: Point
    element_id: str = ""

    @property
    def horizontal(self) -> bool:
        return abs(self.start.y - self.end.y) <= EPSILON

    @property
    def vertical(self) -> bool:
        return abs(self.start.x - self.end.x) <= EPSILON

    @property
    def length(self) -> float:
        return abs(self.end.x - self.start.x) + abs(self.end.y - self.start.y)


def same_point(first: Point, second: Point) -> bool:
    return abs(first.x - second.x) <= EPSILON and abs(first.y - second.y) <= EPSILON


def simplify(points: Iterable[Point]) -> list[Point]:
    deduped: list[Point] = []
    for point in points:
        if deduped and same_point(deduped[-1], point):
            continue
        deduped.append(Point.model_validate(point.model_dump(mode="python")))
    if len(deduped) < 3:
        return deduped
    result: list[Point] = [deduped[0]]
    for index in range(1, len(deduped) - 1):
        previous = result[-1]
        current = deduped[index]
        following = deduped[index + 1]
        collinear = (abs(previous.x - current.x) <= EPSILON and abs(current.x - following.x) <= EPSILON) or (
            abs(previous.y - current.y) <= EPSILON and abs(current.y - following.y) <= EPSILON
        )
        if collinear:
            continue
        result.append(current)
    result.append(deduped[-1])
    return result


def segments(points: Iterable[Point], element_id: str = "") -> list[Segment]:
    ordered = list(points)
    return [
        Segment(first, second, element_id)
        for first, second in zip(ordered, ordered[1:], strict=False)
    ]


def interval_overlap(a1: float, a2: float, b1: float, b2: float) -> float:
    left = max(min(a1, a2), min(b1, b2))
    right = min(max(a1, a2), max(b1, b2))
    return max(0.0, right - left)


def rects_overlap(first: Rect, second: Rect) -> bool:
    return not (
        first.x2 <= second.x1 + EPSILON
        or second.x2 <= first.x1 + EPSILON
        or first.y2 <= second.y1 + EPSILON
        or second.y2 <= first.y1 + EPSILON
    )


def point_inside_rect(point: Point, rect: Rect) -> bool:
    return (
        rect.x1 + EPSILON < point.x < rect.x2 - EPSILON
        and rect.y1 + EPSILON < point.y < rect.y2 - EPSILON
    )


def segment_intersects_rect(segment: Segment, rect: Rect) -> bool:
    if segment.horizontal:
        y = segment.start.y
        if not (rect.y1 + EPSILON < y < rect.y2 - EPSILON):
            return False
        return interval_overlap(segment.start.x, segment.end.x, rect.x1, rect.x2) > EPSILON
    if segment.vertical:
        x = segment.start.x
        if not (rect.x1 + EPSILON < x < rect.x2 - EPSILON):
            return False
        return interval_overlap(segment.start.y, segment.end.y, rect.y1, rect.y2) > EPSILON
    return rects_overlap(segment_bounds(segment), rect)


def segment_bounds(segment: Segment) -> Rect:
    return Rect(
        min(segment.start.x, segment.end.x),
        min(segment.start.y, segment.end.y),
        max(segment.start.x, segment.end.x),
        max(segment.start.y, segment.end.y),
        segment.element_id,
    )


def crossing_point(first: Segment, second: Segment) -> Point | None:
    """Strict interior crossing of one horizontal and one vertical segment.

    Mirrors the definition used by ``diagram_quality`` (an endpoint touch is not a
    crossing; a shared bound element is filtered by the caller).
    """

    if first.horizontal and second.vertical:
        horizontal, vertical = first, second
    elif first.vertical and second.horizontal:
        horizontal, vertical = second, first
    else:
        return None
    x = vertical.start.x
    y = horizontal.start.y
    if not (
        min(horizontal.start.x, horizontal.end.x) + EPSILON
        < x
        < max(horizontal.start.x, horizontal.end.x) - EPSILON
        and min(vertical.start.y, vertical.end.y) + EPSILON
        < y
        < max(vertical.start.y, vertical.end.y) - EPSILON
    ):
        return None
    return Point(x=x, y=y)


def element_rect(element: Element) -> Rect | None:
    """Axis-aligned bounds of a node-like element (rotation-aware for symbols)."""

    if element.type == "symbol":
        angle = element.rotation % 360
        if angle <= EPSILON or abs(angle - 180) <= EPSILON:
            return Rect(
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
        return Rect(min(xs), min(ys), max(xs), max(ys), element.id)
    if element.type == "junction":
        return Rect(
            element.position.x - element.radius,
            element.position.y - element.radius,
            element.position.x + element.radius,
            element.position.y + element.radius,
            element.id,
        )
    return None


def text_rect(element: TextElement) -> Rect:
    box = text_bounds(element)
    return Rect(box.x1, box.y1, box.x2, box.y2, element.id)


def connector_segments(element: ConnectorElement) -> list[Segment]:
    return segments(element.points, element.id)


# --------------------------------------------------------------------------- #
# Canonical order, projections and digests
# --------------------------------------------------------------------------- #


def canonical_elements(document: Document) -> list[Element]:
    """Every element in one deterministic order: by id, never by document order."""

    return sorted(document.elements, key=lambda element: element.id)


def canonical_document(document: Document) -> Document:
    """Deep copy with elements id-sorted, so no pass can depend on storage order."""

    copy = Document.model_validate(document.model_dump(mode="python"))
    copy.elements = canonical_elements(copy)
    return copy


def _element_projection(element: Element) -> dict[str, Any]:
    payload = element.model_dump(mode="json")
    # Style is presentation only: a restyle must not change drafting identity.
    payload["style"] = {}
    return payload


def drafting_content_hash(document: Document) -> str:
    """Stable hash of everything a drafting pass may be affected by.

    Includes canvas/grid, layers and systems (visibility and locks decide what may move)
    and every element's canonical projection (identity, geometry, metadata such as
    ``drafting_lock``). Element order is normalised away on purpose.
    """

    payload = {
        "id": document.id,
        "canvas": document.canvas.model_dump(mode="json"),
        "layers": [
            layer.model_dump(mode="json") for layer in sorted(document.layers, key=lambda item: item.id)
        ],
        "systems": [
            system.model_dump(mode="json")
            for system in sorted(document.systems, key=lambda item: item.id)
        ],
        "elements": [_element_projection(element) for element in canonical_elements(document)],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def operation_payload(operation: Operation) -> dict[str, Any]:
    return operation.model_dump(mode="json")


#: Ordering rank for one drafting transaction. Geometry that connectors attach to comes
#: first, then the connectors themselves: a pipe's route only means something once its
#: symbols are where they will end up.
OPERATION_RANK: dict[str, int] = {
    "symbol": 0,
    "junction": 0,
    "text": 0,
    "connector": 1,
}


def canonical_operations(
    operations: Iterable[Operation],
    *,
    element_kinds: Mapping[str, str] | None = None,
) -> list[Operation]:
    """One final, canonically ordered operation per element.

    A pipeline collects operations pass by pass, so the same element can appear several
    times. Replaying them in sequence would walk through intermediate geometry, and an
    intermediate state can be *invalid*: the write boundary re-binds a pipe whenever a
    symbol it is bound to moves, so a route expressed before its symbol moved becomes a
    diagonal and is rejected. The transaction therefore states the final state once per
    element — patches merged in emission order — with symbols, junctions and annotations
    before the connectors bound to them. The digest is computed over this list, so the same
    drawing content and the same request produce the same transaction.
    """

    kinds = element_kinds or {}
    merged: dict[str, Operation] = {}
    for operation in operations:
        element_id = getattr(operation, "element_id", "")
        previous = merged.get(element_id)
        if previous is None:
            merged[element_id] = operation
            continue
        patch = {**getattr(previous, "patch", {}), **getattr(operation, "patch", {})}
        merged[element_id] = operation.model_copy(update={"patch": patch})
    return sorted(
        merged.values(),
        key=lambda operation: (
            OPERATION_RANK.get(kinds.get(getattr(operation, "element_id", ""), ""), 0),
            getattr(operation, "op", ""),
            getattr(operation, "element_id", ""),
            json.dumps(operation_payload(operation), sort_keys=True, ensure_ascii=False),
        ),
    )


def transaction_digest(
    *,
    engine_version: int,
    request: DraftingRequest,
    input_hash: str,
    output_hash: str,
    operations: Iterable[Operation],
    element_kinds: Mapping[str, str] | None = None,
) -> str:
    """Digest that identifies a drafting run's result exactly."""

    payload = {
        "engine_version": engine_version,
        "policy": request.policy.model_dump(mode="json"),
        "scope": {
            "region": request.region.model_dump(mode="json") if request.region else None,
            "element_ids": sorted(request.element_ids),
            "locked_element_ids": sorted(request.locked_element_ids),
            "direction": request.direction,
            "relayout": request.relayout,
            "rank_gap": request.rank_gap,
            "node_gap": request.node_gap,
            "component_gap": request.component_gap,
            "reroute_connectors": request.reroute_connectors,
            "place_annotations": request.place_annotations,
            "bridge_crossings": request.bridge_crossings,
            "resolve_collisions": request.resolve_collisions,
            "include_hidden": request.include_hidden,
        },
        "input_content_hash": input_hash,
        "output_content_hash": output_hash,
        "operations": [
            operation_payload(operation)
            for operation in canonical_operations(operations, element_kinds=element_kinds)
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #


def symbol_port_point(symbol: SymbolElement, port: SymbolPort, definition: Any) -> Point:
    """Absolute drawing point of a symbol port (scaled and rotation-aware).

    This is the single implementation of the port mapping;
    ``DocumentService._symbol_port_point`` delegates here so ports cannot drift apart
    between the router, the compiler and the drafting report.
    """

    local_x = port.x * symbol.width / definition.width
    local_y = port.y * symbol.height / definition.height
    center_x = symbol.width / 2
    center_y = symbol.height / 2
    angle = radians(symbol.rotation)
    dx = local_x - center_x
    dy = local_y - center_y
    rotated_x = center_x + dx * cos(angle) - dy * sin(angle)
    rotated_y = center_y + dx * sin(angle) + dy * cos(angle)
    return Point(x=symbol.position.x + rotated_x, y=symbol.position.y + rotated_y)


def resolve_ports(document: Document, registry: SymbolRegistry) -> list[ResolvedPort]:
    """Every addressable port with its direction, outward side and current load.

    Deterministic: sorted by (element id, port id). A connector endpoint bound to a
    port that does not exist in the symbol definition is *not* silently skipped — see
    :func:`port_findings`.
    """

    bound: dict[tuple[str, str], list[str]] = {}
    for element in canonical_elements(document):
        if element.type != "connector":
            continue
        for endpoint in (element.source, element.target):
            if endpoint is None or endpoint.element_id is None or endpoint.port_id is None:
                continue
            bound.setdefault((endpoint.element_id, endpoint.port_id), []).append(element.id)

    result: list[ResolvedPort] = []
    for element in canonical_elements(document):
        if element.type == "symbol":
            try:
                definition = registry.get(element.symbol_key)
            except KeyError:
                continue
            for port in definition.ports:
                connector_ids = sorted(bound.get((element.id, port.id), []))
                result.append(
                    ResolvedPort(
                        element_id=element.id,
                        element_kind="symbol",
                        port_id=port.id,
                        port_name=port.name,
                        direction=port.direction,
                        medium=port.medium,
                        side=definition_port_side(  # type: ignore[arg-type]
                            definition.width, definition.height, port.x, port.y
                        ),
                        outward_normal=port_outward_normal(element, port.id, registry),
                        point=symbol_port_point(element, port, definition),
                        connected_element_ids=sorted(
                            {
                                other
                                for connector_id in connector_ids
                                for other in _other_endpoint_element_ids(document, connector_id, element.id)
                            }
                        ),
                        connector_ids=connector_ids,
                    )
                )
        elif element.type == "junction":
            connector_ids = sorted(bound.get((element.id, "node"), []))
            result.append(
                ResolvedPort(
                    element_id=element.id,
                    element_kind="junction",
                    port_id="node",
                    port_name="node",
                    direction="bidirectional",
                    medium="process",
                    side="interior",
                    outward_normal=None,
                    point=Point(x=element.position.x, y=element.position.y),
                    connected_element_ids=sorted(
                        {
                            other
                            for connector_id in connector_ids
                            for other in _other_endpoint_element_ids(document, connector_id, element.id)
                        }
                    ),
                    connector_ids=connector_ids,
                )
            )
    return sorted(result, key=lambda port: (port.element_id, port.port_id))


def _other_endpoint_element_ids(
    document: Document,
    connector_id: str,
    element_id: str,
) -> list[str]:
    for element in document.elements:
        if element.type != "connector" or element.id != connector_id:
            continue
        others: list[str] = []
        for endpoint in (element.source, element.target):
            if endpoint is None or endpoint.element_id is None:
                continue
            if endpoint.element_id != element_id:
                others.append(endpoint.element_id)
        return others
    return []


def port_findings(document: Document, registry: SymbolRegistry) -> list[DraftingFinding]:
    """Report connector endpoints bound to a port the symbol does not define.

    The router silently returns ``None`` for such a port; that silence is exactly how a
    broken binding survives a "clean" layout run, so the drafting gate says it out loud.
    """

    findings: list[DraftingFinding] = []
    element_map = {element.id: element for element in document.elements}
    for element in canonical_elements(document):
        if element.type != "connector":
            continue
        for name, endpoint in (("source", element.source), ("target", element.target)):
            if endpoint is None or endpoint.element_id is None or endpoint.port_id is None:
                continue
            bound = element_map.get(endpoint.element_id)
            if bound is None:
                findings.append(
                    DraftingFinding(
                        severity="error",
                        code="DRAFT_PORT_UNRESOLVED",
                        message=(
                            f"connector {element.id} {name} references missing element "
                            f"{endpoint.element_id}"
                        ),
                        element_ids=[element.id, endpoint.element_id],
                        details={"endpoint": name},
                    )
                )
                continue
            if bound.type == "junction":
                if endpoint.port_id != "node":
                    findings.append(
                        DraftingFinding(
                            severity="error",
                            code="DRAFT_PORT_UNRESOLVED",
                            message=(
                                f"connector {element.id} {name} uses port "
                                f"{endpoint.port_id!r} on junction {bound.id}"
                            ),
                            element_ids=[element.id, bound.id],
                            details={"endpoint": name, "port_id": endpoint.port_id},
                        )
                    )
                continue
            if bound.type != "symbol":
                continue
            try:
                definition = registry.get(bound.symbol_key)
            except KeyError:
                findings.append(
                    DraftingFinding(
                        severity="warning",
                        code="DRAFT_PORT_UNRESOLVED",
                        message=(
                            f"connector {element.id} {name} is bound to symbol {bound.id} with "
                            f"unknown symbol key {bound.symbol_key!r}"
                        ),
                        element_ids=[element.id, bound.id],
                        details={"endpoint": name, "symbol_key": bound.symbol_key},
                    )
                )
                continue
            if not any(port.id == endpoint.port_id for port in definition.ports):
                findings.append(
                    DraftingFinding(
                        severity="error",
                        code="DRAFT_PORT_UNRESOLVED",
                        message=(
                            f"connector {element.id} {name} binds port {endpoint.port_id!r} which "
                            f"symbol {bound.symbol_key!r} does not define"
                        ),
                        element_ids=[element.id, bound.id],
                        details={
                            "endpoint": name,
                            "port_id": endpoint.port_id,
                            "available_port_ids": sorted(port.id for port in definition.ports),
                        },
                    )
                )
    return findings


# --------------------------------------------------------------------------- #
# Crossing / junction semantics
# --------------------------------------------------------------------------- #


def _bound_element_ids(connector: ConnectorElement) -> set[str]:
    return {
        endpoint.element_id
        for endpoint in (connector.source, connector.target)
        if endpoint is not None and endpoint.element_id is not None
    }


def shared_bound_element(first: ConnectorElement, second: ConnectorElement) -> bool:
    return bool(_bound_element_ids(first) & _bound_element_ids(second))


def detect_crossings(document: Document) -> list[DraftingCrossing]:
    """Geometric crossings that are *not* connections, and whether they are bridged.

    Charter P0-4/§15: visual intersection is not engineering connectivity. A crossing is
    legal only when exactly one of the two connectors draws a jump bridge; a crossing
    that lands on a junction element means the drawing is claiming a connection that the
    model does not have, which is reported instead of smoothed over.
    """

    connectors = [element for element in canonical_elements(document) if element.type == "connector"]
    junctions = [
        (element, element_rect(element))
        for element in canonical_elements(document)
        if element.type == "junction"
    ]
    seen: set[tuple[str, str, float, float]] = set()
    crossings: list[DraftingCrossing] = []
    for index, first in enumerate(connectors):
        for second in connectors[index + 1 :]:
            if shared_bound_element(first, second):
                continue
            for first_segment in connector_segments(first):
                for second_segment in connector_segments(second):
                    point = crossing_point(first_segment, second_segment)
                    if point is None:
                        continue
                    key = (
                        min(first.id, second.id),
                        max(first.id, second.id),
                        round(point.x, 6),
                        round(point.y, 6),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    first_bridged = first.crossing_style == "jump"
                    second_bridged = second.crossing_style == "jump"
                    at_junction = ""
                    for junction, rect in junctions:
                        if rect is not None and point_inside_rect(point, rect.expanded(1.0)):
                            at_junction = junction.id
                            break
                    if first_bridged and second_bridged:
                        bridged_by = "both"
                    elif first_bridged:
                        bridged_by = first.id
                    elif second_bridged:
                        bridged_by = second.id
                    else:
                        bridged_by = ""
                    crossings.append(
                        DraftingCrossing(
                            x=point.x,
                            y=point.y,
                            first_connector_id=first.id,
                            second_connector_id=second.id,
                            bridged=bridged_by != "",
                            bridged_by=bridged_by,
                            at_junction_id=at_junction,
                        )
                    )
    return sorted(
        crossings,
        key=lambda item: (
            item.first_connector_id,
            item.second_connector_id,
            round(item.x, 6),
            round(item.y, 6),
        ),
    )


def classify_junctions(document: Document) -> list[DraftingJunction]:
    """What each explicit junction element really implements.

    ``branch`` (degree ≥ 3) is a real tee/merge, ``inline`` (degree 2) is a legitimate
    segment boundary, ``dangling`` (degree < 2) is a topology error: an object that was
    meant to be a connection but connects nothing (Charter §6.6, §21.1).
    """

    incident: dict[str, list[str]] = {}
    neighbours: dict[str, set[str]] = {}
    for element in canonical_elements(document):
        if element.type != "connector":
            continue
        for endpoint in (element.source, element.target):
            if endpoint is None or endpoint.element_id is None:
                continue
            incident.setdefault(endpoint.element_id, []).append(element.id)
            for other in (element.source, element.target):
                if other is None or other.element_id is None or other.element_id == endpoint.element_id:
                    continue
                neighbours.setdefault(endpoint.element_id, set()).add(other.element_id)

    result: list[DraftingJunction] = []
    for element in canonical_elements(document):
        if element.type != "junction":
            continue
        connector_ids = sorted(set(incident.get(element.id, [])))
        degree = len(connector_ids)
        kind: Literal["branch", "inline", "dangling"] = (
            "branch" if degree >= 3 else "inline" if degree == 2 else "dangling"
        )
        result.append(
            DraftingJunction(
                element_id=element.id,
                degree=degree,
                kind=kind,
                connector_ids=connector_ids,
                element_ids=sorted(neighbours.get(element.id, set())),
            )
        )
    return result


# --------------------------------------------------------------------------- #
# Reserved drawing space (legend / title block / notes / keep-clear)
# --------------------------------------------------------------------------- #


def reserved_region_intrusions(
    document: Document,
    registry: SymbolRegistry,
) -> list[DraftingFinding]:
    """Symbols, pipes and labels that intrude into declared reserved drawing space.

    Charter §15 asks for legend overlap avoidance and §6.2 gives a drawing a border and
    title block. Reserved space is declared as document data
    (``metadata.layout_regions`` with a non-``lock`` kind), so this is a real check with
    a real geometry, not a guess about pixels.
    """

    regions = declared_reserved_regions(document)
    if not regions:
        return []
    findings: list[DraftingFinding] = []
    for element in canonical_elements(document):
        label = f"元素 {element.id}"
        if element.type == "symbol":
            rect = element_rect(element)
            label = f"设备 {element.label.strip() or element.id}"
        elif element.type == "junction":
            rect = element_rect(element)
            label = f"连接节点 {element.id}"
        elif element.type == "connector":
            rect = None
        elif element.type == "text":
            rect = text_rect(element)
            label = f"标注 {element.text.strip() or element.id!s}"
        else:
            rect = None
        if rect is not None:
            for region in regions:
                if region.overlaps_box(rect.x1, rect.y1, rect.x2, rect.y2):
                    findings.append(
                        DraftingFinding(
                            severity="error",
                            code="DRAFT_RESERVED_REGION_OVERLAP",
                            message=(
                                f"{label} 侵入预留区域 {region.label or region.kind}"
                            ),
                            element_ids=[element.id],
                            details={
                                "region_kind": region.kind,
                                "region_label": region.label,
                                "region_bounds": list(region.bounds),
                            },
                        )
                    )
                    break
            continue
        if element.type == "connector":
            for segment in connector_segments(element):
                hit = next(
                    (
                        region
                        for region in regions
                        if segment_intersects_rect(segment, _region_rect(region))
                    ),
                    None,
                )
                if hit is not None:
                    findings.append(
                        DraftingFinding(
                            severity="error",
                            code="DRAFT_RESERVED_REGION_OVERLAP",
                            message=(
                                f"管线 {element.process_tag.strip() or element.id} 穿越预留区域 "
                                f"{hit.label or hit.kind}"
                            ),
                            element_ids=[element.id],
                            details={
                                "region_kind": hit.kind,
                                "region_label": hit.label,
                                "region_bounds": list(hit.bounds),
                            },
                        )
                    )
                    break
    return findings


def _region_rect(region: RegionBox) -> Rect:
    left, top, right, bottom = region.bounds
    return Rect(left, top, right, bottom)


# --------------------------------------------------------------------------- #
# Findings, collisions and snapshots
# --------------------------------------------------------------------------- #


def crossing_findings(crossings: Iterable[DraftingCrossing]) -> list[DraftingFinding]:
    findings: list[DraftingFinding] = []
    for crossing in crossings:
        pair = [crossing.first_connector_id, crossing.second_connector_id]
        if crossing.at_junction_id:
            findings.append(
                DraftingFinding(
                    severity="error",
                    code="DRAFT_CROSSING_ON_JUNCTION",
                    message=(
                        f"管线 {crossing.first_connector_id} 与 {crossing.second_connector_id} 的交叉点"
                        f"落在连接节点 {crossing.at_junction_id} 上，但两者并未绑定该节点"
                    ),
                    element_ids=[*pair, crossing.at_junction_id],
                    details={"point": {"x": crossing.x, "y": crossing.y}},
                )
            )
            continue
        if crossing.bridged_by == "both":
            findings.append(
                DraftingFinding(
                    severity="warning",
                    code="DRAFT_CROSSING_DOUBLE_BRIDGED",
                    message=(
                        f"管线 {crossing.first_connector_id} 与 {crossing.second_connector_id} 的交叉点"
                        "两侧都画了跨线桥，应按主/次管线只保留一侧"
                    ),
                    element_ids=pair,
                    details={"point": {"x": crossing.x, "y": crossing.y}},
                )
            )
            continue
        if not crossing.bridged:
            findings.append(
                DraftingFinding(
                    severity="error",
                    code="DRAFT_CROSSING_UNBRIDGED",
                    message=(
                        f"管线 {crossing.first_connector_id} 与 {crossing.second_connector_id} 交叉"
                        "但没有任何跨线桥，无法区分连接与交叉"
                    ),
                    element_ids=pair,
                    details={"point": {"x": crossing.x, "y": crossing.y}},
                )
            )
    return findings


def junction_findings(junctions: Iterable[DraftingJunction]) -> list[DraftingFinding]:
    findings: list[DraftingFinding] = []
    for junction in junctions:
        if junction.kind != "dangling":
            continue
        findings.append(
            DraftingFinding(
                severity="error",
                code="DRAFT_JUNCTION_DANGLING",
                message=(
                    f"连接节点 {junction.element_id} 只有 {junction.degree} 条管线接入，"
                    "既不是分支也不是正常的管线分段"
                ),
                element_ids=[junction.element_id, *junction.connector_ids],
                details={"degree": junction.degree},
            )
        )
    return findings


def collision_findings(
    document: Document,
    registry: SymbolRegistry,
    policy: DraftingPolicy,
) -> list[DraftingFinding]:
    """Hard geometric collisions, with the specific offending elements.

    Counts are intentionally the same quantities ``diagram_quality`` scores, so this
    report and the drafting-quality report can never disagree about whether a drawing
    has an overlap; the difference is that each collision here names its elements.
    """

    findings: list[DraftingFinding] = []
    rects = [
        rect
        for element in canonical_elements(document)
        if (rect := element_rect(element)) is not None
    ]
    for index, first in enumerate(rects):
        for second in rects[index + 1 :]:
            if rects_overlap(first, second):
                findings.append(
                    DraftingFinding(
                        severity="error",
                        code="DRAFT_NODE_OVERLAP",
                        message=f"设备/节点重叠: {first.element_id}, {second.element_id}",
                        element_ids=[first.element_id, second.element_id],
                    )
                )
    rect_by_id = {rect.element_id: rect for rect in rects}
    for connector in canonical_elements(document):
        if connector.type != "connector":
            continue
        excluded = _bound_element_ids(connector)
        obstacles = [
            rect.expanded(policy.route_clearance)
            for element_id, rect in rect_by_id.items()
            if element_id not in excluded
        ]
        hits = [
            segment
            for segment in connector_segments(connector)
            if any(segment_intersects_rect(segment, rect) for rect in obstacles)
        ]
        if hits:
            findings.append(
                DraftingFinding(
                    severity="error",
                    code="DRAFT_PIPE_THROUGH_EQUIPMENT",
                    message=f"管线 {connector.process_tag.strip() or connector.id} 穿越设备或节点",
                    element_ids=[connector.id],
                    details={"intersection_count": len(hits)},
                )
            )
    grid = max(5.0, document.canvas.grid_size)
    for connector in canonical_elements(document):
        if connector.type != "connector":
            continue
        element_segments = connector_segments(connector)
        diagonal = sum(not (segment.horizontal or segment.vertical) for segment in element_segments)
        if diagonal:
            findings.append(
                DraftingFinding(
                    severity="error",
                    code="DRAFT_NON_ORTHOGONAL_SEGMENT",
                    message=f"管线 {connector.id} 含非正交线段",
                    element_ids=[connector.id],
                    details={"segment_count": diagonal},
                )
            )
        micro = sum(0 < segment.length < grid - EPSILON for segment in element_segments)
        if micro:
            findings.append(
                DraftingFinding(
                    severity="error",
                    code="DRAFT_MICRO_SEGMENT",
                    message=f"管线 {connector.id} 含小于栅格的短线段",
                    element_ids=[connector.id],
                    details={"count": micro, "minimum_leg": grid},
                )
            )
        outside = sum(
            point.x < -EPSILON
            or point.y < -EPSILON
            or point.x > document.canvas.width + EPSILON
            or point.y > document.canvas.height + EPSILON
            for point in connector.points
        )
        if outside:
            findings.append(
                DraftingFinding(
                    severity="error",
                    code="DRAFT_OUT_OF_BOUNDS",
                    message=f"管线 {connector.id} 超出图纸幅面",
                    element_ids=[connector.id],
                    details={"point_count": outside},
                )
            )
    for element in canonical_elements(document):
        if element.type != "symbol":
            continue
        rect = element_rect(element)
        if rect is None:
            continue
        if (
            rect.x1 < -EPSILON
            or rect.y1 < -EPSILON
            or rect.x2 > document.canvas.width + EPSILON
            or rect.y2 > document.canvas.height + EPSILON
        ):
            findings.append(
                DraftingFinding(
                    severity="warning",
                    code="DRAFT_OUT_OF_BOUNDS",
                    message=f"设备 {element.id} 超出图纸幅面",
                    element_ids=[element.id],
                )
            )
    # Only *editable* labels are drafting geometry. A symbol's derived label is not an
    # element the drafting engine may move — materialising and placing it is the
    # annotation-polish pass — and the drawing rules already score derived labels through
    # ``measure_annotation_quality``, so excluding them here removes noise, not evidence.
    text_boxes = [
        (element, text_rect(element))
        for element in canonical_elements(document)
        if element.type == "text" and element.text.strip()
    ]
    for index, (first, first_rect) in enumerate(text_boxes):
        for second, second_rect in text_boxes[index + 1 :]:
            if rects_overlap(first_rect.expanded(2), second_rect.expanded(2)):
                findings.append(
                    DraftingFinding(
                        severity="error",
                        code="DRAFT_TEXT_TEXT_OVERLAP",
                        message=f"标注互相重叠: {first.text.strip()!r}, {second.text.strip()!r}",
                        element_ids=[first.id, second.id],
                    )
                )
    node_boxes = [
        rect
        for element in canonical_elements(document)
        if (rect := _annotation_blocker_rect(element)) is not None
    ]
    for text, text_box in text_boxes:
        for node_box in node_boxes:
            if rects_overlap(text_box.expanded(2), node_box.expanded(2)):
                findings.append(
                    DraftingFinding(
                        severity="error",
                        code="DRAFT_TEXT_SYMBOL_OVERLAP",
                        message=f"标注 {text.text.strip()!r} 压在设备/节点 {node_box.element_id} 上",
                        element_ids=[text.id, node_box.element_id],
                    )
                )
    for text, text_box in text_boxes:
        for connector in canonical_elements(document):
            if connector.type != "connector":
                continue
            if any(
                segment_intersects_rect(segment, text_box.expanded(2))
                for segment in connector_segments(connector)
            ):
                findings.append(
                    DraftingFinding(
                        severity="error",
                        code="DRAFT_TEXT_CONNECTOR_CROSSING",
                        message=f"标注 {text.text.strip()!r} 压在管线 {connector.id} 上",
                        element_ids=[text.id, connector.id],
                    )
                )
    return findings


def _annotation_blocker_rect(element: Element) -> Rect | None:
    if element.type in {"symbol", "junction"}:
        return element_rect(element)
    return None


def symbol_definition_findings(
    document: Document,
    registry: SymbolRegistry,
) -> list[DraftingFinding]:
    """Symbols whose definition the loaded catalog does not provide.

    A real drawing can reference a symbol key that no longer exists (a renamed library,
    a file produced by another checkout). Nothing about that element can be laid out,
    and staying silent would let the report claim it measured the whole drawing. Warned,
    not blocked: the drafting engine still repairs everything it *can* see, and the
    verdict about the drawing itself belongs to the drawing rules and the IR findings,
    which already report the same fact under their own code.
    """

    findings: list[DraftingFinding] = []
    for element in canonical_elements(document):
        if element.type != "symbol":
            continue
        try:
            registry.get(element.symbol_key)
        except KeyError:
            findings.append(
                DraftingFinding(
                    severity="warning",
                    code="DRAFT_SYMBOL_DEFINITION_MISSING",
                    message=(
                        f"图例 {element.symbol_key!r} 不在当前图例库中，"
                        f"符号 {element.id} 无法参与排布与端口解析。"
                    ),
                    element_ids=[element.id],
                    details={"symbol_key": element.symbol_key},
                )
            )
    return findings


def unresolvable_element_ids(
    document: Document,
    registry: SymbolRegistry,
) -> set[str]:
    """Elements whose geometry cannot be moved or normalized at all.

    A real drawing reaches the drafting engine with history: a symbol key the loaded
    catalog no longer defines (see :func:`symbol_definition_findings`), and the pipes
    bound to it. Writing through the service for such an element raises, correctly --
    the service refuses to compute a port point for a symbol it cannot resolve. The
    drafting pipeline must therefore *exclude* them from its passes rather than discover
    them by crashing: everything else still gets repaired, and the report says what could
    not be touched.

    Returns the unresolvable symbols plus every connector bound to one, so no stage can
    pick a candidate that the service would reject.
    """

    unknown_symbols: set[str] = set()
    for element in document.elements:
        if element.type != "symbol":
            continue
        try:
            registry.get(element.symbol_key)
        except KeyError:
            unknown_symbols.add(element.id)
    if not unknown_symbols:
        return set()
    unusable = set(unknown_symbols)
    for element in document.elements:
        if element.type != "connector":
            continue
        for endpoint in (element.source, element.target):
            if endpoint is not None and endpoint.element_id in unknown_symbols:
                unusable.add(element.id)
                break
    return unusable


def structural_findings(
    document: Document,
    registry: SymbolRegistry,
    policy: DraftingPolicy,
) -> list[DraftingFinding]:
    """Every drafting-rule finding for one drawing state, canonically ordered."""

    findings = [
        *crossing_findings(detect_crossings(document)),
        *junction_findings(classify_junctions(document)),
        *reserved_region_intrusions(document, registry),
        *port_findings(document, registry),
        *symbol_definition_findings(document, registry),
    ]
    waived = {code for code in policy.waived_codes}
    return sorted(
        (
            finding.model_copy(update={"waived": finding.code in waived})
            for finding in findings
        ),
        key=lambda finding: (finding.code, finding.element_ids, finding.message),
    )


def snapshot(
    document: Document,
    registry: SymbolRegistry,
    policy: DraftingPolicy,
) -> DraftingSnapshot:
    """Measure one drawing state with the existing drawing rules plus drafting facts."""

    report = analyze_diagram_quality(document, registry)
    metrics = report.metrics
    junctions = classify_junctions(document)
    reserved = reserved_region_intrusions(document, registry)
    return DraftingSnapshot(
        score=report.score,
        passed=report.passed,
        error_issue_count=sum(1 for issue in report.issues if issue.severity == "error"),
        warning_issue_count=sum(1 for issue in report.issues if issue.severity == "warning"),
        symbol_count=metrics.symbol_count,
        connector_count=metrics.connector_count,
        junction_count=len(junctions),
        dangling_junction_count=sum(1 for junction in junctions if junction.kind == "dangling"),
        node_overlaps=metrics.node_overlaps,
        crowded_node_pairs=metrics.crowded_node_pairs,
        pipe_obstacle_intersections=metrics.pipe_obstacle_intersections,
        geometric_crossings=metrics.geometric_crossings,
        unbridged_crossings=metrics.unbridged_crossings,
        total_bends=metrics.total_bends,
        non_orthogonal_segments=metrics.non_orthogonal_segments,
        micro_segments=metrics.micro_segments,
        unnecessary_bends=metrics.unnecessary_bends,
        text_text_overlaps=metrics.text_text_overlaps,
        text_symbol_overlaps=metrics.text_symbol_overlaps,
        text_connector_intersections=metrics.text_connector_intersections,
        duplicate_label_count=metrics.duplicate_label_count,
        out_of_bounds_symbols=metrics.out_of_bounds_symbols,
        out_of_bounds_connector_points=metrics.out_of_bounds_connector_points,
        reserved_region_intrusions=len(reserved),
        total_route_length=round(
            sum(
                segment.length
                for element in document.elements
                if element.type == "connector"
                for segment in connector_segments(element)
            ),
            3,
        ),
    )


def annotation_quality_counts(document: Document, registry: SymbolRegistry) -> tuple[int, int, int]:
    """(text-text, text-symbol, text-connector) counts, via the annotation module."""

    quality = measure_annotation_quality(document, registry)
    return (
        quality.text_text_overlaps,
        quality.text_symbol_overlaps,
        quality.text_connector_intersections,
    )


def hard_regressions(before: DraftingSnapshot, after: DraftingSnapshot) -> list[str]:
    """Field-level description of every hard metric that got worse."""

    before_signature = dict(before.hard_signature())
    after_signature = dict(after.hard_signature())
    return [
        f"{name}: {before_signature[name]:g} -> {after_signature[name]:g}"
        for name in before_signature
        if after_signature[name] > before_signature[name] + EPSILON
    ]


def improvements(before: DraftingSnapshot, after: DraftingSnapshot) -> list[str]:
    before_signature = dict(before.hard_signature())
    after_signature = dict(after.hard_signature())
    return [
        f"{name}: {before_signature[name]:g} -> {after_signature[name]:g}"
        for name in before_signature
        if after_signature[name] < before_signature[name] - EPSILON
    ]


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #


def resolve_scope(
    document: Document,
    request: DraftingRequest,
) -> tuple[list[str], Literal["document", "region", "selection"]]:
    """Which elements a drafting run may touch, and why.

    A region restricts scope to geometry that sits inside it; an explicit selection
    restricts it to those elements plus the connectors between them; neither means the
    whole drawing. Locked elements are *reported* as locked but stay in scope so they can
    act as fixed anchors and obstacles; the engine never moves them.
    """

    connectable = {
        element.id
        for element in document.elements
        if element.type in {"symbol", "junction"}
    }
    connector_ids = {
        element.id for element in document.elements if element.type == "connector"
    }
    text_ids = {element.id for element in document.elements if element.type == "text"}
    if request.region is not None:
        scope = {
            element.id
            for element in document.elements
            if element.id in connectable
            and (rect := element_rect(element)) is not None
            and request.region.overlaps_box(rect.x1, rect.y1, rect.x2, rect.y2)
        }
        if not scope:
            return [], "region"
        for element in document.elements:
            if element.type != "connector":
                continue
            bound = _bound_element_ids(element)
            if bound and bound <= scope:
                scope.add(element.id)
        return sorted(scope), "region"
    if request.element_ids:
        requested = {element_id for element_id in request.element_ids if element_id}
        scope = requested & (connectable | connector_ids | text_ids)
        for element in document.elements:
            if element.type != "connector" or element.id not in scope:
                continue
            for element_id in _bound_element_ids(element):
                if element_id in connectable:
                    scope.add(element_id)
        # A selection of connectable objects also owns the pipes *between* them: an
        # engineer who selects two valves and asks for a tidy repair means the run
        # between them, not just the symbols (documented contract of this function).
        for element in document.elements:
            if element.type != "connector" or element.id in scope:
                continue
            bound = _bound_element_ids(element)
            if bound and bound <= scope:
                scope.add(element.id)
        return sorted(scope), "selection"
    return sorted(connectable | connector_ids | text_ids), "document"


def element_is_movable(
    element: Element,
    locked_ids: set[str],
    scope_ids: set[str],
) -> bool:
    return element.id in scope_ids and element.id not in locked_ids


def lock_provenance(
    document: Document,
    request: DraftingRequest,
    locked_ids: set[str],
) -> dict[str, list[str]]:
    """Split a lock set into its sources so the report can explain every freeze."""

    metadata_locked = sorted(
        element.id for element in document.elements if element_is_locked(element)
    )
    region_ids: set[str] = set()
    regions = declared_lock_regions(document)
    for element in document.elements:
        rect = element_rect(element)
        if rect is None:
            continue
        if any(region.contains_box(rect.x1, rect.y1, rect.x2, rect.y2) for region in regions):
            region_ids.add(element.id)
    return {
        "locked_element_ids": sorted(locked_ids),
        "request_element_ids": sorted(
            element_id for element_id in request.locked_element_ids if element_id
        ),
        "metadata_element_ids": metadata_locked,
        "region_element_ids": sorted(region_ids),
        "region_labels": sorted(region.label or region.kind for region in regions),
    }
