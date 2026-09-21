"""The M5 scale track: the same repairs, on a real large drawing (baseline §G).

M4 could argue that the analyzer's work had not changed and skip re-measuring the big drawing.
M5 cannot: the repair path adds a localized context, a shadow candidate and a replan loop, and
"does the agent still converge when the drawing has ten thousand elements" is exactly the
question a scale track exists to answer.

What this module measures, per case:

* the frozen scope's size, so "localized" is a number rather than an adjective;
* touched existing ids, context bytes, attempts and validation time;
* RSS and wall clock, reported as a baseline rather than a cross-machine gate (§F: no uniform
  second-based performance gate).

The original drawing is never modified: every case works on its own imported copy, and the
source file is identified by SHA-256 so the evidence names the bytes it was measured on.
"""

from __future__ import annotations

import hashlib
import resource
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .models import (
    AddElementOperation,
    CreateDocumentRequest,
    Point,
    SymbolElement,
)
from .repair_benchmark import (
    MUTATIONS,
    BaseDrawing,
    BenchmarkCase,
    derive_seed,
    spec_fingerprint,
)
from .repair_benchmark_runner import BenchmarkContext, run_case
from .repair_evidence import RepairCaseRecord, RepairContractModel
from .repair_planner import RepairPlanner
from .symbols import SymbolRegistry

SCALE_REPORT_SCHEMA = "pid-agent.repair-scale-report"

#: Five deterministic cases across five families (§G: 5 cases, at least 3 families, 5/5 oracles).
SCALE_CASES: tuple[tuple[str, str], ...] = (
    ("scale:F1", "f1_line_medium_missing"),
    ("scale:F2", "f2_endpoint_dangling"),
    ("scale:F4", "f4_micro_segment"),
    ("scale:F5", "f5_node_overlap"),
    ("scale:F6", "f6_replan_once"),
)

#: The three cases fixed for the real-model scale run. Named here, before the model runs, for the
#: reason §G gives: a case list chosen after seeing which repairs failed measures nothing.
SCALE_MODEL_CASES: tuple[str, ...] = ("scale:F1", "scale:F2", "scale:F5")


class ScaleSetupError(RuntimeError):
    """The scale source could not be prepared at all — a fixture problem, not a repair failure."""


@dataclass
class ScaleSource:
    """Where the big drawing comes from: a real DWG, or a synthetic one for reproducible CI."""

    kind: Literal["dwg", "synthetic"]
    path: str = ""
    element_target: int = 400
    #: A real CAD file contains geometry, not a semantic P&ID: the named drawing imports 9757
    #: lines/polylines/circles and nothing else. A repair case needs an element with ports to
    #: break, so when the imported copy has none the working copy gets one governed valve train.
    #: This is off-by-default-honest rather than hidden: the report says whether it was used and
    #: how many elements it added.
    semantic_seed: bool = True

    def describe(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind, "semantic_seed": self.semantic_seed}
        if self.kind == "dwg":
            source = Path(self.path)
            payload["file_name"] = source.name
            payload["source_sha256"] = (
                hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else ""
            )
            payload["source_bytes"] = source.stat().st_size if source.is_file() else 0
        else:
            payload["element_target"] = self.element_target
        return payload


class ScaleCaseMetrics(RepairContractModel):
    """One scale case: the repair verdict plus the numbers that make "large" measurable."""

    case_id: str
    family: str
    operator_id: str
    classification: str
    failure_code: str = ""
    attempts: int = 0
    scope_size: int = 0
    touched_existing_ids: int = 0
    context_bytes: int = 0
    context_elements: int = 0
    shadow_validation_ms: int = 0
    plan_ms: int = 0
    apply_ms: int = 0
    validation_ms: int = 0
    wall_clock_ms: int = 0
    rss_bytes: int = 0
    writes: int = 0


class ScaleReport(RepairContractModel):
    schema_name: Literal["pid-agent.repair-scale-report"] = Field(
        default=SCALE_REPORT_SCHEMA, alias="schema"
    )
    version: Literal[1] = 1
    track: str = "deterministic"
    candidate_sha: str = ""
    source: dict[str, Any] = Field(default_factory=dict)
    document_id: str = ""
    element_count: int = 0
    connector_count: int = 0
    symbol_count: int = 0
    import_report: dict[str, Any] = Field(default_factory=dict)
    semantic_seed: dict[str, Any] = Field(default_factory=dict)
    import_ms: int = 0
    base_validation_ms: int = 0
    role_map: dict[str, str] = Field(default_factory=dict)
    cases: list[ScaleCaseMetrics] = Field(default_factory=list)
    passed_cases: int = 0
    total_cases: int = 0
    governance_violations: int = 0
    planner: dict[str, Any] | None = None
    reasons: list[str] = Field(default_factory=list)

    def passed(self) -> bool:
        return (
            self.total_cases > 0
            and self.passed_cases == self.total_cases
            and self.governance_violations == 0
        )


# -- preparing the working copy ---------------------------------------------- #


#: A train of this many ball valves joined end to end; the drawing is a grid of trains.
_TRAIN_LENGTH = 8
_TRAIN_SPACING_X = 240.0
_TRAIN_SPACING_Y = 220.0
_SYMBOL_SIZE = 60.0


def build_synthetic_drawing(
    service: Any,
    registry: SymbolRegistry,
    *,
    element_target: int = 400,
    name: str = "M5 scale base (synthetic)",
) -> str:
    """A large, legal P&ID built through the governed path: a grid of valve trains.

    Not a benchmark base and not a simulation of one: it exists so the scale track can run in CI
    without the private drawing, with the same *shape* of difficulty (hundreds of elements, many
    neighbours, real ports and connectors).

    Two properties matter more than the size, and both are why this is not simply many elements:

    * every connector is produced by ``ConnectPortsOperation`` — the same semantic path the
      production connect-ports command uses — so every endpoint is really bound to a real port,
      and the drawing is *legal* rather than merely big. A drawing full of dangling endpoints
      would make every scale case a re-run of F2;
    * the canvas grows with the grid, so the field of equipment is inside the page and the
      out-of-bounds rule is not silently firing on every symbol.
    """

    from .agent_semantic_models import ConnectPortsOperation
    from .repair_benchmark import _apply_semantic

    trains = max(2, element_target // (_TRAIN_LENGTH * 2))
    columns = 4
    rows = max(1, -(-trains // columns))
    width = 200.0 + columns * _TRAIN_LENGTH * _TRAIN_SPACING_X
    height = 200.0 + rows * _TRAIN_SPACING_Y
    document = service.create_document(CreateDocumentRequest(name=name, width=width, height=height))

    # One governed transaction per train, not one for the whole field. The compiler's annotation
    # polish re-emits a text annotation per symbol *with a label*, so a single 300-element
    # transaction overruns the 1000-operation ceiling and the base drawing would be created
    # without its annotations -- a fixture defect that would look like a validation failure
    # later. Chunking keeps every individual transaction comfortably inside the ceiling while
    # the drawing as a whole grows to whatever size the track asks for.
    for train in range(trains):
        column, row = train % columns, train // columns
        base_x = 120.0 + column * _TRAIN_LENGTH * _TRAIN_SPACING_X
        base_y = 120.0 + row * _TRAIN_SPACING_Y
        chunk: list[Any] = []
        for index in range(_TRAIN_LENGTH):
            tag = f"HV-{1000 + train * _TRAIN_LENGTH + index}"
            element_id = f"sv_{train:03d}_{index:02d}"
            chunk.append(
                AddElementOperation(
                    element=SymbolElement(
                        id=element_id,
                        symbol_key="ball_valve",
                        position=Point(x=base_x + index * _TRAIN_SPACING_X, y=base_y),
                        width=_SYMBOL_SIZE,
                        height=_SYMBOL_SIZE,
                        label=tag,
                        properties={"tag": tag},
                    )
                )
            )
        for index in range(_TRAIN_LENGTH - 1):
            chunk.append(
                ConnectPortsOperation(
                    connector_id=f"sp_{train:03d}_{index:02d}",
                    source_element_id=f"sv_{train:03d}_{index:02d}",
                    source_port_id="out",
                    target_element_id=f"sv_{train:03d}_{index + 1:02d}",
                    target_port_id="in",
                    routing="orthogonal",
                    process_tag=f"L-{train:03d}-{index:02d}",
                    medium="process",
                    nominal_diameter="DN50",
                    flow_direction="forward",
                )
            )
        _apply_semantic(service, document.id, chunk, f"scale.base.train{train:03d}")
    # The trains are one-sided by construction (every valve in a train is bound), so the field
    # gets a separate, deliberately *unanchored* set of equipment: legal drafting state, and the
    # only elements in the drawing that can be moved without dragging a routed line along.
    unanchored = max(2, trains // 2)
    loose: list[Any] = []
    for index in range(unanchored):
        tag = f"P-{2000 + index}"
        loose.append(
            AddElementOperation(
                element=SymbolElement(
                    id=f"pv_{index:04d}",
                    symbol_key="centrifugal_pump",
                    position=Point(
                        x=120.0 + (index % columns) * _TRAIN_LENGTH * _TRAIN_SPACING_X,
                        y=height - 120.0,
                    ),
                    width=70,
                    height=70,
                    label=tag,
                    properties={"tag": tag},
                )
            )
        )
    _apply_semantic(service, document.id, loose, "scale.base.unanchored")
    return document.id


def _is_bound_symbol(element: Any, document_elements: dict[str, Any]) -> bool:
    return element.type == "symbol" and element.id in document_elements


def role_map_for(document: Any) -> dict[str, str]:
    """Resolve the benchmark roles (``p1``/``v1``/``v2``/``v3``) against a real drawing.

    The operators are written against roles precisely so they can run on a drawing whose element
    ids nobody knows in advance. The mapping is derived from the drawing's own structure — a
    connector with both endpoints bound is a line to repair, its two ends are the equipment that
    line joins, and an element the trunk does not touch is the movable one — and it is published
    with the report so a reviewer can check it rather than trust it.
    """

    elements = {element.id: element for element in document.elements}
    connectors = sorted(
        (element for element in document.elements if element.type == "connector"),
        key=lambda item: item.id,
    )
    trunk = next(
        (
            connector
            for connector in connectors
            if connector.source is not None
            and connector.target is not None
            and connector.source.element_id
            and connector.target.element_id
            and _is_bound_symbol(elements.get(connector.source.element_id), elements)
            and _is_bound_symbol(elements.get(connector.target.element_id), elements)
            and len(connector.points) >= 2
        ),
        None,
    )
    if trunk is None:
        raise ScaleSetupError("the drawing has no connector with both endpoints bound")
    roles = {
        "p1": trunk.id,
        "v1": trunk.source.element_id,
        "v2": trunk.target.element_id,
    }
    touching = [
        connector
        for connector in connectors
        if connector.id != trunk.id
        and connector.source is not None
        and connector.target is not None
        and roles["v2"] in {connector.source.element_id, connector.target.element_id}
    ]
    if touching:
        roles["p2"] = touching[0].id
    elif len(connectors) > 1:
        roles["p2"] = connectors[0].id if connectors[0].id != trunk.id else connectors[1].id
    neighbours = {
        connector.source.element_id
        for connector in connectors
        if connector.source is not None and connector.source.element_id
    } | {
        connector.target.element_id
        for connector in connectors
        if connector.target is not None and connector.target.element_id
    }

    def movable(element_id: str) -> bool:
        # "Movable" is the property the F5 operators actually need: an element no connector
        # terminates on, so repositioning it cannot silently re-route a line and turn a
        # collision case into a routing case. It is a preference, not a requirement -- a
        # drawing whose every symbol is bound still gets a `v3`, and the report shows which
        # one, so a reviewer can see that the collision was measured on a bound element.
        return element_id not in neighbours

    others = [
        element.id
        for element in sorted(document.elements, key=lambda item: item.id)
        if element.type == "symbol" and element.id not in {roles["v1"], roles["v2"]}
    ]
    if not others:
        raise ScaleSetupError("the drawing has no third symbol to move")
    roles["v3"] = next((element_id for element_id in others if movable(element_id)), others[0])
    return roles


def _rss_bytes() -> int:
    """Peak RSS, normalized to bytes (``ru_maxrss`` is KiB on Linux and bytes on macOS)."""

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def run_scale_suite(
    context: BenchmarkContext,
    *,
    source: ScaleSource | dict[str, Any],
    candidate_sha: str = "",
    planner: RepairPlanner | None = None,
    case_ids: tuple[str, ...] | None = None,
    progress: Any = None,
) -> ScaleReport:
    """Import a working copy, inject the fixed defects, and repair each one.

    Every case gets its **own** imported copy: a scale run must not be able to pass because an
    earlier case already moved the thing it was about to break.
    """

    if isinstance(source, dict):
        source = ScaleSource(**source)
    selected = [case for case in SCALE_CASES if case_ids is None or case[0] in case_ids]
    # The seed index is the case's position in the frozen list, so re-ordering the list is a
    # visible change to the evidence rather than a silent re-roll of every seed.
    case_index = {case_id: index for index, (case_id, _) in enumerate(SCALE_CASES)}
    reasons: list[str] = []
    metrics: list[ScaleCaseMetrics] = []
    element_count = 0
    role_map: dict[str, str] = {}
    document_id = ""
    import_ms = 0
    base_validation_ms = 0

    import_report: dict[str, Any] = {}
    semantic_seed: dict[str, Any] = {"used": False}
    symbol_count = 0
    connector_count = 0
    for case_id, operator_id in selected:
        index = case_index[case_id]
        import_started = time.perf_counter()
        staged = _stage_working_copy(context, source, operator_id)
        import_ms = int((time.perf_counter() - import_started) * 1000)
        document = context.service.get_document(staged.drawing.document_id)
        element_count = staged.element_count
        symbol_count = staged.symbol_count
        connector_count = staged.connector_count
        import_report = staged.import_report
        semantic_seed = staged.semantic_seed
        role_map = dict(staged.drawing.elements)
        document_id = staged.drawing.document_id
        drawing = staged.drawing

        validation_started = time.perf_counter()
        from .validation_engine import run_validation

        run_validation(document, context.registry, context.profile, service=context.service)
        base_validation_ms = int((time.perf_counter() - validation_started) * 1000)

        family = MUTATIONS[operator_id].family
        case = BenchmarkCase(
            case_id=case_id,
            family=family,
            operator_id=operator_id,
            # A fixed list, but the seed is still derived the way the frozen suites derive
            # theirs, so the same candidate reproduces the same scale run. ``hash()`` would
            # not: it is salted per process, and evidence that changes every run is not
            # evidence.
            index=index,
            seed=derive_seed(
                spec=spec_fingerprint(),
                candidate_sha=candidate_sha,
                family=family,
                index=index,
            ),
            suite="scale",
        )
        started = time.perf_counter()
        record: RepairCaseRecord = run_case(
            context,
            case,
            planner=planner,
            candidate_sha=candidate_sha,
            base_drawing=drawing,
        )
        wall_clock_ms = int((time.perf_counter() - started) * 1000)
        metrics.append(_metrics_for(record, operator_id, wall_clock_ms))
        if progress is not None:
            progress(case_id, record)
        if record.write_count > 1:
            reasons.append(f"{case_id} wrote {record.write_count} times")

    passed = sum(1 for item in metrics if _is_success(item))
    return ScaleReport(
        track="deterministic" if planner is None else "model",
        candidate_sha=candidate_sha,
        source=source.describe(),
        document_id=document_id,
        element_count=element_count,
        connector_count=connector_count,
        symbol_count=symbol_count,
        import_report=import_report,
        semantic_seed=semantic_seed,
        import_ms=import_ms,
        base_validation_ms=base_validation_ms,
        role_map=role_map,
        cases=metrics,
        passed_cases=passed,
        total_cases=len(metrics),
        governance_violations=sum(1 for item in metrics if item.writes > 1),
        planner=_planner_payload(planner),
        reasons=reasons,
    )


@dataclass
class StagedDrawing:
    """One case's working copy plus the facts the report has to publish about how it was made."""

    drawing: BaseDrawing
    element_count: int = 0
    symbol_count: int = 0
    connector_count: int = 0
    import_report: dict[str, Any] = field(default_factory=dict)
    semantic_seed: dict[str, Any] = field(default_factory=dict)


def _import_drawing(
    context: BenchmarkContext, source: ScaleSource, operator_id: str
) -> tuple[str, dict[str, Any]]:
    """Import the named CAD file as a governed document and return its report (§G1).

    The import goes through ``CadImporter`` — the same path ``pid-agent import-cad`` uses — so the
    numbers the report publishes (format detail, converter and its version, element counts,
    duration) are the production ones, not a benchmark-specific reader's.
    """

    from .audit_models import AuditContext
    from .cad_import import CadImporter, CadImportError
    from .cad_models import CadImportOptions

    path = Path(source.path)
    if not path.is_file():
        raise ScaleSetupError(f"scale source not found: {path}")
    importer = CadImporter(context.service, max_source_bytes=path.stat().st_size + 1)
    try:
        result = importer.import_path(
            path,
            options=CadImportOptions(),
            audit=AuditContext(
                actor="m5-scale-track",
                surface="internal",
                tool_name="import_cad_drawing",
                label=f"M5 scale working copy: {path.name}",
                metadata={"case": operator_id},
            ),
            source="system",
        )
    except CadImportError as exc:
        raise ScaleSetupError(f"{exc.code}: {exc.message}") from exc
    return result.document_id, result.model_dump(mode="json")["report"]


def _seed_semantic_train(context: BenchmarkContext, document_id: str, *, operator_id: str) -> int:
    """Add one governed valve train to an imported drawing that has nothing to repair.

    Required, not convenient: a real CAD file is geometry, and this module will not pretend a
    line element is a connector with ports. The seed is written through the semantic compiler
    and the governed transaction path, so it is an ordinary revision of the working copy — the
    defect the case injects afterwards is still a real governed mutation of a real drawing.
    """

    from .agent_semantic_models import ConnectPortsOperation
    from .repair_benchmark import _apply_semantic

    prefix = f"scaleseed_{operator_id.replace(':', '_')}"
    operations: list[Any] = []
    for index in range(_TRAIN_LENGTH):
        tag = f"HV-{9000 + index}"
        operations.append(
            AddElementOperation(
                element=SymbolElement(
                    id=f"{prefix}_v{index}",
                    symbol_key="ball_valve",
                    position=Point(x=600.0 + index * _TRAIN_SPACING_X, y=600.0),
                    width=_SYMBOL_SIZE,
                    height=_SYMBOL_SIZE,
                    label=tag,
                    properties={"tag": tag},
                )
            )
        )
    for index in range(_TRAIN_LENGTH - 1):
        operations.append(
            ConnectPortsOperation(
                connector_id=f"{prefix}_p{index}",
                source_element_id=f"{prefix}_v{index}",
                source_port_id="out",
                target_element_id=f"{prefix}_v{index + 1}",
                target_port_id="in",
                routing="orthogonal",
                process_tag=f"L-SEED-{index:02d}",
                medium="process",
                nominal_diameter="DN50",
                flow_direction="forward",
            )
        )
    return _apply_semantic(context.service, document_id, operations, "scale.seed")


def _stage_working_copy(
    context: BenchmarkContext, source: ScaleSource, operator_id: str
) -> StagedDrawing:
    """One fresh working copy per case, with the roles the operator needs already known."""

    import_report: dict[str, Any] = {}
    seed: dict[str, Any] = {"used": False}
    if source.kind == "synthetic":
        document_id = build_synthetic_drawing(
            context.service,
            context.registry,
            element_target=source.element_target,
            name=f"M5 scale base (synthetic, {operator_id})",
        )
    else:
        document_id, import_report = _import_drawing(context, source, operator_id)
        if source.semantic_seed:
            imported = context.service.get_document(document_id)
            if not _has_repairable_connector(imported):
                before = len(imported.elements)
                _seed_semantic_train(context, document_id, operator_id=operator_id)
                seeded = context.service.get_document(document_id)
                seed = {
                    "used": True,
                    "reason": (
                        "the imported drawing contains raw CAD geometry only "
                        "(no symbol and no connector with bound ports); a repair case needs an "
                        "element with ports to break"
                    ),
                    "added_elements": len(seeded.elements) - before,
                }
    document = context.service.get_document(document_id)
    roles = role_map_for(document)
    return StagedDrawing(
        drawing=BaseDrawing(document_id=document_id, elements=roles),
        element_count=len(document.elements),
        symbol_count=sum(1 for element in document.elements if element.type == "symbol"),
        connector_count=sum(1 for element in document.elements if element.type == "connector"),
        import_report=import_report,
        semantic_seed=seed,
    )


def _has_repairable_connector(document: Any) -> bool:
    """Whether this drawing already has a connector whose endpoints are bound to real ports."""

    elements = {element.id: element for element in document.elements}
    return any(
        element.type == "connector"
        and element.source is not None
        and element.target is not None
        and _is_bound_symbol(elements.get(element.source.element_id or ""), elements)
        and _is_bound_symbol(elements.get(element.target.element_id or ""), elements)
        for element in document.elements
    )


def _planner_payload(planner: RepairPlanner | None) -> dict[str, Any] | None:
    describe = getattr(planner, "identity", None)
    payload = describe() if callable(describe) else None
    return payload.model_dump(mode="json") if payload is not None else None


def _is_success(metrics: ScaleCaseMetrics) -> bool:
    return metrics.classification == "success"


def _metrics_for(
    record: RepairCaseRecord, operator_id: str, wall_clock_ms: int
) -> ScaleCaseMetrics:
    timings = record.timings_ms or {}
    metrics = record.attempt_metrics or []
    return ScaleCaseMetrics(
        case_id=record.case_id,
        family=record.family,
        operator_id=operator_id,
        classification=record.classification,
        failure_code=record.failure_code,
        attempts=record.attempts,
        scope_size=len(record.protected_pre) + len(record.target_element_ids),
        touched_existing_ids=len(record.attempt_plan_hashes),
        context_bytes=max((metric.context_bytes for metric in metrics), default=0),
        context_elements=max((metric.context_elements for metric in metrics), default=0),
        shadow_validation_ms=int(timings.get("shadow_validate_ms", 0)),
        plan_ms=int(timings.get("plan_ms", 0)),
        apply_ms=int(timings.get("apply_ms", 0)),
        validation_ms=int(timings.get("post_validate_ms", 0)),
        wall_clock_ms=wall_clock_ms,
        rss_bytes=_rss_bytes(),
        writes=record.write_count,
    )


__all__ = [
    "SCALE_CASES",
    "SCALE_MODEL_CASES",
    "SCALE_REPORT_SCHEMA",
    "ScaleReport",
    "ScaleSetupError",
    "ScaleSource",
    "StagedDrawing",
    "build_synthetic_drawing",
    "role_map_for",
    "run_scale_suite",
]
