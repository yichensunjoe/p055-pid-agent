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
from dataclasses import dataclass, field
from typing import Any

from .m7_diagram_adapter import SemanticTopology
from .m7_layout_contract import (
    INTENT_DIMENSIONS_ARE_RECEIVED_AT_STEP,
    LAYOUT_DIGEST_INPUTS,
    LAYOUT_INTENT_CONSUMPTION,
    LAYOUT_INTENT_DIMENSIONS,
    M7_INGRESS_HAS_NO_PRESERVE_POSITIONS_PARAMETER,
    M7_SYNTHESIS_INGRESS_PRESERVE_POSITIONS,
    PHASE_2B_STEPS,
)

#: Published by the engine, not by the contract: a rules change is an engine edit that carries
#: a visible version, and the digest reads both through these names.
LAYOUT_ENGINE_VERSION = "auto_layout/1"
LAYOUT_RULES_VERSION = "deterministic-layout-rules/1"

#: The plan's own digest version, so a plan identity cannot be confused with the canonical
#: layout digest that step 5 will produce. Deliberately absent from ``LAYOUT_DIGEST_INPUTS``.
SEMANTIC_LAYOUT_PLAN_DIGEST_VERSION = "m7-semantic-layout-plan-digest/1"

#: Step 1, by the name the contract gives it. Named by its declaration rather than by its
#: position, so reordering the steps is a loud failure here instead of a silent shift.
INGRESS_STEP = next(
    key
    for key, value in PHASE_2B_STEPS
    if value == "semantic_topology_ingress_and_intent_resolution"
)


class SemanticTopologyIngressError(ValueError):
    """The ingress was handed something that is not the engine-facing input contract."""


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
            "placement": list(self.placement),
            "canvas_bounds": self.canvas_bounds,
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
            use.dimension
            for use in self.intent_uses
            if _step_index(use.applied_at_step) > produced
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
    )


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
