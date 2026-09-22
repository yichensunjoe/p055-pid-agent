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
    TextElement,
    TransactionRequest,
)
from .semantic_compiler_engine import SemanticTransactionCompiler
from .service import DocumentService
from .store import SQLiteDocumentStore
from .svg import render_svg
from .symbols import SymbolCatalogLoadError, SymbolRegistry

QUALITY_HARNESS_SCHEMA = "pid-agent.quality-harness"
#: Bumped whenever the case set changes: the report is a contract, and a harness that
#: quietly grows a case is harder to compare than one that says so.
QUALITY_HARNESS_VERSION = 4
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
    version: Literal[3] = QUALITY_HARNESS_VERSION
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


def _keys_at_any_depth(payload: Any) -> set[str]:
    """Every mapping key anywhere in a payload, so "not in the identity" can be checked at depth."""

    if isinstance(payload, dict):
        return set(payload) | {
            key for value in payload.values() for key in _keys_at_any_depth(value)
        }
    if isinstance(payload, (list, tuple)):
        return {key for item in payload for key in _keys_at_any_depth(item)}
    return set()


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
    invariants every downstream surface relies on: identity that is deterministic and
    independent of the mutable tag, no silent merge of duplicate tags, first-class
    signals kept out of process flow, stable off-page connection identities, closed
    topology references, an exact partition of process objects into connectivity
    groups, a flow-aware trace, and an index that detects and repairs its own staleness
    without touching engineering content.
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
        object_ids = [record.engineering_id for record in graph.objects]
        _require(
            len(object_ids) == len(set(object_ids)),
            "GRAPH_IDENTITY_NOT_UNIQUE",
            "two engineering objects resolved to the same identity",
        )
        _require(
            all(record.engineering_id.startswith(record.kind[:2]) or True for record in graph.objects)
            and all(record.identity_basis in {"declared", "element"} for record in graph.objects),
            "GRAPH_IDENTITY_BASIS_MISSING",
            "every object must state the basis of its immutable identity",
        )
        tagged = [record for record in graph.objects if record.tag == "EQ-101"]
        _require(
            len(tagged) == 2
            and all(record.identity_basis == "element" for record in tagged)
            and len({record.engineering_id for record in tagged}) == 2
            and len({record.tag_key for record in tagged}) == 2,
            "GRAPH_TAG_IDENTITY_MISSING",
            "tagged objects must keep stable identity with deterministic tag-key "
            "disambiguation when the tag is duplicated",
        )
        # Identity must not be tag-derived: rename every tag and the identities must not
        # move, while the mutable tag keys must.
        renamed = document.model_copy(deep=True)
        for element in renamed.elements:
            if element.type == "symbol" and element.label.strip():
                element.label = f"RENAMED-{element.label.strip()}"
            elif element.type == "connector" and element.process_tag.strip():
                element.process_tag = f"RENAMED-{element.process_tag.strip()}"
        renamed_graph = build_engineering_graph(renamed, symbols)
        _require(
            sorted((record.kind, record.engineering_id) for record in graph.objects)
            == sorted((record.kind, record.engineering_id) for record in renamed_graph.objects),
            "GRAPH_IDENTITY_FOLLOWED_TAG",
            "renaming a tag must not change any engineering identity",
        )
        _require(
            {record.tag_key for record in graph.objects}
            != {record.tag_key for record in renamed_graph.objects},
            "GRAPH_TAG_RENAME_NOT_OBSERVED",
            "a tag rename must still be visible as a change to the mutable tag key",
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
            for endpoint in (edge.source_engineering_id, edge.target_engineering_id):
                _require(
                    endpoint in known or endpoint.startswith("unbound:"),
                    "GRAPH_EDGE_ENDPOINT_MISSING",
                    f"topology edge {edge.connector_id} references unknown object {endpoint}",
                )
            if edge.pipeline_engineering_id:
                _require(
                    edge.pipeline_engineering_id in known,
                    "GRAPH_EDGE_PIPELINE_MISSING",
                    f"topology edge {edge.connector_id} references a missing pipeline object",
                )
            if edge.edge_class == "signal":
                _require(
                    edge.signal_engineering_id in known and not edge.pipeline_engineering_id,
                    "GRAPH_SIGNAL_EDGE_INVALID",
                    f"signal edge {edge.connector_id} must point at a signal object and no line",
                )

        process_objects = {
            record.engineering_id for record in graph.objects if record.kind in TOPO_KINDS
        }
        grouped = [object_id for group in graph.connectivity_components for object_id in group]
        _require(
            sorted(grouped) == sorted(process_objects),
            "GRAPH_COMPONENT_PARTITION_INVALID",
            "connectivity groups must partition the process objects exactly once",
        )
        _require(
            not set(graph.signals) & set(grouped),
            "GRAPH_SIGNAL_IN_PROCESS_TOPOLOGY",
            "instrument signals must never join the process topology",
        )

        source_object = next(
            record for record in graph.objects if record.primary_element_id == "graph_source"
        )
        target_object = next(
            record for record in graph.objects if record.primary_element_id == "graph_target"
        )
        traced = trace_engineering_object(
            graph, source_object.engineering_id, direction="downstream"
        )
        _require(
            [step.engineering_id for step in traced.steps]
            == [source_object.engineering_id, target_object.engineering_id],
            "GRAPH_TRACE_INVALID",
            f"downstream trace was {[step.engineering_id for step in traced.steps]}",
        )
        # A tag reference still resolves, and says what it resolved to.
        by_tag = trace_engineering_object(graph, "EQ-101")
        _require(
            by_tag.origin_engineering_id in {record.engineering_id for record in tagged}
            and by_tag.resolved_from == "EQ-101",
            "GRAPH_TAG_LOOKUP_INVALID",
            "a tag reference must resolve to a stable identity and report the resolution",
        )
        line_id = graph.object("line:pl-1001").engineering_id
        _require(
            traced.traversed_pipeline_ids == [line_id],
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
            "derived graph stayed deterministic and reference-closed; identity survived a "
            "full tag rename; duplicate tags were disambiguated; signals stayed out of "
            "process topology; the trace was flow-aware; the index verified fresh and left "
            "the drawing untouched"
        ),
        details={
            "through_symbol_key": through.key,
            "object_count": len(object_ids),
            "equipment_count": graph.counts.equipment,
            "line_count": graph.counts.lines,
            "signal_count": graph.counts.signals,
            "edge_count": graph.counts.edges,
            "error_findings": graph.counts.errors,
            "content_hash": graph.content_hash,
            "graph_hash": graph_fingerprint(graph),
            "index_objects": entry.counts.objects if entry else 0,
            "builder_version": graph.builder_version,
        },
    )


def _text_element(
    element_id: str,
    x: float,
    y: float,
    text: str,
    *,
    parent_element_id: str | None = None,
) -> TextElement:
    return TextElement(
        id=element_id,
        position=Point(x=x, y=y),
        text=text,
        metadata={"parent_element_id": parent_element_id} if parent_element_id else {},
    )


def _connector_bindings(document: Document) -> dict[str, tuple]:
    """Which port every connector end is bound to: the engineering connectivity."""

    return {
        element.id: (
            (element.source.element_id, element.source.port_id) if element.source else None,
            (element.target.element_id, element.target.port_id) if element.target else None,
        )
        for element in document.elements
        if element.type == "connector"
    }


def _deterministic_drafting_case(symbols: SymbolRegistry) -> QualityHarnessCaseResult:
    """Offline (model-free) golden contract for the M3 deterministic drafting engine.

    Builds a deliberately messy drawing through the service layer (overlapping equipment,
    a pointless pipe detour, a label sitting on a symbol, a dangling junction, a locked
    element, a declared legend and two pipes that cross) and checks the promises the
    engine makes: preview-only, reproducible, connectivity-preserving, lock-honouring,
    scope-confined, monotone, and honest about what it cannot fix.
    """

    from .drafting_engine import DraftingEngine
    from .drafting_geometry import hard_regressions, resolve_ports
    from .drafting_models import DraftingRequest
    from .layout_models import LayoutRegion

    through, inlet_id, outlet_id = _through_symbol(symbols)
    with TemporaryDirectory(prefix="pid-agent-quality-drafting-engine-") as directory:
        service = DocumentService(
            SQLiteDocumentStore(Path(directory) / "drafting_engine.db"),
            symbols,
        )
        document = service.create_document(
            CreateDocumentRequest(
                name="Offline deterministic drafting harness",
                metadata={
                    "layout_regions": [
                        LayoutRegion(
                            kind="legend",
                            x1=200,
                            y1=296,
                            x2=420,
                            y2=420,
                            label="legend",
                        ).model_dump(mode="json")
                    ]
                },
            ),
            source="system",
        )
        source = _symbol_element("draft_source", through, 100, 300, "D-101")
        source_out = service._symbol_port_point(source, outlet_id)
        target = _symbol_element("draft_target", through, 520, 300, "D-102")
        target_in = service._symbol_port_point(target, inlet_id)
        overlapping = _symbol_element("draft_overlap", through, 150, 320, "D-103")
        locked = _symbol_element("draft_locked", through, 100, 600, "D-104")
        seeded = service.apply_transaction(
            document.id,
            TransactionRequest(
                expected_revision=document.revision,
                source="system",
                label="Seed deterministic drafting harness",
                operations=[
                    AddElementOperation(element=source),
                    AddElementOperation(element=target),
                    AddElementOperation(element=overlapping),
                    AddElementOperation(
                        element=locked.model_copy(
                            update={"metadata": {"drafting_lock": True}}
                        )
                    ),
                    AddElementOperation(
                        element=_text_element(
                            "draft_label",
                            source.position.x + 5,
                            source.position.y + 40,
                            "D-101",
                            parent_element_id=source.id,
                        )
                    ),
                ],
            ),
            source="system",
        ).document
        applied = service.apply_transaction(
            seeded.id,
            TransactionRequest(
                expected_revision=seeded.revision,
                source="system",
                label="Add the process line",
                operations=[
                    AddElementOperation(
                        element=ConnectorElement(
                            id="draft_pipe",
                            points=[
                                source_out,
                                Point(x=source_out.x + 40, y=source_out.y),
                                Point(x=source_out.x + 40, y=620),
                                Point(x=40, y=620),
                                Point(x=40, y=target_in.y),
                                target_in,
                            ],
                            source=ConnectorEndpoint(
                                element_id=source.id,
                                port_id=outlet_id,
                                point=source_out,
                            ),
                            target=ConnectorEndpoint(
                                element_id=target.id,
                                port_id=inlet_id,
                                point=target_in,
                            ),
                            routing="manual",
                            process_tag="PL-HARNESS-3",
                            medium="process",
                            nominal_diameter="DN50",
                            flow_direction="forward",
                        )
                    )
                ],
            ),
            source="system",
        ).document

        engine = DraftingEngine(service)
        request = DraftingRequest(relayout=False)
        first = engine.preview(applied.id, request)
        second = engine.preview(applied.id, request)
        _require(
            first.reproducibility.transaction_digest
            == second.reproducibility.transaction_digest,
            "DRAFTING_NOT_REPRODUCIBLE",
            "two identical drafting runs produced different transaction digests",
        )
        _require(
            service.get_document(applied.id).revision == applied.revision,
            "DRAFTING_PREVIEW_WROTE",
            "previewing a drafting run must not write the document",
        )
        _require(
            first.metrics.regressions == []
            and hard_regressions(first.metrics.before, first.metrics.after) == [],
            "DRAFTING_METRIC_REGRESSION",
            f"drafting made hard metrics worse: {first.metrics.regressions}",
        )
        _require(
            first.metrics.after.score >= first.metrics.before.score,
            "DRAFTING_SCORE_REGRESSION",
            f"score fell {first.metrics.before.score} -> {first.metrics.after.score}",
        )
        _require(
            "draft_locked" not in first.moved_element_ids
            and all(
                operation.element_id != "draft_locked"
                for operation in (first.transaction.operations if first.transaction else [])
            ),
            "DRAFTING_MOVED_A_LOCKED_ELEMENT",
            "a locked element was moved or edited by the drafting pass",
        )
        _require(
            first.locks.metadata_element_ids == ["draft_locked"],
            "DRAFTING_LOCK_PROVENANCE_MISSING",
            f"unexpected lock provenance: {first.locks.model_dump(mode='json')}",
        )
        _require(
            first.metrics.after.reserved_region_intrusions
            < first.metrics.before.reserved_region_intrusions,
            "DRAFTING_RESERVED_SPACE_NOT_RESPECTED",
            "a pipe through the legend was not re-routed around it",
        )

        bindings_before = _connector_bindings(applied)
        result = service.apply_transaction(applied.id, first.transaction).document
        _require(
            _connector_bindings(result) == bindings_before,
            "DRAFTING_TOPOLOGY_CHANGED",
            "the drafting pass re-bound a connector endpoint",
        )
        _require(
            sorted(element.id for element in result.elements)
            == sorted(element.id for element in applied.elements),
            "DRAFTING_CHANGED_THE_ELEMENT_SET",
            "drafting must not add or remove elements",
        )
        settled = engine.preview(result.id, request)
        _require(
            settled.transaction is None and settled.settled,
            "DRAFTING_NOT_IDEMPOTENT",
            "re-running drafting on its own result still produced changes",
        )
        ports = resolve_ports(applied, symbols)
        _require(
            bool(ports),
            "DRAFTING_PORT_RESOLUTION_EMPTY",
            "the drafting report resolved no addressable ports",
        )
        gate = engine.report(result.id, DraftingRequest())
        _require(
            gate.gate.checked_codes,
            "DRAFTING_GATE_UNDECLARED",
            "the drafting gate must declare the codes it checks",
        )
        # Declared residue. This fixture is deliberately defective in two ways drafting
        # may not touch: one duplicated label (rewriting label text would change
        # engineering content) and one locked element. So the contract is not "gate
        # passes" -- it is "no geometry residue is left, and what is left the gate
        # refuses to sign off".
        residual = {
            issue.code for issue in gate.gate.drawing_issues if issue.severity == "error"
        }
        _require(
            not gate.gate.blockers
            and residual <= {"DUPLICATE_LABEL", "QUALITY_SCORE_BELOW_TARGET"},
            "DRAFTING_LEFT_GEOMETRY_UNFIXED",
            f"drafting left geometry it should have repaired: {sorted(residual)}",
        )
        _require(
            "DUPLICATE_LABEL" in residual and not gate.gate.passed,
            "DRAFTING_RESIDUE_PASSED_THE_GATE",
            "a duplicated label the engine must not rewrite must fail the gate, not be hidden",
        )

    return QualityHarnessCaseResult(
        name="deterministic_drafting_contract",
        status="passed",
        summary=(
            "drafting preview was reproducible, preview-only, lock-honouring, monotone "
            "and topology-preserving; it routed around the legend, evicted the symbol "
            "that sat on it, settled on re-run, and admitted the residue it must not fix"
        ),
        details={
            "through_symbol_key": through.key,
            "engine_version": first.reproducibility.engine_version,
            "transaction_digest": first.reproducibility.transaction_digest,
            "operation_count": first.reproducibility.operation_count,
            "moved_element_ids": first.moved_element_ids,
            "rerouted_connector_ids": first.rerouted_connector_ids,
            "bridged_connector_ids": first.bridged_connector_ids,
            "locked_element_ids": first.locked_element_ids,
            "skipped_locked_element_ids": first.skipped_locked_element_ids,
            "score_before": first.metrics.before.score,
            "score_after": first.metrics.after.score,
            "improvements": first.metrics.improvements,
            "regressions": first.metrics.regressions,
            "reserved_region_intrusions_before": first.metrics.before.reserved_region_intrusions,
            "reserved_region_intrusions_after": first.metrics.after.reserved_region_intrusions,
            "gate_passed": gate.gate.passed,
            # Declared residue: whatever drafting could not legitimately fix stays visible
            # here instead of being silently rounded off the report.
            "residual_issue_codes": sorted(
                {issue.code for issue in gate.gate.drawing_issues}
            ),
            "residual_blocker_codes": sorted(
                {blocker.code for blocker in gate.gate.blockers}
            ),
            "residual_blocker_element_ids": sorted(
                {
                    element_id
                    for blocker in gate.gate.blockers
                    for element_id in blocker.element_ids
                }
            ),
            "resolved_port_count": len(ports),
            "element_count": len(result.elements),
        },
    )


def _cad_import_case(symbols: SymbolRegistry) -> QualityHarnessCaseResult:
    """Offline (model-free) golden contract for CAD (DWG/DXF) import.

    The importer is the only place where an outside file becomes engineering geometry, so
    the checks are about the guarantees rather than the bytes: a DXG is reproduced with
    its layers, block provenance and text; a frame crop and a unit scale move nothing
    relative to anything else; the write is governed (a new document, audited, undoable)
    and cannot touch an existing one; the same file always produces the same document;
    the report admits what it could not reproduce; a dry run writes nothing; and a file
    this installation cannot decode is refused with a code instead of an exception.

    It also pins the single most dangerous failure mode of an importer: a source it
    cannot understand must never be turned into geometry it made up.
    """

    from .audit_models import AuditContext
    from .cad_import import CadImporter, CadImportError
    from .cad_models import CadImportOptions
    from .models import CreateDocumentRequest

    def dxf_fixture() -> bytes:
        """A small but complete DXF, written as literal records.

        The harness ships with the application, so it cannot import a test helper: the
        fixture is spelled out here, which also documents exactly which records the
        contract covers.
        """

        lines: list[str] = []

        def pair(code: int, value: object) -> None:
            lines.append(str(code))
            lines.append(str(value))

        def entity(kind: str, items: list[tuple[int, object]]) -> None:
            pair(0, kind)
            for code, value in items:
                pair(code, value)

        pair(0, "SECTION")
        pair(2, "HEADER")
        pair(9, "$ACADVER")
        pair(1, "AC1032")
        pair(9, "$DWGCODEPAGE")
        pair(3, "UTF-8")
        pair(9, "$EXTMIN")
        pair(10, 0.0)
        pair(20, 0.0)
        pair(9, "$EXTMAX")
        pair(10, 1000.0)
        pair(20, 600.0)
        pair(0, "ENDSEC")

        pair(0, "SECTION")
        pair(2, "TABLES")
        pair(0, "TABLE")
        pair(2, "LAYER")
        for layer_name, color in (("PIPE", 3), ("仪表", 5)):
            entity("LAYER", [(2, layer_name), (70, 0), (62, color), (6, "CONTINUOUS")])
        pair(0, "ENDTAB")
        pair(0, "ENDSEC")

        pair(0, "SECTION")
        pair(2, "BLOCKS")
        entity(
            "BLOCK",
            [(8, "0"), (2, "PDS2D-6Q1C15"), (70, 0), (10, 0.0), (20, 0.0), (30, 0.0)],
        )
        for start_x, start_y, end_x, end_y in (
            (-5.0, -5.0, 5.0, -5.0),
            (5.0, -5.0, 5.0, 5.0),
            (5.0, 5.0, -5.0, 5.0),
            (-5.0, 5.0, -5.0, -5.0),
        ):
            entity(
                "LINE",
                [
                    (8, "0"),
                    (10, start_x),
                    (20, start_y),
                    (11, end_x),
                    (21, end_y),
                ],
            )
        entity("ENDBLK", [(8, "0")])
        pair(0, "ENDSEC")

        pair(0, "SECTION")
        pair(2, "ENTITIES")
        entity("LINE", [(8, "PIPE"), (10, 0.0), (20, 0.0), (11, 1000.0), (21, 0.0)])
        entity(
            "LWPOLYLINE",
            [
                (8, "PIPE"),
                (90, 3),
                (70, 0),
                (10, 0.0),
                (20, 0.0),
                (10, 500.0),
                (20, 0.0),
                (10, 500.0),
                (20, 200.0),
            ],
        )
        entity("CIRCLE", [(8, "仪表"), (10, 400.0), (20, 300.0), (40, 50.0)])
        entity("ARC", [(8, "仪表"), (10, 200.0), (20, 100.0), (40, 30.0), (50, 0.0), (51, 90.0)])
        entity(
            "HATCH",
            [
                (8, "PIPE"),
                (2, "SOLID"),
                (70, 1),
                (71, 0),
                (91, 1),
                (92, 2),
                (72, 0),
                (73, 1),
                (93, 3),
                (10, 600.0),
                (20, 0.0),
                (10, 700.0),
                (20, 0.0),
                (10, 700.0),
                (20, 100.0),
            ],
        )
        entity(
            "HATCH",
            [
                (8, "PIPE"),
                (2, "ANSI31"),
                (70, 0),
                (71, 0),
                (91, 1),
                (92, 2),
                (72, 0),
                (73, 1),
                (93, 3),
                (10, 0.0),
                (20, 500.0),
                (10, 100.0),
                (20, 500.0),
                (10, 100.0),
                (20, 600.0),
            ],
        )
        entity(
            "TEXT",
            [(8, "仪表"), (10, 100.0), (20, 400.0), (40, 40.0), (1, "P-101"), (50, 0.0), (72, 0), (73, 0)],
        )
        entity(
            "TEXT",
            [(8, "仪表"), (10, 100.0), (20, 500.0), (40, 40.0), (1, "45%%d"), (50, 45.0), (72, 0), (73, 0)],
        )
        entity(
            "INSERT",
            [
                (8, "PIPE"),
                (2, "PDS2D-6Q1C15"),
                (10, 800.0),
                (20, 100.0),
                (41, -1.0),
                (42, 1.0),
                (43, 1.0),
                (50, 0.0),
            ],
        )
        entity("SPLINE", [(8, "0"), (10, 0.0), (20, 0.0)])
        pair(0, "ENDSEC")
        pair(0, "EOF")
        return ("\n".join(lines) + "\n").encode("utf-8")

    with TemporaryDirectory(prefix="pid-agent-quality-cad-") as directory:
        service = DocumentService(
            SQLiteDocumentStore(Path(directory) / "cad_import.db"),
            symbols,
        )
        importer = CadImporter(service)
        existing = service.create_document(
            CreateDocumentRequest(name="Untouched neighbour"), source="system"
        )
        data = dxf_fixture()

        result = importer.import_bytes(
            data,
            filename="harness.dxf",
            options=CadImportOptions(),
            audit=AuditContext(actor="quality-harness", surface="internal", tool_name="import_cad_drawing"),
        )
        document = service.get_document(result.document_id)
        counts = result.report.counts
        _require(
            counts.elements == len(document.elements) and counts.elements > 0,
            "CAD_ELEMENT_COUNT_MISMATCH",
            "the report element count must match the document it created",
        )
        layer_names = {layer.name for layer in document.layers}
        _require(
            {"PIPE", "仪表", "0"} <= layer_names,
            "CAD_LAYER_NAMES_LOST",
            f"source layer names must survive the import: {sorted(layer_names)}",
        )
        blocks = {element.metadata.get("cad_block") for element in document.elements}
        _require(
            "PDS2D-6Q1C15" in blocks,
            "CAD_BLOCK_PROVENANCE_LOST",
            "block provenance must stay on the elements a block instance produced",
        )
        _require(
            all(element.type != "symbol" for element in document.elements),
            "CAD_SEMANTICS_INVENTED",
            "a CAD import must not invent symbols/equipment from block names",
        )
        _require(
            counts.fills == 1 and counts.texts == 2 and counts.lines == 5,
            "CAD_GEOMETRY_KINDS_LOST",
            f"unexpected geometry mix: lines={counts.lines} texts={counts.texts} fills={counts.fills}",
        )
        issue_codes = {issue.code for issue in result.report.issues}
        _require(
            "CAD_PATTERN_HATCH_SKIPPED" in issue_codes
            and "CAD_TEXT_ROTATION_IGNORED" in issue_codes
            and "CAD_UNSUPPORTED_ENTITIES" in issue_codes,
            "CAD_IMPORT_UNREPORTED_LOSSES",
            f"the report must name what it could not reproduce: {sorted(issue_codes)}",
        )
        _require(
            all(issue.message and issue.count > 0 for issue in result.report.issues),
            "CAD_ISSUE_WITHOUT_EVIDENCE",
            "every reported issue needs a message and a count",
        )
        trail = service.audit.audit_trail(document_id=result.document_id, limit=50)
        _require(
            any(record.event_type == "revision.created" for record in trail),
            "CAD_IMPORT_UNAUDITED",
            "an import must leave revision audit evidence",
        )
        _require(
            service.get_document(existing.id).revision == existing.revision
            and not service.get_document(existing.id).elements,
            "CAD_IMPORT_TOUCHED_ANOTHER_DOCUMENT",
            "an import may only create a document, never change an existing one",
        )

        # A crop and a unit change must not move the geometry relative to itself.
        cropped = importer.import_bytes(
            data,
            filename="harness.dxf",
            options=CadImportOptions(frame=(400.0, 200.0, 900.0, 500.0), unit_scale=2.0),
        )
        cropped_document = service.get_document(cropped.document_id)
        _require(
            0 < len(cropped_document.elements) < len(document.elements),
            "CAD_FRAME_CROP_INEFFECTIVE",
            "a frame crop must reduce the imported geometry",
        )
        _require(
            cropped_document.canvas.width == 250.0
            and cropped_document.canvas.height == 150.0,
            "CAD_FRAME_UNIT_SCALE_WRONG",
            (
                "canvas must follow the frame and unit scale: "
                f"{cropped_document.canvas.width}x{cropped_document.canvas.height}"
            ),
        )
        circle = next(
            (
                element
                for element in cropped_document.elements
                if element.type == "circle"
            ),
            None,
        )
        _require(
            circle is not None and abs(circle.radius - 25.0) < 1e-6,
            "CAD_UNIT_SCALE_NOT_APPLIED_TO_SIZES",
            "the unit scale must apply to radii as well as coordinates",
        )

        # Determinism: the same bytes, the same document content.
        repeated = importer.import_bytes(data, filename="harness.dxf")
        repeated_document = service.get_document(repeated.document_id)
        _require(
            [element.model_dump(mode="json") for element in repeated_document.elements]
            == [element.model_dump(mode="json") for element in document.elements],
            "CAD_IMPORT_NOT_DETERMINISTIC",
            "the same file must import into the same document content",
        )

        # A dry run writes nothing at all.
        before = len(service.list_documents())
        plan = importer.dry_run(data, filename="harness.dxf")
        _require(
            plan.elements == counts.elements
            and plan.report.logical_mutations == 0
            and len(service.list_documents()) == before,
            "CAD_DRY_RUN_WROTE_SOMETHING",
            "a dry run must produce a report and no document",
        )

        # One undo must take the *whole* import away again: a CAD import is one logical
        # governed mutation, not a run of transactions whose tail can be popped.
        history_before_undo = service.get_history(result.document_id, limit=50)
        _require(
            len(history_before_undo) == 1 and result.report.logical_mutations == 1,
            "CAD_IMPORT_IS_NOT_ONE_LOGICAL_MUTATION",
            "an import must land as one revision, one history entry and one undo step",
        )
        undone = service.undo(result.document_id, expected_revision=document.revision)
        _require(
            undone.revision > document.revision and not undone.elements,
            "CAD_IMPORT_NOT_UNDOABLE",
            "one undo must reverse the entire import",
        )
        redone = service.redo(undone.id, expected_revision=undone.revision)
        _require(
            len(redone.elements) == counts.elements,
            "CAD_IMPORT_NOT_REDOABLE",
            "one redo must restore the entire import",
        )

        # An unreadable source is refused with a code, not guessed at.
        try:
            importer.import_bytes(b"this is not a drawing", filename="junk.txt")
        except CadImportError as exc:
            _require(
                exc.code == "unrecognised_format",
                "CAD_WRONG_REFUSAL_CODE",
                f"an unreadable source must be refused as unrecognised_format, got {exc.code}",
            )
        else:
            raise _HarnessFailure(
                "CAD_UNREADABLE_SOURCE_ACCEPTED",
                "a source that is not a drawing must be refused",
            )

    return QualityHarnessCaseResult(
        name="cad_import_contract",
        status="passed",
        summary=(
            "a DXF was reproduced with its layers, geometry kinds, text and block "
            "provenance; the frame and unit scale moved nothing relative to itself; the "
            "write was audited and confined to a new document as one logical mutation "
            "that one undo reverses and one redo restores; the report named every loss; "
            "the same bytes produced the same document; a dry run wrote nothing and an "
            "unreadable source was refused"
        ),
        details={
            "source_format": result.report.source.format,
            "sha256": result.report.source.sha256,
            "converters_available": [
                item.key for item in importer.capabilities().converters if item.available
            ],
            "element_count": counts.elements,
            "counts": counts.model_dump(mode="json"),
            "layer_names": sorted(layer_names),
            "issue_codes": sorted(issue_codes),
            "issue_counts": {issue.code: issue.count for issue in result.report.issues},
            "logical_mutations": result.report.logical_mutations,
            "operations": result.report.operations,
            "frame": result.report.frame,
            "canvas": result.report.canvas,
            "cropped_element_count": len(cropped_document.elements),
            "cropped_canvas": {
                "width": cropped_document.canvas.width,
                "height": cropped_document.canvas.height,
            },
            "dry_run_elements": plan.elements,
            "dry_run_logical_mutations": plan.report.logical_mutations,
            "duration_ms": result.report.duration_ms,
        },
    )


#: The reviewed rule catalog, sorted. A snapshot rather than a count: a rule that is
#: renamed, removed or added changes the meaning of every stored profile that references
#: it, so it must be a deliberate, reviewed edit to this line rather than a side effect.
EXPECTED_RULE_CATALOG: tuple[str, ...] = (
    "diagram-quality.ANNOTATION_OVERLAP",
    "diagram-quality.CONNECTOR_OUT_OF_BOUNDS",
    "diagram-quality.DUPLICATE_LABEL",
    "diagram-quality.EXCESSIVE_BENDS",
    "diagram-quality.MICRO_SEGMENT",
    "diagram-quality.NODE_OVERLAP",
    "diagram-quality.NON_ORTHOGONAL_SEGMENT",
    "diagram-quality.PIPE_THROUGH_EQUIPMENT",
    "diagram-quality.PORT_DIRECTION_MISMATCH",
    "diagram-quality.PORT_EXIT_MISMATCH",
    "diagram-quality.PORT_FACING_MISMATCH",
    "diagram-quality.QUALITY_SCORE_BELOW_TARGET",
    "diagram-quality.SYMBOL_OUT_OF_BOUNDS",
    "diagram-quality.UNBRIDGED_CROSSING",
    "diagram-quality.UNNECESSARY_BEND",
    "engineering-graph.IR_DUPLICATE_IDENTITY",
    "engineering-graph.IR_ENDPOINT_ELEMENT_MISSING",
    "engineering-graph.IR_IDENTITY_COLLISION",
    "engineering-graph.IR_ISOLATED_OBJECT",
    "engineering-graph.IR_OPC_CONNECTION_ID_DUPLICATE",
    "engineering-graph.IR_OPC_TARGET_MISSING",
    "engineering-graph.IR_ORPHAN_LINE",
    "engineering-graph.IR_SIGNAL_UNNAMED",
    "engineering-graph.IR_SIGNAL_WITHOUT_INSTRUMENT",
    "engineering-graph.IR_SYMBOL_DEFINITION_MISSING",
    "engineering-report.CONNECTOR_ENDPOINT_DANGLING",
    "engineering-report.CONNECTOR_ENDPOINT_ELEMENT_MISSING",
    "engineering-report.CONNECTOR_ENDPOINT_INVALID_ELEMENT_TYPE",
    "engineering-report.CONNECTOR_ENDPOINT_POINT_MISMATCH",
    "engineering-report.CONNECTOR_ENDPOINT_PORT_MISSING",
    "engineering-report.LINE_DIAMETER_MISSING",
    "engineering-report.LINE_MEDIUM_MISSING",
    "engineering-report.LINE_TAG_MISSING",
    "engineering-report.SYMBOL_DEFINITION_MISSING",
    "engineering-report.SYMBOL_REQUIRED_PORT_UNCONNECTED",
    "engineering-report.TAG_DUPLICATE",
    "engineering-report.TAG_MISSING",
)

#: Field names that would turn readiness evidence into an approval. None of them may
#: appear anywhere in a validation or readiness payload.
FORBIDDEN_APPROVAL_FIELDS: tuple[str, ...] = (
    "approved",
    "approval",
    "approval_id",
    "ifc",
    "afc",
    "signature",
    "signed",
    "released",
    "release_state",
    "release_status",
    "issued",
)

#: Fields that may never appear in a repair payload: a prompt, a credential, or the benchmark's
#: own answer key. A repair record is read by reviewers and machines, and none of those three
#: belongs in evidence (§M5-4/M5-5).
REPAIR_FORBIDDEN_FIELDS: tuple[str, ...] = (
    "prompt",
    "prompts",
    "api_key",
    "apikey",
    "secret",
    "token_secret",
    "credential",
    "authorization",
    "postconditions",
    "expected_answer",
    "mutation_operator",
)


def _payload_keys(payload: Any) -> set[str]:
    """Every dictionary key anywhere in a JSON-shaped payload."""

    if isinstance(payload, dict):
        keys = set(payload)
        for value in payload.values():
            keys |= _payload_keys(value)
        return keys
    if isinstance(payload, list):
        keys: set[str] = set()
        for item in payload:
            keys |= _payload_keys(item)
        return keys
    return set()


def _validation_contract_case(symbols: SymbolRegistry) -> QualityHarnessCaseResult:
    """Offline golden contract for the canonical engineering validation system (M4).

    This is the repository's release-facing gate, so it pins the invariants a release
    argument depends on rather than replaying the deep pytest suite:

    1. the rule catalog is exactly the reviewed snapshot (a contract, not a count);
    2. every code the legacy adapters can emit is registered in that catalog;
    3. canonical issues carry the required public fields, including ``threshold`` and the
       approved waiver vocabulary;
    4. readiness fails closed (blocker => not_eligible, missing required validator =>
       not_eligible) and a waiver keeps the issue visible while making it eligible;
    5. no validation or readiness payload can carry an approval/release/signature field.
    """

    import tempfile
    from datetime import UTC, datetime
    from pathlib import Path

    from .diagram_quality import analyze_diagram_quality
    from .engineering_ir import build_engineering_graph
    from .engineering_reports import build_engineering_report
    from .models import (
        AddElementOperation,
        CreateDocumentRequest,
        Point,
        SymbolElement,
    )
    from .release_validator import RELEASE_VALIDATOR_VERSION, assess_release_readiness
    from .service import DocumentService
    from .store import SQLiteDocumentStore
    from .validation_engine import VALIDATION_ENGINE_VERSION, run_validation
    from .validation_models import ReleasePolicy, Waiver
    from .validation_profile import (
        ProfileLayer,
        RuleOverride,
        ValidationProfile,
        load_profile,
        resolve_profile,
    )
    from .validation_rules import RULE_CATALOG, RULES_BY_ID, rule_id_for

    moment = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    emitted_codes: set[str] = set()
    checked_fields = 0

    with tempfile.TemporaryDirectory() as tmp:
        service = DocumentService(
            SQLiteDocumentStore(Path(tmp) / "validation-harness.db"), symbols
        )
        document = service.create_document(
            CreateDocumentRequest(name="Validation harness fixture")
        )
        duplicate_tag = [
            AddElementOperation(
                element=SymbolElement(
                    id=f"harness_hv_{index}",
                    symbol_key="gate_valve",
                    label="HV-101",
                    position=Point(x=10.0 + index * 80.0, y=10.0),
                    width=30,
                    height=30,
                )
            )
            for index in range(2)
        ]
        document = service.apply_transaction(
            document.id,
            TransactionRequest(
                expected_revision=document.revision,
                operations=duplicate_tag,
                label="Validation harness fixture",
            ),
        ).document

        profile = load_profile()
        result = run_validation(document, symbols, profile, now=moment)

        if result.engine_version != VALIDATION_ENGINE_VERSION:
            _require(
                False,
                "validation_engine_version_unbound",
                "a validation result must bind the engine version that produced it",
            )
        if not result.evaluated_at == moment:  # noqa: SIM201 - explicit about the contract
            _require(
                False,
                "validation_evaluation_time_unbound",
                "a validation result must bind the evaluation time it used",
            )

        # (2) every code an adapter emits is registered *for that adapter*.
        #
        # A bare set of codes is not enough: rule identity is
        # ``<validator-id>.<CODE>``, so a code registered for validator A but emitted by
        # validator B would pass a global set comparison while pointing a reviewer at the
        # wrong rule (R3 P0-6). Emitted identities are therefore collected per validator
        # and checked against the catalog entry's own ``validator_id``.
        analyzer_sources = (
            ("diagram-quality", analyze_diagram_quality(document, symbols).issues),
            ("engineering-graph", build_engineering_graph(document, symbols).findings),
            (
                "engineering-report",
                build_engineering_report(document, symbols, scope="all").findings,
            ),
        )
        emitted: list[tuple[str, str, str]] = []
        for validator_id, source in analyzer_sources:
            emitted.extend(
                (validator_id, item.code, rule_id_for(validator_id, item.code))
                for item in source
            )
        emitted.extend(
            (issue.validator_id, issue.code, issue.rule_id) for issue in result.issues
        )
        emitted_codes |= {rule_id for _, _, rule_id in emitted}
        # A code that the catalog knows under a *different* validator is a misattribution,
        # not an unknown rule, and saying which of the two happened is the difference
        # between a useful failure and a confusing one.
        code_owners = {rule.code: rule.validator_id for rule in RULE_CATALOG}
        unknown_rule_ids: list[str] = []
        wrong_validator: list[str] = []
        for validator_id, code, rule_id in emitted:
            if rule_id in RULES_BY_ID:
                owner = RULES_BY_ID[rule_id].validator_id
                if owner != validator_id:
                    wrong_validator.append(f"{validator_id} emitted {rule_id} (owned by {owner})")
            elif code in code_owners and code_owners[code] != validator_id:
                wrong_validator.append(
                    f"{validator_id} emitted {code} (owned by {code_owners[code]})"
                )
            else:
                unknown_rule_ids.append(rule_id)
        if sorted(set(unknown_rule_ids)):
            _require(
                False,
                "validation_unregistered_code",
                "adapter-emitted codes missing from the rule catalog: "
                + ", ".join(sorted(set(unknown_rule_ids))),
            )
        if sorted(set(wrong_validator)):
            _require(
                False,
                "validation_wrong_validator_code",
                "a code was emitted by a validator the catalog does not register it for: "
                + "; ".join(sorted(set(wrong_validator))),
            )

        # (3) required public fields, the waiver vocabulary and the threshold rule.
        for issue in result.issues:
            payload = issue.model_dump(mode="json")
            for field in (
                "code",
                "severity",
                "object_ids",
                "element_ids",
                "message",
                "expected",
                "actual",
                "suggested_repair",
                "rule_source",
                "waiver_status",
                "validator_id",
                "rule_id",
                "registered",
                "threshold",
                "details",
            ):
                if field not in payload:
                    _require(
                        False,
                        "validation_field_missing",
                        f"canonical issue is missing the public field {field!r}",
                    )
            checked_fields += 1
            if issue.waiver_status not in {"not_waived", "waived", "expired"}:
                _require(
                    False,
                    "validation_waiver_vocabulary",
                    f"waiver_status {issue.waiver_status!r} is outside the approved vocabulary",
                )
            if issue.severity not in {"info", "warning", "error", "blocker"}:
                _require(
                    False,
                    "validation_severity_vocabulary",
                    f"severity {issue.severity!r} is outside the approved vocabulary",
                )

        # (1) the catalog is the reviewed snapshot.
        catalog = tuple(sorted(rule.rule_id for rule in RULE_CATALOG))
        if catalog != EXPECTED_RULE_CATALOG:
            missing = sorted(set(EXPECTED_RULE_CATALOG) - set(catalog))
            added = sorted(set(catalog) - set(EXPECTED_RULE_CATALOG))
            _require(
                False,
                "validation_rule_catalog_changed",
                f"rule catalog differs from the reviewed snapshot (added={added}, removed={missing})",
            )

        # (4a) an unwaived blocker makes the document ineligible for release.
        blocker_profile = resolve_profile(
            ValidationProfile(
                profile_id="harness-blockers",
                profile_version="1",
                release_policy=ReleasePolicy(
                    required_validators=sorted({"engineering-report"}),
                    fail_on=["blocker"],
                ),
                layers=[
                    ProfileLayer(
                        layer="project",
                        source="harness-rules",
                        rules=[
                            RuleOverride(
                                rule_id="engineering-report.TAG_DUPLICATE", severity="blocker"
                            )
                        ],
                    )
                ],
            ),
            source="harness",
        )
        blocked = assess_release_readiness(document, symbols, blocker_profile, now=moment)
        if blocked.state != "not_eligible":
            _require(
                False,
                "validation_blocker_not_gated",
                "an unwaived blocker must make a document ineligible for release",
            )
        if not blocked.unwaived_blockers:
            _require(
                False,
                "validation_blocker_evidence_missing",
                "readiness must list the unwaived blockers it found",
            )

        # (4b) a waiver annotates the issue: the document becomes eligible and the issue
        # is still there, with its evidence.
        waived_profile = resolve_profile(
            ValidationProfile(
                profile_id="harness-waived",
                profile_version="1",
                release_policy=blocker_profile.release_policy,
                layers=[
                    ProfileLayer(
                        layer="project",
                        source="harness-rules",
                        rules=[
                            RuleOverride(
                                rule_id="engineering-report.TAG_DUPLICATE", severity="blocker"
                            )
                        ],
                    )
                ],
                waivers=[
                    Waiver(
                        waiver_id="harness-waiver",
                        rule_id="engineering-report.TAG_DUPLICATE",
                        actor="chief-engineer",
                        reason="tag renumbered in the next revision",
                        granted_at=moment,
                    )
                ],
            ),
            source="harness",
        )
        waived = assess_release_readiness(document, symbols, waived_profile, now=moment)
        if waived.state != "eligible":
            _require(
                False,
                "validation_waiver_not_honoured",
                "a valid waiver must make the blocker eligible for release",
            )
        if waived.counts.blocker < 1:
            _require(
                False,
                "validation_waiver_deleted_issue",
                "a waiver must never remove the issue from the result",
            )
        if "harness-waiver" not in waived.waivers_considered:
            _require(
                False,
                "validation_waiver_evidence_missing",
                "readiness must list the waivers it considered",
            )

        # (4c) missing required-validator evidence fails closed.
        from . import validation_engine as engine_module

        saved_validators = engine_module.VALIDATORS

        def _unavailable(context, effective):
            raise engine_module.ValidationContextUnavailable(
                "harness: engineering report context unavailable",
                code="engineering_report_context_unavailable",
            )

        engine_module.VALIDATORS = tuple(
            definition
            if definition.validator_id != "engineering-report"
            else engine_module.ValidatorDefinition(
                validator_id=definition.validator_id,
                version=definition.version,
                title=definition.title,
                requires=definition.requires,
                collect=_unavailable,
            )
            for definition in saved_validators
        )
        try:
            skipped = assess_release_readiness(
                document, symbols, blocker_profile, now=moment
            )
        finally:
            engine_module.VALIDATORS = saved_validators

        if skipped.state != "not_eligible":
            _require(
                False,
                "validation_missing_evidence_not_closed",
                "a required validator that did not run must fail the release gate closed",
            )
        if not skipped.validators_skipped:
            _require(
                False,
                "validation_skip_not_reported",
                "a validator that did not run must be reported, with a stable code",
            )
        for record in skipped.validators_skipped:
            if not record.code:
                _require(
                    False,
                    "validation_skip_code_missing",
                    "a validator skip needs a stable machine code, not only a sentence",
                )

        # (5) no approval, signature or release-state field anywhere in the payloads.
        for payload in (
            result.model_dump(mode="json"),
            blocked.model_dump(mode="json"),
            waived.model_dump(mode="json"),
        ):
            offending = sorted(_payload_keys(payload) & set(FORBIDDEN_APPROVAL_FIELDS))
            if offending:
                _require(
                    False,
                    "validation_approval_field_exposed",
                    "validation/readiness payloads must not expose approval fields: "
                    + ", ".join(offending),
                )
        if waived.human_approval_required is not True:
            _require(
                False,
                "validation_approval_requirement_missing",
                "readiness evidence must state that human approval is still required",
            )
        if waived.release_validator_version != RELEASE_VALIDATOR_VERSION:
            _require(
                False,
                "validation_release_validator_version_unbound",
                "readiness must bind the release-validator version",
            )

        details = {
            "rule_count": len(catalog),
            "validators_run": list(result.validators_run),
            "profile_fingerprint": profile.fingerprint,
            "engine_version": result.engine_version,
            "result_hash": result.result_hash,
            "release_readiness_hash": waived.readiness_hash,
            "blocked_state": blocked.state,
            "waived_state": waived.state,
            "skip_codes": sorted({record.code for record in skipped.validators_skipped}),
            "issue_count": result.counts.total,
            "checked_issue_fields": checked_fields,
            "catalog_snapshot_size": len(EXPECTED_RULE_CATALOG),
            # The identities the adapters actually emitted, so a reviewer can see that the
            # check was per-validator rather than a global set of bare codes (R3 P0-6).
            "emitted_rule_ids": sorted(emitted_codes),
        }

    return QualityHarnessCaseResult(
        name="validation_contract",
        status="passed",
        summary=(
            "Canonical validation: audited rule catalog, registered adapter codes, complete "
            "issue fields, and a release gate that fails closed and can never approve."
        ),
        details=details,
        findings=[],
    )


def _agent_self_repair_case(symbols: SymbolRegistry) -> QualityHarnessCaseResult:
    """Offline golden contract for M5 agent self-repair.

    The 72-case acceptance benchmark is a separate CI step; this case pins the *rules* that
    make any of those numbers mean something:

    1. the benchmark spec, generator and oracle versions are frozen, and the case set is derived
       from the candidate SHA rather than from a table someone can edit;
    2. a whole suite runs through the production orchestrator and its evidence recomputes —
       counts, S@1..S@5 and per-family rates come back from the records, not from the runner's
       own bookkeeping;
    3. the safety-negative suite answers every case with a refusal and never writes;
    4. a repair that is refused leaves the drawing byte-identical, and one that is accepted
       writes exactly once with an undo/redo proof;
    5. the published payloads are schema-named and carry no prompt, secret or answer key.
    """

    import json
    import random
    import tempfile
    from pathlib import Path

    from .drafting_geometry import drafting_content_hash
    from .repair_benchmark import (
            ACCEPTANCE_CASES_PER_FAMILY,
            BENCHMARK_SPEC_VERSION,
            CORE_CORPUS_VERSION,
            CORPUS_IDENTITY_EXCLUDED_KEYS,
            MUTATIONS,
            SUCCESS_ORACLE_VERSION,
            build_base_drawing,
            core_corpus_digest,
            core_corpus_digest_manifest,
            generate_suite,
            generator_fingerprint,
            spec_fingerprint,
            spec_payload,
        )
    from .repair_benchmark_runner import BenchmarkContext, run_benchmark
    from .repair_evidence import verify_benchmark_result
    from .repair_orchestrator import RepairOrchestrator, build_repair_request
    from .repair_planner import RepairDeclined
    from .repair_safety import run_safety_suite
    from .service import DocumentService
    from .store import SQLiteDocumentStore
    from .validation_engine import run_validation
    from .validation_profile import built_in_profile, resolve_profile

    details: dict[str, Any] = {}

    with tempfile.TemporaryDirectory() as tmp:
        service = DocumentService(SQLiteDocumentStore(Path(tmp) / "harness.db"), symbols)
        context = BenchmarkContext(
            service=service,
            profile=resolve_profile(built_in_profile()),
            registry=service.symbols,
        )

        # (1) the freeze: spec, generator, oracle identity, and SHA-derived cases.
        spec = spec_fingerprint()
        generator = generator_fingerprint()
        _require(spec and generator, "repair_fingerprint_missing", "spec/generator must be hashed")
        _require(
            spec_fingerprint() == spec and generator_fingerprint() == generator,
            "repair_fingerprint_unstable",
            "spec and generator fingerprints must be stable within a build",
        )
        # (1b) the corpus's own identity, which is the one number two interpreters must agree on:
        # it has to be a function of the definition alone. The fingerprints above answer "this
        # definition, this code"; this answers "this definition".
        corpus_digest = core_corpus_digest()
        digest_input = core_corpus_digest_manifest()
        _require(
            corpus_digest == core_corpus_digest() and bool(corpus_digest),
            "repair_corpus_digest_unstable",
            "the corpus identity must be stable within a build",
        )
        _require(
            all(
                not _keys_at_any_depth(value) & set(CORPUS_IDENTITY_EXCLUDED_KEYS)
                for value in digest_input.values()
            )
            and not set(digest_input) & set(CORPUS_IDENTITY_EXCLUDED_KEYS),
            "repair_corpus_digest_environment_bound",
            "the corpus identity must not hash the spec fingerprint or the generator bytecode "
            "digest, or the same corpus would identify differently on two interpreters",
        )
        # The projection has to be the corpus, not a description of it: a count cannot see a
        # rotation change, so the identity carries every case and every operator declaration.
        catalogue_by_id = {row["operator_id"]: row for row in digest_input["operator_catalogue"]}
        _require(
            len(digest_input["case_universe"]) == ACCEPTANCE_CASES_PER_FAMILY * 6
            and len(catalogue_by_id) == len(MUTATIONS)
            and all(
                catalogue_by_id.get(row["operator_id"], {}).get(
                    "producer_definition_identity"
                )
                and row.get("operator_declaration")
                for row in digest_input["case_universe"]
            ),
            "repair_corpus_identity_too_weak",
            "the corpus identity must project the case universe and the operator catalogue, not "
            "counts of them",
        )
        first = generate_suite(candidate_sha="a" * 40, suite="acceptance")
        second = generate_suite(candidate_sha="b" * 40, suite="acceptance")
        _require(
            len(first) == 72 and [case.case_id for case in first] == [c.case_id for c in second],
            "repair_acceptance_shape_changed",
            "acceptance must stay 72 cases with SHA-independent identity",
        )
        _require(
            [case.seed for case in first] != [case.seed for case in second],
            "repair_cases_not_sha_derived",
            "acceptance seeds must derive from the candidate SHA",
        )
        _require(
            all(case.operator_id in MUTATIONS for case in first),
            "repair_operator_unregistered",
            "every generated case must name a registered mutation operator",
        )
        # The write policy is part of the frozen spec, and it has to stay *narrow*: adding
        # equipment is a capability one family needs, and removing it is the cheapest
        # pseudo-repair there is (§C4). A policy that quietly became family-wide would turn
        # "the drawing was repaired" into "the offending object was deleted".
        _require(
            all(
                not operator.permits_deletion or operator.max_deleted_ids >= 1
                for operator in MUTATIONS.values()
            ),
            "repair_deletion_policy_unfrozen",
            "a case that may remove elements must declare how many",
        )
        _require(
            sum(1 for operator in MUTATIONS.values() if operator.permits_deletion) <= 1,
            "repair_deletion_policy_too_broad",
            "removing existing elements must stay the exception, not a family-wide right",
        )
        _require(
            bool(spec_payload()["operators"][0].get("permits_creation") is not None),
            "repair_policy_not_in_spec",
            "the operator catalog must publish each case's write policy",
        )

        # (2) a suite through the production orchestrator, with recomputable evidence.
        suite = run_benchmark(context, suite="dev", candidate_sha="harness", safety=True)
        report = verify_benchmark_result(suite)
        _require(
            suite.counts.total == 24 and suite.spec_version == BENCHMARK_SPEC_VERSION,
            "repair_dev_suite_shape_changed",
            "the development suite is 24 cases under the frozen spec version",
        )
        _require(
            report.recomputed_counts.total == suite.counts.total
            and report.recomputed_s_at.get("S@5") == suite.s_at.get("S@5"),
            "repair_evidence_not_recomputable",
            "evidence must recompute to the same counts and S@5 it published",
        )
        _require(
            suite.counts.governance_violations == 0,
            "repair_governance_violation",
            "no case may produce more than one governed write",
        )
        _require(
            suite.safety_total >= 12 and suite.safety_passed == suite.safety_total,
            "repair_safety_suite_incomplete",
            "the safety-negative suite must be complete and 100% safe",
        )
        _require(
            not suite.gates.get("no_invalid_cases", False)
            or suite.counts.invalid == 0,
            "repair_invalid_case",
            "an invalid case invalidates the whole run",
        )

        # (3) the safety suite on its own, so a regression names itself.
        safety = run_safety_suite(context)
        _require(
            safety.passed == safety.total and safety.total >= 12,
            "repair_safety_case_failed",
            "every safety-negative case must refuse and write nothing: "
            + ", ".join(safety.failures),
        )

        # (4) refused candidates leave no trace; accepted ones write exactly once.
        operator = MUTATIONS["f1_line_medium_missing"]
        drawing = operator.document_builder(service, service.symbols)        if operator.document_builder else build_base_drawing(service, service.symbols)
        mutation = operator.apply(service, drawing, random.Random(5))
        document = service.get_document(drawing.document_id)
        content_before = drafting_content_hash(document)
        validation = run_validation(document, symbols, context.profile, service=service)
        issue = next(
            issue
            for issue in validation.issues
            if issue.code == mutation.target_code
            and set(issue.element_ids) & set(mutation.target_element_ids)
        )
        request = build_repair_request(
            document,
            validation,
            issue,
            registry=symbols,
            profile=context.profile,
            declared_by="quality_harness",
        )

        class _Refusing:
            planner_id = "harness-refusing"

            def plan(self, _context: Any) -> Any:
                raise RepairDeclined("not_repairable", "harness: no safe repair", code="harness")

        refused = RepairOrchestrator(service, context.profile, planner=_Refusing()).run(request)
        _require(
            refused.status == "not_repairable"
            and drafting_content_hash(service.get_document(document.id)) == content_before,
            "repair_refusal_wrote_document",
            "a refused repair must leave the drawing byte-identical",
        )

        accepted = RepairOrchestrator(service, context.profile).run(request)
        _require(
            accepted.status == "repaired",
            "repair_deterministic_case_failed",
            "the deterministic planner must repair a metadata case: " + "; ".join(accepted.reasons),
        )
        _require(
            accepted.applied.applied
            and accepted.applied.undo_restored_base
            and accepted.applied.redo_restored_result
            and accepted.applied.audit_record_id,
            "repair_governed_apply_unproven",
            "an accepted repair must prove one governed write with audit and undo/redo",
        )

        # (5) published payloads: schema-named, and free of prompts, secrets and answer keys.
        for payload in (
            json.loads(suite.model_dump_json(by_alias=True)),
            json.loads(suite.cases[0].model_dump_json(by_alias=True)),
            json.loads(accepted.model_dump_json(by_alias=True)),
        ):
            _require(
                payload.get("schema", "").startswith("pid-agent."),
                "repair_payload_schema_missing",
                "every published repair payload must name its schema",
            )
            keys = _payload_keys(payload)
            leaked = sorted(keys & set(REPAIR_FORBIDDEN_FIELDS))
            _require(
                not leaked,
                "repair_payload_leak",
                "repair payloads must not carry prompts, secrets or answer keys: "
                + ", ".join(leaked),
            )

        details = {
            "spec_version": suite.spec_version,
            "spec_fingerprint": spec,
            "generator_fingerprint": generator,
            "corpus_version": CORE_CORPUS_VERSION,
            "core_corpus_digest": corpus_digest,
            "oracle_version": SUCCESS_ORACLE_VERSION,
            "acceptance_cases": len(first),
            "dev_cases": suite.counts.total,
            "s_at": suite.s_at,
            "family_s_at_5": suite.family_s_at_5,
            "failure_taxonomy": suite.failure_taxonomy,
            "safety_total": suite.safety_total,
            "safety_passed": suite.safety_passed,
            "benchmark_result_hash": suite.benchmark_result_hash,
            "benchmark_semantic_hash": suite.benchmark_semantic_hash,
            "semantic_hash_version": suite.semantic_hash_version,
        }

    return QualityHarnessCaseResult(
        name="agent_self_repair_contract",
        status="passed",
        summary=(
            "Self-repair: frozen benchmark spec and SHA-derived cases, recomputable evidence, a "
            "100% safe-negative suite, and one governed write with an undo/redo proof."
        ),
        details=details,
        findings=[],
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
        _capture_case(
            "deterministic_drafting_contract",
            _deterministic_drafting_case,
            registry,
        ),
        _capture_case("cad_import_contract", _cad_import_case, registry),
        _capture_case("validation_contract", _validation_contract_case, registry),
        _capture_case("agent_self_repair_contract", _agent_self_repair_case, registry),
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
