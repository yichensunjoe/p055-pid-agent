"""M7-2 phase 2B step 1: the layout engine's semantic-topology ingress.

Phase 2A ended at the engine's door with a :class:`SemanticTopology`. This module is the
door. ``AutoLayoutEngine.layout_semantic_topology`` composes it in, so the engine that has
always placed things gains exactly one new way in -- and the topology arrives *as* topology,
never disguised as a document with imagined coordinates. Handing the old
document-shaped entry point a node at ``(0, 0)`` would move coordinate authority from the
model to the adapter, which is the same defect wearing a different hat.

What step 1 does:

* **Takes the topology, and only the topology.** A raw payload or a ``Document`` is refused
  by type rather than coerced. An engine that quietly adapted a specification would be a
  second adapter with semantic authority, which the contract forbids.
* **Receives every declared intent dimension and says where each one applies.** The
  consumption points come from the phase-2B contract, not from this file, so the engine
  cannot claim a different answer than the one that was reviewed. A dimension the contract
  gave no consumption point is a hard error, not a shrug: "declared but unimplemented" may
  not quietly become "declared but ignored".
* **Names what has not applied yet.** ``pending_intent_dimensions`` is the honest form of
  that rule -- at step 1 the aspect class has been received and not yet enforced, and the
  plan says so instead of looking finished.

What step 1 deliberately does not do: no placement, no routing, no annotation, no canvas.
``placement`` is empty and ``canvas_bounds`` is ``None``. ``preserve_positions`` is not a
parameter of the ingress at all, so no caller can ask for the old behaviour; the plan
carries it as ``False`` because that is the policy the semantic-first path runs under.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any

from .m7_diagram_adapter import SemanticTopology
from .m7_layout_contract import (
    GROUPING_FALLBACKS,
    INTENT_DIMENSIONS_ARE_RECEIVED_AT_STEP,
    LAYOUT_COORDINATE_DECIMALS,
    LAYOUT_DIGEST_INPUTS,
    LAYOUT_INTENT_CONSUMPTION,
    LAYOUT_INTENT_DIMENSIONS,
    M7_INGRESS_HAS_NO_PRESERVE_POSITIONS_PARAMETER,
    M7_SYNTHESIS_INGRESS_PRESERVE_POSITIONS,
    PHASE_2B_STEPS,
    PLACEMENT_PROJECTION_FIELDS,
    PLACEMENT_PROJECTION_VERSION,
    STEP_2_PLACEMENT_KINDS,
)

#: Published by the engine, not by the contract: a rules change is an engine edit that carries
#: a visible version, and the digest reads both through these names.
LAYOUT_ENGINE_VERSION = "auto_layout/1"
LAYOUT_RULES_VERSION = "deterministic-layout-rules/1"

#: The plan's own digest version, so a plan identity cannot be confused with the canonical
#: layout digest that step 5 will produce. Deliberately absent from ``LAYOUT_DIGEST_INPUTS``.
#:
#: v2: the plan gained ``node_kinds`` and ``flow_edges``, so the step-1 field set changed. The
#: field set a version names is part of what the version means -- adding fields under v1 would
#: have been a different plan wearing the same name, which is the failure this milestone is
#: about, one layer down.
SEMANTIC_LAYOUT_PLAN_DIGEST_VERSION = "m7-semantic-layout-plan-digest/2"

#: One rules version governs the spacing policy, the node sizes and the rank rules. A separate
#: spacing-policy version would be a second source of truth about the same drawing, which is
#: only worth paying for once the rules are large enough to need independent lifecycles.
DENSITY_SPACING_POLICY_IS_UNDER_THE_LAYOUT_RULES_VERSION = True


@dataclass(frozen=True)
class SpacingPolicy:
    """The numbers a density *class* maps to. Never stated by the model, only by the engine."""

    rank_gap: float
    node_gap: float
    system_gap: float
    component_gap: float

    def to_projection(self) -> dict[str, float]:
        return {
            "rank_gap": self.rank_gap,
            "node_gap": self.node_gap,
            "system_gap": self.system_gap,
            "component_gap": self.component_gap,
        }


#: `compact` is a class the model may ask for; these four numbers are engine facts under
#: ``LAYOUT_RULES_VERSION``. Changing any of them is a layout-rules semantic change and must
#: bump that version -- which is why they are not separately versioned and not in the digest
#: as separate inputs.
DENSITY_SPACING_POLICY: tuple[tuple[str, SpacingPolicy], ...] = (
    (
        "compact",
        SpacingPolicy(rank_gap=150.0, node_gap=90.0, system_gap=200.0, component_gap=120.0),
    ),
    (
        "comfortable",
        SpacingPolicy(rank_gap=220.0, node_gap=130.0, system_gap=280.0, component_gap=180.0),
    ),
)


@dataclass(frozen=True)
class NodeSize:
    width: float
    height: float


#: Symbol-accurate bounds arrive with routing, which is the first step that has to leave room
#: for something. Until then the engine sizes a node from its own rules, and says so.
NODE_SIZE_POLICY: tuple[tuple[str, NodeSize], ...] = (
    ("equipment", NodeSize(width=120.0, height=80.0)),
    ("instrument", NodeSize(width=64.0, height=48.0)),
)


#: Step 1, by the name the contract gives it. Named by its declaration rather than by its
#: position, so reordering the steps is a loud failure here instead of a silent shift.
INGRESS_STEP = next(
    key
    for key, value in PHASE_2B_STEPS
    if value == "semantic_topology_ingress_and_intent_resolution"
)

#: Step 2, by the name the contract gives it.
STEP_2 = next(
    key for key, value in PHASE_2B_STEPS if value == "deterministic_rank_and_absolute_placement"
)


class SemanticTopologyIngressError(ValueError):
    """The ingress was handed something that is not the engine-facing input contract."""


class UnknownDensityError(SemanticTopologyIngressError):
    """A density class the rules do not map. Hard failure: defaulting would silently choose."""


class UnknownPlacementKindError(SemanticTopologyIngressError):
    """A node whose kind has no declared size. Hard failure, for the same reason."""


def _step_index(step: str) -> int:
    keys = tuple(key for key, _ in PHASE_2B_STEPS)
    if step not in keys:
        raise SemanticTopologyIngressError(
            f"{step!r} is not a declared phase-2B step {keys}; the engine integration is "
            "built in a declared order rather than in whatever order is convenient"
        )
    return keys.index(step)


@dataclass(frozen=True)
class IntentDimensionUse:
    """Where one intent dimension enters the engine and where it changes the drawing."""

    dimension: str
    received_at_step: str
    applied_at_step: str
    behaviour: str

    def to_projection(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "received_at_step": self.received_at_step,
            "applied_at_step": self.applied_at_step,
        }


@dataclass(frozen=True)
class SemanticIntentConstraints:
    """The discrete intent as the engine resolved it: classes and orderings, never numbers."""

    orientation: str
    preferred_aspect_class: str
    primary_flow_direction: str
    grouping: str
    density: str
    system_order: tuple[str, ...]

    def to_projection(self) -> dict[str, Any]:
        return {
            "orientation": self.orientation,
            "preferred_aspect_class": self.preferred_aspect_class,
            "primary_flow_direction": self.primary_flow_direction,
            "grouping": self.grouping,
            "density": self.density,
            "system_order": list(self.system_order),
        }


@dataclass(frozen=True)
class SemanticLayoutPlan:
    """What the engine knows after receiving a topology, before it places anything.

    Intermediate by construction: :attr:`placement` is empty and :attr:`canvas_bounds` is
    ``None``, so this cannot be mistaken for a finished layout. Step 2 fills the placement,
    step 4 the canvas, step 5 the canonical digest.
    """

    topology_digest: str
    intent: SemanticIntentConstraints
    intent_uses: tuple[IntentDimensionUse, ...]
    systems_in_reading_order: tuple[str, ...]
    node_ids_by_system: tuple[tuple[str, tuple[str, ...]], ...]
    required_loops: tuple[tuple[str, tuple[str, ...]], ...]
    #: The graph as the engine needs it, geometry-free: who is what kind, and which direction
    #: each connection runs. Step 2 places from these and the topology is never written to.
    node_kinds: tuple[tuple[str, str], ...] = field(default=())
    flow_edges: tuple[tuple[str, str], ...] = field(default=())
    produced_at_step: str = INGRESS_STEP
    engine_version: str = LAYOUT_ENGINE_VERSION
    rules_version: str = LAYOUT_RULES_VERSION
    #: Always ``False`` here. The semantic-first path does not preserve positions, and the
    #: ingress has no parameter through which a caller could ask it to.
    preserve_positions: bool = M7_SYNTHESIS_INGRESS_PRESERVE_POSITIONS
    digest_version: str = SEMANTIC_LAYOUT_PLAN_DIGEST_VERSION
    #: Filled by step 2. Empty is the correct value at step 1, not an oversight.
    placement: tuple[dict[str, Any], ...] = field(default=())
    #: Produced by step 4 from the content bounds. ``None`` until then.
    canvas_bounds: None = None
    #: Set by step 2: the grouping class the engine honoured, and the class it replaced when
    #: the specification cannot yet express what was asked for.
    honoured_grouping: str = ""
    grouping_fallback_from: str | None = None
    spacing: SpacingPolicy | None = None

    def to_projection(self) -> dict[str, Any]:
        return {
            "produced_at_step": self.produced_at_step,
            "topology_digest": self.topology_digest,
            "engine_version": self.engine_version,
            "rules_version": self.rules_version,
            "preserve_positions": self.preserve_positions,
            "intent": self.intent.to_projection(),
            "intent_uses": [use.to_projection() for use in self.intent_uses],
            "systems_in_reading_order": list(self.systems_in_reading_order),
            "node_ids_by_system": [
                [system_id, list(node_ids)] for system_id, node_ids in self.node_ids_by_system
            ],
            "required_loops": [
                [loop_id, list(engineering_ids)] for loop_id, engineering_ids in self.required_loops
            ],
            "node_kinds": [[node_id, kind] for node_id, kind in self.node_kinds],
            "flow_edges": [[source, target] for source, target in self.flow_edges],
            "placement": list(self.placement),
            "canvas_bounds": self.canvas_bounds,
            "honoured_grouping": self.honoured_grouping,
            "grouping_fallback_from": self.grouping_fallback_from,
            "spacing": self.spacing.to_projection() if self.spacing else None,
        }

    @property
    def digest(self) -> str:
        return plan_digest(self)

    @property
    def pending_intent_dimensions(self) -> tuple[str, ...]:
        """The dimensions received here whose effect lands in a later step.

        Non-empty is not a defect: it is the statement that "declared but not yet applied"
        is visible rather than silent. Step 5's gate is where this must be empty for all six
        dimensions.
        """

        produced = _step_index(self.produced_at_step)
        return tuple(
            use.dimension for use in self.intent_uses if _step_index(use.applied_at_step) > produced
        )


def canonical_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def plan_digest(plan: SemanticLayoutPlan) -> str:
    """The plan's identity: same topology and intent, same bytes, no engine run involved."""

    return hashlib.sha256(
        canonical_payload(
            {
                "semantic_layout_plan_digest_version": plan.digest_version,
                "plan": plan.to_projection(),
            }
        ).encode("utf-8")
    ).hexdigest()


def _intent_uses() -> tuple[IntentDimensionUse, ...]:
    """The consumption table, read from the contract -- the engine does not decide it."""

    return tuple(
        IntentDimensionUse(
            dimension=item.dimension,
            received_at_step=item.received_at_step,
            applied_at_step=item.applied_at_step,
            behaviour=item.behaviour,
        )
        for item in LAYOUT_INTENT_CONSUMPTION
    )


def _check_every_dimension_is_consumed(uses: tuple[IntentDimensionUse, ...]) -> None:
    consumed = {use.dimension for use in uses}
    missing = [
        dimension.name for dimension in LAYOUT_INTENT_DIMENSIONS if dimension.name not in consumed
    ]
    if missing:
        raise SemanticTopologyIngressError(
            f"the contract declares no consumption point for {missing}; a declared intent "
            "dimension may not be silently ignored"
        )
    wrong_step = [use.dimension for use in uses if use.received_at_step != INGRESS_STEP]
    if wrong_step:
        raise SemanticTopologyIngressError(
            f"every intent dimension must be received at {INGRESS_STEP!r}, not {wrong_step}"
        )


def plan_semantic_layout(topology: SemanticTopology) -> SemanticLayoutPlan:
    """Turn the engine-facing input contract into the plan step 2 will place from.

    Pure and geometry-free: the same topology always yields the same plan digest, and nothing
    here reads the service, the clock, the environment or the document.
    """

    if not isinstance(topology, SemanticTopology):
        raise SemanticTopologyIngressError(
            "the semantic ingress takes a SemanticTopology, not "
            f"{type(topology).__name__}: a payload belongs to the adapter and a document "
            "belongs to the manual path, and neither may be adapted in here"
        )

    uses = _intent_uses()
    _check_every_dimension_is_consumed(uses)

    intent = SemanticIntentConstraints(
        orientation=topology.intent.orientation,
        preferred_aspect_class=topology.intent.preferred_aspect_class,
        primary_flow_direction=topology.intent.primary_flow_direction,
        grouping=topology.intent.grouping,
        density=topology.intent.density,
        system_order=tuple(topology.intent.system_order),
    )

    # An ordering, not a partition: explicitly named systems first, in the order they were
    # named, then the rest by the order the specification declared. Choosing placement order
    # is step 2; resolving the reading order is what "received at step 1" means.
    declared = set(intent.system_order)
    rest = tuple(
        system.system_id
        for system in sorted(topology.systems, key=lambda item: (item.order, item.system_id))
        if system.system_id not in declared
    )
    reading_order = tuple([*intent.system_order, *rest])

    nodes_by_system: dict[str, list[str]] = {system_id: [] for system_id in reading_order}
    for node in sorted(topology.nodes, key=lambda item: item.engineering_id):
        nodes_by_system.setdefault(node.system_id, []).append(node.engineering_id)

    return SemanticLayoutPlan(
        topology_digest=topology.digest,
        intent=intent,
        intent_uses=uses,
        systems_in_reading_order=reading_order,
        node_ids_by_system=tuple(
            (system_id, tuple(nodes_by_system[system_id])) for system_id in reading_order
        ),
        required_loops=tuple(
            (loop.loop_id, tuple(loop.engineering_ids))
            for loop in sorted(topology.required_loops, key=lambda item: item.loop_id)
        ),
        node_kinds=tuple(
            (node.engineering_id, node.kind)
            for node in sorted(topology.nodes, key=lambda item: item.engineering_id)
        ),
        flow_edges=tuple(
            sorted(
                {
                    (edge.source_engineering_id, edge.target_engineering_id)
                    for edge in topology.edges
                }
            )
        ),
    )


def spacing_policy(density: str) -> SpacingPolicy:
    """The numbers a density class maps to, or a hard failure.

    Defaulting on an unknown class would mean the engine quietly choosing a density the model
    never asked for -- a small version of the same silence this milestone exists to remove.
    """

    for name, policy in DENSITY_SPACING_POLICY:
        if name == density:
            return policy
    raise UnknownDensityError(
        f"no spacing policy for density {density!r}; the rules map "
        f"{[name for name, _ in DENSITY_SPACING_POLICY]}"
    )


def node_size(kind: str) -> NodeSize:
    for name, size in NODE_SIZE_POLICY:
        if name == kind:
            return size
    raise UnknownPlacementKindError(
        f"no declared size for node kind {kind!r}; the rules size "
        f"{[name for name, _ in NODE_SIZE_POLICY]}"
    )


def resolve_grouping(grouping: str) -> tuple[str, str | None]:
    """The grouping class the engine will honour, and the fallback it used, if any.

    A class the specification cannot yet express (`grouped_by_zone`: there are no zones in the
    model) falls back *by declaration* and reports which class was honoured, so "we did
    something else" is visible in the plan rather than inferable from the picture.
    """

    for source, target, _reason in GROUPING_FALLBACKS:
        if source == grouping:
            return target, source
    return grouping, None


def _strongly_connected_components(
    node_ids: tuple[str, ...], edges: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, ...], ...]:
    """Tarjan, iterative and deterministic.

    Iterative because a plant-wide drawing is deep enough that recursion is a real risk, and
    deterministic because the neighbours are visited in sorted order -- a rank that depended on
    dictionary order would make the placement digest a coin toss.
    """

    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for source, target in edges:
        if source in adjacency and target in adjacency:
            adjacency[source].append(target)
    for neighbours in adjacency.values():
        neighbours.sort()

    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[tuple[str, ...]] = []
    counter = 0

    for root in node_ids:
        if root in index_of:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, position = work[-1]
            if position == 0:
                index_of[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            neighbours = adjacency[node]
            descended = False
            while position < len(neighbours):
                neighbour = neighbours[position]
                position += 1
                if neighbour not in index_of:
                    work[-1] = (node, position)
                    work.append((neighbour, 0))
                    descended = True
                    break
                if neighbour in on_stack:
                    low[node] = min(low[node], index_of[neighbour])
            if descended:
                continue
            work[-1] = (node, position)
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(tuple(sorted(component)))
    return tuple(components)


def _ranks(node_ids: tuple[str, ...], edges: tuple[tuple[str, str], ...]) -> dict[str, int]:
    """Longest-path ranks on the condensation of the system's own subgraph.

    Two rules worth stating, because both are choices rather than consequences:

    * **A cycle is ranked, not rejected, and its members are spread over consecutive ranks.**
      A recycle loop is a chain that comes back; collapsing it into one rank would draw it as a
      pile, and the legacy engine's cyclic handling is the same idea.
    * **Only edges inside one system affect that system's ranks.** A connection that crosses
      systems is a step-3 routing problem, and letting it merge two systems' rank structures
      would make the partitions stop meaning anything.
    """

    components = _strongly_connected_components(node_ids, edges)
    # Tarjan emits a component only after everything it can reach, so this order is a reverse
    # topological order of the condensation -- which is exactly what longest path needs here.
    component_of: dict[str, int] = {
        node_id: index for index, component in enumerate(components) for node_id in component
    }
    successors: dict[int, set[int]] = {index: set() for index in range(len(components))}
    for source, target in edges:
        if source not in component_of or target not in component_of:
            continue
        if component_of[source] != component_of[target]:
            successors[component_of[source]].add(component_of[target])

    base_rank = [0] * len(components)
    for index in range(len(components) - 1, -1, -1):
        for successor in successors[index]:
            base_rank[successor] = max(base_rank[successor], base_rank[index] + 1)

    ranks: dict[str, int] = {}
    for index, component in enumerate(components):
        for offset, node_id in enumerate(component):
            ranks[node_id] = base_rank[index] + offset
    return ranks


def _quantize(value: float) -> float:
    """The declared coordinate quantum, not whatever the host happens to print."""

    rounded = round(value, LAYOUT_COORDINATE_DECIMALS)
    return 0.0 if rounded == 0 else rounded


def _system_geometry(
    node_ids: tuple[str, ...], edges: tuple[tuple[str, str], ...], policy: SpacingPolicy
) -> dict[str, tuple[float, float]]:
    """Where each node of one system sits relative to that system's own origin."""

    ranks = _ranks(node_ids, edges)
    lanes: dict[int, list[str]] = {}
    for node_id in sorted(node_ids):
        lanes.setdefault(ranks[node_id], []).append(node_id)
    return {
        node_id: (_quantize(rank * policy.rank_gap), _quantize(lane * policy.node_gap))
        for rank, members in lanes.items()
        for lane, node_id in enumerate(members)
    }


def place_semantic_layout(plan: SemanticLayoutPlan) -> SemanticLayoutPlan:
    """Step 2: deterministic partition, rank and absolute placement.

    A pure function of the plan, the intent and the rules version -- no clock, no service, no
    environment, no model coordinates. The result is a *new* plan carrying a placement
    projection; the topology the plan describes is never written to, which is what makes
    "semantic digest before == semantic digest after" a real check rather than an assertion
    about a copy nobody kept.

    What it deliberately does not do: routing, waypoints, obstacle avoidance, annotations,
    content bounds, the canvas, and the canonical layout digest. Those are steps 3 to 5, and
    each one has to be able to fail on its own.
    """

    if plan.produced_at_step != INGRESS_STEP:
        raise SemanticTopologyIngressError(
            f"the placement step takes the step-1 plan, not one produced at "
            f"{plan.produced_at_step!r}: re-placing an already placed plan would make the "
            "second run's input the first run's output"
        )

    policy = spacing_policy(plan.intent.density)
    honoured_grouping, fallback_from = resolve_grouping(plan.intent.grouping)
    kinds = dict(plan.node_kinds)
    unroutable = sorted(
        node_id for node_id, kind in plan.node_kinds if kind not in STEP_2_PLACEMENT_KINDS
    )
    if unroutable:
        raise UnknownPlacementKindError(
            f"step 2 places {list(STEP_2_PLACEMENT_KINDS)} rows, but the topology declares "
            f"{[kinds[node_id] for node_id in unroutable]} for {unroutable}: a kind this step "
            "cannot place is a failure, not a row to invent"
        )
    sizes = {node_id: node_size(kind) for node_id, kind in plan.node_kinds}
    system_of = {
        node_id: system_id
        for system_id, node_ids in plan.node_ids_by_system
        for node_id in node_ids
    }
    intra_system_edges = tuple(
        (source, target)
        for source, target in plan.flow_edges
        if system_of.get(source) is not None and system_of.get(source) == system_of.get(target)
    )

    reading_order = list(plan.systems_in_reading_order)
    cursor = 0.0
    rows: list[dict[str, Any]] = []
    for system_id in reading_order:
        node_ids = next(
            (nodes for candidate, nodes in plan.node_ids_by_system if candidate == system_id),
            (),
        )
        relative = _system_geometry(node_ids, intra_system_edges, policy)
        if not relative:
            continue
        extent = max(
            sizes[node_id].width
            if plan.intent.primary_flow_direction == "left_to_right"
            else sizes[node_id].height
            for node_id in node_ids
        )
        flow_extent = max(offset_x for offset_x, _ in relative.values()) + extent
        cross_extent = max(offset_y for _, offset_y in relative.values()) + max(
            sizes[node_id].height
            if plan.intent.primary_flow_direction == "left_to_right"
            else sizes[node_id].width
            for node_id in node_ids
        )
        if honoured_grouping == "flat":
            # One band, systems chained end to end along the flow axis.
            block_flow = cursor
            block_cross = 0.0
            advance = flow_extent + policy.component_gap
        else:
            # One band per system, stacked along the cross axis.
            block_flow = 0.0
            block_cross = cursor
            advance = cross_extent + policy.system_gap
        for node_id in sorted(node_ids):
            offset_flow, offset_cross = relative[node_id]
            if plan.intent.primary_flow_direction == "left_to_right":
                x, y = block_flow + offset_flow, block_cross + offset_cross
            else:
                x, y = block_cross + offset_cross, block_flow + offset_flow
            size = sizes[node_id]
            rows.append(
                {
                    "placement_kind": kinds[node_id],
                    "engineering_id": node_id,
                    "x": _quantize(x),
                    "y": _quantize(y),
                    "width": size.width,
                    "height": size.height,
                }
            )
        cursor += advance

    rows.sort(key=lambda row: (row["placement_kind"], row["engineering_id"]))
    for row in rows:
        # Exact, in both directions: an undeclared key and a declared-but-unused one are each
        # a defect, and a subset check would hide the first behind a permissive superset.
        if set(row) != set(PLACEMENT_PROJECTION_FIELDS):
            raise SemanticTopologyIngressError(
                f"the placement row for {row.get('engineering_id')!r} carries "
                f"{sorted(set(row) ^ set(PLACEMENT_PROJECTION_FIELDS))} against the declared "
                "placement projection fields"
            )

    def _canonical_key(placed: dict[str, Any]) -> tuple[str, str]:
        return (placed["placement_kind"], placed["engineering_id"])

    if len({_canonical_key(placed) for placed in rows}) != len(rows):
        raise SemanticTopologyIngressError(
            "two placement rows share the canonical sort key: the placement projection must be "
            "a total order, not a stable accident"
        )
    return replace(
        plan,
        placement=tuple(rows),
        produced_at_step=STEP_2,
        honoured_grouping=honoured_grouping,
        grouping_fallback_from=fallback_from,
        spacing=policy,
    )


def placement_digest(plan: SemanticLayoutPlan) -> str:
    """The placement's identity, deliberately separate from the layout digest of step 5."""

    return hashlib.sha256(
        canonical_payload(
            {
                "placement_projection_version": PLACEMENT_PROJECTION_VERSION,
                "topology_digest": plan.topology_digest,
                "engine_version": plan.engine_version,
                "rules_version": plan.rules_version,
                "intent": plan.intent.to_projection(),
                "placement_projection": list(plan.placement),
            }
        ).encode("utf-8")
    ).hexdigest()


class SemanticTopologyIngress:
    """Mixed into the one layout authority so the door belongs to the engine, not beside it.

    The signature is the enforcement: there is no ``preserve_positions`` parameter, so a
    caller cannot override the policy -- and a wrapper cannot smuggle one past a keyword
    check that does not exist.
    """

    #: Restated where a reader of the engine can see it, and asserted against the contract.
    ACCEPTS_A_PRESERVE_POSITIONS_ARGUMENT = not M7_INGRESS_HAS_NO_PRESERVE_POSITIONS_PARAMETER
    RECEIVES_INTENT_AT_STEP = INTENT_DIMENSIONS_ARE_RECEIVED_AT_STEP

    def layout_semantic_topology(self, topology: SemanticTopology) -> SemanticLayoutPlan:
        """Step 1: receive the topology and the intent; place nothing.

        Step 2 continues from the returned plan. Keeping the ingress free of placement is
        what makes "the model has lost geometry authority" checkable separately from
        "the engine lays a plant drawing out well".
        """

        return plan_semantic_layout(topology)


#: The plan digest must never be mistaken for the canonical layout digest: the latter is
#: computed at step 5 from the placement, and this one exists so a difference between two runs
#: is attributable to the ingress or to the engine.
PLAN_DIGEST_IS_THE_CANONICAL_LAYOUT_DIGEST = (
    SEMANTIC_LAYOUT_PLAN_DIGEST_VERSION in LAYOUT_DIGEST_INPUTS
)
