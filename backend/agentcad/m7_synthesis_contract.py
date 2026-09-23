"""M7 phase 1: the contract for natural-language P&ID synthesis, as declared data.

M7 exists because of a measured failure, and the failure was not "the model drew badly".
An agent asked for a plant-wide gas-system P&ID produced **197 proposed operations, 120 of
which compiled**; the remaining 77 were dropped one by one, and every surface a human or an
agent could look at then reported success: the assessment was ``valid``, the automatic
runner's loop condition was ``not valid``, and the confirmation dialog said the plan had
passed validation. A session whose output is missing 39% of its own plan was recorded as
``completed``. Alongside that, the model was also doing the work of a CAD layout engine —
by byte count, 80% of the payload it had to emit was coordinates and styling, and the
deterministic layout engine that already exists was wired to ``preserve_positions=True``,
i.e. explicitly told not to place anything.

So M7's success criterion is not "draw more". It is two statements that can be contradicted
by a test:

1. **A synthesis that is incomplete says so, and says which parts are missing** — validity
   and completeness are separate axes, a partial result can never be reported as success,
   and the raw proposal survives as durable evidence so the rejected part is auditable.
2. **The model owns meaning and code owns geometry** — the model emits a semantic diagram
   specification (equipment, connections, required loops, layout *intent*) and never
   absolute coordinates, so that a plant-wide drawing is bounded by how much *meaning* fits
   in one completion rather than by how many coordinate pairs fit.

Design choices worth stating, because they are the reason this is a module and not a
paragraph:

* **Validity and completeness are declared as two independent axes.** The tempting shortcut
  is to make a partially-compiled plan ``invalid``; that is wrong in the other direction,
  because "the 120 accepted operations are each legal" and "the plan is whole" are
  different questions with different recoveries. The matrix in §3 is exhaustive over the
  cross product, so a future fourth case cannot be invented by silence.
* **The receipt is the product, not a log line.** Each rejected operation carries its own
  reason code, field path, available values and suggestions, because the compiler already
  computes all of it and then throws it away. Returning it is what turns a dead end into a
  replan.
* **The catalogue audit separates "hidden" from "missing".** Those are different defects
  with different fixes: a suppressed-but-renderable transmitter is a visibility bug, while a
  device the catalogue has no symbol for is a gap that must be *reported*, never silently
  omitted, substituted with a lookalike, or invented.
* **Layout intent is declared, raw canvas arithmetic is not.** "Make it a wide drawing" is
  engineering intent and belongs to the model; "make it 58273.8 by 4772.2" is layout
  arithmetic and belongs to code. The forbidden-class list in §10 names coordinate-shaped
  fields so that re-admitting them is a visible act rather than a quiet one.
* **Phase 1 is design and contract only.** As in M6, the forbidden list is part of the
  contract, and a test enumerates the live surface and asserts none of it exists yet.

Nothing here writes anything, and nothing here is imported by the application. It is a
review document with a build-breaking check attached, in the same spirit as
``surface_contract.py``, ``m4_invariance.py`` and ``m6_ingestion_contract.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

M7_CONTRACT_VERSION = "m7-synthesis-contract/1"

# --------------------------------------------------------------------------------------
# §1 The two axes. Validity is about what was accepted; completeness is about what was
#    proposed. Collapsing them is what let a 39%-dropped plan report success.
# --------------------------------------------------------------------------------------

Validity = Literal["valid", "invalid"]
Completeness = Literal["complete", "partial", "empty"]

VALIDITY_VALUES: tuple[Validity, ...] = ("valid", "invalid")
COMPLETENESS_VALUES: tuple[Completeness, ...] = ("complete", "partial", "empty")

#: Completeness is a statement about ``proposed`` vs ``compiled``, so it is derivable and
#: must not be supplied by the caller that also supplied the operations.
COMPLETENESS_IS_DERIVED_FROM_COUNTS = True
#: A session may only be recorded as completed when *both* axes are satisfied.
COMPLETENESS_IS_INDEPENDENT_OF_VALIDITY = True
SESSION_SUCCESS_REQUIRES: tuple[Validity, Completeness] = ("valid", "complete")

SYNTHESIS_ASSESSMENT_AXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("validity", VALIDITY_VALUES),
    ("completeness", COMPLETENESS_VALUES),
)

#: Counts the assessment must carry. ``proposed_operation_count`` is the one that was
#: missing: without it, "120 of what" is unanswerable and the loss is invisible.
SYNTHESIS_COUNT_FIELDS: tuple[str, ...] = (
    "proposed_operation_count",
    "compiled_operation_count",
    "rejected_operation_count",
)

# --------------------------------------------------------------------------------------
# §2 Proposal evidence: the plan that was *submitted* is durable, not just the part that
#    was applied. Persisting only the applied transaction is what made the 77 rejected
#    operations unrecoverable from the local database.
# --------------------------------------------------------------------------------------

REJECTED_OPERATION_RECEIPT_FIELDS: tuple[str, ...] = (
    "original_index",
    "operation_id",
    "operation_kind",
    "reason_code",
    "message",
    "field_path",
    "available_values",
    "suggestions",
)

PROPOSAL_EVIDENCE_REQUIRED_FIELDS: tuple[str, ...] = (
    "plan_id",
    "session_id",
    "document_id",
    "base_revision",
    "proposed_operations",
    "rejected_operations",
    *SYNTHESIS_COUNT_FIELDS,
)

#: The submitted proposal is evidence in its own right; the applied transaction cannot
#: stand in for it, because the difference between them *is* the finding.
RAW_PROPOSAL_IS_DURABLE_EVIDENCE = True
APPLIED_ONLY_PERSISTENCE_IS_FORBIDDEN = True
#: Evidence must be readable without replaying the session against a live provider.
EVIDENCE_READABLE_WITHOUT_PROVIDER = True

# --------------------------------------------------------------------------------------
# §3 What the automatic agent does with each outcome. Exhaustive over §1's cross product.
# --------------------------------------------------------------------------------------

ContinuationAction = Literal[
    "proceed_to_human_confirmation",
    "return_receipt_and_replan",
    "replan_or_abort",
    "existing_error_recovery",
]

#: The actions as a value, so "every action names a branch" is checkable rather than asserted.
CONTINUATION_ACTIONS: tuple[ContinuationAction, ...] = (
    "proceed_to_human_confirmation",
    "return_receipt_and_replan",
    "replan_or_abort",
    "existing_error_recovery",
)


@dataclass(frozen=True)
class ContinuationRule:
    validity: Validity
    completeness: Completeness
    action: ContinuationAction
    note: str


CONTINUATION_MATRIX: tuple[ContinuationRule, ...] = (
    ContinuationRule(
        validity="valid",
        completeness="complete",
        action="proceed_to_human_confirmation",
        note="Nothing was dropped; the plan may be offered for human authorisation.",
    ),
    ContinuationRule(
        validity="valid",
        completeness="partial",
        action="return_receipt_and_replan",
        note=(
            "The accepted operations are legal but the plan is not whole. This is the case "
            "the failure exhibited: it must neither be reported as success nor be treated "
            "as a hard error — the receipt is returned and the agent repairs."
        ),
    ),
    ContinuationRule(
        validity="valid",
        completeness="empty",
        action="replan_or_abort",
        note="A plan that compiles to nothing is a planning failure, not a quiet no-op.",
    ),
    ContinuationRule(
        validity="invalid",
        completeness="partial",
        action="existing_error_recovery",
        note="Hard invalidity keeps its precedence and its existing recovery path.",
    ),
    ContinuationRule(
        validity="invalid",
        completeness="empty",
        action="existing_error_recovery",
        note="Hard invalidity keeps its precedence and its existing recovery path.",
    ),
    ContinuationRule(
        validity="invalid",
        completeness="complete",
        action="existing_error_recovery",
        note=(
            "Unreachable by construction — a fully compiled plan is valid — and declared "
            "here so that the matrix is total and an implementer cannot add the case by "
            "accident."
        ),
    ),
)

#: The single most important prohibition in this milestone.
PARTIAL_MAY_BE_REPORTED_AS_SUCCESS = False
PARTIAL_MAY_BE_RECORDED_AS_COMPLETED_SESSION = False
#: A caller must not be able to suppress the receipt in order to obtain a "clean" result.
RECEIPT_IS_OMITTABLE = False

#: What a human must be able to see before authorising anything.
UI_MUST_DISPLAY_COMPLETENESS_FIELDS: tuple[str, ...] = (
    "proposed_operation_count",
    "compiled_operation_count",
    "rejected_operation_count",
    "completeness",
)

# --------------------------------------------------------------------------------------
# §4 Catalogue audit: "hidden from the model" and "absent from the catalogue" are
#    different defects. The audit is a chain so that a claim about visibility is checked
#    at every step rather than asserted at one.
# --------------------------------------------------------------------------------------

CATALOGUE_AUDIT_STEPS: tuple[str, ...] = (
    "declared_requirement",
    "catalogue_existence",
    "visible_to_model",
    "compiler_accepts",
    "renderer_supports",
)

#: ``declared_requirement`` is the requirement in the user's own request, so the audit is
#: anchored to intent rather than to whatever the catalogue happens to contain.
CATALOGUE_AUDIT_IS_MACHINE_VERIFIABLE = True
CATALOGUE_AUDIT_ANCHOR_IS_DECLARED_REQUIREMENT = True
HIDDEN_IS_NOT_MISSING = True

#: Handling that the audit forbids for a requirement the catalogue cannot express.
CATALOGUE_GAP_FORBIDDEN_HANDLING: tuple[str, ...] = (
    "silent_omission",
    "substitute_a_lookalike_symbol",
    "invent_an_undefined_symbol_key",
)

# --------------------------------------------------------------------------------------
# §5 catalog_gap: the explicit, machine-visible record of an unrepresentable requirement.
# --------------------------------------------------------------------------------------

CATALOG_GAP_REQUIRED_FIELDS: tuple[str, ...] = (
    "requested_type",
    "requested_tag",
    "source_requirement",
    "available_alternatives",
)

CATALOG_GAP_IS_REPORTED_NOT_HIDDEN = True
#: A gap makes the synthesis partial rather than invalid: nothing is wrong with the plan,
#: the catalogue simply cannot express part of it.
CATALOG_GAP_FORCES_COMPLETENESS: Completeness = "partial"
#: Phase 1 requires gaps to be *visible*. Auto-degrading a specialised device into a
#: generic one is a semantic lie and is explicitly deferred, not silently permitted.
AUTO_GENERIC_SUBSTITUTION_ALLOWED_IN_PHASE_1 = False
GENERIC_SUBSTITUTION_REQUIRES_EXPLICIT_UNMAPPED_MARKER = True

# --------------------------------------------------------------------------------------
# §6 Layout intent: the model states the *kind* of drawing it wants, never its arithmetic.
# --------------------------------------------------------------------------------------

Orientation = Literal["landscape", "portrait"]
AspectClass = Literal["standard", "wide", "extra_wide"]
FlowDirection = Literal["left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top"]


@dataclass(frozen=True)
class LayoutIntentField:
    key: str
    allowed: tuple[str, ...]
    required: bool
    note: str


LAYOUT_INTENT_FIELDS: tuple[LayoutIntentField, ...] = (
    LayoutIntentField(
        key="orientation",
        allowed=("landscape", "portrait"),
        required=True,
        note="The prompt's 'wide sheet, not a square' requirement lands here.",
    ),
    LayoutIntentField(
        key="preferred_aspect_class",
        allowed=("standard", "wide", "extra_wide"),
        required=True,
        note="A class, not a ratio and not a pixel size.",
    ),
    LayoutIntentField(
        key="system_order",
        allowed=("declared_system_ids_in_order",),
        required=False,
        note="Which system sits left/right/top/bottom, so 'left=supply, right=offgas' is expressible.",
    ),
    LayoutIntentField(
        key="primary_flow_direction",
        allowed=("left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top"),
        required=True,
        note="Reading direction of the primary process flow.",
    ),
)

LAYOUT_INTENT_DATACLASS_NAME = "DrawingLayoutIntent"
LAYOUT_INTENT_IS_DECLARED_NOT_COMPUTED = True

# --------------------------------------------------------------------------------------
# §7 The canvas is derived from the content. This is why layout intent can be honoured
#    without the model performing pixel arithmetic.
# --------------------------------------------------------------------------------------

CANVAS_IS_DERIVED_FROM_CONTENT = True
CANVAS_GROWS_WHEN_CONTENT_EXCEEDS_CURRENT_BOUNDS = True
CANVAS_SHRINKS_WITHOUT_EXPLICIT_REQUEST = False
CANVAS_MARGIN_IS_REQUIRED = True
CONTENT_MAY_BE_CLIPPED_BY_CANVAS = False
#: An extra-wide intent plus a multi-system topology must stay extra-wide; the grow rule
#: may not be satisfied by growing height alone.
EXTRA_WIDE_INTENT_SURVIVES_CONTENT_GROWTH = True

CANVAS_DERIVATION_INPUTS: tuple[str, ...] = (
    "content_bounds",
    "required_margin",
    "layout_intent.preferred_aspect_class",
    "layout_intent.orientation",
)

# --------------------------------------------------------------------------------------
# §8 Responsibility split. Declared as two disjoint sets so that overlap is a test
#    failure rather than an interpretation.
# --------------------------------------------------------------------------------------

MODEL_OWNS: tuple[str, ...] = (
    "equipment_inventory",
    "equipment_classification",
    "connectivity",
    "system_membership",
    "medium_pressure_diameter",
    "instrument_requirements",
    "redundancy_and_parallel_groups",
    "required_closed_loops",
    "engineering_grouping",
    "layout_intent",
)

CODE_OWNS: tuple[str, ...] = (
    "absolute_coordinates",
    "element_dimensions",
    "canvas_bounds",
    "system_partitioning",
    "hierarchical_placement",
    "orthogonal_routing",
    "obstacle_avoidance",
    "crossing_bridges",
    "label_placement",
    "leader_lines",
    "visual_polish",
)

#: Fields a model-emitted specification may not contain. Named explicitly so that
#: re-admitting coordinate responsibility is a deliberate, reviewable act.
FORBIDDEN_IN_MODEL_OUTPUT: tuple[str, ...] = (
    "absolute_x",
    "absolute_y",
    "element_width",
    "element_height",
    "canvas_width",
    "canvas_height",
    "pixel_margin",
    "connector_waypoints",
)

MODEL_OUTPUT_IS_A_DIAGRAM_SPEC = True
DIAGRAM_SPEC_CARRIES_ABSOLUTE_COORDINATES = False
DIAGRAM_SPEC_NAME = "DiagramSpec"

DIAGRAM_SPEC_SECTIONS: tuple[str, ...] = (
    "systems",
    "equipment",
    "instruments",
    "connections",
    "required_loops",
    "annotations",
    "layout_intent",
)

#: The existing large-diagram pipeline tells the layout engine to keep the model's
#: coordinates. That is the mechanism by which the model ended up owning macro placement.
PRESERVE_POSITIONS_IS_VALID_FOR_LARGE_DIAGRAM_LAYOUT = False

# --------------------------------------------------------------------------------------
# §9 Regression fixtures. Phase 1 declares them; M7-1 acceptance runs them.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptanceFixture:
    key: str
    name: str
    input_shape: str
    must_observe: tuple[str, ...]
    must_not_observe: tuple[str, ...]


ACCEPTANCE_FIXTURES: tuple[AcceptanceFixture, ...] = (
    AcceptanceFixture(
        key="A",
        name="partial_compile_is_not_success",
        input_shape=(
            "A plan whose operations are each individually legal but whose accumulated "
            "state rejects a substantial tail — the observed shape is 197 proposed, 120 "
            "compiled, 77 rejected."
        ),
        must_observe=(
            "completeness == partial",
            "rejected_operation_count == 77",
            "a receipt for every rejected operation",
            "the session is not recorded as completed",
            "the human sees proposed / compiled / rejected counts",
        ),
        must_not_observe=(
            "a success or 'passed validation' message",
            "a completed session status",
            "silent dropping of the rejected operations",
        ),
    ),
    AcceptanceFixture(
        key="B",
        name="unrepresentable_requirement_becomes_a_gap",
        input_shape=(
            "A request naming equipment or an instrument class the catalogue cannot "
            "express — e.g. a molecular-sieve bed, or a transmitter class with no symbol."
        ),
        must_observe=(
            "a catalog_gap naming requested_type, requested_tag, source_requirement",
            "available_alternatives enumerated",
            "completeness == partial",
        ),
        must_not_observe=(
            "silent omission of the requirement",
            "a lookalike symbol presented as the requested device",
            "an invented symbol key",
            "an indicator used as a transmitter",
        ),
    ),
    AcceptanceFixture(
        key="C",
        name="wide_multi_system_spec_is_expressible",
        input_shape=(
            "A specification with several systems and a landscape / extra-wide layout "
            "intent, whose content bounds exceed the current canvas."
        ),
        must_observe=(
            "the layout intent is representable without pixel arithmetic",
            "the canvas grows to contain the content plus margin",
            "the extra-wide aspect class survives the growth",
        ),
        must_not_observe=(
            "content clipped by the canvas",
            "a model-supplied canvas width or height",
            "growth that only adds height",
        ),
    ),
)

# --------------------------------------------------------------------------------------
# §10 Phase-1 boundary: what this milestone has designed but must not yet build, and the
#     specific defects it is named after.
# --------------------------------------------------------------------------------------

#: The five defects this milestone exists to remove, in the remote gate's own severity
#: order. The first two are P0.
DEFECT_REGISTER: tuple[tuple[str, str, str], ...] = (
    (
        "P0",
        "silent_partial_compilation",
        "A partially compiled plan is assessed valid and recorded as a completed session.",
    ),
    (
        "P0",
        "model_owns_low_level_geometry",
        "A one-shot plan makes the model responsible for every coordinate in a large drawing.",
    ),
    (
        "P1",
        "layout_intent_not_expressible",
        "No operation can express a sheet-shape requirement, so 'a wide drawing' is unaskable.",
    ),
    (
        "P1",
        "catalogue_makes_requested_content_unrepresentable",
        "Visibility suppression and genuine catalogue gaps silently remove required content.",
    ),
    (
        "P1",
        "layout_engine_does_not_own_macro_placement",
        "The deterministic layout engine is invoked with preserve_positions, so it never places.",
    ),
)

#: Root causes the evidence does NOT support. Kept as data because the wrong diagnosis is
#: what produced the prompt-hardening recommendation list.
REJECTED_ROOT_CAUSES: tuple[str, ...] = (
    "prompt_quality",
    "model_capability",
    "cad_element_count",
)

PHASE_1_FORBIDDEN_SURFACES: tuple[str, ...] = (
    "new_http_route",
    "new_mcp_tool",
    "new_ui_surface",
    "diagram_spec_runtime",
    "deterministic_layout_runtime",
    "incremental_declaration_primitives",
    "proposal_evidence_persistence",
)

#: Substrings that would appear in an HTTP path or MCP tool name if a surface for this
#: milestone were added ahead of its phase. Checked against the live OpenAPI paths, the
#: live MCP tool names and the declared surface bindings, so the check cannot be satisfied
#: by renaming one of them.
PHASE_1_FORBIDDEN_SURFACE_TOKENS: tuple[str, ...] = (
    "diagram_spec",
    "diagram-spec",
    "synthesis",
    "catalog_gap",
    "catalog-gap",
    "layout_intent",
    "layout-intent",
    "required_loop",
    "required-loop",
)

#: Deferred to later phases, each with the phase that owns it.
DEFERRED_TO: tuple[tuple[str, str], ...] = (
    ("semantic_first_deterministic_layout", "M7-2"),
    ("incremental_declaration_primitives", "M7-3"),
    ("coverage_driven_completion", "M7-3"),
    ("raw_coordinate_responsibility_for_the_model", "never"),
)

#: Incremental primitives named so that M7-3 builds semantic ones, not low-level ones.
M7_3_SEMANTIC_PRIMITIVES: tuple[str, ...] = (
    "declare_system",
    "declare_equipment",
    "declare_instrument",
    "declare_connection",
    "declare_required_loop",
    "complete_subsystem",
)

M7_3_LOW_LEVEL_PRIMITIVES_FORBIDDEN: tuple[str, ...] = (
    "add_symbol_with_coordinates",
    "add_connector_with_waypoints",
)

#: The receipt an agent receives each incremental round in M7-3.
M7_3_RECEIPT_FIELDS: tuple[str, ...] = (
    "required",
    "covered",
    "rejected",
    "catalog_gaps",
    "isolated_equipment",
    "unclosed_required_loops",
    "duplicate_tags",
    "unresolved_relations",
)

#: CAD is a benchmark, never a runtime input.
CAD_IS_A_RUNTIME_INPUT = False
CAD_ROLE: str = "offline_benchmark_reference"
#: The metric the failure report used, and why it is forbidden.
FORBIDDEN_FIDELITY_METRICS: tuple[str, ...] = (
    "semantic_element_count_divided_by_cad_primitive_count",
)
FORBIDDEN_FIDELITY_METRIC_REASON: str = (
    "The two counts are different representations: the imported CAD sheet is 9579 raw "
    "primitives with no symbols and no connectors, while the synthesis is a symbol model. "
    "A ratio between them measures the representation, not the drawing."
)

# --------------------------------------------------------------------------------------
# §11 Phase-2A runtime obligations. The part of the contract that now exists as code.
# --------------------------------------------------------------------------------------

PROPOSAL_EVIDENCE_TABLE = "synthesis_proposal_evidence"
PROPOSAL_EVIDENCE_CARRIER: str = "dedicated_append_only_table"
PROPOSAL_EVIDENCE_MAY_LIVE_IN_TOOL_CALL_METADATA = False
#: Proposals and tool calls are not one-to-one, so a tool call cannot be the container:
#: doing that would again show only the last attempt and hide the rejected plans.
PROPOSAL_EVIDENCE_IS_APPEND_ONLY = True
PROPOSAL_EVIDENCE_OVERWRITE_ALLOWED = False

PROPOSAL_EVIDENCE_REQUIRED_FIELDS_PHASE_2A: tuple[str, ...] = (
    "proposal_evidence_id",
    "session_id",
    "document_id",
    "proposal_attempt_index",
    "provider_class",
    "model",
    "planner_identity",
    "raw_proposed_operations",
    "proposed_operation_count",
    "accepted_operation_count",
    "compiled_operation_count",
    "rejected_operation_count",
    "rejected_operations",
    "validity",
    "completeness",
    "operation_accounting",
    "global_failure_reason",
    "compiler_version",
    "proposal_payload_digest",
    "assessment_digest",
    "related_tool_call_id",
    "created_at",
)

#: The counts a proposal record must satisfy. ``accepted`` is the count of retained semantic
#: operations; ``compiled`` keeps its existing meaning of produced low-level operations.
PROPOSAL_COUNT_INVARIANTS: tuple[str, ...] = (
    "proposed_operation_count == accepted_operation_count + rejected_operation_count",
    "rejected_operation_count == len(rejected_operations)",
    "completeness follows from the counts and is never supplied",
    "a partial proposal has at least one rejection",
    "a complete proposal has none",
    "a proposal that was not evaluated carries no counts and no completeness verdict",
)

#: Session completion is gated in the backend, not only in the interface. A partial plan must
#: not be recordable as completed by any caller, including one that never draws a dialog.
COMPLETED_SESSION_REQUIRES: tuple[str, ...] = ("valid", "complete")
COMPLETED_SESSION_IS_REFUSED_IN_BACKEND = True
COMPLETED_SESSION_REFUSAL_IS_UI_ONLY = False

# --------------------------------------------------------------------------------------
# §12 Phase-2A closeout. The three closures the review required before the gate may shut:
# a proposal is accounted for or explicitly unaccounted, the real agent loop asks again on a
# partial plan, and the completeness arithmetic is visible where a human decides.
# --------------------------------------------------------------------------------------

#: A proposal is either accounted operation by operation, or it is not accounted at all.
#: There is no third state in which the counts are unknown but reported as numbers.
OPERATION_ACCOUNTING_STATES: tuple[str, ...] = ("evaluated", "not_evaluated")
ACCOUNTING_STATE_IS_DECLARED_NOT_INFERRED = True
#: Zero is a claim ("nothing survived"); the absence of a claim is ``None``, not ``0``.
ZERO_IS_A_STAND_IN_FOR_UNKNOWN = False
#: The six-cell validity x completeness matrix describes evaluated proposals and only those,
#: which is why a second completeness value was not invented for "unknown".
SIX_CELL_MATRIX_APPLIES_TO: tuple[str, ...] = ("evaluated",)
#: An uncounted proposal still has to say why it was never counted.
NOT_EVALUATED_PROPOSAL_MUST_RECORD_WHY = True

#: Every proposal a caller terminates leaves exactly one durable row, evaluated or not:
#: "no row" previously meant "no evidence we could inspect", and that is how 77 dropped
#: operations stayed invisible. One, not zero, and not one per call site.
EVIDENCE_PER_TERMINAL_ATTEMPT: int = 1
TERMINAL_PROPOSAL_WITHOUT_EVIDENCE_IS_ALLOWED = False

#: What the confirmation surface must show before a human may approve anything.
COMPLETENESS_PRESENTATION_FIELDS: tuple[str, ...] = (
    "proposed_operation_count",
    "accepted_operation_count",
    "rejected_operation_count",
    "completeness",
)
#: A partial plan may not be dressed as a passing plan: no "validated" wording and no CTA.
PARTIAL_PROPOSAL_MAY_RENDER_AS_PASSED_VALIDATION = False
PARTIAL_PROPOSAL_MAY_OFFER_APPLY = False
#: The real automatic-agent loop must act on the receipt: valid + partial asks for the next
#: proposal rather than stopping at "cannot proceed". The existing replan limit is reused.
PARTIAL_PROPOSAL_REQUESTS_NEXT_ATTEMPT = True
PARTIAL_REPLAN_USES_EXISTING_LIMIT = True

#: A response that does not carry the accounting contract is not a business outcome, it is a
#: protocol violation, and the two must not share a recovery path. The reason is cost: a
#: legitimate ``not_evaluated`` answer is a real refusal worth replanning around, while a
#: malformed response is a server defect -- silently replanning it would spend the model's
#: attempts hiding a broken contract. The remote gate drew this line; these are its names.
ASSESSMENT_CONTRACT_VIOLATION_CODE = "assessment_contract_violation"
ASSESSMENT_CONTRACT_VIOLATION_IS_A_BUSINESS_OUTCOME = False
CONTRACT_VIOLATION_MAY_APPLY = False
CONTRACT_VIOLATION_MAY_TRIGGER_AUTOMATIC_SEMANTIC_REPLAN = False
CONTRACT_VIOLATION_REQUIRES_USER_VISIBLE_MESSAGE = True

#: The axis a consumer reads first. Absent or unrecognised, the response is malformed rather
#: than merely unevaluated -- absence is not a fourth accounting state.
ACCOUNTING_AXIS_FIELD = "operation_accounting"

#: What each accounting state must actually carry to be that state. An ``evaluated``
#: assessment without counts is not a partial result, it is a broken envelope; a
#: ``not_evaluated`` one without a reason explains nothing.
EVALUATED_ASSESSMENT_REQUIRED_FIELDS: tuple[str, ...] = (
    "accepted_operation_count",
    "rejected_operation_count",
    "completeness",
    "rejected_operations",
)
NOT_EVALUATED_ASSESSMENT_REQUIRED_FIELDS: tuple[str, ...] = ("global_failure_reason",)


@dataclass(frozen=True)
class AssessmentBranch:
    """One exhaustive route an assessment may take, and what it may do on that route.

    Declared as data for the same reason the two axes are: the previous defect was a branch
    that nobody had enumerated ("valid, so stop"), so the fix is to enumerate them and let a
    test read the runtime against the list.
    """

    key: str
    applies_to: str
    may_apply: bool
    may_replan_automatically: bool
    note: str


ASSESSMENT_BRANCHES: tuple[AssessmentBranch, ...] = (
    AssessmentBranch(
        key="evaluated_complete",
        applies_to="evaluated",
        may_apply=True,
        may_replan_automatically=False,
        note="Nothing was dropped; this is the only branch that may reach a human.",
    ),
    AssessmentBranch(
        key="evaluated_partial",
        applies_to="evaluated",
        may_apply=False,
        may_replan_automatically=True,
        note="Legal but not whole: the receipt names what to repair.",
    ),
    AssessmentBranch(
        key="evaluated_empty",
        applies_to="evaluated",
        may_apply=False,
        may_replan_automatically=True,
        note=(
            "Nothing survived. Approved as not applyable: if a product ever needs "
            "'no change required', that must be its own outcome rather than an empty "
            "transaction dressed as a successful apply."
        ),
    ),
    AssessmentBranch(
        key="evaluated_invalid",
        applies_to="evaluated",
        may_apply=False,
        may_replan_automatically=True,
        note="Hard invalidity keeps its precedence over completeness and its existing recovery.",
    ),
    AssessmentBranch(
        key="not_evaluated",
        applies_to="not_evaluated",
        may_apply=False,
        may_replan_automatically=True,
        note=(
            "A real answer that the compiler never reached an operation: recover using the "
            "recorded global failure reason, which may include replanning with new context."
        ),
    ),
    AssessmentBranch(
        key=ASSESSMENT_CONTRACT_VIOLATION_CODE,
        applies_to="any_response",
        may_apply=False,
        may_replan_automatically=False,
        note=(
            "The response does not carry the accounting contract at all. Zero apply, zero "
            "automatic semantic replan, and a message a human can act on (refresh or "
            "regenerate) -- because the alternative is spending replan attempts on a defect "
            "that no proposal can fix."
        ),
    ),
)

#: Which declared branch each continuation action is. The matrix above stays a statement
#: about the two axes; this is the statement about what the runtime does with each cell.
CONTINUATION_ACTION_BRANCH: tuple[tuple[ContinuationAction, str], ...] = (
    ("proceed_to_human_confirmation", "evaluated_complete"),
    ("return_receipt_and_replan", "evaluated_partial"),
    ("replan_or_abort", "evaluated_empty"),
    ("existing_error_recovery", "evaluated_invalid"),
)

#: The verdicts the read-only catalogue audit may return, in classification order.
CATALOGUE_AUDIT_VERDICTS: tuple[str, ...] = (
    "visible",
    "hidden",
    "missing",
    "compiler_unsupported",
    "renderer_unsupported",
)
CATALOGUE_AUDIT_CLASSIFIES_EXISTENCE_BEFORE_VISIBILITY = True

BENCHMARK_ACCEPTANCE_ITEMS: tuple[str, ...] = (
    "required_equipment",
    "required_instruments",
    "required_connections",
    "required_closed_loops",
    "required_systems",
    "reference_zones",
    "reference_layout_characteristics",
)


# --------------------------------------------------------------------------------------
# The validator. Every relaxation above that would make the failure possible again is
# reported here, and each one has a mutation test in the companion test module.
# --------------------------------------------------------------------------------------


def iter_continuation_rules() -> tuple[ContinuationRule, ...]:
    return CONTINUATION_MATRIX


def continuation_rule(validity: Validity, completeness: Completeness) -> ContinuationRule:
    """The declared rule for one cell of the matrix. The matrix is total, so this raises."""

    for rule in CONTINUATION_MATRIX:
        if rule.validity == validity and rule.completeness == completeness:
            return rule
    raise KeyError(f"no continuation rule for ({validity}, {completeness})")


def validate_contract() -> list[str]:
    """Return the list of contract violations (empty means the contract is coherent)."""

    problems: list[str] = []

    # §1: the axes are real axes, and success needs both.
    if tuple(name for name, _ in SYNTHESIS_ASSESSMENT_AXES) != ("validity", "completeness"):
        problems.append("the assessment must declare validity and completeness, in that order")
    if SESSION_SUCCESS_REQUIRES != ("valid", "complete"):
        problems.append("a session may only succeed on valid + complete")
    if not COMPLETENESS_IS_INDEPENDENT_OF_VALIDITY:
        problems.append(
            "completeness must stay independent of validity; collapsing them is the defect"
        )
    if not COMPLETENESS_IS_DERIVED_FROM_COUNTS:
        problems.append("completeness must be derived from the proposed/compiled counts")

    # §1: the counts that make the loss visible.
    for required in ("proposed_operation_count", "compiled_operation_count"):
        if required not in SYNTHESIS_COUNT_FIELDS:
            problems.append(f"the assessment must carry {required!r}")
    if "rejected_operation_count" not in SYNTHESIS_COUNT_FIELDS:
        problems.append("the assessment must carry 'rejected_operation_count'")

    # §2: durable proposal evidence.
    if not RAW_PROPOSAL_IS_DURABLE_EVIDENCE:
        problems.append(
            "the raw proposal must be durable evidence, not reconstructed from the diff"
        )
    if not APPLIED_ONLY_PERSISTENCE_IS_FORBIDDEN:
        problems.append("persisting only the applied transaction must stay forbidden")
    for field in ("proposed_operations", "rejected_operations"):
        if field not in PROPOSAL_EVIDENCE_REQUIRED_FIELDS:
            problems.append(f"proposal evidence must record {field!r}")
    for field in ("reason_code", "available_values", "suggestions"):
        if field not in REJECTED_OPERATION_RECEIPT_FIELDS:
            problems.append(f"a rejected-operation receipt must carry {field!r}")

    # §3: the matrix is total over the cross product, and partial never succeeds.
    seen = {(rule.validity, rule.completeness) for rule in CONTINUATION_MATRIX}
    expected = {(v, c) for v in VALIDITY_VALUES for c in COMPLETENESS_VALUES}
    missing = sorted(expected - seen)
    if missing:
        problems.append(f"the continuation matrix must be total; missing {missing}")
    if len(CONTINUATION_MATRIX) != len(expected):
        problems.append("the continuation matrix must have exactly one rule per cell")
    partial = continuation_rule("valid", "partial")
    if partial.action != "return_receipt_and_replan":
        problems.append(
            f"valid + partial must return the receipt and replan, found {partial.action!r}"
        )
    complete = continuation_rule("valid", "complete")
    if complete.action != "proceed_to_human_confirmation":
        problems.append("valid + complete must be the only path to human confirmation")
    for rule in CONTINUATION_MATRIX:
        if rule.validity != "valid" and rule.action != "existing_error_recovery":
            problems.append(
                f"invalid plans must keep the existing recovery path, found {rule.action!r}"
            )
    if PARTIAL_MAY_BE_REPORTED_AS_SUCCESS:
        problems.append("a partial result must never be reported as success")
    if PARTIAL_MAY_BE_RECORDED_AS_COMPLETED_SESSION:
        problems.append("a partial result must never be recorded as a completed session")
    if RECEIPT_IS_OMITTABLE:
        problems.append("the rejection receipt must not be suppressible")

    # §4/§5: catalogue audit and gaps.
    if CATALOGUE_AUDIT_STEPS[0] != "declared_requirement":
        problems.append("the catalogue audit must be anchored on the declared requirement")
    for step in ("visible_to_model", "compiler_accepts", "renderer_supports"):
        if step not in CATALOGUE_AUDIT_STEPS:
            problems.append(f"the catalogue audit must check {step!r}")
    if not HIDDEN_IS_NOT_MISSING:
        problems.append("hidden and missing are different defects and must stay distinguishable")
    if not CATALOG_GAP_IS_REPORTED_NOT_HIDDEN:
        problems.append("a catalog gap must be reported, never hidden")
    if CATALOG_GAP_FORCES_COMPLETENESS != "partial":
        problems.append("a catalog gap must force completeness to partial")
    if AUTO_GENERIC_SUBSTITUTION_ALLOWED_IN_PHASE_1:
        problems.append("auto generic substitution stays forbidden in phase 1")
    forbidden_gap_handling = set(CATALOGUE_GAP_FORBIDDEN_HANDLING)
    for handling in (
        "silent_omission",
        "substitute_a_lookalike_symbol",
        "invent_an_undefined_symbol_key",
    ):
        if handling not in forbidden_gap_handling:
            problems.append(f"{handling!r} must stay forbidden for unrepresentable requirements")

    # §6: layout intent is declared, never computed.
    intent_keys = {field.key for field in LAYOUT_INTENT_FIELDS}
    for key in ("orientation", "preferred_aspect_class", "primary_flow_direction"):
        if key not in intent_keys:
            problems.append(f"layout intent must declare {key!r}")
    for field in LAYOUT_INTENT_FIELDS:
        if not field.allowed:
            problems.append(f"layout intent field {field.key!r} must declare allowed values")
    if not LAYOUT_INTENT_IS_DECLARED_NOT_COMPUTED:
        problems.append("layout intent must be declared, not computed")

    # §7: the canvas follows the content.
    if not CANVAS_IS_DERIVED_FROM_CONTENT:
        problems.append("the canvas must be derived from the content")
    if not CANVAS_GROWS_WHEN_CONTENT_EXCEEDS_CURRENT_BOUNDS:
        problems.append("the canvas must grow when the content exceeds it")
    if CONTENT_MAY_BE_CLIPPED_BY_CANVAS:
        problems.append("content must never be clipped by the canvas")
    if CANVAS_SHRINKS_WITHOUT_EXPLICIT_REQUEST:
        problems.append("the canvas must not shrink without an explicit request")
    if not EXTRA_WIDE_INTENT_SURVIVES_CONTENT_GROWTH:
        problems.append("an extra-wide intent must survive content growth")
    if "required_margin" not in CANVAS_DERIVATION_INPUTS:
        problems.append("canvas derivation must include a required margin")

    # §8: the split is a partition, and coordinates stay out of the model's output.
    overlap = sorted(set(MODEL_OWNS) & set(CODE_OWNS))
    if overlap:
        problems.append(f"the responsibility split must be disjoint; overlapping {overlap}")
    if "absolute_coordinates" not in CODE_OWNS:
        problems.append("code must own absolute coordinates")
    if "absolute_coordinates" in MODEL_OWNS:
        problems.append("the model must not own absolute coordinates")
    for field in ("absolute_x", "absolute_y", "canvas_width", "canvas_height"):
        if field not in FORBIDDEN_IN_MODEL_OUTPUT:
            problems.append(f"{field!r} must be forbidden in model output")
    if DIAGRAM_SPEC_CARRIES_ABSOLUTE_COORDINATES:
        problems.append("the diagram specification must not carry absolute coordinates")
    if PRESERVE_POSITIONS_IS_VALID_FOR_LARGE_DIAGRAM_LAYOUT:
        problems.append(
            "preserve_positions must not be the large-diagram layout strategy; that is how "
            "the model came to own macro placement"
        )

    # §9: the fixtures exist, are distinct, and each forbids something.
    keys = [fixture.key for fixture in ACCEPTANCE_FIXTURES]
    if keys != ["A", "B", "C"]:
        problems.append(f"the three phase-1 fixtures must be declared as A/B/C, found {keys}")
    for fixture in ACCEPTANCE_FIXTURES:
        if not fixture.must_observe:
            problems.append(f"fixture {fixture.key} must declare what it must observe")
        if not fixture.must_not_observe:
            problems.append(f"fixture {fixture.key} must declare what it must not observe")

    # §10: the severity register and the phase boundary.
    severities = [severity for severity, _, _ in DEFECT_REGISTER]
    if severities[:2] != ["P0", "P0"]:
        problems.append("the register's two highest-severity defects must both be P0")
    for _, key, _ in DEFECT_REGISTER:
        if key in REJECTED_ROOT_CAUSES:
            problems.append(f"{key!r} is registered as a defect but also as a rejected cause")
    registered = {key for _, key, _ in DEFECT_REGISTER}
    for key in (
        "silent_partial_compilation",
        "model_owns_low_level_geometry",
        "layout_intent_not_expressible",
    ):
        if key not in registered:
            problems.append(f"the defect register must name {key!r}")
    for _, phase in DEFERRED_TO:
        if phase not in ("M7-2", "M7-3", "never"):
            problems.append(f"deferred work must name a known phase, found {phase!r}")
    if CAD_IS_A_RUNTIME_INPUT:
        problems.append("CAD must not be a runtime input")
    if CAD_ROLE != "offline_benchmark_reference":
        problems.append("CAD's declared role is an offline benchmark reference")
    if "semantic_element_count_divided_by_cad_primitive_count" not in FORBIDDEN_FIDELITY_METRICS:
        problems.append("the representation-mismatched fidelity ratio must stay forbidden")

    # §11: phase-2A runtime obligations.
    if PROPOSAL_EVIDENCE_MAY_LIVE_IN_TOOL_CALL_METADATA:
        problems.append(
            "proposal evidence must not live in tool-call metadata; proposals and tool "
            "calls are not one-to-one"
        )
    if not PROPOSAL_EVIDENCE_IS_APPEND_ONLY or PROPOSAL_EVIDENCE_OVERWRITE_ALLOWED:
        problems.append("proposal evidence must be append-only")
    if PROPOSAL_EVIDENCE_CARRIER != "dedicated_append_only_table":
        problems.append(
            f"proposal evidence carrier must be a dedicated append-only table, found "
            f"{PROPOSAL_EVIDENCE_CARRIER!r}"
        )
    for field in (
        "raw_proposed_operations",
        "accepted_operation_count",
        "rejected_operations",
        "completeness",
        "related_tool_call_id",
    ):
        if field not in PROPOSAL_EVIDENCE_REQUIRED_FIELDS_PHASE_2A:
            problems.append(f"a proposal record must carry {field!r}")
    if not any(
        "accepted_operation_count" in invariant for invariant in PROPOSAL_COUNT_INVARIANTS
    ):
        problems.append("the count invariants must relate proposed, accepted and rejected")
    if COMPLETED_SESSION_REQUIRES != ("valid", "complete"):
        problems.append("a completed session requires a valid and complete proposal")
    if COMPLETED_SESSION_REFUSAL_IS_UI_ONLY or not COMPLETED_SESSION_IS_REFUSED_IN_BACKEND:
        problems.append("session completion must be refused in the backend, not only in the UI")

    # §12: the closeout. An unaccounted proposal must look unaccounted, not empty.
    if OPERATION_ACCOUNTING_STATES != ("evaluated", "not_evaluated"):
        problems.append(
            f"a proposal is accounted for or explicitly not, found {OPERATION_ACCOUNTING_STATES!r}"
        )
    if not ACCOUNTING_STATE_IS_DECLARED_NOT_INFERRED:
        problems.append("the accounting state must be declared rather than inferred from counts")
    if ZERO_IS_A_STAND_IN_FOR_UNKNOWN:
        problems.append("zero must not stand in for an unknown count")
    if SIX_CELL_MATRIX_APPLIES_TO != ("evaluated",):
        problems.append("the six-cell matrix must apply to evaluated proposals only")
    if not NOT_EVALUATED_PROPOSAL_MUST_RECORD_WHY:
        problems.append("an unevaluated proposal must record why it was not evaluated")
    if EVIDENCE_PER_TERMINAL_ATTEMPT != 1 or TERMINAL_PROPOSAL_WITHOUT_EVIDENCE_IS_ALLOWED:
        problems.append("every terminal proposal attempt must leave exactly one durable row")
    for field in ("operation_accounting", "global_failure_reason", "raw_proposed_operations"):
        if field not in PROPOSAL_EVIDENCE_REQUIRED_FIELDS_PHASE_2A:
            problems.append(f"a proposal record must carry {field!r}")
    for field in ("proposed_operation_count", "accepted_operation_count"):
        if field not in COMPLETENESS_PRESENTATION_FIELDS:
            problems.append(f"the confirmation surface must show {field!r}")
    for field in ("rejected_operation_count", "completeness"):
        if field not in COMPLETENESS_PRESENTATION_FIELDS:
            problems.append(f"the confirmation surface must show {field!r}")
    if PARTIAL_PROPOSAL_MAY_RENDER_AS_PASSED_VALIDATION:
        problems.append("a partial plan must not be rendered as a plan that passed validation")
    if PARTIAL_PROPOSAL_MAY_OFFER_APPLY:
        problems.append("a partial plan must not offer an apply button")
    if not PARTIAL_PROPOSAL_REQUESTS_NEXT_ATTEMPT:
        problems.append("a partial plan must make the agent ask for the next proposal")
    if not PARTIAL_REPLAN_USES_EXISTING_LIMIT:
        problems.append("the partial replan must reuse the existing replan limit")
    if CATALOGUE_AUDIT_VERDICTS[:3] != ("visible", "hidden", "missing"):
        problems.append(
            "the catalogue audit must classify visible, hidden and missing before support"
        )
    if not CATALOGUE_AUDIT_CLASSIFIES_EXISTENCE_BEFORE_VISIBILITY:
        problems.append("the catalogue audit must ask existence before visibility")

    # §13: malformed is not a business outcome, and the branches are exhaustive.
    if ACCOUNTING_AXIS_FIELD != "operation_accounting":
        problems.append("the accounting axis is 'operation_accounting'")
    if ASSESSMENT_CONTRACT_VIOLATION_IS_A_BUSINESS_OUTCOME:
        problems.append(
            "a response missing the accounting contract is a protocol violation, not a "
            "business outcome that may be replanned like a partial plan"
        )
    if CONTRACT_VIOLATION_MAY_APPLY:
        problems.append("an assessment contract violation must not apply anything")
    if CONTRACT_VIOLATION_MAY_TRIGGER_AUTOMATIC_SEMANTIC_REPLAN:
        problems.append(
            "an assessment contract violation must not trigger an automatic semantic replan"
        )
    if not CONTRACT_VIOLATION_REQUIRES_USER_VISIBLE_MESSAGE:
        problems.append("an assessment contract violation must say so to the user")
    if not EVALUATED_ASSESSMENT_REQUIRED_FIELDS:
        problems.append("an evaluated assessment must declare what it must carry")
    if ACCOUNTING_AXIS_FIELD in EVALUATED_ASSESSMENT_REQUIRED_FIELDS:
        problems.append("the accounting axis is the premise of being evaluated, not one of its fields")
    if not NOT_EVALUATED_ASSESSMENT_REQUIRED_FIELDS:
        problems.append("an unevaluated assessment must declare that it carries a reason")

    keys = [branch.key for branch in ASSESSMENT_BRANCHES]
    if len(keys) != len(set(keys)):
        problems.append("assessment branches must have distinct keys")
    if ASSESSMENT_CONTRACT_VIOLATION_CODE not in keys:
        problems.append(
            f"the contract violation branch must be declared as {ASSESSMENT_CONTRACT_VIOLATION_CODE!r}"
        )
    violation = [branch for branch in ASSESSMENT_BRANCHES if branch.key == ASSESSMENT_CONTRACT_VIOLATION_CODE]
    if len(violation) == 1:
        if violation[0].may_replan_automatically or violation[0].may_apply:
            problems.append("the contract violation branch may neither apply nor replan")
        if violation[0].applies_to != "any_response":
            problems.append("a malformed response can arrive at any accounting state")
    non_bypassable = [
        branch.key
        for branch in ASSESSMENT_BRANCHES
        if not branch.may_apply and not branch.may_replan_automatically
    ]
    if non_bypassable != [ASSESSMENT_CONTRACT_VIOLATION_CODE]:
        problems.append(
            "exactly one branch may be neither applied nor replanned: the contract violation"
        )
    applyable = [branch.key for branch in ASSESSMENT_BRANCHES if branch.may_apply]
    if applyable != ["evaluated_complete"]:
        problems.append("only a valid and complete evaluated proposal may apply")

    declared_actions = {action for action, _ in CONTINUATION_ACTION_BRANCH}
    if declared_actions != set(CONTINUATION_ACTIONS):
        problems.append("every continuation action must name the branch it leads to")
    mapped = {key for _, key in CONTINUATION_ACTION_BRANCH}
    evaluated_keys = {
        branch.key for branch in ASSESSMENT_BRANCHES if branch.applies_to == "evaluated"
    }
    if mapped != evaluated_keys:
        problems.append(
            "the matrix cells must map exactly onto the evaluated branches, no more and no fewer"
        )
    for rule in CONTINUATION_MATRIX:
        branch_key = dict(CONTINUATION_ACTION_BRANCH).get(rule.action)
        if branch_key is None:
            problems.append(f"cell ({rule.validity}, {rule.completeness}) names no branch")
    for branch in ASSESSMENT_BRANCHES:
        if branch.applies_to == "evaluated" and not any(
            dict(CONTINUATION_ACTION_BRANCH).get(rule.action) == branch.key
            for rule in CONTINUATION_MATRIX
        ):
            problems.append(f"evaluated branch {branch.key!r} is unreachable from the matrix")

    return problems


if __name__ == "__main__":  # pragma: no cover - manual review entry point
    violations = validate_contract()
    if violations:
        for violation in violations:
            print(f"CONTRACT VIOLATION: {violation}")
        raise SystemExit(1)
    print(f"{M7_CONTRACT_VERSION}: contract coherent")
