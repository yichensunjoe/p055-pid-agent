from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .diagram_quality_models import DiagramQualityReport
from .m7_synthesis_models import Completeness, RejectedOperationReceipt
from .models import (
    AddElementOperation,
    AddLayerOperation,
    AddSystemOperation,
    AgentPlan,
    CircleElement,
    ClearDocumentOperation,
    DeleteLayerOperation,
    DeleteSystemOperation,
    JunctionElement,
    LineElement,
    Point,
    PolylineElement,
    ProviderConfig,
    RectangleElement,
    StrictModel,
    SymbolElement,
    TextElement,
    TransactionRequest,
    UpdateElementOperation,
    UpdateLayerOperation,
    UpdateSystemOperation,
)


def _semantic_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class SafeDeleteElementOperation(StrictModel):
    op: Literal["delete_element"] = "delete_element"
    element_id: str
    connection_policy: Literal["reject_if_connected", "detach", "delete_connectors"] = (
        "reject_if_connected"
    )


class ReplaceSymbolOperation(StrictModel):
    op: Literal["replace_symbol"] = "replace_symbol"
    element_id: str
    symbol_key: str
    port_mapping: dict[str, str] = Field(default_factory=dict)
    preserve_size: bool = True
    label: str | None = None
    properties_patch: dict[str, Any] = Field(default_factory=dict)


class ReconnectConnectorOperation(StrictModel):
    op: Literal["reconnect_connector"] = "reconnect_connector"
    connector_id: str
    endpoint: Literal["source", "target"]
    element_id: str | None = None
    port_id: str | None = None
    point: Point | None = None
    routing: Literal["orthogonal", "direct", "manual"] | None = None

    @model_validator(mode="after")
    def validate_connection(self) -> ReconnectConnectorOperation:
        bound = self.element_id is not None or self.port_id is not None
        if bound and (self.element_id is None or self.port_id is None):
            raise ValueError("element_id and port_id must be provided together")
        if not bound and self.point is None:
            raise ValueError("a free connector endpoint requires point")
        return self


class ConnectPortsOperation(StrictModel):
    """Connect two real ports with an engineering connector.

    Express pipe flow with flow_direction and arrow_position. Do not add standalone
    text elements such as '→' or '←' to represent connector flow.
    """

    op: Literal["connect_ports"] = "connect_ports"
    connector_id: str = Field(default_factory=lambda: _semantic_id("pipe"))
    source_element_id: str
    source_port_id: str
    target_element_id: str
    target_port_id: str
    routing: Literal["orthogonal", "direct"] = "orthogonal"
    waypoints: list[Point] = Field(default_factory=list, max_length=50)
    process_tag: str = ""
    medium: str = ""
    nominal_diameter: str = ""
    flow_direction: Literal["forward", "reverse", "none"] = Field(
        default="none",
        description=(
            "Connector flow relative to source→target. Use 'forward' when flow follows "
            "source→target, 'reverse' when it flows target→source, and 'none' only when "
            "no direction should be shown. Prefer this over standalone arrow text."
        ),
    )
    arrow_position: Literal["start", "middle", "end"] = Field(
        default="middle",
        description="Position of the rendered engineering flow arrow on the connector.",
    )
    crossing_style: Literal["none", "jump"] = Field(
        default="none",
        description="Use 'jump' when this connector should render bridge arcs at crossings.",
    )
    jump_radius: float = Field(
        default=7,
        gt=1,
        le=50,
        description="Bridge-arc radius when crossing_style is 'jump'.",
    )
    layer_id: str | None = None
    system_id: str | None = None
    style: dict[str, Any] | None = None
    name: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_waypoints(self) -> ConnectPortsOperation:
        if self.waypoints and self.routing == "direct":
            raise ValueError("waypoints require orthogonal routing")
        return self


class InstrumentTapOperation(StrictModel):
    op: Literal["instrument_tap"] = "instrument_tap"
    main_connector_id: str
    junction_point: Point
    direction: Literal["up", "down"] = "up"
    branch_length: float = Field(default=150, ge=80, le=500)
    measurement: Literal["pressure", "temperature", "flow"]
    instrument_label: str
    instrument_symbol_key: str | None = None
    instrument_port_id: str = "process"
    root_valve_symbol_key: str = "ball_valve"
    root_valve_in_port_id: str = "in"
    root_valve_out_port_id: str = "out"
    root_valve_label: str = ""
    junction_id: str = Field(default_factory=lambda: _semantic_id("junction"))
    downstream_connector_id: str = Field(default_factory=lambda: _semantic_id("pipe"))
    root_valve_id: str = Field(default_factory=lambda: _semantic_id("root_valve"))
    instrument_id: str = Field(default_factory=lambda: _semantic_id("instrument"))
    junction_to_valve_connector_id: str = Field(default_factory=lambda: _semantic_id("pipe"))
    valve_to_instrument_connector_id: str = Field(default_factory=lambda: _semantic_id("pipe"))
    layer_id: str | None = None
    system_id: str | None = None
    style: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_generated_ids(self) -> InstrumentTapOperation:
        generated_ids = [
            self.junction_id,
            self.downstream_connector_id,
            self.root_valve_id,
            self.instrument_id,
            self.junction_to_valve_connector_id,
            self.valve_to_instrument_connector_id,
        ]
        if len(generated_ids) != len(set(generated_ids)):
            raise ValueError("instrument_tap generated element ids must be unique")
        if self.main_connector_id in generated_ids:
            raise ValueError("instrument_tap generated ids cannot reuse main_connector_id")
        return self


DiagramElement = Annotated[
    LineElement
    | PolylineElement
    | RectangleElement
    | CircleElement
    | TextElement
    | SymbolElement
    | JunctionElement,
    Field(discriminator="type"),
]


class AddDiagramElementOperation(StrictModel):
    op: Literal["add_element"] = "add_element"
    element: DiagramElement


SemanticOperation = Annotated[
    AddElementOperation
    | UpdateElementOperation
    | SafeDeleteElementOperation
    | ReplaceSymbolOperation
    | ReconnectConnectorOperation
    | ConnectPortsOperation
    | InstrumentTapOperation
    | AddLayerOperation
    | UpdateLayerOperation
    | DeleteLayerOperation
    | AddSystemOperation
    | UpdateSystemOperation
    | DeleteSystemOperation
    | ClearDocumentOperation,
    Field(discriminator="op"),
]


FullDiagramOperation = Annotated[
    AddDiagramElementOperation
    | ConnectPortsOperation
    | InstrumentTapOperation
    | AddLayerOperation
    | AddSystemOperation,
    Field(discriminator="op"),
]


def _validate_instrument_tap_labels(operations: Sequence[Any]) -> None:
    tap_labels = {
        operation.instrument_label.strip().casefold()
        for operation in operations
        if isinstance(operation, InstrumentTapOperation) and operation.instrument_label.strip()
    }
    if not tap_labels:
        return

    duplicate_labels = sorted(
        {
            operation.element.label.strip()
            for operation in operations
            if isinstance(operation, (AddElementOperation, AddDiagramElementOperation))
            and operation.element.type == "symbol"
            and operation.element.label.strip().casefold() in tap_labels
        }
    )
    if duplicate_labels:
        raise ValueError(
            "instrument_tap already creates its instrument symbol; remove standalone "
            f"add_element symbol operations for labels: {', '.join(duplicate_labels)}"
        )


class SemanticTransaction(StrictModel):
    operations: list[SemanticOperation] = Field(min_length=1, max_length=500)
    expected_revision: int | None = Field(default=None, ge=0)
    label: str = ""

    @model_validator(mode="after")
    def validate_instrument_tap_labels(self) -> SemanticTransaction:
        _validate_instrument_tap_labels(self.operations)
        return self


class FullDiagramTransaction(StrictModel):
    operations: list[FullDiagramOperation] = Field(min_length=1, max_length=500)
    expected_revision: int | None = Field(default=None, ge=0)
    label: str = ""

    @model_validator(mode="after")
    def validate_instrument_tap_labels(self) -> FullDiagramTransaction:
        _validate_instrument_tap_labels(self.operations)
        return self


class SemanticAgentPlan(StrictModel):
    plan_id: str = Field(default_factory=lambda: uuid4().hex)
    explanation: str = ""
    transaction: SemanticTransaction


class AgentOperationIssue(StrictModel):
    operation_index: int | None = Field(default=None, ge=0)
    operation: str = ""
    code: str
    message: str
    field_path: str = ""
    element_id: str | None = None
    connector_id: str | None = None
    available_values: dict[str, list[str]] = Field(default_factory=dict)
    suggestions: list[str] = Field(default_factory=list)


class AgentTransactionAssessment(StrictModel):
    """How a compiled plan is judged, on two axes rather than one.

    ``valid`` answers "is each retained operation legal". ``completeness`` answers "is the
    submitted plan whole". They were one field, which is why a plan that lost 39% of its own
    operations was reported as passing validation and recorded as a completed session.

    ``semantic_operation_count`` keeps its meaning (semantic operations submitted) and
    ``compiled_operation_count`` keeps its meaning (low-level operations produced, which is
    larger because one semantic operation expands into several elements). Neither of those
    is the count of retained semantic operations, so ``accepted_operation_count`` carries it
    explicitly and the invariant the gate wrote as "proposed = compiled + rejected" holds
    here as ``semantic == accepted + rejected``.
    """

    valid: bool
    stage: Literal["compile", "validate"]
    document_id: str
    current_revision: int = Field(ge=0)
    next_revision: int = Field(ge=0)
    semantic_operation_count: int = Field(default=0, ge=0)
    compiled_operation_count: int = Field(default=0, ge=0)
    resulting_element_count: int | None = Field(default=None, ge=0)
    affected_element_ids: list[str] = Field(default_factory=list)
    added_element_ids: list[str] = Field(default_factory=list)
    updated_element_ids: list[str] = Field(default_factory=list)
    deleted_element_ids: list[str] = Field(default_factory=list)
    issues: list[AgentOperationIssue] = Field(default_factory=list)

    # Phase 2A: the accountability axis. ``semantic_operation_count`` is the proposed count;
    # these carry what survived, what did not, and why.
    accepted_operation_count: int = Field(default=0, ge=0)
    rejected_operation_count: int = Field(default=0, ge=0)
    completeness: Completeness = "complete"
    rejected_operations: list[RejectedOperationReceipt] = Field(default_factory=list)

    @property
    def proposed_operation_count(self) -> int:
        return self.semantic_operation_count

    @property
    def is_complete(self) -> bool:
        return self.completeness == "complete"

    def may_proceed_to_authorisation(self) -> bool:
        """The only combination that may be offered to a human."""

        return self.valid and self.completeness == "complete"


class AnnotationQuality(StrictModel):
    duplicate_label_count: int = Field(default=0, ge=0)
    text_text_overlaps: int = Field(default=0, ge=0)
    text_symbol_overlaps: int = Field(default=0, ge=0)
    text_connector_intersections: int = Field(default=0, ge=0)


class AnnotationLayoutMetrics(StrictModel):
    before: AnnotationQuality = Field(default_factory=AnnotationQuality)
    after: AnnotationQuality = Field(default_factory=AnnotationQuality)
    generated_text_ids: list[str] = Field(default_factory=list)
    moved_text_ids: list[str] = Field(default_factory=list)
    deleted_text_ids: list[str] = Field(default_factory=list)
    leader_line_ids: list[str] = Field(default_factory=list)


class SemanticAgentPlanResult(StrictModel):
    session_id: str
    plan: SemanticAgentPlan
    compiled_plan: AgentPlan | None = None
    assessment: AgentTransactionAssessment
    attempt: int = Field(default=0, ge=0, le=5)
    parent_plan_id: str | None = None
    annotation_metrics: AnnotationLayoutMetrics | None = None
    diagram_quality: DiagramQualityReport | None = None


class SemanticAgentReplanRequest(StrictModel):
    session_id: str | None = None
    prompt: str = Field(min_length=1, max_length=100_000)
    context: str = Field(default="", max_length=200_000)
    provider: ProviderConfig | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    failed_plan: SemanticAgentPlan
    attempt: int = Field(default=1, ge=1, le=5)


class SemanticAgentApplyRequest(StrictModel):
    session_id: str
    approval_id: str
    plan_id: str
    parent_plan_id: str | None = None
    attempt: int = Field(default=0, ge=0, le=5)
    transaction: TransactionRequest


class CompiledSemanticTransaction(StrictModel):
    transaction: TransactionRequest | None = None
    assessment: AgentTransactionAssessment
    annotation_metrics: AnnotationLayoutMetrics | None = None
    diagram_quality: DiagramQualityReport | None = None
