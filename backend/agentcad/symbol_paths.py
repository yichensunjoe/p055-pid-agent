"""One SVG path grammar for symbol shapes, shared by every consumer.

The catalogue's builtin symbols draw with SVG path ``d`` strings. Two places interpret
those strings, and they must agree on what a legal path is:

* the M7 symbol-geometry freeze and containment proofs (bounds), and
* the DXF exporter (polyline sampling).

This module is the single grammar both read. It resolves absolute and relative commands
(``M L H V C S Q T A Z`` and their lowercase forms) into canonical absolute segments, so
a path the freeze accepts can never be rejected by the exporter -- or vice versa -- for
having "another grammar". A consumer that needs points along the drawing samples the
segments; a consumer that needs extents reads endpoints, controls and arc corners.

The endpoint-to-centre parameterisation of elliptical arcs follows the SVG specification
(F.6.5), the same math the containment proof uses: radii scale up when they cannot span
the chord, and a degenerate radius draws a straight line.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from dataclasses import dataclass

_PATH_TOKEN = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])|(-?\d*\.?\d+(?:[eE][-+]?\d+)?)")

#: The SVG path commands the catalogue may use, with how many numbers each takes.
PATH_COMMAND_ARITY: dict[str, int] = {
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

_QUAD_SAMPLE_STEPS = 8
_CUBIC_SAMPLE_STEPS = 8
_ARC_SAMPLE_STEPS = 16


class SymbolPathError(ValueError):
    """A symbol path ``d`` string is not interpretable under the shared grammar."""


@dataclass(frozen=True)
class PathSegment:
    """One canonical segment, fully absolute.

    ``controls`` holds the quadratic control point (one) or the cubic control points
    (two). ``arc`` is ``(radius_x, radius_y, rotation_degrees, large_arc, sweep)`` for an
    elliptical arc segment and ``None`` otherwise.
    """

    kind: str  # "line" | "quad" | "cubic" | "arc"
    start: tuple[float, float]
    end: tuple[float, float]
    controls: tuple[tuple[float, float], ...] = ()
    arc: tuple[float, float, float, bool, bool] | None = None


#: One grammar event: a pen move, a drawing segment, or a close-path back to the
#: subpath start (emitted even when the pen is already there, so extent readers see
#: exactly what the freeze has always measured).
PathEvent = (
    tuple[str, tuple[float, float]]
    | tuple[str, PathSegment]
    | tuple[str, tuple[float, float]]
)


def _walk_path(described: str) -> Iterator[PathEvent]:
    """The single traversal every consumer reads.

    Yields ``("move", point)`` for ``M`` (implicit repeats included) and ``("segment",
    segment)`` for every drawing command, in path order. Raises
    :class:`SymbolPathError` for anything the grammar cannot read: a leading number, a
    truncated command, or a command where a coordinate belongs -- the same failures the
    freeze reports as out-of-bounds, so a catalogue path is checked against exactly one
    legality notion, whichever consumer reads it first.
    """

    tokens = [match.group(1) or match.group(2) for match in _PATH_TOKEN.finditer(described)]
    if "".join(tokens).replace(".", "").replace("-", "").replace("+", "").isdigit():
        raise SymbolPathError(f"path {described!r} is missing commands")

    current = (0.0, 0.0)
    subpath_start = (0.0, 0.0)
    quad_control: tuple[float, float] | None = None
    cubic_control: tuple[float, float] | None = None
    command = ""
    index = 0

    def take(count: int) -> list[float]:
        nonlocal index
        if index + count > len(tokens):
            raise SymbolPathError(f"path {described!r} ends mid-command for {command!r}")
        values: list[float] = []
        for offset in range(count):
            token = tokens[index + offset]
            if token.isalpha():
                raise SymbolPathError(
                    f"path {described!r} has a command where a coordinate belongs"
                )
            values.append(float(token))
        index += count
        return values

    def resolve(point: tuple[float, float], origin: tuple[float, float], relative: bool) -> tuple[float, float]:
        if relative:
            return (origin[0] + point[0], origin[1] + point[1])
        return point

    while index < len(tokens):
        token = tokens[index]
        if token.isalpha():
            command = token
            index += 1
            if command.upper() == "Z":
                current = subpath_start
                yield ("close", subpath_start)
                quad_control = None
                cubic_control = None
                continue
        if not command:
            raise SymbolPathError(
                f"path {described!r} starts with a number rather than a command"
            )
        upper = command.upper()
        relative = command.islower()
        origin = current

        if upper == "M":
            xy = take(2)
            current = (origin[0] + xy[0], origin[1] + xy[1]) if relative else (xy[0], xy[1])
            subpath_start = current
            quad_control = None
            cubic_control = None
            yield ("move", current)
        elif upper == "L":
            xy = take(2)
            target = resolve((xy[0], xy[1]), origin, relative)
            yield ("segment", PathSegment("line", current, target))
            current = target
            quad_control = None
            cubic_control = None
        elif upper == "H":
            (x,) = take(1)
            target = (origin[0] + x, current[1]) if relative else (x, current[1])
            yield ("segment", PathSegment("line", current, target))
            current = target
            quad_control = None
            cubic_control = None
        elif upper == "V":
            (y,) = take(1)
            target = (current[0], origin[1] + y) if relative else (current[0], y)
            yield ("segment", PathSegment("line", current, target))
            current = target
            quad_control = None
            cubic_control = None
        elif upper == "C":
            numbers = take(6)
            c1 = resolve((numbers[0], numbers[1]), origin, relative)
            c2 = resolve((numbers[2], numbers[3]), origin, relative)
            target = resolve((numbers[4], numbers[5]), origin, relative)
            yield ("segment", PathSegment("cubic", current, target, (c1, c2)))
            current = target
            cubic_control = c2
            quad_control = None
        elif upper == "S":
            numbers = take(4)
            if cubic_control is not None:
                c1 = (2 * current[0] - cubic_control[0], 2 * current[1] - cubic_control[1])
            else:
                c1 = current
            c2 = resolve((numbers[0], numbers[1]), origin, relative)
            target = resolve((numbers[2], numbers[3]), origin, relative)
            yield ("segment", PathSegment("cubic", current, target, (c1, c2)))
            current = target
            cubic_control = c2
            quad_control = None
        elif upper == "Q":
            numbers = take(4)
            control = resolve((numbers[0], numbers[1]), origin, relative)
            target = resolve((numbers[2], numbers[3]), origin, relative)
            yield ("segment", PathSegment("quad", current, target, (control,)))
            current = target
            quad_control = control
            cubic_control = None
        elif upper == "T":
            numbers = take(2)
            if quad_control is not None:
                control = (2 * current[0] - quad_control[0], 2 * current[1] - quad_control[1])
            else:
                control = current
            target = resolve((numbers[0], numbers[1]), origin, relative)
            yield ("segment", PathSegment("quad", current, target, (control,)))
            current = target
            quad_control = control
            cubic_control = None
        elif upper == "A":
            numbers = take(7)
            radius_x, radius_y = abs(numbers[0]), abs(numbers[1])
            rotation = numbers[2]
            large_arc, sweep = numbers[3] != 0.0, numbers[4] != 0.0
            target = resolve((numbers[5], numbers[6]), origin, relative)
            yield (
                "segment",
                PathSegment(
                    "arc",
                    current,
                    target,
                    (),
                    (radius_x, radius_y, rotation, large_arc, sweep),
                ),
            )
            current = target
            quad_control = None
            cubic_control = None
        else:  # pragma: no cover - the arity table and the loop guard keep this dead
            raise SymbolPathError(f"unsupported symbol path command: {command!r}")


def parse_symbol_path(described: str) -> tuple[PathSegment, ...]:
    """The canonical absolute drawing segments one ``d`` string draws."""

    return tuple(
        event[1] for event in _walk_path(described) if event[0] == "segment"
    )


def arc_center_parameters(
    *,
    start: tuple[float, float],
    end: tuple[float, float],
    radius_x: float,
    radius_y: float,
    rotation: float,
    large_arc: bool,
    sweep: bool,
) -> tuple[tuple[float, float], float, float, float, float, bool] | None:
    """Centre and angle span of an elliptical arc, or ``None`` when it degenerates.

    Returns ``(centre, radius_x, radius_y, start_angle, angle_delta, counter_clockwise)``
    where angles are radians in the unrotated ellipse frame and ``angle_delta`` is the
    signed sweep from start to end. ``None`` means the arc draws as a straight line: a
    zero radius, or endpoints that coincide.
    """

    if radius_x == 0.0 or radius_y == 0.0:
        return None
    if abs(start[0] - end[0]) < 1e-12 and abs(start[1] - end[1]) < 1e-12:
        return None
    cos_rotation, sin_rotation = math.cos(rotation), math.sin(rotation)
    half_dx = (start[0] - end[0]) / 2
    half_dy = (start[1] - end[1]) / 2
    x1 = cos_rotation * half_dx + sin_rotation * half_dy
    y1 = -sin_rotation * half_dx + cos_rotation * half_dy
    rx, ry = radius_x, radius_y
    scale = (x1 / rx) ** 2 + (y1 / ry) ** 2
    if scale > 1.0:
        factor = math.sqrt(scale)
        rx *= factor
        ry *= factor
    denominator = (rx * y1) ** 2 + (ry * x1) ** 2
    numerator = (rx * ry) ** 2 - denominator
    coefficient = math.sqrt(max(0.0, numerator / denominator)) if denominator else 0.0
    if large_arc == sweep:
        coefficient = -coefficient
    centre_x = (
        cos_rotation * coefficient * rx * y1 / ry
        - sin_rotation * coefficient * ry * x1 / rx
        + (start[0] + end[0]) / 2
    )
    centre_y = (
        sin_rotation * coefficient * rx * y1 / ry
        + cos_rotation * coefficient * ry * x1 / rx
        + (start[1] + end[1]) / 2
    )

    def frame_angle(point: tuple[float, float]) -> float:
        dx = point[0] - centre_x
        dy = point[1] - centre_y
        local_x = (cos_rotation * dx + sin_rotation * dy) / rx
        local_y = (-sin_rotation * dx + cos_rotation * dy) / ry
        return math.atan2(local_y, local_x)

    start_angle = frame_angle(start)
    end_angle = frame_angle(end)
    delta = end_angle - start_angle
    counter_clockwise = not sweep
    if counter_clockwise:
        while delta >= 0.0:
            delta -= 2 * math.pi
    else:
        while delta <= 0.0:
            delta += 2 * math.pi
    return (centre_x, centre_y), rx, ry, start_angle, delta, counter_clockwise


def sample_symbol_path(described: str) -> list[tuple[float, float]]:
    """Ordered polyline points along a path, for the DXF exporter.

    Line segments contribute their endpoint; quadratic and cubic curves are sampled
    along their control polygons; arcs are sampled along the ellipse they parameterise.
    A close-path yields the closing point, so a closed path's points start and end at
    the same coordinate.
    """

    points: list[tuple[float, float]] = []
    for event in _walk_path(described):
        if event[0] == "move":
            points.append(event[1])
            continue
        if event[0] == "close":
            if points and points[-1] != event[1]:
                points.append(event[1])
            continue
        segment: PathSegment = event[1]
        if segment.kind == "line":
            points.append(segment.end)
        elif segment.kind == "quad":
            (control,) = segment.controls
            origin = segment.start
            for step in range(1, _QUAD_SAMPLE_STEPS + 1):
                t = step / _QUAD_SAMPLE_STEPS
                points.append(
                    (
                        (1 - t) ** 2 * origin[0] + 2 * (1 - t) * t * control[0] + t**2 * segment.end[0],
                        (1 - t) ** 2 * origin[1] + 2 * (1 - t) * t * control[1] + t**2 * segment.end[1],
                    )
                )
        elif segment.kind == "cubic":
            c1, c2 = segment.controls
            origin = segment.start
            for step in range(1, _CUBIC_SAMPLE_STEPS + 1):
                t = step / _CUBIC_SAMPLE_STEPS
                mt = 1 - t
                points.append(
                    (
                        mt**3 * origin[0] + 3 * mt**2 * t * c1[0] + 3 * mt * t**2 * c2[0] + t**3 * segment.end[0],
                        mt**3 * origin[1] + 3 * mt**2 * t * c1[1] + 3 * mt * t**2 * c2[1] + t**3 * segment.end[1],
                    )
                )
        elif segment.kind == "arc":
            assert segment.arc is not None
            radius_x, radius_y, rotation_deg, large_arc, sweep = segment.arc
            parameters = arc_center_parameters(
                start=segment.start,
                end=segment.end,
                radius_x=radius_x,
                radius_y=radius_y,
                rotation=math.radians(rotation_deg),
                large_arc=large_arc,
                sweep=sweep,
            )
            if parameters is None:
                points.append(segment.end)
                continue
            (centre_x, centre_y), rx, ry, start_angle, delta, _ = parameters
            cos_rotation = math.cos(math.radians(rotation_deg))
            sin_rotation = math.sin(math.radians(rotation_deg))
            for step in range(1, _ARC_SAMPLE_STEPS + 1):
                angle = start_angle + delta * step / _ARC_SAMPLE_STEPS
                local_x = rx * math.cos(angle)
                local_y = ry * math.sin(angle)
                points.append(
                    (
                        centre_x + cos_rotation * local_x - sin_rotation * local_y,
                        centre_y + sin_rotation * local_x + cos_rotation * local_y,
                    )
                )
    return points


def path_points(described: str) -> tuple[tuple[float, float], ...]:
    """Every point a path's ``d`` touches, control points included.

    This is the extent semantics the M7 freeze measures: move targets, segment
    endpoints, curve controls, and the axis-aligned corners of each arc's bounding
    ellipse.
    """

    points: list[tuple[float, float]] = []
    for event in _walk_path(described):
        if event[0] == "move":
            points.append(event[1])
            continue
        if event[0] == "close":
            # The freeze appends the subpath start unconditionally on Z, even when the
            # pen is already there; extents are a union, so the duplicate is harmless
            # there and preserved here for exact parity.
            points.append(event[1])
            continue
        segment: PathSegment = event[1]
        if segment.kind == "arc":
            assert segment.arc is not None
            radius_x, radius_y, rotation_deg, large_arc, sweep = segment.arc
            for corner in arc_extent_corners(
                start=segment.start,
                end=segment.end,
                radius_x=radius_x,
                radius_y=radius_y,
                rotation=math.radians(rotation_deg),
                large_arc=large_arc,
                sweep=sweep,
            ):
                points.append(corner)
            # The freeze appends the endpoint once more after the corners (which already
            # contain it); extents are a union, so the duplicate is preserved here for
            # exact parity rather than "cleaned up" into a third semantics.
            points.append(segment.end)
        else:
            for control in segment.controls:
                points.append(control)
            points.append(segment.end)
    if not points:
        raise SymbolPathError(f"path {described!r} has no geometry")
    return tuple(points)


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
    """The axis-aligned bounding corners of an arc's ellipse, plus its endpoints.

    Identical math to the containment proof's ``arc_extent_corners``: the centre is
    recovered per SVG F.6.5, radii scale up when too small to span the chord, and a
    degenerate radius contributes only the endpoints.
    """

    if radius_x == 0.0 or radius_y == 0.0:
        return (start, end)
    cos_rotation, sin_rotation = math.cos(rotation), math.sin(rotation)
    half_dx = (start[0] - end[0]) / 2
    half_dy = (start[1] - end[1]) / 2
    x1 = cos_rotation * half_dx + sin_rotation * half_dy
    y1 = -sin_rotation * half_dx + cos_rotation * half_dy
    rx, ry = radius_x, radius_y
    scale = (x1 / rx) ** 2 + (y1 / ry) ** 2
    if scale > 1.0:
        factor = math.sqrt(scale)
        rx *= factor
        ry *= factor
    denominator = (rx * y1) ** 2 + (ry * x1) ** 2
    numerator = (rx * ry) ** 2 - denominator
    coefficient = math.sqrt(max(0.0, numerator / denominator)) if denominator else 0.0
    if large_arc == sweep:
        coefficient = -coefficient
    centre_x = (
        cos_rotation * coefficient * rx * y1 / ry
        - sin_rotation * coefficient * ry * x1 / rx
        + (start[0] + end[0]) / 2
    )
    centre_y = (
        sin_rotation * coefficient * rx * y1 / ry
        + cos_rotation * coefficient * ry * x1 / rx
        + (start[1] + end[1]) / 2
    )
    half_x = math.sqrt((rx * cos_rotation) ** 2 + (ry * sin_rotation) ** 2)
    half_y = math.sqrt((rx * sin_rotation) ** 2 + (ry * cos_rotation) ** 2)
    return (
        (centre_x - half_x, centre_y - half_y),
        (centre_x + half_x, centre_y + half_y),
        start,
        end,
    )


__all__ = [
    "PATH_COMMAND_ARITY",
    "PathEvent",
    "PathSegment",
    "SymbolPathError",
    "arc_center_parameters",
    "arc_extent_corners",
    "parse_symbol_path",
    "path_points",
    "sample_symbol_path",
]
