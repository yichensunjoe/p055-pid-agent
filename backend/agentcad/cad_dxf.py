"""Read a DXF stream with our own group-code reader.

Charter reference: §14 (drawing intake), §21.6 (reviewability).

Why not a library: the runtime dependency list is deliberately small (``ezdxf`` is a
test-only extra), and a DXF is a documented, line-oriented format. This reader
implements the subset a P&ID sheet uses — LINE, LWPOLYLINE/POLYLINE (with bulges),
CIRCLE, ARC, ELLIPSE, SOLID/TRACE, TEXT/MTEXT, INSERT (with array rows/columns and
mirrored instances), HATCH boundaries, DIMENSION/LEADER geometry — and reports every
entity type it does not decode instead of dropping it.

Two behaviours are worth knowing when reading the code:

* **Encoding is resolved from the file.** Drawings from Chinese CAD installations are
  commonly GBK-encoded, and their text is not decoration. ``$DWGCODEPAGE`` is honoured
  and the result is recorded in the report.
* **Paper space is not imported.** Model space is the drawing; layout sheets are a
  different frame. The count of skipped layout entities is reported so nobody has to
  guess whether something went missing.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass

from .cad_geometry import (
    DEFAULT_INK,
    Affine,
    CadDecodeResult,
    CadGeometryError,
    CadPrimitive,
    aci_color,
    bulge_to_arc,
    clean_text,
    fill_polygon,
    ocs_transform,
)

BINARY_DXF_MARKER = b"AutoCAD Binary DXF"

#: DXF ``$DWGCODEPAGE`` -> Python codec, for the encodings engineering drawings use.
CODEPAGE_CODECS: dict[str, str] = {
    "ANSI_874": "cp874",
    "ANSI_932": "shift_jis",
    "ANSI_936": "gbk",
    "ANSI_949": "cp949",
    "ANSI_950": "big5",
    "ANSI_1250": "cp1250",
    "ANSI_1251": "cp1251",
    "ANSI_1252": "cp1252",
    "ANSI_1253": "cp1253",
    "ANSI_1254": "cp1254",
    "ANSI_1255": "cp1255",
    "ANSI_1256": "cp1256",
    "ANSI_1257": "cp1257",
    "ANSI_1258": "cp1258",
    "UTF-8": "utf-8",
    "UTF8": "utf-8",
}

#: Entity types that carry no drawable 2D geometry, so not decoding them is not a loss.
IGNORABLE_ENTITIES = frozenset(
    {
        "SEQEND",
        "VERTEX",
        "ATTRIB",
        "ATTDEF",
        "POINT",
        "VIEWPORT",
        "BLOCK",
        "ENDBLK",
        "OLE2FRAME",
        "OLEFRAME",
        "WIPEOUT",
        "XLINE",
        "RAY",
        "PROXY",
        "SHAPE",
        "TOLERANCE",
        "REGION",
        "BODY",
        "3DSOLID",
        "HELIX",
        "ACAD_PROXY_ENTITY",
    }
)

@dataclass(frozen=True)
class DxfRecord:
    """One DXF entity/table record: its type plus every group code it carried."""

    kind: str
    pairs: tuple[tuple[int, str], ...]

    def first(self, code: int) -> str | None:
        for pair_code, value in self.pairs:
            if pair_code == code:
                return value
        return None

    def values(self, code: int) -> list[str]:
        return [value for pair_code, value in self.pairs if pair_code == code]

    def number(self, code: int, default: float = 0.0) -> float:
        raw = self.first(code)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def integer(self, code: int, default: int = 0) -> int:
        raw = self.first(code)
        if raw is None or raw == "":
            return default
        try:
            return int(float(raw))
        except ValueError:
            return default

    def has(self, code: int) -> bool:
        return self.first(code) is not None

    def point(self, x_code: int, y_code: int, *, required: bool = False) -> tuple[float, float] | None:
        raw_x, raw_y = self.first(x_code), self.first(y_code)
        if raw_x is None or raw_y is None:
            if required:
                raise CadGeometryError(
                    "dxf_missing_coordinate",
                    f"{self.kind} is missing coordinate group codes {x_code}/{y_code}",
                )
            return None
        try:
            return (float(raw_x), float(raw_y))
        except ValueError:
            return None


def tokenize_dxf(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(group code, value)`` pairs from DXF text."""

    lines = text.splitlines()
    total = len(lines)
    index = 0
    while index + 1 < total:
        raw_code = lines[index].strip()
        if not raw_code:
            index += 1
            continue
        value = lines[index + 1].strip()
        index += 2
        try:
            code = int(raw_code)
        except ValueError as exc:
            raise CadGeometryError(
                "dxf_group_code_invalid",
                f"expected a DXF group code, found {raw_code[:40]!r}",
            ) from exc
        yield code, value


def records_from_tokens(tokens: Iterator[tuple[int, str]]) -> Iterator[DxfRecord]:
    """Group a token stream into records at each ``0`` (entity type) boundary."""

    current: list[tuple[int, str]] = []
    for code, value in tokens:
        if code == 0:
            if current:
                yield DxfRecord(kind=current[0][1], pairs=tuple(current))
            current = [(code, value)]
        else:
            current.append((code, value))
    if current:
        yield DxfRecord(kind=current[0][1], pairs=tuple(current))


def decode_dxf_bytes(data: bytes) -> tuple[str, str]:
    """Decode DXF bytes, honouring ``$DWGCODEPAGE``; returns ``(text, codec)``."""

    if data.startswith(BINARY_DXF_MARKER) or data[:22].startswith(BINARY_DXF_MARKER):
        raise CadGeometryError(
            "binary_dxf_unsupported",
            "binary DXF is not supported; re-save the drawing as ASCII DXF",
        )
    probe = data.decode("latin-1", errors="replace")
    codec = ""
    for line in probe.splitlines():
        stripped = line.strip()
        if stripped.startswith("$DWGCODEPAGE"):
            marker = probe.find("$DWGCODEPAGE")
            window = probe[marker : marker + 200].splitlines()
            for candidate in window[1:6]:
                value = candidate.strip().upper()
                if value in CODEPAGE_CODECS:
                    codec = CODEPAGE_CODECS[value]
                    break
            break
    candidates = [codec] if codec else []
    candidates.extend(["utf-8", "gbk", "big5", "cp1252"])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return data.decode(candidate), candidate
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


class DxfDecoder:
    """Turn DXF records into primitives, expanding blocks and dimension geometry."""

    def __init__(
        self,
        tokens: list[tuple[int, str]],
        *,
        codec: str = "",
        max_block_depth: int = 12,
    ):
        self.tokens = tokens
        self.max_block_depth = max_block_depth
        self.result = CadDecodeResult(encoding=codec)
        self.header: dict[str, list[tuple[int, str]]] = {}
        self.layers: dict[str, tuple[int | None, str | None]] = {}
        self.blocks: dict[str, list[DxfRecord]] = {}
        self.entities: list[DxfRecord] = []
        self.paper_space_records = 0

    # -- structure ---------------------------------------------------------- #

    def _sections(self) -> dict[str, list[tuple[int, str]]]:
        """Slice the token stream into the sections this reader consumes.

        The splitting happens on tokens, not on records: a HEADER section carries no
        ``0`` pairs at all, so record grouping would swallow it whole.
        """

        sections: dict[str, list[tuple[int, str]]] = {}
        current = ""
        start = 0
        index = 0
        total = len(self.tokens)
        while index < total:
            code, value = self.tokens[index]
            if code == 0 and value == "SECTION":
                name = self.tokens[index + 1][1] if index + 1 < total else ""
                current = name.strip().upper()
                index += 2
                start = index
                continue
            if code == 0 and value == "ENDSEC":
                if current:
                    sections.setdefault(current, []).extend(self.tokens[start:index])
                current = ""
                index += 1
                continue
            index += 1
        if current:
            sections.setdefault(current, []).extend(self.tokens[start:index])
        return sections

    def split_sections(self) -> None:
        sections = self._sections()
        self._read_header(sections.get("HEADER", []))
        for record in records_from_tokens(iter(sections.get("TABLES", []))):
            if record.kind == "LAYER":
                self._layer_record(record)
        self._read_blocks(sections.get("BLOCKS", []))
        entities = list(records_from_tokens(iter(sections.get("ENTITIES", []))))
        index = 0
        total = len(entities)
        while index < total:
            record = entities[index]
            index += 1
            if record.kind == "POLYLINE":
                polyline, index = self._consume_polyline(record, index, entities)
                self.entities.append(polyline)
                continue
            self.entities.append(record)
        self.result.paper_space_skipped = self.paper_space_records

    def _read_header(self, tokens: list[tuple[int, str]]) -> None:
        index = 0
        total = len(tokens)
        while index < total:
            code, value = tokens[index]
            if code != 9:
                index += 1
                continue
            bucket: list[tuple[int, str]] = []
            index += 1
            while index < total and tokens[index][0] != 9:
                bucket.append(tokens[index])
                index += 1
            self.header[value.strip().upper()] = bucket

    def _layer_record(self, record: DxfRecord) -> None:
        name = (record.first(2) or "").strip()
        if not name:
            return
        index_value = record.integer(62) if record.has(62) else None
        true: str | None = None
        raw_true = record.first(420)
        if raw_true:
            try:
                true = "#" + f"{int(raw_true) & 0xFFFFFF:06x}"
            except ValueError:
                true = None
        self.layers[name] = (index_value, true)

    def _read_blocks(self, tokens: list[tuple[int, str]]) -> None:
        current: str | None = None
        for record in records_from_tokens(iter(tokens)):
            if record.kind == "BLOCK":
                name = (record.first(2) or "").strip()
                current = name or None
                if current:
                    self.blocks.setdefault(current, [])
                continue
            if record.kind == "ENDBLK":
                current = None
                continue
            if current is None:
                continue
            self.blocks[current].append(record)
            if current.startswith("*Paper_Space"):
                self.paper_space_records += 1

    def _consume_polyline(
        self,
        head: DxfRecord,
        index: int,
        entities: list[DxfRecord],
    ) -> tuple[DxfRecord, int]:
        """Fold the VERTEX records that follow a POLYLINE into one record.

        The POLYLINE head carries its own 10/20 pair, which is a dummy point for this
        entity type and would otherwise become a spurious vertex at the origin.
        """

        vertices: list[tuple[int, str]] = []
        total = len(entities)
        while index < total:
            record = entities[index]
            if record.kind == "SEQEND":
                index += 1
                break
            if record.kind != "VERTEX":
                break
            # A VERTEX record's 10/20 pair *is* the coordinate, so only its type marker
            # is dropped when folding it into the polyline record.
            vertices.extend((code, value) for code, value in record.pairs if code != 0)
            index += 1
        head_pairs = tuple(
            (code, value) for code, value in head.pairs if code not in {10, 20, 30}
        )
        merged = head_pairs + tuple(vertices)
        return DxfRecord(kind=head.kind, pairs=merged), index

    def header_extents(self) -> tuple[float, float, float, float] | None:
        low = self.header.get("$EXTMIN")
        high = self.header.get("$EXTMAX")
        if not low or not high:
            return None
        try:
            x0 = float(next(value for code, value in low if code == 10))
            y0 = float(next(value for code, value in low if code == 20))
            x1 = float(next(value for code, value in high if code == 10))
            y1 = float(next(value for code, value in high if code == 20))
        except (StopIteration, ValueError):
            return None
        if x1 <= x0 or y1 <= y0:
            return None
        return (x0, y0, x1, y1)

    # -- decoding ----------------------------------------------------------- #

    def decode(self) -> CadDecodeResult:
        self.split_sections()
        self.result.extents = self.header_extents()
        for record in self.entities:
            self._entity(record, Affine(), DEFAULT_INK, "", depth=0)
        if self.paper_space_records:
            self.result.issues.add(
                "CAD_PAPER_SPACE_SKIPPED",
                "layout (paper space) entities are not part of the drawing and were skipped",
                count=self.paper_space_records,
            )
        return self.result

    def _layer_name(self, record: DxfRecord) -> str:
        return (record.first(8) or "0").strip() or "0"

    def _layer_ink(self, name: str) -> str:
        entry = self.layers.get(name)
        if not entry:
            return DEFAULT_INK
        index, true = entry
        if true:
            return true
        if index is None or index == 7:
            return DEFAULT_INK
        return aci_color(index) or DEFAULT_INK

    def _color(self, record: DxfRecord, inherited: str, layer: str) -> str:
        true = record.first(420)
        if true:
            try:
                return "#" + f"{int(true) & 0xFFFFFF:06x}"
            except ValueError:
                pass
        if not record.has(62):
            return self._layer_ink(layer)
        index = record.integer(62, 256)
        if index == 0:
            return inherited
        if index == 256 or index == 7:
            return self._layer_ink(layer) if index == 256 else DEFAULT_INK
        return aci_color(index) or self._layer_ink(layer)

    def _linetype(self, record: DxfRecord) -> str:
        return (record.first(6) or "").strip()

    # -- record readers ----------------------------------------------------- #

    def _entity(
        self,
        record: DxfRecord,
        transform: Affine,
        inherited_color: str,
        block: str,
        *,
        depth: int,
    ) -> None:
        kind = record.kind
        if not kind:
            return
        layer = self._layer_name(record)
        color = self._color(record, inherited_color, layer)
        linetype = self._linetype(record)
        handle = (record.first(5) or "").strip()
        extrusion = _extrusion_of(record)
        local = transform.compose(extrusion)
        self.result.note(kind)
        if kind == "LINE":
            start, end = record.point(10, 20), record.point(11, 21)
            if start and end:
                world_start, world_end = local.apply(start), local.apply(end)
                if world_start != world_end:
                    self._emit(
                        CadPrimitive(
                            kind="line",
                            layer=layer,
                            color=color,
                            linetype=linetype,
                            points=(world_start, world_end),
                            block=block,
                            handle=handle,
                        )
                    )
                else:
                    self.result.issues.add(
                        "CAD_DEGENERATE_GEOMETRY", "zero-length lines were skipped"
                    )
        elif kind == "LWPOLYLINE":
            self._polyline(record, local, layer, color, linetype, block, handle)
        elif kind == "POLYLINE":
            if record.integer(70) & 16 or record.integer(70) & 64:
                self.result.issues.add(
                    "CAD_UNSUPPORTED_ENTITIES",
                    "entities this importer does not decode were skipped",
                    detail={"POLYLINE_MESH": 1},
                )
                return
            self._polyline(record, local, layer, color, linetype, block, handle)
        elif kind == "CIRCLE":
            center, radius = record.point(10, 20), record.number(40)
            if center and radius > 0:
                self._emit(
                    CadPrimitive(
                        kind="circle",
                        layer=layer,
                        color=color,
                        linetype=linetype,
                        center=center,
                        radius=radius,
                        transform=local,
                        block=block,
                        handle=handle,
                    )
                )
        elif kind == "ARC":
            center, radius = record.point(10, 20), record.number(40)
            if center and radius > 0:
                self._emit(
                    CadPrimitive(
                        kind="arc",
                        layer=layer,
                        color=color,
                        linetype=linetype,
                        center=center,
                        radius=radius,
                        start_angle=math.radians(record.number(50)),
                        end_angle=math.radians(record.number(51)),
                        transform=local,
                        block=block,
                        handle=handle,
                    )
                )
        elif kind == "ELLIPSE":
            center, major = record.point(10, 20), record.point(11, 21)
            if center and major:
                length = math.hypot(*major)
                ratio = record.number(40, 1.0)
                if length > 0:
                    self._emit(
                        CadPrimitive(
                            kind="ellipse",
                            layer=layer,
                            color=color,
                            linetype=linetype,
                            center=center,
                            radius=length,
                            axis_ratio=ratio if ratio > 0 else 1.0,
                            major_axis_angle=math.atan2(major[1], major[0]),
                            start_param=record.number(41, 0.0),
                            end_param=record.number(42, math.tau),
                            transform=local,
                            block=block,
                            handle=handle,
                        )
                    )
        elif kind in {"SOLID", "TRACE"}:
            corners = [
                point
                for point in (record.point(10, 20), record.point(11, 21),
                              record.point(13, 23), record.point(12, 22))
                if point
            ]
            polygon = [local.apply(point) for point in fill_polygon(corners, swapped=False)]
            if len(polygon) >= 3:
                self._emit(
                    CadPrimitive(
                        kind="fill",
                        layer=layer,
                        color=color,
                        points=tuple(polygon),
                        closed=True,
                        block=block,
                        handle=handle,
                    )
                )
        elif kind == "TEXT":
            self._text(record, local, layer, color, block, handle)
        elif kind == "MTEXT":
            self._mtext(record, local, layer, color, block, handle)
        elif kind == "INSERT":
            self._insert(record, local, color, block, depth=depth)
        elif kind == "DIMENSION":
            self._dimension(record, local, color, block, depth=depth)
        elif kind == "LEADER":
            self._leader(record, local, layer, color, linetype, block, handle)
        elif kind == "HATCH":
            self._hatch(record, local, layer, color, block, handle)
        elif kind not in IGNORABLE_ENTITIES:
            self.result.issues.add(
                "CAD_UNSUPPORTED_ENTITIES",
                "entities this importer does not decode were skipped",
                detail={kind: 1},
            )

    def _polyline(
        self,
        record: DxfRecord,
        transform: Affine,
        layer: str,
        color: str,
        linetype: str,
        block: str,
        handle: str,
    ) -> None:
        closed = bool(record.integer(70) & 1)
        xs = record.values(10)
        ys = record.values(20)
        bulges = record.values(42)
        points: list[tuple[float, float]] = []
        try:
            coordinates = [(float(x), float(y)) for x, y in zip(xs, ys, strict=False)]
        except ValueError:
            return
        for index, point in enumerate(coordinates):
            if index == 0:
                points.append(point)
                continue
            bulge = 0.0
            if bulges:
                bulge_index = index - 1
                if bulge_index < len(bulges) and bulges[bulge_index]:
                    try:
                        bulge = float(bulges[bulge_index])
                    except ValueError:
                        bulge = 0.0
            previous = coordinates[index - 1]
            if bulge:
                points.extend(bulge_to_arc(previous, point, bulge)[1:])
            else:
                points.append(point)
        if closed and len(coordinates) > 2:
            bulge = 0.0
            if bulges and len(bulges) >= len(coordinates):
                try:
                    bulge = float(bulges[-1] or 0.0)
                except ValueError:
                    bulge = 0.0
            if bulge:
                points.extend(bulge_to_arc(coordinates[-1], coordinates[0], bulge)[1:-1])
        world = [transform.apply(point) for point in points]
        if len(world) < 2:
            return
        self._emit(
            CadPrimitive(
                kind="polyline",
                layer=layer,
                color=color,
                linetype=linetype,
                points=tuple(world),
                closed=closed,
                block=block,
                handle=handle,
            )
        )

    def _text(
        self,
        record: DxfRecord,
        transform: Affine,
        layer: str,
        color: str,
        block: str,
        handle: str,
    ) -> None:
        text = clean_text(record.first(1) or "")
        if not text:
            return
        horizontal = record.integer(72)
        vertical = record.integer(73)
        anchor_point = record.point(10, 20)
        if horizontal or vertical:
            anchor_point = record.point(11, 21) or anchor_point
        height = record.number(40)
        if anchor_point is None or height <= 0:
            return
        rotation = math.remainder(record.number(50), 360.0)
        if abs(rotation) > 1e-6:
            self.result.issues.add(
                "CAD_TEXT_ROTATION_IGNORED",
                (
                    "rotated text was placed at its anchor but not rotated: the editor "
                    "has no rotated-text primitive yet"
                ),
            )
        anchor = "start"
        if horizontal in (1, 4):
            anchor = "middle"
        elif horizontal == 2:
            anchor = "end"
        self._emit(
            CadPrimitive(
                kind="text",
                layer=layer,
                color=color,
                points=(transform.apply(anchor_point),),
                text=text,
                height=height * transform.scale,
                anchor=anchor,
                block=block,
                handle=handle,
            )
        )

    def _mtext(
        self,
        record: DxfRecord,
        transform: Affine,
        layer: str,
        color: str,
        block: str,
        handle: str,
    ) -> None:
        chunks = record.values(3)
        chunks.append(record.first(1) or "")
        text = clean_text("".join(chunks))
        anchor_point = record.point(10, 20)
        height = record.number(40)
        if not text or anchor_point is None or height <= 0:
            return
        if abs(math.remainder(record.number(50), 360.0)) > 1e-6:
            self.result.issues.add(
                "CAD_TEXT_ROTATION_IGNORED",
                (
                    "rotated text was placed at its anchor but not rotated: the editor "
                    "has no rotated-text primitive yet"
                ),
            )
        attachment = record.integer(71, 1)
        column = (attachment - 1) % 3
        anchor = "start" if column == 0 else "middle" if column == 1 else "end"
        self._emit(
            CadPrimitive(
                kind="text",
                layer=layer,
                color=color,
                points=(transform.apply(anchor_point),),
                text=text,
                height=height * transform.scale,
                anchor=anchor,
                block=block,
                handle=handle,
            )
        )

    def _insert(
        self,
        record: DxfRecord,
        transform: Affine,
        inherited_color: str,
        block: str,
        *,
        depth: int,
    ) -> None:
        name = (record.first(2) or "").strip()
        entities = self.blocks.get(name)
        if not entities:
            self.result.issues.add(
                "CAD_MISSING_BLOCK_DEFINITION",
                "block instances whose definition is missing from the source were skipped",
            )
            return
        if depth >= self.max_block_depth:
            self.result.issues.add(
                "CAD_BLOCK_DEPTH_LIMIT",
                "block instances nested deeper than the import limit were skipped",
            )
            return
        insertion = record.point(10, 20) or (0.0, 0.0)
        scale_x = record.number(41, 1.0) or 1.0
        scale_y = record.number(42, 1.0) or 1.0
        rotation = math.radians(record.number(50))
        columns = max(1, record.integer(70, 1))
        rows = max(1, record.integer(71, 1))
        column_spacing = record.number(44)
        row_spacing = record.number(45)
        cos_r, sin_r = math.cos(rotation), math.sin(rotation)
        base = Affine(
            matrix=(
                cos_r * scale_x,
                sin_r * scale_x,
                -sin_r * scale_y,
                cos_r * scale_y,
            ),
            translation=insertion,
        )
        # The owning INSERT's colour is what a BYBLOCK entity inside the block inherits.
        block_color = self._color(record, inherited_color, self._layer_name(record))
        base = transform.compose(base)
        for column in range(columns):
            for row in range(rows):
                if columns == 1 and rows == 1:
                    offset = (0.0, 0.0)
                else:
                    local_x = column * column_spacing
                    local_y = row * row_spacing
                    offset = (
                        local_x * cos_r - local_y * sin_r,
                        local_x * sin_r + local_y * cos_r,
                    )
                placed = Affine(matrix=base.matrix, translation=(base.translation[0] + offset[0], base.translation[1] + offset[1]))
                for entity in entities:
                    self._entity(
                        entity,
                        placed,
                        block_color,
                        name or block,
                        depth=depth + 1,
                    )

    def _dimension(
        self,
        record: DxfRecord,
        transform: Affine,
        inherited_color: str,
        block: str,
        *,
        depth: int,
    ) -> None:
        """Dimensions draw through an anonymous block; import that geometry.

        A dimension's lines, arrows and measurement text live in a block referenced by
        group code 2. Dropping DIMENSION would drop dimension linework; expanding the
        block keeps it as ordinary geometry.
        """

        name = (record.first(2) or "").strip()
        entities = self.blocks.get(name) if name else None
        if entities is None:
            self.result.issues.add(
                "CAD_MISSING_BLOCK_DEFINITION",
                "block instances whose definition is missing from the source were skipped",
            )
            return
        if depth >= self.max_block_depth:
            self.result.issues.add(
                "CAD_BLOCK_DEPTH_LIMIT",
                "block instances nested deeper than the import limit were skipped",
            )
            return
        for entity in entities:
            self._entity(entity, transform, inherited_color, name or block, depth=depth + 1)

    def _leader(
        self,
        record: DxfRecord,
        transform: Affine,
        layer: str,
        color: str,
        linetype: str,
        block: str,
        handle: str,
    ) -> None:
        points: list[tuple[float, float]] = []
        xs = [value for code, value in record.pairs if code == 10]
        ys = [value for code, value in record.pairs if code == 20]
        for x_raw, y_raw in zip(xs, ys, strict=False):
            try:
                points.append((float(x_raw), float(y_raw)))
            except ValueError:
                continue
        # A LEADER repeats 10/20 once per vertex; the first pair is the arrow point.
        if len(points) < 2:
            return
        world = [transform.apply(point) for point in points]
        self._emit(
            CadPrimitive(
                kind="polyline",
                layer=layer,
                color=color,
                linetype=linetype,
                points=tuple(world),
                block=block,
                handle=handle,
            )
        )

    def _hatch(
        self,
        record: DxfRecord,
        transform: Affine,
        layer: str,
        color: str,
        block: str,
        handle: str,
    ) -> None:
        solid = bool(record.integer(70) & 1) or (record.first(2) or "").strip().upper() == "SOLID"
        if not solid:
            # Report the pattern before looking at the boundary: the reason this hatch
            # is missing is that the editor has no hatch pattern, not that the boundary
            # was unreadable, and the more precise reason is the useful one to keep.
            self.result.issues.add(
                "CAD_PATTERN_HATCH_SKIPPED",
                (
                    "pattern (non-solid) hatches were skipped: the editor has no hatch "
                    "pattern, and drawing them solid would misrepresent the sheet"
                ),
            )
            return
        paths = self._hatch_paths(record)
        if not paths:
            self.result.issues.add(
                "CAD_HATCH_BOUNDARY_UNREADABLE",
                "hatch boundaries could not be decoded and were skipped",
            )
            return
        if len(paths) > 1:
            # Islands are holes; filling every boundary would fill them in.
            for path in paths:
                world = [transform.apply(point) for point in path]
                if len(world) >= 3:
                    self._emit(
                        CadPrimitive(
                            kind="polyline",
                            layer=layer,
                            color=color,
                            points=tuple(world),
                            closed=True,
                            block=block,
                            handle=handle,
                        )
                    )
            self.result.issues.add(
                "CAD_HATCH_ISLANDS_OUTLINED",
                (
                    "solid hatches with islands were imported as outlines, because "
                    "filling them would fill their holes"
                ),
            )
            return
        world = [transform.apply(point) for point in paths[0]]
        if len(world) >= 3:
            self._emit(
                CadPrimitive(
                    kind="fill",
                    layer=layer,
                    color=color,
                    points=tuple(world),
                    closed=True,
                    block=block,
                    handle=handle,
                )
            )

    def _hatch_paths(self, record: DxfRecord) -> list[list[tuple[float, float]]]:
        """Read the boundary loops of a HATCH record (line, arc and polyline edges)."""

        pairs = record.pairs
        index = 0
        total = len(pairs)
        while index < total and not (pairs[index][0] == 91):
            index += 1
        if index >= total:
            return []
        paths: list[list[tuple[float, float]]] = []
        index += 1
        while index < total:
            code, value = pairs[index]
            if code != 92:
                break
            try:
                flags = int(float(value))
            except ValueError:
                break
            index += 1
            # Skip through any edge-type metadata this reader does not use.
            if flags & 2:
                points, index = self._hatch_polyline_path(pairs, index)
                if points:
                    paths.append(points)
                continue
            count = 0
            while index < total and pairs[index][0] != 93:
                if pairs[index][0] == 97:
                    break
                index += 1
            if index < total and pairs[index][0] == 93:
                try:
                    count = int(float(pairs[index][1]))
                except ValueError:
                    count = 0
                index += 1
            for _ in range(max(0, count)):
                points, index = self._hatch_edge(pairs, index)
                if points:
                    paths.append(points)
            # Skip source boundary object references and seed points.
            while index < total and pairs[index][0] in {97, 98, 10, 20}:
                index += 1
        return paths

    def _hatch_polyline_path(
        self, pairs: tuple[tuple[int, str], ...], index: int
    ) -> tuple[list[tuple[float, float]], int]:
        total = len(pairs)
        count = 0
        while index < total and pairs[index][0] != 93:
            index += 1
        if index < total:
            try:
                count = int(float(pairs[index][1]))
            except ValueError:
                count = 0
            index += 1
        raw: list[tuple[float, float]] = []
        bulges: list[float] = []
        seen = 0
        while index < total and seen < max(0, count):
            code, value = pairs[index]
            if code == 10:
                x = float(value)
                y = 0.0
                bulge = 0.0
                probe = index + 1
                while probe < total and pairs[probe][0] in {20, 42}:
                    if pairs[probe][0] == 20:
                        y = float(pairs[probe][1])
                    else:
                        bulge = float(pairs[probe][1])
                    probe += 1
                raw.append((x, y))
                bulges.append(bulge)
                seen += 1
                index = probe
                continue
            if code in {97, 92}:
                break
            index += 1
        if len(raw) < 3:
            return [], index
        points: list[tuple[float, float]] = []
        for position, point in enumerate(raw):
            if position == 0:
                points.append(point)
                continue
            bulge = bulges[position - 1]
            if bulge:
                points.extend(bulge_to_arc(raw[position - 1], point, bulge)[1:])
            else:
                points.append(point)
        return points, index

    def _hatch_edge(
        self, pairs: tuple[tuple[int, str], ...], index: int
    ) -> tuple[list[tuple[float, float]], int]:
        total = len(pairs)
        if index >= total:
            return [], index
        code, value = pairs[index]
        if code != 72:
            return [], index
        try:
            edge_type = int(float(value))
        except ValueError:
            return [], index
        index += 1
        values: dict[int, float] = {}
        # Edge geometry ends at the next edge (72) or at the next path (92/97/98).
        while index < total and pairs[index][0] not in {72, 92, 97, 98}:
            sub_code, sub_value = pairs[index]
            if sub_code in {10, 11, 20, 21, 40, 50, 51, 73}:
                try:
                    values[sub_code] = float(sub_value)
                except ValueError:
                    pass
            index += 1
        start = (values.get(10, float("nan")), values.get(20, float("nan")))
        end = (values.get(11, float("nan")), values.get(21, float("nan")))
        if edge_type == 0:
            if start[0] == start[0] and end[0] == end[0]:
                return [start, end], index
            return [], index
        if edge_type == 1:
            center = start
            radius = values.get(40, 0.0)
            if radius <= 0:
                return [], index
            arc = CadPrimitive(
                kind="arc",
                center=center,
                radius=radius,
                start_angle=math.radians(values.get(50, 0.0)),
                end_angle=math.radians(values.get(51, 0.0)),
            )
            arc_points = arc.curve_points(24)
            # Group code 73 = 0 asks for the clockwise arc.
            return (arc_points if values.get(73, 1.0) else list(reversed(arc_points))), index
        if edge_type == 2:
            center = start
            major = (values.get(11, 0.0), values.get(21, 0.0))
            length = math.hypot(*major)
            if length <= 0:
                return [], index
            ellipse = CadPrimitive(
                kind="ellipse",
                center=center,
                radius=length,
                axis_ratio=values.get(40, 1.0) or 1.0,
                major_axis_angle=math.atan2(major[1], major[0]),
                start_param=values.get(50, 0.0),
                end_param=values.get(51, math.tau),
            )
            ellipse_points = ellipse.curve_points(24)
            return (
                ellipse_points
                if values.get(73, 1.0)
                else list(reversed(ellipse_points))
            ), index
        self.result.issues.add(
            "CAD_SPLINE_EDGE_SKIPPED",
            "curved hatch boundaries described as splines were skipped",
        )
        return [], index

    def _emit(self, primitive: CadPrimitive) -> None:
        self.result.primitives.append(primitive)
        self.result.layers.setdefault(primitive.layer, self._layer_ink(primitive.layer))


def _extrusion_of(record: DxfRecord) -> Affine:
    if not (record.has(210) or record.has(220) or record.has(230)):
        return Affine()
    return ocs_transform((record.number(210), record.number(220), record.number(230, 1.0)))


def read_dxf(
    data: bytes,
    *,
    max_block_depth: int = 12,
) -> CadDecodeResult:
    """Read DXF bytes into primitives."""

    text, codec = decode_dxf_bytes(data)
    if "SECTION" not in text[:4096] and "\n0\n" not in text[:4096]:
        raise CadGeometryError(
            "dxf_not_recognised",
            "the file does not look like an ASCII DXF stream",
        )
    decoder = DxfDecoder(
        list(tokenize_dxf(text)),
        codec=codec,
        max_block_depth=max_block_depth,
    )
    return decoder.decode()


__all__ = [
    "BINARY_DXF_MARKER",
    "CODEPAGE_CODECS",
    "DxfDecoder",
    "DxfRecord",
    "decode_dxf_bytes",
    "read_dxf",
    "records_from_tokens",
    "tokenize_dxf",
]
