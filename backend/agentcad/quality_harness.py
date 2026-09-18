from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from math import ceil, isfinite
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from pydantic import Field

from .agent_semantic_models import SemanticAgentPlan
from .diagram_quality import analyze_diagram_quality
from .engineering_ir import TOPO_KINDS
from .models import (
    AddElementOperation,
    ConnectorElement,
    ConnectorEndpoint,
    CreateDocumentRequest,
    Document,
    JunctionElement,
    Point,
    StrictModel,
    SymbolDefinition,
    SymbolElement,
    TransactionRequest,
)
from .semantic_compiler_engine import SemanticTransactionCompiler
from .service import DocumentService
from .store import SQLiteDocumentStore
from .svg import render_svg
from .symbols import SymbolCatalogLoadError, SymbolRegistry

QUALITY_HARNESS_SCHEMA = "pid-agent.quality-harness"
QUALITY_HARNESS_VERSION = 2
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_SUPPORTED_SHAPES = {"line", "polyline", "rect", "circle", "path", "text"}
_EPSILON = 1e-6


class QualityHarnessFinding(StrictModel):
    code: str
    message: str
    symbol_key: str | None = None


class QualityHarnessCaseResult(StrictModel):
    name: str
    status: Literal["passed", "failed"]
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    findings: list[QualityHarnessFinding] = Field(default_factory=list)


class QualityHarnessReport(StrictModel):
    schema_name: Literal["pid-agent.quality-harness"] = Field(
        default=QUALITY_HARNESS_SCHEMA,
        alias="schema",
    )
    version: Literal[2] = QUALITY_HARNESS_VERSION
    passed: bool
    total_cases: int
    passed_cases: int
    failed_cases: int
    symbol_count: int
    cases: list[QualityHarnessCaseResult]


class _HarnessFailure(AssertionError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise _HarnessFailure(code, message)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(float(value))
    )


def _shape_findings(symbol: SymbolDefinition, index: int, shape: dict[str, Any]):
    prefix = f"shape[{index}]"
    kind = shape.get("type")
    if kind not in _SUPPORTED_SHAPES:
        return [
            QualityHarnessFinding(
                code="SYMBOL_SHAPE_TYPE_UNSUPPORTED",
                message=f"{prefix} uses unsupported type {kind!r}",
                symbol_key=symbol.key,
            )
        ]

    findings: list[QualityHarnessFinding] = []
    raw_dash = shape.get("dash")
    if raw_dash is not None:
        if (
            not isinstance(raw_dash, list)
            or not raw_dash
            or not all(_is_number(value) and value > 0 for value in raw_dash)
        ):
            findings.append(
                QualityHarnessFinding(
                    code="SYMBOL_DASH_INVALID",
                    message=(
                        f"{prefix}.dash must be a non-empty list of positive "
                        "finite numbers"
                    ),
                    symbol_key=symbol.key,
                )
            )

    def require_numbers(*fields: str) -> None:
        for field in fields:
            if not _is_number(shape.get(field)):
                findings.append(
                    QualityHarnessFinding(
                        code="SYMBOL_SHAPE_NUMBER_INVALID",
                        message=f"{prefix}.{field} must be a finite number",
                        symbol_key=symbol.key,
                    )
                )

    if kind == "line":
        require_numbers("x1", "y1", "x2", "y2")
    elif kind == "polyline":
        points = shape.get("points")
        if not isinstance(points, list) or len(points) < 2:
            findings.append(
                QualityHarnessFinding(
                    code="SYMBOL_POLYLINE_POINTS_INVALID",
                    message=f"{prefix}.points must contain at least two coordinate pairs",
                    symbol_key=symbol.key,
                )
            )
        else:
            for point_index, point in enumerate(points):
                if (
                    not isinstance(point, (list, tuple))
                    or len(point) != 2
                    or not all(_is_number(value) for value in point)
                ):
                    findings.append(
                        QualityHarnessFinding(
                            code="SYMBOL_POLYLINE_POINT_INVALID",
                            message=(
                                f"{prefix}.points[{point_index}] must be two finite numbers"
                            ),
                            symbol_key=symbol.key,
                        )
                    )
    elif kind == "rect":
        require_numbers("x", "y", "width", "height")
        if _is_number(shape.get("width")) and float(shape["width"]) <= 0:
            findings.append(
                QualityHarnessFinding(
                    code="SYMBOL_RECT_SIZE_INVALID",
                    message=f"{prefix}.width must be greater than zero",
                    symbol_key=symbol.key,
                )
            )
        if _is_number(shape.get("height")) and float(shape["height"]) <= 0:
            findings.append(
                QualityHarnessFinding(
                    code="SYMBOL_RECT_SIZE_INVALID",
                    message=f"{prefix}.height must be greater than zero",
                    symbol_key=symbol.key,
                )
            )
        if "rx" in shape and (
            not _is_number(shape["rx"]) or float(shape["rx"]) < 0
        ):
            findings.append(
                QualityHarnessFinding(
                    code="SYMBOL_RECT_RADIUS_INVALID",
                    message=f"{prefix}.rx must be a non-negative finite number",
                    symbol_key=symbol.key,
                )
            )
    elif kind == "circle":
        require_numbers("cx", "cy", "r")
        if _is_number(shape.get("r")) and float(shape["r"]) <= 0:
            findings.append(
                QualityHarnessFinding(
                    code="SYMBOL_CIRCLE_RADIUS_INVALID",
                    message=f"{prefix}.r must be greater than zero",
                    symbol_key=symbol.key,
                )
            )
    elif kind == "path":
        if not isinstance(shape.get("d"), str) or not shape["d"].strip():
            findings.append(
                QualityHarnessFinding(
                    code="SYMBOL_PATH_DATA_MISSING",
                    message=f"{prefix}.d must be a non-empty SVG path",
                    symbol_key=symbol.key,
                )
            )
    elif kind == "text":
        require_numbers("x", "y")
        if not isinstance(shape.get("text"), str) or not shape["text"].strip():
            findings.append(
                QualityHarnessFinding(
                    code="SYMBOL_TEXT_MISSING",
                    message=f"{prefix}.text must be non-empty",
                    symbol_key=symbol.key,
                )
            )
        anchor = shape.get("anchor", "middle")
        if anchor not in {"start", "middle", "end"}:
            findings.append(
                QualityHarnessFinding(
                    code="SYMBOL_TEXT_ANCHOR_INVALID",
                    message=f"{prefix}.anchor must be start, middle, or end",
                    symbol_key=symbol.key,
                )
            )
    return findings


def _catalog_case(symbols: SymbolRegistry) -> QualityHarnessCaseResult:
    definitions = symbols.list()
    findings: list[QualityHarnessFinding] = []
    if not definitions:
        findings.append(
            QualityHarnessFinding(
                code="SYMBOL_CATALOG_EMPTY",
                message="the active symbol catalog contains no symbols",
            )
        )

    keys: set[str] = set()
    for symbol in definitions:
        if symbol.key in keys:
            findings.append(
                QualityHarnessFinding(
                    code="SYMBOL_KEY_DUPLICATE",
                    message="active symbol keys must be unique",
                    symbol_key=symbol.key,
                )
            )
        keys.add(symbol.key)
        if not _IDENTIFIER.fullmatch(symbol.key):
            findings.append(
                QualityHarnessFinding(
                    code="SYMBOL_KEY_INVALID",
                    message="key must match ^[a-z][a-z0-9_]*$",
                    symbol_key=symbol.key,
                )
            )
        for field_name, value in (
            ("name", symbol.name),
            ("category", symbol.category),
            ("description", symbol.description),
        ):
            if not value.strip():
                findings.append(
                    QualityHarnessFinding(
                        code=f"SYMBOL_{field_name.upper()}_MISSING",
                        message=f"{field_name} must be non-empty",
                        symbol_key=symbol.key,
                    )
                )
        if not symbol.shapes:
            findings.append(
                QualityHarnessFinding(
                    code="SYMBOL_SHAPES_EMPTY",
                    message="at least one rendered shape is required",
                    symbol_key=symbol.key,
                )
            )

        port_ids: set[str] = set()
        for port in symbol.ports:
            if port.id in port_ids:
                findings.append(
                    QualityHarnessFinding(
                        code="SYMBOL_PORT_ID_DUPLICATE",
                        message=f"port id {port.id!r} is duplicated",
                        symbol_key=symbol.key,
                    )
                )
            port_ids.add(port.id)
            if not _IDENTIFIER.fullmatch(port.id):
                findings.append(
                    QualityHarnessFinding(
                        code="SYMBOL_PORT_ID_INVALID",
                        message=f"port id {port.id!r} must be a stable lowercase identifier",
                        symbol_key=symbol.key,
                    )
                )
            if not port.name.strip():
                findings.append(
                    QualityHarnessFinding(
                        code="SYMBOL_PORT_NAME_MISSING",
                        message=f"port {port.id!r} has no display name",
                        symbol_key=symbol.key,
                    )
                )
            if not port.medium.strip():
                findings.append(
                    QualityHarnessFinding(
                        code="SYMBOL_PORT_MEDIUM_MISSING",
                        message=f"port {port.id!r} has no medium",
                        symbol_key=symbol.key,
                    )
                )
            if (
                port.x < -_EPSILON
                or port.x > symbol.width + _EPSILON
                or port.y < -_EPSILON
                or port.y > symbol.height + _EPSILON
            ):
                findings.append(
                    QualityHarnessFinding(
                        code="SYMBOL_PORT_OUT_OF_BOUNDS",
                        message=(
                            f"port {port.id!r} at ({port.x}, {port.y}) is outside "
                            f"{symbol.width}x{symbol.height}"
                        ),
                        symbol_key=symbol.key,
                    )
                )
        for index, shape in enumerate(symbol.shapes):
            findings.extend(_shape_findings(symbol, index, shape))

    if definitions:
        columns = 8
        elements = [
            SymbolElement(
                id=f"catalog_{symbol.key}",
                symbol_key=symbol.key,
                position=Point(
                    x=40 + (index % columns) * 220,
                    y=40 + (index // columns) * 220,
                ),
                width=symbol.width,
                height=symbol.height,
            )
            for index, symbol in enumerate(definitions)
        ]
        try:
            svg = render_svg(
                Document(
                    id="quality_harness_catalog",
                    name="Quality harness catalog",
                    canvas={
                        "width": columns * 220 + 80,
                        "height": max(320, ceil(len(elements) / columns) * 220 + 80),
                    },
                    elements=elements,
                ),
                symbols,
            )
            for symbol in definitions:
                if f'data-symbol-key="{symbol.key}"' not in svg:
                    findings.append(
                        QualityHarnessFinding(
                            code="SYMBOL_SVG_RENDER_MISSING",
                            message="symbol was absent from the catalog SVG smoke render",
                            symbol_key=symbol.key,
                        )
                    )
        except Exception as exc:
            findings.append(
                QualityHarnessFinding(
                    code="SYMBOL_SVG_RENDER_FAILED",
                    message=f"{type(exc).__name__}: {exc}",
                )
            )

    findings.sort(key=lambda item: (item.symbol_key or "", item.code, item.message))
    categories = Counter(symbol.category for symbol in definitions)
    passed = not findings
    return QualityHarnessCaseResult(
        name="symbol_catalog_integrity",
        status="passed" if passed else "failed",
        summary=(
            f"{len(definitions)} symbols validated and rendered"
            if passed
            else f"{len(findings)} catalog issue(s) found across {len(definitions)} symbols"
        ),
        details={
            "symbol_count": len(definitions),
            "category_count": len(categories),
            "categories": dict(sorted(categories.items())),
        },
        findings=findings,
    )


def _through_symbol(symbols: SymbolRegistry) -> tuple[SymbolDefinition, str, str]:
    candidates: list[tuple[int, str, SymbolDefinition, str, str]] = []
    for definition in symbols.list():
        for inlet in definition.ports:
            if inlet.direction not in {"in", "bidirectional"}:
                continue
            for outlet in definition.ports:
                if inlet.id == outlet.id or outlet.direction not in {"out", "bidirectional"}:
                    continue
                centered = (
                    abs(inlet.y - definition.height / 2) <= _EPSILON
                    and abs(outlet.y - definition.height / 2) <= _EPSILON
                )
                if not centered:
                    continue
                score = 0
                if definition.key == "ball_valve":
                    score += 100
                if inlet.id == "in":
                    score += 20
                if outlet.id == "out":
                    score += 20
                if inlet.direction == "in":
                    score += 5
                if outlet.direction == "out":
                    score += 5
                candidates.append(
                    (score, definition.key, definition, inlet.id, outlet.id)
                )
    if not candidates:
        raise _HarnessFailure(
            "CATALOG_THROUGH_SYMBOL_MISSING",
            "the catalog needs a straight-through symbol with centered input and output ports",
        )
    _, _, definition, inlet_id, outlet_id = sorted(
        candidates,
        key=lambda item: (-item[0], item[1], item[3], item[4]),
    )[0]
    return definition, inlet_id, outlet_id


def _branch_symbol(symbols: SymbolRegistry) -> tuple[SymbolDefinition, str]:
    definitions = symbols.list()
    preferred = next(
        (
            definition
            for definition in definitions
            if definition.key == "pressure_indicator" and definition.ports
        ),
        None,
    )
    if preferred is not None:
        return preferred, preferred.ports[0].id
    candidates = [definition for definition in definitions if definition.ports]
    if not candidates:
        raise _HarnessFailure(
            "CATALOG_CONNECTABLE_SYMBOL_MISSING",
            "the catalog needs at least one symbol with a real port",
        )
    definition = sorted(candidates, key=lambda item: item.key)[0]
    return definition, definition.ports[0].id


def _symbol_element(
    element_id: str,
    definition: SymbolDefinition,
    x: float,
    y: float,
    label: str,
) -> SymbolElement:
    return SymbolElement(
        id=element_id,
        symbol_key=definition.key,
        position=Point(x=x, y=y),
        width=definition.width,
        height=definition.height,
        label=label,
    )


def _bound_connector(
    connector_id: str,
    source_id: str,
    source_port_id: str,
    target_id: str,
    target_port_id: str,
) -> ConnectorElement:
    placeholder = Point(x=0, y=0)
    return ConnectorElement(
        id=connector_id,
        points=[placeholder, Point(x=1, y=0)],
        source=ConnectorEndpoint(
            element_id=source_id,
            port_id=source_port_id,
            point=placeholder,
        ),
        target=ConnectorEndpoint(
            element_id=target_id,
            port_id=target_port_id,
            point=placeholder,
        ),
        routing="orthogonal",
        process_tag="L-HARNESS-001",
        medium="process",
        nominal_diameter="DN50",
        flow_direction="forward",
    )


def _assert_orthogonal(connectors: list[ConnectorElement]) -> None:
    for connector in connectors:
        for first, second in zip(
            connector.points,
            connector.points[1:],
            strict=False,
        ):
            _require(
                abs(first.x - second.x) <= _EPSILON
                or abs(first.y - second.y) <= _EPSILON,
                "TOPOLOGY_NON_ORTHOGONAL",
                f"connector {connector.id} contains a non-orthogonal segment",
            )


def _atomic_topology_case(symbols: SymbolRegistry) -> QualityHarnessCaseResult:
    through, inlet_id, outlet_id = _through_symbol(symbols)
    branch_definition, branch_port_id = _branch_symbol(symbols)
    with TemporaryDirectory(prefix="pid-agent-quality-atomic-") as directory:
        service = DocumentService(
            SQLiteDocumentStore(Path(directory) / "atomic.db"),
            symbols,
        )
        document = service.create_document(
            CreateDocumentRequest(name="Offline atomic topology harness"),
            source="system",
        )
        source = _symbol_element("atomic_source", through, 80, 300, "S-H1")
        target = _symbol_element("atomic_target", through, 720, 300, "T-H1")
        branch = _symbol_element(
            "atomic_branch",
            branch_definition,
            380,
            80,
            "I-H1",
        )
        junction = JunctionElement(
            id="atomic_junction",
            position=Point(x=420, y=320),
        )
        transaction = TransactionRequest(
            expected_revision=document.revision,
            label="Offline atomic topology fixture",
            source="mcp",
            operations=[
                AddElementOperation(element=source),
                AddElementOperation(element=target),
                AddElementOperation(element=branch),
                AddElementOperation(element=junction),
                AddElementOperation(
                    element=_bound_connector(
                        "atomic_upstream",
                        source.id,
                        outlet_id,
                        junction.id,
                        "node",
                    )
                ),
                AddElementOperation(
                    element=_bound_connector(
                        "atomic_downstream",
                        junction.id,
                        "node",
                        target.id,
                        inlet_id,
                    )
                ),
                AddElementOperation(
                    element=_bound_connector(
                        "atomic_branch_pipe",
                        junction.id,
                        "node",
                        branch.id,
                        branch_port_id,
                    )
                ),
            ],
        )
        result = service.apply_transaction(document.id, transaction, source="mcp")
        persisted = service.get_document(document.id)
        connectors = [
            element for element in persisted.elements if element.type == "connector"
        ]
        degree = sum(
            endpoint is not None
            and endpoint.element_id == junction.id
            and endpoint.port_id == "node"
            for connector in connectors
            for endpoint in (connector.source, connector.target)
        )
        _require(result.document.revision == 1, "ATOMIC_REVISION_INVALID", "revision must advance once")
        _require(persisted.revision == 1, "ATOMIC_PERSISTENCE_FAILED", "transaction was not persisted")
        _require(
            len(persisted.elements) == 7,
            "ATOMIC_ELEMENT_COUNT_INVALID",
            "representative atomic transaction must persist seven elements",
        )
        _require(degree == 3, "ATOMIC_JUNCTION_DEGREE_INVALID", "junction must have degree three")
        _require(
            all(
                endpoint is not None and endpoint.element_id is not None
                for connector in connectors
                for endpoint in (connector.source, connector.target)
            ),
            "ATOMIC_FREE_ENDPOINT",
            "all representative topology endpoints must be bound",
        )
        _assert_orthogonal(connectors)
        summary = service.scene_summary(document.id)
        _require(
            summary["elements_by_type"]
            == {"symbol": 3, "junction": 1, "connector": 3},
            "ATOMIC_SCENE_SUMMARY_INVALID",
            "scene summary did not preserve the representative topology",
        )

    return QualityHarnessCaseResult(
        name="atomic_topology_transaction",
        status="passed",
        summary="one seven-element topology was committed atomically to temporary SQLite",
        details={
            "through_symbol_key": through.key,
            "branch_symbol_key": branch_definition.key,
            "revision": 1,
            "element_count": 7,
            "connector_count": 3,
            "junction_degree": 3,
        },
    )


def _semantic_agent_case(symbols: SymbolRegistry) -> QualityHarnessCaseResult:
    through, inlet_id, outlet_id = _through_symbol(symbols)
    instrument, instrument_port_id = _branch_symbol(symbols)
    with TemporaryDirectory(prefix="pid-agent-quality-semantic-") as directory:
        service = DocumentService(
            SQLiteDocumentStore(Path(directory) / "semantic.db"),
            symbols,
        )
        document = service.create_document(
            CreateDocumentRequest(name="Offline semantic Agent harness"),
            source="system",
        )
        source = _symbol_element("semantic_source", through, 100, 320, "S-H2")
        source_out = service._symbol_port_point(source, outlet_id)
        inlet = next(port for port in through.ports if port.id == inlet_id)
        target = _symbol_element(
            "semantic_target",
            through,
            800,
            source_out.y - inlet.y,
            "T-H2",
        )
        target_in = service._symbol_port_point(target, inlet_id)
        _require(
            abs(source_out.y - target_in.y) <= _EPSILON,
            "SEMANTIC_FIXTURE_ALIGNMENT_FAILED",
            "the harness could not align source and target process ports",
        )
        seeded = service.apply_transaction(
            document.id,
            TransactionRequest(
                expected_revision=document.revision,
                label="Seed semantic harness",
                source="system",
                operations=[
                    AddElementOperation(element=source),
                    AddElementOperation(element=target),
                ],
            ),
            source="system",
        ).document
        tap_point = Point(
            x=(source_out.x + target_in.x) / 2,
            y=source_out.y,
        )
        plan = SemanticAgentPlan.model_validate(
            {
                "plan_id": "offline_quality_harness_plan",
                "explanation": "Deterministic model-output fixture with real ports and a tap.",
                "transaction": {
                    "expected_revision": seeded.revision,
                    "label": "Offline semantic topology",
                    "operations": [
                        {
                            "op": "connect_ports",
                            "connector_id": "semantic_main",
                            "source_element_id": source.id,
                            "source_port_id": outlet_id,
                            "target_element_id": target.id,
                            "target_port_id": inlet_id,
                            "process_tag": "L-HARNESS-002",
                            "medium": "process",
                            "nominal_diameter": "DN80",
                            "flow_direction": "forward",
                        },
                        {
                            "op": "instrument_tap",
                            "main_connector_id": "semantic_main",
                            "junction_point": tap_point.model_dump(mode="json"),
                            "measurement": "pressure",
                            "instrument_label": "PT-H2",
                            "instrument_symbol_key": instrument.key,
                            "instrument_port_id": instrument_port_id,
                            "root_valve_symbol_key": through.key,
                            "root_valve_in_port_id": inlet_id,
                            "root_valve_out_port_id": outlet_id,
                            "junction_id": "semantic_junction",
                            "downstream_connector_id": "semantic_main_after_tap",
                            "root_valve_id": "semantic_root_valve",
                            "instrument_id": "semantic_instrument",
                            "junction_to_valve_connector_id": "semantic_branch_a",
                            "valve_to_instrument_connector_id": "semantic_branch_b",
                        },
                    ],
                },
            }
        )
        compiler = SemanticTransactionCompiler(service)
        compiled = compiler.compile(document.id, plan.transaction)
        _require(
            compiled.assessment.valid and compiled.transaction is not None,
            "SEMANTIC_VALID_PLAN_REJECTED",
            (
                "deterministic Agent plan did not compile: "
                + ", ".join(issue.code for issue in compiled.assessment.issues)
            ),
        )
        applied = service.apply_transaction(
            document.id,
            compiled.transaction,
            source="llm",
        ).document
        elements = {element.id: element for element in applied.elements}
        connectors = [
            element for element in applied.elements if element.type == "connector"
        ]
        junction_degree = sum(
            endpoint is not None
            and endpoint.element_id == "semantic_junction"
            and endpoint.port_id == "node"
            for connector in connectors
            for endpoint in (connector.source, connector.target)
        )
        _require(
            {"semantic_main", "semantic_main_after_tap", "semantic_branch_a", "semantic_branch_b"}
            .issubset(elements),
            "SEMANTIC_TOPOLOGY_INCOMPLETE",
            "compiled Agent output is missing a main or branch connector",
        )
        _require(
            junction_degree == 3,
            "SEMANTIC_JUNCTION_DEGREE_INVALID",
            "compiled Agent instrument tap must create a degree-three junction",
        )
        _assert_orthogonal(connectors)

        invalid_plan = SemanticAgentPlan.model_validate(
            {
                "plan_id": "offline_quality_harness_invalid_plan",
                "explanation": "Known-invalid fixture proving hallucinated ports are rejected.",
                "transaction": {
                    "expected_revision": applied.revision,
                    "label": "Reject hallucinated port",
                    "operations": [
                        {
                            "op": "connect_ports",
                            "connector_id": "semantic_rejected_pipe",
                            "source_element_id": source.id,
                            "source_port_id": "__missing_port__",
                            "target_element_id": target.id,
                            "target_port_id": inlet_id,
                        }
                    ],
                },
            }
        )
        rejected = compiler.compile(document.id, invalid_plan.transaction)
        issue_codes = [issue.code for issue in rejected.assessment.issues]
        _require(
            not rejected.assessment.valid
            and rejected.transaction is None
            and issue_codes == ["unknown_port"],
            "SEMANTIC_INVALID_PLAN_ACCEPTED",
            "the semantic compiler must reject a hallucinated port with unknown_port",
        )
        _require(
            service.get_document(document.id).revision == applied.revision,
            "SEMANTIC_REJECTION_MUTATED_DOCUMENT",
            "rejecting invalid Agent output must not mutate the document",
        )

    return QualityHarnessCaseResult(
        name="semantic_agent_output_contract",
        status="passed",
        summary="valid model-shaped JSON compiled and applied; a hallucinated port was rejected",
        details={
            "through_symbol_key": through.key,
            "instrument_symbol_key": instrument.key,
            "semantic_operation_count": len(plan.transaction.operations),
            "compiled_operation_count": len(compiled.transaction.operations),
            "final_revision": applied.revision,
            "final_element_count": len(applied.elements),
            "junction_degree": junction_degree,
            "rejected_issue_codes": issue_codes,
        },
    )


def _drafting_quality_case(symbols: SymbolRegistry) -> QualityHarnessCaseResult:
    through, inlet_id, outlet_id = _through_symbol(symbols)
    with TemporaryDirectory(prefix="pid-agent-quality-drafting-") as directory:
        service = DocumentService(
            SQLiteDocumentStore(Path(directory) / "drafting.db"),
            symbols,
        )
        document = service.create_document(
            CreateDocumentRequest(name="Offline drafting-quality harness"),
            source="system",
        )
        source = _symbol_element("draft_source", through, 120, 280, "S-D1")
        source_out = service._symbol_port_point(source, outlet_id)
        inlet = next(port for port in through.ports if port.id == inlet_id)
        target = _symbol_element(
            "draft_target",
            through,
            720,
            source_out.y - inlet.y,
            "T-D1",
        )
        seeded = service.apply_transaction(
            document.id,
            TransactionRequest(
                expected_revision=document.revision,
                source="system",
                label="Seed drafting harness",
                operations=[
                    AddElementOperation(element=source),
                    AddElementOperation(element=target),
                ],
            ),
            source="system",
        ).document
        plan = SemanticAgentPlan.model_validate(
            {
                "explanation": "One aligned process connection.",
                "transaction": {
                    "expected_revision": seeded.revision,
                    "label": "Drafting-quality route",
                    "operations": [
                        {
                            "op": "connect_ports",
                            "connector_id": "draft_pipe",
                            "source_element_id": source.id,
                            "source_port_id": outlet_id,
                            "target_element_id": target.id,
                            "target_port_id": inlet_id,
                            "flow_direction": "forward",
                        }
                    ],
                },
            }
        )
        compiled = SemanticTransactionCompiler(service).compile(
            document.id,
            plan.transaction,
        )
        _require(
            compiled.assessment.valid and compiled.transaction is not None,
            "DRAFTING_VALID_ROUTE_REJECTED",
            "an aligned semantic connection must compile",
        )
        applied = service.apply_transaction(
            document.id,
            compiled.transaction,
            source="llm",
        ).document
        good = analyze_diagram_quality(applied, symbols)
        _require(
            good.passed and good.score == 100,
            "DRAFTING_GOOD_ROUTE_FAILED",
            f"canonical aligned route scored {good.score}: {[item.code for item in good.issues]}",
        )

        bad = Document.model_validate(applied.model_dump(mode="python"))
        pipe = next(
            element
            for element in bad.elements
            if element.id == "draft_pipe" and element.type == "connector"
        )
        start, end = pipe.points[0], pipe.points[-1]
        pipe.points = [
            start,
            Point(x=start.x + 5, y=start.y),
            Point(x=start.x + 5, y=start.y + 5),
            Point(x=end.x, y=start.y + 5),
            end,
        ]
        pipe.routing = "manual"
        bad = Document.model_validate(bad.model_dump(mode="python"))
        rejected = analyze_diagram_quality(bad, symbols)
        rejected_codes = {issue.code for issue in rejected.issues}
        _require(
            not rejected.passed
            and {"MICRO_SEGMENT", "UNNECESSARY_BEND"}.issubset(rejected_codes),
            "DRAFTING_BAD_ROUTE_ACCEPTED",
            "sub-grid doglegs on aligned endpoints must fail deterministic drafting quality",
        )

    return QualityHarnessCaseResult(
        name="drafting_quality_contract",
        status="passed",
        summary="canonical route scored 100; micro-dogleg fixture was deterministically rejected",
        details={
            "good_score": good.score,
            "good_bends": good.metrics.total_bends,
            "rejected_issue_codes": sorted(rejected_codes),
        },
    )


def _engineering_graph_case(symbols: SymbolRegistry) -> QualityHarnessCaseResult:
    """Offline (model-free) golden contract for the M2 engineering semantic graph.

    Builds a real drawing through the service layer, derives the graph, and checks the
    invariants every downstream surface relies on: deterministic identity, no silent
    merge of duplicate tags, closed topology references, an exact partition of process
    objects into connectivity groups, a flow-aware trace, and an index that detects
    and repairs its own staleness without touching engineering content.
    """

    from .engineering_ir import build_engineering_graph, graph_fingerprint, trace_engineering_object
    from .project_index import ProjectIndexService

    through, inlet_id, outlet_id = _through_symbol(symbols)
    with TemporaryDirectory(prefix="pid-agent-quality-graph-") as directory:
        store = SQLiteDocumentStore(Path(directory) / "graph.db")
        service = DocumentService(store, symbols)
        document = service.create_document(
            CreateDocumentRequest(name="Offline engineering-graph harness"),
            source="system",
        )
        source = _symbol_element("graph_source", through, 120, 280, "EQ-101")
        source_out = service._symbol_port_point(source, outlet_id)
        inlet = next(port for port in through.ports if port.id == inlet_id)
        target = _symbol_element(
            "graph_target",
            through,
            720,
            source_out.y - inlet.y,
            "EQ-102",
        )
        duplicate_tag = _symbol_element("graph_duplicate", through, 320, 760, "EQ-101")
        seeded = service.apply_transaction(
            document.id,
            TransactionRequest(
                expected_revision=document.revision,
                source="system",
                label="Seed engineering-graph harness",
                operations=[
                    AddElementOperation(element=source),
                    AddElementOperation(element=target),
                    AddElementOperation(element=duplicate_tag),
                ],
            ),
            source="system",
        ).document
        target_in = service._symbol_port_point(target, inlet_id)
        document = service.apply_transaction(
            document.id,
            TransactionRequest(
                expected_revision=seeded.revision,
                source="system",
                label="Connect the two tagged equipment objects",
                operations=[
                    AddElementOperation(
                        element=ConnectorElement(
                            id="graph_pipe",
                            points=[source_out, target_in],
                            source=ConnectorEndpoint(
                                element_id=source.id, port_id=outlet_id, point=source_out
                            ),
                            target=ConnectorEndpoint(
                                element_id=target.id, port_id=inlet_id, point=target_in
                            ),
                            routing="manual",
                            process_tag="PL-1001",
                            medium="water",
                            nominal_diameter="DN50",
                            flow_direction="forward",
                        )
                    )
                ],
            ),
            source="system",
        ).document

        graph = build_engineering_graph(document, symbols)
        object_ids = [record.object_id for record in graph.objects]
        _require(
            len(object_ids) == len(set(object_ids)),
            "GRAPH_IDENTITY_NOT_UNIQUE",
            "two engineering objects resolved to the same identity",
        )
        tagged = [record for record in graph.objects if record.tag == "EQ-101"]
        _require(
            len(tagged) == 2
            and all(record.identity_scope == "tag" for record in tagged)
            and len({record.object_id for record in tagged}) == 2,
            "GRAPH_TAG_IDENTITY_MISSING",
            "tagged objects must keep tag-scoped identity (with deterministic "
            "disambiguation when the tag is duplicated)",
        )
        duplicate = [
            finding for finding in graph.findings if finding.code == "IR_DUPLICATE_IDENTITY"
        ]
        _require(
            bool(duplicate) and duplicate[0].details["count"] == 2,
            "GRAPH_DUPLICATE_TAG_SILENTLY_MERGED",
            "a duplicate equipment tag must be disambiguated and reported, never merged",
        )
        line = graph.object("line:pl-1001")
        _require(
            line is not None and line.element_ids == ["graph_pipe"],
            "GRAPH_LINE_AGGREGATION_INVALID",
            "the tagged pipeline must own its member connector",
        )

        known = set(object_ids)
        for edge in graph.edges:
            for endpoint in (edge.source_object_id, edge.target_object_id):
                _require(
                    endpoint in known or endpoint.startswith("unbound:"),
                    "GRAPH_EDGE_ENDPOINT_MISSING",
                    f"topology edge {edge.connector_id} references unknown object {endpoint}",
                )
            if edge.pipeline_object_id:
                _require(
                    edge.pipeline_object_id in known,
                    "GRAPH_EDGE_PIPELINE_MISSING",
                    f"topology edge {edge.connector_id} references a missing pipeline object",
                )

        process_objects = {
            record.object_id for record in graph.objects if record.kind in TOPO_KINDS
        }
        grouped = [object_id for group in graph.connectivity_components for object_id in group]
        _require(
            sorted(grouped) == sorted(process_objects),
            "GRAPH_COMPONENT_PARTITION_INVALID",
            "connectivity groups must partition the process objects exactly once",
        )

        source_object = next(
            record for record in graph.objects if record.primary_element_id == "graph_source"
        )
        target_object = next(
            record for record in graph.objects if record.primary_element_id == "graph_target"
        )
        traced = trace_engineering_object(
            graph, source_object.object_id, direction="downstream"
        )
        _require(
            [step.object_id for step in traced.steps]
            == [source_object.object_id, target_object.object_id],
            "GRAPH_TRACE_INVALID",
            f"downstream trace was {[step.object_id for step in traced.steps]}",
        )
        _require(
            traced.traversed_pipeline_ids == ["line:pl-1001"],
            "GRAPH_TRACE_PIPELINE_MISSING",
            "the trace must report the pipeline it traversed",
        )
        _require(
            graph_fingerprint(build_engineering_graph(document, symbols))
            == graph_fingerprint(graph),
            "GRAPH_NOT_DETERMINISTIC",
            "deriving the same document twice produced a different graph",
        )

        project_index = ProjectIndexService(store, symbols)
        report = project_index.rebuild_all()
        _require(
            report.rebuilt == [document.id] and report.stale_after == [],
            "GRAPH_INDEX_REBUILD_INVALID",
            f"unexpected rebuild report: {report.model_dump(mode='json')}",
        )
        entry = project_index.get_entry(document.id)
        _require(
            entry is not None and entry.staleness == "verified_fresh",
            "GRAPH_INDEX_NOT_VERIFIED_FRESH",
            "a freshly rebuilt index row must verify against the live document",
        )
        _require(
            store.get(document.id).document.model_dump(mode="json")
            == document.model_dump(mode="json"),
            "GRAPH_INDEX_MUTATED_DOCUMENT",
            "rebuilding the derived index must never modify the drawing",
        )

        stale = project_index.project_graph()
        _require(
            stale.stale_document_ids == [],
            "GRAPH_INDEX_STALE_AFTER_REBUILD",
            f"index still stale: {stale.stale_document_ids}",
        )

    return QualityHarnessCaseResult(
        name="engineering_graph_contract",
        status="passed",
        summary=(
            "derived graph stayed deterministic and reference-closed; duplicate tags were "
            "disambiguated; the trace was flow-aware; the index verified fresh and left the "
            "drawing untouched"
        ),
        details={
            "through_symbol_key": through.key,
            "object_count": len(object_ids),
            "equipment_count": graph.counts.equipment,
            "line_count": graph.counts.lines,
            "edge_count": graph.counts.edges,
            "error_findings": graph.counts.errors,
            "content_hash": graph.content_hash,
            "graph_hash": graph_fingerprint(graph),
            "index_objects": entry.counts.objects if entry else 0,
        },
    )


def _capture_case(
    name: str,
    runner: Callable[[SymbolRegistry], QualityHarnessCaseResult],
    symbols: SymbolRegistry,
) -> QualityHarnessCaseResult:
    try:
        return runner(symbols)
    except _HarnessFailure as exc:
        return QualityHarnessCaseResult(
            name=name,
            status="failed",
            summary=str(exc),
            findings=[QualityHarnessFinding(code=exc.code, message=str(exc))],
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        return QualityHarnessCaseResult(
            name=name,
            status="failed",
            summary=message,
            findings=[
                QualityHarnessFinding(
                    code="HARNESS_UNEXPECTED_EXCEPTION",
                    message=message,
                )
            ],
        )


def run_quality_harness(symbols: SymbolRegistry | None = None) -> QualityHarnessReport:
    registry = symbols or SymbolRegistry()
    cases = [
        _capture_case("symbol_catalog_integrity", _catalog_case, registry),
        _capture_case("atomic_topology_transaction", _atomic_topology_case, registry),
        _capture_case("semantic_agent_output_contract", _semantic_agent_case, registry),
        _capture_case("drafting_quality_contract", _drafting_quality_case, registry),
        _capture_case("engineering_graph_contract", _engineering_graph_case, registry),
    ]
    passed_cases = sum(case.status == "passed" for case in cases)
    return QualityHarnessReport(
        passed=passed_cases == len(cases),
        total_cases=len(cases),
        passed_cases=passed_cases,
        failed_cases=len(cases) - passed_cases,
        symbol_count=len(registry.list()),
        cases=cases,
    )


def symbol_load_failure_report(exc: SymbolCatalogLoadError) -> QualityHarnessReport:
    details: dict[str, Any] = {"source_path": str(exc.path)}
    if exc.entry_index is not None:
        details["entry_index"] = exc.entry_index
    if exc.symbol_key is not None:
        details["symbol_key"] = exc.symbol_key
    case = QualityHarnessCaseResult(
        name="symbol_catalog_load",
        status="failed",
        summary=str(exc),
        details=details,
        findings=[
            QualityHarnessFinding(
                code=exc.code,
                message=str(exc),
                symbol_key=exc.symbol_key,
            )
        ],
    )
    return QualityHarnessReport(
        passed=False,
        total_cases=1,
        passed_cases=0,
        failed_cases=1,
        symbol_count=0,
        cases=[case],
    )
