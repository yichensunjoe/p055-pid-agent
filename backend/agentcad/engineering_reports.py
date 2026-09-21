from __future__ import annotations

from collections import defaultdict
from math import cos, radians, sin
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .exporting import visible_elements
from .models import ConnectorElement, Document, Element, Point, SymbolElement
from .symbols import SymbolRegistry
from .tag_resolver import resolve_symbol_tag

ReportScope = Literal["visible", "all"]
RuleSeverity = Literal["info", "warning", "error"]


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class EquipmentScheduleRow(ReportModel):
    element_id: str
    tag: str
    name: str
    symbol_key: str
    symbol_name: str
    category: str
    layer_id: str
    layer_name: str
    system_id: str
    system_name: str
    required_port_count: int
    connected_port_count: int
    properties: dict[str, Any] = Field(default_factory=dict)


class LineScheduleRow(ReportModel):
    element_id: str
    line_tag: str
    name: str
    medium: str
    nominal_diameter: str
    routing: str
    flow_direction: str
    layer_id: str
    layer_name: str
    system_id: str
    system_name: str
    source: str
    target: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class InstrumentScheduleRow(ReportModel):
    element_id: str
    tag: str
    name: str
    symbol_key: str
    symbol_name: str
    category: str
    layer_id: str
    layer_name: str
    system_id: str
    system_name: str
    required_port_count: int
    connected_port_count: int
    properties: dict[str, Any] = Field(default_factory=dict)


class RuleFinding(ReportModel):
    severity: RuleSeverity
    code: str
    message: str
    element_ids: list[str]
    details: dict[str, Any] = Field(default_factory=dict)


class EngineeringReportCounts(ReportModel):
    equipment: int
    lines: int
    instruments: int
    errors: int
    warnings: int
    info: int


class EngineeringReport(ReportModel):
    schema_name: Literal["pid-agent.engineering-report"] = Field(
        default="pid-agent.engineering-report", alias="schema"
    )
    version: Literal[1] = 1
    document_id: str
    document_name: str
    revision: int
    scope: ReportScope
    counts: EngineeringReportCounts
    equipment: list[EquipmentScheduleRow]
    lines: list[LineScheduleRow]
    instruments: list[InstrumentScheduleRow]
    findings: list[RuleFinding]


def _selected_elements(document: Document, scope: ReportScope) -> list[Element]:
    return visible_elements(document) if scope == "visible" else list(document.elements)


def _is_instrument(category: str) -> bool:
    normalized = category.strip().casefold()
    return "仪表" in normalized or "instrument" in normalized


def _endpoint_label(endpoint: Any) -> str:
    if endpoint is None:
        return ""
    if endpoint.element_id:
        return f"{endpoint.element_id}:{endpoint.port_id}"
    return f"free@{endpoint.point.x:g},{endpoint.point.y:g}"


def _symbol_port_point(symbol: SymbolElement, port_id: str, registry: SymbolRegistry) -> Point | None:
    try:
        definition = registry.get(symbol.symbol_key)
    except KeyError:
        return None
    port = next((item for item in definition.ports if item.id == port_id), None)
    if port is None:
        return None
    local_x = port.x * symbol.width / definition.width
    local_y = port.y * symbol.height / definition.height
    center_x = symbol.width / 2
    center_y = symbol.height / 2
    angle = radians(symbol.rotation)
    dx = local_x - center_x
    dy = local_y - center_y
    rotated_x = center_x + dx * cos(angle) - dy * sin(angle)
    rotated_y = center_y + dx * sin(angle) + dy * cos(angle)
    return Point(x=symbol.position.x + rotated_x, y=symbol.position.y + rotated_y)


def _same_point(first: Point, second: Point, tolerance: float = 1e-6) -> bool:
    return abs(first.x - second.x) <= tolerance and abs(first.y - second.y) <= tolerance


def _port_connections(connectors: list[ConnectorElement]) -> dict[tuple[str, str], set[str]]:
    connected: dict[tuple[str, str], set[str]] = defaultdict(set)
    for connector in connectors:
        for endpoint in (connector.source, connector.target):
            if endpoint and endpoint.element_id and endpoint.port_id:
                connected[(endpoint.element_id, endpoint.port_id)].add(connector.id)
    return connected


def _schedule_symbol(
    symbol: SymbolElement,
    document: Document,
    registry: SymbolRegistry,
    connections: dict[tuple[str, str], set[str]],
) -> EquipmentScheduleRow | InstrumentScheduleRow:
    definition = registry.get(symbol.symbol_key)
    layer_names = {layer.id: layer.name for layer in document.layers}
    system_names = {system.id: system.name for system in document.systems}
    required_ports = [port for port in definition.ports if port.direction != "none"]
    connected_port_count = sum(1 for port in required_ports if connections.get((symbol.id, port.id)))
    payload = {
        "element_id": symbol.id,
        "tag": symbol.label.strip(),
        "name": symbol.name,
        "symbol_key": symbol.symbol_key,
        "symbol_name": definition.name,
        "category": definition.category,
        "layer_id": symbol.layer_id,
        "layer_name": layer_names.get(symbol.layer_id, symbol.layer_id),
        "system_id": symbol.system_id,
        "system_name": system_names.get(symbol.system_id, symbol.system_id),
        "required_port_count": len(required_ports),
        "connected_port_count": connected_port_count,
        "properties": symbol.properties,
    }
    model = InstrumentScheduleRow if _is_instrument(definition.category) else EquipmentScheduleRow
    return model.model_validate(payload)


def _line_row(connector: ConnectorElement, document: Document) -> LineScheduleRow:
    layer_names = {layer.id: layer.name for layer in document.layers}
    system_names = {system.id: system.name for system in document.systems}
    return LineScheduleRow(
        element_id=connector.id,
        line_tag=connector.process_tag.strip(),
        name=connector.name,
        medium=connector.medium.strip(),
        nominal_diameter=connector.nominal_diameter.strip(),
        routing=connector.routing,
        flow_direction=connector.flow_direction,
        layer_id=connector.layer_id,
        layer_name=layer_names.get(connector.layer_id, connector.layer_id),
        system_id=connector.system_id,
        system_name=system_names.get(connector.system_id, connector.system_id),
        source=_endpoint_label(connector.source),
        target=_endpoint_label(connector.target),
        metadata=connector.metadata,
    )


def _finding(
    severity: RuleSeverity,
    code: str,
    message: str,
    element_ids: list[str],
    **details: Any,
) -> RuleFinding:
    return RuleFinding(
        severity=severity,
        code=code,
        message=message,
        element_ids=sorted(set(element_ids)),
        details=details,
    )


def _rule_findings(
    document: Document,
    selected: list[Element],
    registry: SymbolRegistry,
    connections: dict[tuple[str, str], set[str]],
) -> list[RuleFinding]:
    findings: list[RuleFinding] = []
    symbols = [element for element in selected if element.type == "symbol"]
    connectors = [element for element in selected if element.type == "connector"]
    all_elements = {element.id: element for element in document.elements}

    # The tag is read through ``resolve_symbol_tag`` rather than off ``symbol.label``,
    # because the production polish moves a symbol's fixed label into an editable
    # annotation and clears the field. Reading the raw field made every symbol on a
    # freshly drafted drawing look untagged, and made a duplicate tag unrepresentable.
    # Comparison (``casefold``), trimming and finding granularity are unchanged; only
    # the source of the tag text moved.
    tags: dict[str, list[SymbolElement]] = defaultdict(list)
    resolved: dict[str, str] = {}
    for symbol in symbols:
        tag = resolve_symbol_tag(document, symbol)
        resolved[symbol.id] = tag
        if not tag:
            findings.append(_finding("warning", "TAG_MISSING", f"符号 {symbol.id} 缺少位号。", [symbol.id], symbol_key=symbol.symbol_key))
        else:
            tags[tag.casefold()].append(symbol)
    for duplicate in tags.values():
        if len(duplicate) > 1:
            tag = resolved[duplicate[0].id]
            findings.append(_finding("error", "TAG_DUPLICATE", f"位号 {tag} 被 {len(duplicate)} 个符号重复使用。", [symbol.id for symbol in duplicate], tag=tag))

    for symbol in symbols:
        try:
            definition = registry.get(symbol.symbol_key)
        except KeyError:
            findings.append(_finding("error", "SYMBOL_DEFINITION_MISSING", f"符号 {symbol.id} 引用了未知图例 {symbol.symbol_key}。", [symbol.id], symbol_key=symbol.symbol_key))
            continue
        for port in definition.ports:
            if port.direction != "none" and not connections.get((symbol.id, port.id)):
                # Display name only: the trigger (``port.direction`` and the connection
                # map) and the locators are untouched, so the finding fires exactly when
                # it always did and only the symbol it names in prose got more accurate.
                display = resolved.get(symbol.id) or symbol.id
                findings.append(_finding("warning", "SYMBOL_REQUIRED_PORT_UNCONNECTED", f"{display} 的端口 {port.name} 未连接。", [symbol.id], port_id=port.id, port_name=port.name, direction=port.direction))

    for connector in connectors:
        display = connector.process_tag.strip() or connector.id
        if not connector.process_tag.strip():
            findings.append(_finding("warning", "LINE_TAG_MISSING", f"管线 {connector.id} 缺少管线号。", [connector.id]))
        if not connector.medium.strip():
            findings.append(_finding("warning", "LINE_MEDIUM_MISSING", f"管线 {display} 缺少介质。", [connector.id]))
        if not connector.nominal_diameter.strip():
            findings.append(_finding("warning", "LINE_DIAMETER_MISSING", f"管线 {display} 缺少公称直径。", [connector.id]))
        for endpoint_name, endpoint, route_point in (("source", connector.source, connector.points[0]), ("target", connector.target, connector.points[-1])):
            if endpoint is None or endpoint.element_id is None:
                findings.append(_finding("error", "CONNECTOR_ENDPOINT_DANGLING", f"管线 {display} 的 {endpoint_name} 端未绑定设备或连接节点。", [connector.id], endpoint=endpoint_name))
                continue
            referenced = all_elements.get(endpoint.element_id)
            if referenced is None:
                findings.append(_finding("error", "CONNECTOR_ENDPOINT_ELEMENT_MISSING", f"管线 {connector.id} 的 {endpoint_name} 端引用了不存在的元素 {endpoint.element_id}。", [connector.id, endpoint.element_id], endpoint=endpoint_name))
                continue
            expected: Point | None = None
            if referenced.type == "junction":
                if endpoint.port_id != "node":
                    findings.append(_finding("error", "CONNECTOR_ENDPOINT_PORT_MISSING", f"连接节点 {referenced.id} 不存在端口 {endpoint.port_id}。", [connector.id, referenced.id], endpoint=endpoint_name, port_id=endpoint.port_id))
                else:
                    expected = referenced.position
            elif referenced.type == "symbol":
                expected = _symbol_port_point(referenced, endpoint.port_id or "", registry)
                if expected is None:
                    findings.append(_finding("error", "CONNECTOR_ENDPOINT_PORT_MISSING", f"符号 {referenced.id} 不存在端口 {endpoint.port_id}。", [connector.id, referenced.id], endpoint=endpoint_name, port_id=endpoint.port_id))
            else:
                findings.append(_finding("error", "CONNECTOR_ENDPOINT_INVALID_ELEMENT_TYPE", f"管线 {connector.id} 的 {endpoint_name} 端不能绑定 {referenced.type}。", [connector.id, referenced.id], endpoint=endpoint_name))
            if expected is not None and (not _same_point(endpoint.point, expected) or not _same_point(route_point, expected)):
                findings.append(_finding("error", "CONNECTOR_ENDPOINT_POINT_MISMATCH", f"管线 {connector.id} 的 {endpoint_name} 端绑定坐标已失效。", [connector.id, referenced.id], endpoint=endpoint_name, port_id=endpoint.port_id))

    severity_order = {"error": 0, "warning": 1, "info": 2}
    return sorted(findings, key=lambda item: (severity_order[item.severity], item.code, tuple(item.element_ids), item.message))


def build_engineering_report(document: Document, registry: SymbolRegistry, *, scope: ReportScope = "visible") -> EngineeringReport:
    selected = _selected_elements(document, scope)
    connectors = [element for element in selected if element.type == "connector"]
    connections = _port_connections(connectors)
    equipment: list[EquipmentScheduleRow] = []
    instruments: list[InstrumentScheduleRow] = []
    for symbol in (element for element in selected if element.type == "symbol"):
        row = _schedule_symbol(symbol, document, registry, connections)
        (instruments if isinstance(row, InstrumentScheduleRow) else equipment).append(row)
    lines = [_line_row(connector, document) for connector in connectors]
    equipment.sort(key=lambda row: (row.tag.casefold(), row.symbol_name.casefold(), row.element_id))
    instruments.sort(key=lambda row: (row.tag.casefold(), row.symbol_name.casefold(), row.element_id))
    lines.sort(key=lambda row: (row.line_tag.casefold(), row.element_id))
    findings = _rule_findings(document, selected, registry, connections)
    return EngineeringReport(
        document_id=document.id,
        document_name=document.name,
        revision=document.revision,
        scope=scope,
        counts=EngineeringReportCounts(
            equipment=len(equipment), lines=len(lines), instruments=len(instruments),
            errors=sum(item.severity == "error" for item in findings),
            warnings=sum(item.severity == "warning" for item in findings),
            info=sum(item.severity == "info" for item in findings),
        ),
        equipment=equipment, lines=lines, instruments=instruments, findings=findings,
    )
