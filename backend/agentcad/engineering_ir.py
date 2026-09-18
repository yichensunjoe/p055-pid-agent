"""Derived engineering IR: the semantic graph behind a P&ID document.

Charter reference: §6 (semantic first), §8 (engineering objects, not pixels), §16
(M2 engineering semantic graph). The drawing document stores *geometry*; this module
derives the *engineering objects* an engineer actually reasons about:

    equipment · valve · instrument · signal · line (pipeline) · junction
    off-page connector · annotation · graphic

Design rules
------------
* **Derived, never authoritative.** The graph is a pure function of
  ``(document, symbol registry)``. Nothing here writes the document, so it cannot
  become a second source of truth. ``document_content_hash`` makes that function
  checkable, which is what lets the project index detect staleness.
* **Identity is not the tag.** Every object carries ``engineering_id``, an
  immutable surrogate derived from the *declared* stable id in the drawing when one
  exists (``line_id`` / ``signal_id`` / ``connection_id`` / ``engineering_id`` in
  element properties, ``identity_basis="declared"``) and otherwise from the drawing
  element handle (``identity_basis="element"``). A tag is a *mutable engineering
  attribute*: renaming ``P-101`` to ``P-201`` changes ``tag``/``tag_key`` and leaves
  ``engineering_id`` untouched. Tags still exist as ``tag_key`` (``equipment:p-101``,
  duplicate tags disambiguated deterministically with ``#n``) so humans and existing
  integrations can keep addressing objects by tag.
* **Signals are engineering objects, not connector annotations.** A connector that
  carries an instrument signal becomes a first-class ``signal`` object with its own
  stable id, type, source, target, associated instruments and provenance. Signal
  wiring is kept out of the process topology: the two edge classes are separate, and
  connectivity components are computed over process edges only, so a signal can never
  be mistaken for a process line.
* **Off-page connectors declare a stable connection identity.** Each OPC object
  exposes ``off_page_connection_id``; the project index resolves the reciprocal end
  across drawings and derives a *symmetric, tag-free* connection id from the two
  stable object ids, using tags only as a cross-check.
* **Pixels never become engineering truth.** Coordinates, styles and SVG output are
  intentionally absent from the graph; geometry is only used for connectivity
  (ports, endpoints) and length.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict, deque
from math import hypot
from typing import Any, Literal

from pydantic import Field

from .flow_topology import is_opc_symbol, is_valve_symbol, normalize_flow_medium, opc_direction
from .models import Document, Element, StrictModel
from .symbols import SymbolRegistry

IR_SCHEMA = "pid-agent.engineering-graph"
IR_VERSION = 2
IR_BUILDER_VERSION = 2

ObjectKind = Literal[
    "equipment",
    "valve",
    "instrument",
    "signal",
    "line",
    "junction",
    "off_page_connector",
    "annotation",
    "graphic",
]
#: How the immutable ``engineering_id`` was obtained.
IdentityBasis = Literal["declared", "element"]
EdgeClass = Literal["process", "signal"]
SignalType = Literal["analog", "digital", "electrical", "pneumatic", "impulse", "unknown"]
FindingSeverity = Literal["info", "warning", "error"]
TraceDirection = Literal["upstream", "downstream", "both"]

#: Object kinds that participate in process topology (signals/annotations/graphics do not).
TOPO_KINDS: frozenset[str] = frozenset(
    {"equipment", "valve", "instrument", "line", "junction", "off_page_connector"}
)

PROCESS_KINDS = TOPO_KINDS

_ID_PREFIX: dict[str, str] = {
    "equipment": "eq",
    "valve": "vl",
    "instrument": "inst",
    "signal": "sg",
    "line": "ln",
    "junction": "jn",
    "off_page_connector": "opc",
    "annotation": "an",
    "graphic": "gr",
}

_TAG_PREFIX: dict[str, str] = {
    "equipment": "equipment",
    "valve": "valve",
    "instrument": "instrument",
    "signal": "signal",
    "line": "line",
    "junction": "junction",
    "off_page_connector": "opc",
    "annotation": "annotation",
    "graphic": "graphic",
}

#: Element properties that a drawing may use to declare an authoritative stable id.
_DECLARED_ID_KEYS: dict[str, tuple[str, ...]] = {
    "equipment": ("engineering_id", "equipment_id", "asset_id"),
    "valve": ("engineering_id", "valve_id", "asset_id"),
    "instrument": ("engineering_id", "instrument_id", "asset_id"),
    "signal": ("signal_id", "engineering_id"),
    "line": ("line_id", "engineering_id"),
    "off_page_connector": ("connection_id", "engineering_id"),
    "junction": ("engineering_id",),
    "annotation": ("engineering_id",),
    "graphic": ("engineering_id",),
}

#: Medium families that mean "this connector is instrument wiring, not process fluid".
_SIGNAL_FAMILY_TOKENS: tuple[tuple[SignalType, tuple[str, ...]], ...] = (
    ("electrical", ("electric", "electrical", "elec", "电气", "电")),
    ("pneumatic", ("pneumatic", "气动")),
    ("impulse", ("impulse", "脉冲")),
    ("digital", ("data", "communication", "bus", "digital", "通讯", "通信", "数字")),
)
_SIGNAL_GENERIC_TOKENS: tuple[str, ...] = (
    "signal",
    "信号",
    "instrument",
    "instrumentation",
    "仪表",
)
#: Utility fluids whose names contain signal-ish words but which stay process lines.
_PROCESS_UTILITY_TOKENS: tuple[str, ...] = ("air", "风", "蒸汽", "steam", "气")


class SignalDetail(StrictModel):
    """The engineering content of a first-class signal object."""

    signal_id: str
    connector_id: str
    signal_type: SignalType
    medium: str = ""
    medium_class: str = ""
    source_engineering_id: str = ""
    target_engineering_id: str = ""
    instrument_engineering_ids: list[str] = Field(default_factory=list)
    equipment_engineering_ids: list[str] = Field(default_factory=list)
    classification: Literal["declared_medium", "instrument_to_instrument"] = "declared_medium"
    #: Where the classification came from, so a reviewer can audit the rule that fired.
    provenance: dict[str, Any] = Field(default_factory=dict)


class EngineeringObject(StrictModel):
    """One engineering entity derived from one or more drawing elements.

    ``engineering_id`` is the immutable identity; ``tag``/``tag_key`` are mutable
    engineering attributes. ``tag_key`` exists so tag-based addressing keeps working
    (``equipment:p-101``), but it is *not* the identity and may change freely.
    """

    engineering_id: str = Field(min_length=1)
    kind: ObjectKind
    identity_basis: IdentityBasis
    declared_id: str = ""
    tag: str = ""
    tag_key: str = ""
    name: str = ""
    label: str = ""
    symbol_key: str = ""
    symbol_name: str = ""
    category: str = ""
    capability: str = ""
    element_ids: list[str] = Field(default_factory=list)
    primary_element_id: str
    layer_id: str = ""
    layer_name: str = ""
    system_id: str = ""
    system_name: str = ""
    media: str = ""
    medium_class: str = ""
    nominal_diameter: str = ""
    flow_direction: str = "none"
    required_port_count: int = 0
    connected_port_count: int = 0
    connection_count: int = 0
    length: float = 0.0
    opc_direction: Literal["in", "out", ""] = ""
    target_document_id: str = ""
    #: Stable identity of the off-page connection this connector carries.
    off_page_connection_id: str = ""
    signal: SignalDetail | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class TopologyEdge(StrictModel):
    """A connection between two engineering objects.

    ``edge_class`` keeps instrument wiring out of process topology: a signal edge is
    never part of a flow path, and a process edge never carries a signal.
    """

    connector_id: str
    edge_class: EdgeClass = "process"
    pipeline_engineering_id: str = ""
    signal_engineering_id: str = ""
    source_engineering_id: str
    target_engineering_id: str
    source_port_id: str = ""
    target_port_id: str = ""
    medium: str = ""
    medium_class: str = ""
    flow_direction: Literal["forward", "reverse", "none"] = "none"
    directed: bool = False


class GraphFinding(StrictModel):
    severity: FindingSeverity
    code: str
    message: str
    object_ids: list[str] = Field(default_factory=list)
    element_ids: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class EngineeringGraphCounts(StrictModel):
    equipment: int = 0
    valves: int = 0
    instruments: int = 0
    signals: int = 0
    lines: int = 0
    junctions: int = 0
    off_page_connectors: int = 0
    annotations: int = 0
    graphics: int = 0
    objects: int = 0
    edges: int = 0
    process_edges: int = 0
    signal_edges: int = 0
    errors: int = 0
    warnings: int = 0
    info: int = 0


class EngineeringGraph(StrictModel):
    schema_name: Literal["pid-agent.engineering-graph"] = Field(
        default=IR_SCHEMA, alias="schema"
    )
    version: Literal[2] = IR_VERSION
    builder_version: Literal[2] = IR_BUILDER_VERSION
    document_id: str
    document_name: str
    revision: int
    content_hash: str
    counts: EngineeringGraphCounts
    objects: list[EngineeringObject]
    edges: list[TopologyEdge]
    #: Engineering ids of the first-class signal objects, sorted.
    signals: list[str]
    off_page_object_ids: list[str]
    connectivity_components: list[list[str]]
    findings: list[GraphFinding]

    def object(self, ref: str) -> EngineeringObject | None:
        """Resolve an object by engineering id, tag key, tag or drawing element id.

        Tag-based references are accepted for backwards compatibility with callers
        that still address objects by tag (``equipment:p-101``); the returned object's
        ``engineering_id`` is what follow-up calls should use.
        """

        return resolve_object(self, ref)


class TraceStep(StrictModel):
    depth: int
    engineering_id: str
    kind: ObjectKind
    via_connector_id: str = ""
    direction: Literal["upstream", "downstream", "undirected", "origin"] = "undirected"


class TraceResult(StrictModel):
    """Result of walking the graph.

    Steps are *engineering objects* of one edge class. Pipelines are deliberately not
    steps: a pipeline aggregates every connector that shares a tag, so it is not a
    single node on a path. The pipelines actually traversed are reported separately,
    and a trace may also be *started* from a pipeline object to enumerate what it
    connects.
    """

    document_id: str
    revision: int
    origin_engineering_id: str
    resolved_from: str = ""
    direction: TraceDirection
    edge_class: EdgeClass = "process"
    steps: list[TraceStep]
    reached_engineering_ids: list[str]
    traversed_pipeline_ids: list[str] = Field(default_factory=list)
    truncated: bool = False


# --------------------------------------------------------------------------- #
# Content hash
# --------------------------------------------------------------------------- #


def _element_projection(element: Element) -> dict[str, Any]:
    payload = element.model_dump(mode="json")
    payload["style"] = {}
    return payload


def document_content_hash(document: Document) -> str:
    """Hash the engineering content that the graph depends on.

    Editor-only fields (style, timestamps, name) are excluded so a restyle does not
    invalidate the index, while any change to identity, connectivity or attributes
    does. This is the staleness check for the project index.
    """

    payload = {
        "id": document.id,
        "layers": [layer.model_dump(mode="json") for layer in document.layers],
        "systems": [system.model_dump(mode="json") for system in document.systems],
        "elements": [
            _element_projection(element)
            for element in sorted(document.elements, key=lambda item: item.id)
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Identity helpers
# --------------------------------------------------------------------------- #


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _stable_id(kind: str, anchor: str) -> str:
    """Immutable surrogate id: ``eq_9f3a2b1c4d5e`` for ``sha256("kind|anchor")``."""

    return f"{_ID_PREFIX[kind]}_{_digest(kind, anchor)}"


def off_page_connection_pair_id(first: str, second: str) -> str:
    """Symmetric identity of a *resolved* off-page connection.

    Derived from the two stable engineering ids (sorted, so both drawings agree) and
    never from a tag, line number or service name: renaming a service cannot change
    which connection this is.
    """

    return f"opc_conn_{_digest('off_page_connection_pair', *sorted([first, second]))}"


def off_page_connection_open_id(engineering_id: str, target_document_id: str) -> str:
    """Identity of an off-page connection whose other end is not resolvable here."""

    return f"opc_conn_{_digest('off_page_connection_open', engineering_id, target_document_id)}"


def off_page_connection_id_for(anchor: str, *, declared: str = "") -> str:
    """Stable identifier of one side of an off-page connection.

    Derived from the connector's own stable anchor (never from its tag), so renaming
    the service/tag keeps the connection identity while the declared id wins when the
    drawing provides one.
    """

    if declared.strip():
        return declared.strip()
    return f"opc_conn_{_digest('off_page_connection', anchor)}"


def _slug(value: str) -> str:
    cleaned = " ".join(value.strip().split()).casefold()
    out = [char if (char.isalnum() or char in "-_.") else "-" for char in cleaned]
    slug = "".join(out).strip("-")
    return slug or "unnamed"


def _element_data(element: Any) -> dict[str, Any]:
    """Merged free-form attribute bags: ``properties`` (symbols) over ``metadata``.

    ``ElementBase`` gives every element a ``metadata`` bag while symbols additionally
    carry a typed ``properties`` bag, so declared ids are read from both.
    """

    merged: dict[str, Any] = {}
    metadata = getattr(element, "metadata", None)
    if isinstance(metadata, dict):
        merged.update(metadata)
    properties = getattr(element, "properties", None)
    if isinstance(properties, dict):
        merged.update(properties)
    return merged


def _prop(element: Any, key: str) -> str:
    return str(_element_data(element).get(key, "") or "").strip()


def _declared_id(kind: str, elements: list[Any]) -> str:
    """First declared stable id found on the element(s), in a deterministic order."""

    for element in elements:
        data = _element_data(element)
        for key in _DECLARED_ID_KEYS[kind]:
            value = str(data.get(key, "") or "").strip()
            if value:
                return value
    return ""


def _tag_key(kind: str, tag: str, used: Counter[str]) -> str:
    """Human/tag-derived alias key, deterministically disambiguated, or ``""``."""

    if not tag.strip():
        return ""
    base = f"{_TAG_PREFIX[kind]}:{_slug(tag)}"
    used[base] += 1
    if used[base] == 1:
        return base
    return f"{base}#{used[base]}"


def legacy_key(kind: str, element_id: str) -> str:
    """The pre-identity-rework key form (``equipment:p-101``, ``annotation:note-1``).

    Kept as a resolution alias so integrations that address objects by tag or by
    element-derived key keep working after identity moved to stable ids.
    """

    return f"{_TAG_PREFIX[kind]}:{_slug(element_id)}"


def resolve_object(graph: EngineeringGraph, ref: str) -> EngineeringObject | None:
    """Resolve a reference to an object.

    Accepted, in order: exact ``engineering_id``, ``tag_key``, raw ``tag``
    (case-insensitive, first by engineering id), the legacy element-derived key form
    and finally the drawing ``element_id``. Tag-based references are a convenience for
    humans and older integrations — the returned ``engineering_id`` is the identity.
    """

    needle = ref.strip()
    if not needle:
        return None
    for record in graph.objects:
        if record.engineering_id == needle:
            return record
    for record in graph.objects:
        if record.tag_key and record.tag_key == needle:
            return record
    folded = needle.casefold()
    candidates = [
        record
        for record in graph.objects
        if (record.tag and record.tag.casefold() == folded)
        or (record.tag_key and record.tag_key.casefold() == folded)
    ]
    if candidates:
        return sorted(candidates, key=lambda item: item.engineering_id)[0]
    for record in graph.objects:
        if needle in record.element_ids or record.primary_element_id == needle:
            return record
    for record in graph.objects:
        if legacy_key(record.kind, record.primary_element_id) == needle.casefold():
            return record
        if any(legacy_key(record.kind, element_id) == needle.casefold() for element_id in record.element_ids):
            return record
    return None


# --------------------------------------------------------------------------- #
# Element-level classification
# --------------------------------------------------------------------------- #


def _symbol_kind(element: Any, registry: SymbolRegistry) -> tuple[ObjectKind, str, str, Any]:
    """Return (kind, capability, definition_name, definition)."""

    try:
        definition = registry.get(element.symbol_key)
    except KeyError:
        return "equipment", "", "", None
    capability = str(definition.metadata.get("capability", "")).strip().casefold()
    if capability == "opc" or is_opc_symbol(element, registry):
        return "off_page_connector", "opc", definition.name, definition
    if capability == "valve" or is_valve_symbol(element, registry):
        return "valve", "valve", definition.name, definition
    if "仪表" in definition.category or "instrument" in definition.category.casefold():
        return "instrument", "instrument", definition.name, definition
    return "equipment", capability, definition.name, definition


def _port_connections(connectors: list[Any]) -> dict[tuple[str, str], set[str]]:
    connected: dict[tuple[str, str], set[str]] = defaultdict(set)
    for connector in connectors:
        for endpoint in (connector.source, connector.target):
            if endpoint and endpoint.element_id and endpoint.port_id:
                connected[(endpoint.element_id, endpoint.port_id)].add(connector.id)
    return connected


def _polyline_length(points: list[Any]) -> float:
    return sum(
        hypot(points[index + 1].x - points[index].x, points[index + 1].y - points[index].y)
        for index in range(len(points) - 1)
    )


def _signal_type(medium: str) -> SignalType | None:
    """Classify a medium as instrument wiring, and if so, which family.

    Returns ``None`` when the medium is not a signal at all. Utility fluids whose
    names contain signal-ish words (``instrument air``/``仪表风``) stay process lines,
    because they carry fluid, not information.
    """

    normalized = " ".join(medium.strip().casefold().replace("_", " ").split())
    if not normalized:
        return None
    if any(token in normalized for token in _PROCESS_UTILITY_TOKENS):
        return None
    for signal_type, tokens in _SIGNAL_FAMILY_TOKENS:
        if any(token in normalized for token in tokens):
            return signal_type
    if any(token in normalized for token in _SIGNAL_GENERIC_TOKENS):
        return "unknown"
    return None


def _is_signal_connector(medium: str, instrument_endpoint_count: int) -> bool:
    return _signal_type(medium) is not None or instrument_endpoint_count == 2


# --------------------------------------------------------------------------- #
# Graph construction
# --------------------------------------------------------------------------- #


class _IdentityAllocator:
    """Allocates immutable ids; a collision is a drawing error, never a silent merge."""

    def __init__(self) -> None:
        self.used: Counter[str] = Counter()
        self.collisions: dict[str, list[str]] = defaultdict(list)

    def allocate(self, kind: ObjectKind, anchor: str) -> tuple[str, bool]:
        base = _stable_id(kind, anchor)
        self.used[base] += 1
        if self.used[base] == 1:
            return base, False
        return f"{base}#{self.used[base]}", True

    def allocate_from(self, kind: ObjectKind, anchors: list[str]) -> tuple[str, str, bool]:
        """Allocate using the first anchor that is free, else suffix deterministically.

        Used where a derived identity has a natural preferred basis and a documented
        fallback (a line anchored on its endpoints, then on its specification), so a
        genuinely ambiguous drawing still produces deterministic, distinct ids.
        """

        for anchor in anchors:
            base = _stable_id(kind, anchor)
            if self.used[base] == 0:
                self.used[base] = 1
                return base, anchor, False
        base = _stable_id(kind, anchors[0])
        self.used[base] += 1
        return f"{base}#{self.used[base]}", anchors[0], True

    def record(self, base_key: str, engineering_id: str) -> None:
        if base_key:
            self.collisions[base_key].append(engineering_id)


def build_engineering_graph(document: Document, registry: SymbolRegistry) -> EngineeringGraph:
    layer_names = {layer.id: layer.name for layer in document.layers}
    system_names = {system.id: system.name for system in document.systems}
    elements = list(document.elements)
    connectors = [element for element in elements if element.type == "connector"]
    connections = _port_connections(connectors)

    identity = _IdentityAllocator()
    tag_keys: Counter[str] = Counter()
    objects: list[EngineeringObject] = []
    element_to_object: dict[str, str] = {}
    findings: list[GraphFinding] = []
    id_collisions: dict[str, list[str]] = defaultdict(list)

    def add_object(
        *,
        kind: ObjectKind,
        tag: str,
        anchor: str,
        declared: str,
        element_ids: list[str],
        anchors: list[str] | None = None,
        **fields: Any,
    ) -> EngineeringObject:
        if anchors and len(anchors) > 1:
            engineering_id, anchor, collided = identity.allocate_from(kind, anchors)
        else:
            engineering_id, collided = identity.allocate(kind, anchor)
        if collided:
            id_collisions[_stable_id(kind, anchor)].append(engineering_id)
        tag_key = _tag_key(kind, tag, tag_keys)
        identity.record(tag_key.split("#")[0] if tag_key else "", engineering_id)
        record = EngineeringObject(
            engineering_id=engineering_id,
            kind=kind,
            identity_basis="declared" if declared else "element",
            declared_id=declared,
            tag=tag.strip(),
            tag_key=tag_key,
            element_ids=element_ids,
            primary_element_id=element_ids[0] if element_ids else anchor,
            **fields,
        )
        objects.append(record)
        return record

    # --- symbols ---------------------------------------------------------- #
    for element in elements:
        if element.type == "symbol":
            kind, capability, symbol_name, definition = _symbol_kind(element, registry)
            if definition is None:
                findings.append(
                    GraphFinding(
                        severity="error",
                        code="IR_SYMBOL_DEFINITION_MISSING",
                        message=f"符号 {element.id} 引用了未知图例 {element.symbol_key}。",
                        element_ids=[element.id],
                        details={"symbol_key": element.symbol_key},
                    )
                )
            required_ports = [
                port for port in (definition.ports if definition else []) if port.direction != "none"
            ]
            connected = sum(
                1 for port in required_ports if connections.get((element.id, port.id))
            )
            tag = element.label.strip() or _prop(element, "tag")
            declared = _declared_id(kind, [element])
            anchor = declared or element.id
            data = _element_data(element)
            record = add_object(
                kind=kind,
                tag=tag,
                anchor=anchor,
                declared=declared,
                element_ids=[element.id],
                name=element.name,
                label=element.label,
                symbol_key=element.symbol_key,
                symbol_name=symbol_name,
                category=definition.category if definition else "",
                capability=capability,
                layer_id=element.layer_id,
                layer_name=layer_names.get(element.layer_id, element.layer_id),
                system_id=element.system_id,
                system_name=system_names.get(element.system_id, element.system_id),
                required_port_count=len(required_ports),
                connected_port_count=connected,
                connection_count=sum(
                    len(connections.get((element.id, port.id), ())) for port in required_ports
                ),
                opc_direction=(
                    (opc_direction(element, registry) or "") if kind == "off_page_connector" else ""
                ),
                target_document_id=_prop(element, "target_document_id"),
                off_page_connection_id=(
                    off_page_connection_id_for(
                        declared or element.id,
                        declared=_prop(element, "connection_id"),
                    )
                    if kind == "off_page_connector"
                    else ""
                ),
                properties=data,
            )
            element_to_object[element.id] = record.engineering_id
        elif element.type == "junction":
            declared = _declared_id("junction", [element])
            record = add_object(
                kind="junction",
                tag=element.label.strip(),
                anchor=declared or element.id,
                declared=declared,
                element_ids=[element.id],
                name=element.name,
                label=element.label,
                layer_id=element.layer_id,
                layer_name=layer_names.get(element.layer_id, element.layer_id),
                system_id=element.system_id,
                system_name=system_names.get(element.system_id, element.system_id),
                connection_count=sum(
                    1
                    for connector in connectors
                    if (connector.source and connector.source.element_id == element.id)
                    or (connector.target and connector.target.element_id == element.id)
                ),
            )
            element_to_object[element.id] = record.engineering_id
        elif element.type == "text":
            declared = _declared_id("annotation", [element])
            record = add_object(
                kind="annotation",
                tag="",
                anchor=declared or element.id,
                declared=declared,
                element_ids=[element.id],
                name=element.name,
                label=element.text,
                layer_id=element.layer_id,
                layer_name=layer_names.get(element.layer_id, element.layer_id),
                system_id=element.system_id,
                system_name=system_names.get(element.system_id, element.system_id),
            )
            element_to_object[element.id] = record.engineering_id

    # --- connector classification (process vs signal) ---------------------- #
    def endpoint_object_id(endpoint: Any) -> str:
        if endpoint is None or not endpoint.element_id:
            return ""
        return element_to_object.get(endpoint.element_id, "")

    def endpoint_kind(object_id: str) -> str:
        return _object_kind(objects, object_id) if object_id else ""

    connector_classes: dict[str, tuple[EdgeClass, str | None]] = {}
    for connector in connectors:
        endpoint_ids = [endpoint_object_id(connector.source), endpoint_object_id(connector.target)]
        instruments = [
            object_id
            for object_id in endpoint_ids
            if object_id and endpoint_kind(object_id) == "instrument"
        ]
        signal_type = _signal_type(connector.medium)
        if signal_type is not None:
            connector_classes[connector.id] = ("signal", signal_type)
        elif len(instruments) == 2:
            connector_classes[connector.id] = ("signal", "unknown")
        else:
            connector_classes[connector.id] = ("process", None)

    process_connectors = [
        connector for connector in connectors if connector_classes[connector.id][0] == "process"
    ]

    # --- pipelines (line aggregation over process connectors only) -------- #
    pipelines: dict[tuple[str, ...], list[Any]] = defaultdict(list)
    for connector in process_connectors:
        tag = connector.process_tag.strip()
        if tag:
            key = (
                "tag",
                tag.casefold(),
                normalize_flow_medium(connector.medium),
                connector.nominal_diameter.strip().casefold(),
            )
        else:
            key = ("element", connector.id)
        pipelines[key].append(connector)

    for key in sorted(pipelines, key=lambda item: item):
        members = sorted(pipelines[key], key=lambda item: item.id)
        first = members[0]
        tag = first.process_tag.strip()
        media = sorted({member.medium.strip() for member in members if member.medium.strip()})
        diameters = sorted(
            {member.nominal_diameter.strip() for member in members if member.nominal_diameter.strip()}
        )
        endpoint_ids = sorted(
            {
                endpoint.element_id
                for member in members
                for endpoint in (member.source, member.target)
                if endpoint and endpoint.element_id
            }
        )
        declared = _declared_id("line", members)
        external_endpoints = _external_endpoints(members, connectors)
        # Identity: the declared line id when the drawing carries one, otherwise the
        # *external* nodes the line connects (a junction created purely by splitting one
        # of its own segments is internal and must not move the identity). Never the
        # tag/line number — that is a mutable engineering attribute (``line_number``),
        # reported separately.
        anchors = [
            anchor
            for anchor in (
                declared,
                "endpoints:" + "|".join(external_endpoints) if external_endpoints else "",
                (
                    "endpoints:"
                    + "|".join(external_endpoints)
                    + "|"
                    + normalize_flow_medium(first.medium)
                    + "|"
                    + first.nominal_diameter.strip().casefold()
                    if external_endpoints
                    else ""
                ),
            )
            if anchor
        ] or ["members:" + "|".join(sorted(member.id for member in members))]
        record = add_object(
            kind="line",
            tag=tag,
            anchor=anchors[0],
            declared=declared,
            anchors=anchors,
            element_ids=[member.id for member in members],
            name=first.name,
            label=tag,
            media=" / ".join(media),
            medium_class=normalize_flow_medium(first.medium),
            nominal_diameter=" / ".join(diameters),
            flow_direction=first.flow_direction,
            connection_count=len(endpoint_ids),
            length=round(sum(_polyline_length(member.points) for member in members), 6),
            layer_id=first.layer_id,
            layer_name=layer_names.get(first.layer_id, first.layer_id),
            system_id=first.system_id,
            system_name=system_names.get(first.system_id, first.system_id),
            properties={
                "member_count": len(members),
                "line_number": tag,
            },
        )
        for member in members:
            element_to_object[member.id] = record.engineering_id

    # --- graphics (kept addressable, excluded from topology) -------------- #
    for element in elements:
        if element.type in {"line", "polyline", "rectangle", "circle"}:
            declared = _declared_id("graphic", [element])
            add_object(
                kind="graphic",
                tag="",
                anchor=declared or element.id,
                declared=declared,
                element_ids=[element.id],
                name=element.name,
                layer_id=element.layer_id,
                layer_name=layer_names.get(element.layer_id, element.layer_id),
                system_id=element.system_id,
                system_name=system_names.get(element.system_id, element.system_id),
            )

    # --- edges: process flow and instrument signals, kept apart ----------- #
    edges: list[TopologyEdge] = []
    signal_object_ids: list[str] = []
    for connector in sorted(connectors, key=lambda item: item.id):
        edge_class, signal_type = connector_classes[connector.id]
        source_id = endpoint_object_id(connector.source)
        target_id = endpoint_object_id(connector.target)
        source_ref = source_id or f"unbound:{connector.id}:source"
        target_ref = target_id or f"unbound:{connector.id}:target"
        instrument_endpoints = sorted(
            object_id
            for object_id in (source_id, target_id)
            if object_id and endpoint_kind(object_id) == "instrument"
        )
        if edge_class == "signal":
            tag = connector.process_tag.strip()
            declared = _declared_id("signal", [connector])
            anchor = declared or connector.id
            record = add_object(
                kind="signal",
                tag=tag,
                anchor=anchor,
                declared=declared,
                element_ids=[connector.id],
                name=connector.name,
                label=tag,
                media=connector.medium.strip(),
                medium_class=normalize_flow_medium(connector.medium),
                layer_id=connector.layer_id,
                layer_name=layer_names.get(connector.layer_id, connector.layer_id),
                system_id=connector.system_id,
                system_name=system_names.get(connector.system_id, connector.system_id),
                connection_count=len([item for item in (source_id, target_id) if item]),
                length=round(_polyline_length(connector.points), 6),
                signal=SignalDetail(
                    signal_id="",  # filled in below, once the id is known
                    connector_id=connector.id,
                    signal_type=signal_type or "unknown",
                    medium=connector.medium,
                    medium_class=normalize_flow_medium(connector.medium),
                    source_engineering_id=source_ref,
                    target_engineering_id=target_ref,
                    instrument_engineering_ids=instrument_endpoints,
                    equipment_engineering_ids=sorted(
                        object_id
                        for object_id in (source_id, target_id)
                        if object_id and endpoint_kind(object_id) not in {"instrument", ""}
                    ),
                    classification=(
                        "declared_medium"
                        if _signal_type(connector.medium) is not None
                        else "instrument_to_instrument"
                    ),
                    provenance={
                        "basis": (
                            "medium"
                            if _signal_type(connector.medium) is not None
                            else "endpoint_kinds"
                        ),
                        "medium": connector.medium,
                        "element_ids": [connector.id],
                    },
                ),
                properties=_element_data(connector),
            )
            # The signal object knows its own id, which the detail needs.
            record.signal = record.signal.model_copy(update={"signal_id": record.engineering_id})
            signal_object_ids.append(record.engineering_id)
            element_to_object[connector.id] = record.engineering_id
            edges.append(
                TopologyEdge(
                    connector_id=connector.id,
                    edge_class="signal",
                    signal_engineering_id=record.engineering_id,
                    source_engineering_id=source_ref,
                    target_engineering_id=target_ref,
                    source_port_id=(connector.source.port_id or "") if connector.source else "",
                    target_port_id=(connector.target.port_id or "") if connector.target else "",
                    medium=connector.medium,
                    medium_class=normalize_flow_medium(connector.medium),
                    flow_direction=connector.flow_direction,
                    directed=connector.flow_direction != "none",
                )
            )
            if not instrument_endpoints:
                findings.append(
                    GraphFinding(
                        severity="warning",
                        code="IR_SIGNAL_WITHOUT_INSTRUMENT",
                        message=f"信号 {tag or record.engineering_id} 未连接到任何仪表对象。",
                        object_ids=[record.engineering_id],
                        element_ids=[connector.id],
                        details={"classification": record.signal.classification},
                    )
                )
            continue

        pipeline_id = element_to_object.get(connector.id, "")
        declared_direction = connector.flow_direction != "none"
        edges.append(
            TopologyEdge(
                connector_id=connector.id,
                edge_class="process",
                pipeline_engineering_id=pipeline_id,
                source_engineering_id=source_ref,
                target_engineering_id=target_ref,
                source_port_id=(connector.source.port_id or "") if connector.source else "",
                target_port_id=(connector.target.port_id or "") if connector.target else "",
                medium=connector.medium,
                medium_class=normalize_flow_medium(connector.medium),
                flow_direction=connector.flow_direction,
                directed=declared_direction,
            )
        )

        if connector.source and connector.source.element_id and not source_id:
            findings.append(
                GraphFinding(
                    severity="error",
                    code="IR_ENDPOINT_ELEMENT_MISSING",
                    message=(
                        f"管线 {connector.process_tag.strip() or connector.id} 的 source 端引用了"
                        f"不存在的元素 {connector.source.element_id}。"
                    ),
                    element_ids=[connector.id, connector.source.element_id],
                    details={"endpoint": "source"},
                )
            )
        if connector.target and connector.target.element_id and not target_id:
            findings.append(
                GraphFinding(
                    severity="error",
                    code="IR_ENDPOINT_ELEMENT_MISSING",
                    message=(
                        f"管线 {connector.process_tag.strip() or connector.id} 的 target 端引用了"
                        f"不存在的元素 {connector.target.element_id}。"
                    ),
                    element_ids=[connector.id, connector.target.element_id],
                    details={"endpoint": "target"},
                )
            )

    # --- derived findings --------------------------------------------------- #
    for base, duplicates in sorted(id_collisions.items()):
        findings.append(
            GraphFinding(
                severity="error",
                code="IR_IDENTITY_COLLISION",
                message=f"稳定工程标识 {base} 被 {len(duplicates)} 个对象重复分配。",
                object_ids=sorted(duplicates),
                details={"engineering_id": base, "count": len(duplicates)},
                element_ids=[],
            )
        )

    for base_key, engineering_ids in sorted(identity.collisions.items()):
        if not base_key or len(engineering_ids) < 2:
            continue
        findings.append(
            GraphFinding(
                severity="error",
                code="IR_DUPLICATE_IDENTITY",
                message=(
                    f"位号 {base_key.split(':')[-1].upper()} 被 {len(engineering_ids)} 个对象重复使用。"
                ),
                object_ids=sorted(engineering_ids),
                details={"identity": base_key, "count": len(engineering_ids)},
            )
        )

    declared_connections: dict[str, list[str]] = defaultdict(list)
    for record in objects:
        if record.kind == "off_page_connector" and record.declared_id:
            declared_connections[record.off_page_connection_id].append(record.engineering_id)
        if record.kind == "off_page_connector" and not record.target_document_id:
            findings.append(
                GraphFinding(
                    severity="warning",
                    code="IR_OPC_TARGET_MISSING",
                    message=f"跨图连接 {record.tag or record.engineering_id} 未声明目标图纸。",
                    object_ids=[record.engineering_id],
                    element_ids=list(record.element_ids),
                )
            )
        if record.kind in {"equipment", "valve"} and record.connection_count == 0:
            findings.append(
                GraphFinding(
                    severity="info",
                    code="IR_ISOLATED_OBJECT",
                    message=f"{record.label or record.engineering_id} 未与任何管线相连。",
                    object_ids=[record.engineering_id],
                    element_ids=list(record.element_ids),
                )
            )
        if record.kind == "line" and record.connection_count == 0:
            findings.append(
                GraphFinding(
                    severity="warning",
                    code="IR_ORPHAN_LINE",
                    message=(
                        f"管线 {record.tag or record.engineering_id} 两端均未绑定设备或连接节点。"
                    ),
                    object_ids=[record.engineering_id],
                    element_ids=list(record.element_ids),
                )
            )
        if record.kind == "signal" and not record.tag:
            findings.append(
                GraphFinding(
                    severity="info",
                    code="IR_SIGNAL_UNNAMED",
                    message=f"信号 {record.engineering_id} 未声明信号标识（位号）。",
                    object_ids=[record.engineering_id],
                    element_ids=list(record.element_ids),
                )
            )

    for connection_id, object_ids in sorted(declared_connections.items()):
        if len(object_ids) < 2:
            continue
        findings.append(
            GraphFinding(
                severity="error",
                code="IR_OPC_CONNECTION_ID_DUPLICATE",
                message=f"跨图连接身份 {connection_id} 被 {len(object_ids)} 个连接器重复声明。",
                object_ids=sorted(object_ids),
                details={"off_page_connection_id": connection_id, "count": len(object_ids)},
            )
        )

    components = _connectivity_components(objects, edges)
    findings.sort(key=lambda item: (item.severity, item.code, tuple(item.object_ids), item.message))

    kind_count = Counter(record.kind for record in objects)
    process_edges = [edge for edge in edges if edge.edge_class == "process"]
    signal_edges = [edge for edge in edges if edge.edge_class == "signal"]
    return EngineeringGraph(
        document_id=document.id,
        document_name=document.name,
        revision=document.revision,
        content_hash=document_content_hash(document),
        counts=EngineeringGraphCounts(
            equipment=kind_count["equipment"],
            valves=kind_count["valve"],
            instruments=kind_count["instrument"],
            signals=kind_count["signal"],
            lines=kind_count["line"],
            junctions=kind_count["junction"],
            off_page_connectors=kind_count["off_page_connector"],
            annotations=kind_count["annotation"],
            graphics=kind_count["graphic"],
            objects=len(objects),
            edges=len(edges),
            process_edges=len(process_edges),
            signal_edges=len(signal_edges),
            errors=sum(item.severity == "error" for item in findings),
            warnings=sum(item.severity == "warning" for item in findings),
            info=sum(item.severity == "info" for item in findings),
        ),
        objects=objects,
        edges=edges,
        signals=sorted(signal_object_ids),
        off_page_object_ids=sorted(
            record.engineering_id for record in objects if record.kind == "off_page_connector"
        ),
        connectivity_components=components,
        findings=findings,
    )


def _external_endpoints(members: list[Any], connectors: list[Any]) -> list[str]:
    """Nodes a line connects *outside itself*, sorted.

    A node that the line merely passes through — every connector touching it is one of
    the line's own segments, and at least two of them are — is internal, not an
    endpoint. Splitting one segment in two creates exactly such a node, so this keeps
    the line's identity stable while still moving it when the line really reaches
    somewhere new. A leaf node (touched by a single member segment) is an endpoint.
    """

    member_ids = {member.id for member in members}
    touching: dict[str, set[str]] = defaultdict(set)
    for connector in connectors:
        for endpoint in (connector.source, connector.target):
            if endpoint and endpoint.element_id:
                touching[endpoint.element_id].add(connector.id)
    endpoints = {
        endpoint.element_id
        for member in members
        for endpoint in (member.source, member.target)
        if endpoint and endpoint.element_id
    }

    def is_internal(element_id: str) -> bool:
        connectors_here = touching.get(element_id, set())
        return connectors_here <= member_ids and len(connectors_here) >= 2

    return sorted(element_id for element_id in endpoints if not is_internal(element_id))


def _object_kind(objects: list[EngineeringObject], engineering_id: str) -> str:
    for record in objects:
        if record.engineering_id == engineering_id:
            return record.kind
    return ""


def _connectivity_components(
    objects: list[EngineeringObject],
    edges: list[TopologyEdge],
) -> list[list[str]]:
    """Union-find over process objects and process edges; signals/annotations excluded."""

    parent: dict[str, str] = {
        record.engineering_id: record.engineering_id
        for record in objects
        if record.kind in TOPO_KINDS
    }

    def find(node: str) -> str:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(first: str, second: str) -> None:
        if first not in parent or second not in parent:
            return
        root_a, root_b = find(first), find(second)
        if root_a != root_b:
            parent[max(root_a, root_b)] = min(root_a, root_b)

    for edge in edges:
        if edge.edge_class != "process":
            continue
        union(edge.source_engineering_id, edge.target_engineering_id)
        if edge.pipeline_engineering_id:
            union(edge.source_engineering_id, edge.pipeline_engineering_id)
            union(edge.target_engineering_id, edge.pipeline_engineering_id)

    groups: dict[str, list[str]] = defaultdict(list)
    for node in parent:
        groups[find(node)].append(node)
    return sorted((sorted(members) for members in groups.values()), key=lambda item: item[0])


def graph_fingerprint(graph: EngineeringGraph) -> str:
    canonical = json.dumps(
        graph.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Tracing
# --------------------------------------------------------------------------- #


def _neighbours(
    edges: list[TopologyEdge],
    node: str,
    edge_class: EdgeClass,
) -> list[tuple[str, str, Literal["upstream", "downstream", "undirected"]]]:
    """Return (neighbour, connector_id, direction) sorted for determinism."""

    out: list[tuple[str, str, Literal["upstream", "downstream", "undirected"]]] = []
    for edge in edges:
        if edge.edge_class != edge_class:
            continue
        if edge_class == "signal":
            # A signal object *is* the connection: the wiring runs from one end, through
            # the signal, to the other end — never directly end-to-end.
            for peer, direction in _signal_peers(edge, node):
                out.append(
                    (peer, edge.connector_id, direction if edge.directed else "undirected")
                )
            continue
        if edge.source_engineering_id == node and edge.target_engineering_id:
            out.append(
                (
                    edge.target_engineering_id,
                    edge.connector_id,
                    "downstream" if edge.directed else "undirected",
                )
            )
        if edge.target_engineering_id == node and edge.source_engineering_id:
            out.append(
                (
                    edge.source_engineering_id,
                    edge.connector_id,
                    "upstream" if edge.directed else "undirected",
                )
            )
    return sorted(out, key=lambda item: (item[0], item[1]))


def _signal_peers(
    edge: TopologyEdge, node: str
) -> list[tuple[str, Literal["upstream", "downstream", "undirected"]]]:
    """Neighbours of one node along a signal edge, routed through the signal object."""

    signal_id = edge.signal_engineering_id
    endpoints = (edge.source_engineering_id, edge.target_engineering_id)
    if signal_id and node == signal_id:
        return [
            (endpoint, direction)
            for endpoint, direction in zip(endpoints, ("upstream", "downstream"), strict=True)
            if endpoint and not endpoint.startswith("unbound:")
        ]
    if signal_id and node in endpoints:
        directed = "downstream" if node == endpoints[0] else "upstream"
        return [(signal_id, directed)]
    if node == endpoints[0] and endpoints[1] and not endpoints[1].startswith("unbound:"):
        return [(endpoints[1], "undirected")]
    if node == endpoints[1] and endpoints[0] and not endpoints[0].startswith("unbound:"):
        return [(endpoints[0], "undirected")]
    return []


def _node_neighbours(
    graph: EngineeringGraph,
    node: str,
    edge_class: EdgeClass,
) -> list[tuple[str, str, Literal["upstream", "downstream", "undirected"]]]:
    """Expand one node, including the "start from a pipeline" case.

    For a pipeline node the members are the connectors sharing that tag: their
    endpoints are reported with the pipeline's own reading direction (declared
    source is upstream, declared target is downstream; undeclared flow is
    undirected), which is the only interpretation the drawing actually supports.
    """

    member_edges = [
        edge
        for edge in graph.edges
        if edge.edge_class == "process" and edge.pipeline_engineering_id == node
    ]
    if not member_edges:
        return _neighbours(graph.edges, node, edge_class)
    out: list[tuple[str, str, Literal["upstream", "downstream", "undirected"]]] = []
    for edge in member_edges:
        for endpoint_id, edge_direction in (
            (edge.source_engineering_id, "upstream"),
            (edge.target_engineering_id, "downstream"),
        ):
            if not endpoint_id or endpoint_id.startswith("unbound:"):
                continue
            out.append(
                (endpoint_id, edge.connector_id, edge_direction if edge.directed else "undirected")
            )
    return sorted(out, key=lambda item: (item[0], item[1]))


def trace_engineering_object(
    graph: EngineeringGraph,
    ref: str,
    *,
    direction: TraceDirection = "both",
    max_depth: int = 64,
    max_steps: int = 512,
) -> TraceResult:
    """Walk the graph from one engineering object.

    The reference may be an engineering id, a tag key, a tag or an element id. Traces
    stay inside one edge class: starting from a signal walks instrument wiring, and
    starting from anything else walks process flow, so the two networks are never
    conflated. Direction is honoured only for edges that declare a flow direction;
    undeclared (``none``) connections are traversable both ways, which matches how a
    P&ID is actually read (topology first, flow second).
    """

    origin = resolve_object(graph, ref)
    if origin is None:
        raise KeyError(f"unknown engineering object: {ref}")
    object_id = origin.engineering_id
    edge_class: EdgeClass = "signal" if origin.kind == "signal" else "process"

    allowed: set[str]
    if direction == "both":
        allowed = {"upstream", "downstream", "undirected"}
    elif direction == "downstream":
        allowed = {"downstream", "undirected"}
    else:
        allowed = {"upstream", "undirected"}

    steps: list[TraceStep] = [
        TraceStep(depth=0, engineering_id=object_id, kind=origin.kind, direction="origin")
    ]
    visited = {object_id}
    queue: deque[tuple[str, int]] = deque([(object_id, 0)])
    truncated = False
    while queue:
        node, depth = queue.popleft()
        if depth >= max_depth:
            truncated = True
            continue
        for neighbour, connector_id, edge_direction in _node_neighbours(graph, node, edge_class):
            if edge_direction not in allowed:
                continue
            if neighbour in visited or neighbour.startswith("unbound:"):
                continue
            if len(steps) >= max_steps:
                truncated = True
                break
            visited.add(neighbour)
            record = graph.object(neighbour)
            steps.append(
                TraceStep(
                    depth=depth + 1,
                    engineering_id=neighbour,
                    kind=record.kind if record else "graphic",
                    via_connector_id=connector_id,
                    direction=edge_direction,
                )
            )
            queue.append((neighbour, depth + 1))
    traversed = {
        edge.pipeline_engineering_id
        for edge in graph.edges
        if edge.connector_id in {step.via_connector_id for step in steps if step.via_connector_id}
        and edge.pipeline_engineering_id
    }
    return TraceResult(
        document_id=graph.document_id,
        revision=graph.revision,
        origin_engineering_id=object_id,
        resolved_from=ref if ref != object_id else "",
        direction=direction,
        edge_class=edge_class,
        steps=steps,
        reached_engineering_ids=sorted(visited),
        traversed_pipeline_ids=sorted(traversed),
        truncated=truncated,
    )


__all__ = [
    "IR_BUILDER_VERSION",
    "IR_SCHEMA",
    "IR_VERSION",
    "PROCESS_KINDS",
    "TOPO_KINDS",
    "EdgeClass",
    "EngineeringGraph",
    "EngineeringGraphCounts",
    "EngineeringObject",
    "GraphFinding",
    "IdentityBasis",
    "ObjectKind",
    "SignalDetail",
    "SignalType",
    "TopologyEdge",
    "TraceResult",
    "TraceStep",
    "build_engineering_graph",
    "document_content_hash",
    "graph_fingerprint",
    "legacy_key",
    "off_page_connection_id_for",
    "off_page_connection_open_id",
    "off_page_connection_pair_id",
    "resolve_object",
    "trace_engineering_object",
]
