"""What a repair is allowed to touch, and what must not move while it does (baseline §C).

The baseline's rule is that locality is *mechanical*, not a matter of taste: before
planning, the runner freezes the set of element ids a repair may modify, plus two
projections of everything outside that set. A candidate that changes a protected projection
is rejected even if it made every validator green, because "the validator is happy" and
"only the intended thing changed" are different claims and a drawing release needs both.

The scope default is the 1-hop engineering neighbourhood named in §C1: the target elements,
the connectors incident to them, and the elements at the other end of those connectors. The
opposite endpoint is included because a reconnection legitimately has to touch both ends of
a line; anything beyond that must be declared by the case, not discovered by the planner.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from .engineering_ir import build_engineering_graph
from .models import Document, Element
from .repair_models import (
    RepairFindingRef,
    RepairProtectedHashes,
    RepairScope,
)
from .symbols import SymbolRegistry

#: How close two things have to be, in grid units, before a repair is allowed to treat them as
#: neighbours. One constant, used by the scope *and* by the planner, because a scope rule and a
#: planner rule that disagree about what "local" means would fail every edge case twice.
LOCAL_BINDING_RADIUS = 8.0

#: Baseline §C2 suggests these ceilings by case class. They are defaults, not limits the
#: planner may raise: the case spec decides, and M5-0 freezes the value into the spec
#: fingerprint.
TOUCHED_BUDGET_BY_CLASS: dict[str, int] = {
    "simple_metadata": 6,
    "simple_endpoint": 6,
    "multi_connector": 16,
    "local_geometry": 16,
    "complex_collision": 32,
    "large_drawing": 64,
}


def element_map(document: Document) -> dict[str, Element]:
    return {element.id: element for element in document.elements}


def connector_endpoint_element_ids(element: Element) -> set[str]:
    """The element ids a connector is bound to, ignoring free endpoints."""

    if element.type != "connector":
        return set()
    ids: set[str] = set()
    for endpoint in (element.source, element.target):
        if endpoint is not None and endpoint.element_id:
            ids.add(endpoint.element_id)
    return ids


def incident_connector_ids(document: Document, element_id: str) -> set[str]:
    return {
        element.id
        for element in document.elements
        if element.type == "connector"
        and element_id in connector_endpoint_element_ids(element)
    }


def anchor_points(element: Element) -> list[tuple[float, float]]:
    """Where an element physically *is*, for locality purposes: no symbol catalogue needed.

    A connector is its two ends, because that is where it can be re-bound. Anything with a
    footprint is its bounding box corners plus its centre, which is enough to answer "is this
    thing next to that thing" without loading port definitions into the scope computation.
    """

    if element.type == "connector":
        return [
            (element.points[0].x, element.points[0].y),
            (element.points[-1].x, element.points[-1].y),
        ]
    position = getattr(element, "position", None)
    if position is None:
        return []
    width = float(getattr(element, "width", 0) or 0)
    height = float(getattr(element, "height", 0) or 0)
    return [
        (position.x, position.y),
        (position.x + width, position.y),
        (position.x, position.y + height),
        (position.x + width, position.y + height),
        (position.x + width / 2, position.y + height / 2),
    ]


def geometric_neighbours(
    document: Document, anchor_ids: Iterable[str], *, radius: float
) -> set[str]:
    """Elements physically within ``radius`` of the anchors.

    This is what makes a repair region coherent. A connector that was detached from the
    equipment it used to join is, topologically, connected to nothing — but it is still lying
    right next to the port it belonged to, and that is the evidence a local repair is entitled
    to use. Purely graph-shaped scopes miss it, and every dangling-endpoint repair then has to
    invent its own replacement instead of reconnecting to what is already there.
    """

    anchors: list[tuple[float, float]] = []
    elements = element_map(document)
    for element_id in sorted(set(anchor_ids)):
        element = elements.get(element_id)
        if element is not None:
            anchors.extend(anchor_points(element))
    if not anchors:
        return set()
    neighbours: set[str] = set()
    for element in document.elements:
        for point in anchor_points(element):
            if any(_distance(point, anchor) <= radius for anchor in anchors):
                neighbours.add(element.id)
                break
    return neighbours


def _distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return ((first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2) ** 0.5


def engineering_neighborhood(
    document: Document, seed_element_ids: Iterable[str], *, hop: int = 1
) -> set[str]:
    """The 1-hop (or explicitly declared 2-hop) neighbourhood around the target.

    Two rules compose here, and both are load-bearing:

    * a connector seed counts as being *at* both of its endpoints, so a line defect drags in
      the equipment it joins;
    * every hop also closes over the geometric neighbourhood, so equipment that the *finding*
      involves but the *topology* no longer links is still inside the repair region.

    Hop expansion is deterministic and monotone.
    """

    elements = element_map(document)
    known = set(elements)
    radius = max(5.0, document.canvas.grid_size) * LOCAL_BINDING_RADIUS
    frontier = {element_id for element_id in seed_element_ids if element_id in known}
    for element_id in sorted(frontier):
        element = elements[element_id]
        if element.type == "connector":
            frontier |= connector_endpoint_element_ids(element) & known
    reachable = set(frontier)
    for _ in range(max(hop, 0)):
        step: set[str] = set()
        for element_id in sorted(frontier):
            for connector_id in incident_connector_ids(document, element_id):
                step.add(connector_id)
                step |= connector_endpoint_element_ids(elements[connector_id])
        step |= geometric_neighbours(document, reachable, radius=radius)
        step &= known
        new = step - reachable
        if not new:
            break
        reachable |= new
        frontier = new
    return reachable


def object_ids_for_elements(
    document: Document, registry: SymbolRegistry, element_ids: Iterable[str]
) -> list[str]:
    wanted = set(element_ids)
    graph = build_engineering_graph(document, registry)
    return sorted(
        {
            obj.engineering_id
            for obj in graph.objects
            if wanted & set(obj.element_ids) or obj.primary_element_id in wanted
        }
    )


def derive_scope(
    document: Document,
    registry: SymbolRegistry,
    finding: RepairFindingRef,
    *,
    hop: int = 1,
    max_touched_existing_ids: int = TOUCHED_BUDGET_BY_CLASS["multi_connector"],
    declared_by: str = "derived",
    permits_creation: bool = False,
    max_created_ids: int = 0,
    permits_deletion: bool = False,
    max_deleted_ids: int = 0,
) -> RepairScope:
    """Freeze the allowed set for one finding before the planner sees anything."""

    seeds = set(finding.element_ids)
    if not seeds and finding.object_ids:
        graph = build_engineering_graph(document, registry)
        for obj in graph.objects:
            if obj.engineering_id in set(finding.object_ids):
                seeds |= set(obj.element_ids)
                seeds.add(obj.primary_element_id)
    allowed_elements = engineering_neighborhood(document, seeds, hop=hop)
    return RepairScope(
        hop=2 if hop >= 2 else 1,
        allowed_element_ids=sorted(allowed_elements),
        allowed_object_ids=object_ids_for_elements(document, registry, allowed_elements),
        max_touched_existing_ids=max_touched_existing_ids,
        declared_by=declared_by,
        permits_creation=permits_creation,
        max_created_ids=max_created_ids,
        permits_deletion=permits_deletion,
        max_deleted_ids=max_deleted_ids,
    )


def extend_scope(
    scope: RepairScope,
    document: Document,
    registry: SymbolRegistry,
    declared_element_ids: Iterable[str],
) -> RepairScope:
    """Widen a derived scope with the ids a case declares (baseline §C1).

    §C1 says extra reach "must be declared by the case rather than discovered by the planner",
    which makes the derived neighbourhood a floor, not a starting offer: a case may name ids it
    wants inside the region, but naming them may not silently evict the ones the derivation had
    already justified. Replacing instead of widening lets a case spec shrink the region until a
    sprawling repair looks local, which is the one thing the scope exists to prevent.

    Ids the drawing does not contain are dropped rather than trusted: a scope is a claim about
    *this* document, and an id that is not in it cannot be part of that claim.
    """

    known = element_map(document)
    declared = {element_id for element_id in declared_element_ids if element_id in known}
    allowed = sorted(set(scope.allowed_element_ids) | declared)
    if allowed == scope.allowed_element_ids:
        return scope
    return scope.model_copy(
        update={
            "allowed_element_ids": allowed,
            "allowed_object_ids": object_ids_for_elements(document, registry, allowed),
        }
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def element_projection_hash(document: Document, element_ids: Iterable[str]) -> str:
    """Hash of a set of drawing elements, in id order, with no incidental formatting."""

    wanted = set(element_ids)
    payload = [
        element.model_dump(mode="json")
        for element in sorted(document.elements, key=lambda item: item.id)
        if element.id in wanted
    ]
    return _sha(_canonical(payload))


def drawing_projection_hash(document: Document, *, exclude_element_ids: Iterable[str]) -> str:
    """Hash of every drawing element *outside* the excluded set (baseline §C3)."""

    excluded = set(exclude_element_ids)
    return element_projection_hash(
        document,
        [element.id for element in document.elements if element.id not in excluded],
    )


def engineering_projection_hash(
    document: Document, registry: SymbolRegistry, *, exclude_element_ids: Iterable[str]
) -> str:
    """Hash of the engineering graph as seen from outside the scope.

    Objects that touch the scope are dropped entirely rather than partially rewritten: an
    object is either inside the region being repaired or part of the protected evidence, and
    pretending a half-touched object is "protected" would hide exactly the collateral damage
    this hash exists to catch.

    Edges *carried by* an in-scope connector are dropped as well, for the same reason. An edge
    is not a reference to a line, it *is* the line's engineering state — it carries the medium,
    the diameter and the flow direction — so keeping a repaired connector's edge in the
    protected projection would make every legitimate line repair look like a violation.
    """

    excluded = set(exclude_element_ids)
    graph = build_engineering_graph(document, registry)
    objects = [
        obj.model_dump(mode="json")
        for obj in sorted(graph.objects, key=lambda item: item.engineering_id)
        if not (excluded & set(obj.element_ids)) and obj.primary_element_id not in excluded
    ]
    remaining = {obj["engineering_id"] for obj in objects}
    edges = [
        edge.model_dump(mode="json")
        for edge in sorted(graph.edges, key=lambda item: item.connector_id)
        if edge.connector_id not in excluded
        and edge.source_engineering_id in remaining
        and edge.target_engineering_id in remaining
    ]
    return _sha(_canonical({"objects": objects, "edges": edges}))


def scope_fingerprint(scope: RepairScope) -> str:
    """Stable identity of the frozen scope, published so a reviewer can recompute it."""

    return _sha(
        _canonical(
            {
                "hop": scope.hop,
                "allowed_element_ids": sorted(scope.allowed_element_ids),
                "allowed_object_ids": sorted(scope.allowed_object_ids),
                "max_touched_existing_ids": scope.max_touched_existing_ids,
                "permits_creation": scope.permits_creation,
                "max_created_ids": scope.max_created_ids,
                "permits_deletion": scope.permits_deletion,
                "max_deleted_ids": scope.max_deleted_ids,
                "declared_by": scope.declared_by,
            }
        )
    )


def protected_hashes(
    document: Document,
    registry: SymbolRegistry,
    scope: RepairScope,
    *,
    rule_bundle_fingerprint: str,
    created_ids: Iterable[str] = (),
) -> RepairProtectedHashes:
    """The pre/post comparison basis for locality (baseline §C3).

    ``created_ids`` belongs to the region rather than to the protected evidence, and has to be
    named explicitly because a created element cannot be in ``scope.allowed_element_ids`` — the
    scope is frozen before the element exists. Leaving it out would make every permitted
    creation read as "a protected projection changed", which would turn the case's create
    policy into a switch that can never be turned on. What polices a creation is that policy
    (baseline §B5), not this hash.
    """

    excluded = sorted(set(scope.allowed_element_ids) | set(created_ids))
    return RepairProtectedHashes(
        engineering_projection=engineering_projection_hash(
            document, registry, exclude_element_ids=excluded
        ),
        drawing_projection=drawing_projection_hash(document, exclude_element_ids=excluded),
        rule_bundle_fingerprint=rule_bundle_fingerprint,
        scope_fingerprint=scope_fingerprint(scope),
    )


def changed_existing_ids(before: Document, after: Document) -> list[str]:
    """Existing element ids whose canonical content changed, sorted.

    Locators that only appear in one of the two documents are reported as well, because a
    created or deleted id is also a change to the drawing — the caller decides whether the
    case permitted it.
    """

    before_map = element_map(before)
    after_map = element_map(after)
    changed = {
        element_id
        for element_id in set(before_map) | set(after_map)
        if _canonical(before_map[element_id].model_dump(mode="json") if element_id in before_map else None)
        != _canonical(after_map[element_id].model_dump(mode="json") if element_id in after_map else None)
    }
    return sorted(changed)


def touched_existing_ids(before: Document, after: Document) -> list[str]:
    """Ids present in both documents whose content changed (the touched-budget measure)."""

    before_map = element_map(before)
    after_map = element_map(after)
    return sorted(
        element_id
        for element_id in set(before_map) & set(after_map)
        if _canonical(before_map[element_id].model_dump(mode="json"))
        != _canonical(after_map[element_id].model_dump(mode="json"))
    )


def created_ids(before: Document, after: Document) -> list[str]:
    return sorted(set(element_map(after)) - set(element_map(before)))


def change_summary(before: Document, after: Document) -> dict[str, list[str]]:
    """The four change measures a repair report is made of, computed one way.

    ``touched`` counts only ids that existed before *and* changed, which is what the touched
    budget is about; ``changed`` is every id whose content differs, including the ones that
    appeared or disappeared. Keeping these four in one place is what stops a report and a gate
    from disagreeing about the same repair.
    """

    return {
        "created": created_ids(before, after),
        "deleted": deleted_ids(before, after),
        "touched": touched_existing_ids(before, after),
        "changed": changed_existing_ids(before, after),
    }


def deleted_ids(before: Document, after: Document) -> list[str]:
    return sorted(set(element_map(before)) - set(element_map(after)))


__all__ = [
    "LOCAL_BINDING_RADIUS",
    "TOUCHED_BUDGET_BY_CLASS",
    "anchor_points",
    "change_summary",
    "changed_existing_ids",
    "geometric_neighbours",
    "connector_endpoint_element_ids",
    "created_ids",
    "deleted_ids",
    "derive_scope",
    "drawing_projection_hash",
    "element_map",
    "element_projection_hash",
    "engineering_neighborhood",
    "extend_scope",
    "engineering_projection_hash",
    "incident_connector_ids",
    "object_ids_for_elements",
    "protected_hashes",
    "scope_fingerprint",
    "touched_existing_ids",
]
