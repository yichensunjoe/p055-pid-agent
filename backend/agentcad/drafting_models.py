"""Data model for the M3 deterministic drafting engine.

Charter reference: §15 (deterministic layout/routing, manual lock, crossing vs
junction, annotation placement, region relayout), §21.3 (graphical quality),
§43/§48 (M3 completion: layout, routing, collision, annotation, regional repair and
quality gate produce stable, reproducible results).

Two rule sets are kept visibly separate on purpose, so the project never has two
competing truths about the same drawing:

* the *drawing* rules live in ``diagram_quality`` (orthogonality, micro segments,
  port exit/facing, pipe-through-equipment, duplicate labels, score). The drafting
  gate **consumes** that report instead of re-deriving it.
* the *drafting* rules live here and cover what a layout pass can silently break:
  unbridged crossings, junction degree, reserved drawing space, lock integrity,
  scope integrity and topology preservation.

Every ``DRAFT_*`` code below is stable, machine-readable and documented in
``docs/deterministic-drafting-engine.md``. A caller may waive a code through
``DraftingPolicy.waived_codes``; waived findings are still reported, marked as
waived, so a waiver can never hide evidence.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .layout_models import RegionBox
from .models import Point, StrictModel, TransactionRequest

#: Bumped whenever the drafting pipeline changes how it moves geometry or how it decides to
#: accept a stage, because ``transaction_digest`` is computed over this value: a digest
#: recorded under one engine version must never be compared with another. v2 is the release
#: where every stage is compared against the *last accepted* state instead of the run's
#: input, so the same drawing can legitimately produce a different (and no longer
#: accidentally regressing) result while remaining fully reproducible within a version.
DRAFTING_ENGINE_VERSION = 2

#: Drafting-specific finding codes (the drawing rules come from ``diagram_quality``).
DRAFTING_CODES: tuple[str, ...] = (
    "DRAFT_CROSSING_UNBRIDGED",
    #: Same fact as ``DRAFT_CROSSING_UNBRIDGED``, but the engine *could* have bridged it
    #: and did not, because the secondary line is locked or out of scope. Reported as a
    #: warning next to the structural error so the reason is visible without hiding the
    #: fact: one fact, one finding, one explanation.
    "DRAFT_CROSSING_UNBRIDGED_LOCKED",
    "DRAFT_CROSSING_DOUBLE_BRIDGED",
    "DRAFT_CROSSING_ON_JUNCTION",
    "DRAFT_JUNCTION_DANGLING",
    "DRAFT_RESERVED_REGION_OVERLAP",
    "DRAFT_PORT_UNRESOLVED",
    #: The drawing references a symbol the loaded catalog does not define. The element
    #: cannot be laid out or port-resolved, so the report says so instead of quietly
    #: measuring a drawing it can only partly see.
    "DRAFT_SYMBOL_DEFINITION_MISSING",
    #: A pipeline stage could not run at all (for example a pass met an element the
    #: service cannot compute geometry for). What was already committed stands; this
    #: records what was skipped so the result is not mistaken for a complete pass.
    "DRAFT_STAGE_UNAVAILABLE",
    "DRAFT_LOCKED_ELEMENT_MOVED",
    "DRAFT_OUT_OF_SCOPE_CHANGE",
    "DRAFT_TOPOLOGY_CHANGED",
    "DRAFT_STAGE_ROLLED_BACK",
    "DRAFT_RESULT_REGRESSION",
)

#: Drafting codes that block a gate even before the drawing rules are considered.
DRAFTING_BLOCKER_CODES: tuple[str, ...] = (
    "DRAFT_CROSSING_UNBRIDGED",
    "DRAFT_CROSSING_ON_JUNCTION",
    "DRAFT_RESERVED_REGION_OVERLAP",
    "DRAFT_LOCKED_ELEMENT_MOVED",
    "DRAFT_OUT_OF_SCOPE_CHANGE",
    "DRAFT_TOPOLOGY_CHANGED",
    "DRAFT_PIPE_THROUGH_EQUIPMENT",
    "DRAFT_NODE_OVERLAP",
    "DRAFT_TEXT_TEXT_OVERLAP",
    "DRAFT_TEXT_SYMBOL_OVERLAP",
    "DRAFT_TEXT_CONNECTOR_CROSSING",
    "DRAFT_NON_ORTHOGONAL_SEGMENT",
    "DRAFT_MICRO_SEGMENT",
    "DRAFT_OUT_OF_BOUNDS",
    "DRAFT_RESULT_REGRESSION",
)

#: Snapshot fields that must never get worse. This tuple is the definition of
#: "a drafting pass may tidy, but it may not damage the drawing": the engine rolls a
#: stage back when one of these increases, and the gate reports any surviving
#: regression instead of hiding it behind a better total score.
DRAFTING_HARD_FIELDS: tuple[str, ...] = (
    "node_overlaps",
    "pipe_obstacle_intersections",
    "unbridged_crossings",
    "non_orthogonal_segments",
    "micro_segments",
    "unnecessary_bends",
    "text_text_overlaps",
    "text_symbol_overlaps",
    "text_connector_intersections",
    "duplicate_label_count",
    "out_of_bounds_symbols",
    "out_of_bounds_connector_points",
    "reserved_region_intrusions",
    "dangling_junction_count",
    "error_issue_count",
)


class DraftingPolicy(StrictModel):
    """Deterministic drafting thresholds. Every value is an engineering choice."""

    target_score: float = Field(default=95, ge=0, le=100)
    obstacle_margin: float = Field(default=24, ge=0, le=200)
    lane_gap: float = Field(default=24, ge=1, le=200)
    route_clearance: float = Field(default=8, ge=0, le=100)
    max_bends: int = Field(default=3, ge=0, le=20)
    collision_passes: int = Field(default=3, ge=0, le=10)
    annotation_passes: int = Field(default=1, ge=0, le=5)
    #: How many times the whole pass pipeline may repeat before the result is declared
    #: settled. The passes interfere (a collision move invalidates the route that was
    #: optimal before it), so one sweep is not enough to reach a fixed point; the bound
    #: is what keeps the run finite and reproducible.
    pipeline_rounds: int = Field(default=3, ge=1, le=10)
    #: Finding codes the project has explicitly waived. Waived findings stay visible.
    waived_codes: list[str] = Field(default_factory=list, max_length=64)


class DraftingRequest(StrictModel):
    expected_revision: int | None = Field(default=None, ge=0)
    #: Region relayout scope: only geometry that sits inside this box may change.
    region: RegionBox | None = None
    #: Explicit scope alternative (and complement) to ``region``.
    element_ids: list[str] = Field(default_factory=list, max_length=5000)
    #: Transient locks for this run. Persistent locks live in the document itself.
    locked_element_ids: list[str] = Field(default_factory=list, max_length=5000)
    direction: Literal["horizontal", "vertical"] = "horizontal"
    rank_gap: float = Field(default=180, ge=60, le=1000)
    node_gap: float = Field(default=90, ge=20, le=500)
    component_gap: float = Field(default=180, ge=40, le=1000)
    #: ``False`` keeps every symbol where it is and only tidies routing, labels,
    #: crossings and overlaps ("route-only" repair of a region an engineer already
    #: placed).
    relayout: bool = True
    reroute_connectors: bool = True
    place_annotations: bool = True
    bridge_crossings: bool = True
    resolve_collisions: bool = True
    include_hidden: bool = False
    policy: DraftingPolicy = Field(default_factory=DraftingPolicy)


class ResolvedPort(StrictModel):
    """A connection point an engineer (and the router) can actually address.

    Charter §6.4: a connectable object must expose ports with a direction, an
    outward orientation and a medium. This is the read model of that contract.
    """

    element_id: str
    element_kind: Literal["symbol", "junction"]
    port_id: str
    port_name: str = ""
    direction: Literal["in", "out", "bidirectional", "none"] = "bidirectional"
    medium: str = "process"
    side: Literal["left", "right", "top", "bottom", "interior"]
    outward_normal: tuple[int, int] | None = None
    point: Point
    connected_element_ids: list[str] = Field(default_factory=list)
    connector_ids: list[str] = Field(default_factory=list)


class DraftingFinding(StrictModel):
    severity: Literal["info", "warning", "error", "blocker"] = "error"
    code: str
    message: str = ""
    element_ids: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    rule_source: str = "drafting"
    waived: bool = False


class DraftingCrossing(StrictModel):
    """A geometric intersection that is *not* an engineering connection."""

    x: float
    y: float
    first_connector_id: str
    second_connector_id: str
    bridged: bool = False
    bridged_by: str = ""
    at_junction_id: str = ""


class DraftingJunction(StrictModel):
    """An explicit junction element and the topology it really implements."""

    element_id: str
    degree: int = Field(ge=0)
    kind: Literal["branch", "inline", "dangling"]
    connector_ids: list[str] = Field(default_factory=list)
    element_ids: list[str] = Field(default_factory=list)


class DraftingSnapshot(StrictModel):
    """One measured state of the drawing. Before/after snapshots are comparable."""

    score: float = Field(default=100, ge=0, le=100)
    passed: bool = True
    error_issue_count: int = Field(default=0, ge=0)
    warning_issue_count: int = Field(default=0, ge=0)
    symbol_count: int = Field(default=0, ge=0)
    connector_count: int = Field(default=0, ge=0)
    junction_count: int = Field(default=0, ge=0)
    dangling_junction_count: int = Field(default=0, ge=0)
    node_overlaps: int = Field(default=0, ge=0)
    crowded_node_pairs: int = Field(default=0, ge=0)
    pipe_obstacle_intersections: int = Field(default=0, ge=0)
    geometric_crossings: int = Field(default=0, ge=0)
    unbridged_crossings: int = Field(default=0, ge=0)
    total_bends: int = Field(default=0, ge=0)
    non_orthogonal_segments: int = Field(default=0, ge=0)
    micro_segments: int = Field(default=0, ge=0)
    unnecessary_bends: int = Field(default=0, ge=0)
    text_text_overlaps: int = Field(default=0, ge=0)
    text_symbol_overlaps: int = Field(default=0, ge=0)
    text_connector_intersections: int = Field(default=0, ge=0)
    duplicate_label_count: int = Field(default=0, ge=0)
    out_of_bounds_symbols: int = Field(default=0, ge=0)
    out_of_bounds_connector_points: int = Field(default=0, ge=0)
    reserved_region_intrusions: int = Field(default=0, ge=0)
    total_route_length: float = Field(default=0, ge=0)

    def hard_signature(self) -> tuple[tuple[str, float], ...]:
        """The fields a drafting pass is never allowed to make worse."""

        return tuple((name, float(getattr(self, name))) for name in DRAFTING_HARD_FIELDS)


class DraftingMetrics(StrictModel):
    before: DraftingSnapshot = Field(default_factory=DraftingSnapshot)
    after: DraftingSnapshot = Field(default_factory=DraftingSnapshot)
    improvements: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)


class DraftingGate(StrictModel):
    """Deterministic pass/fail for one drawing state.

    ``passed`` requires all three: no drafting blocker, no drawing-rule error, and the
    score at or above the policy target. Waived findings are reported but do not fail
    the gate.
    """

    passed: bool
    score: float = Field(ge=0, le=100)
    target_score: float = Field(ge=0, le=100)
    blockers: list[DraftingFinding] = Field(default_factory=list)
    drawing_issues: list[DraftingFinding] = Field(default_factory=list)
    waived_codes: list[str] = Field(default_factory=list)
    checked_codes: list[str] = Field(default_factory=list)


class DraftingLocks(StrictModel):
    """Where every frozen element came from, so a lock is never a mystery."""

    locked_element_ids: list[str] = Field(default_factory=list)
    request_element_ids: list[str] = Field(default_factory=list)
    metadata_element_ids: list[str] = Field(default_factory=list)
    region_element_ids: list[str] = Field(default_factory=list)
    region_labels: list[str] = Field(default_factory=list)


class DraftingReport(StrictModel):
    """Read-only drafting analysis of one drawing state (never writes a document)."""

    schema_name: Literal["pid-agent.drafting-report"] = Field(
        default="pid-agent.drafting-report",
        alias="schema",
    )
    version: Literal[1] = 1
    engine_version: int = DRAFTING_ENGINE_VERSION
    document_id: str
    document_name: str = ""
    revision: int = Field(ge=0)
    content_hash: str = ""
    input_content_hash: str = ""
    scope_kind: Literal["document", "region", "selection"] = "document"
    scope_element_ids: list[str] = Field(default_factory=list)
    locks: DraftingLocks = Field(default_factory=DraftingLocks)
    ports: list[ResolvedPort] = Field(default_factory=list)
    collisions: list[DraftingFinding] = Field(default_factory=list)
    crossings: list[DraftingCrossing] = Field(default_factory=list)
    junctions: list[DraftingJunction] = Field(default_factory=list)
    findings: list[DraftingFinding] = Field(default_factory=list)
    gate: DraftingGate
    score: float = Field(ge=0, le=100)


class DraftingReproducibility(StrictModel):
    """Everything needed to prove two drafting runs were the same run.

    ``transaction_digest`` is the stable identity of the result: it is computed over
    canonical (id-sorted, style-free) element state plus the canonically ordered
    operations, so the same drawing content produces the same digest no matter in
    which order elements happen to be stored or which pass produced them.
    """

    engine_version: int = DRAFTING_ENGINE_VERSION
    algorithm: str = (
        "canonical-order pipeline: locked-set resolution -> region relayout -> "
        "port-aware reroute -> annotation placement -> crossing bridge -> junction "
        "check -> collision relaxation, each stage accepted only if no hard drafting "
        "metric worsens"
    )
    input_content_hash: str = ""
    output_content_hash: str = ""
    transaction_digest: str = ""
    operation_count: int = Field(default=0, ge=0)
    settled: bool = False


class DraftingPreview(StrictModel):
    """A complete drafting result, previewed. Applying it stays with the caller.

    The engine is preview-only by design: every write goes through the one governed
    transaction channel (Charter §7, P0-2), so a drafting run can never bypass
    permission, approval or audit.
    """

    valid: bool = True
    document_id: str
    document_name: str = ""
    current_revision: int = Field(ge=0)
    transaction: TransactionRequest | None = None
    settled: bool = False
    moved_element_ids: list[str] = Field(default_factory=list)
    rerouted_connector_ids: list[str] = Field(default_factory=list)
    moved_annotation_ids: list[str] = Field(default_factory=list)
    bridged_connector_ids: list[str] = Field(default_factory=list)
    locked_element_ids: list[str] = Field(default_factory=list)
    locks: DraftingLocks = Field(default_factory=DraftingLocks)
    collisions: list[DraftingFinding] = Field(default_factory=list)
    crossings: list[DraftingCrossing] = Field(default_factory=list)
    junctions: list[DraftingJunction] = Field(default_factory=list)
    skipped_locked_element_ids: list[str] = Field(default_factory=list)
    in_scope_element_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metrics: DraftingMetrics = Field(default_factory=DraftingMetrics)
    gate: DraftingGate
    findings: list[DraftingFinding] = Field(default_factory=list)
    reproducibility: DraftingReproducibility = Field(default_factory=DraftingReproducibility)
