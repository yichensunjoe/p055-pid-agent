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
from collections.abc import Callable, Mapping
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
#: Bumped when the *gate* changes, because changing what counts as passing changes the spec.
#: Version 2 removed the deterministic track's global-S@1 rejection (an observation, not a
#: verdict) and added the F6 convergence gate plus the per-case attempt contract.
#: Version 3 is the coverage promotion (remote ruling §③, "coverage-promotion"): the four codes
#: the coverage-extension track proved a document in this repository can actually hold --
#: ``DUPLICATE_LABEL``, ``PORT_DIRECTION_MISMATCH``, ``UNBRIDGED_CROSSING`` and
#: ``ANNOTATION_OVERLAP`` -- became real cases in the frozen catalogue. The thresholds, the oracle
#: version, the family structure and the case layout did not move; what changed is which defects
#: the corpus can stage. The v2 body stays recomputable through :data:`_SPEC_ARCHIVE`, so the
#: published v2 numbers keep their derivation instead of becoming folklore.
BENCHMARK_SPEC_VERSION = "3"

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
    #: F6 is the family that exists to prove convergence under injected failure, so its S@5 is
    #: gated on its own rather than only through the family minimum.
    "f6_s5_overall": 1.0,
    "safety_suite": 1.0,
    "model_s5_overall": 0.80,
    "model_family_min": 0.50,
}

DEV_CASES_PER_FAMILY = 4
ACCEPTANCE_CASES_PER_FAMILY = 12
SAFETY_CASES = 12

#: The frozen acceptance corpus. Versioned and fingerprinted separately from the spec because
#: "what counts as passing" (the spec) and "which cases were run" (the corpus) are different
#: questions, and a reviewer has to be able to answer "what did M5 accept?" years later. Growing
#: coverage means a *new* corpus version, never an edit to the one already published.
#:
#: Version 3 keeps the layout intact -- still 12 acceptance cases per family, so the gate this
#: corpus is judged by does not change shape -- while the *mix* grows from 19 operators to 23:
#: the coverage promotion added one operator to each of F1, F2, F4 and F5. Because the cases
#: derive from ``spec_fingerprint()``, that is a different case set with different seeds, and the
#: v2 numbers are not comparable with v3 ones. They are not rewritten either: ``reports/m5/**``
#: and the v2 identities recorded in :mod:`agentcad.repair_coverage_ledger` stay as published.
CORE_CORPUS_ID = "m5-core-corpus"
CORE_CORPUS_VERSION = "3"


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


def _three_valves_base(service: DocumentService, registry: SymbolRegistry) -> BaseDrawing:
    """The base with the middle-to-last valve trunk present (roles ``p1`` and ``p2``).

    Shared by every case whose defect is about a *second* line: F3 detaches it, and the promoted
    crossing case re-routes it. Named rather than inlined so the two operators cannot drift into
    two slightly different ``three_valves`` drawings.
    """

    return build_base_drawing(
        service, registry, name="M5 benchmark base (three valves)", variant="three_valves"
    )


def _crossing_base(service: DocumentService, registry: SymbolRegistry) -> BaseDrawing:
    """The three-valve base plus a fourth device and a branch line that runs to it.

    A crossing between two lines that share a device is not a crossing in this model -- the two
    ends meet at a junction -- so the branch has to run between devices the trunk does not touch.
    Both the device and its line are added through the semantic compiler as part of the *base*
    drawing, exactly the way the trunk is built, which keeps the mutation itself a single re-route.
    """

    from .agent_semantic_models import ConnectPortsOperation

    base = _three_valves_base(service, registry)
    _apply_semantic(
        service,
        base.document_id,
        [
            AddElementOperation(
                element=_symbol("v4", registry, "ball_valve", 400, 700, label="HV-104")
            ),
            ConnectPortsOperation(
                connector_id="q1",
                source_element_id="v4",
                source_port_id="out",
                target_element_id="v3",
                target_port_id="in",
                routing="orthogonal",
                process_tag="L-M5-004",
                medium="process",
                nominal_diameter="DN50",
                flow_direction="forward",
            ),
        ],
        "benchmark.crossing-base",
    )
    base.elements.update({"v4": "v4", "q1": "q1"})
    return base


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
    #: Which base drawing a case under this operator starts from, published in the spec catalogue
    #: so a reviewer sees it without reading the builder. Stated explicitly because the old
    #: derivation ("has a builder implies three_valves") stopped being true the moment a second
    #: builder joined the catalogue: the crossing base is the three-valve drawing *plus* a branch
    #: line and a fourth device, and a catalogue that called it "three_valves" would be wrong
    #: about the drawing the case is judged on.
    base_variant: str = "standard"


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


def _connector_element(service: DocumentService, document_id: str, connector_id: str) -> ConnectorElement:
    document = service.get_document(document_id)
    connector = next(item for item in document.elements if item.id == connector_id)
    assert isinstance(connector, ConnectorElement)
    return connector


def _manual_points(service: DocumentService, document_id: str, connector_id: str) -> list[Point]:
    connector = _connector_element(service, document_id, connector_id)
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


def _f1_duplicate_label(service: DocumentService, base: BaseDrawing, rng: random.Random) -> MutationResult:
    """Make the free note repeat a device's visible label, near that device.

    ``DUPLICATE_LABEL`` counts *visible* labels that are also close together, so the note has to be
    both textually and spatially a duplicate. It is deliberately not a ``TAG_DUPLICATE``: the
    device's own tag is untouched, which is why this producer buys exactly one code and is a
    different case from ``f1_symbol_tag_duplicate``.
    """

    from .annotation_layout import measure_annotation_quality

    note_id = base["note1"]
    document = service.get_document(base.document_id)
    labelled = next(element for element in document.elements if element.id == base["v1"])
    label = str((labelled.properties or {}).get("tag") or labelled.label or "")
    revision = _patch(service, base.document_id, note_id, {"text": label}, "f1-duplicate-label")
    quality = measure_annotation_quality(
        service.get_document(base.document_id), service.symbols
    )
    if quality.duplicate_label_count <= 0:
        raise BenchmarkSetupError(
            f"{note_id} was made identical to a visible label but no duplicate was measured"
        )
    return MutationResult(
        operator_id="f1_duplicate_label",
        family="F1",
        target_code="DUPLICATE_LABEL",
        target_validator_id="diagram-quality",
        target_element_ids=[note_id],
        declared_scope_ids=[note_id],
        touched_budget_class="simple_metadata",
        mutation_revision=revision,
        notes=[
            f"the note now reads {label!r}, which is also the visible label of {base['v1']}",
            f"duplicate_label_count={quality.duplicate_label_count}",
        ],
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


def _f2_port_direction_mismatch(
    service: DocumentService, base: BaseDrawing, rng: random.Random
) -> MutationResult:
    """Declare the trunk's flow opposite to the ports it is wire between.

    Nothing about the route or the binding moves: the two ports still say the medium runs
    *forward*, and only the connector's own declaration disagrees. A repair that reconnects
    something has misread the finding.
    """

    connector_id = base["p1"]
    revision = _patch(
        service,
        base.document_id,
        connector_id,
        {"flow_direction": "reverse"},
        "f2-port-direction-mismatch",
    )
    return MutationResult(
        operator_id="f2_port_direction_mismatch",
        family="F2",
        target_code="PORT_DIRECTION_MISMATCH",
        target_validator_id="diagram-quality",
        target_element_ids=[connector_id],
        # The finding fires per connector and carries what the ports imply; pinning that makes the
        # binding the case's own fact rather than whichever finding sorted first.
        target_details={"inferred_flow_direction": "forward"},
        declared_scope_ids=[connector_id],
        touched_budget_class="simple_endpoint",
        mutation_revision=revision,
        notes=["the ports still determine flow 'forward'; only the declaration moved"],
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


def _f4_unbridged_crossing(
    service: DocumentService, base: BaseDrawing, rng: random.Random
) -> MutationResult:
    """Re-route the branch line straight across the trunk, with no jump bridge on either side.

    The line is re-routed rather than added, so the case needs no create permission and the defect
    stays a geometry defect. Departure and approach stay port-outward and the declared flow keeps
    agreeing with the ports -- this case is about a crossing, not about a broken exit. The detour
    costs more than three bends by construction (both ports are horizontal, so any path that
    crosses a horizontal trunk and returns to the ports has at least four), which the manifestation
    proof reports as an added ``EXCESSIVE_BENDS`` rather than hides.
    """

    connector_id = base["q1"]
    connector = _connector_element(service, base.document_id, connector_id)
    start = connector.points[0]
    end = connector.points[-1]
    grid = max(5.0, service.get_document(base.document_id).canvas.grid_size)
    # The crossing column is derived from the trunk the branch has to cross, not hard-coded: the
    # vertical has to pass *through* the trunk, so its x must be strictly inside the trunk's span.
    # The route then climbs over the trunk and comes back down onto the target port from the left,
    # which is the only approach its outward normal allows.
    trunk = _connector_element(service, base.document_id, base["p1"])
    trunk_xs = [point.x for point in trunk.points]
    corridor = min(point.y for point in trunk.points) - 5.0 * grid
    column = min(max(start.x + 7.0 * grid, min(trunk_xs) + grid), max(trunk_xs) - grid)
    descent = end.x - 3.5 * grid
    route = [
        Point(x=start.x, y=start.y),
        Point(x=column, y=start.y),
        Point(x=column, y=corridor),
        Point(x=descent, y=corridor),
        Point(x=descent, y=end.y),
        Point(x=end.x, y=end.y),
    ]
    revision = _patch(
        service,
        base.document_id,
        connector_id,
        {"routing": "manual", "points": [point.model_dump(mode="json") for point in route]},
        "f4-unbridged-crossing-route",
    )
    return MutationResult(
        operator_id="f4_unbridged_crossing",
        family="F4",
        target_code="UNBRIDGED_CROSSING",
        target_validator_id="diagram-quality",
        target_element_ids=[connector_id],
        postconditions={"element_ids_preserved": [connector_id, base["p1"]]},
        declared_scope_ids=[connector_id],
        touched_budget_class="local_geometry",
        mutation_revision=revision,
        notes=[f"{connector_id} crosses {base['p1']} and neither side asks for a jump bridge"],
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


def _f5_annotation_overlap(
    service: DocumentService, base: BaseDrawing, rng: random.Random
) -> MutationResult:
    """Put the isolated device's label annotation on top of the device itself.

    The annotation is ``symbol_label`` text the production polish generated, so this is a drafting
    collision between an element and its own label -- not a missing or duplicated tag.
    """

    element_id = base["v3"]
    annotations = _symbol_label_annotations(service, base.document_id, element_id)
    if not annotations:
        raise BenchmarkSetupError(f"{element_id} has no generated label annotation to overlap")
    annotation_id = annotations[0]
    document = service.get_document(base.document_id)
    placed = next(element for element in document.elements if element.id == element_id)
    revision = _patch(
        service,
        base.document_id,
        annotation_id,
        {"position": {"x": placed.position.x, "y": placed.position.y}},
        "f5-annotation-overlap",
    )
    return MutationResult(
        operator_id="f5_annotation_overlap",
        family="F5",
        target_code="ANNOTATION_OVERLAP",
        target_validator_id="diagram-quality",
        # The finding is drawing-wide: the case declares which annotation it broke, so the binding
        # is a fact about the case rather than about sort order.
        target_element_ids=[annotation_id],
        declared_scope_ids=[annotation_id, element_id],
        touched_budget_class="local_geometry",
        mutation_revision=revision,
        notes=[f"{annotation_id} now sits at the centre of {element_id}"],
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
    # Promoted by the coverage promotion (remote ruling §③). The producer, its base and its target
    # binding are the ones the coverage-extension track proved, moved here unchanged: the point of
    # the promotion is that these codes stop being a side track and become cases the frozen gate
    # owns. The extension module keeps the record of *why* they were believed, this keeps the cases.
    "f1_duplicate_label": MutationOperator(
        "f1_duplicate_label", "F1", "DUPLICATE_LABEL", "diagram-quality", _f1_duplicate_label,
        document_builder=_three_valves_base, touched_budget_class="simple_metadata",
        base_variant="three_valves",
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
    "f2_port_direction_mismatch": MutationOperator(
        "f2_port_direction_mismatch", "F2", "PORT_DIRECTION_MISMATCH", "diagram-quality",
        _f2_port_direction_mismatch,
        document_builder=_three_valves_base, touched_budget_class="simple_endpoint",
        base_variant="three_valves",
    ),
    "f3_delete_middle_detach": MutationOperator(
        "f3_delete_middle_detach", "F3", "CONNECTOR_ENDPOINT_DANGLING", "engineering-report",
        _f3_delete_detach,
        document_builder=lambda service, registry: build_base_drawing(
            service, registry, name="M5 benchmark base (three valves)", variant="three_valves"
        ),
        touched_budget_class="multi_connector",
        base_variant="three_valves",
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
    "f4_unbridged_crossing": MutationOperator(
        "f4_unbridged_crossing", "F4", "UNBRIDGED_CROSSING", "diagram-quality",
        _f4_unbridged_crossing,
        document_builder=_crossing_base, touched_budget_class="local_geometry",
        base_variant="three_valves+crossing",
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
    "f5_annotation_overlap": MutationOperator(
        "f5_annotation_overlap", "F5", "ANNOTATION_OVERLAP", "diagram-quality",
        _f5_annotation_overlap,
        document_builder=_three_valves_base, touched_budget_class="local_geometry",
        base_variant="three_valves",
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


def _operator_row(operator: MutationOperator) -> dict[str, Any]:
    return {
        "operator_id": operator.operator_id,
        "family": operator.family,
        "target_code": operator.target_code,
        "validator_id": operator.target_validator_id,
        "touched_budget_class": operator.touched_budget_class,
        "base_variant": operator.base_variant,
        "permits_creation": operator.permits_creation,
        "max_created_ids": operator.max_created_ids,
        "permits_deletion": operator.permits_deletion,
        "max_deleted_ids": operator.max_deleted_ids,
    }


def _operator_catalog() -> list[dict[str, Any]]:
    """The working catalogue, in the one order the spec is allowed to be in."""

    return [
        _operator_row(operator)
        for operator in sorted(MUTATIONS.values(), key=lambda item: item.operator_id)
    ]


# -- the spec archive -------------------------------------------------------- #
#
# A fingerprint is only an identity if somebody can recompute it. The working spec grows, so the
# published v2 fingerprint (``c8520c5e…``) would otherwise become a number a reader has to take on
# trust: the v2 *body* would exist nowhere in the tree. The archive below is that body, kept
# verbatim so ``spec_fingerprint("2")`` still answers with the published value, and
# ``tests/test_repair_contract.py`` asserts the recomputation against the constant that was
# published -- which is what makes this a checked archive rather than a second source of truth.
#
# Nothing here is derived from today's catalogue on purpose. Deriving it ("the v2 operators are
# the ones not promoted in v3") would silently move the v2 identity the first time an old
# operator's metadata changed, and the whole point of freezing a spec is that it cannot move.

#: ``(operator_id, family, target_code, validator_id, touched_budget_class, base_variant,
#: permits_creation, max_created_ids, permits_deletion, max_deleted_ids)``.
_SPEC_V2_OPERATORS: tuple[tuple[Any, ...], ...] = (
    ("f1_line_diameter_missing", "F1", "LINE_DIAMETER_MISSING", "engineering-report", "simple_metadata", "standard", False, 0, False, 0),
    ("f1_line_medium_missing", "F1", "LINE_MEDIUM_MISSING", "engineering-report", "simple_metadata", "standard", False, 0, False, 0),
    ("f1_line_name_missing", "F1", "LINE_TAG_MISSING", "engineering-report", "simple_metadata", "standard", False, 0, False, 0),
    ("f1_line_tag_missing", "F1", "LINE_TAG_MISSING", "engineering-report", "simple_metadata", "standard", False, 0, False, 0),
    ("f1_symbol_tag_duplicate", "F1", "TAG_DUPLICATE", "engineering-report", "simple_metadata", "standard", False, 0, False, 0),
    ("f1_symbol_tag_missing", "F1", "TAG_MISSING", "engineering-report", "simple_metadata", "standard", False, 0, False, 0),
    ("f2_endpoint_dangling", "F2", "CONNECTOR_ENDPOINT_DANGLING", "engineering-report", "simple_endpoint", "standard", False, 0, False, 0),
    ("f2_endpoint_dangling_both", "F2", "CONNECTOR_ENDPOINT_DANGLING", "engineering-report", "simple_endpoint", "standard", False, 0, False, 0),
    ("f2_required_port_unconnected", "F2", "SYMBOL_REQUIRED_PORT_UNCONNECTED", "engineering-report", "simple_endpoint", "standard", False, 0, False, 0),
    ("f3_delete_middle_detach", "F3", "CONNECTOR_ENDPOINT_DANGLING", "engineering-report", "multi_connector", "three_valves", True, 1, True, 1),
    ("f4_micro_segment", "F4", "MICRO_SEGMENT", "diagram-quality", "local_geometry", "standard", False, 0, False, 0),
    ("f4_port_exit_mismatch", "F4", "PORT_EXIT_MISMATCH", "diagram-quality", "local_geometry", "standard", False, 0, False, 0),
    ("f4_unnecessary_bend", "F4", "UNNECESSARY_BEND", "diagram-quality", "local_geometry", "standard", False, 0, False, 0),
    ("f5_node_overlap", "F5", "NODE_OVERLAP", "diagram-quality", "complex_collision", "standard", False, 0, False, 0),
    ("f5_pipe_through_equipment", "F5", "PIPE_THROUGH_EQUIPMENT", "diagram-quality", "complex_collision", "standard", False, 0, False, 0),
    ("f5_symbol_out_of_bounds", "F5", "SYMBOL_OUT_OF_BOUNDS", "diagram-quality", "complex_collision", "standard", False, 0, False, 0),
    ("f6_replan_four_times", "F6", "", "", "simple_metadata", "standard", False, 0, False, 0),
    ("f6_replan_once", "F6", "", "", "simple_endpoint", "standard", False, 0, False, 0),
    ("f6_replan_twice", "F6", "", "", "simple_endpoint", "standard", False, 0, False, 0),
)

_SPEC_OPERATOR_FIELDS: tuple[str, ...] = (
    "operator_id",
    "family",
    "target_code",
    "validator_id",
    "touched_budget_class",
    "base_variant",
    "permits_creation",
    "max_created_ids",
    "permits_deletion",
    "max_deleted_ids",
)

#: Published spec identities, kept beside the body they came from. ``spec_fingerprint("2")`` has to
#: equal the first one; the test that checks it is the reason the archive cannot rot.
SPEC_FINGERPRINT_V2 = "c8520c5e7b0b1d7e77e3752080393b4965aee60538ad71a6bf95d5fc4ab5ede1"


def archived_operator_catalogue(version: str) -> dict[str, dict[str, Any]]:
    """A superseded spec's operator catalogue, keyed by operator id.

    Published so the *derivation* of an old case set can be repeated, not only its fingerprint
    recomputed: the operator a family's round-robin picks is read from here, which is what makes
    "these are the 72 cases v2 ran" a claim somebody can check instead of a reconstruction from a
    saved JSON file.
    """

    payload = spec_payload(version)
    return {row["operator_id"]: row for row in payload["operators"]}


def _archived_spec_payload(version: str) -> dict[str, Any] | None:
    """The frozen body of a superseded spec version, or ``None`` if there is no archive for it."""

    if version != "2":
        return None
    return {
        "spec_version": "2",
        "oracle_version": "1",
        "families": ["F1", "F2", "F3", "F4", "F5", "F6"],
        # v2's thresholds, spelled out rather than read from ``THRESHOLDS``: they happen to be the
        # same numbers today, and the archive has to stay right even on the day they are not.
        "thresholds": {
            "f6_s5_overall": 1.0,
            "model_family_min": 0.5,
            "model_s5_overall": 0.8,
            "s5_family_min": 0.75,
            "s5_overall": 0.9,
            "safety_suite": 1.0,
        },
        "operators": [
            dict(zip(_SPEC_OPERATOR_FIELDS, row, strict=True)) for row in _SPEC_V2_OPERATORS
        ],
        "dev_cases_per_family": 4,
        "acceptance_cases_per_family": 12,
        "safety_cases": 12,
    }


def spec_payload(version: str | None = None) -> dict[str, Any]:
    """The spec body for ``version`` -- the working one by default, an archived one on request.

    An unknown version raises instead of falling back to the working spec: silently answering with
    today's body would make ``spec_fingerprint("1")`` look like a successful recomputation.
    """

    resolved = version or BENCHMARK_SPEC_VERSION
    if resolved != BENCHMARK_SPEC_VERSION:
        archived = _archived_spec_payload(resolved)
        if archived is None:
            raise ValueError(f"no spec body is archived for version {resolved!r}")
        return archived
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


def supported_code_manifest() -> list[dict[str, str]]:
    """The codes the frozen catalogue can actually produce, one row per operator.

    ``target_code`` is what the operator claims and the case's binding is checked against at run
    time, so this manifest is a claim the benchmark itself enforces: an operator that stops
    producing its code turns its cases into ``invalid_case`` (baseline §A7).
    """

    return [
        {
            "operator_id": operator.operator_id,
            "family": operator.family,
            "target_code": operator.target_code,
            "validator_id": operator.target_validator_id,
        }
        for operator in sorted(MUTATIONS.values(), key=lambda item: item.operator_id)
        if operator.target_code
    ]


def core_corpus_manifest(version: str | None = None) -> dict[str, Any]:
    """Identify the frozen acceptance corpus -- the working one, or an archived version.

    The manifest carries both the corpus's own coordinates and the two fingerprints of the world
    it ran in. Those two are why the *identity* of the corpus is published separately, as
    :func:`core_corpus_digest`.

    An archived version is rebuilt from its frozen spec body rather than from today's catalogue,
    for the same reason ``spec_fingerprint("2")`` is: "these are the cases v2 ran" has to stay a
    claim somebody can check. One field cannot come back, and the manifest says so by leaving it
    out: ``generator_fingerprint`` digests the *bytecode* of the v2 operators, and that bytecode is
    gone. The archived manifest therefore has no such key, which is exactly why the corpus identity
    (and not the fingerprint) is the thing that survives a code move.
    """

    resolved = version or CORE_CORPUS_VERSION
    if resolved != CORE_CORPUS_VERSION:
        archived = _archived_core_corpus_manifest(resolved)
        if archived is None:
            raise ValueError(f"no corpus manifest is archived for version {resolved!r}")
        return archived
    return {
        "corpus_id": CORE_CORPUS_ID,
        "corpus_version": CORE_CORPUS_VERSION,
        "spec_version": BENCHMARK_SPEC_VERSION,
        "spec_fingerprint": spec_fingerprint(),
        "generator_fingerprint": generator_fingerprint(),
        "families": list(FAMILIES),
        "cases_per_family": ACCEPTANCE_CASES_PER_FAMILY,
        "dev_cases_per_family": DEV_CASES_PER_FAMILY,
        "case_count": ACCEPTANCE_CASES_PER_FAMILY * len(FAMILIES),
        "safety_case_count": SAFETY_CASES,
        "operator_count": len(MUTATIONS),
        "supported_codes": supported_code_manifest(),
    }


def _archived_core_corpus_manifest(version: str) -> dict[str, Any] | None:
    """The corpus manifest of a superseded version, or ``None`` when no spec body is archived."""

    try:
        payload = spec_payload(version)
    except ValueError:
        return None
    operators = sorted(payload["operators"], key=lambda row: str(row["operator_id"]))
    return {
        "corpus_id": CORE_CORPUS_ID,
        "corpus_version": version,
        "spec_version": payload["spec_version"],
        "spec_fingerprint": spec_fingerprint(version),
        "families": list(payload["families"]),
        "cases_per_family": payload["acceptance_cases_per_family"],
        "dev_cases_per_family": payload["dev_cases_per_family"],
        "case_count": payload["acceptance_cases_per_family"] * len(payload["families"]),
        "safety_case_count": payload["safety_cases"],
        "operator_count": len(operators),
        "supported_codes": [
            {
                "operator_id": row["operator_id"],
                "family": row["family"],
                "target_code": row["target_code"],
                "validator_id": row["validator_id"],
            }
            for row in operators
            if row["target_code"]
        ],
    }


#: Manifest keys that name the world the corpus ran in rather than the corpus itself, so they stay
#: out of the corpus *identity*.
#:
#: ``generator_fingerprint`` digests CPython bytecode, so the identical corpus publishes a different
#: number on every interpreter. ``spec_fingerprint`` is not interpreter-derived, but it identifies
#: the *spec* -- the thresholds and the judgement -- and the spec already has its own stable
#: identity, so folding it in would make a spec revision look like a corpus change. What the corpus
#: identity answers is narrower and more useful: "is this the same frozen set of cases and codes",
#: which is precisely the question two machines must be able to agree on.
CORPUS_IDENTITY_EXCLUDED_KEYS: tuple[str, ...] = ("spec_fingerprint", "generator_fingerprint")


def core_corpus_digest(version: str | None = None) -> str:
    """The corpus identity: interpreter-independent, so two machines compute the same value.

    Published beside :func:`core_corpus_fingerprint` rather than replacing it. The fingerprint
    keeps answering the provenance question it has always answered ("this definition, this code,
    on this machine"); the digest answers the one a comparison needs ("this definition").
    """

    text = json.dumps(
        core_corpus_digest_manifest(version),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def core_corpus_digest_manifest(version: str | None = None) -> dict[str, Any]:
    """The digest's input, published so a reviewer can recompute it instead of trusting it."""

    return {
        key: value
        for key, value in core_corpus_manifest(version).items()
        if key not in CORPUS_IDENTITY_EXCLUDED_KEYS
    }


def core_corpus_fingerprint() -> str:
    """Fingerprint of the corpus definition *as this interpreter and this code build it*.

    Runtime provenance, not an identity: it hashes :func:`core_corpus_manifest` whole, so it moves
    both with the operator bytecode and with the spec the corpus is judged by. The published values
    are recorded in the closeout evidence rather than pinned here, because pinning one would make
    "frozen" mean "frozen on the workstation that pinned it". Use :func:`core_corpus_digest` for
    anything two machines have to agree on.
    """

    text = json.dumps(
        core_corpus_manifest(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def spec_fingerprint(version: str | None = None) -> str:
    """Hash of a spec body -- the working one by default, or an archived version on request.

    The optional version exists so a reviewer can still recompute what a published payload names:
    ``spec_fingerprint("2")`` answers with the value the v2 evidence carries, from the frozen body,
    after the working spec has moved on.
    """

    text = json.dumps(
        spec_payload(version), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
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


def _entry_identity(entry: Any) -> tuple[str, str]:
    """``(operator_id, family)`` for a working operator or for an archived spec row."""

    if isinstance(entry, Mapping):
        return str(entry["operator_id"]), str(entry["family"])
    return entry.operator_id, entry.family


def operators_for_family(
    family: str, *, catalogue: Mapping[str, Any] | None = None
) -> list[str]:
    """The round-robin order for one family, sorted so the order cannot drift.

    ``catalogue`` is the operator table to read: the working one by default, or the row mapping an
    archived spec publishes. It exists for the same reason the archive does -- a superseded case
    set has to stay re-derivable, which means its *rotation* has to be recoverable and not only its
    fingerprint.
    """

    entries = MUTATIONS if catalogue is None else catalogue
    return sorted(
        operator_id
        for operator_id, entry_family in (_entry_identity(entry) for entry in entries.values())
        if entry_family == family
    )


def generate_cases(
    *,
    candidate_sha: str,
    family: str,
    count: int,
    suite: str = "acceptance",
    spec: str | None = None,
    catalogue: Mapping[str, Any] | None = None,
) -> list[BenchmarkCase]:
    """Deterministically derive ``count`` cases for one family.

    Operator selection is round-robin over the family's catalogue and the seed is derived from
    ``(spec fingerprint, candidate SHA, family, index)``: same candidate, same cases; different
    candidate, different cases. Nothing here is sampled at run time, so a case cannot be
    dropped because it failed.

    ``spec`` and ``catalogue`` together are what re-derive a superseded case set: pass an archived
    fingerprint and its archived catalogue and the resulting cases are the ones that spec ran.
    """

    spec_fingerprint_value = spec or spec_fingerprint()
    operator_ids = operators_for_family(family, catalogue=catalogue)
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
    "CORE_CORPUS_ID",
    "CORE_CORPUS_VERSION",
    "CORPUS_IDENTITY_EXCLUDED_KEYS",
    "DEV_CASES_PER_FAMILY",
    "FAMILIES",
    "FAMILY_TITLES",
    "MUTATIONS",
    "SAFETY_CASES",
    "SPEC_FINGERPRINT_V2",
    "SUCCESS_ORACLE_VERSION",
    "THRESHOLDS",
    "BaseDrawing",
    "BenchmarkCase",
    "MutationOperator",
    "archived_operator_catalogue",
    "MutationResult",
    "build_base_drawing",
    "core_corpus_digest",
    "core_corpus_digest_manifest",
    "core_corpus_fingerprint",
    "core_corpus_manifest",
    "derive_seed",
    "generate_cases",
    "generate_suite",
    "generator_fingerprint",
    "operators_for_family",
    "spec_fingerprint",
    "spec_payload",
    "supported_code_manifest",
]
