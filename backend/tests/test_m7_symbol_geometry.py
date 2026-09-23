"""M7-2 phase 2B step 3: the frozen symbol geometry snapshot, and what it must refuse.

The tests here are about identity rather than about drawings: the snapshot exists so that a
layout can say *which* symbol geometry it was placed against. So they check the two directions
that make that claim real -- a change to the facts this drawing uses must move the digest, and a
change to a symbol it does not use must not -- and they check that the facts never grow a
position.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcad import m7_layout_contract as contract
from agentcad.annotation_layout import text_bounds
from agentcad.m7_symbol_geometry import (
    SNAPSHOT_EXCLUDED_NAMES,
    SNAPSHOT_FACT_FIELDS,
    SNAPSHOT_PORT_FIELDS,
    DuplicateSymbolPortError,
    EntityKindIncompatibleError,
    EntitySymbolResolutionError,
    MissingSymbolGeometryError,
    SymbolGeometryError,
    SymbolNotRenderableError,
    UnknownSymbolScaleConstraintError,
    declared_symbol_key,
    freeze_symbol_geometry,
    require_node_symbol,
    symbol_closure_for_kinds,
    symbol_geometry_catalog_digest,
    symbol_key_field,
)
from agentcad.models import Point, SymbolDefinition, SymbolPort, TextElement
from agentcad.symbols import SymbolRegistry

TASK_BOOK = Path(__file__).resolve().parents[2] / "docs" / "m7-2-deterministic-layout.md"


def _symbol_payload(**overrides: object) -> dict:
    payload: dict = {
        "key": "unit_test_symbol",
        "name": "测试符号",
        "category": "设备",
        "width": 40.0,
        "height": 20.0,
        "ports": [
            {"id": "in", "name": "入口", "x": 0.0, "y": 10.0, "direction": "in"},
            {"id": "out", "name": "出口", "x": 40.0, "y": 10.0, "direction": "out"},
        ],
        "shapes": [{"type": "rect", "x": 0, "y": 0, "width": 40, "height": 20}],
    }
    payload.update(overrides)
    return payload


def _registry_with(tmp_path: Path, *entries: dict, name: str = "fixture.json") -> SymbolRegistry:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"symbols": list(entries)}, ensure_ascii=False), encoding="utf-8")
    return SymbolRegistry(search_paths=[path])


# ---------------------------------------------------------------------------------------
# Identity: what moves the digest, and what must not
# ---------------------------------------------------------------------------------------


def test_same_facts_produce_the_same_digest() -> None:
    first = freeze_symbol_geometry(["purifier", "gas_tank"])
    second = freeze_symbol_geometry(["gas_tank", "purifier"])
    assert first.digest == second.digest
    assert first.closure == second.closure == ("gas_tank", "purifier")


def test_closure_is_not_the_catalogue(tmp_path: Path) -> None:
    """A symbol the drawing does not use must not join its identity."""

    alone = _registry_with(tmp_path / "a", _symbol_payload(), name="alone.json")
    with_neighbour = _registry_with(
        tmp_path / "b",
        _symbol_payload(),
        _symbol_payload(key="unrelated_symbol", name="无关符号"),
        name="neighbour.json",
    )

    used = freeze_symbol_geometry(["unit_test_symbol"], registry=alone)
    same_closure_other_catalogue = freeze_symbol_geometry(
        ["unit_test_symbol"], registry=with_neighbour
    )
    assert used.digest == same_closure_other_catalogue.digest
    # The unrelated symbol is absent from the projection entirely, not merely ignored in the
    # digest: a reader of the snapshot cannot even see that it exists.
    projection = json.dumps(used.to_projection(), ensure_ascii=False)
    assert "unrelated_symbol" not in projection
    assert SymbolRegistry().exists("purifier")


def test_editing_an_unused_symbol_does_not_move_the_digest(tmp_path: Path) -> None:
    before = _registry_with(
        tmp_path / "before",
        _symbol_payload(key="used_symbol"),
        _symbol_payload(key="bystander_symbol"),
        name="before.json",
    )
    after_edit = _registry_with(
        tmp_path / "after",
        _symbol_payload(key="used_symbol"),
        _symbol_payload(key="bystander_symbol", width=999.0, height=999.0),
        name="after.json",
    )
    assert (
        freeze_symbol_geometry(["used_symbol"], registry=before).digest
        == freeze_symbol_geometry(["used_symbol"], registry=after_edit).digest
    )


def test_intrinsic_size_change_moves_the_digest(tmp_path: Path) -> None:
    before = _registry_with(tmp_path / "size_before", _symbol_payload(), name="before.json")
    after = _registry_with(
        tmp_path / "size_after", _symbol_payload(width=80.0), name="after.json"
    )
    assert (
        freeze_symbol_geometry(["unit_test_symbol"], registry=before).digest
        != freeze_symbol_geometry(["unit_test_symbol"], registry=after).digest
    )


def test_renderer_geometry_change_moves_the_digest_even_at_the_same_size(
    tmp_path: Path,
) -> None:
    """Bounds can stay identical while the drawing of the symbol changes -- so both are in."""

    before = _registry_with(tmp_path / "shape_before", _symbol_payload(), name="before.json")
    after = _registry_with(
        tmp_path / "shape_after",
        _symbol_payload(shapes=[{"type": "circle", "cx": 20, "cy": 10, "r": 9}]),
        name="after.json",
    )
    assert (
        freeze_symbol_geometry(["unit_test_symbol"], registry=before).digest
        != freeze_symbol_geometry(["unit_test_symbol"], registry=after).digest
    )


def test_port_anchor_change_moves_the_digest(tmp_path: Path) -> None:
    before = _registry_with(tmp_path / "port_before", _symbol_payload(), name="before.json")
    moved_port = _symbol_payload()
    moved_port["ports"] = [
        {"id": "in", "name": "入口", "x": 0.0, "y": 2.0, "direction": "in"},
        {"id": "out", "name": "出口", "x": 40.0, "y": 10.0, "direction": "out"},
    ]
    after = _registry_with(tmp_path / "port_after", moved_port, name="after.json")
    assert (
        freeze_symbol_geometry(["unit_test_symbol"], registry=before).digest
        != freeze_symbol_geometry(["unit_test_symbol"], registry=after).digest
    )


def test_digest_is_versioned_and_differs_from_the_layout_digest() -> None:
    snapshot = freeze_symbol_geometry(["purifier"])
    assert snapshot.digest_version == contract.SYMBOL_GEOMETRY_CATALOG_DIGEST_VERSION
    assert snapshot.digest_version != contract.LAYOUT_DIGEST_VERSION
    assert snapshot.digest == symbol_geometry_catalog_digest(snapshot.facts)


def test_empty_closure_has_a_stable_digest() -> None:
    empty = freeze_symbol_geometry([])
    assert empty.closure == ()
    assert empty.digest == freeze_symbol_geometry([]).digest


# ---------------------------------------------------------------------------------------
# Facts: ports, scale constraints, and the geometry the snapshot must never carry
# ---------------------------------------------------------------------------------------


def test_ports_are_normalized_sorted_and_queryable() -> None:
    snapshot = freeze_symbol_geometry(["gas_tank"])
    fact = snapshot.require("gas_tank")
    assert fact.intrinsic_width == 90.0 and fact.intrinsic_height == 140.0
    assert [port.port_id for port in fact.ports] == ["drain", "in", "out", "top"]
    in_port = fact.port("in")
    assert in_port is not None
    assert (in_port.normalized_x, in_port.normalized_y) == (0.0, 0.5)
    assert in_port.anchor(90.0, 140.0) == (0.0, 70.0)
    # Scaled: the anchor follows the instance, which is why the fact stores a fraction.
    assert in_port.anchor(180.0, 280.0) == (0.0, 140.0)


def test_ports_with_direction_is_ordered_and_empty_is_not_an_error() -> None:
    snapshot = freeze_symbol_geometry(["purifier", "agitator"])
    purifier = snapshot.require("purifier")
    assert [port.port_id for port in purifier.ports_with_direction("in")] == ["in"]
    assert purifier.ports_with_direction("none") == ()
    agitator = snapshot.require("agitator")
    assert agitator.ports == ()


def test_snapshot_carries_no_position_and_no_authority() -> None:
    snapshot = freeze_symbol_geometry(["purifier", "gas_tank"])
    serialized = json.dumps(snapshot.to_projection(), ensure_ascii=False)
    for forbidden in SNAPSHOT_EXCLUDED_NAMES:
        if forbidden in {"x", "y"}:
            continue
        assert f'"{forbidden}"' not in serialized, forbidden
    assert set(snapshot.to_projection()["facts"][0]) == set(SNAPSHOT_FACT_FIELDS)
    assert set(snapshot.to_projection()["facts"][0]["ports"][0]) == set(SNAPSHOT_PORT_FIELDS)
    assert snapshot.to_projection()["closure"] == ["gas_tank", "purifier"]


def test_unknown_symbol_key_is_a_hard_failure_with_a_code() -> None:
    with pytest.raises(MissingSymbolGeometryError) as raised:
        freeze_symbol_geometry(["purifier", "not_a_catalogue_symbol"])
    assert raised.value.code == "symbol_geometry_missing"
    assert raised.value.symbol_key == "not_a_catalogue_symbol"
    assert "no rule size to fall back to" in str(raised.value)


def test_missing_fact_in_a_frozen_snapshot_is_refused() -> None:
    snapshot = freeze_symbol_geometry(["purifier"])
    assert snapshot.fact("gas_tank") is None
    with pytest.raises(MissingSymbolGeometryError):
        snapshot.require("gas_tank")


def test_unknown_scale_constraint_is_refused(tmp_path: Path) -> None:
    registry = _registry_with(
        tmp_path / "scale",
        _symbol_payload(metadata={"scale_constraint": "stretch_to_fill"}),
        name="scale.json",
    )
    with pytest.raises(UnknownSymbolScaleConstraintError) as raised:
        freeze_symbol_geometry(["unit_test_symbol"], registry=registry)
    assert raised.value.code == "unknown_symbol_scale_constraint"


def test_declared_scale_constraint_is_recorded(tmp_path: Path) -> None:
    registry = _registry_with(
        tmp_path / "fixed",
        _symbol_payload(metadata={"scale_constraint": "intrinsic_size_fixed"}),
        name="fixed.json",
    )
    fact = freeze_symbol_geometry(["unit_test_symbol"], registry=registry).require(
        "unit_test_symbol"
    )
    assert fact.scale_constraint == "intrinsic_size_fixed"
    assert (
        freeze_symbol_geometry(["purifier"]).require("purifier").scale_constraint
        == contract.SYMBOL_SCALE_CONSTRAINT_DEFAULT
    )


def test_duplicate_port_id_is_refused(tmp_path: Path) -> None:
    payload = _symbol_payload()
    payload["ports"] = [
        {"id": "in", "name": "入口", "x": 0.0, "y": 10.0, "direction": "in"},
        {"id": "in", "name": "另一个入口", "x": 40.0, "y": 10.0, "direction": "out"},
    ]
    registry = _registry_with(tmp_path / "dup", payload, name="dup.json")
    with pytest.raises(DuplicateSymbolPortError):
        freeze_symbol_geometry(["unit_test_symbol"], registry=registry)


def test_non_finite_intrinsic_size_is_refused() -> None:
    """The model refuses NaN, and the snapshot may not let one in through the back door."""

    from dataclasses import dataclass

    @dataclass
    class FakeSymbol:
        key = "fake"
        width = float("nan")
        height = 20.0
        ports: tuple = ()
        shapes: tuple = ()

    class FakeCatalog:
        def exists(self, key: str) -> bool:
            return True

        def get(self, key: str) -> FakeSymbol:
            return FakeSymbol()

    with pytest.raises(SymbolGeometryError):
        freeze_symbol_geometry(["fake"], registry=FakeCatalog())


# ---------------------------------------------------------------------------------------
# Binding: the node's symbol key, read from the contract rather than guessed
# ---------------------------------------------------------------------------------------


def test_symbol_key_field_comes_from_the_contract() -> None:
    """One explicit field for both kinds: the key is not a function of the entity kind."""

    assert symbol_key_field() == contract.SYMBOL_KEY_FIELD == "symbol_key"


class _Node:
    """A stand-in for ``TopologyNode``: the binding reads kind, id and symbol_key."""

    def __init__(self, *, kind: str, symbol_key: str, engineering_id: str = "el_1") -> None:
        self.kind = kind
        self.symbol_key = symbol_key
        self.engineering_id = engineering_id


def test_the_symbol_key_field_comes_from_the_contract() -> None:
    assert symbol_key_field() == "symbol_key" == contract.SYMBOL_KEY_FIELD


def test_a_blank_symbol_key_is_refused_rather_than_inferred() -> None:
    with pytest.raises(EntitySymbolResolutionError) as raised:
        declared_symbol_key(_Node(kind="equipment", symbol_key="   "))
    assert raised.value.code == "entity_symbol_key_unresolved"
    assert "symbol_key" in str(raised.value)


def test_the_engineering_class_is_not_the_symbol_key() -> None:
    """Two facts that coincide today: one class, one graphic, and the graphic may change."""

    class Node:
        kind = "equipment"
        engineering_id = "el_1"
        equipment_class = "gas_tank"
        symbol_key = "horizontal_vessel"

    assert declared_symbol_key(Node()) == "horizontal_vessel"
    assert contract.SYMBOL_KEY_MUST_EQUAL_THE_ENGINEERING_CLASS is False


def test_closure_is_exactly_the_keys_the_nodes_name() -> None:
    nodes = (
        _Node(kind="equipment", symbol_key="purifier", engineering_id="el_1"),
        _Node(kind="equipment", symbol_key="gas_tank", engineering_id="el_2"),
        _Node(kind="equipment", symbol_key="purifier", engineering_id="el_3"),
    )
    assert symbol_closure_for_kinds(nodes) == ("gas_tank", "purifier")


def test_a_node_is_bound_to_a_frozen_fact_with_all_three_requirements() -> None:
    snapshot = freeze_symbol_geometry(["purifier", "pressure_indicator"])
    equipment = _Node(kind="equipment", symbol_key="purifier")
    instrument = _Node(kind="instrument", symbol_key="pressure_indicator", engineering_id="el_2")
    assert require_node_symbol(equipment, snapshot).symbol_key == "purifier"
    assert require_node_symbol(instrument, snapshot).symbol_key == "pressure_indicator"


def test_a_missing_symbol_is_refused_before_anything_is_placed() -> None:
    snapshot = freeze_symbol_geometry(["purifier"])
    with pytest.raises(MissingSymbolGeometryError):
        require_node_symbol(_Node(kind="equipment", symbol_key="gas_tank"), snapshot)


def test_an_instrument_may_not_borrow_an_equipment_symbol() -> None:
    snapshot = freeze_symbol_geometry(["purifier", "pressure_indicator"])
    with pytest.raises(EntityKindIncompatibleError) as raised:
        require_node_symbol(_Node(kind="instrument", symbol_key="purifier"), snapshot)
    assert raised.value.code == "entity_kind_incompatible"
    with pytest.raises(EntityKindIncompatibleError):
        require_node_symbol(_Node(kind="equipment", symbol_key="pressure_indicator"), snapshot)


def test_a_symbol_without_renderer_geometry_is_not_placeable(tmp_path: Path) -> None:
    registry = _registry_with(
        tmp_path / "noshapes", _symbol_payload(shapes=[]), name="noshapes.json"
    )
    snapshot = freeze_symbol_geometry(["unit_test_symbol"], registry=registry)
    fact = snapshot.require("unit_test_symbol")
    assert fact.renderer_supported is False
    with pytest.raises(SymbolNotRenderableError) as raised:
        require_node_symbol(_Node(kind="equipment", symbol_key="unit_test_symbol"), snapshot)
    assert raised.value.code == "symbol_not_renderable"


# ---------------------------------------------------------------------------------------
# The fixture erratum: the phase-2A payload now names geometry that exists
# ---------------------------------------------------------------------------------------


def test_the_erratum_made_the_fixture_placeable() -> None:
    """The fixture used to name a symbol and a port that do not exist; both are repaired."""

    from test_m7_diagram_adapter import fixture_a_payload

    registry = SymbolRegistry()
    payload = fixture_a_payload()
    keys = [entity["symbol_key"] for entity in payload["entities"]]
    assert keys == ["gas_tank", "purifier", "pressure_indicator", "purifier"]
    for key in keys:
        assert registry.exists(key), key
    # Every referenced port exists on the symbol the connection names it for.
    symbols = {entity["engineering_id"]: entity["symbol_key"] for entity in payload["entities"]}
    for connection in payload["connections"]:
        for role in ("source", "target"):
            port_id = connection.get(f"{role}_port_id") or ""
            if not port_id:
                continue
            symbol_key = symbols[connection[f"{role}_engineering_id"]]
            ports = {port.id for port in registry.get(symbol_key).ports}
            assert port_id in ports, (connection["engineering_id"], role, port_id, symbol_key)
    # And the historical typo stays visible as history rather than as a hidden alias.
    assert not registry.exists("pressure_transmitter")
    assert "tap" not in {port.id for port in registry.get("purifier").ports}


# ---------------------------------------------------------------------------------------
# Contract binding and the annotation text-extent finding
# ---------------------------------------------------------------------------------------


def test_contract_is_coherent_and_declares_step_3() -> None:
    assert contract.validate_contract() == []
    assert contract.LAYOUT_DIGEST_VERSION == "m7-layout-digest/2"
    assert "symbol_geometry_catalog_digest" in contract.LAYOUT_DIGEST_INPUTS
    assert contract.LAYOUT_PROJECTION_VERSION == "m7-layout-projection/1"
    assert contract.DIGEST_VERSION_CONTRACT.digest_version == contract.LAYOUT_DIGEST_VERSION
    assert (
        contract.SYMBOL_GEOMETRY_CATALOG_DIGEST_VERSION
        == "m7-symbol-geometry-catalog-digest/1"
    )


def test_declared_catalogue_sources_match_the_model() -> None:
    """The snapshot may read the catalogue, so the two field lists are pinned to each other."""

    assert set(contract.SYMBOL_CATALOG_SOURCE_FIELDS) == set(SymbolDefinition.model_fields)
    assert set(contract.SYMBOL_CATALOG_SOURCE_PORT_FIELDS) == set(SymbolPort.model_fields)


def test_declared_projection_fields_match_the_runtime() -> None:
    assert contract.SYMBOL_GEOMETRY_FACT_FIELDS == SNAPSHOT_FACT_FIELDS
    assert contract.SYMBOL_GEOMETRY_PORT_FIELDS == SNAPSHOT_PORT_FIELDS


def test_annotation_text_extent_is_pure_code() -> None:
    """Step 3 must not import the machine into the layout identity.

    The rule is recomputed from the function itself rather than restated, so a change to the
    formula fails here instead of silently becoming a new canonical layout.
    """

    factor = contract.ANNOTATION_TEXT_EXTENT_CHARACTER_FACTOR
    for text, font_size in (("V-101", 12.0), ("氩气储罐出口", 10.0), ("", 14.0)):
        element = TextElement(
            id="t1",
            text=text,
            position=Point(x=0.0, y=0.0),
            font_size=font_size,
        )
        bounds = text_bounds(element)
        expected_width = max(font_size, len(text) * font_size * factor)
        assert bounds.x2 - bounds.x1 == pytest.approx(expected_width)


def test_annotation_layout_never_measures_fonts() -> None:
    source = Path(text_bounds.__module__.replace(".", "/") + ".py")
    module_path = Path(__file__).resolve().parents[1] / source
    text = module_path.read_text(encoding="utf-8")
    for forbidden in ("measureText", "getContext", "document.fonts", "font-family"):
        assert forbidden not in text, forbidden
    assert not contract.ANNOTATION_PLACEMENT_READS_BROWSER_FONT_METRICS
    assert not contract.ANNOTATION_PLACEMENT_READS_OS_FONTS
    assert not contract.ANNOTATION_PLACEMENT_USES_CANVAS_MEASURE_TEXT


def test_task_book_declares_the_step_3_obligations() -> None:
    text = TASK_BOOK.read_text(encoding="utf-8")
    for token in (
        "m7-symbol-geometry-catalog-digest/1",
        "m7-layout-digest/2",
        "symbol_geometry_catalog_digest",
        "closure",
    ):
        assert token in text, token
