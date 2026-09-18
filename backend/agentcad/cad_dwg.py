"""Decode a DWG object dump (``dwgread -O JSON``) into CAD primitives.

Charter reference: §14 (drawing intake). This is the *high-fidelity* DWG path: the
LibreDWG object dump keeps the contents of dynamic blocks, which its DXF writer drops
(measured on a real 气路系统总图: 140 valve instances and 12 pumps have empty block
definitions in the DXF but are complete in the object dump). Those symbols are the most
visible content on the sheet, so the dump — not the DXF — is the primary DWG source.

The dump is LibreDWG's own JSON serialisation of the DWG object stream. This decoder
reads it defensively: every reference is resolved through its absolute handle, an
unknown entity is counted rather than guessed at, and nested block inserts compose an
:class:`Affine` instead of a scalar scale so mirrored instances stay mirrored.
"""

from __future__ import annotations

import math
from typing import Any

from .cad_geometry import (
    DEFAULT_INK,
    Affine,
    CadDecodeResult,
    CadGeometryError,
    CadPrimitive,
    clean_text,
    fill_polygon,
    ocs_transform,
    resolve_dwg_color,
)

#: Entity types this decoder turns into geometry. Anything else is reported, not guessed.
DECODED_ENTITIES = frozenset(
    {
        "LINE",
        "LWPOLYLINE",
        "POLYLINE_2D",
        "CIRCLE",
        "ARC",
        "ELLIPSE",
        "TEXT",
        "MTEXT",
        "INSERT",
        "SOLID",
        "3DFACE",
        "HATCH",
    }
)

#: Types that are known to carry no drawable 2D geometry, so their absence from the
#: output is not a fidelity loss worth reporting as a warning.
IGNORABLE_ENTITIES = frozenset(
    {
        "SEQEND",
        "VERTEX_2D",
        "ATTRIB",
        "ATTDEF",
        "POINT",
        "VIEWPORT",
        "BLOCK",
        "ENDBLK",
        "OLE2FRAME",
        "WIPEOUT",
    }
)

EMPTY_HANDLES = frozenset({"0", ""})


def handle_value(reference: Any) -> int | None:
    """The absolute handle of a LibreDWG reference.

    References appear as ``[code, size, value, absolute]`` or as a bare handle triplet,
    and the absolute handle is always the last element.
    """

    if isinstance(reference, list) and reference and all(
        isinstance(item, int) for item in reference
    ):
        return reference[-1]
    return None


def handle_label(reference: Any) -> str:
    handle = handle_value(reference)
    if handle is None:
        return ""
    label = f"{handle:X}"
    return "" if label in EMPTY_HANDLES else label


def _point(values: Any) -> tuple[float, float] | None:
    if not isinstance(values, (list, tuple)) or len(values) < 2:
        return None
    try:
        return (float(values[0]), float(values[1]))
    except (TypeError, ValueError):
        return None


def _extrusion(entity: dict[str, Any]) -> Affine:
    raw = entity.get("extrusion")
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        try:
            return ocs_transform((float(raw[0]), float(raw[1]), float(raw[2])))
        except (TypeError, ValueError):
            return Affine()
    return Affine()


def _anchor_from_alignment(horizontal: int, vertical: int) -> str:
    if horizontal in (1, 4):
        return "middle"
    if horizontal in (2, 5):
        return "end"
    if horizontal == 3:  # aligned text centres on its baseline
        return "middle"
    if vertical == 0 and horizontal == 0:
        return "start"
    return "middle" if horizontal in (1, 4) else "start"


def _mtext_anchor(attachment: int) -> str:
    column = (attachment - 1) % 3
    return "start" if column == 0 else "middle" if column == 1 else "end"


class ObjectStreamDecoder:
    """Flatten one LibreDWG object dump into primitives."""

    def __init__(self, payload: dict[str, Any], *, max_block_depth: int = 12):
        if not isinstance(payload, dict) or not isinstance(payload.get("OBJECTS"), list):
            raise CadGeometryError(
                "object_stream_missing",
                "the DWG object dump does not contain an OBJECTS array",
            )
        self.payload = payload
        self.objects: list[dict[str, Any]] = [
            item for item in payload["OBJECTS"] if isinstance(item, dict)
        ]
        self.max_block_depth = max_block_depth
        self.result = CadDecodeResult()
        file_header = payload.get("FILEHEADER")
        if isinstance(file_header, dict):
            self.result.format_detail = str(file_header.get("version", ""))
        self._index()

    # -- indexing ----------------------------------------------------------- #

    def _index(self) -> None:
        self.by_handle: dict[int, dict[str, Any]] = {}
        for order, item in enumerate(self.objects):
            handle = handle_value(item.get("handle"))
            if handle is None:
                continue
            item["_order"] = order
            self.by_handle[handle] = item

        self.layers: dict[int, tuple[str, str]] = {}
        for item in self.objects:
            if item.get("object") != "LAYER":
                continue
            handle = handle_value(item.get("handle"))
            name = str(item.get("name") or "0").strip() or "0"
            if handle is not None:
                self.layers[handle] = (name, self._layer_color(item))

        self.blocks: dict[int, dict[str, Any]] = {
            handle_value(item["handle"]): item
            for item in self.objects
            if item.get("object") == "BLOCK_HEADER" and handle_value(item.get("handle"))
        }

        self.vertices: dict[int, list[dict[str, Any]]] = {}
        for item in self.objects:
            if item.get("entity") != "VERTEX_2D":
                continue
            owner = handle_value(item.get("ownerhandle"))
            if owner is not None:
                self.vertices.setdefault(owner, []).append(item)
        for group in self.vertices.values():
            group.sort(key=lambda vertex: vertex.get("_order", 0))

    def _layer_color(self, layer: dict[str, Any]) -> str:
        color = layer.get("color")
        if isinstance(color, dict):
            return resolve_dwg_color(
                color.get("index"),
                color.get("rgb"),
                inherited=DEFAULT_INK,
                layer_ink=DEFAULT_INK,
            )
        return DEFAULT_INK

    def _model_space(self) -> dict[str, Any]:
        for header in self.blocks.values():
            if str(header.get("name", "")).endswith("Model_Space"):
                return header
        raise CadGeometryError(
            "no_model_space",
            "the DWG object dump has no model space block to read geometry from",
        )

    # -- decoding ----------------------------------------------------------- #

    def decode(self) -> CadDecodeResult:
        model_space = self._model_space()
        self._walk(
            self._block_entities(model_space),
            Affine(),
            DEFAULT_INK,
            "",
            depth=0,
        )
        self._report_skipped()
        return self.result

    def _block_entities(self, header: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for reference in header.get("entities") or []:
            handle = handle_value(reference)
            item = self.by_handle.get(handle) if handle is not None else None
            if item is not None:
                out.append(item)
        return out

    def _layer_name(self, entity: dict[str, Any]) -> str:
        handle = handle_value(entity.get("layer"))
        if handle is None:
            return "0"
        entry = self.layers.get(handle)
        return entry[0] if entry else "0"

    def _layer_ink(self, entity: dict[str, Any]) -> str:
        handle = handle_value(entity.get("layer"))
        if handle is None:
            return DEFAULT_INK
        entry = self.layers.get(handle)
        return entry[1] if entry else DEFAULT_INK

    def _color(self, entity: dict[str, Any], inherited: str, layer: str) -> str:
        """Resolve an entity's ink, honouring BYBLOCK and BYLAYER.

        BYBLOCK (index 0) takes the colour of the block instance it lives in; BYLAYER
        (index 256) takes the colour of its own layer. Conflating the two turns a
        mirrored instrument block into whatever colour happened to be inherited.
        """

        color = entity.get("color")
        layer_ink = self._layer_ink_for(layer)
        if not isinstance(color, dict):
            return layer_ink
        return resolve_dwg_color(
            color.get("index"),
            color.get("rgb"),
            inherited=inherited,
            layer_ink=layer_ink,
        )

    def _walk(
        self,
        entities: list[dict[str, Any]],
        transform: Affine,
        inherited_color: str,
        block: str,
        *,
        depth: int,
    ) -> None:
        for entity in entities:
            if entity.get("invisible"):
                self.result.issues.add(
                    "CAD_INVISIBLE_ENTITIES_SKIPPED",
                    "entities flagged invisible in the source were not imported",
                )
                continue
            self._one(entity, transform, inherited_color, block, depth=depth)

    def _one(
        self,
        entity: dict[str, Any],
        transform: Affine,
        inherited_color: str,
        block: str,
        *,
        depth: int,
    ) -> None:
        kind = entity.get("entity")
        if not isinstance(kind, str):
            return
        self.result.note(kind)
        layer = self._layer_name(entity)
        color = self._color(entity, inherited_color, layer)
        local = ocs_transform_from(entity)
        transform = transform.compose(local)
        handle = handle_label(entity.get("handle"))

        def world(point: tuple[float, float] | None) -> tuple[float, float] | None:
            return None if point is None else transform.apply(point)

        if kind == "LINE":
            start, end = world(_point(entity.get("start"))), world(_point(entity.get("end")))
            if start and end and start != end:
                self._emit(
                    CadPrimitive(
                        kind="line",
                        layer=layer,
                        color=color,
                        linetype=str(entity.get("linetype") or ""),
                        points=(start, end),
                        block=block,
                        handle=handle,
                    )
                )
            elif start and end:
                self.result.issues.add(
                    "CAD_DEGENERATE_GEOMETRY",
                    "zero-length lines were skipped",
                )
        elif kind == "LWPOLYLINE":
            points = [
                point
                for point in (world(_point(raw)) for raw in entity.get("points") or [])
                if point
            ]
            if len(points) >= 2:
                self._emit(
                    CadPrimitive(
                        kind="polyline",
                        layer=layer,
                        color=color,
                        linetype=str(entity.get("linetype") or ""),
                        points=tuple(points),
                        closed=bool(int(entity.get("flag", 0) or 0) & 1),
                        block=block,
                        handle=handle,
                    )
                )
        elif kind == "POLYLINE_2D":
            handle_value_ = handle_value(entity.get("handle"))
            vertices = self.vertices.get(handle_value_ or -1, [])
            points = [
                point
                for point in (world(_point(vertex.get("point"))) for vertex in vertices)
                if point
            ]
            if len(points) >= 2:
                self._emit(
                    CadPrimitive(
                        kind="polyline",
                        layer=layer,
                        color=color,
                        linetype=str(entity.get("linetype") or ""),
                        points=tuple(points),
                        closed=bool(int(entity.get("flag", 0) or 0) & 1),
                        block=block,
                        handle=handle,
                    )
                )
        elif kind in {"CIRCLE", "ARC"}:
            center = _point(entity.get("center"))
            radius = entity.get("radius")
            if center is None or not isinstance(radius, (int, float)) or radius <= 0:
                return
            self._emit(
                CadPrimitive(
                    kind="circle" if kind == "CIRCLE" else "arc",
                    layer=layer,
                    color=color,
                    linetype=str(entity.get("linetype") or ""),
                    center=center,
                    radius=float(radius),
                    start_angle=float(entity.get("start_angle") or 0.0),
                    end_angle=float(entity.get("end_angle") or 0.0),
                    transform=transform,
                    block=block,
                    handle=handle,
                )
            )
        elif kind == "ELLIPSE":
            center = _point(entity.get("center"))
            major = _point(entity.get("sm_axis"))
            if center is None or major is None:
                return
            length = math.hypot(major[0], major[1])
            if length <= 0:
                return
            ratio = float(entity.get("axis_ratio") or 1.0)
            self._emit(
                CadPrimitive(
                    kind="ellipse",
                    layer=layer,
                    color=color,
                    linetype=str(entity.get("linetype") or ""),
                    center=center,
                    radius=length,
                    axis_ratio=ratio if ratio > 0 else 1.0,
                    major_axis_angle=math.atan2(major[1], major[0]),
                    start_param=float(entity.get("start_angle") or 0.0),
                    end_param=float(entity.get("end_angle") or math.tau),
                    transform=transform,
                    block=block,
                    handle=handle,
                )
            )
        elif kind in {"TEXT", "MTEXT"}:
            self._text(entity, kind, transform, layer, color, block, handle)
        elif kind == "INSERT":
            self._insert(entity, transform, color, block, depth=depth)
        elif kind in {"SOLID", "3DFACE"}:
            corners = [
                point
                for point in (
                    world(_point(entity.get(f"corner{index}"))) for index in range(1, 5)
                )
                if point
            ]
            polygon = fill_polygon(corners, swapped=kind == "SOLID")
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
        elif kind == "HATCH":
            self.result.issues.add(
                "CAD_HATCH_BOUNDARY_UNREADABLE",
                (
                    "solid hatches were not imported: this decoder cannot trust the "
                    "boundary geometry of a hatch in the object dump"
                ),
            )
        elif kind not in IGNORABLE_ENTITIES:
            self.result.issues.add(
                "CAD_UNSUPPORTED_ENTITIES",
                "entities this importer does not decode were skipped",
                detail={kind: 1},
            )

    def _text(
        self,
        entity: dict[str, Any],
        kind: str,
        transform: Affine,
        layer: str,
        color: str,
        block: str,
        handle: str,
    ) -> None:
        if kind == "TEXT":
            text = clean_text(str(entity.get("text_value") or ""))
            raw_height = float(entity.get("height") or 0.0)
            horizontal = int(entity.get("horiz_alignment") or 0)
            vertical = int(entity.get("vert_alignment") or 0)
            anchor_point = _point(entity.get("alignment_pt")) if horizontal or vertical else None
            anchor_point = anchor_point or _point(entity.get("ins_pt"))
            if not text:
                return
            anchor = _anchor_from_alignment(horizontal, vertical)
        else:
            text = clean_text(str(entity.get("text") or ""))
            raw_height = float(entity.get("text_height") or 0.0)
            anchor_point = _point(entity.get("ins_pt"))
            if not text:
                return
            anchor = _mtext_anchor(int(entity.get("attachment") or 1))
        if anchor_point is None or raw_height <= 0:
            return
        rotation = math.remainder(float(entity.get("rotation") or 0.0), math.tau)
        if abs(rotation) > 1e-6:
            self.result.issues.add(
                "CAD_TEXT_ROTATION_IGNORED",
                (
                    "rotated text was placed at its anchor but not rotated: the editor "
                    "has no rotated-text primitive yet"
                ),
            )
        self._emit(
            CadPrimitive(
                kind="text",
                layer=layer,
                color=color,
                points=(transform.apply(anchor_point),),
                text=text,
                height=raw_height * transform.scale,
                anchor=anchor,
                block=block,
                handle=handle,
            )
        )

    def _insert(
        self,
        entity: dict[str, Any],
        transform: Affine,
        color: str,
        block: str,
        *,
        depth: int,
    ) -> None:
        handle = handle_value(entity.get("block_header"))
        header = self.blocks.get(handle) if handle is not None else None
        if header is None:
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
        insertion = _point(entity.get("ins_pt")) or (0.0, 0.0)
        scale = entity.get("scale") or [1.0, 1.0, 1.0]
        try:
            scale_x = float(scale[0])
            scale_y = float(scale[1])
        except (TypeError, ValueError, IndexError):
            scale_x = scale_y = 1.0
        # LibreDWG serialises the block reference's rotation in radians (the DWG binary
        # representation), unlike DXF where group code 50 is degrees. Measured on a real
        # sheet: 1.5708 for quarter turns.
        rotation = float(entity.get("rotation") or 0.0)
        cos_r, sin_r = math.cos(rotation), math.sin(rotation)
        child = Affine(
            matrix=(
                cos_r * scale_x,
                sin_r * scale_x,
                -sin_r * scale_y,
                cos_r * scale_y,
            ),
            translation=insertion,
        )
        name = str(header.get("name", "")) or block
        outer = transform.compose(child)
        self._walk(
            self._block_entities(header),
            outer,
            color,
            name,
            depth=depth + 1,
        )

    def _emit(self, primitive: CadPrimitive) -> None:
        self.result.primitives.append(primitive)
        if primitive.layer not in self.result.layers:
            self.result.layers[primitive.layer] = self._layer_ink_for(primitive.layer)

    def _layer_ink_for(self, name: str) -> str:
        for layer_name, ink in self.layers.values():
            if layer_name == name:
                return ink
        return DEFAULT_INK

    def _report_skipped(self) -> None:
        """Finish the decode.

        Entity kinds that are neither decoded nor known to be non-geometric are reported
        as they are met; the totals live in ``entity_kinds`` so the importer can show
        coverage without guessing at it.
        """

        self.result.layers = {name: ink for name, ink in self.result.layers.items() if name}


def ocs_transform_from(entity: dict[str, Any]) -> Affine:
    """Extrusion transform of one entity (identity for the usual 2D case)."""

    return _extrusion(entity)


def decode_object_stream(
    payload: dict[str, Any], *, max_block_depth: int = 12
) -> CadDecodeResult:
    """Decode a LibreDWG object dump payload into primitives."""

    return ObjectStreamDecoder(payload, max_block_depth=max_block_depth).decode()


__all__ = [
    "DECODED_ENTITIES",
    "IGNORABLE_ENTITIES",
    "ObjectStreamDecoder",
    "decode_object_stream",
    "handle_label",
    "handle_value",
]
