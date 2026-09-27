from __future__ import annotations

from math import atan2, cos, pi, radians, sin
from typing import Any

from .dxf_core import DxfExportError, _Builder, _clean_text
from .models import Element, Point, SymbolElement
from .symbol_paths import SymbolPathError, sample_symbol_path
from .symbols import SymbolRegistry


def _rotate(point: tuple[float, float], center: tuple[float, float], angle_degrees: float) -> tuple[float, float]:
    if angle_degrees == 0:
        return point
    angle = radians(angle_degrees)
    dx = point[0] - center[0]
    dy = point[1] - center[1]
    return (
        center[0] + dx * cos(angle) - dy * sin(angle),
        center[1] + dx * sin(angle) + dy * cos(angle),
    )


def _symbol_point(element: SymbolElement, definition: Any, x: float, y: float) -> tuple[float, float]:
    scaled = (x * element.width / definition.width, y * element.height / definition.height)
    rotated = _rotate(scaled, (element.width / 2, element.height / 2), element.rotation)
    return (element.position.x + rotated[0], element.position.y + rotated[1])


def _rounded_rectangle_points(x: float, y: float, width: float, height: float, radius: float) -> list[tuple[float, float]]:
    radius = min(max(radius, 0.0), width / 2, height / 2)
    if radius <= 0:
        return [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
    points: list[tuple[float, float]] = []
    for center_x, center_y, start_angle in (
        (x + width - radius, y + radius, -90),
        (x + width - radius, y + height - radius, 0),
        (x + radius, y + height - radius, 90),
        (x + radius, y + radius, 180),
    ):
        for step in range(5):
            angle = radians(start_angle + step * 22.5)
            points.append((center_x + radius * cos(angle), center_y + radius * sin(angle)))
    return points


def _sample_symbol_path(path: str) -> list[tuple[float, float]]:
    """Polyline points along a symbol path, under the shared catalogue grammar.

    The path semantics are owned by :mod:`symbol_paths` -- the same grammar the M7
    symbol-geometry freeze measures -- so a path legal for layout can never be rejected
    here for "another grammar". Only genuinely uninterpretable paths raise.
    """

    try:
        return sample_symbol_path(path)
    except SymbolPathError as exc:
        raise DxfExportError("unsupported_symbol_path", str(exc)) from exc


def _path_point(points: list[Point], fraction: float) -> tuple[Point, float]:
    segments = [
        (first, second, ((second.x - first.x) ** 2 + (second.y - first.y) ** 2) ** 0.5)
        for first, second in zip(points, points[1:], strict=False)
    ]
    total = sum(length for _, _, length in segments)
    if total <= 0:
        return points[0], 0.0
    target = total * fraction
    walked = 0.0
    for first, second, length in segments:
        if walked + length >= target and length > 0:
            ratio = (target - walked) / length
            return (
                Point(
                    x=first.x + (second.x - first.x) * ratio,
                    y=first.y + (second.y - first.y) * ratio,
                ),
                atan2(second.y - first.y, second.x - first.x),
            )
        walked += length
    first, second, _ = segments[-1]
    return second, atan2(second.y - first.y, second.x - first.x)


def _render_symbol(builder: _Builder, element: SymbolElement, registry: SymbolRegistry) -> None:
    definition = registry.get(element.symbol_key)
    sx = element.width / definition.width
    sy = element.height / definition.height
    for shape in definition.shapes:
        kind = shape.get("type")
        if kind == "line":
            builder.line(
                element,
                _symbol_point(element, definition, float(shape["x1"]), float(shape["y1"])),
                _symbol_point(element, definition, float(shape["x2"]), float(shape["y2"])),
                subtype="symbol_line",
            )
        elif kind == "polyline":
            points = [
                _symbol_point(element, definition, float(point[0]), float(point[1]))
                for point in shape["points"]
            ]
            builder.polyline(element, points, closed=bool(shape.get("closed")), subtype="symbol_polyline")
        elif kind == "rect":
            local = _rounded_rectangle_points(
                float(shape["x"]),
                float(shape["y"]),
                float(shape["width"]),
                float(shape["height"]),
                float(shape.get("rx", 0)),
            )
            builder.polyline(
                element,
                [_symbol_point(element, definition, x, y) for x, y in local],
                closed=True,
                subtype="symbol_rectangle",
            )
        elif kind == "circle":
            local_center = (float(shape["cx"]), float(shape["cy"]))
            center = _symbol_point(element, definition, *local_center)
            radius = float(shape["r"])
            rx = radius * sx
            ry = radius * sy
            if abs(rx - ry) < 1e-9:
                builder.circle(element, center, rx, subtype="symbol_circle")
            else:
                angle = radians(element.rotation)
                if rx >= ry:
                    major = (rx * cos(angle), rx * sin(angle))
                    ratio = ry / rx
                else:
                    major = (-ry * sin(angle), ry * cos(angle))
                    ratio = rx / ry
                builder.ellipse(element, center, major, ratio, subtype="symbol_ellipse")
        elif kind == "path":
            local = _sample_symbol_path(str(shape["d"]))
            builder.polyline(
                element,
                [_symbol_point(element, definition, x, y) for x, y in local],
                closed=len(local) > 2 and local[0] == local[-1],
                subtype="symbol_path",
            )
        elif kind == "text":
            position = _symbol_point(element, definition, float(shape["x"]), float(shape["y"]))
            builder.text(
                element,
                position,
                str(shape.get("text", "")),
                float(shape.get("font_size", 12)) * (sx + sy) / 2,
                anchor=str(shape.get("anchor", "middle")),
                rotation=element.rotation,
                subtype="symbol_text",
            )
        else:
            raise DxfExportError("unsupported_symbol_shape", f"unsupported symbol shape: {kind}")
    if element.label:
        builder.text(
            element,
            _symbol_point(element, definition, definition.width / 2, definition.height + 16),
            element.label,
            12 * (sx + sy) / 2,
            anchor="middle",
            rotation=element.rotation,
            subtype="symbol_label",
        )


def _render_element(builder: _Builder, element: Element, registry: SymbolRegistry) -> None:
    if element.type == "line":
        builder.line(element, (element.start.x, element.start.y), (element.end.x, element.end.y))
    elif element.type == "polyline":
        builder.polyline(element, [(point.x, point.y) for point in element.points], closed=element.closed)
    elif element.type == "rectangle":
        builder.polyline(
            element,
            _rounded_rectangle_points(element.x, element.y, element.width, element.height, element.corner_radius),
            closed=True,
        )
    elif element.type == "circle":
        builder.circle(element, (element.center.x, element.center.y), element.radius)
    elif element.type == "text":
        builder.text(
            element,
            (element.position.x, element.position.y),
            element.text,
            element.font_size,
            anchor=element.anchor,
        )
    elif element.type == "symbol":
        _render_symbol(builder, element, registry)
    elif element.type == "junction":
        builder.circle(element, (element.position.x, element.position.y), element.radius)
        if element.label:
            builder.text(
                element,
                (element.position.x + element.radius + 6, element.position.y + 4),
                element.label,
                12,
                subtype="junction_label",
            )
    elif element.type == "connector":
        metadata = [
            f"routing={element.routing}",
            f"flow_direction={element.flow_direction}",
            f"crossing_style={element.crossing_style}",
        ]
        for key, value in (
            ("process_tag", element.process_tag),
            ("medium", element.medium),
            ("nominal_diameter", element.nominal_diameter),
        ):
            if value:
                metadata.append(f"{key}={_clean_text(value, limit=180)}")
        if element.source and element.source.element_id:
            metadata.append(f"source={element.source.element_id}:{element.source.port_id}")
        if element.target and element.target.element_id:
            metadata.append(f"target={element.target.element_id}:{element.target.port_id}")
        builder.polyline(
            element,
            [(point.x, point.y) for point in element.points],
            extra_metadata=metadata,
        )
        if element.flow_direction != "none":
            fraction = {"start": 0.15, "middle": 0.5, "end": 0.85}[element.arrow_position]
            point, angle = _path_point(element.points, fraction)
            if element.flow_direction == "reverse":
                angle += pi
            size = max(6.0, element.style.stroke_width * 3.2)
            builder.solid(
                element,
                [
                    (point.x + cos(angle) * size, point.y + sin(angle) * size),
                    (point.x + cos(angle + 2.45) * size, point.y + sin(angle + 2.45) * size),
                    (point.x + cos(angle - 2.45) * size, point.y + sin(angle - 2.45) * size),
                ],
                subtype="flow_arrow",
            )
    else:
        raise DxfExportError("unsupported_dxf_element", f"unsupported element type: {element.type}")




def render_elements(
    builder: _Builder, elements: list[Element], registry: SymbolRegistry
) -> None:
    for element in elements:
        _render_element(builder, element, registry)
