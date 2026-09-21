"""What may plan a repair, and what it is allowed to see (baseline §B1, §M5-4).

The remote baseline splits planning into two tracks that must share **one contract**:

* the deterministic planner (track D), which is the CI hard gate, and
* a real model (track M), which is release-gate evidence.

Both implement :class:`RepairPlanner` and both receive a :class:`RepairContext`. The thing
that makes the split honest is what the context *does not* contain: the mutation oracle, the
expected answer, or the pre-mutation drawing. A planner that could see any of those would
turn "success rate" into a measurement of how well it reads the answer key, so the context is
built from the document as it *is*, the canonical findings, the semantic schema and the
structured assessments of previous attempts — nothing else.

Two deliberate choices in the deterministic planner:

* **It reuses the repository's own router** (``diagram_quality.route_connector_points``) for
  routing findings. Writing a second router would be a second source of truth about what a
  legal route is, which is precisely the failure mode M4 spent three review rounds removing.
* **It declines rather than guesses.** A finding it has no mechanical repair for produces
  ``RepairDecline(kind="not_repairable")``, and the safety suite treats that refusal as the
  correct answer. Guessing is how a repair agent turns into a drawing mutation agent.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .agent_semantic_models import (
    ReconnectConnectorOperation,
    ReplaceSymbolOperation,
    SafeDeleteElementOperation,
    SemanticTransaction,
)
from .diagram_quality import (
    _element_rect,  # noqa: PLC2701 - one router, one geometry
    route_connector_points,
)
from .models import (
    AddElementOperation,
    ConnectorElement,
    Document,
    Element,
    Point,
    SymbolElement,
    TextElement,
    UpdateElementOperation,
)
from .repair_models import RepairAttemptRecord, RepairFindingRef, RepairRequest
from .repair_scope import LOCAL_BINDING_RADIUS, element_map
from .symbols import SymbolRegistry
from .validation_models import ValidationIssue

#: How far a planner may reach. The deterministic planner is a *repair* planner: it produces
#: a local plan for one finding, never a whole-drawing rewrite.
PLANNER_VERSION = "1"

#: Re-exported so the planner and the scope freeze use one definition of "local" (see
#: :mod:`agentcad.repair_scope`).
LOCAL_BINDING_RADIUS = LOCAL_BINDING_RADIUS


class RepairDeclined(RuntimeError):
    """A planner refused to plan, with a machine-readable reason.

    Refusal is a first-class outcome (baseline §D): "no safe repair exists" is the right
    answer for an ambiguous deletion or an out-of-scope fix, and the runner must record it
    rather than convert it into an attempted write.
    """

    def __init__(self, kind: Literal["human_required", "not_repairable", "policy_violation"], reason: str, code: str = ""):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class FreeEnd:
    """One connector endpoint that points at nothing, plus what the drawing still says about it.

    ``anchor`` is where the missing port used to be — the endpoint's own recorded point — which
    is what makes reconstructing the removed device a *verification* rather than a guess.
    """

    connector_id: str
    endpoint: Literal["source", "target"]
    anchor: Point
    direction: Literal["in", "out"]


@dataclass
class RepairPlanDraft:
    """One attempt's plan, before the compiler has looked at it.

    ``operations`` are the same semantic operations a model would return, so the two tracks
    converge on one compile/validate path instead of two.
    """

    operations: list[Any] = field(default_factory=list)
    rationale: str = ""
    source: str = "deterministic"
    #: What this attempt cost, when the planner knows. A deterministic planner leaves these at
    #: zero; a model reports its provider usage, or an estimator and says so (baseline §F). The
    #: orchestrator enforces the per-case budget from here rather than the planner policing
    #: itself, so an over-budget attempt is recorded as a failure with the same evidence as any
    #: other refusal.
    input_tokens: int = 0
    output_tokens: int = 0
    token_usage_estimated: bool = False

    def transaction(self, *, expected_revision: int, label: str) -> SemanticTransaction:
        return SemanticTransaction(
            operations=self.operations,
            expected_revision=expected_revision,
            label=label,
        )


@dataclass
class RepairContext:
    """Everything a planner may read, bounded by the §F context budget."""

    request: RepairRequest
    document: Document
    registry: SymbolRegistry
    #: The findings that match the request's target identity: what "this repair" is about.
    findings: list[ValidationIssue]
    #: Every canonical finding of the target's *class* inside the frozen scope. Only the
    #: endpoint family reads it, and for the reason that makes the family exist: removing one
    #: device breaks every line that pointed at it, so a repair that rebinds one line has not
    #: repaired the drawing. Handing this view to every handler instead would let a planner act
    #: far beyond the finding the release gate named.
    class_findings: list[ValidationIssue] = field(default_factory=list)
    prior_attempts: list[RepairAttemptRecord] = field(default_factory=list)
    attempt: int = 1
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def target(self) -> RepairFindingRef:
        return self.request.target

    def element(self, element_id: str) -> Element | None:
        return element_map(self.document).get(element_id)

    def connectors(self) -> list[ConnectorElement]:
        return [
            element for element in self.document.elements if isinstance(element, ConnectorElement)
        ]

    def symbols(self) -> list[SymbolElement]:
        return [element for element in self.document.elements if isinstance(element, SymbolElement)]

    def context_bytes(self) -> int:
        return len(
            json.dumps(self.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        )

    def context_elements(self) -> int:
        return int(self.payload.get("context_element_count", 0))

    def within_budget(self) -> tuple[bool, str]:
        """Whether this context may be handed to a planner at all (baseline §F)."""

        if self.context_bytes() > self.request.budgets.max_context_bytes:
            return False, "context_budget_exceeded"
        if self.context_elements() > self.request.budgets.max_context_elements:
            return False, "context_budget_exceeded"
        return True, ""


class RepairPlanner(Protocol):
    """The one planning contract both tracks implement."""

    planner_id: str

    def plan(self, context: RepairContext) -> RepairPlanDraft: ...


# -- bounded, oracle-blind context ------------------------------------------- #


def build_repair_context(
    request: RepairRequest,
    document: Document,
    registry: SymbolRegistry,
    findings: list[ValidationIssue],
    *,
    class_findings: list[ValidationIssue] | None = None,
    prior_attempts: list[RepairAttemptRecord] | None = None,
    attempt: int = 1,
) -> RepairContext:
    """Assemble the localized context, and measure it against the published budgets.

    Locality is a rule about *writes*; the budget is a rule about what crosses the wire. So
    the planner sees the whole drawing (an engineering repair legitimately needs to know
    what an obstacle is) while the serialized context that a model would receive contains
    only the scope and the target findings.
    """

    scope = request.scope
    in_scope = [
        element.model_dump(mode="json")
        for element in sorted(document.elements, key=lambda item: item.id)
        if element.id in set(scope.allowed_element_ids)
    ]
    symbol_keys = sorted(
        {
            str(payload.get("symbol_key", ""))
            for payload in in_scope
            if payload.get("type") == "symbol" and payload.get("symbol_key")
        }
    )
    symbol_definitions = []
    for key in symbol_keys:
        try:
            definition = registry.get(key)
        except KeyError:
            continue
        symbol_definitions.append(
            {
                "key": definition.key,
                "width": definition.width,
                "height": definition.height,
                "ports": [
                    {
                        "id": port.id,
                        "direction": port.direction,
                        "side": getattr(port, "side", ""),
                        "x": port.x,
                        "y": port.y,
                    }
                    for port in definition.ports
                ],
            }
        )
    allowed_ids = set(scope.allowed_element_ids)
    findings_payload = [
        {
            "code": issue.code,
            "severity": issue.severity,
            "validator_id": issue.validator_id,
            "rule_id": issue.rule_id,
            "object_ids": list(issue.object_ids),
            "element_ids": list(issue.element_ids),
            "message": issue.message,
            "expected": issue.expected,
            "actual": issue.actual,
            "suggested_repair": issue.suggested_repair,
            "details": dict(issue.details),
        }
        for issue in [*findings, *(class_findings or [])]
        # Only findings that touch the frozen scope (plus the target class itself) cross the
        # wire: the context budget is a promise about what a planner is shown.
        if not issue.element_ids
        or allowed_ids & set(issue.element_ids)
        or issue.code == request.target.code
    ]
    payload: dict[str, Any] = {
        "document": {
            "id": document.id,
            "revision": document.revision,
            "content_hash": request.content_hash,
            "canvas": document.canvas.model_dump(mode="json"),
            "layer_ids": sorted(layer.id for layer in document.layers),
            "system_ids": sorted(system.id for system in document.systems),
        },
        "profile": {
            "profile_id": request.profile_id,
            "profile_version": request.profile_version,
            "rule_bundle_fingerprint": request.rule_bundle_fingerprint,
        },
        "validation_hash": request.validation_hash,
        "target": request.target.model_dump(mode="json"),
        "scope": request.scope.model_dump(mode="json"),
        "target_findings": findings_payload,
        "scope_elements": in_scope,
        "context_element_count": len(in_scope),
        "symbol_definitions": symbol_definitions,
        "attempt": attempt,
        "prior_attempts": [
            {
                "attempt": record.attempt,
                "failure_code": record.failure_code,
                "compile_issue_codes": record.compile_issue_codes,
                "notes": record.notes,
            }
            for record in (prior_attempts or [])
        ],
    }
    return RepairContext(
        request=request,
        document=document,
        registry=registry,
        findings=findings,
        class_findings=list(class_findings or []),
        prior_attempts=list(prior_attempts or []),
        attempt=attempt,
        payload=payload,
    )


# -- deterministic planner --------------------------------------------------- #

#: The order routes are tried when a connector's geometry has to be rebuilt.
ROUTING_CODES = frozenset(
    {
        "NON_ORTHOGONAL_SEGMENT",
        "MICRO_SEGMENT",
        "UNNECESSARY_BEND",
        "PIPE_THROUGH_EQUIPMENT",
        "PORT_EXIT_MISMATCH",
        "PORT_FACING_MISMATCH",
        "EXCESSIVE_BENDS",
    }
)

#: Codes whose mechanical repair is "put the metadata back", all in one place because they
#: differ only by which attribute is missing.
CONNECTOR_METADATA_CODES: dict[str, str] = {
    "LINE_TAG_MISSING": "process_tag",
    "LINE_MEDIUM_MISSING": "medium",
    "LINE_DIAMETER_MISSING": "nominal_diameter",
}


def _distance(first: Point, second: Point) -> float:
    return math.hypot(first.x - second.x, first.y - second.y)


def _unique_suffix(value: str) -> str:
    """A short, stable, human-plausible discriminator derived from an id.

    Determinism matters more than prettiness: the same defective drawing must always be
    repaired to the same drawing, or the benchmark measures the machine's mood.
    """

    total = 0
    for index, char in enumerate(value):
        total = (total + (index + 1) * ord(char)) % 997
    return f"{total:03d}"


class DeterministicRepairPlanner:
    """A rule-catalogue-driven planner that reads the document, never the answer key.

    Every recipe is a mechanical function of the canonical finding and the drawing state.
    Where a code has no unique safe repair, the planner declines — see ``RepairDeclined``.
    """

    planner_id = "deterministic-repair-planner"

    def __init__(self, *, max_candidates: int = 1) -> None:
        self.planner_version = PLANNER_VERSION
        self.max_candidates = max_candidates

    # -- entry point ----------------------------------------------------- #

    def plan(self, context: RepairContext) -> RepairPlanDraft:
        code = context.target.code
        validator = context.target.validator_id
        handler = self._handler_for(validator, code)
        if handler is None:
            raise RepairDeclined(
                "not_repairable",
                f"no mechanical repair is defined for {validator}.{code}",
                code="unsupported_finding",
            )
        operations = handler(context)
        if not operations:
            raise RepairDeclined(
                "not_repairable",
                f"the drawing offers no legal repair for {validator}.{code}",
                code="no_candidate_repair",
            )
        return RepairPlanDraft(
            operations=operations,
            rationale=f"deterministic repair for {validator}.{code}",
            source="deterministic",
        )

    def _handler_for(self, validator: str, code: str):
        table = {
            ("engineering-report", "TAG_MISSING"): self._repair_tag_missing,
            ("engineering-report", "TAG_DUPLICATE"): self._repair_tag_duplicate,
            ("engineering-report", "LINE_TAG_MISSING"): self._repair_connector_metadata,
            ("engineering-report", "LINE_MEDIUM_MISSING"): self._repair_connector_metadata,
            ("engineering-report", "LINE_DIAMETER_MISSING"): self._repair_connector_metadata,
            ("engineering-report", "CONNECTOR_ENDPOINT_DANGLING"): self._repair_endpoint,
            ("engineering-report", "CONNECTOR_ENDPOINT_PORT_MISSING"): self._repair_endpoint,
            ("engineering-report", "CONNECTOR_ENDPOINT_POINT_MISMATCH"): self._repair_endpoint,
            ("engineering-report", "SYMBOL_REQUIRED_PORT_UNCONNECTED"): self._repair_required_port,
            ("engineering-report", "SYMBOL_DEFINITION_MISSING"): self._repair_symbol_definition,
            ("engineering-graph", "IR_SYMBOL_DEFINITION_MISSING"): self._repair_symbol_definition,
            ("diagram-quality", "DUPLICATE_LABEL"): self._repair_duplicate_label,
            ("diagram-quality", "NODE_OVERLAP"): self._repair_node_overlap,
            ("diagram-quality", "ANNOTATION_OVERLAP"): self._repair_annotation_overlap,
            ("diagram-quality", "UNBRIDGED_CROSSING"): self._repair_unbridged_crossing,
            ("diagram-quality", "PORT_DIRECTION_MISMATCH"): self._repair_port_direction,
            ("diagram-quality", "SYMBOL_OUT_OF_BOUNDS"): self._repair_symbol_out_of_bounds,
        }
        if (validator, code) in table:
            return table[(validator, code)]
        if validator == "diagram-quality" and code in ROUTING_CODES:
            return self._repair_connector_route
        if validator == "diagram-quality" and code == "CONNECTOR_OUT_OF_BOUNDS":
            return self._repair_connector_route
        return None

    # -- F1 identity / metadata ------------------------------------------ #

    def _repair_tag_missing(self, context: RepairContext) -> list[Any]:
        element = self._target_element(context)
        if not isinstance(element, SymbolElement):
            return []
        tag = str(element.properties.get("tag") or element.label or "").strip()
        if tag:
            # The report flags the *property*; a label alone is not a tag.
            tag = ""
        derived = self._derive_tag(context, element)
        patch: dict[str, Any] = {"properties": {**element.properties, "tag": derived}}
        if not element.label.strip():
            patch["label"] = derived
        return [UpdateElementOperation(element_id=element.id, patch=patch)]

    def _repair_tag_duplicate(self, context: RepairContext) -> list[Any]:
        element = self._target_element(context)
        if not isinstance(element, SymbolElement):
            return []
        current = str(element.properties.get("tag") or "").strip()
        derived = f"{current or 'TAG'}-{_unique_suffix(element.id)}"
        patch: dict[str, Any] = {"properties": {**element.properties, "tag": derived}}
        if element.label.strip() == current:
            patch["label"] = derived
        return [UpdateElementOperation(element_id=element.id, patch=patch)]

    def _repair_duplicate_label(self, context: RepairContext) -> list[Any]:
        """De-duplicate a label by deriving a stable suffix from the element's own id.

        The case's postcondition is uniqueness, not a specific string: inventing the drawing's
        "real" label would be inventing knowledge the document does not contain.
        """

        element = self._target_element(context)
        if isinstance(element, TextElement):
            label = element.text.strip()
            if not label:
                return []
            return [
                UpdateElementOperation(
                    element_id=element.id, patch={"text": f"{label}-{_unique_suffix(element.id)}"}
                )
            ]
        if isinstance(element, SymbolElement):
            label = element.label.strip()
            if not label:
                return []
            return [
                UpdateElementOperation(
                    element_id=element.id, patch={"label": f"{label}-{_unique_suffix(element.id)}"}
                )
            ]
        return []

    def _derive_tag(self, context: RepairContext, element: SymbolElement) -> str:
        base = element.symbol_key.replace("_", "-").upper() or "TAG"
        # Count same-symbol equipment that already carries a tag, so the derived number is a
        # function of the drawing rather than of the planner's mood.
        index = 0
        for other in context.symbols():
            if other.id >= element.id:
                continue
            if other.symbol_key == element.symbol_key and str(
                other.properties.get("tag", "")
            ).strip():
                index += 1
        return f"{base}-{index + 1:03d}"

    def _repair_connector_metadata(self, context: RepairContext) -> list[Any]:
        connector = self._target_connector(context)
        if connector is None:
            return []
        code = context.target.code
        attribute = CONNECTOR_METADATA_CODES.get(code)
        if attribute is None:
            return []
        value = self._derive_line_attribute(context, connector, attribute)
        if not value:
            return []
        return [UpdateElementOperation(element_id=connector.id, patch={attribute: value})]

    def _derive_line_attribute(
        self, context: RepairContext, connector: ConnectorElement, attribute: str
    ) -> str:
        """A deterministic, plausible line attribute derived from the connector's neighbours.

        The case's postcondition is "the attribute is present and consistent", not "it is one
        specific string": inventing the project's real line number would be pretending to
        knowledge the drawing does not contain.
        """

        element_map_local = element_map(context.document)
        if attribute == "process_tag":
            source = element_map_local.get(connector.source.element_id) if connector.source else None
            target = element_map_local.get(connector.target.element_id) if connector.target else None
            def _tag(element: Element | None) -> str:
                if isinstance(element, SymbolElement):
                    return str(element.properties.get("tag") or element.label or element.id)
                return element.id if element is not None else "OPEN"
            return f"L-{_tag(source)}-{_tag(target)}"
        if attribute == "medium":
            return "process"
        if attribute == "nominal_diameter":
            return "DN50"
        return ""

    # -- F2 endpoint / connectivity -------------------------------------- #

    def _repair_endpoint(self, context: RepairContext) -> list[Any]:
        """Re-bind every dangling endpoint of the target's finding class inside the scope.

        Repairing the whole class rather than the single named connector is what makes a
        multi-connector case solvable in one plan: when equipment is removed, every line that
        pointed at it is broken, and a repair that fixes one of them has not repaired the
        drawing.
        """

        operations: list[Any] = []
        free_ends: list[FreeEnd] = []
        for connector in self._connectors_for_class(context, {"CONNECTOR_ENDPOINT_DANGLING"}):
            for endpoint_name in ("source", "target"):
                if not self._endpoint_broken(context, connector, endpoint_name):
                    continue
                anchor = self._endpoint_anchor(connector, endpoint_name)
                binding = self._best_port_binding(context, connector, endpoint_name, anchor)
                if binding is not None:
                    operations.append(
                        ReconnectConnectorOperation(
                            connector_id=connector.id,
                            endpoint=endpoint_name,
                            element_id=binding[0],
                            port_id=binding[1],
                        )
                    )
                    continue
                # Nothing free within reach: the equipment this line pointed at is *gone*, so the
                # repair has to put equipment back rather than stretch the line to whatever
                # happens to be nearby.
                free_ends.append(
                    FreeEnd(
                        connector_id=connector.id,
                        endpoint=endpoint_name,
                        anchor=anchor,
                        # A line arriving at the missing device needs an inlet; the end a line
                        # leaves from needs an outlet. Which one this is follows from the
                        # endpoint, not from taste.
                        direction="in" if endpoint_name == "target" else "out",
                    )
                )
        if not free_ends:
            return operations
        if not context.request.scope.permits_creation:
            # Putting the device back means adding an element, and this case never granted that
            # right. Leaving the lines dangling is the honest answer; guessing is not.
            raise RepairDeclined(
                "human_required",
                "the missing equipment has to be recreated, which this case does not permit",
                code="creation_not_permitted",
            )
        for cluster in self._cluster_free_ends(context, free_ends):
            operations.extend(self._rebuild_equipment(context, cluster))
        return operations

    def _cluster_free_ends(
        self, context: RepairContext, free_ends: list[FreeEnd]
    ) -> list[list[FreeEnd]]:
        """Group free line ends that the *same* piece of missing equipment used to serve.

        Two ends belong together when they sit on the same missing device — one arriving, one
        leaving — which is exactly the case in which one replacement satisfies both. Clustering
        is single-linkage on distance with a fixed radius, ordered by anchor, so the grouping is
        a function of the drawing rather than of the order the connectors were walked in.
        """

        radius = self._binding_radius(context)
        remaining = sorted(free_ends, key=lambda end: (round(end.anchor.x, 3), round(end.anchor.y, 3), end.connector_id))
        clusters: list[list[FreeEnd]] = []
        while remaining:
            seed = remaining.pop(0)
            cluster = [seed]
            changed = True
            while changed:
                changed = False
                for candidate in list(remaining):
                    if any(_distance(candidate.anchor, member.anchor) <= radius for member in cluster):
                        cluster.append(candidate)
                        remaining.remove(candidate)
                        changed = True
            clusters.append(sorted(cluster, key=lambda end: (end.direction, round(end.anchor.x, 3), round(end.anchor.y, 3), end.connector_id)))
        return clusters

    def _binding_radius(self, context: RepairContext) -> float:
        return max(5.0, context.document.canvas.grid_size) * LOCAL_BINDING_RADIUS

    def _rebuild_equipment(self, context: RepairContext, cluster: list[FreeEnd]) -> list[Any]:
        """Add one replacement device that serves every free end of the cluster.

        Position is *solved*, not invented: each free end records where the missing port was, and
        a candidate placement is kept only when re-computing every port point from it reproduces,
        exactly, the anchors the drawing still has. A placement that cannot do that is not a
        reconstruction of the removed device and is refused.
        """

        requirements = sorted({end.direction for end in cluster})
        other_element = self._other_end_element(context, cluster[0].connector_id)
        key = self._replacement_symbol_key(context, other_element, requirements)
        if not key:
            return []
        definition = context.registry.get(key)
        assignments = self._assign_ports(definition, cluster)
        if assignments is None:
            return []
        paired = list(zip(cluster, assignments, strict=True))
        position = self._solve_position(definition, paired)
        if position is None:
            return []
        anchor = cluster[0].anchor
        orphan_annotations: list[Any] = []
        inherited = self._inherited_identity(context, anchor)
        if inherited is not None:
            element_id, tag, annotation_ids = inherited
            # The replacement carries the tag the drawing still remembers, which is what the
            # engineering report reads. The orphaned annotation that displayed it is deleted in
            # the same transaction: the label now lives on the element again, and leaving the
            # generated text behind would print the tag twice.
            label = tag
            orphan_annotations = [
                SafeDeleteElementOperation(element_id=annotation_id)
                for annotation_id in annotation_ids
            ]
        else:
            element_id = f"repair_eq_{_unique_suffix(f'{anchor.x:g}:{anchor.y:g}:{key}')}"
            if context.element(element_id) is not None:
                return []
            tag = self._derive_replacement_tag(context, key, element_id)
            label = tag
        replacement = SymbolElement(
            id=element_id,
            symbol_key=key,
            position=position,
            width=definition.width,
            height=definition.height,
            label=label,
            properties={"tag": tag},
        )
        operations: list[Any] = [AddElementOperation(element=replacement), *orphan_annotations]
        operations.extend(
            ReconnectConnectorOperation(
                connector_id=end.connector_id,
                endpoint=end.endpoint,
                element_id=element_id,
                port_id=port_id,
            )
            for end, port_id in paired
        )
        return operations

    def _inherited_identity(
        self, context: RepairContext, anchor: Point
    ) -> tuple[str, str, list[str]] | None:
        """The id, tag and leftover annotations of the device that was removed.

        Deleting equipment leaves its generated label behind. That orphaned annotation *is* the
        drawing's record of what stood here: it names the parent element the label was drawn for
        and the tag that was displayed. So the replacement can take the id and the tag rather
        than inventing a new identity with no label at all — an element with no label is a
        drawing where the device has lost its number.

        The ids come back with the identity because the annotations are the *same* repair: once
        the element carries the tag again, the generated text that was displaying it is a second
        copy of that tag, so the plan replaces one with the other instead of leaving both.

        Nothing else in the drawing is trusted to remember a deleted element. An annotation whose
        parent still exists is not an orphan, and one further from the missing port than the
        binding radius is not describing this device.
        """

        present = set(element_map(context.document))
        radius = self._binding_radius(context)
        candidates: list[tuple[float, str, str]] = []
        leftovers: dict[str, list[str]] = {}
        for element in context.document.elements:
            if not isinstance(element, TextElement):
                continue
            parent_id = element.metadata.get("parent_element_id")
            if not isinstance(parent_id, str) or not parent_id or parent_id in present:
                continue
            text = element.text.strip()
            if not text:
                continue
            leftovers.setdefault(parent_id, []).append(element.id)
            distance = _distance(anchor, element.position)
            if distance > radius:
                continue
            candidates.append((round(distance, 3), parent_id, text))
        if not candidates:
            return None
        _, parent_id, tag = sorted(candidates)[0]
        return parent_id, tag, sorted(leftovers.get(parent_id, []))

    def _assign_ports(self, definition: Any, cluster: list[FreeEnd]) -> list[str] | None:
        """Which port of the replacement each free end binds to.

        Preference is by direction (in/out), then by a bidirectional port, then by any port still
        unused — each of those three is a rule about the drawing's own port semantics. A cluster
        that cannot be satisfied that way is not repairable here.
        """

        by_direction: dict[str, list[str]] = {}
        for port in sorted(definition.ports, key=lambda item: item.id):
            by_direction.setdefault(port.direction, []).append(port.id)
        assignments: list[str] = []
        used: set[str] = set()
        for end in cluster:
            candidates = [
                port_id
                for port_id in by_direction.get(end.direction, [])
                if port_id not in used
            ]
            if not candidates:
                candidates = [
                    port_id
                    for port_id in by_direction.get("bidirectional", [])
                    if port_id not in used
                ]
            if not candidates:
                candidates = [
                    port.id for port in sorted(definition.ports, key=lambda item: item.id) if port.id not in used
                ]
            if not candidates:
                return None
            assignments.append(candidates[0])
            used.add(candidates[0])
        return assignments

    def _solve_position(
        self, definition: Any, assignments: list[tuple[FreeEnd, str]]
    ) -> Point | None:
        """The placement whose port points are the anchors the drawing still holds.

        Computed from the first assignment and then *verified* against all of them: the drawing
        is the authority on where the device stood, and a placement that only roughly fits is a
        different drawing, not a repair.
        """

        from .drafting_geometry import symbol_port_point

        first_end, first_port_id = assignments[0]
        port = next((item for item in definition.ports if item.id == first_port_id), None)
        if port is None:
            return None
        # The replacement is created at the catalogue size, so the port offset is the offset.
        position = Point(x=first_end.anchor.x - port.x, y=first_end.anchor.y - port.y)
        probe = SymbolElement(
            id="repair_probe",
            symbol_key=definition.key,
            position=position,
            width=definition.width,
            height=definition.height,
        )
        for end, port_id in assignments:
            candidate = next((item for item in definition.ports if item.id == port_id), None)
            if candidate is None:
                return None
            point = symbol_port_point(probe, candidate, definition)
            if abs(point.x - end.anchor.x) > 1e-6 or abs(point.y - end.anchor.y) > 1e-6:
                return None
        return position

    def _other_end_element(self, context: RepairContext, connector_id: str) -> Element | None:
        connector = next(
            (item for item in context.connectors() if item.id == connector_id), None
        )
        if connector is None:
            return None
        elements = element_map(context.document)
        for endpoint in (connector.source, connector.target):
            if endpoint is None or not endpoint.element_id:
                continue
            element = elements.get(endpoint.element_id)
            if isinstance(element, SymbolElement):
                return element
        return None

    def _endpoint_broken(
        self, context: RepairContext, connector: ConnectorElement, endpoint_name: str
    ) -> bool:
        endpoint = getattr(connector, endpoint_name)
        if endpoint is None or not endpoint.element_id or not endpoint.port_id:
            return True
        element = context.element(endpoint.element_id)
        if not isinstance(element, SymbolElement):
            return True
        try:
            definition = context.registry.get(element.symbol_key)
        except KeyError:
            return True
        port = next((port for port in definition.ports if port.id == endpoint.port_id), None)
        if port is None:
            return True
        from .drafting_geometry import symbol_port_point

        expected = symbol_port_point(element, port, definition)
        return (
            abs(expected.x - endpoint.point.x) > 1e-6
            or abs(expected.y - endpoint.point.y) > 1e-6
        )

    @staticmethod
    def _endpoint_anchor(connector: ConnectorElement, endpoint_name: str) -> Point:
        endpoint = getattr(connector, endpoint_name)
        if endpoint is not None and endpoint.point is not None:
            return endpoint.point
        return connector.points[0] if endpoint_name == "source" else connector.points[-1]

    def _best_port_binding(
        self,
        context: RepairContext,
        connector: ConnectorElement,
        endpoint_name: str,
        anchor: Point,
    ) -> tuple[str, str] | None:
        """The deterministic best *free* port for a broken endpoint, or ``None``.

        Two rules, both mechanical:

        * the port must be free — re-binding to a port another line already owns would trade
          one defect for another;
        * it must be within :data:`LOCAL_BINDING_RADIUS` grid units of where the line now
          ends. Beyond that the line is not "attached to the wrong thing", it is attached to
          nothing, and the honest repair is to replace the missing equipment.

        Ties break by ``(element_id, port_id)``, so the choice never depends on iteration
        order or on which connector was repaired first.
        """

        endpoint = getattr(connector, endpoint_name)
        allowed = set(context.request.scope.allowed_element_ids)
        preferred_element = (
            endpoint.element_id if endpoint is not None and endpoint.element_id in allowed else None
        )
        radius = max(5.0, context.document.canvas.grid_size) * LOCAL_BINDING_RADIUS
        own = {
            (endpoint.element_id, endpoint.port_id)
            for endpoint in (connector.source, connector.target)
            if endpoint is not None and endpoint.element_id and endpoint.port_id
        }
        bound_by_others = self._bound_pairs(context) - own
        candidates: list[tuple[int, float, str, str]] = []
        for element in context.symbols():
            if element.id not in allowed:
                continue
            try:
                definition = context.registry.get(element.symbol_key)
            except KeyError:
                continue
            from .drafting_geometry import symbol_port_point

            for port in definition.ports:
                if (element.id, port.id) in bound_by_others:
                    continue
                point = symbol_port_point(element, port, definition)
                distance = _distance(anchor, point)
                if distance > radius:
                    continue
                candidates.append(
                    (
                        0 if preferred_element == element.id else 1,
                        distance,
                        element.id,
                        port.id,
                    )
                )
        if not candidates:
            return None
        _, _, element_id, port_id = sorted(candidates)[0]
        return element_id, port_id

    def _bound_pairs(self, context: RepairContext) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for connector in context.connectors():
            for endpoint in (connector.source, connector.target):
                if endpoint is not None and endpoint.element_id and endpoint.port_id:
                    pairs.add((endpoint.element_id, endpoint.port_id))
        return pairs

    def _replacement_symbol_key(
        self, context: RepairContext, other_element: Element | None, requirements: list[str]
    ) -> str:
        """Pick replacement equipment with exactly the ports the broken lines need.

        The neighbour's type is preferred *when it fits*, because "the same kind of device stood
        here" is the only defensible guess the drawing supports. Otherwise the smallest catalogue
        symbol that exposes exactly the required port directions wins, ties broken by key.
        """

        def _fits(key: str) -> bool:
            try:
                definition = context.registry.get(key)
            except KeyError:
                return False
            if not definition.ports:
                return False
            directions = {port.direction for port in definition.ports}
            if len(definition.ports) != len(requirements):
                return False
            return all(
                requirement in directions or "bidirectional" in directions
                for requirement in requirements
            )

        if isinstance(other_element, SymbolElement) and _fits(other_element.symbol_key):
            return other_element.symbol_key
        candidates = sorted(
            definition.key for definition in context.registry.list() if _fits(definition.key)
        )
        if candidates:
            return candidates[0]
        if isinstance(other_element, SymbolElement):
            return other_element.symbol_key
        fallback = [
            definition.key for definition in context.registry.list() if definition.ports
        ]
        return sorted(fallback)[0] if fallback else ""

    def _derive_replacement_tag(
        self, context: RepairContext, key: str, element_id: str
    ) -> str:
        base = key.replace("_", "-").upper() or "TAG"
        existing = {
            str(element.properties.get("tag", "")).strip()
            for element in context.symbols()
        }
        existing |= {element.label.strip() for element in context.symbols()}
        index = 1
        while f"{base}-{index:03d}" in existing:
            index += 1
        return f"{base}-{index:03d}"

    def _repair_required_port(self, context: RepairContext) -> list[Any]:
        """Attach a line to a required port that has nothing on it.

        Preference order is mechanical: a connector inside the scope whose free end is nearest
        to the port, then — if the drawing has no free end at all — a new connector to the
        nearest other free port, because the missing thing is the connection itself.
        """

        operations: list[Any] = []
        for finding in self._matching_findings(context, {"SYMBOL_REQUIRED_PORT_UNCONNECTED"}):
            element = context.element(finding.element_ids[0]) if finding.element_ids else None
            if not isinstance(element, SymbolElement):
                continue
            port_id = str(finding.details.get("port_id", ""))
            if not port_id:
                free = self._free_port(context, element)
                port_id = free or ""
            if not port_id:
                continue
            try:
                definition = context.registry.get(element.symbol_key)
                port = next(port for port in definition.ports if port.id == port_id)
            except (KeyError, StopIteration):
                continue
            from .drafting_geometry import symbol_port_point

            target_point = symbol_port_point(element, port, definition)
            free_connector = self._nearest_free_connector(context, target_point)
            if free_connector is not None:
                operations.append(
                    ReconnectConnectorOperation(
                        connector_id=free_connector[0],
                        endpoint=free_connector[1],
                        element_id=element.id,
                        port_id=port_id,
                    )
                )
        return operations

    def _nearest_free_connector(
        self, context: RepairContext, target: Point
    ) -> tuple[str, str] | None:
        allowed = set(context.request.scope.allowed_element_ids)
        ranked: list[tuple[float, str, str]] = []
        for connector in context.connectors():
            if connector.id not in allowed:
                continue
            for endpoint_name in ("source", "target"):
                endpoint = getattr(connector, endpoint_name)
                if endpoint is not None and endpoint.element_id:
                    continue
                anchor = self._endpoint_anchor(connector, endpoint_name)
                ranked.append((_distance(anchor, target), connector.id, endpoint_name))
        if not ranked:
            return None
        ranked.sort()
        return ranked[0][1], ranked[0][2]

    def _free_port(self, context: RepairContext, element: SymbolElement) -> str | None:
        try:
            definition = context.registry.get(element.symbol_key)
        except KeyError:
            return None
        bound = self._bound_ports(context, element.id)
        free = [port.id for port in definition.ports if port.id not in bound]
        return sorted(free)[0] if free else None

    def _bound_ports(self, context: RepairContext, element_id: str) -> set[str]:
        return {
            port_id for candidate_id, port_id in self._bound_pairs(context) if candidate_id == element_id
        }

    def _class_findings(
        self, context: RepairContext, codes: set[str]
    ) -> list[ValidationIssue]:
        """Findings of the target's class from the whole result, inside the frozen scope.

        Same validator, same code, and at least one locator inside the scope, resolved against
        *every* canonical finding rather than only the target's own. This is the view a
        class-wide repair needs; handlers that act on exactly one finding use
        :meth:`_matching_findings` instead.
        """

        allowed = set(context.request.scope.allowed_element_ids)
        target = context.request.target
        pool = context.class_findings or context.findings
        return sorted(
            (
                finding
                for finding in pool
                if finding.validator_id == target.validator_id
                and finding.code in codes
                and not finding.is_waived
                and (not finding.element_ids or set(finding.element_ids) & allowed)
            ),
            key=lambda item: (item.code, tuple(item.element_ids)),
        )

    def _matching_findings(
        self, context: RepairContext, codes: set[str]
    ) -> list[ValidationIssue]:
        """The target's own findings of these codes, inside the frozen scope.

        One finding, one handler: the default view for a handler whose repair acts on the
        finding the release gate named.
        """

        allowed = set(context.request.scope.allowed_element_ids)
        target = context.request.target
        return sorted(
            (
                finding
                for finding in context.findings
                if finding.validator_id == target.validator_id
                and finding.code in codes
                and not finding.is_waived
                and (not finding.element_ids or set(finding.element_ids) & allowed)
            ),
            key=lambda item: (item.code, tuple(item.element_ids)),
        )

    def _connectors_for_class(
        self, context: RepairContext, codes: set[str]
    ) -> list[ConnectorElement]:
        connectors: dict[str, ConnectorElement] = {}
        for finding in self._class_findings(context, codes):
            for element_id in finding.element_ids:
                element = context.element(element_id)
                if isinstance(element, ConnectorElement):
                    connectors[element.id] = element
        if not connectors and codes == {"CONNECTOR_ENDPOINT_DANGLING"}:
            target_connector = self._target_connector(context)
            if target_connector is not None:
                connectors[target_connector.id] = target_connector
        return [connectors[key] for key in sorted(connectors)]

    # -- F3 replacement -------------------------------------------------- #

    def _repair_symbol_definition(self, context: RepairContext) -> list[Any]:
        """Restore a symbol whose key is not in the catalog, keeping its identity.

        Selection is mechanical: the replacement is the catalog symbol that explains the
        connectors already attached to this element — the one with the most port ids in
        common, then the fewest extra ports, then the smallest key. The element id, position,
        size and rotation are preserved by ``replace_symbol``, and ports that exist under a
        different name are mapped explicitly, because silently rebinding a connector to
        "some port" is how a repair changes engineering meaning.
        """

        element = self._target_element(context)
        if not isinstance(element, SymbolElement):
            return []
        attached = self._attached_port_ids(context, element.id)
        best: tuple[int, int, str, dict[str, str]] | None = None
        for definition in context.registry.list():
            catalogue_ports = {port.id for port in definition.ports}
            if not catalogue_ports:
                continue
            matched = attached & catalogue_ports
            if attached and not matched:
                continue
            mapping = {port_id: port_id for port_id in sorted(matched)}
            unresolved = sorted(attached - catalogue_ports)
            if unresolved and len(catalogue_ports) < len(attached):
                continue
            # Ports the element's own connectors use that the catalogue does not define are
            # mapped onto the closest unused catalogue port, deterministically.
            spare = sorted(catalogue_ports - set(mapping.values()))
            for missing in unresolved:
                if not spare:
                    mapping = {}
                    break
                mapping[missing] = spare.pop(0)
            if not mapping and attached:
                continue
            extra = len(catalogue_ports) - len(set(mapping.values()))
            score = (-len(matched), extra, definition.key, mapping)
            if best is None or score[:3] < best[:3]:
                best = score
        if best is None:
            return []
        _, _, symbol_key, port_mapping = best
        return [
            ReplaceSymbolOperation(
                element_id=element.id,
                symbol_key=symbol_key,
                port_mapping=port_mapping,
                preserve_size=True,
            )
        ]

    def _attached_port_ids(self, context: RepairContext, element_id: str) -> set[str]:
        port_ids: set[str] = set()
        for connector in context.connectors():
            for endpoint in (connector.source, connector.target):
                if endpoint is not None and endpoint.element_id == element_id and endpoint.port_id:
                    port_ids.add(endpoint.port_id)
        return port_ids

    # -- F4 routing ------------------------------------------------------ #

    def _repair_connector_route(self, context: RepairContext) -> list[Any]:
        connector = self._target_connector(context)
        if connector is None or connector.source is None or connector.target is None:
            return []
        points = route_connector_points(context.document, connector, context.registry)
        if len(points) < 2:
            return []
        return [
            UpdateElementOperation(
                element_id=connector.id,
                patch={
                    "routing": "manual",
                    "points": [point.model_dump(mode="json") for point in points],
                },
            )
        ]

    def _repair_port_direction(self, context: RepairContext) -> list[Any]:
        """Make the connector's declared flow agree with its ports.

        Only the "declared direction contradicts the ports" half is repairable mechanically:
        when two in-ports are wired together there is no flow to agree with, and inventing one
        would be a design decision, not a repair. The planner declines in that case.
        """

        connector = self._target_connector(context)
        if connector is None:
            return []
        from .diagram_quality import infer_flow_direction

        inferred = infer_flow_direction(context.document, connector, context.registry)
        if inferred == "none":
            raise RepairDeclined(
                "human_required",
                "the connector's ports do not determine a flow direction; a human must decide",
                code="ambiguous_flow_direction",
            )
        return [UpdateElementOperation(element_id=connector.id, patch={"flow_direction": inferred})]

    # -- F5 collisions and annotations ----------------------------------- #

    def _repair_node_overlap(self, context: RepairContext) -> list[Any]:
        """Separate two overlapping nodes by the smallest grid-aligned displacement.

        The element that moves is the lexicographically greater id, so the same drawing always
        yields the same repair, and the displacement is computed from the real bounding boxes
        rather than from a fixed nudge.
        """

        pairs = sorted(
            {
                tuple(sorted(issue.element_ids))
                for issue in context.findings
                if issue.code == "NODE_OVERLAP" and len(issue.element_ids) >= 2
            }
        )
        target_ids = set(context.request.target.element_ids)
        pair = next(
            (candidate for candidate in pairs if target_ids & set(candidate)),
            pairs[0] if pairs else None,
        )
        if pair is None:
            return []
        mover = context.element(pair[-1])
        other = context.element(pair[0])
        if mover is None or other is None:
            return []
        mover_rect = _element_rect(mover)
        other_rect = _element_rect(other)
        if mover_rect is None or other_rect is None or not hasattr(mover, "position"):
            return []
        grid = max(5.0, context.document.canvas.grid_size)
        overlap_x = min(mover_rect.x2, other_rect.x2) - max(mover_rect.x1, other_rect.x1)
        overlap_y = min(mover_rect.y2, other_rect.y2) - max(mover_rect.y1, other_rect.y1)
        position = mover.position
        if overlap_x <= overlap_y:
            delta_x = math.ceil((overlap_x + grid) / grid) * grid
            direction = 1 if position.x >= (other_rect.x1 + other_rect.x2) / 2 else -1
            new_position = Point(x=position.x + direction * delta_x, y=position.y)
        else:
            delta_y = math.ceil((overlap_y + grid) / grid) * grid
            direction = 1 if position.y >= (other_rect.y1 + other_rect.y2) / 2 else -1
            new_position = Point(x=position.x, y=position.y + direction * delta_y)
        return [
            UpdateElementOperation(element_id=mover.id, patch={"position": new_position.model_dump(mode="json")})
        ]

    def _repair_annotation_overlap(self, context: RepairContext) -> list[Any]:
        texts = sorted(
            (element for element in context.document.elements if isinstance(element, TextElement)),
            key=lambda item: item.id,
        )
        if len(texts) < 2:
            return []
        mover = texts[-1]
        grid = max(5.0, context.document.canvas.grid_size)
        new_position = Point(x=mover.position.x, y=mover.position.y + grid * 6)
        return [
            UpdateElementOperation(
                element_id=mover.id, patch={"position": new_position.model_dump(mode="json")}
            )
        ]

    def _repair_symbol_out_of_bounds(self, context: RepairContext) -> list[Any]:
        element = self._target_element(context)
        if not isinstance(element, SymbolElement):
            return []
        canvas = context.document.canvas
        grid = max(5.0, canvas.grid_size)
        margin = grid * 4
        rect = _element_rect(element)
        if rect is None:
            return []
        x = element.position.x
        y = element.position.y
        if rect.x1 < 0:
            x += math.ceil((-rect.x1 + margin) / grid) * grid
        if rect.y1 < 0:
            y += math.ceil((-rect.y1 + margin) / grid) * grid
        if rect.x2 > canvas.width:
            x -= math.ceil((rect.x2 - canvas.width + margin) / grid) * grid
        if rect.y2 > canvas.height:
            y -= math.ceil((rect.y2 - canvas.height + margin) / grid) * grid
        if (x, y) == (element.position.x, element.position.y):
            return []
        return [UpdateElementOperation(element_id=element.id, patch={"position": {"x": x, "y": y}})]

    def _repair_unbridged_crossing(self, context: RepairContext) -> list[Any]:
        connectors = {connector.id: connector for connector in context.connectors()}
        candidates = sorted(
            {
                element_id
                for issue in context.findings
                if issue.code == "UNBRIDGED_CROSSING"
                for element_id in issue.element_ids
            }
            & set(connectors)
        )
        target_ids = [
            element_id
            for element_id in context.request.target.element_ids
            if element_id in connectors
        ]
        chosen = (target_ids or candidates)[:1]
        if not chosen:
            return []
        return [
            UpdateElementOperation(
                element_id=chosen[0], patch={"crossing_style": "jump", "routing": "manual"}
            )
        ]

    # -- lookups --------------------------------------------------------- #

    def _target_element(self, context: RepairContext) -> Element | None:
        for element_id in context.request.target.element_ids:
            element = context.element(element_id)
            if element is not None:
                return element
        return None

    def _target_connector(self, context: RepairContext) -> ConnectorElement | None:
        element = self._target_element(context)
        if isinstance(element, ConnectorElement):
            return element
        return None

    def _connector_for_element_id(self, context: RepairContext) -> ConnectorElement | None:
        for finding in context.findings:
            for element_id in finding.element_ids:
                element = context.element(element_id)
                if isinstance(element, ConnectorElement):
                    return element
        return None



__all__ = [
    "CONNECTOR_METADATA_CODES",
    "PLANNER_VERSION",
    "ROUTING_CODES",
    "LOCAL_BINDING_RADIUS",
    "DeterministicRepairPlanner",
    "RepairContext",
    "RepairDeclined",
    "RepairPlanDraft",
    "RepairPlanner",
    "build_repair_context",
]
