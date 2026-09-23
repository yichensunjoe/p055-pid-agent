"""M7-2 phase 2A: the adapter between a diagram specification and the layout engine's door.

The chain this module is allowed to complete is:

``DiagramSpec`` → validate → ``DiagramSpecAdapter`` → deterministic semantic topology input.
→ STOP

Nothing here places anything. There is no ``x``, no ``y``, no width, no waypoint, no canvas:
those arrive in phase 2B, produced by ``AutoLayoutEngine``. That boundary is the milestone --
"the model has lost geometry authority" should be provable on its own, before anyone argues
about whether a plant-wide drawing is laid out *well*.

Three properties are built in rather than checked afterwards:

* **Losslessness.** ``semantic_digest`` of the specification equals the digest reconstructed
  from the adapted topology, so the adapter cannot quietly drop a connection or reclassify a
  device on the way to the engine.
* **Determinism.** The topology projection is sorted on ``(kind, engineering_id)`` and the
  digest is computed over that canonical form, so the same specification always produces the
  same digest even if the caller listed its systems or connections in another order.
* **A digest of its own.** ``adapter_topology_digest`` is deliberately *not* the layout
  digest: when phase 2B produces a drawing that differs between two runs, a separate adapter
  digest is what tells a reader whether the difference came from the adapter or the engine.

The adapter reads the forbidden-geometry list from the phase-1 contract, so the vocabulary
that refuses coordinates at the door and the vocabulary that declares them illegitimate are
the same one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .m7_diagram_spec import SPEC_SCHEMA, DiagramSpec, EntityKind, load_diagram_spec
from .m7_layout_contract import (
    ADAPTER_OUTPUT_CONTAINS_ABSOLUTE_GEOMETRY,
    ADAPTER_OUTPUT_CONTAINS_CANVAS,
    ADAPTER_OUTPUT_CONTAINS_WAYPOINTS,
    ADAPTER_TOPOLOGY_DIGEST_SORT_KEY,
    ADAPTER_TOPOLOGY_DIGEST_VERSION,
)


@dataclass(frozen=True)
class TopologySystem:
    system_id: str
    name: str
    order: int


@dataclass(frozen=True)
class TopologyNode:
    """One placed-later entity. ``kind`` distinguishes equipment from instrument."""

    kind: EntityKind
    engineering_id: str
    system_id: str
    tag: str
    name: str
    equipment_class: str
    instrument_type: str
    measurement: str
    ports: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TopologyEdge:
    engineering_id: str
    source_engineering_id: str
    target_engineering_id: str
    source_port_id: str
    target_port_id: str
    medium: str
    tag: str


@dataclass(frozen=True)
class TopologyLoop:
    loop_id: str
    engineering_ids: tuple[str, ...]


@dataclass(frozen=True)
class TopologyIntent:
    """The discrete intent, carried through as classes and orderings."""

    orientation: str
    preferred_aspect_class: str
    primary_flow_direction: str
    grouping: str
    density: str
    system_order: tuple[str, ...]


@dataclass(frozen=True)
class SemanticTopology:
    """What the layout engine will consume in phase 2B. Contains no geometry."""

    systems: tuple[TopologySystem, ...]
    nodes: tuple[TopologyNode, ...]
    edges: tuple[TopologyEdge, ...]
    required_loops: tuple[TopologyLoop, ...]
    intent: TopologyIntent
    digest_version: str = ADAPTER_TOPOLOGY_DIGEST_VERSION
    projection: tuple[dict[str, Any], ...] = field(default=())

    @property
    def digest(self) -> str:
        return topology_digest(self)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def adapt(spec: DiagramSpec | Any) -> SemanticTopology:
    """Adapt a specification into semantic topology input.

    Accepts either an already-validated :class:`DiagramSpec` or the raw payload, in which case
    geometry is refused first -- exactly as :func:`load_diagram_spec` does -- so no caller can
    bypass the check by adapting directly.
    """

    if not isinstance(spec, DiagramSpec):
        spec = load_diagram_spec(spec)

    systems = tuple(
        TopologySystem(system_id=system.system_id, name=system.name, order=system.order)
        for system in sorted(spec.systems, key=lambda item: (item.order, item.system_id))
    )
    ports_by_entity: dict[str, list[tuple[str, str]]] = {}
    for connection in spec.connections:
        if connection.source_port_id:
            ports_by_entity.setdefault(connection.source_engineering_id, []).append(
                ("out", connection.source_port_id)
            )
        if connection.target_port_id:
            ports_by_entity.setdefault(connection.target_engineering_id, []).append(
                ("in", connection.target_port_id)
            )

    nodes = tuple(
        TopologyNode(
            kind=entity.kind,
            engineering_id=entity.engineering_id,
            system_id=entity.system_id,
            tag=entity.tag,
            name=entity.name,
            equipment_class=entity.equipment_class,
            instrument_type=entity.instrument_type,
            measurement=entity.measurement,
            ports=tuple(sorted(set(ports_by_entity.get(entity.engineering_id, [])))),
        )
        for entity in spec.entities
    )
    edges = tuple(
        TopologyEdge(
            engineering_id=connection.engineering_id,
            source_engineering_id=connection.source_engineering_id,
            target_engineering_id=connection.target_engineering_id,
            source_port_id=connection.source_port_id,
            target_port_id=connection.target_port_id,
            medium=connection.medium,
            tag=connection.tag,
        )
        for connection in spec.connections
    )
    loops = tuple(
        TopologyLoop(loop_id=loop.loop_id, engineering_ids=tuple(loop.engineering_ids))
        for loop in spec.required_loops
    )
    intent = TopologyIntent(
        orientation=spec.layout_intent.orientation or "landscape",
        preferred_aspect_class=spec.layout_intent.preferred_aspect_class or "standard",
        primary_flow_direction=spec.layout_intent.primary_flow_direction or "left_to_right",
        grouping=spec.layout_intent.grouping or "grouped_by_system",
        density=spec.layout_intent.density or "comfortable",
        system_order=tuple(spec.layout_intent.system_order),
    )
    projection = topology_projection(systems, nodes, edges, loops, intent)
    return SemanticTopology(
        systems=systems,
        nodes=nodes,
        edges=edges,
        required_loops=loops,
        intent=intent,
        projection=projection,
    )


def topology_projection(
    systems: tuple[TopologySystem, ...],
    nodes: tuple[TopologyNode, ...],
    edges: tuple[TopologyEdge, ...],
    loops: tuple[TopologyLoop, ...],
    intent: TopologyIntent,
) -> tuple[dict[str, Any], ...]:
    """The canonical rows the adapter digest is computed over.

    Rows are sorted on ``(kind, engineering_id)`` -- the same composite key the layout
    projection uses -- and every geometry-free semantic field is in the row, so equality of
    two digests means equality of meaning.
    """

    rows: list[dict[str, Any]] = []
    for node in nodes:
        rows.append(
            {
                "kind": node.kind,
                "engineering_id": node.engineering_id,
                "system_id": node.system_id,
                "tag": node.tag,
                "name": node.name,
                "equipment_class": node.equipment_class,
                "instrument_type": node.instrument_type,
                "measurement": node.measurement,
                "ports": [list(port) for port in node.ports],
            }
        )
    for edge in edges:
        rows.append(
            {
                "kind": "connection",
                "engineering_id": edge.engineering_id,
                "source_engineering_id": edge.source_engineering_id,
                "target_engineering_id": edge.target_engineering_id,
                "source_port_id": edge.source_port_id,
                "target_port_id": edge.target_port_id,
                "medium": edge.medium,
                "tag": edge.tag,
            }
        )
    for loop in loops:
        rows.append(
            {
                "kind": "required_loop",
                "engineering_id": loop.loop_id,
                "engineering_ids": list(loop.engineering_ids),
            }
        )
    for system in systems:
        rows.append(
            {
                "kind": "system",
                "engineering_id": system.system_id,
                "name": system.name,
                "system_order": system.order,
            }
        )
    rows.append(
        {
            "kind": "layout_intent",
            "engineering_id": "layout_intent",
            "orientation": intent.orientation,
            "preferred_aspect_class": intent.preferred_aspect_class,
            "primary_flow_direction": intent.primary_flow_direction,
            "grouping": intent.grouping,
            "density": intent.density,
            "system_order": list(intent.system_order),
        }
    )
    key = lambda row: tuple(str(row[part]) for part in ADAPTER_TOPOLOGY_DIGEST_SORT_KEY)  # noqa: E731
    return tuple(sorted(rows, key=key))


def topology_digest(topology: SemanticTopology) -> str:
    """The adapter's own digest: same specification, same bytes, no engine involved."""

    return _digest(
        {
            "adapter_topology_digest_version": topology.digest_version,
            "adapter_topology_projection": list(topology.projection),
        }
    )


def spec_semantic_digest(spec: DiagramSpec) -> str:
    """The specification's own semantic digest, computed without any placement."""

    return _digest(
        {
            "spec_schema": spec.schema_version,
            "systems": [
                {"system_id": s.system_id, "name": s.name, "order": s.order}
                for s in sorted(spec.systems, key=lambda item: item.system_id)
            ],
            "entities": [
                {
                    "engineering_id": entity.engineering_id,
                    "kind": entity.kind,
                    "system_id": entity.system_id,
                    "tag": entity.tag,
                    "name": entity.name,
                    "equipment_class": entity.equipment_class,
                    "instrument_type": entity.instrument_type,
                    "measurement": entity.measurement,
                }
                for entity in sorted(spec.entities, key=lambda item: item.engineering_id)
            ],
            "connections": [
                {
                    "engineering_id": connection.engineering_id,
                    "source_engineering_id": connection.source_engineering_id,
                    "target_engineering_id": connection.target_engineering_id,
                    "source_port_id": connection.source_port_id,
                    "target_port_id": connection.target_port_id,
                    "medium": connection.medium,
                    "tag": connection.tag,
                }
                for connection in sorted(spec.connections, key=lambda item: item.engineering_id)
            ],
            "required_loops": [
                {"loop_id": loop.loop_id, "engineering_ids": list(loop.engineering_ids)}
                for loop in sorted(spec.required_loops, key=lambda item: item.loop_id)
            ],
            "layout_intent": spec.layout_intent.model_dump(mode="json"),
        }
    )


def topology_semantic_digest(topology: SemanticTopology) -> str:
    """The same semantic digest, rebuilt from the adapted topology.

    Equal to :func:`spec_semantic_digest` for every specification: that equality *is* the
    losslessness claim, and it is a test rather than a comment.
    """

    entities = [
        {
            "engineering_id": node.engineering_id,
            "kind": node.kind,
            "system_id": node.system_id,
            "tag": node.tag,
            "name": node.name,
            "equipment_class": node.equipment_class,
            "instrument_type": node.instrument_type,
            "measurement": node.measurement,
        }
        for node in sorted(topology.nodes, key=lambda item: item.engineering_id)
    ]
    return _digest(
        {
            "spec_schema": SPEC_SCHEMA,
            "systems": [
                {"system_id": s.system_id, "name": s.name, "order": s.order}
                for s in sorted(topology.systems, key=lambda item: item.system_id)
            ],
            "entities": entities,
            "connections": [
                {
                    "engineering_id": edge.engineering_id,
                    "source_engineering_id": edge.source_engineering_id,
                    "target_engineering_id": edge.target_engineering_id,
                    "source_port_id": edge.source_port_id,
                    "target_port_id": edge.target_port_id,
                    "medium": edge.medium,
                    "tag": edge.tag,
                }
                for edge in sorted(topology.edges, key=lambda item: item.engineering_id)
            ],
            "required_loops": [
                {"loop_id": loop.loop_id, "engineering_ids": list(loop.engineering_ids)}
                for loop in sorted(topology.required_loops, key=lambda item: item.loop_id)
            ],
            "layout_intent": {
                "orientation": topology.intent.orientation,
                "preferred_aspect_class": topology.intent.preferred_aspect_class,
                "primary_flow_direction": topology.intent.primary_flow_direction,
                "system_order": list(topology.intent.system_order),
                "grouping": topology.intent.grouping,
                "density": topology.intent.density,
            },
        }
    )


#: The three prohibitions, restated where the code can read them back in a test.
CONTAINS_ABSOLUTE_GEOMETRY = ADAPTER_OUTPUT_CONTAINS_ABSOLUTE_GEOMETRY
CONTAINS_WAYPOINTS = ADAPTER_OUTPUT_CONTAINS_WAYPOINTS
CONTAINS_CANVAS = ADAPTER_OUTPUT_CONTAINS_CANVAS
