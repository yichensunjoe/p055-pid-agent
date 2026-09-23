"""M7-2 phase 2B step 3: which port each connection endpoint actually lands on.

Routing needs two anchors per connection, and a connection may name its ports or leave them
blank. The tempting shortcut is to let the router work that out as it goes -- pick something
plausible, keep moving -- and that is exactly the kind of quiet decision this milestone exists to
remove. So bindings are resolved *before* routing, into an immutable result, and the router
consumes bindings rather than ports:

    every connection endpoint -> exactly one resolved binding

Three rules, each with a stated refusal:

* **An explicit port must exist on the frozen symbol.** A port id the symbol does not define is a
  reference to geometry that is not there, and ``port_not_found`` says so instead of drawing a
  route from a guessed anchor.
* **An explicit port must point the way the connection runs.** A source endpoint on an input-only
  port is ``port_direction_mismatch``; it is not silently replaced by the nearest output.
* **An omitted port may be inferred only when exactly one candidate exists.** One candidate is a
  derivation -- the symbol can only mean one thing. Zero is ``no_compatible_port``, several is
  ``ambiguous_port_binding`` with the candidates named. "Sort them and take the first" is
  rejected on purpose: on a three-way valve it would quietly pick one of three, which is an
  arbitrary choice wearing the clothes of a rule.

A bidirectional port satisfies either role: a symbol that can flow both ways is not thereby
unusable, and refusing it would push the caller towards a worse symbol rather than a better
binding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .m7_layout_contract import (
    EXPLICIT_PORT_BINDING_MUST_BE_DIRECTION_COMPATIBLE,
    PORT_BINDING_CODES,
    PORT_BINDING_RESOLUTIONS,
    PORT_ROLE_COMPATIBLE_DIRECTIONS,
)
from .m7_symbol_geometry import (
    SymbolGeometryError,
    SymbolGeometryFact,
    SymbolGeometrySnapshot,
    declared_symbol_key,
    require_node_symbol,
)


class PortBindingError(SymbolGeometryError):
    """An endpoint whose port cannot be resolved to exactly one real port."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        connection_id: str = "",
        role: str = "",
        candidates: tuple[str, ...] = (),
    ) -> None:
        if code not in PORT_BINDING_CODES:
            raise AssertionError(
                f"{code!r} is not a declared port binding code {list(PORT_BINDING_CODES)}"
            )
        self.code = code
        self.connection_id = connection_id
        self.role = role
        self.candidates = candidates
        rendered = message
        if candidates:
            rendered = f"{message} (candidates: {list(candidates)})"
        super().__init__(rendered)


@dataclass(frozen=True)
class ResolvedEndpointBinding:
    """One endpoint's port, resolved once and then only read.

    ``resolution`` records *how* it was resolved, because "the model said so" and "the symbol
    admitted only one answer" are different facts about the drawing even when the port id is the
    same.
    """

    connection_id: str
    role: str
    node_id: str
    symbol_key: str
    port_id: str
    direction: str
    medium: str
    resolution: str = "explicit"
    node_kind: str = ""

    def __post_init__(self) -> None:
        if self.role not in {"source", "target"}:
            raise AssertionError(f"{self.role!r} is not a connection endpoint role")
        if self.resolution not in PORT_BINDING_RESOLUTIONS:
            raise AssertionError(
                f"{self.resolution!r} is not a declared resolution {list(PORT_BINDING_RESOLUTIONS)}"
            )

    @property
    def key(self) -> tuple[str, str]:
        return (self.connection_id, self.role)

    def to_projection(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "role": self.role,
            "node_id": self.node_id,
            "symbol_key": self.symbol_key,
            "port_id": self.port_id,
            "direction": self.direction,
            "medium": self.medium,
            "resolution": self.resolution,
            "node_kind": self.node_kind,
        }


def compatible_directions(role: str) -> tuple[str, ...]:
    """The port directions an endpoint of this role may use, read from the contract."""

    for declared_role, directions in PORT_ROLE_COMPATIBLE_DIRECTIONS:
        if declared_role == role:
            return tuple(directions)
    raise PortBindingError(
        "no_compatible_port",
        f"{role!r} is not a declared endpoint role, so no port direction can be "
        "considered compatible",
        role=role,
    )


def bind_endpoint(
    *,
    connection_id: str,
    role: str,
    node_id: str,
    fact: SymbolGeometryFact,
    declared_port_id: str,
    node_kind: str = "",
) -> ResolvedEndpointBinding:
    """The port one endpoint uses, or the refusal that names what is wrong with the reference."""

    allowed = compatible_directions(role)
    declared = (declared_port_id or "").strip()

    if declared:
        port = fact.port(declared)
        if port is None:
            raise PortBindingError(
                "port_not_found",
                f"connection {connection_id!r} {role} names port {declared!r}, which symbol "
                f"{fact.symbol_key!r} does not define (it defines "
                f"{[candidate.port_id for candidate in fact.ports]})",
                connection_id=connection_id,
                role=role,
            )
        if EXPLICIT_PORT_BINDING_MUST_BE_DIRECTION_COMPATIBLE and port.direction not in allowed:
            raise PortBindingError(
                "port_direction_mismatch",
                f"connection {connection_id!r} {role} names port {declared!r} on symbol "
                f"{fact.symbol_key!r}, whose direction is {port.direction!r}; a {role} endpoint "
                f"may use {list(allowed)}",
                connection_id=connection_id,
                role=role,
            )
        return ResolvedEndpointBinding(
            connection_id=connection_id,
            role=role,
            node_id=node_id,
            symbol_key=fact.symbol_key,
            port_id=port.port_id,
            direction=port.direction,
            medium=port.medium,
            resolution="explicit",
            node_kind=node_kind,
        )

    candidates = tuple(
        port.port_id for port in fact.ports if port.direction in allowed
    )
    if not candidates:
        raise PortBindingError(
            "no_compatible_port",
            f"connection {connection_id!r} {role} names no port, and symbol "
            f"{fact.symbol_key!r} has no port whose direction is in {list(allowed)}",
            connection_id=connection_id,
            role=role,
        )
    if len(candidates) > 1:
        raise PortBindingError(
            "ambiguous_port_binding",
            f"connection {connection_id!r} {role} names no port, and symbol "
            f"{fact.symbol_key!r} offers {len(candidates)} compatible ports; a unique inference "
            "is a derivation and this is not one",
            connection_id=connection_id,
            role=role,
            candidates=candidates,
        )
    port = fact.port(candidates[0])
    assert port is not None  # the candidate came from this fact
    return ResolvedEndpointBinding(
        connection_id=connection_id,
        role=role,
        node_id=node_id,
        symbol_key=fact.symbol_key,
        port_id=port.port_id,
        direction=port.direction,
        medium=port.medium,
        resolution="inferred_unique",
        node_kind=node_kind,
    )


def resolve_endpoint_bindings(
    *,
    connections: Any,
    nodes: Any,
    snapshot: SymbolGeometrySnapshot,
) -> tuple[ResolvedEndpointBinding, ...]:
    """One binding per connection endpoint, sorted so the result is a canonical sequence.

    ``connections`` are the plan's connections and ``nodes`` its nodes; both are read for their
    identities, and the symbol geometry comes from the snapshot rather than from the registry --
    a binding that consulted the live catalogue could disagree with the digest that claims to
    describe the drawing.
    """

    nodes_by_id = {node.engineering_id: node for node in nodes}
    facts: dict[str, SymbolGeometryFact] = {}
    for node in nodes:
        facts[node.engineering_id] = require_node_symbol(node, snapshot)

    bindings: list[ResolvedEndpointBinding] = []
    for connection in connections:
        for role, node_field, port_field in (
            ("source", "source_engineering_id", "source_port_id"),
            ("target", "target_engineering_id", "target_port_id"),
        ):
            node_id = getattr(connection, node_field)
            node = nodes_by_id.get(node_id)
            if node is None:
                raise PortBindingError(
                    "port_not_found",
                    f"connection {connection.connection_id!r} {role} names node {node_id!r}, "
                    "which the plan does not place: a route to nowhere is not a route",
                    connection_id=connection.connection_id,
                    role=role,
                )
            bindings.append(
                bind_endpoint(
                    connection_id=connection.connection_id,
                    role=role,
                    node_id=node_id,
                    fact=facts[node_id],
                    declared_port_id=str(getattr(connection, port_field) or ""),
                    node_kind=node.kind,
                )
            )
    return tuple(sorted(bindings, key=lambda binding: binding.key))


def binding_index(
    bindings: tuple[ResolvedEndpointBinding, ...],
) -> dict[tuple[str, str], ResolvedEndpointBinding]:
    return {binding.key: binding for binding in bindings}


def symbol_key_of(node: Any) -> str:
    """Re-exported for callers that want the declaration rule without importing two modules."""

    return declared_symbol_key(node)
