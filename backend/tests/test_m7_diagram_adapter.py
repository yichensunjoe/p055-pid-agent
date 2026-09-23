"""M7-2 phase 2A: the model has lost geometry authority, and this is where that is proved.

The phase's claim is deliberately narrow: a geometry-free specification can be adapted into
deterministic semantic topology input, untouched in meaning, and any specification that
carries geometry is refused before anything else happens. Whether a plant-wide drawing comes
out well is *not* answered here -- that is phase 2B's problem, and keeping the two apart is
what makes a bad drawing attributable to the adapter or to the engine instead of to "the
pipeline".

Fixture A is the positive half; fixture C is the negative half, and the negative half has a
falsifiable part that matters more than the rejection itself: the coordinates must be
*refused*, not silently stripped. A stripped violation looks exactly like a success.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcad import m7_layout_contract as contract
from agentcad.m7_diagram_adapter import (
    adapt,
    spec_semantic_digest,
    topology_digest,
    topology_semantic_digest,
)
from agentcad.m7_diagram_spec import (
    DiagramSpec,
    DiagramSpecGeometryError,
    load_diagram_spec,
    reject_geometry,
)

TASK_BOOK = Path(__file__).resolve().parents[2] / "docs" / "m7-2-deterministic-layout.md"


def fixture_a_payload() -> dict:
    """A small, semantically complete drawing with no geometry anywhere."""

    return {
        "label": "供气与覆盖气小图",
        "systems": [
            {"system_id": "S_supply", "name": "供气", "order": 0},
            {"system_id": "S_cover", "name": "覆盖气", "order": 1},
        ],
        "entities": [
            {
                "engineering_id": "el_ar_tank",
                "kind": "equipment",
                "system_id": "S_supply",
                "tag": "V-101",
                "name": "氩气储罐",
                "equipment_class": "gas_tank",
            },
            {
                "engineering_id": "el_purifier",
                "kind": "equipment",
                "system_id": "S_supply",
                "tag": "X-201",
                "name": "纯化器",
                "equipment_class": "purifier",
            },
            {
                "engineering_id": "el_pt_101",
                "kind": "instrument",
                "system_id": "S_supply",
                "tag": "PT-101",
                "instrument_type": "pressure_transmitter",
                "measurement": "pressure",
            },
            {
                "engineering_id": "el_recycle",
                "kind": "equipment",
                "system_id": "S_cover",
                "tag": "X-301",
                "name": "覆盖气回用净化",
                "equipment_class": "purifier",
            },
        ],
        "connections": [
            {
                "engineering_id": "cn_1",
                "source_engineering_id": "el_ar_tank",
                "target_engineering_id": "el_purifier",
                "source_port_id": "out",
                "target_port_id": "in",
                "medium": "argon",
                "tag": "AR-1001",
            },
            {
                "engineering_id": "cn_2",
                "source_engineering_id": "el_purifier",
                "target_engineering_id": "el_recycle",
                "target_port_id": "in",
                "medium": "argon",
            },
            {
                "engineering_id": "cn_3",
                "source_engineering_id": "el_purifier",
                "target_engineering_id": "el_pt_101",
                "source_port_id": "tap",
                "target_port_id": "process",
                "medium": "argon",
            },
        ],
        "required_loops": [
            {
                "loop_id": "loop_cover_gas",
                "engineering_ids": ["el_ar_tank", "el_purifier", "el_recycle"],
                "note": "覆盖气必须闭环",
            }
        ],
        "layout_intent": {
            "orientation": "landscape",
            "preferred_aspect_class": "extra_wide",
            "primary_flow_direction": "left_to_right",
            "system_order": ["S_supply", "S_cover"],
            "grouping": "grouped_by_system",
            "density": "compact",
        },
    }


# --------------------------------------------------------------------------------------
# Fixture A: the specification adapts, completely and without geometry
# --------------------------------------------------------------------------------------


def test_fixture_a_adapts_every_declared_entity_exactly_once() -> None:
    topology = adapt(fixture_a_payload())

    assert [node.engineering_id for node in topology.nodes] == [
        "el_ar_tank",
        "el_purifier",
        "el_pt_101",
        "el_recycle",
    ]
    assert len({node.engineering_id for node in topology.nodes}) == len(topology.nodes)
    assert [system.system_id for system in topology.systems] == ["S_supply", "S_cover"]
    # The instrument keeps its instrument-ness rather than arriving as an anonymous node.
    instrument = next(node for node in topology.nodes if node.engineering_id == "el_pt_101")
    assert instrument.kind == "instrument"
    assert instrument.measurement == "pressure"


def test_fixture_a_preserves_every_connection_and_the_required_loop() -> None:
    payload = fixture_a_payload()
    topology = adapt(payload)

    assert {edge.engineering_id for edge in topology.edges} == {
        connection["engineering_id"] for connection in payload["connections"]
    }
    assert len(topology.edges) == len(payload["connections"])
    loop = topology.required_loops[0]
    assert loop.loop_id == "loop_cover_gas"
    assert loop.engineering_ids == ("el_ar_tank", "el_purifier", "el_recycle")
    # Ports arrive as declared, not invented.
    first = next(edge for edge in topology.edges if edge.engineering_id == "cn_1")
    assert (first.source_port_id, first.target_port_id) == ("out", "in")


def test_an_instrument_arrives_as_a_relation_and_not_only_as_a_node() -> None:
    """`instrument -> instrument nodes and relations`: the edges carry the relation, the node
    carries the ports those edges attach to. An instrument with no relation is a node and
    nothing more, which is the honest reading of a one-way declaration."""

    topology = adapt(fixture_a_payload())
    instrument = next(node for node in topology.nodes if node.engineering_id == "el_pt_101")

    assert instrument.ports == (("in", "process"),)
    attached = [
        edge
        for edge in topology.edges
        if "el_pt_101" in (edge.source_engineering_id, edge.target_engineering_id)
    ]
    assert [edge.engineering_id for edge in attached] == ["cn_3"]
    # Derived, not declared: the specification has no port field on an entity at all.
    assert "ports" not in {field for field in DiagramSpec.model_fields}


def test_fixture_a_translates_the_discrete_intent() -> None:
    topology = adapt(fixture_a_payload())

    assert topology.intent.preferred_aspect_class == "extra_wide"
    assert topology.intent.primary_flow_direction == "left_to_right"
    assert topology.intent.grouping == "grouped_by_system"
    assert topology.intent.system_order == ("S_supply", "S_cover")
    # Intent is classes and orderings: no number may appear in it.
    assert not any(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in vars(topology.intent).values()
    )


def test_fixture_a_output_contains_no_absolute_geometry() -> None:
    """The positive half of the phase's claim, asserted on the serialized output."""

    topology = adapt(fixture_a_payload())
    rendered = json.dumps(list(topology.projection), ensure_ascii=False)

    assert contract.ADAPTER_OUTPUT_CONTAINS_ABSOLUTE_GEOMETRY is False
    assert contract.ADAPTER_OUTPUT_CONTAINS_WAYPOINTS is False
    assert contract.ADAPTER_OUTPUT_CONTAINS_CANVAS is False
    for field in contract.FORBIDDEN_MODEL_GEOMETRY_FIELDS:
        assert f'"{field}"' not in rendered, field
    declared = set(contract.ADAPTER_TOPOLOGY_PROJECTION_FIELDS)
    for row in topology.projection:
        assert set(row) <= declared, set(row) - declared


def test_the_declared_adapter_projection_is_exactly_what_the_rows_carry() -> None:
    """Both directions: an undeclared key and an unused declaration are each a defect."""

    topology = adapt(fixture_a_payload())
    declared = set(contract.ADAPTER_TOPOLOGY_PROJECTION_FIELDS)
    carried: set[str] = set()
    for row in topology.projection:
        carried |= set(row)
    assert carried <= declared, carried - declared
    assert declared <= carried, declared - carried


def test_the_adapter_digest_version_pins_its_field_set() -> None:
    assert contract.ADAPTER_TOPOLOGY_DIGEST_VERSION_FIELD_SET == (
        contract.ADAPTER_TOPOLOGY_PROJECTION_FIELDS
    )


# --------------------------------------------------------------------------------------
# Fixture C: geometry is refused, by name, and never quietly stripped
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"entities": [{"engineering_id": "e1", "kind": "equipment", "system_id": "s", "x": 10}]},
        {
            "entities": [
                {
                    "engineering_id": "e1",
                    "kind": "equipment",
                    "system_id": "s",
                    "position": {"x": 1, "y": 2},
                }
            ]
        },
        {
            "entities": [
                {"engineering_id": "e1", "kind": "equipment", "system_id": "s", "width": 40}
            ]
        },
        {"connections": [{"engineering_id": "c1", "waypoints": [{"x": 1, "y": 2}]}]},
        {"canvas_width": 58273.8, "canvas_height": 4772.2},
        {
            "entities": [
                {"engineering_id": "e1", "kind": "equipment", "system_id": "s", "port_y": 12}
            ]
        },
        {"layout_intent": {"anchor": {"relative_to": "el_ar_tank"}}},
        {"notes": "place this left_of the reactor"},
        {
            "entities": [
                {"engineering_id": "e1", "kind": "equipment", "system_id": "s", "radius": 5}
            ]
        },
        {
            "entities": [
                {"engineering_id": "e1", "kind": "equipment", "system_id": "s", "points": []}
            ]
        },
    ],
)
def test_fixture_c_refuses_geometry_and_names_the_field(payload: dict) -> None:
    with pytest.raises(DiagramSpecGeometryError) as failure:
        load_diagram_spec(payload)

    assert failure.value.field_path.startswith("$")
    assert "geometry" in str(failure.value)


def test_fixture_c_does_not_silently_strip_geometry() -> None:
    """The half of fixture C that is easy to get wrong: rejection, not sanitation.

    A specification carrying coordinates must not produce a topology. If it did, a response
    that violates the contract would read as a success -- which is the failure mode this
    milestone exists to remove, not to reproduce in a new place.
    """

    payload = fixture_a_payload()
    payload["entities"][0]["x"] = 120
    payload["entities"][0]["y"] = 340

    assert contract.ADAPTER_MAY_SILENTLY_STRIP_GEOMETRY is False
    with pytest.raises(DiagramSpecGeometryError) as failure:
        adapt(payload)
    assert failure.value.field_path == "$.entities[0].x"

    # And the same for a nested anchor predicate, which is geometry spelled as a relation.
    anchored = fixture_a_payload()
    anchored["layout_intent"]["anchor"] = {"relative_to": "el_ar_tank"}
    with pytest.raises(DiagramSpecGeometryError):
        adapt(anchored)


def test_the_geometry_scan_walks_lists_and_nesting() -> None:
    with pytest.raises(DiagramSpecGeometryError) as failure:
        reject_geometry({"a": {"b": [{"c": {"y": 1}}]}})
    assert failure.value.field_path == "$.a.b[0].c.y"


def test_a_clean_payload_passes_the_scan() -> None:
    reject_geometry(fixture_a_payload())


# --------------------------------------------------------------------------------------
# Determinism and losslessness
# --------------------------------------------------------------------------------------


def test_the_same_specification_produces_the_same_topology_digest() -> None:
    first = topology_digest(adapt(fixture_a_payload()))
    second = topology_digest(adapt(fixture_a_payload()))

    assert first == second
    assert len(first) == 64


def test_listing_order_does_not_change_the_digest() -> None:
    """Canonical means canonical: the caller's ordering is not part of the identity."""

    shuffled = fixture_a_payload()
    shuffled["entities"] = list(reversed(shuffled["entities"]))
    shuffled["connections"] = list(reversed(shuffled["connections"]))
    shuffled["systems"] = list(reversed(shuffled["systems"]))

    assert topology_digest(adapt(shuffled)) == topology_digest(adapt(fixture_a_payload()))


def test_a_changed_meaning_changes_the_digest() -> None:
    """The other direction: a digest that never moves would be decoration."""

    changed = fixture_a_payload()
    changed["connections"] = changed["connections"][:-1]

    assert topology_digest(adapt(changed)) != topology_digest(adapt(fixture_a_payload()))


def test_the_adapter_does_not_change_the_semantic_digest() -> None:
    """Losslessness, as an equality rather than a promise."""

    spec = load_diagram_spec(fixture_a_payload())
    topology = adapt(spec)

    assert contract.ADAPTER_SEMANTIC_DIGEST_BEFORE_EQUALS_AFTER is True
    assert spec_semantic_digest(spec) == topology_semantic_digest(topology)


def test_the_adapter_digest_is_not_the_layout_digest() -> None:
    """A later difference must be attributable to the adapter or to the engine, not to both."""

    assert contract.ADAPTER_TOPOLOGY_DIGEST_IS_NOT_THE_LAYOUT_DIGEST is True
    assert contract.ADAPTER_TOPOLOGY_DIGEST_VERSION not in contract.LAYOUT_DIGEST_INPUTS
    topology = adapt(fixture_a_payload())
    assert topology.digest_version == contract.ADAPTER_TOPOLOGY_DIGEST_VERSION
    assert topology.digest == topology_digest(topology)


def test_the_adapter_projection_is_sorted_on_the_declared_composite_key() -> None:
    topology = adapt(fixture_a_payload())
    key = tuple((row["kind"], row["engineering_id"]) for row in topology.projection)
    assert list(key) == sorted(key)
    assert contract.ADAPTER_TOPOLOGY_DIGEST_SORT_KEY == ("kind", "engineering_id")
    # Unique, so the ordering is a total order rather than a stable accident.
    assert len(set(key)) == len(key)


# --------------------------------------------------------------------------------------
# The phase boundary, read off the running code
# --------------------------------------------------------------------------------------


def test_the_adapter_module_emits_no_geometry_field_in_its_projection() -> None:
    projection = set(contract.ADAPTER_TOPOLOGY_PROJECTION_FIELDS)
    assert not projection & set(contract.FORBIDDEN_MODEL_GEOMETRY_FIELDS)
    for dimension in contract.LAYOUT_INTENT_DIMENSIONS:
        assert dimension.name in projection


def test_only_the_declared_phase_2a_modules_read_the_contract() -> None:
    """The phase-1 rule ("no application module imports the contract") stays a rule."""

    app_sources = list((Path(__file__).resolve().parents[1] / "agentcad").glob("*.py"))
    importers = sorted(
        path.name
        for path in app_sources
        if path.name != "m7_layout_contract.py"
        and "m7_layout_contract" in path.read_text(encoding="utf-8")
    )
    assert importers == sorted(contract.PHASE_2A_MAY_IMPORT_THE_CONTRACT), importers


def test_the_adapter_is_not_placed_on_any_request_path() -> None:
    """Phase 2A is a module with tests, not a surface: no route or tool may reference it."""

    from fastapi.testclient import TestClient

    from agentcad.main import create_app

    app = create_app()
    with TestClient(app):
        paths = set(app.openapi()["paths"])
    assert not [path for path in paths if "diagram" in path or "topology" in path.lower()]
    mcp_source = (Path(__file__).resolve().parents[1] / "agentcad" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    for token in ("diagram_spec", "semantic_topology", "m7_diagram"):
        assert token not in mcp_source


def test_the_task_book_names_the_phase_2a_boundary() -> None:
    task_book = TASK_BOOK.read_text(encoding="utf-8")
    names = (
        *contract.PHASE_2A_MAY_BUILD,
        *contract.PHASE_2A_FORBIDDEN,
        *[source for source, _ in contract.ADAPTER_TRANSLATES],
        *[target for _, target in contract.ADAPTER_TRANSLATES],
        *contract.ADAPTER_MUST_PRESERVE,
        *contract.PHASE_2A_MAY_IMPORT_THE_CONTRACT,
        contract.ADAPTER_TOPOLOGY_DIGEST_VERSION,
        *contract.ADAPTER_TOPOLOGY_DIGEST_SORT_KEY,
        "ADAPTER_OUTPUT_CONTAINS_ABSOLUTE_GEOMETRY",
        "ADAPTER_OUTPUT_CONTAINS_WAYPOINTS",
        "ADAPTER_OUTPUT_CONTAINS_CANVAS",
        "ADAPTER_REJECTS_MODEL_GEOMETRY_BEFORE_VALIDATION",
        "ADAPTER_MAY_SILENTLY_STRIP_GEOMETRY",
        "ADAPTER_IS_DETERMINISTIC",
        "ADAPTER_SEMANTIC_DIGEST_BEFORE_EQUALS_AFTER",
        "ADAPTER_TOPOLOGY_DIGEST_IS_NOT_THE_LAYOUT_DIGEST",
        "VERSION_CHECK_DETECTS_DRIFT_BETWEEN_LIVE_AND_FROZEN_DEFINITION",
        "VERSION_CHECK_CANNOT_PREVENT_A_DELIBERATE_DOUBLE_EDIT",
        "DiagramSpec",
        "DiagramSpecAdapter",
        "load_diagram_spec",
        "reject_geometry",
        "DiagramSpecGeometryError",
    )
    missing = [name for name in names if name not in task_book]
    assert not missing, missing
