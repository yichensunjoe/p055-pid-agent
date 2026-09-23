"""M7-2 phase 2B step 5: the canonical layout identity and deterministic replay.

Steps 1-4 produce a drawing. Step 5 says *which drawing* it is, and that needs two proofs that
have nothing to do with whether the picture is good:

**The layout did not change the plant.** The engineering digest is computed from the topology the
engine received and reconstructed from the plan's own live fields after the layout has run. They
are built by two code paths from two inputs -- the topology, and the plan's record of it -- so
equality means the engine did not reclassify a device, re-point a connection, move a node to
another system or drop a loop member on the way through placement, reflow and routing. Two copies
of the same function would only have proven that a function is deterministic.

**The identity depends on nothing volatile.** The digest is computed over a closed, declared
envelope: the two versions, the engineering digest, the engine and rules versions, the symbol
geometry snapshot, and the canonical placement projection with its envelope. Timing, run ids,
providers and attempt counters are neither fields of the plan nor keys of the envelope, and the
envelope check refuses a payload that carries one -- so \"two runs of the same drawing agree\" is
enforced structurally instead of by remembering not to add a timestamp.

What the canonical projection deliberately does *not* contain is what the drawing *says*: no label
text, no tags. A layout digest identifies where things are. That is the same ruling the annotation
step already made, restated here because the digest is exactly where someone would be tempted to
put the text back.

The geometry-coverage half of the first proof is separate from any digest, because a digest can
only compare what both sides carry: every device is placed exactly once and as the kind it is,
every connection is routed exactly once, and no geometry row names an entity or connection nobody
declared. A dropped entity would leave both digests equal and the drawing short.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .auto_layout_semantic import (
    STEP_4,
    STEP_5,
    SemanticLayoutPlan,
    SemanticTopologyIngressError,
)
from .m7_diagram_adapter import (
    SemanticTopology,
    engineering_digest,
    engineering_digest_rows,
    topology_engineering_rows,
    topology_semantic_digest,
)
from .m7_diagram_spec import SPEC_SCHEMA
from .m7_layout_contract import (
    CANONICAL_LAYOUT_PROJECTION_FIELDS,
    CANONICAL_PROJECTION_ENVELOPE_FIELDS,
    CANONICAL_PROJECTION_SORT_KEY,
    LAYOUT_COORDINATE_DECIMALS,
    LAYOUT_DIGEST_EXCLUDES_VOLATILE_BOOKKEEPING,
    LAYOUT_DIGEST_INPUTS,
    LAYOUT_DIGEST_VERSION,
    LAYOUT_PROJECTION_VERSION,
    PLAN_DIGEST_AND_LAYOUT_DIGEST_ARE_INDEPENDENT,
    REPLAY_MAY_COMPARE_TWO_DIGESTS_FROM_DIFFERENT_VERSIONS,
)

#: The projection is declared inside the plan as rows carrying exactly these field names, so a
#: reader of a digest knows what was inside it. Derived here rather than retyped.
CANONICAL_PROJECTION_FIELD_NAMES: tuple[str, ...] = tuple(
    field.name for field in CANONICAL_LAYOUT_PROJECTION_FIELDS if field.included
)
CANONICAL_PROJECTION_EXCLUDED_FIELD_NAMES: tuple[str, ...] = tuple(
    field.name for field in CANONICAL_LAYOUT_PROJECTION_FIELDS if not field.included
)


class LayoutIdentityError(SemanticTopologyIngressError):
    """The identity could not be produced, or a gate about it failed."""


class EngineeringSemanticPreservationError(LayoutIdentityError):
    """The layout changed the plant it was asked to draw."""


class LayoutInputPreservationError(LayoutIdentityError):
    """The layout swapped a symbol binding or a layout intent class on its own."""


class GeometryCoverageError(LayoutIdentityError):
    """The geometry does not cover the semantics: an entity or connection was dropped or invented."""


class CanonicalProjectionError(LayoutIdentityError):
    """The canonical projection is not a total order, or carries a non-finite coordinate."""


class ReplayIdentityMismatchError(LayoutIdentityError):
    """Two runs of the same input produced different identities."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _quantize(value: float) -> float:
    """The declared quantum, and never anything the environment happens to print."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CanonicalProjectionError(
            f"a canonical coordinate must be a number, not {type(value).__name__}"
        )
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise CanonicalProjectionError("a canonical coordinate must be finite")
    rounded = round(number, LAYOUT_COORDINATE_DECIMALS)
    # -0.0 equals 0.0 but formats differently, and two equal values must produce equal bytes.
    return 0.0 if rounded == 0 else rounded


# --------------------------------------------------------------------------------------------
# The first negative gate: the layout did not change the plant.
# --------------------------------------------------------------------------------------------


def plan_engineering_rows(plan: SemanticLayoutPlan) -> dict[str, Any]:
    """The engineering facts, rebuilt from the plan's own live fields.

    This is the "after layout" side of the preservation gate. It reads the plan, not a stored
    copy of the digest, which is what makes it able to see a step that mutated a connection's
    endpoints or a device's kind: a stored digest would happily outlive the facts it described.
    """

    return engineering_digest_rows(
        spec_schema=SPEC_SCHEMA,
        systems=[fact.to_projection() for fact in plan.engineering_systems],
        entities=[fact.to_projection() for fact in plan.engineering_entities],
        # The connection row is the *engineering* row, so it is keyed by the connection's
        # engineering id: the plan's own field is called ``connection_id`` and renaming the
        # digest key to match would give the same plant two row shapes.
        connections=[
            {
                "engineering_id": connection.connection_id,
                "source_engineering_id": connection.source_engineering_id,
                "target_engineering_id": connection.target_engineering_id,
                "source_port_id": connection.source_port_id,
                "target_port_id": connection.target_port_id,
                "medium": connection.medium,
                "tag": connection.tag,
            }
            for connection in plan.connections
        ],
        required_loops=[
            {"loop_id": loop_id, "engineering_ids": list(engineering_ids)}
            for loop_id, engineering_ids in plan.required_loops
        ],
        layout_intent=plan.intent.to_projection(),
    )


def plan_engineering_digest(plan: SemanticLayoutPlan) -> str:
    """The engineering digest reconstructed from the finalized plan."""

    return engineering_digest(plan_engineering_rows(plan))


def engineering_semantic_rows(rows: Mapping[str, Any]) -> dict[str, Any]:
    """The engineering subset of the rows: the plant, without the layout request.

    The upstream semantic digest does include the layout intent -- it is part of the
    specification -- but this gate is the engineering subset, because the layout intent is a
    request about presentation rather than a fact about the plant. Sharing the row *shape* while
    selecting fewer parts is what keeps "the same words" from meaning two different things.
    """

    return {part: rows.get(part) for part in ENGINEERING_SEMANTIC_ROW_PARTS}


def plan_engineering_semantic_digest(plan: SemanticLayoutPlan) -> str:
    """The engineering digest of the finalized plan, restricted to the plant."""

    return engineering_digest(engineering_semantic_rows(plan_engineering_rows(plan)))


def engineering_semantic_preservation_problems(
    plan: SemanticLayoutPlan, topology: SemanticTopology
) -> list[str]:
    """Proof A: the plant before the layout against the plant reconstructed after it.

    The comparison is over the *rows*, not only the digests, so a failure names the part that
    differs. A digest mismatch with no explanation is a report a reader cannot act on.
    """

    before = engineering_semantic_rows(topology_engineering_rows(topology))
    after = engineering_semantic_rows(plan_engineering_rows(plan))
    problems: list[str] = []
    for part in ENGINEERING_SEMANTIC_ROW_PARTS:
        if before.get(part) != after.get(part):
            problems.append(
                f"the layout changed the {part!r} it was given: "
                f"received {_summarize(before.get(part))}, holds {_summarize(after.get(part))}"
            )
    if not problems and engineering_digest(before) != engineering_digest(after):
        problems.append(
            "the engineering digests differ although every declared part matches: the row shape "
            "is not the one the digest is computed over"
        )
    if not plan.engineering_entities or not plan.engineering_systems:
        problems.append(
            "the plan carries no engineering record to reconstruct from: an empty record would "
            "make this gate pass by having nothing to compare"
        )
    return problems


def layout_input_preservation_problems(
    plan: SemanticLayoutPlan, topology: SemanticTopology
) -> list[str]:
    """Proof B: the engine did not swap a symbol or change the intent it was handed.

    Structural equality against the topology the engine received, in its own name: there is no
    second identity axis for it, and it is deliberately *not* part of the engineering digest --
    a renderer binding and a presentation request are not facts about the plant.
    """

    problems: list[str] = []
    if plan.topology_digest != topology.digest:
        problems.append(
            "the plan no longer names the topology it was built from: a gate over the wrong "
            "input proves nothing about the layout"
        )
    expected_symbols = tuple(
        (node.engineering_id, node.symbol_key)
        for node in sorted(topology.nodes, key=lambda item: item.engineering_id)
    )
    if plan.node_symbols != expected_symbols:
        problems.append(
            "the layout engine changed the symbol binding it was given: "
            f"received {_summarize(expected_symbols)}, holds {_summarize(plan.node_symbols)}"
        )
    expected_kinds = tuple(
        (node.engineering_id, node.kind)
        for node in sorted(topology.nodes, key=lambda item: item.engineering_id)
    )
    if plan.node_kinds != expected_kinds:
        problems.append(
            "the layout engine changed a node kind used for rendering: "
            f"received {_summarize(expected_kinds)}, holds {_summarize(plan.node_kinds)}"
        )
    expected_intent = {
        "orientation": topology.intent.orientation,
        "preferred_aspect_class": topology.intent.preferred_aspect_class,
        "primary_flow_direction": topology.intent.primary_flow_direction,
        "grouping": topology.intent.grouping,
        "density": topology.intent.density,
        "system_order": list(topology.intent.system_order),
    }
    held_intent = plan.intent.to_projection()
    if held_intent != expected_intent:
        problems.append(
            "the layout engine changed the layout intent it was given: "
            f"received {_summarize(expected_intent)}, holds {_summarize(held_intent)}"
        )
    return problems


ENGINEERING_SEMANTIC_ROW_PARTS: tuple[str, ...] = (
    "spec_schema",
    "systems",
    "entities",
    "connections",
    "required_loops",
)
#: The shape the upstream semantic digest is computed over: the engineering parts plus the layout
#: request. Kept as one list so the subset above is visibly a subset rather than a second shape.
ENGINEERING_ROW_PARTS: tuple[str, ...] = (*ENGINEERING_SEMANTIC_ROW_PARTS, "layout_intent")


def _summarize(value: Any) -> str:
    text = _canonical(value)
    return text if len(text) <= 240 else text[:237] + "..."


def geometry_coverage_problems(plan: SemanticLayoutPlan) -> list[str]:
    """Every declared entity and connection, drawn exactly once and as what it is declared to be.

    Separate from the digest gate on purpose. A digest compares what both sides carry, so it is
    structurally blind to a row that went *missing* -- and a missing vessel is exactly the
    failure a layout is most likely to have and least likely to announce.
    """

    problems: list[str] = []
    kinds = {fact.engineering_id: fact.kind for fact in plan.engineering_entities}
    connection_ids = {connection.connection_id for connection in plan.connections}

    placed: dict[str, list[str]] = {}
    for row in plan.placement:
        placed.setdefault(str(row["engineering_id"]), []).append(str(row["placement_kind"]))
    for node_id, node_kinds in sorted(placed.items()):
        if node_id not in kinds:
            problems.append(f"the layout placed {node_id!r}, which nobody declared")
            continue
        if len(node_kinds) != 1:
            problems.append(
                f"the layout placed {node_id!r} {len(node_kinds)} times: a device is placed once"
            )
        elif node_kinds[0] != kinds[node_id]:
            problems.append(
                f"the layout placed {node_id!r} as {node_kinds[0]!r}, and it is declared "
                f"{kinds[node_id]!r}"
            )
    for node_id, kind in sorted(kinds.items()):
        if node_id not in placed:
            problems.append(
                f"the layout dropped {kind} {node_id!r}: it is declared and never placed"
            )

    routed = [str(row["engineering_id"]) for row in plan.routing]
    for connection_id in sorted(set(routed)):
        if connection_id not in connection_ids:
            problems.append(f"the layout routed {connection_id!r}, which nobody declared")
        elif routed.count(connection_id) != 1:
            problems.append(
                f"the layout routed {connection_id!r} {routed.count(connection_id)} times: a "
                "connection is routed once"
            )
    for connection_id in sorted(connection_ids):
        if connection_id not in routed:
            problems.append(
                f"the layout dropped connection {connection_id!r}: it is declared and never routed"
            )

    for row in plan.annotations:
        node_id = str(row["engineering_id"])
        if node_id not in kinds:
            problems.append(f"the layout annotated {node_id!r}, which nobody declared")
        elif node_id not in placed:
            problems.append(
                f"the layout annotated {node_id!r} without placing it: a label belongs to an "
                "entity that exists"
            )
    return problems


# --------------------------------------------------------------------------------------------
# The canonical projection: the drawing as the identity sees it.
# --------------------------------------------------------------------------------------------


def canonical_projection_rows(plan: SemanticLayoutPlan) -> tuple[dict[str, Any], ...]:
    """Every presentation row, reduced to the declared fields, sorted and proven unique.

    The placement, routing and annotation rows are separate lists because different steps produce
    them; the identity is *one* projection over all three, because a reader comparing two digests
    is comparing two drawings, not three pipelines.
    """

    rows: list[dict[str, Any]] = []
    for source in (plan.placement, plan.routing, plan.annotations):
        for row in source:
            canonical = {
                "engineering_id": str(row["engineering_id"]),
                "placement_kind": str(row["placement_kind"]),
                "x": _quantize(row["x"]),
                "y": _quantize(row["y"]),
                "width": _quantize(row["width"]),
                "height": _quantize(row["height"]),
                "ordered_waypoints": [
                    [_quantize(point[0]), _quantize(point[1])]
                    for point in row.get("ordered_waypoints", ())
                ],
            }
            unexpected = set(row) - set(CANONICAL_PROJECTION_FIELD_NAMES)
            if unexpected:
                raise CanonicalProjectionError(
                    f"a layout row carries undeclared fields {sorted(unexpected)}: a canonical "
                    "projection that kept them would digest a value the contract does not know "
                    "exists"
                )
            rows.append(canonical)
    rows.sort(key=lambda row: tuple(str(row[part]) for part in CANONICAL_PROJECTION_SORT_KEY))
    keys = [tuple(str(row[part]) for part in CANONICAL_PROJECTION_SORT_KEY) for row in rows]
    if len(keys) != len(set(keys)):
        duplicate = next(key for key in keys if keys.count(key) > 1)
        raise CanonicalProjectionError(
            f"two presentation rows share the sort key {duplicate}: the projection would not be "
            "a total order, and a digest over it would depend on the accidental order of rows"
        )
    return tuple(rows)


def canonical_projection_envelope(plan: SemanticLayoutPlan) -> dict[str, dict[str, float]]:
    """The whole-drawing bounds, as envelope fields rather than repeated per row."""

    if plan.content_bounds is None or plan.canvas_bounds is None:
        raise LayoutIdentityError(
            "the canonical projection needs the content and canvas envelopes step 4 derived: "
            "without them the identity would describe the placement and not the drawing"
        )
    envelope = {
        "content_bounds": {
            name: _quantize(plan.content_bounds[name])
            for name in ("x", "y", "width", "height")
        },
        "canvas_bounds": {
            name: _quantize(plan.canvas_bounds[name]) for name in ("x", "y", "width", "height")
        },
    }
    missing = set(CANONICAL_PROJECTION_ENVELOPE_FIELDS) - set(envelope)
    if missing:
        raise LayoutIdentityError(f"the envelope is missing {sorted(missing)}")
    return envelope


def canonical_layout_payload(plan: SemanticLayoutPlan) -> dict[str, Any]:
    """The closed envelope the digest is computed over, keyed by the declared input names."""

    return {
        "layout_digest_version": LAYOUT_DIGEST_VERSION,
        "layout_projection_version": LAYOUT_PROJECTION_VERSION,
        "diagram_spec_semantic_digest": plan_engineering_digest(plan),
        # Read from the plan rather than from the constants: the plan records which engine and
        # rules actually ran, and a digest that named today's constants would describe a run it
        # cannot see.
        "layout_engine_version": plan.engine_version,
        "layout_rules_version": plan.rules_version,
        "symbol_geometry_catalog_digest": plan.symbol_geometry_catalog_digest,
        "canonical_projection_envelope": canonical_projection_envelope(plan),
        "canonical_placement_projection": list(canonical_projection_rows(plan)),
    }


def payload_problems(payload: Mapping[str, Any]) -> list[str]:
    """Why a payload may not be digested, as a list a caller can act on.

    Closed on both sides: a declared input that is missing and an undeclared key that is present
    are each a defect, because either one means the digest names something other than what the
    contract says it names.
    """

    problems: list[str] = []
    declared = set(LAYOUT_DIGEST_INPUTS)
    present = set(payload)
    for missing in sorted(declared - present):
        problems.append(f"the digest payload is missing the declared input {missing!r}")
    for undeclared in sorted(present - declared):
        if undeclared in LAYOUT_DIGEST_EXCLUDES_VOLATILE_BOOKKEEPING:
            problems.append(
                f"{undeclared!r} is volatile bookkeeping and may not enter the layout digest: two "
                "identical runs differ there, so the identity would be stable only by accident"
            )
        else:
            problems.append(
                f"{undeclared!r} is not a declared digest input: an undeclared key makes the "
                "digest's meaning private to whoever added it"
            )
    for name in ("layout_digest_version", "layout_projection_version"):
        if name in payload and not str(payload[name]).strip():
            problems.append(f"{name!r} must carry a version: an unversioned identity is not one")
    if plan_symbol_digest_missing(payload):
        problems.append(
            "the digest payload carries no symbol geometry catalog digest: the engine no longer "
            "owns the only input to placement"
        )
    return problems


def plan_symbol_digest_missing(payload: Mapping[str, Any]) -> bool:
    return "symbol_geometry_catalog_digest" in payload and not str(
        payload["symbol_geometry_catalog_digest"]
    ).strip()


def canonical_layout_digest_for_payload(payload: Mapping[str, Any]) -> str:
    """Hash a payload that has already been checked, in the canonical byte form."""

    problems = payload_problems(payload)
    if problems:
        raise LayoutIdentityError("the digest payload is not the declared envelope: " + "; ".join(problems))
    return hashlib.sha256(_canonical(dict(payload)).encode("utf-8")).hexdigest()


def canonical_layout_digest(plan: SemanticLayoutPlan) -> str:
    """The layout's identity: `m7-layout-digest/2`, computed over the declared inputs only."""

    if plan.produced_at_step != STEP_4:
        raise LayoutIdentityError(
            f"the canonical identity is computed over the finalized step-4 plan, not over one "
            f"produced at {plan.produced_at_step!r}: digesting a drawing that is not finished "
            "would name a drawing nobody drew"
        )
    return canonical_layout_digest_for_payload(canonical_layout_payload(plan))


# --------------------------------------------------------------------------------------------
# The second negative gate: replay compares canonical identity, not objects.
# --------------------------------------------------------------------------------------------


def deterministic_replay_problems(
    first: SemanticLayoutPlan, second: SemanticLayoutPlan
) -> list[str]:
    """Two runs of the same input, compared by identity.

    Compared by digest rather than by plan objects or bytes on purpose: two identical runs differ
    in timing and identifiers by construction, and a comparison that could see those differences
    would report a correct replay as a change.
    """

    problems: list[str] = []
    for name, plan in (("first", first), ("second", second)):
        if plan.produced_at_step != STEP_5:
            problems.append(
                f"the {name} plan has no identity: it was produced at {plan.produced_at_step!r}"
            )
        if not plan.canonical_layout_digest:
            problems.append(f"the {name} plan carries no canonical layout digest")
    if problems:
        return problems
    if first.canonical_layout_digest != second.canonical_layout_digest:
        problems.append(
            "two runs of the same input produced different identities "
            f"({first.canonical_layout_digest[:12]}... vs {second.canonical_layout_digest[:12]}...)"
        )
    return problems


def replay_verifies_identity_but_not_semantics(first: SemanticLayoutPlan, second: SemanticLayoutPlan) -> bool:
    """Restated for a test: the replay gate is an equality of identities and claims no more."""

    return not deterministic_replay_problems(first, second)


# --------------------------------------------------------------------------------------------
# The step itself.
# --------------------------------------------------------------------------------------------


def finalize_semantic_layout(
    plan: SemanticLayoutPlan, topology: SemanticTopology
) -> SemanticLayoutPlan:
    """Step 5: prove the layout kept the plant, then name the drawing.

    The order is the rule. A digest computed before the preservation gate would be a stable name
    for a drawing that may have changed the plant, and a name is the one thing a wrong drawing
    must not get: it would make \"the same drawing\" a claim about the wrong picture.
    """

    if plan.produced_at_step != STEP_4:
        raise LayoutIdentityError(
            f"step 5 takes the finalized step-4 plan, not one produced at "
            f"{plan.produced_at_step!r}"
        )
    # Proof A, then proof B, then coverage, then the name -- the order is the rule: an identity
    # computed for a drawing that changed the plant would be a stable name for the wrong picture.
    engineering = engineering_semantic_preservation_problems(plan, topology)
    if engineering:
        raise EngineeringSemanticPreservationError(
            "the layout changed the plant it was asked to draw: " + "; ".join(engineering)
        )
    layout_input = layout_input_preservation_problems(plan, topology)
    if layout_input:
        raise LayoutInputPreservationError(
            "the layout changed its own input: " + "; ".join(layout_input)
        )
    coverage = geometry_coverage_problems(plan)
    if coverage:
        raise GeometryCoverageError(
            "the layout's geometry does not cover its semantics: " + "; ".join(coverage)
        )
    rows = canonical_projection_rows(plan)
    envelope = canonical_projection_envelope(plan)
    staged = replace(
        plan,
        canonical_placement_projection=rows,
        canonical_projection_envelope=envelope,
    )
    payload = canonical_layout_payload(staged)
    digest = canonical_layout_digest_for_payload(payload)
    return replace(staged, canonical_layout_digest=digest, produced_at_step=STEP_5)


class SemanticLayoutIdentity:
    """Step 5, mixed into the one layout authority for the same reason steps 1, 3 and 4 are.

    An identity computed beside the engine would be a second opinion about which drawing came
    out -- and the drawing that came out is exactly the thing a reader compares across runs.
    """

    def layout_semantic_identity(
        self, plan: SemanticLayoutPlan, topology: SemanticTopology
    ) -> SemanticLayoutPlan:
        """Finalize a step-4 plan into a named, replayable layout."""

        return finalize_semantic_layout(plan, topology)


#: Re-exported for callers that only need the "before" side, so the gate is reachable without
#: importing the adapter as well.
ENGINEERING_DIGEST_BEFORE_LAYOUT = topology_semantic_digest

#: Restated so a test can read the decision: the two identities are independent, so neither
#: contains the other and two names can never disagree about which drawing they describe.
CANONICAL_DIGEST_IS_NOT_THE_PLAN_DIGEST = PLAN_DIGEST_AND_LAYOUT_DIGEST_ARE_INDEPENDENT
REPLAY_COMPARES_ACROSS_VERSIONS = REPLAY_MAY_COMPARE_TWO_DIGESTS_FROM_DIFFERENT_VERSIONS


__all__ = [
    "CANONICAL_DIGEST_IS_NOT_THE_PLAN_DIGEST",
    "CANONICAL_PROJECTION_EXCLUDED_FIELD_NAMES",
    "CANONICAL_PROJECTION_FIELD_NAMES",
    "ENGINEERING_DIGEST_BEFORE_LAYOUT",
    "ENGINEERING_ROW_PARTS",
    "ENGINEERING_SEMANTIC_ROW_PARTS",
    "REPLAY_COMPARES_ACROSS_VERSIONS",
    "CanonicalProjectionError",
    "EngineeringSemanticPreservationError",
    "GeometryCoverageError",
    "LayoutIdentityError",
    "LayoutInputPreservationError",
    "ReplayIdentityMismatchError",
    "SemanticLayoutIdentity",
    "canonical_layout_digest",
    "canonical_layout_digest_for_payload",
    "canonical_layout_payload",
    "canonical_projection_envelope",
    "canonical_projection_rows",
    "deterministic_replay_problems",
    "engineering_semantic_preservation_problems",
    "engineering_semantic_rows",
    "finalize_semantic_layout",
    "geometry_coverage_problems",
    "layout_input_preservation_problems",
    "payload_problems",
    "plan_engineering_digest",
    "plan_engineering_rows",
    "plan_engineering_semantic_digest",
    "replay_verifies_identity_but_not_semantics",
]
