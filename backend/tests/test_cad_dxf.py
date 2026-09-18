"""Reader-level tests for the DXF group-code reader.

The reader is the one part of CAD import that must be exactly right without help from an
external tool, so these tests state the awkward records directly: a POLYLINE with its
dummy head point, a bulge that must curve to the correct side, a mirrored block insert,
an array insert, a hatch with islands, a dimension that draws through an anonymous
block, codepage-encoded text, and the entities the reader deliberately does not decode.

Where a CAD library is available (a test-only dependency) one case cross-checks this
reader against it: two independent implementations agreeing on the same file is worth
more than either one agreeing with itself.
"""

from __future__ import annotations

import math

import pytest
from cad_fixtures import DxfBuilder

from agentcad.cad_dxf import read_dxf
from agentcad.cad_geometry import CadGeometryError, bounds_of


def test_line_keeps_its_endpoints() -> None:
    builder = DxfBuilder()
    builder.line((10.0, 20.0), (30.0, 40.0))
    result = read_dxf(builder.build())

    assert len(result.primitives) == 1
    primitive = result.primitives[0]
    assert primitive.kind == "line"
    assert primitive.points == ((10.0, 20.0), (30.0, 40.0))


def test_zero_length_lines_are_dropped_and_reported() -> None:
    builder = DxfBuilder()
    builder.line((5.0, 5.0), (5.0, 5.0))
    result = read_dxf(builder.build())

    assert result.primitives == []
    assert result.issues.codes["CAD_DEGENERATE_GEOMETRY"] == 1


def test_old_style_polyline_does_not_gain_a_vertex_at_the_origin() -> None:
    """A POLYLINE head carries a dummy 10/20 pair; it must not become a vertex."""

    builder = DxfBuilder()
    builder.polyline([(100.0, 100.0), (200.0, 100.0), (200.0, 200.0)], closed=False)
    result = read_dxf(builder.build())

    assert len(result.primitives) == 1
    points = result.primitives[0].points
    assert points == ((100.0, 100.0), (200.0, 100.0), (200.0, 200.0))
    assert (0.0, 0.0) not in points
    assert result.primitives[0].closed is False


def test_polyline_bulge_curves_to_the_correct_side() -> None:
    """Bulge 1 on a 100-unit chord is a semicircle bulging left of start -> end."""

    builder = DxfBuilder()
    builder.lwpolyline([(0.0, 0.0), (100.0, 0.0)], bulges=[1.0])
    result = read_dxf(builder.build())

    points = result.primitives[0].points
    assert points[0] == (0.0, 0.0)
    assert points[-1] == (100.0, 0.0)
    apex = max(points, key=lambda point: point[1])
    assert apex[1] > 40.0, "a semicircle on a 100-unit chord rises about 50 units"
    assert all(point[1] >= -1e-6 for point in points), "the arc must bulge upward"

    builder = DxfBuilder()
    builder.lwpolyline([(0.0, 0.0), (100.0, 0.0)], bulges=[-1.0])
    negative = read_dxf(builder.build()).primitives[0].points
    assert min(point[1] for point in negative) < -40.0


def test_closed_polyline_closes_through_its_last_bulge() -> None:
    builder = DxfBuilder()
    builder.lwpolyline(
        [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)],
        closed=True,
        bulges=[1.0, 0.0, 0.0],
    )
    result = read_dxf(builder.build())

    primitive = result.primitives[0]
    assert primitive.closed is True
    assert len(primitive.points) > 3


def test_arc_angles_are_degrees_in_the_file_and_radians_in_geometry() -> None:
    builder = DxfBuilder()
    builder.arc((0.0, 0.0), 10.0, 0.0, 90.0)
    primitive = read_dxf(builder.build()).primitives[0]

    assert primitive.kind == "arc"
    assert primitive.start_angle == pytest.approx(0.0)
    assert primitive.end_angle == pytest.approx(math.pi / 2)
    points = primitive.curve_points(24)
    assert points[0] == pytest.approx((10.0, 0.0))
    assert points[-1] == pytest.approx((0.0, 10.0), abs=1e-6)


def test_ellipse_keeps_its_axis_angle_and_ratio() -> None:
    builder = DxfBuilder()
    builder.ellipse((10.0, 20.0), (5.0, 5.0), 0.5)
    primitive = read_dxf(builder.build()).primitives[0]

    assert primitive.kind == "ellipse"
    assert primitive.radius == pytest.approx(math.hypot(5.0, 5.0))
    assert primitive.axis_ratio == pytest.approx(0.5)
    assert primitive.major_axis_angle == pytest.approx(math.pi / 4)


def test_circle_becomes_a_native_circle() -> None:
    builder = DxfBuilder()
    builder.circle((12.0, 34.0), 5.0)
    primitive = read_dxf(builder.build()).primitives[0]

    assert primitive.kind == "circle"
    assert primitive.center == (12.0, 34.0)
    assert primitive.radius == 5.0


def test_mirrored_insert_mirrors_its_block_geometry() -> None:
    """A negative insert scale is a mirror, and a scalar radius cannot express it."""

    builder = DxfBuilder()
    # An asymmetric block, so a mirror is observable: local x in {-10, 20}.
    builder.rect_block("VALVE", corner=(-10.0, -10.0), size=(30.0, 20.0))
    builder.insert("VALVE", (100.0, 0.0), scale=(-1.0, 1.0))
    result = read_dxf(builder.build())

    assert len(result.primitives) == 4
    xs = sorted({point[0] for primitive in result.primitives for point in primitive.points})
    # The mirror turns {-10, 20} into {-20, 10}, and then the insert point adds 100.
    assert xs == [80.0, 110.0]
    assert result.primitives[0].block == "VALVE"


def test_insert_scale_is_applied_to_block_geometry() -> None:
    builder = DxfBuilder()
    builder.rect_block("VALVE", corner=(-10.0, -10.0), size=(20.0, 20.0))
    builder.insert("VALVE", (0.0, 0.0), scale=(2.0, 2.0))
    result = read_dxf(builder.build())

    box = bounds_of(result.primitives)
    assert box == pytest.approx((-20.0, -20.0, 20.0, 20.0))


def test_array_insert_repeats_the_block_at_the_declared_spacing() -> None:
    builder = DxfBuilder()
    builder.rect_block("TAG", corner=(0.0, 0.0), size=(10.0, 10.0))
    builder.insert("TAG", (0.0, 0.0), columns=3, rows=2, column_spacing=100.0, row_spacing=50.0)
    result = read_dxf(builder.build())

    # 4 lines per rectangle x 6 instances.
    assert len(result.primitives) == 24
    xs = sorted({round(point[0], 3) for primitive in result.primitives for point in primitive.points})
    ys = sorted({round(point[1], 3) for primitive in result.primitives for point in primitive.points})
    assert xs == [0.0, 10.0, 100.0, 110.0, 200.0, 210.0]
    assert ys == [0.0, 10.0, 50.0, 60.0]


def test_nested_blocks_are_expanded_to_the_depth_limit() -> None:
    builder = DxfBuilder()
    builder.rect_block("INNER", corner=(0.0, 0.0), size=(10.0, 10.0))
    inner_lines = builder.blocks[-1][1]
    builder.block("OUTER", [[(0, "INSERT"), (8, "0"), (2, "INNER"), (10, 0.0), (20, 0.0)]])
    builder.insert("OUTER", (100.0, 100.0))
    result = read_dxf(builder.build())

    assert len(result.primitives) == len(inner_lines)
    box = bounds_of(result.primitives)
    assert box == pytest.approx((100.0, 100.0, 110.0, 110.0))


def test_missing_block_definition_is_reported_not_guessed() -> None:
    builder = DxfBuilder()
    builder.insert("NOT_DEFINED", (0.0, 0.0))
    result = read_dxf(builder.build())

    assert result.primitives == []
    assert result.issues.codes["CAD_MISSING_BLOCK_DEFINITION"] == 1


def test_dimension_geometry_comes_from_its_anonymous_block() -> None:
    """A dimension draws through a block; dropping it would drop dimension linework."""

    builder = DxfBuilder()
    builder.block(
        "*D1",
        [
            [(0, "LINE"), (8, "0"), (10, 0.0), (20, 0.0), (11, 50.0), (21, 0.0)],
            [(0, "TEXT"), (8, "0"), (10, 25.0), (20, 5.0), (40, 2.5), (1, "1200")],
        ],
    )
    builder.dimension("*D1")
    result = read_dxf(builder.build())

    kinds = sorted(primitive.kind for primitive in result.primitives)
    assert kinds == ["line", "text"]
    assert result.primitives[1].text == "1200"


def test_solid_hatch_becomes_a_filled_polygon() -> None:
    builder = DxfBuilder()
    builder.hatch([[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]], solid=True)
    result = read_dxf(builder.build())

    assert len(result.primitives) == 1
    primitive = result.primitives[0]
    assert primitive.kind == "fill"
    assert primitive.points == ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0))
    assert primitive.closed is True


def test_solid_hatch_with_islands_is_outlined_rather_than_filled() -> None:
    """A hole must not be filled in; the honest approximation is an outline."""

    builder = DxfBuilder()
    builder.hatch(
        [
            [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
            [(4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0)],
        ],
        solid=True,
    )
    result = read_dxf(builder.build())

    assert [primitive.kind for primitive in result.primitives] == ["polyline", "polyline"]
    assert result.issues.codes["CAD_HATCH_ISLANDS_OUTLINED"] == 1


def test_pattern_hatch_is_skipped_and_reported() -> None:
    builder = DxfBuilder()
    builder.hatch(
        [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]],
        solid=False,
        pattern="ANSI31",
    )
    result = read_dxf(builder.build())

    assert result.primitives == []
    assert result.issues.codes["CAD_PATTERN_HATCH_SKIPPED"] == 1


def test_unknown_entities_are_counted_with_their_type() -> None:
    builder = DxfBuilder()
    builder.unknown("SPLINE")
    builder.unknown("MLEADER")
    builder.line((0.0, 0.0), (1.0, 0.0))
    result = read_dxf(builder.build())

    assert len(result.primitives) == 1
    assert result.issues.codes["CAD_UNSUPPORTED_ENTITIES"] == 2
    assert result.issues.details["CAD_UNSUPPORTED_ENTITIES"] == {"SPLINE": 1, "MLEADER": 1}


def test_entities_the_reader_ignores_are_not_reported_as_losses() -> None:
    builder = DxfBuilder()
    builder.unknown("POINT")
    builder.unknown("VIEWPORT")
    result = read_dxf(builder.build())

    assert result.issues.empty()


def test_paper_space_blocks_are_counted_as_skipped() -> None:
    builder = DxfBuilder()
    builder.block(
        "*Paper_Space",
        [[(0, "LINE"), (8, "0"), (10, 0.0), (20, 0.0), (11, 1.0), (21, 1.0)]],
    )
    builder.line((0.0, 0.0), (5.0, 0.0))
    result = read_dxf(builder.build())

    assert len(result.primitives) == 1
    assert result.paper_space_skipped == 1
    assert result.issues.codes["CAD_PAPER_SPACE_SKIPPED"] == 1


def test_text_alignment_point_is_used_when_alignment_is_set() -> None:
    builder = DxfBuilder()
    builder.text("P-101", (0.0, 0.0), 40.0, halign=2, alignment=(100.0, 200.0))
    primitive = read_dxf(builder.build()).primitives[0]

    assert primitive.anchor == "end"
    assert primitive.points == ((100.0, 200.0),)


def test_text_symbol_codes_are_resolved() -> None:
    builder = DxfBuilder()
    builder.text("45%%d %%p0.5 %%c25", (0.0, 0.0), 40.0)
    primitive = read_dxf(builder.build()).primitives[0]

    assert primitive.text == "45° ±0.5 ⌀25"


def test_mtext_attachment_and_format_codes_are_resolved() -> None:
    builder = DxfBuilder()
    builder.mtext(
        "{\\fSimSun|b0|i0;熔盐泵}",
        (10.0, 20.0),
        25.0,
        attachment=5,
        chunks=["第一行", "\\P第二行"],
    )
    primitive = read_dxf(builder.build()).primitives[0]

    assert primitive.kind == "text"
    # MTEXT concatenates its 3-chunks first and the final 1-chunk last; the font run and
    # the braces are formatting, and a paragraph break flattens to a space because the
    # editor's text element renders one line (a newline would silently disappear).
    assert primitive.text == "第一行 第二行熔盐泵"
    assert primitive.anchor == "middle"
    assert primitive.height == 25.0


def test_rotated_text_is_placed_at_its_anchor_and_reported() -> None:
    builder = DxfBuilder()
    builder.text("ROTATED", (0.0, 0.0), 10.0, rotation=90.0)
    result = read_dxf(builder.build())

    assert result.primitives[0].points == ((0.0, 0.0),)
    assert result.issues.codes["CAD_TEXT_ROTATION_IGNORED"] == 1


def test_codepage_encoded_text_is_decoded_from_the_file_header() -> None:
    builder = DxfBuilder()
    builder.codepage_value("ANSI_936")
    builder.text("气路系统总图", (0.0, 0.0), 40.0)
    data = builder.source().encode("gbk")

    result = read_dxf(data)

    assert result.encoding == "gbk"
    assert result.primitives[0].text == "气路系统总图"


def test_layer_colour_is_used_for_bylayer_entities() -> None:
    builder = DxfBuilder()
    builder.layer("PIPE", color=3)
    builder.line((0.0, 0.0), (1.0, 0.0), layer="PIPE", color=256)
    result = read_dxf(builder.build())

    assert result.primitives[0].color == "#00ff00"
    assert result.layers["PIPE"] == "#00ff00"


def test_explicit_colour_overrides_the_layer_colour() -> None:
    builder = DxfBuilder()
    builder.layer("PIPE", color=3)
    builder.line((0.0, 0.0), (1.0, 0.0), layer="PIPE", color=1)
    result = read_dxf(builder.build())

    assert result.primitives[0].color == "#ff0000"


def test_true_colour_record_wins_over_the_index() -> None:
    builder = DxfBuilder()
    builder.add("LINE", [(8, "0"), (62, 1), (420, 0x123456), (10, 0.0), (20, 0.0), (11, 1.0), (21, 0.0)])
    result = read_dxf(builder.build())

    assert result.primitives[0].color == "#123456"


def test_header_extents_are_read_when_present() -> None:
    builder = DxfBuilder()
    builder.extents((0.0, 0.0, 1000.0, 400.0))
    builder.line((0.0, 0.0), (10.0, 10.0))
    result = read_dxf(builder.build())

    assert result.extents == (0.0, 0.0, 1000.0, 400.0)


def test_linetype_name_is_carried_on_the_primitive() -> None:
    builder = DxfBuilder()
    builder.add(
        "LINE",
        [(8, "0"), (6, "DASHED"), (10, 0.0), (20, 0.0), (11, 1.0), (21, 0.0)],
    )
    result = read_dxf(builder.build())

    assert result.primitives[0].linetype == "DASHED"


def test_binary_dxf_is_refused_with_a_code() -> None:
    with pytest.raises(CadGeometryError) as excinfo:
        read_dxf(b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\x00" * 64)

    assert excinfo.value.code == "binary_dxf_unsupported"


def test_a_stream_that_is_not_dxf_is_refused() -> None:
    with pytest.raises(CadGeometryError) as excinfo:
        read_dxf(b"this is not a drawing at all\n")

    assert excinfo.value.code == "dxf_not_recognised"


def test_reader_agrees_with_an_independent_dxf_library() -> None:
    """Cross-check against ezdxf (a test-only dependency) on the same drawing.

    Two independent readers agreeing on entity counts and geometry is stronger evidence
    than either reader agreeing with its own fixtures.
    """

    ezdxf = pytest.importorskip("ezdxf")
    document = ezdxf.new("R2010", setup=True)
    modelspace = document.modelspace()
    modelspace.add_line((0.0, 0.0), (100.0, 0.0))
    modelspace.add_line((0.0, 0.0), (0.0, 100.0))
    modelspace.add_circle((50.0, 50.0), 25.0)
    modelspace.add_arc((50.0, 50.0), 10.0, 0.0, 90.0)
    modelspace.add_lwpolyline([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)], close=True)
    modelspace.add_text("P-101", dxfattribs={"height": 5.0}).set_placement((5.0, 5.0))
    block = document.blocks.new("VALVE")
    block.add_line((-2.0, -2.0), (2.0, -2.0))
    block.add_line((2.0, -2.0), (2.0, 2.0))
    modelspace.add_blockref("VALVE", (200.0, 200.0))
    path = None
    try:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crosscheck.dxf"
            document.saveas(path)
            data = path.read_bytes()
    finally:
        del path

    result = read_dxf(data)
    kinds = sorted(primitive.kind for primitive in result.primitives)
    assert kinds == ["arc", "circle", "line", "line", "line", "line", "polyline", "text"]
    circle = next(primitive for primitive in result.primitives if primitive.kind == "circle")
    assert circle.center == (50.0, 50.0)
    assert circle.radius == 25.0
    text = next(primitive for primitive in result.primitives if primitive.kind == "text")
    assert text.text == "P-101"
    assert text.height == pytest.approx(5.0)


def test_block_definitions_are_not_imported_as_model_space_entities() -> None:
    """A block's geometry belongs to its insert, not to the sheet on its own."""

    builder = DxfBuilder()
    builder.rect_block("UNUSED", corner=(0.0, 0.0), size=(10.0, 10.0))
    builder.line((0.0, 0.0), (1.0, 0.0))
    result = read_dxf(builder.build())

    assert len(result.primitives) == 1
    assert result.primitives[0].kind == "line"
