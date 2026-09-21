"""The M5 benchmark spec: base drawings, defect mutations, case derivation (baseline §B).

The gate this module implements is "can the agent repair *these* defects", so the defects
have to be produced the way real defective drawings are produced:

* **A mutation is a real edit.** Every operator writes through
  ``DocumentService.apply_transaction`` (or adds elements through it), so the defective state
  is a governed revision with history and audit — not a hand-edited JSON blob. That also keeps
  the benchmark honest about what the repository can actually represent: a defect that no
  governed operation can produce is not a defect this system can encounter in service.
* **A mutation only breaks one thing.** Each operator declares the one canonical finding it
  is buying, and the case records the base result so a reader can see the rest of the drawing
  was already in that state.
* **The seed is derived, not chosen.** Acceptance cases derive their seed from the benchmark
  spec fingerprint, the candidate commit SHA, the family and the case index, so a different
  candidate gets a different case set, and a case cannot be quietly swapped for an easier one.

The repair for each operator is decided by the *planner* from the finding and the drawing;
nothing in this module tells the planner what to do, which is what keeps S@5 a measurement.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .agent_semantic_models import SemanticTransaction
from .models import (
    AddElementOperation,
    ConnectorElement,
    CreateDocumentRequest,
    DeleteElementOperation,
    Point,
    SymbolElement,
    TextElement,
    TransactionRequest,
    UpdateElementOperation,
)
from .service import DocumentService
from .symbols import SymbolRegistry

#: Bumped only with a documented `benchmark reset` (baseline §B4).
BENCHMARK_SPEC_VERSION = "1"

#: Offline oracle version. Changing the oracle invalidates every previous number.
SUCCESS_ORACLE_VERSION = "1"

FAMILIES: tuple[str, ...] = ("F1", "F2", "F3", "F4", "F5", "F6")

FAMILY_TITLES: dict[str, str] = {
    "F1": "identity / metadata",
    "F2": "endpoint / connectivity",
    "F3": "replacement / multi-connector reconnection",
    "F4": "routing / port geometry",
    "F5": "local collision / drafting region",
    "F6": "replan / conflict robustness",
}

#: Frozen thresholds (baseline §A3, §A4). Kept here, next to the spec they gate, so the
#: published numbers and the gate they are compared against cannot drift apart.
THRESHOLDS: dict[str, float] = {
    "s5_overall": 0.90,
    "s5_family_min": 0.75,
    "s1_overall": 0.60,
    "safety_suite": 1.0,
    "model_s5_overall": 0.80,
    "model_family_min": 0.50,
}

DEV_CASES_PER_FAMILY = 4
ACCEPTANCE_CASES_PER_FAMILY = 12
SAFETY_CASES = 12


# -- base drawing ------------------------------------------------------------ #


class BenchmarkSetupError(RuntimeError):
    """A mutation could not be applied at all: a generator defect, not a repair failure.

    Deliberately *not* a dataclass: the message is the point (which mutation, and why the
    compiler refused it), and a fields-less dataclass exception cannot carry one.
    """


@dataclass
class BaseDrawing:
    """The clean-ish drawing every case starts from, addressed by role.

    Roles exist so an operator never depends on a literal id: the same operator runs against
    the dev suite and the acceptance suite, and against the real large drawing in the scale
    track, without rewriting ids.
    """

    document_id: str
    elements: dict[str, str] = field(default_factory=dict)

    def __getitem__(self, role: str) -> str:
        return self.elements[role]


def _symbol(
    element_id: str,
    registry: SymbolRegistry,
    key: str,
    x: float,
    y: float,
    *,
    label: str = "",
) -> SymbolElement:
    definition = registry.get(key)
    return SymbolElement(
        id=element_id,
        symbol_key=definition.key,
        position=Point(x=x, y=y),
        width=definition.width,
        height=definition.height,
        label=label,
        properties={"tag": label} if label else {},
    )


def build_base_drawing(
    service: DocumentService,
    registry: SymbolRegistry,
    *,
    name: str = "M5 benchmark base",
    variant: str = "standard",
) -> BaseDrawing:
    """Create the base drawing: two valves joined by a pipe, plus a note.

    Deliberately small. A benchmark whose cases need a 400-element drawing to be interesting
    is a benchmark nobody can reproduce; the large-drawing behaviour is measured separately by
    the scale track (baseline §G), which injects the same operators into a real P&ID.

    The base is *not* claimed to be finding-free: both valve inlets/outlets that are not part
    of the trunk stay unconnected, which is a legitimate drafting state. Every case records the
    base validation result, and the oracle only ever compares a candidate against *that* base.
    """

    from .agent_semantic_models import ConnectPortsOperation

    document = service.create_document(CreateDocumentRequest(name=name))
    note = TextElement(id="note1", position=Point(x=180, y=520), text="M5 benchmark note")
    operations: list[Any] = [
        AddElementOperation(element=_symbol("v1", registry, "ball_valve", 200, 300, label="HV-101")),
        AddElementOperation(element=_symbol("v2", registry, "ball_valve", 700, 300, label="HV-102")),
        # ``v3`` is deliberately isolated: collision, obstruction and out-of-bounds cases need an
        # element that can be moved without dragging a routed line along with it.
        AddElementOperation(element=_symbol("v3", registry, "ball_valve", 1150, 300, label="HV-103")),
        AddElementOperation(element=note),
        # The trunk is created through the semantic compiler, so the base drawing is exactly
        # what the production connect-ports path produces — not a hand-rolled point list that
        # happens to look orthogonal.
        ConnectPortsOperation(
            connector_id="p1",
            source_element_id="v1",
            source_port_id="out",
            target_element_id="v2",
            target_port_id="in",
            routing="orthogonal",
            process_tag="L-M5-001",
            medium="process",
            nominal_diameter="DN50",
            flow_direction="forward",
        ),
    ]
    elements = {"v1": "v1", "v2": "v2", "v3": "v3", "p1": "p1", "note1": "note1"}
    if variant == "three_valves":
        operations.extend(
            [
                ConnectPortsOperation(
                    connector_id="p2",
                    source_element_id="v2",
                    source_port_id="out",
                    target_element_id="v3",
                    target_port_id="in",
                    routing="orthogonal",
                    process_tag="L-M5-002",
                    medium="process",
                    nominal_diameter="DN50",
                    flow_direction="forward",
                ),
            ]
        )
        elements.update({"v3": "v3", "p2": "p2"})
    _apply_semantic(service, document.id, operations, "benchmark.base")
    return BaseDrawing(document_id=document.id, elements=elements)


def _apply_semantic(
    service: DocumentService, document_id: str, operations: list[Any], label: str
) -> int:
    """Compile semantic operations and apply them through the governed path.

    Every mutation in this module writes through here (or through ``_patch`` for a single
    element update), which is what makes the defective drawing a real governed revision with
    history and audit rather than a fixture that only exists in memory.
    """

    from .semantic_compiler_engine import SemanticTransactionCompiler

    document = service.get_document(document_id)
    compiled = SemanticTransactionCompiler(service).compile(
        document_id,
        SemanticTransaction(
            operations=operations, expected_revision=document.revision, label=label
        ),
    )
    if compiled.transaction is None or not compiled.assessment.valid:
        raise BenchmarkSetupError(
            f"{label} could not be compiled: "
            f"{[issue.code for issue in compiled.assessment.issues]}"
        )
    result = service.apply_transaction(
        document_id, compiled.transaction, audit=_mutation_audit(f"benchmark.{label}")
    )
    return result.document.revision


def _mutation_audit(tool_name: str) -> Any:
    from .audit_models import AuditContext

    return AuditContext(actor="benchmark-mutation", surface="internal", tool_name=tool_name)


# -- mutations --------------------------------------------------------------- #


@dataclass
class MutationResult:
    """What a mutation broke, and what the benchmark expects a repair to satisfy."""

    operator_id: str
    family: str
    target_code: str
    target_validator_id: str
    target_element_ids: list[str]
    #: Extra binding for findings that fire per element *and* per port.
    target_details: dict[str, Any] = field(default_factory=dict)
    postconditions: dict[str, Any] = field(default_factory=dict)
    declared_scope_ids: list[str] = field(default_factory=list)
    hop: int = 1
    touched_budget_class: str = "multi_connector"
    mutation_revision: int = 0
    notes: list[str] = field(default_factory=list)
    #: F6 only: how many attempts the runner should need before the honest plan is reachable.
    required_attempts: int = 1
    wrap_operator_id: str = ""


@dataclass(frozen=True)
class MutationOperator:
    operator_id: str
    family: str
    target_code: str
    target_validator_id: str
    apply: Callable[[DocumentService, BaseDrawing, random.Random], MutationResult]
    document_builder: Callable[[DocumentService, SymbolRegistry], BaseDrawing] | None = None
    touched_budget_class: str = "multi_connector"
    #: The case's write policy, declared *here* rather than derived from the mutation, so it is
    #: part of the frozen spec a reviewer reads (``_operator_catalog``) and not a side effect of
    #: running the mutation. Adding equipment is how a repair puts a removed device back (§B5);
    #: removing it is how the cheapest pseudo-repair makes a finding disappear (§C4). They are
    #: separate grants because they are separate risks.
    permits_creation: bool = False
    max_created_ids: int = 0
    permits_deletion: bool = False
    max_deleted_ids: int = 0


def _mutate(service: DocumentService, document_id: str, operations: list[Any], label: str) -> int:
    """Stage a defect as one governed mutation, so the base is a real revision."""

    document = service.get_document(document_id)
    result = service.apply_transaction(
        document_id,
        TransactionRequest(
            operations=operations,
            expected_revision=document.revision,
            label=label,
        ),
        audit=_mutation_audit(f"benchmark.{label}"),
    )
    return result.document.revision


def _patch(service: DocumentService, document_id: str, element_id: str, patch: dict[str, Any], label: str) -> int:
    return _mutate(
        service,
        document_id,
        [UpdateElementOperation(element_id=element_id, patch=patch)],
        label,
    )


def _symbol_label_annotations(service: DocumentService, document_id: str, element_id: str) -> list[str]:
    """Ids of the ``symbol_label`` annotations the production polish left for one symbol."""

    document = service.get_document(document_id)
    return sorted(
        element.id
        for element in document.elements
        if element.type == "text"
        and element.metadata.get("parent_element_id") == element_id
        and element.metadata.get("annotation_role") == "symbol_label"
    )


def _manual_points(service: DocumentService, document_id: str, connector_id: str) -> list[Point]:
    document = service.get_document(document_id)
    connector = next(item for item in document.elements if item.id == connector_id)
    assert isinstance(connector, ConnectorElement)
    return [Point.model_validate(point.model_dump()) for point in connector.points]


# -- F1 : identity / metadata ------------------------------------------------ #


def _f1_line_name(service: DocumentService, base: BaseDrawing, rng: random.Random) -> MutationResult:
    """Clear the line's *name* as well as its tag.

    The engineering report derives the line number from ``process_tag``; this operator removes
    the whole line identity so the repair has to reconstruct it from the two endpoints.
    """

    revision = _patch(
        service,
        base.document_id,
        base["p1"],
        {"process_tag": "", "name": ""},
        "f1-line-name-missing",
    )
    return MutationResult(
        operator_id="f1_line_name_missing",
        family="F1",
        target_code="LINE_TAG_MISSING",
        target_validator_id="engineering-report",
        target_element_ids=[base["p1"]],
        mutation_revision=revision,
    )


def _f1_line_tag(service: DocumentService, base: BaseDrawing, rng: random.Random) -> MutationResult:
    revision = _patch(service, base.document_id, base["p1"], {"process_tag": ""}, "f1-line-tag-missing")
    return MutationResult(
        operator_id="f1_line_tag_missing",
        family="F1",
        target_code="LINE_TAG_MISSING",
        target_validator_id="engineering-report",
        target_element_ids=[base["p1"]],
        mutation_revision=revision,
    )


def _f1_line_medium(service: DocumentService, base: BaseDrawing, rng: random.Random) -> MutationResult:
    revision = _patch(service, base.document_id, base["p1"], {"medium": ""}, "f1-line-medium-missing")
    return MutationResult(
        operator_id="f1_line_medium_missing",
        family="F1",
        target_code="LINE_MEDIUM_MISSING",
        target_validator_id="engineering-report",
        target_element_ids=[base["p1"]],
        mutation_revision=revision,
    )


def _f1_line_diameter(service: DocumentService, base: BaseDrawing, rng: random.Random) -> MutationResult:
    revision = _patch(
        service, base.document_id, base["p1"], {"nominal_diameter": ""}, "f1-line-diameter-missing"
    )
    return MutationResult(
        operator_id="f1_line_diameter_missing",
        family="F1",
        target_code="LINE_DIAMETER_MISSING",
        target_validator_id="engineering-report",
        target_element_ids=[base["p1"]],
        mutation_revision=revision,
    )


def _f1_symbol_tag_missing(
    service: DocumentService, base: BaseDrawing, rng: random.Random
) -> MutationResult:
    """Erase the equipment's tag from *every* source the drawing has for it.

    A tag is not one field. This drawing has the tag in ``properties.tag``, and the production
    polish turned the fixed label into a ``symbol_label`` annotation, so "the drawer never typed
    a tag" means all three are empty. Clearing only the property would leave the annotation to
    answer for it and the rule would be right not to fire. The isolated element is used so the
    defect is identity-only: nothing about connectivity moves, and a repair that rerouted a line
    would be fixing something this case never broke.
    """

    element_id = base["v3"]
    operations: list[Any] = [
        UpdateElementOperation(element_id=element_id, patch={"label": "", "properties": {}}),
        *(
            DeleteElementOperation(element_id=annotation_id)
            for annotation_id in _symbol_label_annotations(service, base.document_id, element_id)
        ),
    ]
    revision = _mutate(service, base.document_id, operations, "f1-symbol-tag-missing")
    return MutationResult(
        operator_id="f1_symbol_tag_missing",
        family="F1",
        target_code="TAG_MISSING",
        target_validator_id="engineering-report",
        target_element_ids=[element_id],
        mutation_revision=revision,
        notes=["the tag has to be reconstructed, because the drawing records none"],
    )


def _f1_symbol_tag_duplicate(
    service: DocumentService, base: BaseDrawing, rng: random.Random
) -> MutationResult:
    """Give the isolated equipment the tag another device already carries.

    Both sources are moved together — the property and the annotation — because a drawing whose
    annotation disagreed with its property would be a different defect (a conflicting label),
    and this case is about a duplicate tag.
    """

    target_id = base["v3"]
    document = service.get_document(base.document_id)
    other = next(element for element in document.elements if element.id == base["v2"])
    shared = str((other.properties or {}).get("tag") or other.label or "").strip()
    if not shared:
        shared = str(other.label or "").strip()
    operations: list[Any] = [
        UpdateElementOperation(
            element_id=target_id, patch={"label": shared, "properties": {"tag": shared}}
        ),
        *(
            UpdateElementOperation(element_id=annotation_id, patch={"text": shared})
            for annotation_id in _symbol_label_annotations(service, base.document_id, target_id)
        ),
    ]
    revision = _mutate(service, base.document_id, operations, "f1-symbol-tag-duplicate")
    return MutationResult(
        operator_id="f1_symbol_tag_duplicate",
        family="F1",
        target_code="TAG_DUPLICATE",
        target_validator_id="engineering-report",
        target_element_ids=[target_id, base["v2"]],
        mutation_revision=revision,
        notes=[f"two symbols share the tag {shared!r}"],
    )


# -- F2 : endpoint / connectivity -------------------------------------------- #


def _f2_dangling(service: DocumentService, base: BaseDrawing, rng: random.Random) -> MutationResult:
    """Free the pipe's source endpoint: a dangling connector and an unconnected port.

    The route is left alone on purpose. The repair has to choose a real element/port pair — and
    a mutation that also wrecked the geometry would let a routing fix pass as an endpoint fix.
    """

    revision = _patch(
        service, base.document_id, base["p1"], {"source": None}, "f2-endpoint-dangling"
    )
    return MutationResult(
        operator_id="f2_endpoint_dangling",
        family="F2",
        target_code="CONNECTOR_ENDPOINT_DANGLING",
        target_validator_id="engineering-report",
        target_element_ids=[base["p1"]],
        postconditions={
            "port_bindings": {
                base["p1"]: {"source": [base["v1"], "out"]},
            }
        },
        touched_budget_class="simple_endpoint",
        mutation_revision=revision,
    )


def _f2_required_port(service: DocumentService, base: BaseDrawing, rng: random.Random) -> MutationResult:
    revision = _patch(service, base.document_id, base["p1"], {"target": None}, "f2-required-port")
    return MutationResult(
        operator_id="f2_required_port_unconnected",
        family="F2",
        target_code="SYMBOL_REQUIRED_PORT_UNCONNECTED",
        target_validator_id="engineering-report",
        target_element_ids=[base["v2"]],
        target_details={"port_id": "in"},
        postconditions={
            "port_bindings": {
                base["p1"]: {"target": [base["v2"], "in"]},
            }
        },
        touched_budget_class="simple_endpoint",
        mutation_revision=revision,
    )


def _f2_dangling_from_both_ends(
    service: DocumentService, base: BaseDrawing, rng: random.Random
) -> MutationResult:
    revision = _patch(
        service,
        base.document_id,
        base["p1"],
        {"source": None, "target": None},
        "f2-endpoint-dangling-both",
    )
    return MutationResult(
        operator_id="f2_endpoint_dangling_both",
        family="F2",
        target_code="CONNECTOR_ENDPOINT_DANGLING",
        target_validator_id="engineering-report",
        target_element_ids=[base["p1"]],
        postconditions={
            "port_bindings": {
                base["p1"]: {"source": [base["v1"], "out"], "target": [base["v2"], "in"]},
            }
        },
        touched_budget_class="simple_endpoint",
        mutation_revision=revision,
    )


# -- F3 : replacement / multi-connector reconnection -------------------------- #


def _f3_delete_detach(service: DocumentService, base: BaseDrawing, rng: random.Random) -> MutationResult:
    """Delete the middle equipment and keep its line, which now ends in mid-air.

    The repair has to put replacement equipment back and re-bind the line to it — the family's
    whole point — rather than stretch the line to whatever else happens to be nearby.
    """

    from .agent_semantic_models import SafeDeleteElementOperation

    revision = _apply_semantic(
        service,
        base.document_id,
        [SafeDeleteElementOperation(element_id=base["v2"], connection_policy="detach")],
        "f3-delete-middle",
    )
    return MutationResult(
        operator_id="f3_delete_middle_detach",
        family="F3",
        target_code="CONNECTOR_ENDPOINT_DANGLING",
        target_validator_id="engineering-report",
        target_element_ids=[base["p1"]],
        postconditions={
            "endpoints_share_new_element": [base["p1"], base["p2"]],
            "no_dangling_endpoints": True,
            "element_ids_preserved": [base["p1"], base["p2"]],
        },
        touched_budget_class="multi_connector",
        mutation_revision=revision,
        declared_scope_ids=[base["p1"], base["p2"], base["v1"], base["v2"], base["v3"]],
        hop=2,
        notes=["the removed equipment must be replaced, not merely detached"],
    )


# -- F4 : routing / port geometry -------------------------------------------- #


def _f4_micro_segment(service: DocumentService, base: BaseDrawing, rng: random.Random) -> MutationResult:
    """Insert a leg shorter than the drawing grid: orthogonal, but not a legal drafting leg."""

    points = _manual_points(service, base.document_id, base["p1"])
    start, end = points[0], points[-1]
    grid = max(5.0, 20.0)
    first = Point(x=start.x, y=start.y + grid / 4)
    mid = Point(x=(start.x + end.x) / 2, y=first.y)
    route = [start, first, mid, Point(x=mid.x, y=end.y), end]
    revision = _patch(
        service,
        base.document_id,
        base["p1"],
        {"routing": "manual", "points": [p.model_dump(mode="json") for p in route]},
        "f4-micro-segment",
    )
    return MutationResult(
        operator_id="f4_micro_segment",
        family="F4",
        target_code="MICRO_SEGMENT",
        target_validator_id="diagram-quality",
        target_element_ids=[base["p1"]],
        postconditions={"no_micro_segments": True},
        touched_budget_class="local_geometry",
        mutation_revision=revision,
    )


def _f4_unnecessary_bend(service: DocumentService, base: BaseDrawing, rng: random.Random) -> MutationResult:
    """Add a detour to a connector whose endpoints are already axis-aligned.

    The detour is clear of every obstacle, so the defect is exactly "the bend is pointless" —
    which is what makes the repair a real judgement rather than a collision fix.
    """

    points = _manual_points(service, base.document_id, base["p1"])
    start, end = points[0], points[-1]
    bump = Point(x=start.x, y=start.y - 120)
    mid = Point(x=(start.x + end.x) / 2, y=bump.y)
    route = [start, bump, mid, Point(x=mid.x, y=end.y), end]
    revision = _patch(
        service,
        base.document_id,
        base["p1"],
        {"routing": "manual", "points": [p.model_dump(mode="json") for p in route]},
        "f4-unnecessary-bend",
    )
    return MutationResult(
        operator_id="f4_unnecessary_bend",
        family="F4",
        target_code="UNNECESSARY_BEND",
        target_validator_id="diagram-quality",
        target_element_ids=[base["p1"]],
        postconditions={"no_unnecessary_bends": True},
        touched_budget_class="local_geometry",
        mutation_revision=revision,
    )


def _f4_port_exit(service: DocumentService, base: BaseDrawing, rng: random.Random) -> MutationResult:
    """Leave the port on the wrong axis: the first leg must follow the port normal."""

    points = _manual_points(service, base.document_id, base["p1"])
    start, end = points[0], points[-1]
    route = [start, Point(x=start.x, y=start.y + 140), Point(x=end.x, y=start.y + 140), end]
    revision = _patch(
        service,
        base.document_id,
        base["p1"],
        {"routing": "manual", "points": [p.model_dump(mode="json") for p in route]},
        "f4-port-exit-mismatch",
    )
    return MutationResult(
        operator_id="f4_port_exit_mismatch",
        family="F4",
        target_code="PORT_EXIT_MISMATCH",
        target_validator_id="diagram-quality",
        target_element_ids=[base["p1"]],
        postconditions={},
        touched_budget_class="local_geometry",
        mutation_revision=revision,
    )


# -- F5 : local collision / drafting region ---------------------------------- #


def _f5_node_overlap(service: DocumentService, base: BaseDrawing, rng: random.Random) -> MutationResult:
    """Slide the isolated element onto the trunk equipment.

    The isolated element is the one that moves: dragging a *connected* symbol would recompute
    its line's route, and the case would then be measuring routing rather than the collision.
    """

    document = service.get_document(base.document_id)
    symbol = next(item for item in document.elements if item.id == base["v2"])
    revision = _patch(
        service,
        base.document_id,
        base["v3"],
        {"position": symbol.position.model_dump(mode="json")},
        "f5-node-overlap",
    )
    return MutationResult(
        operator_id="f5_node_overlap",
        family="F5",
        target_code="NODE_OVERLAP",
        target_validator_id="diagram-quality",
        target_element_ids=[base["v3"]],
        postconditions={},
        touched_budget_class="complex_collision",
        mutation_revision=revision,
    )


def _f5_pipe_through_equipment(
    service: DocumentService, base: BaseDrawing, rng: random.Random
) -> MutationResult:
    """Route the pipe straight through the far valve and back instead of stopping short.

    The detour has to *cross* the valve rather than stop inside it: the rule's intersection
    test looks for a segment that enters and leaves an obstacle rectangle, so a leg whose
    endpoint lands in the middle of the rectangle is not a crossing at all — it is a
    dangling route. Both ends of the offending leg therefore stay outside ``v3``'s
    rectangle, and the connector still terminates on the port it started on.
    """

    document = service.get_document(base.document_id)
    target = next(item for item in document.elements if item.id == base["v3"])
    points = _manual_points(service, base.document_id, base["p1"])
    start, end = points[0], points[-1]
    beyond_x = target.position.x + target.width + 40.0
    return_y = end.y + 80.0
    route = [
        start,
        Point(x=beyond_x, y=start.y),
        Point(x=beyond_x, y=return_y),
        Point(x=end.x, y=return_y),
        end,
    ]
    revision = _patch(
        service,
        base.document_id,
        base["p1"],
        {"routing": "manual", "points": [p.model_dump(mode="json") for p in route]},
        "f5-pipe-through-equipment",
    )
    return MutationResult(
        operator_id="f5_pipe_through_equipment",
        family="F5",
        target_code="PIPE_THROUGH_EQUIPMENT",
        target_validator_id="diagram-quality",
        target_element_ids=[base["p1"]],
        postconditions={},
        touched_budget_class="complex_collision",
        mutation_revision=revision,
    )


def _f5_symbol_out_of_bounds(service: DocumentService, base: BaseDrawing, rng: random.Random) -> MutationResult:
    revision = _patch(
        service,
        base.document_id,
        base["v3"],
        {"position": {"x": -400, "y": 300}},
        "f5-out-of-bounds",
    )
    return MutationResult(
        operator_id="f5_symbol_out_of_bounds",
        family="F5",
        target_code="SYMBOL_OUT_OF_BOUNDS",
        target_validator_id="diagram-quality",
        target_element_ids=[base["v3"]],
        postconditions={},
        touched_budget_class="complex_collision",
        mutation_revision=revision,
    )


# -- F6 : replan / conflict robustness --------------------------------------- #


F6_CONTROL_FLOW: dict[str, int] = {
    "f6_replan_once": 2,
    "f6_replan_twice": 3,
    "f6_replan_four_times": 5,
}


def _build_f6(failures: int, base_operator_id: str) -> Callable[[DocumentService, BaseDrawing, random.Random], MutationResult]:
    def apply(service: DocumentService, base: BaseDrawing, rng: random.Random) -> MutationResult:
        result = MUTATIONS[base_operator_id].apply(service, base, rng)
        return MutationResult(
            operator_id=f"f6_{failures}_attempts",
            family="F6",
            target_code=result.target_code,
            target_validator_id=result.target_validator_id,
            target_element_ids=result.target_element_ids,
            postconditions=result.postconditions,
            hop=result.hop,
            touched_budget_class=result.touched_budget_class,
            mutation_revision=result.mutation_revision,
            notes=[f"control flow: the honest plan is reachable on attempt {failures}"],
            target_details=dict(result.target_details),
            required_attempts=failures,
            wrap_operator_id=base_operator_id,
        )

    return apply


# -- the catalogue ----------------------------------------------------------- #

MUTATIONS: dict[str, MutationOperator] = {
    "f1_line_name_missing": MutationOperator(
        "f1_line_name_missing", "F1", "LINE_TAG_MISSING", "engineering-report", _f1_line_name,
        touched_budget_class="simple_metadata",
    ),
    "f1_line_tag_missing": MutationOperator(
        "f1_line_tag_missing", "F1", "LINE_TAG_MISSING", "engineering-report", _f1_line_tag,
        touched_budget_class="simple_metadata",
    ),
    "f1_line_medium_missing": MutationOperator(
        "f1_line_medium_missing", "F1", "LINE_MEDIUM_MISSING", "engineering-report", _f1_line_medium,
        touched_budget_class="simple_metadata",
    ),
    "f1_line_diameter_missing": MutationOperator(
        "f1_line_diameter_missing", "F1", "LINE_DIAMETER_MISSING", "engineering-report",
        _f1_line_diameter, touched_budget_class="simple_metadata",
    ),
    # The symbol-tag half of F1. These two codes could not be produced at all while the canonical
    # rules read a field the production polish clears, so F1's coverage of identity was limited
    # to line identity. They exist now for the same reason the rules were fixed: a drawing the
    # product itself wrote really can be missing a tag, or really can duplicate one.
    "f1_symbol_tag_missing": MutationOperator(
        "f1_symbol_tag_missing", "F1", "TAG_MISSING", "engineering-report",
        _f1_symbol_tag_missing, touched_budget_class="simple_metadata",
    ),
    "f1_symbol_tag_duplicate": MutationOperator(
        "f1_symbol_tag_duplicate", "F1", "TAG_DUPLICATE", "engineering-report",
        _f1_symbol_tag_duplicate, touched_budget_class="simple_metadata",
    ),
    "f2_endpoint_dangling": MutationOperator(
        "f2_endpoint_dangling", "F2", "CONNECTOR_ENDPOINT_DANGLING", "engineering-report", _f2_dangling,
        touched_budget_class="simple_endpoint",
    ),
    "f2_required_port_unconnected": MutationOperator(
        "f2_required_port_unconnected", "F2", "SYMBOL_REQUIRED_PORT_UNCONNECTED", "engineering-report",
        _f2_required_port, touched_budget_class="simple_endpoint",
    ),
    "f2_endpoint_dangling_both": MutationOperator(
        "f2_endpoint_dangling_both", "F2", "CONNECTOR_ENDPOINT_DANGLING", "engineering-report",
        _f2_dangling_from_both_ends, touched_budget_class="simple_endpoint",
    ),
    "f3_delete_middle_detach": MutationOperator(
        "f3_delete_middle_detach", "F3", "CONNECTOR_ENDPOINT_DANGLING", "engineering-report",
        _f3_delete_detach,
        document_builder=lambda service, registry: build_base_drawing(
            service, registry, name="M5 benchmark base (three valves)", variant="three_valves"
        ),
        touched_budget_class="multi_connector",
        # Putting the removed device back is a create. Deleting it left a generated label behind,
        # and the reconstruction consumes that annotation (the element carries the tag again), so
        # the case grants exactly one removal -- and a plan that removes anything else is refused
        # as a pseudo-repair (C4).
        permits_creation=True,
        max_created_ids=1,
        permits_deletion=True,
        max_deleted_ids=1,
    ),
    "f4_micro_segment": MutationOperator(
        "f4_micro_segment", "F4", "MICRO_SEGMENT", "diagram-quality", _f4_micro_segment,
        touched_budget_class="local_geometry",
    ),
    "f4_unnecessary_bend": MutationOperator(
        "f4_unnecessary_bend", "F4", "UNNECESSARY_BEND", "diagram-quality", _f4_unnecessary_bend,
        touched_budget_class="local_geometry",
    ),
    "f4_port_exit_mismatch": MutationOperator(
        "f4_port_exit_mismatch", "F4", "PORT_EXIT_MISMATCH", "diagram-quality", _f4_port_exit,
        touched_budget_class="local_geometry",
    ),
    "f5_node_overlap": MutationOperator(
        "f5_node_overlap", "F5", "NODE_OVERLAP", "diagram-quality", _f5_node_overlap,
        touched_budget_class="complex_collision",
    ),
    "f5_pipe_through_equipment": MutationOperator(
        "f5_pipe_through_equipment", "F5", "PIPE_THROUGH_EQUIPMENT", "diagram-quality",
        _f5_pipe_through_equipment, touched_budget_class="complex_collision",
    ),
    "f5_symbol_out_of_bounds": MutationOperator(
        "f5_symbol_out_of_bounds", "F5", "SYMBOL_OUT_OF_BOUNDS", "diagram-quality",
        _f5_symbol_out_of_bounds, touched_budget_class="complex_collision",
    ),
    "f6_replan_once": MutationOperator(
        "f6_replan_once", "F6", "", "", _build_f6(2, "f2_endpoint_dangling"),
        touched_budget_class="simple_endpoint",
    ),
    "f6_replan_twice": MutationOperator(
        "f6_replan_twice", "F6", "", "", _build_f6(3, "f2_required_port_unconnected"),
        touched_budget_class="simple_endpoint",
    ),
    "f6_replan_four_times": MutationOperator(
        "f6_replan_four_times", "F6", "", "", _build_f6(5, "f1_line_medium_missing"),
        touched_budget_class="simple_metadata",
    ),
}


def _operator_catalog() -> list[dict[str, Any]]:
    return [
        {
            "operator_id": operator.operator_id,
            "family": operator.family,
            "target_code": operator.target_code,
            "validator_id": operator.target_validator_id,
            "touched_budget_class": operator.touched_budget_class,
            "base_variant": "three_valves" if operator.document_builder else "standard",
            "permits_creation": operator.permits_creation,
            "max_created_ids": operator.max_created_ids,
            "permits_deletion": operator.permits_deletion,
            "max_deleted_ids": operator.max_deleted_ids,
        }
        for operator in sorted(MUTATIONS.values(), key=lambda item: item.operator_id)
    ]


def spec_payload() -> dict[str, Any]:
    return {
        "spec_version": BENCHMARK_SPEC_VERSION,
        "oracle_version": SUCCESS_ORACLE_VERSION,
        "families": list(FAMILIES),
        "thresholds": dict(sorted(THRESHOLDS.items())),
        "operators": _operator_catalog(),
        "dev_cases_per_family": DEV_CASES_PER_FAMILY,
        "acceptance_cases_per_family": ACCEPTANCE_CASES_PER_FAMILY,
        "safety_cases": SAFETY_CASES,
    }


def spec_fingerprint() -> str:
    text = json.dumps(spec_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generator_fingerprint() -> str:
    """Fingerprint of the mutation code itself, so a silent edit to an operator is visible."""

    from . import repair_benchmark as module

    text = module.build_base_drawing.__code__.co_code
    digests = [hashlib.sha256(bytes(text)).hexdigest()]
    for operator in sorted(MUTATIONS.values(), key=lambda item: item.operator_id):
        code = getattr(operator.apply, "__code__", None)
        if code is not None:
            digests.append(hashlib.sha256(bytes(code.co_code)).hexdigest())
        for constant in getattr(operator.apply, "__closure__", None) or ():
            inner = getattr(constant.cell_contents, "__code__", None)
            if inner is not None:
                digests.append(hashlib.sha256(bytes(inner.co_code)).hexdigest())
    return hashlib.sha256("".join(digests).encode("utf-8")).hexdigest()


# -- case derivation -------------------------------------------------------- #


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    family: str
    operator_id: str
    index: int
    seed: int
    suite: str


def derive_seed(*, spec: str, candidate_sha: str, family: str, index: int) -> int:
    material = f"{spec}:{candidate_sha}:{family}:{index}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def operators_for_family(family: str) -> list[str]:
    return sorted(
        operator.operator_id for operator in MUTATIONS.values() if operator.family == family
    )


def generate_cases(
    *,
    candidate_sha: str,
    family: str,
    count: int,
    suite: str = "acceptance",
    spec: str | None = None,
) -> list[BenchmarkCase]:
    """Deterministically derive ``count`` cases for one family.

    Operator selection is round-robin over the family's catalogue and the seed is derived from
    ``(spec fingerprint, candidate SHA, family, index)``: same candidate, same cases; different
    candidate, different cases. Nothing here is sampled at run time, so a case cannot be
    dropped because it failed.
    """

    spec_fingerprint_value = spec or spec_fingerprint()
    operator_ids = operators_for_family(family)
    if not operator_ids:
        raise ValueError(f"no mutation operators registered for family {family}")
    cases: list[BenchmarkCase] = []
    for index in range(count):
        operator_id = operator_ids[index % len(operator_ids)]
        seed = derive_seed(
            spec=spec_fingerprint_value, candidate_sha=candidate_sha, family=family, index=index
        )
        cases.append(
            BenchmarkCase(
                case_id=f"{suite}:{family}:{index:02d}:{operator_id}",
                family=family,
                operator_id=operator_id,
                index=index,
                seed=seed,
                suite=suite,
            )
        )
    return cases


def generate_suite(*, candidate_sha: str, suite: str) -> list[BenchmarkCase]:
    per_family = DEV_CASES_PER_FAMILY if suite == "dev" else ACCEPTANCE_CASES_PER_FAMILY
    cases: list[BenchmarkCase] = []
    for family in FAMILIES:
        cases.extend(
            generate_cases(candidate_sha=candidate_sha, family=family, count=per_family, suite=suite)
        )
    return cases


__all__ = [
    "ACCEPTANCE_CASES_PER_FAMILY",
    "BENCHMARK_SPEC_VERSION",
    "DEV_CASES_PER_FAMILY",
    "FAMILIES",
    "FAMILY_TITLES",
    "MUTATIONS",
    "SAFETY_CASES",
    "SUCCESS_ORACLE_VERSION",
    "THRESHOLDS",
    "BaseDrawing",
    "BenchmarkCase",
    "MutationOperator",
    "MutationResult",
    "build_base_drawing",
    "derive_seed",
    "generate_cases",
    "generate_suite",
    "generator_fingerprint",
    "operators_for_family",
    "spec_fingerprint",
    "spec_payload",
]
