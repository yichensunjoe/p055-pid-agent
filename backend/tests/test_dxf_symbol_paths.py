"""DXF-Q1 repair: one shared path grammar between the freeze and the exporter.

The qualification run (M7-Q1) drew a real buffer tank -- legal under the M7
symbol-geometry freeze, rendered correctly on canvas and PDF -- and the DXF exporter
rejected it, because the exporter re-implemented a *narrower* path grammar (M/L/Q/Z)
than the one the freeze measures (M/L/H/V/C/S/Q/T/A/Z, relative forms included). These
tests pin the repair: the contract that ties both consumers to one grammar, the
standalone symbols the isolation run named, and a full end-to-end drawing whose symbol
geometry must actually survive into the DXF.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentcad.cad_dxf import read_dxf
from agentcad.config import Settings
from agentcad.m7_symbol_geometry import unstroked_shape_bounds
from agentcad.main import create_app
from agentcad.symbol_paths import (
    SymbolPathError,
    parse_symbol_path,
    sample_symbol_path,
)
from agentcad.symbol_paths import (
    path_points as shared_path_points,
)
from agentcad.symbols import SymbolRegistry


def _builtin_path_shapes() -> list[tuple[str, int, str]]:
    """Every path shape the built-in catalogue carries, with its symbol and index."""

    shapes: list[tuple[str, int, str]] = []
    for definition in SymbolRegistry().list():
        for index, shape in enumerate(definition.shapes):
            if shape.get("type") == "path" and shape.get("d"):
                shapes.append((definition.key, index, str(shape["d"])))
    return shapes


# ---------------------------------------------------------------------------------------------
# The contract: one grammar. A path the freeze accepts can never be rejected by the exporter
# for "another grammar" -- and both consumers must agree on what the path touches.
# ---------------------------------------------------------------------------------------------


#: The point set the freeze measured for every built-in path before the grammar was
#: shared, captured from the pre-repair implementation (main @ ba4d447). The shared
#: module must reproduce these exactly: the extraction changed where the grammar
#: lives, never what any path means.
GOLDEN_PATH_POINTS: dict[str, list[list[float]]] = {"octagon_box#0": [[20.0, 0.0], [50.0, 0.0], [70.0, 20.0], [70.0, 50.0], [50.0, 70.0], [20.0, 70.0], [0.0, 50.0], [0.0, 20.0], [20.0, 0.0]], "hexagon_tag#0": [[20.0, 5.0], [60.0, 5.0], [80.0, 35.0], [60.0, 65.0], [20.0, 65.0], [0.0, 35.0], [20.0, 5.0]], "revision_cloud#0": [[15.0, 20.0], [15.0, 8.0], [30.0, 8.0], [35.0, 16.0], [45.0, 6.0], [65.0, 6.0], [72.0, 16.0], [85.0, 8.0], [95.0, 20.0], [90.0, 32.0], [100.0, 42.0], [95.0, 58.0], [82.0, 60.0], [72.0, 68.0], [55.0, 68.0], [45.0, 60.0], [35.0, 68.0], [20.0, 65.0], [16.0, 55.0], [5.0, 48.0], [5.0, 30.0], [15.0, 20.0], [15.0, 20.0]], "block_arrow_right#0": [[0.0, 12.0], [45.0, 12.0], [45.0, 0.0], [80.0, 20.0], [45.0, 40.0], [45.0, 28.0], [0.0, 28.0], [0.0, 12.0]], "parallelogram_io#0": [[18.0, 0.0], [80.0, 0.0], [62.0, 50.0], [0.0, 50.0], [18.0, 0.0]], "callout_bubble#0": [[10.0, 0.0], [80.0, 0.0], [85.0, 0.0], [90.0, 5.0], [90.0, 10.0], [90.0, 40.0], [90.0, 45.0], [85.0, 50.0], [80.0, 50.0], [30.0, 50.0], [10.0, 60.0], [16.0, 50.0], [10.0, 50.0], [5.0, 50.0], [0.0, 45.0], [0.0, 40.0], [0.0, 10.0], [0.0, 5.0], [5.0, 0.0], [10.0, 0.0], [10.0, 0.0]], "trapezoid_hopper#0": [[0.0, 0.0], [80.0, 0.0], [55.0, 60.0], [25.0, 60.0], [0.0, 0.0]], "cube_cabinet#0": [[0.0, 25.0], [55.0, 25.0], [55.0, 80.0], [0.0, 80.0], [0.0, 25.0]], "cube_cabinet#1": [[0.0, 25.0], [25.0, 0.0], [80.0, 0.0], [55.0, 25.0], [0.0, 25.0]], "cube_cabinet#2": [[55.0, 25.0], [80.0, 0.0], [80.0, 55.0], [55.0, 80.0], [55.0, 25.0]], "cylinder_vessel#0": [[0.0, 20.0], [0.0, 5.0], [70.0, 5.0], [70.0, 20.0], [70.0, 80.0], [70.0, 95.0], [0.0, 95.0], [0.0, 80.0], [0.0, 20.0]], "cylinder_vessel#1": [[0.0, 20.0], [0.0, 35.0], [70.0, 35.0], [70.0, 20.0]], "diamond_decision#0": [[40.0, 0.0], [80.0, 30.0], [40.0, 60.0], [0.0, 30.0], [40.0, 0.0]], "rupture_disc#1": [[25.0, 12.0], [40.0, 30.0], [25.0, 48.0]], "rupture_disc#2": [[55.0, 12.0], [40.0, 30.0], [55.0, 48.0]], "horizontal_vessel#0": [[20.0, 5.0], [0.0, 40.0], [20.0, 75.0], [110.0, 75.0], [130.0, 40.0], [110.0, 5.0], [20.0, 5.0]], "reactor_vessel#0": [[15.0, 25.0], [50.0, 5.0], [85.0, 25.0], [85.0, 125.0], [50.0, 145.0], [15.0, 125.0], [15.0, 25.0]], "gas_tank#0": [[10.0, 25.0], [45.0, 0.0], [80.0, 25.0], [80.0, 115.0], [45.0, 140.0], [10.0, 115.0], [10.0, 25.0]], "separator_vessel#0": [[15.0, 20.0], [45.0, 0.0], [75.0, 20.0], [75.0, 120.0], [45.0, 140.0], [15.0, 120.0], [15.0, 20.0]], "fractionation_column#0": [[0.0, 25.0], [0.0, 0.0], [70.0, 50.0], [0.0, 25.0], [70.0, 25.0], [70.0, 25.0], [70.0, 135.0], [0.0, 110.0], [70.0, 160.0], [70.0, 135.0], [0.0, 135.0], [0.0, 135.0], [0.0, 25.0]], "buffer_tank#0": [[0.0, 35.0], [0.0, 0.0], [70.0, 70.0], [0.0, 35.0], [70.0, 35.0], [70.0, 35.0], [70.0, 65.0], [0.0, 30.0], [70.0, 100.0], [70.0, 65.0], [0.0, 65.0], [0.0, 65.0], [0.0, 35.0]], "vacuum_pump#3": [[45.0, 44.0], [70.0, 20.0], [55.0, 16.0], [45.0, 12.0], [45.0, 0.0]], "centrifugal_pump#2": [[38.0, 38.0], [58.0, 22.0], [48.0, 0.0]], "agitator#3": [[14.0, 104.0], [35.0, 94.0], [56.0, 104.0]], "venturi_mixer#0": [[0.0, 16.0], [30.0, 24.0], [30.0, 32.0], [0.0, 40.0], [0.0, 16.0]], "venturi_mixer#1": [[30.0, 24.0], [90.0, 14.0], [90.0, 42.0], [30.0, 32.0], [30.0, 24.0]], "flexible_hose#0": [[0.0, 25.0], [15.0, 25.0], [25.0, 5.0], [35.0, 25.0], [45.0, 45.0], [55.0, 25.0], [65.0, 5.0], [75.0, 25.0], [100.0, 25.0]], "cyclone_separator#0": [[10.0, 15.0], [60.0, 15.0], [60.0, 45.0], [42.0, 100.0], [28.0, 100.0], [10.0, 45.0], [10.0, 15.0]], "bag_filter#0": [[10.0, 10.0], [70.0, 10.0], [70.0, 65.0], [48.0, 95.0], [32.0, 95.0], [10.0, 65.0], [10.0, 10.0]], "three_way_control_valve#0": [[0.0, 40.0], [40.0, 55.0], [0.0, 70.0], [0.0, 40.0]], "three_way_control_valve#1": [[80.0, 40.0], [40.0, 55.0], [80.0, 70.0], [80.0, 40.0]], "three_way_control_valve#2": [[25.0, 75.0], [40.0, 55.0], [55.0, 75.0], [25.0, 75.0]], "three_way_control_valve#4": [[20.0, 24.0], [20.0, 12.0], [60.0, 36.0], [20.0, 24.0], [60.0, 24.0], [60.0, 24.0], [20.0, 24.0]], "pressure_reducing_valve#0": [[0.0, 18.0], [35.0, 30.0], [0.0, 42.0], [0.0, 18.0]], "pressure_reducing_valve#1": [[70.0, 18.0], [35.0, 30.0], [70.0, 42.0], [70.0, 18.0]], "pressure_reducing_valve#4": [[55.0, 30.0], [55.0, 8.0], [48.0, 8.0]], "butterfly_valve#1": [[28.0, 20.0], [40.0, 35.0], [28.0, 50.0]], "butterfly_valve#2": [[52.0, 20.0], [40.0, 35.0], [52.0, 50.0]], "angle_valve#0": [[0.0, 15.0], [30.0, 30.0], [0.0, 45.0], [0.0, 15.0]], "angle_valve#1": [[15.0, 60.0], [30.0, 30.0], [45.0, 60.0], [15.0, 60.0]]}


@pytest.mark.parametrize(
    "symbol_key,shape_index,described",
    _builtin_path_shapes(),
    ids=[f"{key}#{i}" for key, i, _d in _builtin_path_shapes()],
)
def test_every_builtin_path_the_freeze_accepts_samples_for_dxf(
    symbol_key: str, shape_index: int, described: str
) -> None:
    """The freeze's legality notion is the reference: its bounds reader must accept the
    path. The exporter reads the same grammar -- it is the same code now -- so sampling
    must succeed too: not skip a command, not drop the shape, just read it."""

    key = f"{symbol_key}#{shape_index}"
    bounds = unstroked_shape_bounds({"type": "path", "d": described})  # the freeze's entry
    sampled = sample_symbol_path(described)  # raises SymbolPathError on any grammar drift

    assert sampled, f"{key} sampled to nothing"
    # Every sampled point lies inside the extent the freeze measures: the exporter's
    # reading of the path cannot wander outside the geometry the layout proved fits.
    margin = 1e-6
    for x, y in sampled:
        assert bounds[0] - margin <= x <= bounds[2] + margin, (key, x, bounds)
        assert bounds[1] - margin <= y <= bounds[3] + margin, (key, y, bounds)


def test_the_shared_grammar_reproduces_the_frozen_point_sets_exactly() -> None:
    """Golden regression: the shared module's extent reading must equal, point for
    point, what the freeze measured before the extraction. This is the proof that
    deduplication moved the grammar without changing any path's meaning."""

    for symbol_key, shape_index, described in _builtin_path_shapes():
        key = f"{symbol_key}#{shape_index}"
        assert [list(p) for p in shared_path_points(described)] == GOLDEN_PATH_POINTS[key], (
            f"{key}: shared grammar changed what a frozen path means"
        )


def test_the_q1_failure_path_is_a_two_arc_stadium() -> None:
    """The exact path M7-Q1 drew: two arcs closing a stadium. Both arcs survive as arc
    segments, so the exporter cannot silently flatten them away."""

    # one move + three drawing segments; the close-path is a path-level event, not a
    # segment, and sampling renders it as the closing edge back to the start.
    segments = parse_symbol_path("M 0 35 A 35 35 0 0 1 70 35 L 70 65 A 35 35 0 0 1 0 65 Z")
    assert [s.kind for s in segments] == ["arc", "line", "arc"]
    sampled = sample_symbol_path("M 0 35 A 35 35 0 0 1 70 35 L 70 65 A 35 35 0 0 1 0 65 Z")
    assert sampled[0] == sampled[-1], "the sampled outline closes back on itself"


def test_a_genuinely_broken_path_is_still_refused() -> None:
    """The repair widens the grammar, not the tolerance: a path with a command where a
    coordinate belongs is still an error, in both consumers."""

    with pytest.raises(SymbolPathError):
        sample_symbol_path("M 0 35 L 70 Z 10")
    from agentcad.m7_symbol_geometry import SymbolShapeOutOfBoundsError

    with pytest.raises(SymbolShapeOutOfBoundsError):
        unstroked_shape_bounds({"type": "path", "d": "M 0 35 L 70 Z 10"})


# ---------------------------------------------------------------------------------------------
# End to end: the isolation run's symbols, and a full governed drawing, exported to DXF
# with the symbol geometry present in the file -- read back by the repo's own parser.
# ---------------------------------------------------------------------------------------------

SENTENCE = "添加一个缓冲罐 V-101，添加一台燃料盐泵 P-101，把 V-101 接到 P-101"
EDIT = "再添加一个缓冲罐 V-102，把 P-101 接到 V-102"


class _JudgeStub:
    def __call__(self, state: dict, questions: dict) -> dict:
        return {
            "model": "judge-stub",
            "answers": {
                question: {
                    "choice": sorted(definition["criteria"])[0],
                    "confidence": 0.9,
                }
                for question, definition in questions.items()
            },
        }


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from agentcad.typesafe import TypesafeClient

    monkeypatch.setattr(TypesafeClient, "judge", _JudgeStub())
    app = create_app(
        Settings(
            database_path=tmp_path / "dxf-q1.db",
            cors_origins=["http://localhost:5173"],
            frontend_dist=tmp_path / "missing-dist",
        )
    )
    with TestClient(app) as test_client:
        yield test_client


def _symbol_element(element_id: str, symbol_key: str) -> dict:
    from agentcad.symbols import SymbolRegistry

    definition = SymbolRegistry().get(symbol_key)
    return {
        "id": element_id,
        "type": "symbol",
        "symbol_key": symbol_key,
        "position": {"x": 100.0, "y": 100.0},
        "width": float(definition.width),
        "height": float(definition.height),
        "rotation": 0,
        "label": "ISO",
        "properties": {},
        "name": "ISO",
        "layer_id": "layer_default",
        "system_id": "system_default",
        "style": {"stroke": "#111827", "fill": "none", "stroke_width": 1.5, "opacity": 1, "dash": []},
    }


def _doc_with_symbol(client: TestClient, symbol_key: str) -> str:
    document_id = client.post("/api/v2/documents", json={"name": f"iso-{symbol_key}"}).json()["id"]
    response = client.post(
        f"/api/v2/documents/{document_id}/transactions",
        json={
            "expected_revision": 0,
            "operations": [{"op": "add_element", "element": _symbol_element("iso", symbol_key)}],
            "label": "iso",
        },
    )
    assert response.status_code == 200, response.text
    return document_id


def _export_dxf(client: TestClient, document_id: str) -> bytes:
    response = client.get(f"/api/v2/documents/{document_id}/export-v2.dxf?range=content&padding=24")
    assert response.status_code == 200, response.text
    return response.content


def _readback_vertices(data: bytes) -> list[tuple[float, float]]:
    """The vertices of the largest polyline the repo's own DXF parser finds -- the symbol
    outline. Labels and leader lines are text primitives and stay out of the way."""

    result = read_dxf(data)
    polylines = [
        [(float(p[0]), float(p[1])) for p in primitive.points]
        for primitive in result.primitives
        if getattr(primitive, "kind", "") == "polyline" and getattr(primitive, "points", None)
    ]
    assert polylines, "the export carried no polyline geometry at all"
    return max(polylines, key=len)


def test_buffer_tank_standalone_exports_with_its_stadium_geometry(client: TestClient) -> None:
    """The exact symbol M7-Q1 drew. The DXF must contain the tank's rounded outline --
    not a bounding box, not an empty file, the actual sampled stadium."""

    document_id = _doc_with_symbol(client, "buffer_tank")
    data = _export_dxf(client, document_id)
    assert data.startswith(b"999"), data[:80]
    vertices = _readback_vertices(data)
    # The content-range export translates the drawing, so assertions are about spans.
    # The stadium is 70 wide (two radius-35 arcs) and the sampled arcs put far more
    # vertices along the outline than a bounding rectangle's four corners would --
    # that is the "no bounding-box fallback" proof.
    assert len(vertices) >= 20, f"expected the sampled outline, got {len(vertices)} vertices"
    xs = [v[0] for v in vertices]
    assert abs(max(xs) - min(xs) - 70.0) < 1.0, (min(xs), max(xs))


def test_pump_standalone_still_exports_unchanged(client: TestClient) -> None:
    """The symbol that always worked must not change shape: regression anchor for the
    grammar widening."""

    document_id = _doc_with_symbol(client, "positive_displacement_pump")
    data = _export_dxf(client, document_id)
    result = read_dxf(data)
    circles = [p for p in result.primitives if getattr(p, "kind", "") == "circle"]
    # the pump is one casing circle plus two gear circles; the export must carry them
    assert len(circles) >= 3, f"pump circles must survive, got {len(circles)}"


def test_the_q1_chain_draws_and_exports_dxf_end_to_end(client: TestClient) -> None:
    """The full M7-Q1 loop on the repaired exporter: sentence -> r1, edit -> r2, and the
    r2 drawing -- the exact downstream case that failed qualification -- exports DXF
    with every symbol's geometry present."""

    document_id = client.post("/api/v2/documents", json={"name": "q1-requal"}).json()["id"]
    drawn = client.post(
        f"/api/v2/documents/{document_id}/agent/text-plan",
        json={"sentence": SENTENCE, "api_key": "test-key"},
    )
    assert drawn.status_code == 200, drawn.text
    first = drawn.json()
    assert first["revision"] == 1

    edited = client.post(
        f"/api/v2/documents/{document_id}/agent/text-edit",
        json={
            "sentence": EDIT,
            "expected_revision": 1,
            "base_spec_digest": first["spec_digest"],
            "api_key": "test-key",
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"] == 2

    data = _export_dxf(client, document_id)
    result = read_dxf(data)
    polylines = [
        p for p in result.primitives
        if getattr(p, "kind", "") == "polyline" and getattr(p, "points", None)
    ]
    # every symbol outline is its own polyline: two tanks and a pump body, plus the
    # connectors, must all be present -- nothing dropped to make the export pass.
    assert len(polylines) >= 4, f"expected at least four outlines, got {len(polylines)}"
    assert max(len(p.points) for p in polylines) >= 20, "an arc outline must be sampled"
