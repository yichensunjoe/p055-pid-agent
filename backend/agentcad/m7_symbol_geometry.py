"""M7-2 phase 2B step 3: the frozen symbol geometry a layout is placed against.

Step 2 placed nodes from the engine's own rules, and said so. Real bounds arrive here, which
creates a new way for a drawing to move between two runs: the catalogue itself. If a symbol's
bounding geometry changes and the engine is unchanged, the same topology under the same rules
can legitimately produce a different drawing -- so *which* symbol geometry was used has to be
part of the layout's identity. That is what :class:`SymbolGeometrySnapshot` is: an immutable,
digestible record of the geometry facts this drawing depends on.

The line this module must not cross:

* the **catalogue** owns what a symbol *is* -- its renderer geometry, its intrinsic size, its
  scale constraint, and where its ports sit;
* the **layout engine** owns what an instance *is* -- how large it is drawn, where it sits, which
  rank and band it belongs to, and how the route reaches it.

So the snapshot carries no ``x``, no ``y``, no rank, no canvas, no route. A snapshot that carried
a position would be a second placement authority, which is the defect this milestone exists to
remove, wearing a third hat.

Two choices worth stating because they are choices:

* **The digest covers this layout's closure, not the catalogue.** Placing a drawing that uses
  seventeen symbols must not change identity because someone added a valve definition somewhere
  else. Closure is complete in the other direction: every symbol key the plan needs is in it, and
  a key the catalogue does not define is a hard failure *before* routing rather than a quiet
  fall back to step 2's rule sizes.
* **Port anchors are normalized.** The engine may draw an instance larger or smaller than the
  catalogue's nominal size, so an absolute anchor would only be correct at one size. A hidden
  (suppressed) catalogue key is accepted here: whether a symbol is *visible to the model* is a
  catalogue-audit question, while whether its geometry *exists* is what this step needs, and
  refusing a key the topology already names would only hide the drawing behind a visibility
  question.

An out-of-bounds port anchor is deliberately not judged here: the contract does not claim anchors
must sit inside the bounds, and inventing that rule in the runtime would be a second source of
truth about geometry. What this module does guarantee is that every anchor is finite and
quantized to the declared coordinate quantum.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .m7_layout_contract import (
    EQUIPMENT_SYMBOL_CATEGORIES_ARE_THE_REST,
    INSTRUMENT_SYMBOL_CATEGORY,
    LAYOUT_COORDINATE_DECIMALS,
    SYMBOL_GEOMETRY_CATALOG_DIGEST_VERSION,
    SYMBOL_GEOMETRY_FACT_EXCLUSIONS,
    SYMBOL_GEOMETRY_FACT_FIELDS,
    SYMBOL_GEOMETRY_PORT_FIELDS,
    SYMBOL_KEY_FIELD,
    SYMBOL_SCALE_CONSTRAINT_DEFAULT,
    SYMBOL_SCALE_CONSTRAINT_METADATA_KEY,
    SYMBOL_SCALE_CONSTRAINTS,
)
from .symbols import SymbolRegistry


class SymbolGeometryError(ValueError):
    """The catalogue cannot answer a question the layout asked of it."""

    code = "symbol_geometry_error"

    def __init__(self, message: str, *, symbol_key: str = "") -> None:
        self.symbol_key = symbol_key
        super().__init__(message)


class MissingSymbolGeometryError(SymbolGeometryError):
    """A symbol key the frozen catalogue does not define.

    Hard failure, not a fallback. Falling back to step 2's rule sizes would draw the node as
    *something* while the drawing claims to be made of catalogue symbols -- an invisible
    substitution, which is the failure the grouping-intent ruling rejected one step earlier.
    """

    code = "symbol_geometry_missing"


class UnknownSymbolScaleConstraintError(SymbolGeometryError):
    """A declared scale constraint outside the vocabulary. Defaulting would invent a rule."""

    code = "unknown_symbol_scale_constraint"


class DuplicateSymbolPortError(SymbolGeometryError):
    """Two ports with one id: an anchor reference would be ambiguous, not merely imprecise."""

    code = "duplicate_symbol_port_id"


class EntitySymbolResolutionError(SymbolGeometryError):
    """An entity whose ``symbol_key`` is missing, or whose symbol it may not use."""

    code = "entity_symbol_key_unresolved"


class EntityKindIncompatibleError(SymbolGeometryError):
    """An instrument bound to a non-instrument symbol (or the other way round)."""

    code = "entity_kind_incompatible"


class SymbolNotRenderableError(SymbolGeometryError):
    """A symbol the catalogue defines without any renderer geometry to draw."""

    code = "symbol_not_renderable"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _quantize(value: float) -> float:
    rounded = round(value, LAYOUT_COORDINATE_DECIMALS)
    return 0.0 if rounded == 0 else rounded


def _finite(value: float, *, symbol_key: str, what: str) -> float:
    if value != value or value in (float("inf"), float("-inf")):
        raise SymbolGeometryError(
            f"symbol {symbol_key!r} declares a non-finite {what}: a coordinate that is not a "
            "number cannot be placed deterministically",
            symbol_key=symbol_key,
        )
    return value


@dataclass(frozen=True)
class SymbolPortGeometry:
    """One port, as the catalogue states it: identity, semantics, and a normalized anchor."""

    port_id: str
    direction: str
    medium: str
    normalized_x: float
    normalized_y: float

    def to_projection(self) -> dict[str, Any]:
        return {
            "port_id": self.port_id,
            "direction": self.direction,
            "medium": self.medium,
            "normalized_x": self.normalized_x,
            "normalized_y": self.normalized_y,
        }

    def anchor(self, width: float, height: float) -> tuple[float, float]:
        """The port's absolute position on an instance of the given size.

        The engine owns the instance size and asks the fact where the port lands; the fact never
        asks the engine where the instance is.
        """

        return (_quantize(self.normalized_x * width), _quantize(self.normalized_y * height))


@dataclass(frozen=True)
class SymbolGeometryFact:
    """Everything the renderer needs to draw a symbol, and nothing about a drawn instance."""

    symbol_key: str
    renderer_geometry_identity: str
    #: Whether the catalogue declares any renderer geometry at all. A catalogue entry without
    #: shapes is a placeable-looking key that cannot be drawn, so the fact says so rather than
    #: leaving the caller to discover it while routing.
    renderer_supported: bool
    intrinsic_width: float
    intrinsic_height: float
    scale_constraint: str
    #: The placement kinds this symbol may serve, derived from its catalogue category by the
    #: declared compatibility rule. Carried as a fact so a node can be checked against the
    #: *frozen* catalogue it was placed from, not against whatever the registry says later.
    entity_kinds: tuple[str, ...] = ()
    ports: tuple[SymbolPortGeometry, ...] = ()

    @property
    def aspect_ratio(self) -> float:
        return self.intrinsic_width / self.intrinsic_height

    def port(self, port_id: str) -> SymbolPortGeometry | None:
        for candidate in self.ports:
            if candidate.port_id == port_id:
                return candidate
        return None

    def ports_with_direction(self, direction: str) -> tuple[SymbolPortGeometry, ...]:
        """Every port whose declared direction matches, in port id order.

        Sorted because the caller may pick the first of these as the anchor for a connection that
        named no port, and "the first one the catalogue happened to list" is not a rule.
        """

        return tuple(
            sorted(
                (port for port in self.ports if port.direction == direction),
                key=lambda port: port.port_id,
            )
        )

    def serves(self, kind: str) -> bool:
        return kind in self.entity_kinds

    def to_projection(self) -> dict[str, Any]:
        return {
            "symbol_key": self.symbol_key,
            "renderer_geometry_identity": self.renderer_geometry_identity,
            "renderer_supported": self.renderer_supported,
            "intrinsic_width": self.intrinsic_width,
            "intrinsic_height": self.intrinsic_height,
            "scale_constraint": self.scale_constraint,
            "entity_kinds": list(self.entity_kinds),
            "ports": [port.to_projection() for port in self.ports],
        }


@dataclass(frozen=True)
class SymbolGeometrySnapshot:
    """The immutable geometry closure one layout depends on.

    Frozen at construction, so nothing can consult the live registry halfway through routing and
    get a different answer than the digest promised. That is the whole point of freezing rather
    than querying: identity that can change while it is being used is not identity.
    """

    facts: tuple[SymbolGeometryFact, ...]
    digest_version: str = SYMBOL_GEOMETRY_CATALOG_DIGEST_VERSION

    @property
    def closure(self) -> tuple[str, ...]:
        return tuple(fact.symbol_key for fact in self.facts)

    def fact(self, symbol_key: str) -> SymbolGeometryFact | None:
        for candidate in self.facts:
            if candidate.symbol_key == symbol_key:
                return candidate
        return None

    def require(self, symbol_key: str) -> SymbolGeometryFact:
        """The fact for a key, or the refusal that says which symbol geometry is missing."""

        found = self.fact(symbol_key)
        if found is None:
            raise MissingSymbolGeometryError(
                f"the layout depends on symbol {symbol_key!r}, which the frozen snapshot "
                f"({self.digest_version}) does not cover; it was frozen over {list(self.closure)}",
                symbol_key=symbol_key,
            )
        return found

    def to_projection(self) -> dict[str, Any]:
        return {
            "digest_version": self.digest_version,
            "closure": list(self.closure),
            "facts": [fact.to_projection() for fact in self.facts],
        }

    @property
    def digest(self) -> str:
        return symbol_geometry_catalog_digest(self.facts, self.digest_version)


def symbol_geometry_catalog_digest(
    facts: Iterable[SymbolGeometryFact],
    digest_version: str = SYMBOL_GEOMETRY_CATALOG_DIGEST_VERSION,
) -> str:
    """The closure's identity: same facts, same digest, whatever order they were read in."""

    rows = sorted((fact.to_projection() for fact in facts), key=lambda row: row["symbol_key"])
    return _digest(
        {
            "symbol_geometry_catalog_digest_version": digest_version,
            "closure": [row["symbol_key"] for row in rows],
            "facts": rows,
        }
    )


def _scale_constraint(symbol: Any) -> str:
    declared = symbol.metadata.get(SYMBOL_SCALE_CONSTRAINT_METADATA_KEY)
    if declared is None:
        return SYMBOL_SCALE_CONSTRAINT_DEFAULT
    if declared not in SYMBOL_SCALE_CONSTRAINTS:
        raise UnknownSymbolScaleConstraintError(
            f"symbol {symbol.key!r} declares scale constraint {declared!r}, which is not one of "
            f"{list(SYMBOL_SCALE_CONSTRAINTS)}: an unrecognised constraint is a hard failure "
            "rather than a default",
            symbol_key=symbol.key,
        )
    return str(declared)


def _entity_kinds(category: str) -> tuple[str, ...]:
    """The placement kinds a catalogue category may serve, by the declared compatibility rule."""

    if category == INSTRUMENT_SYMBOL_CATEGORY:
        return ("instrument",)
    if EQUIPMENT_SYMBOL_CATEGORIES_ARE_THE_REST:
        return ("equipment",)
    return ()


def _port_geometry(symbol: Any) -> tuple[SymbolPortGeometry, ...]:
    seen: set[str] = set()
    ports: list[SymbolPortGeometry] = []
    for port in symbol.ports:
        if port.id in seen:
            raise DuplicateSymbolPortError(
                f"symbol {symbol.key!r} defines port {port.id!r} more than once: an anchor "
                "reference would be ambiguous rather than merely imprecise",
                symbol_key=symbol.key,
            )
        seen.add(port.id)
        width = _finite(float(symbol.width), symbol_key=symbol.key, what="width")
        height = _finite(float(symbol.height), symbol_key=symbol.key, what="height")
        if width <= 0 or height <= 0:
            raise SymbolGeometryError(
                f"symbol {symbol.key!r} declares a non-positive intrinsic size "
                f"({width}, {height})",
                symbol_key=symbol.key,
            )
        ports.append(
            SymbolPortGeometry(
                port_id=port.id,
                direction=port.direction,
                medium=port.medium,
                normalized_x=_quantize(
                    _finite(float(port.x), symbol_key=symbol.key, what=f"port {port.id!r} x")
                    / width
                ),
                normalized_y=_quantize(
                    _finite(float(port.y), symbol_key=symbol.key, what=f"port {port.id!r} y")
                    / height
                ),
            )
        )
    return tuple(sorted(ports, key=lambda item: item.port_id))


def freeze_symbol_geometry(
    symbol_keys: Iterable[str], *, registry: SymbolRegistry | None = None
) -> SymbolGeometrySnapshot:
    """Read the catalogue once and freeze the facts for exactly these keys.

    The closure is the input, not the catalogue: two drawings that use different symbols get
    different digests, and an edit to a symbol neither of them uses changes neither digest.
    """

    keys = sorted({key for key in symbol_keys if key})
    catalog = registry if registry is not None else SymbolRegistry()
    facts: list[SymbolGeometryFact] = []
    for symbol_key in keys:
        if not catalog.exists(symbol_key):
            raise MissingSymbolGeometryError(
                f"the layout depends on symbol {symbol_key!r}, which the catalogue does not "
                "define; there is no geometry to place against and no rule size to fall back to",
                symbol_key=symbol_key,
            )
        symbol = catalog.get(symbol_key)
        facts.append(
            SymbolGeometryFact(
                symbol_key=symbol.key,
                renderer_geometry_identity=_digest(symbol.shapes),
                renderer_supported=bool(symbol.shapes),
                intrinsic_width=_quantize(
                    _finite(float(symbol.width), symbol_key=symbol.key, what="width")
                ),
                intrinsic_height=_quantize(
                    _finite(float(symbol.height), symbol_key=symbol.key, what="height")
                ),
                scale_constraint=_scale_constraint(symbol),
                entity_kinds=_entity_kinds(str(symbol.category)),
                ports=_port_geometry(symbol),
            )
        )
    return SymbolGeometrySnapshot(facts=tuple(facts))


def symbol_key_field() -> str:
    """The declared field that carries a node's symbol key. Read from the contract, not assumed."""

    return SYMBOL_KEY_FIELD


def declared_symbol_key(node: Any) -> str:
    """The catalogue key a node names, or the refusal that says why it does not name one.

    The field is explicit, so a missing value is a specification defect rather than an
    opportunity to infer: inferring from the engineering class would place the node as whatever
    that class usually looks like, which is not the same statement as "draw it this way".
    """

    value = str(getattr(node, SYMBOL_KEY_FIELD, "") or "").strip()
    if not value:
        raise EntitySymbolResolutionError(
            f"node {node.engineering_id!r} of kind {node.kind!r} declares no {SYMBOL_KEY_FIELD!r}: "
            "without it there is no symbol to place, and inferring one from the engineering "
            "class would draw a different statement than the one that was made",
        )
    return value


def require_node_symbol(node: Any, snapshot: SymbolGeometrySnapshot) -> SymbolGeometryFact:
    """The frozen fact a node is to be drawn from, with all three requirements checked.

    Every requirement comes from the snapshot rather than the live registry: a node must be
    checked against the catalogue it was frozen from, or "the drawing is reproducible" would
    depend on when it was re-run.
    """

    symbol_key = declared_symbol_key(node)
    fact = snapshot.require(symbol_key)
    if not fact.renderer_supported:
        raise SymbolNotRenderableError(
            f"node {node.engineering_id!r} is bound to symbol {symbol_key!r}, which the catalogue "
            "defines without any renderer geometry: there is nothing to place",
            symbol_key=symbol_key,
        )
    if not fact.serves(node.kind):
        raise EntityKindIncompatibleError(
            f"node {node.engineering_id!r} of kind {node.kind!r} is bound to symbol "
            f"{symbol_key!r}, which serves {list(fact.entity_kinds)}: drawing an instrument as "
            "a vessel is a wrong drawing even though both are renderable symbols",
            symbol_key=symbol_key,
        )
    return fact


def symbol_closure_for_kinds(nodes: Iterable[Any]) -> tuple[str, ...]:
    """The closure a set of nodes implies: exactly the keys they name, sorted and deduplicated."""

    return tuple(sorted({declared_symbol_key(node) for node in nodes}))


#: The names this module promises not to be about. Kept as data so the test that walks the
#: snapshot projection can assert it, rather than trusting the docstring.
SNAPSHOT_EXCLUDED_NAMES: tuple[str, ...] = tuple(SYMBOL_GEOMETRY_FACT_EXCLUSIONS)
SNAPSHOT_FACT_FIELDS: tuple[str, ...] = tuple(SYMBOL_GEOMETRY_FACT_FIELDS)
SNAPSHOT_PORT_FIELDS: tuple[str, ...] = tuple(SYMBOL_GEOMETRY_PORT_FIELDS)

#: Recomputed here from the contract, so an accidental second copy of the constant cannot drift.
SNAPSHOT_DEFAULT_SCALE_CONSTRAINT = SYMBOL_SCALE_CONSTRAINT_DEFAULT
