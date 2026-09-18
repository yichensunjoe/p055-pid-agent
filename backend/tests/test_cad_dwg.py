"""Decoder tests for the LibreDWG object dump (the high-fidelity DWG path).

This decoder reads an external tool's serialisation, so the tests state the shapes that
matter and pin the behaviour the importer depends on: block instances compose an affine
transform (mirrored inserts stay mirrored), SOLID corners are in crossed order (getting
that wrong turns every arrowhead into a bow tie), BYLAYER and BYBLOCK resolve to
different inks, and an entity the decoder does not understand is counted rather than
guessed at.
"""

from __future__ import annotations

import math

import pytest
from cad_fixtures import ObjectStreamBuilder

from agentcad.cad_dwg import decode_object_stream
from agentcad.cad_geometry import CadGeometryError, bounds_of


def test_line_circle_arc_and_ellipse_are_decoded() -> None:
    builder = ObjectStreamBuilder()
    layer = builder.layer("PIPE", color_index=3)
    entities = [
        builder.line((0.0, 0.0), (100.0, 0.0), layer=layer),
        builder.circle((50.0, 50.0), 10.0, layer=layer),
        builder.arc((0.0, 0.0), 20.0, 0.0, math.pi / 2, layer=layer),
        builder.ellipse((0.0, 0.0), (5.0, 5.0), 0.5, layer=layer),
    ]
    builder.model_space(entities)

    result = decode_object_stream(builder.build())

    kinds = sorted(primitive.kind for primitive in result.primitives)
    assert kinds == ["arc", "circle", "ellipse", "line"]
    circle = next(item for item in result.primitives if item.kind == "circle")
    assert circle.center == (50.0, 50.0)
    assert circle.radius == 10.0
    assert circle.color == "#00ff00"


def test_format_detail_comes_from_the_file_header() -> None:
    builder = ObjectStreamBuilder()
    builder.model_space([builder.line((0.0, 0.0), (1.0, 0.0))])
    result = decode_object_stream(builder.build())

    assert result.format_detail == "AC1032"


def test_lwpolyline_keeps_its_closed_flag() -> None:
    builder = ObjectStreamBuilder()
    builder.model_space(
        [
            builder.lwpolyline([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)], closed=True),
            builder.lwpolyline([(0.0, 0.0), (10.0, 10.0)]),
        ]
    )
    result = decode_object_stream(builder.build())

    assert [primitive.closed for primitive in result.primitives] == [True, False]
    assert result.primitives[0].points == ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0))


def test_polyline_2d_reads_its_vertex_children_in_order() -> None:
    builder = ObjectStreamBuilder()
    builder.model_space(
        [builder.polyline_2d([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)], closed=True)]
    )
    result = decode_object_stream(builder.build())

    assert len(result.primitives) == 1
    assert result.primitives[0].points == ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0))
    assert result.primitives[0].closed is True


def test_block_insert_geometry_is_transformed_into_place() -> None:
    builder = ObjectStreamBuilder()
    layer = builder.layer("VALVE", color_index=1)
    block = builder.block(
        "VALVE",
        [
            builder.line((-10.0, -10.0), (10.0, -10.0), layer=layer),
            builder.line((10.0, -10.0), (10.0, 10.0), layer=layer),
        ],
    )
    builder.model_space([builder.insert(block, (100.0, 200.0))])
    result = decode_object_stream(builder.build())

    assert len(result.primitives) == 2
    assert result.primitives[0].block == "VALVE"
    assert result.primitives[0].points == ((90.0, 190.0), (110.0, 190.0))


def test_mirrored_insert_keeps_its_block_geometry_mirrored() -> None:
    builder = ObjectStreamBuilder()
    block = builder.block(
        "PDS2D-6Q1C15",
        [builder.line((-10.0, 0.0), (20.0, 0.0)), builder.circle((0.0, 0.0), 5.0)],
    )
    builder.model_space([builder.insert(block, (0.0, 0.0), scale=(-2.0, 2.0))])
    result = decode_object_stream(builder.build())

    line = next(item for item in result.primitives if item.kind == "line")
    assert line.points == ((20.0, 0.0), (-40.0, 0.0))
    circle = next(item for item in result.primitives if item.kind == "circle")
    # The mirrored curve stays a circle and samples through the affine transform.
    samples = circle.curve_points(24)
    assert max(point[0] for point in samples) == pytest.approx(10.0, abs=1e-6)
    assert min(point[0] for point in samples) == pytest.approx(-10.0, abs=1e-6)


def test_rotated_insert_rotates_its_block_geometry() -> None:
    builder = ObjectStreamBuilder()
    block = builder.block("TAG", [builder.circle((0.0, 0.0), 1.0)])
    builder.model_space(
        [builder.insert(block, (10.0, 0.0), scale=(2.0, 2.0), rotation=math.pi / 2)]
    )
    result = decode_object_stream(builder.build())

    # A quarter turn leaves a circle centred at the origin where it was, so the only
    # observable effect is the scale and the insertion point.
    circle = result.primitives[0]
    samples = circle.curve_points(24)
    assert min(point[0] for point in samples) == pytest.approx(8.0, abs=1e-6)
    assert max(point[0] for point in samples) == pytest.approx(12.0, abs=1e-6)
    assert min(point[1] for point in samples) == pytest.approx(-2.0, abs=1e-6)
    assert max(point[1] for point in samples) == pytest.approx(2.0, abs=1e-6)


def test_nested_blocks_compose_their_transforms() -> None:
    builder = ObjectStreamBuilder()
    inner = builder.block("INNER", [builder.circle((0.0, 0.0), 1.0)])
    outer = builder.block("OUTER", [builder.insert(inner, (5.0, 0.0))])
    builder.model_space([builder.insert(outer, (100.0, 100.0), scale=(3.0, 3.0))])
    result = decode_object_stream(builder.build())

    circle = result.primitives[0]
    samples = circle.curve_points(24)
    centre_x = (min(point[0] for point in samples) + max(point[0] for point in samples)) / 2.0
    assert centre_x == pytest.approx(115.0, abs=1e-6)
    assert max(point[0] for point in samples) - min(point[0] for point in samples) == pytest.approx(6.0)
    assert circle.block == "INNER"


def test_text_height_is_scaled_by_its_block_instance() -> None:
    builder = ObjectStreamBuilder()
    block = builder.block("LABEL", [builder.text("P-101", (0.0, 0.0), 10.0)])
    builder.model_space([builder.insert(block, (0.0, 0.0), scale=(0.5, 0.5))])
    result = decode_object_stream(builder.build())

    text = result.primitives[0]
    assert text.text == "P-101"
    assert text.height == pytest.approx(5.0)


def test_text_alignment_point_is_used_when_alignment_is_set() -> None:
    builder = ObjectStreamBuilder()
    builder.model_space(
        [builder.text("P-101", (0.0, 0.0), 10.0, horizontal=2, alignment=(50.0, 60.0))]
    )
    primitive = decode_object_stream(builder.build()).primitives[0]

    assert primitive.anchor == "end"
    assert primitive.points == ((50.0, 60.0),)


def test_mtext_attachment_maps_to_an_anchor() -> None:
    builder = ObjectStreamBuilder()
    builder.model_space(
        [
            builder.mtext("{\\fSimSun|b0;熔盐泵}", (0.0, 0.0), 25.0, attachment=5),
            builder.mtext("LEFT", (0.0, 0.0), 25.0, attachment=1),
            builder.mtext("RIGHT", (0.0, 0.0), 25.0, attachment=3),
        ]
    )
    result = decode_object_stream(builder.build())

    assert [primitive.anchor for primitive in result.primitives] == ["middle", "start", "end"]
    assert result.primitives[0].text == "熔盐泵"


def test_rotated_text_is_reported() -> None:
    builder = ObjectStreamBuilder()
    builder.model_space([builder.text("ROTATED", (0.0, 0.0), 10.0, rotation=1.5)])
    result = decode_object_stream(builder.build())

    assert result.issues.codes["CAD_TEXT_ROTATION_IGNORED"] == 1


def test_solid_corners_are_reordered_into_a_drawable_polygon() -> None:
    """A SOLID stores corners 3 and 4 crossed; the outline is 1-2-4-3."""

    builder = ObjectStreamBuilder()
    builder.model_space(
        [
            builder.solid(
                [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
            )
        ]
    )
    result = decode_object_stream(builder.build())

    assert len(result.primitives) == 1
    primitive = result.primitives[0]
    assert primitive.kind == "fill"
    assert primitive.points == ((0.0, 0.0), (10.0, 0.0), (0.0, 10.0), (10.0, 10.0))
    assert primitive.closed is True


def test_triangular_solid_with_a_repeated_corner_stays_a_triangle() -> None:
    builder = ObjectStreamBuilder()
    builder.model_space(
        [builder.solid([(0.0, 0.0), (10.0, 0.0), (5.0, 8.0), (5.0, 8.0)])]
    )
    result = decode_object_stream(builder.build())

    assert len(result.primitives[0].points) == 3


def test_hatch_boundaries_are_reported_as_unreadable() -> None:
    """The object dump's hatch geometry is not trustworthy; say so instead of guessing."""

    builder = ObjectStreamBuilder()
    builder.model_space([builder.hatch(), builder.line((0.0, 0.0), (1.0, 0.0))])
    result = decode_object_stream(builder.build())

    assert len(result.primitives) == 1
    assert result.issues.codes["CAD_HATCH_BOUNDARY_UNREADABLE"] == 1


def test_unknown_entities_are_counted_with_their_type() -> None:
    builder = ObjectStreamBuilder()
    builder.model_space([builder.unknown("SPLINE"), builder.unknown("MLEADER")])
    result = decode_object_stream(builder.build())

    assert result.issues.details["CAD_UNSUPPORTED_ENTITIES"] == {"SPLINE": 1, "MLEADER": 1}


def test_invisible_entities_are_skipped_and_reported() -> None:
    builder = ObjectStreamBuilder()
    builder.model_space(
        [
            builder.line((0.0, 0.0), (1.0, 0.0), invisible=True),
            builder.line((0.0, 0.0), (2.0, 0.0)),
        ]
    )
    result = decode_object_stream(builder.build())

    assert len(result.primitives) == 1
    assert result.issues.codes["CAD_INVISIBLE_ENTITIES_SKIPPED"] == 1


def test_bylayer_takes_the_layer_ink_and_byblock_inherits_the_insert() -> None:
    builder = ObjectStreamBuilder()
    layer = builder.layer("PIPE", color_index=3)
    block = builder.block(
        "VALVE",
        [
            builder.line((0.0, 0.0), (1.0, 0.0), layer=layer, color=0),
            builder.line((0.0, 1.0), (1.0, 1.0), layer=layer, color=256),
        ],
    )
    builder.model_space(
        [builder.insert(block, (0.0, 0.0), layer=layer, color=1)]
    )
    result = decode_object_stream(builder.build())

    by_block = next(item for item in result.primitives if item.points[0] == (0.0, 0.0))
    by_layer = next(item for item in result.primitives if item.points[0] == (0.0, 1.0))
    assert by_block.color == "#ff0000", "BYBLOCK inherits the insert colour"
    assert by_layer.color == "#00ff00", "BYLAYER takes its own layer's colour"


def test_colour_index_seven_maps_to_the_editor_ink() -> None:
    builder = ObjectStreamBuilder()
    builder.model_space([builder.line((0.0, 0.0), (1.0, 0.0), color=7)])
    result = decode_object_stream(builder.build())

    assert result.primitives[0].color == "#111827"


def test_true_colour_is_preferred_when_present() -> None:
    builder = ObjectStreamBuilder()
    builder.model_space([builder.line((0.0, 0.0), (1.0, 0.0), color=256, rgb="123456")])
    result = decode_object_stream(builder.build())

    assert result.primitives[0].color == "#123456"


def test_extrusion_minus_one_mirrors_the_entity() -> None:
    builder = ObjectStreamBuilder()
    builder.model_space(
        [builder.line((10.0, 0.0), (30.0, 0.0), extraction=(0.0, 0.0, -1.0))]
    )
    result = decode_object_stream(builder.build())

    assert result.primitives[0].points == ((-10.0, 0.0), (-30.0, 0.0))


def test_missing_block_definition_is_reported() -> None:
    builder = ObjectStreamBuilder()
    builder.model_space([builder.insert(0xFFFF, (0.0, 0.0))])
    result = decode_object_stream(builder.build())

    assert result.primitives == []
    assert result.issues.codes["CAD_MISSING_BLOCK_DEFINITION"] == 1


def test_block_recursion_stops_at_the_configured_depth() -> None:
    builder = ObjectStreamBuilder()
    inner = builder.block("INNER", [builder.circle((0.0, 0.0), 1.0)])
    current = inner
    for index in range(6):
        current = builder.block(f"LEVEL{index}", [builder.insert(current, (1.0, 0.0))])
    builder.model_space([builder.insert(current, (0.0, 0.0))])
    result = decode_object_stream(builder.build(), max_block_depth=2)

    assert result.issues.codes["CAD_BLOCK_DEPTH_LIMIT"] == 1


def test_bounds_are_reported_for_the_decoded_geometry() -> None:
    builder = ObjectStreamBuilder()
    builder.model_space(
        [builder.line((0.0, 0.0), (10.0, 0.0)), builder.circle((5.0, 5.0), 2.0)]
    )
    result = decode_object_stream(builder.build())

    assert bounds_of(result.primitives) == pytest.approx((0.0, 0.0, 10.0, 7.0))


def test_payload_without_objects_is_refused() -> None:
    with pytest.raises(CadGeometryError) as excinfo:
        decode_object_stream({"FILEHEADER": {"version": "AC1032"}})

    assert excinfo.value.code == "object_stream_missing"


def test_payload_without_model_space_is_refused() -> None:
    builder = ObjectStreamBuilder()
    builder.block("ONLY_BLOCK", [builder.line((0.0, 0.0), (1.0, 0.0))])
    with pytest.raises(CadGeometryError) as excinfo:
        decode_object_stream(builder.build())

    assert excinfo.value.code == "no_model_space"


def test_entity_kinds_are_counted_for_coverage_reporting() -> None:
    builder = ObjectStreamBuilder()
    builder.model_space(
        [builder.line((0.0, 0.0), (1.0, 0.0)), builder.circle((0.0, 0.0), 1.0)]
    )
    result = decode_object_stream(builder.build())

    assert result.entity_kinds == {"LINE": 1, "CIRCLE": 1}
