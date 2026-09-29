from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

# M10-P1: StrictModel / utc_now are defined in the domain-neutral runtime
# primitives module and re-exported here, so every historical
# ``from agentcad.models import StrictModel, utc_now`` call site keeps working
# with class identity preserved. Validation / JSON-schema / serialization
# behaviour is unchanged (same class object).
from .runtime.primitives import StrictModel, utc_now


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class Point(StrictModel):
    x: float
    y: float


class Style(StrictModel):
    stroke: str = "#111827"
    fill: str = "none"
    stroke_width: float = Field(default=1.5, gt=0, le=100)
    opacity: float = Field(default=1.0, ge=0, le=1)
    dash: list[float] = Field(default_factory=list)


class ElementBase(StrictModel):
    id: str = Field(default_factory=lambda: new_id("el"))
    layer_id: str = "layer_default"
    system_id: str = "system_default"
    style: Style = Field(default_factory=Style)
    name: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class LineElement(ElementBase):
    type: Literal["line"] = "line"
    start: Point
    end: Point


class PolylineElement(ElementBase):
    type: Literal["polyline"] = "polyline"
    points: list[Point]
    closed: bool = False

    @field_validator("points")
    @classmethod
    def validate_points(cls, value: list[Point]) -> list[Point]:
        if len(value) < 2:
            raise ValueError("polyline requires at least two points")
        return value


class RectangleElement(ElementBase):
    type: Literal["rectangle"] = "rectangle"
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    corner_radius: float = Field(default=0, ge=0)


class CircleElement(ElementBase):
    type: Literal["circle"] = "circle"
    center: Point
    radius: float = Field(gt=0)


class TextElement(ElementBase):
    type: Literal["text"] = "text"
    position: Point
    text: str
    font_size: float = Field(default=14, gt=0, le=500)
    anchor: Literal["start", "middle", "end"] = "start"


class SymbolElement(ElementBase):
    type: Literal["symbol"] = "symbol"
    symbol_key: str
    position: Point
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    rotation: float = 0
    label: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)


class JunctionElement(ElementBase):
    type: Literal["junction"] = "junction"
    position: Point
    radius: float = Field(default=4, gt=0, le=50)
    label: str = ""


class ConnectorEndpoint(StrictModel):
    element_id: str | None = None
    port_id: str | None = None
    point: Point

    @model_validator(mode="after")
    def validate_binding(self) -> ConnectorEndpoint:
        if bool(self.element_id) != bool(self.port_id):
            raise ValueError("element_id and port_id must be provided together")
        return self


class ConnectorElement(ElementBase):
    type: Literal["connector"] = "connector"
    points: list[Point]
    source: ConnectorEndpoint | None = None
    target: ConnectorEndpoint | None = None
    routing: Literal["orthogonal", "direct", "manual"] = "orthogonal"
    process_tag: str = ""
    medium: str = ""
    nominal_diameter: str = ""
    flow_direction: Literal["forward", "reverse", "none"] = "none"
    arrow_position: Literal["start", "middle", "end"] = "middle"
    crossing_style: Literal["none", "jump"] = "none"
    jump_radius: float = Field(default=7, gt=1, le=50)

    @field_validator("points")
    @classmethod
    def validate_points(cls, value: list[Point]) -> list[Point]:
        if len(value) < 2:
            raise ValueError("connector requires at least two points")
        return value


Element = Annotated[
    LineElement
    | PolylineElement
    | RectangleElement
    | CircleElement
    | TextElement
    | SymbolElement
    | JunctionElement
    | ConnectorElement,
    Field(discriminator="type"),
]


class Layer(StrictModel):
    id: str = Field(default_factory=lambda: new_id("layer"))
    name: str
    visible: bool = True
    locked: bool = False


class SystemGroup(StrictModel):
    id: str = Field(default_factory=lambda: new_id("system"))
    name: str
    visible: bool = True


class CanvasSettings(StrictModel):
    width: float = Field(default=1600, gt=0)
    height: float = Field(default=900, gt=0)
    grid_size: float = Field(default=20, gt=0)
    background: str = "#ffffff"


class Document(StrictModel):
    id: str = Field(
        default_factory=lambda: new_id("doc"),
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9._~-]+$",
    )
    name: str = "Untitled P&ID"
    revision: int = Field(default=0, ge=0)
    canvas: CanvasSettings = Field(default_factory=CanvasSettings)
    layers: list[Layer] = Field(default_factory=lambda: [Layer(id="layer_default", name="Default")])
    systems: list[SystemGroup] = Field(
        default_factory=lambda: [SystemGroup(id="system_default", name="Default")]
    )
    elements: list[Element] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_references(self) -> Document:
        self._validate_finite_values(self.model_dump(mode="python"))
        layer_ids = [layer.id for layer in self.layers]
        if len(layer_ids) != len(set(layer_ids)):
            raise ValueError("layer ids must be unique")
        system_ids = [system.id for system in self.systems]
        if len(system_ids) != len(set(system_ids)):
            raise ValueError("system ids must be unique")
        element_ids = [element.id for element in self.elements]
        if len(element_ids) != len(set(element_ids)):
            raise ValueError("element ids must be unique")
        missing_layers = sorted({element.layer_id for element in self.elements} - set(layer_ids))
        if missing_layers:
            raise ValueError(f"elements reference missing layers: {missing_layers}")
        missing_systems = sorted({element.system_id for element in self.elements} - set(system_ids))
        if missing_systems:
            raise ValueError(f"elements reference missing systems: {missing_systems}")

        element_map = {element.id: element for element in self.elements}
        for element in self.elements:
            if element.type != "connector":
                continue
            for endpoint_name, endpoint in (("source", element.source), ("target", element.target)):
                if endpoint is None or endpoint.element_id is None:
                    continue
                referenced = element_map.get(endpoint.element_id)
                if referenced is None:
                    raise ValueError(
                        f"connector {element.id} {endpoint_name} references missing element: "
                        f"{endpoint.element_id}"
                    )
                if referenced.type not in {"symbol", "junction"}:
                    raise ValueError(
                        f"connector {element.id} {endpoint_name} must reference a symbol or junction: "
                        f"{endpoint.element_id}"
                    )
                if referenced.type == "junction" and endpoint.port_id != "node":
                    raise ValueError(
                        f"connector {element.id} {endpoint_name} must use junction port 'node'"
                    )
        return self

    @staticmethod
    def _validate_finite_values(value: Any, path: str = "document") -> None:
        if isinstance(value, float) and not isfinite(value):
            raise ValueError(f"{path} must be a finite number")
        if isinstance(value, dict):
            for key, child in value.items():
                Document._validate_finite_values(child, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                Document._validate_finite_values(child, f"{path}[{index}]")


class CreateDocumentRequest(StrictModel):
    name: str = "Untitled P&ID"
    width: float = Field(default=1600, gt=0)
    height: float = Field(default=900, gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AddElementOperation(StrictModel):
    op: Literal["add_element"] = "add_element"
    element: Element


class UpdateElementOperation(StrictModel):
    op: Literal["update_element"] = "update_element"
    element_id: str
    patch: dict[str, Any]


class DeleteElementOperation(StrictModel):
    op: Literal["delete_element"] = "delete_element"
    element_id: str


class AddLayerOperation(StrictModel):
    op: Literal["add_layer"] = "add_layer"
    layer: Layer


class UpdateLayerOperation(StrictModel):
    op: Literal["update_layer"] = "update_layer"
    layer_id: str
    patch: dict[str, Any]


class DeleteLayerOperation(StrictModel):
    op: Literal["delete_layer"] = "delete_layer"
    layer_id: str
    move_elements_to: str = "layer_default"


class AddSystemOperation(StrictModel):
    op: Literal["add_system"] = "add_system"
    system: SystemGroup


class UpdateSystemOperation(StrictModel):
    op: Literal["update_system"] = "update_system"
    system_id: str
    patch: dict[str, Any]


class DeleteSystemOperation(StrictModel):
    op: Literal["delete_system"] = "delete_system"
    system_id: str
    move_elements_to: str = "system_default"


class ClearDocumentOperation(StrictModel):
    op: Literal["clear_document"] = "clear_document"


Operation = Annotated[
    AddElementOperation
    | UpdateElementOperation
    | DeleteElementOperation
    | AddLayerOperation
    | UpdateLayerOperation
    | DeleteLayerOperation
    | AddSystemOperation
    | UpdateSystemOperation
    | DeleteSystemOperation
    | ClearDocumentOperation,
    Field(discriminator="op"),
]


HistorySource = Literal["web", "llm", "mcp", "system"]


class TransactionRequest(StrictModel):
    # Dense reference reconstructions can legitimately exceed 500 low-level
    # operations after connector normalization and annotation polish even when
    # the semantic plan itself stays below its stricter 500-operation limit.
    operations: list[Operation] = Field(min_length=1, max_length=1000)
    expected_revision: int | None = Field(default=None, ge=0)
    label: str = ""
    source: HistorySource | None = None


class TransactionResult(StrictModel):
    document: Document
    applied_operations: int
    label: str = ""


class HistoryEntry(StrictModel):
    id: int | None = None
    document_id: str
    revision: int = Field(ge=0)
    timestamp: datetime = Field(default_factory=utc_now)
    source: HistorySource = "system"
    action: Literal["create", "transaction", "undo", "redo"] = "transaction"
    label: str = ""
    operation_count: int = Field(default=0, ge=0)


class DocumentSummary(StrictModel):
    id: str
    name: str
    revision: int
    element_count: int
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class SymbolPort(StrictModel):
    id: str
    name: str
    x: float
    y: float
    direction: Literal["in", "out", "bidirectional", "none"] = "bidirectional"
    medium: str = "process"


class SymbolDefinition(StrictModel):
    key: str
    name: str
    category: str
    description: str = ""
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    ports: list[SymbolPort] = Field(default_factory=list)
    shapes: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderConfig(StrictModel):
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    thinking_enabled: bool | None = None
    thinking_level: Literal["low", "high", "max"] | None = None


class AgentGenerateRequest(StrictModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    context: str = Field(default="", max_length=200_000)
    dry_run: bool = False
    provider: ProviderConfig | None = None
    expected_revision: int | None = Field(default=None, ge=0)


class AgentPlan(StrictModel):
    explanation: str = ""
    transaction: TransactionRequest


class AgentGenerateResult(StrictModel):
    plan: AgentPlan
    document: Document | None = None
