"""Minimal DXF authoring helpers for the CAD import tests.

A DXF is a documented, line-oriented format, so a test can write the exact records it
wants to exercise instead of relying on an external writer. That matters here: the
interesting cases are the awkward ones (bulges, mirrored inserts, array inserts, hatch
boundaries, dimension blocks, codepage-encoded text), and those are far easier to state
directly than to produce through a CAD application.
"""

from __future__ import annotations

import math

Pair = tuple[int, object]


def pairs(*items: Pair) -> list[Pair]:
    return list(items)


class DxfBuilder:
    """Build a small ASCII DXF stream for tests."""

    def __init__(self) -> None:
        self.header: list[Pair] = []
        self.layers: dict[str, int] = {"0": 7}
        self.blocks: list[tuple[str, list[list[Pair]]]] = []
        self.entities: list[list[Pair]] = []
        self.codepage = "UTF-8"

    # -- structure ---------------------------------------------------------- #

    def codepage_value(self, name: str) -> DxfBuilder:
        self.codepage = name
        return self

    def extents(self, box: tuple[float, float, float, float]) -> DxfBuilder:
        x0, y0, x1, y1 = box
        self.header.extend([(9, "$EXTMIN"), (10, x0), (20, y0), (30, 0.0)])
        self.header.extend([(9, "$EXTMAX"), (10, x1), (20, y1), (30, 0.0)])
        return self

    def layer(self, name: str, color: int = 7) -> DxfBuilder:
        self.layers[name] = color
        return self

    def block(self, name: str, entities: list[list[Pair]]) -> DxfBuilder:
        self.blocks.append((name, entities))
        return self

    def add(self, kind: str, items: list[Pair]) -> list[Pair]:
        record = [(0, kind), *items]
        self.entities.append(record)
        return record

    # -- entities ----------------------------------------------------------- #

    def line(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        layer: str = "0",
        color: int | None = None,
        linetype: str | None = None,
    ) -> list[Pair]:
        items: list[Pair] = [(8, layer)]
        if linetype:
            items.append((6, linetype))
        if color is not None:
            items.append((62, color))
        items.extend([(10, start[0]), (20, start[1]), (11, end[0]), (21, end[1])])
        return self.add("LINE", items)

    def lwpolyline(
        self,
        points: list[tuple[float, float]],
        *,
        closed: bool = False,
        bulges: list[float] | None = None,
        layer: str = "0",
    ) -> list[Pair]:
        items: list[Pair] = [(8, layer), (90, len(points)), (70, 1 if closed else 0)]
        for index, (x, y) in enumerate(points):
            items.append((10, x))
            items.append((20, y))
            if bulges and index < len(bulges) and bulges[index]:
                items.append((42, bulges[index]))
        return self.add("LWPOLYLINE", items)

    def polyline(
        self,
        points: list[tuple[float, float]],
        *,
        closed: bool = False,
        layer: str = "0",
        bulges: list[float] | None = None,
    ) -> list[Pair]:
        """An old-style POLYLINE: the head record is followed by VERTEX records.

        The head carries a dummy 10/20 pair, exactly like a real file, so a reader that
        forgets to skip it creates a spurious vertex at the origin.
        """

        items: list[Pair] = [(8, layer), (66, 1), (10, 0.0), (20, 0.0), (30, 0.0), (70, 1 if closed else 0)]
        self.add("POLYLINE", items)
        for index, (x, y) in enumerate(points):
            vertex: list[Pair] = [(8, layer), (10, x), (20, y), (30, 0.0)]
            if bulges and index < len(bulges) and bulges[index]:
                vertex.append((42, bulges[index]))
            self.add("VERTEX", vertex)
        self.add("SEQEND", [(8, layer)])
        return items

    def circle(
        self,
        center: tuple[float, float],
        radius: float,
        *,
        layer: str = "0",
        color: int | None = None,
    ) -> list[Pair]:
        items: list[Pair] = [(8, layer)]
        if color is not None:
            items.append((62, color))
        items.extend([(10, center[0]), (20, center[1]), (30, 0.0), (40, radius)])
        return self.add("CIRCLE", items)

    def arc(
        self,
        center: tuple[float, float],
        radius: float,
        start_degrees: float,
        end_degrees: float,
        *,
        layer: str = "0",
    ) -> list[Pair]:
        return self.add(
            "ARC",
            [
                (8, layer),
                (10, center[0]),
                (20, center[1]),
                (30, 0.0),
                (40, radius),
                (50, start_degrees),
                (51, end_degrees),
            ],
        )

    def ellipse(
        self,
        center: tuple[float, float],
        major: tuple[float, float],
        ratio: float,
        *,
        start: float = 0.0,
        end: float = math.tau,
        layer: str = "0",
    ) -> list[Pair]:
        return self.add(
            "ELLIPSE",
            [
                (8, layer),
                (10, center[0]),
                (20, center[1]),
                (30, 0.0),
                (11, major[0]),
                (21, major[1]),
                (31, 0.0),
                (40, ratio),
                (41, start),
                (42, end),
            ],
        )

    def text(
        self,
        value: str,
        position: tuple[float, float],
        height: float,
        *,
        halign: int = 0,
        valign: int = 0,
        alignment: tuple[float, float] | None = None,
        rotation: float = 0.0,
        layer: str = "0",
    ) -> list[Pair]:
        items: list[Pair] = [
            (8, layer),
            (10, position[0]),
            (20, position[1]),
            (30, 0.0),
            (40, height),
            (1, value),
            (50, rotation),
            (72, halign),
            (73, valign),
        ]
        if alignment is not None:
            items.extend([(11, alignment[0]), (21, alignment[1]), (31, 0.0)])
        return self.add("TEXT", items)

    def mtext(
        self,
        value: str,
        position: tuple[float, float],
        height: float,
        *,
        attachment: int = 1,
        rotation: float = 0.0,
        layer: str = "0",
        chunks: list[str] | None = None,
    ) -> list[Pair]:
        items: list[Pair] = [(8, layer), (10, position[0]), (20, position[1]), (30, 0.0), (40, height)]
        for chunk in chunks or []:
            items.append((3, chunk))
        items.extend([(1, value), (71, attachment), (50, rotation), (7, "Standard")])
        return self.add("MTEXT", items)

    def solid(self, corners: list[tuple[float, float]], *, layer: str = "0") -> list[Pair]:
        items: list[Pair] = [(8, layer)]
        for index, (x, y) in enumerate(corners[:4]):
            base = 10 + index
            items.extend([(base, x), (base + 10, y), (base + 20, 0.0)])
        return self.add("SOLID", items)

    def insert(
        self,
        name: str,
        position: tuple[float, float],
        *,
        scale: tuple[float, float] = (1.0, 1.0),
        rotation: float = 0.0,
        columns: int = 1,
        rows: int = 1,
        column_spacing: float = 0.0,
        row_spacing: float = 0.0,
        color: int | None = None,
        layer: str = "0",
    ) -> list[Pair]:
        items: list[Pair] = [(8, layer), (2, name), (10, position[0]), (20, position[1]), (30, 0.0)]
        items.extend([(41, scale[0]), (42, scale[1]), (43, 1.0), (50, rotation)])
        if columns > 1:
            items.extend([(70, columns), (44, column_spacing)])
        if rows > 1:
            items.extend([(71, rows), (45, row_spacing)])
        if color is not None:
            items.append((62, color))
        return self.add("INSERT", items)

    def dimension(self, block_name: str, position: tuple[float, float] = (0.0, 0.0)) -> list[Pair]:
        return self.add(
            "DIMENSION",
            [
                (8, "0"),
                (2, block_name),
                (10, position[0]),
                (20, position[1]),
                (30, 0.0),
                (70, 0),
            ],
        )

    def hatch(
        self,
        loops: list[list[tuple[float, float]]],
        *,
        solid: bool = True,
        pattern: str = "SOLID",
        layer: str = "0",
    ) -> list[Pair]:
        items: list[Pair] = [
            (8, layer),
            (10, 0.0),
            (20, 0.0),
            (30, 0.0),
            (2, pattern),
            (70, 1 if solid else 0),
            (71, 0),
            (91, len(loops)),
        ]
        for loop in loops:
            items.append((92, 2))
            items.append((72, 0))
            items.append((73, 1))
            items.append((93, len(loop)))
            for x, y in loop:
                items.extend([(10, x), (20, y)])
        return self.add("HATCH", items)

    def unknown(self, kind: str, items: list[Pair] | None = None) -> list[Pair]:
        return self.add(kind, items or [(8, "0"), (10, 0.0), (20, 0.0)])

    def rect_block(
        self,
        name: str,
        *,
        corner: tuple[float, float] = (-20.0, -20.0),
        size: tuple[float, float] = (40.0, 40.0),
        layer: str = "0",
    ) -> DxfBuilder:
        """Define a block whose geometry is a rectangle — enough to detect a transform."""

        x0, y0 = corner
        width, height = size
        corners = [(x0, y0), (x0 + width, y0), (x0 + width, y0 + height), (x0, y0 + height)]
        lines: list[list[Pair]] = []
        for index, start in enumerate(corners):
            end = corners[(index + 1) % len(corners)]
            lines.append(
                [
                    (0, "LINE"),
                    (8, layer),
                    (10, start[0]),
                    (20, start[1]),
                    (30, 0.0),
                    (11, end[0]),
                    (21, end[1]),
                    (31, 0.0),
                ]
            )
        return self.block(name, lines)

    # -- output ------------------------------------------------------------- #

    def build(self) -> bytes:
        return self.source().encode("utf-8")

    def source(self) -> str:
        """Render the DXF stream as text (the tests sometimes assert on it directly)."""

        lines: list[str] = []
        lines.extend(["0", "SECTION", "2", "HEADER"])
        for code, value in [(9, "$ACADVER"), (1, "AC1032"), (9, "$DWGCODEPAGE"), (3, self.codepage)]:
            lines.extend([str(code), str(value)])
        for code, value in self.header:
            lines.extend([str(code), str(value)])
        lines.extend(["0", "ENDSEC"])

        layer_records: list[tuple[int, object]] = []
        for name, color in self.layers.items():
            layer_records.append((0, "LAYER"))
            layer_records.append((2, name))
            layer_records.append((70, 0))
            layer_records.append((62, color))
            layer_records.append((6, "CONTINUOUS"))
        lines.extend(["0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER"])
        for code, value in layer_records:
            lines.extend([str(code), str(value)])
        lines.extend(["0", "ENDTAB", "0", "ENDSEC"])

        lines.extend(["0", "SECTION", "2", "BLOCKS"])
        for name, entities in self.blocks:
            lines.extend(["0", "BLOCK", "8", "0", "2", name, "70", "0", "10", "0.0", "20", "0.0", "30", "0.0"])
            for record in entities:
                for code, value in record:
                    lines.extend([str(code), str(value)])
            lines.extend(["0", "ENDBLK", "8", "0"])
        lines.extend(["0", "ENDSEC"])

        lines.extend(["0", "SECTION", "2", "ENTITIES"])
        for record in self.entities:
            for code, value in record:
                lines.extend([str(code), str(value)])
        lines.extend(["0", "ENDSEC", "0", "EOF"])
        return "\n".join(lines) + "\n"


class ObjectStreamBuilder:
    """Build a LibreDWG-style object dump for the DWG decoder tests.

    The shapes here mirror what ``dwgread -O JSON`` emits for a real drawing (verified
    against a production sheet): handles are ``[code, size, value, absolute]`` reference
    lists, colours are ``{"index": n, "rgb": "rrggbb"}`` dictionaries, and block
    references point at ``BLOCK_HEADER`` objects that own their entities.
    """

    def __init__(self) -> None:
        self.objects: list[dict] = []
        self._next_handle = 0x100
        self.version = "AC1032"

    # -- handles ------------------------------------------------------------ #

    def _handle(self) -> int:
        self._next_handle += 1
        return self._next_handle

    @staticmethod
    def ref(handle: int) -> list[int]:
        return [5, 2, handle, handle]

    def add(self, obj: dict) -> int:
        handle = self._handle()
        obj["handle"] = self.ref(handle)
        self.objects.append(obj)
        return handle

    # -- tables and blocks -------------------------------------------------- #

    def layer(self, name: str, color_index: int = 7, rgb: str | None = None) -> int:
        return self.add(
            {
                "object": "LAYER",
                "name": name,
                "color": {"index": color_index, "rgb": rgb or "000000"},
            }
        )

    def block(self, name: str, entities: list[int]) -> int:
        return self.add(
            {
                "object": "BLOCK_HEADER",
                "name": name,
                "entities": [self.ref(handle) for handle in entities],
            }
        )

    def model_space(self, entities: list[int]) -> int:
        return self.block("*Model_Space", entities)

    # -- entities ----------------------------------------------------------- #

    def entity(self, kind: str, **fields: object) -> int:
        obj: dict = {"entity": kind}
        obj.update(fields)
        return self.add(obj)

    def line(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        layer: int | None = None,
        color: int | None = None,
        rgb: str | None = None,
        invisible: bool = False,
        **extra: object,
    ) -> int:
        fields: dict = {
            "start": [start[0], start[1], 0.0],
            "end": [end[0], end[1], 0.0],
            "invisible": int(invisible),
        }
        self._style(fields, layer, color, rgb, **extra)
        return self.entity("LINE", **fields)

    def circle(self, center: tuple[float, float], radius: float, **style: object) -> int:
        fields: dict = {"center": [center[0], center[1], 0.0], "radius": radius}
        self._style(fields, **style)  # type: ignore[arg-type]
        return self.entity("CIRCLE", **fields)

    def arc(
        self,
        center: tuple[float, float],
        radius: float,
        start: float,
        end: float,
        **style: object,
    ) -> int:
        fields: dict = {
            "center": [center[0], center[1], 0.0],
            "radius": radius,
            "start_angle": start,
            "end_angle": end,
        }
        self._style(fields, **style)  # type: ignore[arg-type]
        return self.entity("ARC", **fields)

    def ellipse(
        self,
        center: tuple[float, float],
        major: tuple[float, float],
        ratio: float,
        **style: object,
    ) -> int:
        fields: dict = {
            "center": [center[0], center[1], 0.0],
            "sm_axis": [major[0], major[1], 0.0],
            "axis_ratio": ratio,
            "start_angle": 0.0,
            "end_angle": math.tau,
        }
        self._style(fields, **style)  # type: ignore[arg-type]
        return self.entity("ELLIPSE", **fields)

    def lwpolyline(
        self,
        points: list[tuple[float, float]],
        *,
        closed: bool = False,
        **style: object,
    ) -> int:
        fields: dict = {
            "points": [[x, y] for x, y in points],
            "flag": 1 if closed else 0,
        }
        self._style(fields, **style)  # type: ignore[arg-type]
        return self.entity("LWPOLYLINE", **fields)

    def polyline_2d(
        self,
        points: list[tuple[float, float]],
        *,
        closed: bool = False,
        **style: object,
    ) -> int:
        vertex_handles: list[int] = []
        for x, y in points:
            vertex = self.add(
                {
                    "entity": "VERTEX_2D",
                    "point": [x, y, 0.0],
                    "bulge": 0.0,
                    "flag": 0,
                }
            )
            vertex_handles.append(vertex)
        fields: dict = {
            "flag": 1 if closed else 0,
            "_vertices": vertex_handles,
        }
        self._style(fields, **style)  # type: ignore[arg-type]
        handle = self.entity("POLYLINE_2D", **fields)
        # Vertices point back at their owner, which is how the decoder finds them.
        for vertex in vertex_handles:
            for obj in self.objects:
                if obj.get("handle", [0, 0, 0, 0])[-1] == vertex:
                    obj["ownerhandle"] = self.ref(handle)
        return handle

    def text(
        self,
        value: str,
        position: tuple[float, float],
        height: float,
        *,
        horizontal: int = 0,
        rotation: float = 0.0,
        alignment: tuple[float, float] | None = None,
        **style: object,
    ) -> int:
        fields: dict = {
            "text_value": value,
            "ins_pt": [position[0], position[1], 0.0],
            "height": height,
            "horiz_alignment": horizontal,
            "vert_alignment": 0,
            "rotation": rotation,
        }
        if alignment is not None:
            fields["alignment_pt"] = [alignment[0], alignment[1], 0.0]
        self._style(fields, **style)  # type: ignore[arg-type]
        return self.entity("TEXT", **fields)

    def mtext(
        self,
        value: str,
        position: tuple[float, float],
        height: float,
        *,
        attachment: int = 1,
        **style: object,
    ) -> int:
        fields: dict = {
            "text": value,
            "ins_pt": [position[0], position[1], 0.0],
            "text_height": height,
            "attachment": attachment,
            "rotation": 0.0,
        }
        self._style(fields, **style)  # type: ignore[arg-type]
        return self.entity("MTEXT", **fields)

    def insert(
        self,
        block_handle: int,
        position: tuple[float, float],
        *,
        scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
        rotation: float = 0.0,
        **style: object,
    ) -> int:
        # ``rotation`` is radians, matching the DWG object dump (the binary stores
        # radians; DXF group code 50 is the degrees form).
        fields: dict = {
            "block_header": self.ref(block_handle),
            "ins_pt": [position[0], position[1], 0.0],
            "scale": [scale[0], scale[1], scale[2] if len(scale) > 2 else 1.0],
            "rotation": rotation,
        }
        self._style(fields, **style)  # type: ignore[arg-type]
        return self.entity("INSERT", **fields)

    def solid(
        self,
        corners: list[tuple[float, float]],
        **style: object,
    ) -> int:
        fields: dict = {}
        for index, (x, y) in enumerate(corners[:4]):
            fields[f"corner{index + 1}"] = [x, y, 0.0]
        self._style(fields, **style)  # type: ignore[arg-type]
        return self.entity("SOLID", **fields)

    def hatch(self, **style: object) -> int:
        fields: dict = {"paths": []}
        self._style(fields, **style)  # type: ignore[arg-type]
        return self.entity("HATCH", **fields)

    def unknown(self, kind: str, **style: object) -> int:
        return self.entity(kind, **style)

    def _style(
        self,
        fields: dict,
        layer: int | None = None,
        color: int | None = None,
        rgb: str | None = None,
        extraction: tuple[float, float, float] | None = None,
        **extra: object,
    ) -> None:
        if layer is not None:
            fields["layer"] = self.ref(layer)
        if color is not None:
            fields["color"] = {"index": color, "rgb": rgb or "000000"}
        if extraction is not None:
            fields["extrusion"] = list(extraction)

    # -- output ------------------------------------------------------------- #

    def build(self) -> dict:
        return {
            "created_by": "LibreDWG fixture",
            "FILEHEADER": {"version": self.version},
            "OBJECTS": self.objects,
        }


def simple_dxf() -> bytes:
    """A tiny but complete drawing: two layers, geometry, text and a block insert."""

    builder = DxfBuilder()
    builder.layer("PIPE", color=3)
    builder.layer("EQUIP", color=5)
    builder.rect_block("VALVE")
    builder.line((0.0, 0.0), (1000.0, 0.0), layer="PIPE")
    builder.circle((500.0, 200.0), 50.0, layer="EQUIP")
    builder.text("P-101", (100.0, 300.0), 40.0, layer="EQUIP")
    builder.insert("VALVE", (250.0, 0.0))
    builder.extents((0.0, 0.0, 1000.0, 400.0))
    return builder.build()


if __name__ == "__main__":  # pragma: no cover - manual fixture inspection
    print(simple_dxf().decode("utf-8"))
