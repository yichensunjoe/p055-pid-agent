"""M7-2 phase 1: the contract for semantic-first deterministic layout, as declared data.

M7 phase 2A made an incomplete synthesis say so. The other half of the same finding is
still open: by byte count, 80% of what the model had to emit was coordinates and styling,
in a system that already contains a deterministic layout engine -- wired to
``preserve_positions=True``, i.e. explicitly told not to place anything. Drawing a
plant-wide P&ID was therefore bounded by how many coordinate pairs fit in one completion
rather than by how much *meaning* fits.

M7-2 states the division of labour as something a test can contradict:

1. **The model owns meaning, code owns placement.** The synthesis output is a diagram
   specification -- systems, equipment, connections, required loops, and *discrete* layout
   intent. It carries no coordinates at all, absolute or relative.
2. **Layout is a function, not a drawing.** The same specification, engine version and rules
   version produce the same canonical layout digest, and the layout cannot alter engineering
   meaning: no connection may be dropped, no device reclassified, no tag or system membership
   rewritten because it would draw better.

Design choices worth stating, because they are the reason this is a module and not a
paragraph:

* **There is exactly one layout authority.** ``AutoLayoutEngine`` already owns system
  partition, ranking, placement, routing, obstacle avoidance, annotation and bounds. M7-2
  adds an *adapter* in front of it and gives it discrete constraints; it does not add a second
  placer. Two placers would be two answers to "where does this go", which is the class of
  defect this milestone exists to remove.
* **Canvas is an output.** Accepting ``canvas_width`` / ``canvas_height`` as request
  parameters would reintroduce a second canvas authority -- and with it, the 1600x900 default
  that made a 12:1 plant drawing impossible to ask for. The request may carry intent
  (``extra_wide``) and the engine derives the bounds.
* **``preserve_positions`` is split, not deleted.** It is wrong for synthesis and right for
  a human-edited drawing, a local reroute, or legacy manual editing. The rule is scoped to the
  path rather than to the repository, so M7 does not break editing to make drawing work.
* **Layout intent is discrete.** "Landscape, extra wide, left to right, grouped by system" is
  intent. "P-201 is left of R-101" is a geometry language with extra steps: it hands the model
  the placement decisions back through a side door. Free relative anchors are therefore
  forbidden in v1, and the semantic adjacency constraints that may replace them later are
  named here so that the substitution is visible rather than improvised.
* **Phase 1 is design and contract only.** As in M7 phase 1 and M6, the forbidden list is part
  of the contract, and a test enumerates the live surface and asserts none of it exists yet.
  Nothing in this module is imported by the application, and no production drawing behaviour
  changes.

The companion task book is ``docs/m7-2-deterministic-layout.md``; the binding test asserts
that every name declared here is named there, so prose and data cannot separate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

M7_LAYOUT_CONTRACT_VERSION = "m7-layout-contract/1"

# --------------------------------------------------------------------------------------
# §1 Who decides what. The split is the milestone: read it as "what the model may be asked
#    to know" versus "what code must never ask it to guess".
# --------------------------------------------------------------------------------------

Responsibility = Literal["model", "code"]

MODEL_OWNS: tuple[str, ...] = (
    "meaning",
    "systems",
    "equipment",
    "connections",
    "required_loops",
    "layout_intent",
)

CODE_OWNS: tuple[str, ...] = (
    "system_partition",
    "rank_assignment",
    "absolute_placement",
    "spacing",
    "orthogonal_routing",
    "obstacle_avoidance",
    "annotation_placement",
    "canvas_bounds",
)

#: The two prohibitions this milestone is measured by.
MODEL_OUTPUT_MAY_CONTAIN_ABSOLUTE_GEOMETRY = False
MODEL_OUTPUT_MAY_CONTAIN_RELATIVE_ANCHORS = False
LAYOUT_ENGINE_IS_SINGLE_AUTHORITY = True
DIAGRAM_SPEC_CARRIES_ABSOLUTE_COORDINATES = False

#: Capabilities of the diagram specification. Named as data so the runtime cannot quietly
#: grow a coordinate field: anything not listed is not part of the specification.
DIAGRAM_SPEC_DECLARES: tuple[str, ...] = (
    "system",
    "equipment",
    "instrument",
    "connection",
    "required_loop",
    "layout_intent",
)

#: Geometry-shaped field classes the model may never emit. These are the names a regression
#: would use, which is why they are enumerated rather than described.
FORBIDDEN_MODEL_GEOMETRY_FIELDS: tuple[str, ...] = (
    "x",
    "y",
    "position",
    "points",
    "waypoints",
    "width",
    "height",
    "canvas_width",
    "canvas_height",
    "rotation",
    "start",
    "end",
    "center",
    "radius",
    "port_x",
    "port_y",
)

#: The same names are legitimate as *layout output*. The distinction is direction, not
#: spelling: coordinates flowing out of the engine are the point; coordinates flowing into
#: the model's output are the defect.
FORBIDDEN_MODEL_GEOMETRY_FIELDS_ARE_ALLOWED_AS_LAYOUT_OUTPUT = True

# --------------------------------------------------------------------------------------
# §2 preserve_positions: a path-scoped rule, not a repository-wide deletion.
# --------------------------------------------------------------------------------------

M7_SYNTHESIS_USES_PRESERVE_POSITIONS = False
LEGACY_MANUAL_LAYOUT_MAY_PRESERVE_POSITIONS = True
LEGACY_PRESERVE_POSITIONS_PATHS: tuple[str, ...] = (
    "human_edited_drawing",
    "local_reroute",
    "legacy_manual_editing",
)

# --------------------------------------------------------------------------------------
# §3 Canvas is an output of layout, derived from content. The request states intent.
# --------------------------------------------------------------------------------------

CANVAS_IS_LAYOUT_OUTPUT = True
AUTO_LAYOUT_REQUEST_MAY_TAKE_CANVAS_PIXELS = False
AUTO_LAYOUT_REQUEST_FORBIDDEN_PARAMETERS: tuple[str, ...] = ("canvas_width", "canvas_height")
AUTO_LAYOUT_REQUEST_DISCRETE_CONSTRAINTS: tuple[str, ...] = (
    "layout_intent",
    "system_order",
    "primary_flow_direction",
    "grouping",
)
AUTO_LAYOUT_OUTPUT_ADDS: tuple[str, ...] = (
    "content_bounds",
    "canvas_bounds",
    "canonical_layout_digest",
)
CANVAS_GROWS_TO_FIT_CONTENT = True
CANVAS_MARGIN_IS_DECLARED_NOT_ASSUMED = True
#: Growing the canvas must not dissolve the intent it was asked to satisfy: an `extra_wide`
#: drawing stays extra wide after it grows, or the intent was decoration.
ASPECT_CLASS_SURVIVES_GROWTH = True


@dataclass(frozen=True)
class LayoutIntentDimension:
    """One discrete dimension of layout intent, with the values it may take.

    ``open_ended`` marks a dimension whose values are not a fixed vocabulary but a validated
    reference to declared objects (``system_order`` names systems that must exist). It is a
    flag rather than an empty tuple so that "open" and "unimplemented" cannot look alike.
    """

    name: str
    values: tuple[str, ...]
    open_ended: bool
    note: str


LAYOUT_INTENT_DIMENSIONS: tuple[LayoutIntentDimension, ...] = (
    LayoutIntentDimension(
        name="orientation",
        values=("landscape", "portrait"),
        open_ended=False,
        note="Which way the drawing reads. Not a size.",
    ),
    LayoutIntentDimension(
        name="preferred_aspect_class",
        values=("standard", "wide", "extra_wide"),
        open_ended=False,
        note=(
            "The class the reference plant drawing needs is `extra_wide` -- roughly 12:1. "
            "A class, not a number of pixels: the engine decides the pixels."
        ),
    ),
    LayoutIntentDimension(
        name="primary_flow_direction",
        values=("left_to_right", "top_to_bottom"),
        open_ended=False,
        note="Which way the main process runs, so ranking follows the process order.",
    ),
    LayoutIntentDimension(
        name="system_order",
        values=(),
        open_ended=True,
        note="An explicit sequence of declared system ids, validated against the spec.",
    ),
    LayoutIntentDimension(
        name="grouping",
        values=("grouped_by_system", "grouped_by_zone", "flat"),
        open_ended=False,
        note="Keeping one system's equipment together is an organisational statement.",
    ),
    LayoutIntentDimension(
        name="density",
        values=("compact", "comfortable"),
        open_ended=False,
        note="Spacing class, so crowding is asked for once instead of nudged per element.",
    ),
)

#: Free relative anchors are a geometry language wearing semantic clothes: they hand the
#: model placement decisions again, one predicate at a time.
FREE_RELATIVE_ANCHORS_ALLOWED_IN_V1 = False
FORBIDDEN_RELATIVE_ANCHOR_PREDICATES: tuple[str, ...] = (
    "left_of",
    "right_of",
    "above",
    "below",
    "near",
    "closer_than",
    "distance_ratio",
)

#: If engineering later needs relationships, these are the ones to introduce: they express
#: topology and organisation, not positions. Declared now so the substitution is a visible
#: act rather than an improvisation.
FUTURE_SEMANTIC_ADJACENCY_CONSTRAINTS: tuple[str, ...] = (
    "same_system",
    "upstream_of",
    "downstream_of",
    "keep_together",
    "separate_groups",
)

# --------------------------------------------------------------------------------------
# §4 Layout may change presentation and must not change meaning. This is the negative gate.
# --------------------------------------------------------------------------------------

LAYOUT_MAY_CHANGE_TOPOLOGY = False
LAYOUT_MAY_CREATE_OR_DELETE_ENGINEERING_EQUIPMENT = False
LAYOUT_MAY_CHANGE_TAGS = False
LAYOUT_MAY_CHANGE_SYSTEM_MEMBERSHIP = False
SEMANTIC_DIGEST_BEFORE_LAYOUT_MUST_EQUAL_SEMANTIC_DIGEST_AFTER = True

#: What layout is allowed to produce. Nothing here is engineering semantics.
LAYOUT_MAY_CHANGE_PRESENTATION_ONLY: tuple[str, ...] = (
    "waypoints",
    "annotation_positions",
    "leader_lines",
    "crossing_presentation",
    "canvas_bounds",
)

#: Words that must never appear in the presentation-only list, because they name meaning.
ENGINEERING_SEMANTIC_NOUNS: tuple[str, ...] = (
    "connections",
    "equipment",
    "instruments",
    "tags",
    "systems",
    "topology",
)

# --------------------------------------------------------------------------------------
# §5 Determinism and the canonical layout digest. Phase 1 defines the projection, because
#    the digest is what every later replay and regression compares against.
# --------------------------------------------------------------------------------------

LAYOUT_IS_DETERMINISTIC = True

#: The digest and the projection carry their own versions, because `layout_engine_version`
#: and `layout_rules_version` describe the engine that ran, not the shape of what was
#: digested. Without these, adding a projection field or changing how a number is normalized
#: would silently give a *different* digest the *same* name -- the identity failure this whole
#: milestone is about, one layer down.
#:
#: v2: step 3 froze the symbol geometry the engine places against, so a digest produced without
#: it would be a different identity wearing the same name -- the failure mode this milestone is
#: about, one layer down. The projection version does not move: no canonical projection field
#: changed, and only a field change is a projection change.
LAYOUT_DIGEST_VERSION = "m7-layout-digest/2"
LAYOUT_PROJECTION_VERSION = "m7-layout-projection/1"

#: Everything the digest is computed over. The versions are inputs, not decoration: a reader
#: that does not know which projection produced a digest cannot compare two of them.
LAYOUT_DIGEST_INPUTS: tuple[str, ...] = (
    "layout_digest_version",
    "layout_projection_version",
    "diagram_spec_semantic_digest",
    "layout_engine_version",
    "layout_rules_version",
    "symbol_geometry_catalog_digest",
    "canonical_projection_envelope",
    "canonical_placement_projection",
)

#: A change to any of these is a new identity and must bump a version explicitly. "The
#: digest changed but kept its name" is indistinguishable from "the drawing changed".
PROJECTION_CHANGE_REQUIRES_VERSION_BUMP = True
NUMERIC_CANONICALIZATION_CHANGE_REQUIRES_VERSION_BUMP = True
COORDINATE_QUANTUM_CHANGE_REQUIRES_VERSION_BUMP = True
SORT_KEY_CHANGE_REQUIRES_VERSION_BUMP = True
VERSION_BUMP_IS_EXPLICIT_NOT_IMPLIED = True

# ------------------------------------------------------------------------------------
# §5.1 Numerics: equality of floats is not an identity, so the canonicalization is named
#      and the quantum is a declared constant rather than an implied default.
# ------------------------------------------------------------------------------------

LAYOUT_NUMERIC_CANONICALIZATION = "finite_fixed_decimal_v1"
#: Six decimals, matching the rounding the drawing pipeline already uses (`round(..., 6)` in
#: the drafting geometry). Declared here so that it cannot drift with the grid, the canvas or
#: whatever the host happens to print.
LAYOUT_COORDINATE_DECIMALS = 6
LAYOUT_COORDINATE_QUANTUM = 1e-6
#: The quantum is a contract constant: deriving it from `grid_size`, the canvas or the runtime
#: environment would make the digest depend on the document being drawn.
COORDINATE_QUANTUM_IS_DECLARED_NOT_DERIVED = True
NUMERIC_CANONICALIZATION_REJECTS_NON_FINITE = True
NEGATIVE_ZERO_IS_NORMALIZED_TO_ZERO = True
CANONICAL_SERIALIZATION_IS_REPR_INDEPENDENT = True
CANONICAL_SERIALIZATION_IS_LOCALE_INDEPENDENT = True
CANONICAL_SERIALIZATION_IS_TIME_INDEPENDENT = True
EQUAL_CANONICAL_VALUES_PRODUCE_EQUAL_BYTES = True

#: The rules, spelled out, because "canonical" that is not enumerated is a wish.
LAYOUT_NUMERIC_CANONICALIZATION_RULES: tuple[str, ...] = (
    "reject_nan",
    "reject_positive_infinity",
    "reject_negative_infinity",
    "normalize_negative_zero_to_zero",
    "quantize_to_declared_coordinate_quantum",
    "format_without_python_repr",
    "format_without_locale",
    "no_timing_or_environment_input",
    "equal_canonical_values_must_produce_equal_bytes",
)

#: Bookkeeping that must never enter the digest: it differs between two identical runs, so
#: including it would make a correct replay look like a change.
LAYOUT_DIGEST_EXCLUDES_VOLATILE_BOOKKEEPING: tuple[str, ...] = (
    "created_at",
    "duration_ms",
    "iteration_count",
    "provider",
    "model",
    "session_id",
    "plan_id",
    "attempt",
    "layout_run_id",
)


@dataclass(frozen=True)
class CanonicalProjectionField:
    """One field of the canonical placement projection, and whether it is in the digest.

    The projection is declared field by field because "canonical" is otherwise a word rather
    than a definition: a digest is only reproducible if the reader knows exactly what was
    inside it.
    """

    name: str
    included: bool
    note: str


CANONICAL_LAYOUT_PROJECTION_FIELDS: tuple[CanonicalProjectionField, ...] = (
    CanonicalProjectionField(
        "engineering_id",
        True,
        "Stable engineering identity, so a digest is comparable across renames.",
    ),
    CanonicalProjectionField(
        "placement_kind",
        True,
        "Whether the entity is placed as equipment, annotation or routing, so a digest "
        "cannot be equal by accident across different kinds.",
    ),
    CanonicalProjectionField("x", True, "Chosen by the engine."),
    CanonicalProjectionField("y", True, "Chosen by the engine."),
    CanonicalProjectionField("width", True, "Layout output, not model input."),
    CanonicalProjectionField("height", True, "Layout output, not model input."),
    CanonicalProjectionField(
        "ordered_waypoints",
        True,
        "Routing output, in traversal order, which is what makes two routings comparable.",
    ),
    CanonicalProjectionField(
        "duration_ms",
        False,
        "Volatile: two identical layouts differ here, so it would make determinism unprovable.",
    ),
    CanonicalProjectionField(
        "created_at",
        False,
        "Volatile: the run's clock is bookkeeping, not layout.",
    ),
    CanonicalProjectionField(
        "layout_run_id",
        False,
        "Volatile: an identifier of the run, not of its result.",
    ),
)

#: Bounds describe the whole drawing, so they are envelope fields rather than repeated on
#: every row. Repeating them would make an envelope value depend on the accidental order of
#: rows -- a global fact re-derived per entity is a global fact with a race in it.
CANONICAL_PROJECTION_ENVELOPE_FIELDS: tuple[str, ...] = (
    "content_bounds",
    "canvas_bounds",
)
BOUNDS_ARE_ENVELOPE_FIELDS_NOT_ROWS = True

CANONICAL_PROJECTION_IS_SORTED = True
#: A single identity field cannot prove a total order: two different kinds may legitimately
#: carry the same engineering id, and then the "canonical" ordering depends on insertion
#: order. The sort key is therefore composite, and the projection must be unique on it.
CANONICAL_PROJECTION_SORT_KEY: tuple[str, ...] = ("placement_kind", "engineering_id")
CANONICAL_PROJECTION_SORT_KEY_IS_COMPOSITE = True
CANONICAL_PROJECTION_SORT_KEY_IS_UNIQUE = True
DUPLICATE_SORT_KEY_IS_HARD_FAIL = True
#: If one engineering entity ever yields several presentation rows, this is the field that
#: completes the key; named now so the extension is a declared change rather than an
#: accidental duplicate.
CANONICAL_PROJECTION_PRESENTATION_ROLE_FIELD = "presentation_role"
CANONICAL_PROJECTION_IS_TOTAL_ORDERED = True
CANONICAL_PROJECTION_TOTAL_ORDER_IS_PROVEN_BY_UNIQUENESS = True
#: Two layouts are identical when their projections are identical, field for field and in
#: order. Anything weaker would let a real difference hide behind a stable digest.
CANONICAL_PROJECTION_EQUALITY_IS_FIELD_WISE = True


@dataclass(frozen=True)
class DigestVersionContract:
    """What one digest/projection version version *means*.

    This is the amendment's mechanism: the version string is bound to the rules and field set
    it names, so changing the numeric canonicalization, the quantum, the projection fields or
    the sort key without bumping a version is reported instead of silently re-labelling a
    different digest.
    """

    digest_version: str
    projection_version: str
    numeric_canonicalization: str
    coordinate_decimals: int
    projection_field_names: tuple[str, ...]
    envelope_field_names: tuple[str, ...]
    sort_key: tuple[str, ...]


DIGEST_VERSION_CONTRACT = DigestVersionContract(
    digest_version="m7-layout-digest/2",
    projection_version="m7-layout-projection/1",
    numeric_canonicalization="finite_fixed_decimal_v1",
    coordinate_decimals=6,
    projection_field_names=(
        "engineering_id",
        "placement_kind",
        "x",
        "y",
        "width",
        "height",
        "ordered_waypoints",
    ),
    envelope_field_names=("content_bounds", "canvas_bounds"),
    sort_key=("placement_kind", "engineering_id"),
)

# --------------------------------------------------------------------------------------
# §6 Components. The adapter is not a second placer: it converts meaning into the engine's
#    inputs and owns no geometry.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LayoutResponsibility:
    component: str
    owns: tuple[str, ...]
    must_not_own: tuple[str, ...]


LAYOUT_RESPONSIBILITIES: tuple[LayoutResponsibility, ...] = (
    LayoutResponsibility(
        component="DiagramSpecAdapter",
        owns=(
            "spec_to_topology",
            "semantic_identity_preservation",
            "layout_intent_translation",
        ),
        must_not_own=(
            "x",
            "y",
            "absolute_placement",
            "orthogonal_routing",
            "canvas_bounds",
        ),
    ),
    LayoutResponsibility(
        component="AutoLayoutEngine",
        owns=CODE_OWNS,
        must_not_own=("meaning", "topology", "tags", "system_membership"),
    ),
)

LAYOUT_COMPONENTS: tuple[str, ...] = ("DiagramSpecAdapter", "AutoLayoutEngine")

# --------------------------------------------------------------------------------------
# §7 Acceptance fixtures. Four, because determinism and geometry rejection are separate
#    claims and one fixture cannot make both honestly.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LayoutAcceptanceFixture:
    key: str
    name: str
    input_shape: str
    must_observe: tuple[str, ...]
    must_not_observe: tuple[str, ...]


LAYOUT_ACCEPTANCE_FIXTURES: tuple[LayoutAcceptanceFixture, ...] = (
    LayoutAcceptanceFixture(
        key="A",
        name="small_semantically_complete_diagram",
        input_shape="A handful of systems, equipment, connections and one required loop.",
        must_observe=(
            "every declared entity is placed",
            "every declared connection has a routed path",
            "no entity is left at the default origin",
        ),
        must_not_observe=(
            "any coordinate in the specification",
            "any entity placed outside the derived canvas",
        ),
    ),
    LayoutAcceptanceFixture(
        key="B",
        name="multi_system_plant_with_required_loop",
        input_shape=(
            "Many systems and devices, including a loop that must close, at reference plant scale."
        ),
        must_observe=(
            "system grouping is preserved",
            "primary flow direction follows the process order",
            "the required loop's topology is unchanged by layout",
            "the canvas grows to fit the content",
        ),
        must_not_observe=(
            "a dropped or merged connection",
            "a reclassified device",
            "content cropped by the canvas",
        ),
    ),
    LayoutAcceptanceFixture(
        key="C",
        name="forbidden_geometry_input",
        input_shape="A specification that carries coordinates, anchors or canvas pixels.",
        must_observe=(
            "the geometry is rejected",
            "the rejection names the offending field",
        ),
        must_not_observe=(
            "silent acceptance of coordinates",
            "silent stripping of coordinates",
            "a partial layout produced from a rejected specification",
        ),
    ),
    LayoutAcceptanceFixture(
        key="D",
        name="determinism",
        input_shape="The same specification laid out twice in a row.",
        must_observe=(
            "the canonical layout digest is identical",
            "the semantic digest is identical before and after layout",
        ),
        must_not_observe=(
            "any difference in the canonical projection",
            "any volatile bookkeeping field inside the digest",
        ),
    ),
)

# --------------------------------------------------------------------------------------
# §8 Phase boundary. Phase 1 designs; phase 2 integrates. Nothing here is built yet.
# --------------------------------------------------------------------------------------

M7_2_PHASES: tuple[tuple[str, str], ...] = (
    ("M7-2 Phase-1", "Design & Contract"),
    ("M7-2 Phase-2A", "DiagramSpec Adapter"),
    ("M7-2 Phase-2B", "Deterministic Layout Engine Integration"),
)

# ------------------------------------------------------------------------------------
# §8.1 Phase-2A: the adapter, and the line it must stop at. The runtime chain ends before
#      any placement exists, so that "the model has lost geometry authority" is provable
#      independently of whether the engine lays a drawing out well.
# ------------------------------------------------------------------------------------

PHASE_2A_MAY_BUILD: tuple[str, ...] = (
    "diagram_spec_runtime",
    "diagram_spec_adapter",
    "semantic_topology_input",
    "adapter_topology_digest",
)

#: What phase 2A still may not do, including the surfaces phase 1 already forbade.
PHASE_2A_FORBIDDEN: tuple[str, ...] = (
    "absolute_geometry_generation",
    "width_height_generation",
    "waypoint_generation",
    "routing",
    "annotation_placement",
    "canvas_calculation",
    "auto_layout_engine_placement_changes",
    "new_http_route",
    "new_mcp_tool",
    "new_ui_surface",
)

ADAPTER_OUTPUT_CONTAINS_ABSOLUTE_GEOMETRY = False
ADAPTER_OUTPUT_CONTAINS_WAYPOINTS = False
ADAPTER_OUTPUT_CONTAINS_CANVAS = False
ADAPTER_REJECTS_MODEL_GEOMETRY_BEFORE_VALIDATION = True
#: The tempting shortcut: strip the coordinates and carry on. That would make a response
#: which violates the contract look like a success.
ADAPTER_MAY_SILENTLY_STRIP_GEOMETRY = False

#: The adapter preserves these exactly. A digest before and after proves it rather than
#: asserting it.
ADAPTER_MUST_PRESERVE: tuple[str, ...] = (
    "engineering_ids",
    "tags",
    "system_membership",
    "topology",
    "required_loops",
)

#: The translation table, as data: the adapter maps meaning onto the engine's vocabulary and
#: nothing else.
ADAPTER_TRANSLATES: tuple[tuple[str, str], ...] = (
    ("system", "topology_systems"),
    ("equipment", "topology_nodes"),
    ("instrument", "topology_instrument_nodes_and_relations"),
    ("connection", "topology_edges"),
    ("required_loop", "preserved_loop_constraints"),
    ("layout_intent", "discrete_engine_constraints"),
)

ADAPTER_IS_DETERMINISTIC = True
ADAPTER_SEMANTIC_DIGEST_BEFORE_EQUALS_AFTER = True
ADAPTER_TOPOLOGY_DIGEST_IS_NOT_THE_LAYOUT_DIGEST = True
#:
#: v2: the specification gained an explicit `symbol_key`, so the topology carries which
#: catalogue symbol expresses each device. That is layout input, not engineering semantics --
#: which is why the engineering digest is unchanged while this one moves.
ADAPTER_TOPOLOGY_DIGEST_VERSION = "m7-adapter-topology-digest/2"
ADAPTER_TOPOLOGY_DIGEST_INPUTS: tuple[str, ...] = (
    "adapter_topology_digest_version",
    "adapter_topology_projection",
)
#: Sorted on the same composite key the layout projection uses, for the same reason.
ADAPTER_TOPOLOGY_DIGEST_SORT_KEY: tuple[str, ...] = ("kind", "engineering_id")

#: Every key any canonical row may carry. It is the union of the row shapes rather than a
#: subset, so "a row has a field nobody declared" and "a declared field no row carries" are
#: both reportable instead of being hidden behind a permissive superset check.
ADAPTER_TOPOLOGY_PROJECTION_FIELDS: tuple[str, ...] = (
    "kind",
    "engineering_id",
    "system_id",
    "tag",
    "name",
    "equipment_class",
    "instrument_type",
    "measurement",
    "ports",
    "medium",
    "source_engineering_id",
    "target_engineering_id",
    "source_port_id",
    "target_port_id",
    "engineering_ids",
    "orientation",
    "preferred_aspect_class",
    "primary_flow_direction",
    "grouping",
    "density",
    "system_order",
    "symbol_key",
)

#: What the adapter digest version pins, for the same reason the layout digest pins its own
#: field set: adding a row field without a new version would make two different digests share
#: a name.
ADAPTER_TOPOLOGY_DIGEST_VERSION_FIELD_SET: tuple[str, ...] = (
    "kind",
    "engineering_id",
    "system_id",
    "tag",
    "name",
    "equipment_class",
    "instrument_type",
    "measurement",
    "ports",
    "medium",
    "source_engineering_id",
    "target_engineering_id",
    "source_port_id",
    "target_port_id",
    "engineering_ids",
    "orientation",
    "preferred_aspect_class",
    "primary_flow_direction",
    "grouping",
    "density",
    "system_order",
    "symbol_key",
)

#: The contract is a review document that a phase-1 milestone must not import. Once a phase
#: builds the runtime the contract describes, that runtime is the declared importer -- named
#: here so the rule stays a rule rather than becoming an exception list in a test.
PHASE_2A_MAY_IMPORT_THE_CONTRACT: tuple[str, ...] = (
    "m7_diagram_spec.py",
    "m7_diagram_adapter.py",
)

#: What a version check can and cannot do, stated so nobody builds a second layer of
#: machinery to prove something unprovable.
VERSION_CHECK_DETECTS_DRIFT_BETWEEN_LIVE_AND_FROZEN_DEFINITION = True
VERSION_CHECK_CANNOT_PREVENT_A_DELIBERATE_DOUBLE_EDIT = True

#: The clarification the phase-2A gate asked for: the geometry scan is not weakened, but when
#: the specification grows real prose (notes, annotations) the scan must be scoped by field
#: semantics rather than applied to every string. Declared here so that "scan everything" is a
#: *current* state with a named successor rather than a permanent property.
GEOMETRY_SCAN_COVERS_EVERY_STRING_VALUE = True
GEOMETRY_SCAN_IS_FIELD_SCOPED_WHEN_THE_SPEC_CARRIES_PROSE = False
GEOMETRY_SCAN_SCOPE_DEFERRAL = (
    "When DiagramSpec gains free prose fields, scope the anchor scan to layout-instruction "
    "fields and read prose as content. Do not weaken structured geometry rejection."
)

# ------------------------------------------------------------------------------------
# §8.2 Phase-2B: the seam into the one layout authority. SemanticTopology *is* the
#      engine-facing input contract -- no second adapter, no placeholder coordinates, and
#      the model cannot reach `preserve_positions` at all.
# ------------------------------------------------------------------------------------

#: The internal order the engine integration is built in, declared so "which step consumes
#: which intent dimension" is a fact a test can read instead of a claim in a comment.
PHASE_2B_STEPS: tuple[tuple[str, str], ...] = (
    ("step_1", "semantic_topology_ingress_and_intent_resolution"),
    ("step_2", "deterministic_rank_and_absolute_placement"),
    ("step_3", "orthogonal_routing_and_annotation_placement"),
    ("step_4", "content_bounds_to_canvas_bounds_and_aspect_enforcement"),
    ("step_5", "canonical_projection_and_layout_digest"),
)

#: The ingress, by name. Its shape is a method on the existing engine rather than a new
#: component: a second "clever" adapter would recreate two sources of truth for what the
#: layout input is, which is the defect this milestone removes.
ENGINE_SEMANTIC_TOPOLOGY_INGRESS = "layout_semantic_topology"
SEMANTIC_TOPOLOGY_IS_THE_ENGINE_FACING_INPUT_CONTRACT = True
SECOND_ADAPTER_WITH_SEMANTIC_AUTHORITY_IS_ALLOWED = False
LEGACY_INGRESS_RETAINS_THE_SAME_ALGORITHM_AUTHORITY = True
LAYOUT_AUTHORITY_COUNT = 1

#: Inside the engine, converting the topology into whatever graph objects its algorithms
#: already use is allowed -- but as representation conversion only. The three zeros are the
#: whole licence, and they are numbers rather than adjectives on purpose.
INTERNAL_NORMALIZATION_IS_REPRESENTATION_ONLY = True
INTERNAL_NORMALIZATION_SEMANTIC_DECISIONS = 0
INTERNAL_NORMALIZATION_GEOMETRY_DECISIONS = 0
INTERNAL_NORMALIZATION_TOPOLOGY_EDITS = 0

#: The shortcut that would undo the milestone: give every node a placeholder position so the
#: legacy document-shaped entry point can be reused, then let the engine preserve or correct
#: those coordinates. Coordinate authority would move from the model to the adapter, and the
#: original problem would come back wearing a different hat.
ENGINE_INGRESS_MAY_FABRICATE_PLACEHOLDER_POSITIONS = False
ENGINE_INGRESS_MAY_DISGUISE_TOPOLOGY_AS_A_POSITIONED_DOCUMENT = False

#: Path-scoped policy, carried into the runtime: the semantic-first ingress has no
#: `preserve_positions` parameter at all, so "the caller may not override it" is enforced by
#: the signature rather than by an argument check.
M7_SYNTHESIS_INGRESS_PRESERVE_POSITIONS = False
M7_INGRESS_HAS_NO_PRESERVE_POSITIONS_PARAMETER = True
M7_INGRESS_PRESERVE_POSITIONS_IS_CALLER_OVERRIDABLE = False

#: The engine reports which engine and rules version produced a plan, because the digest names
#: both. The contract declares the *names* the engine must publish; the engine owns the values,
#: so a rules change is an engine edit with a visible version, not a contract edit.
ENGINE_INGRESS_REPORTS_ENGINE_AND_RULES_VERSION = True
ENGINE_VERSION_CONSTANT_NAMES: tuple[str, ...] = (
    "LAYOUT_ENGINE_VERSION",
    "LAYOUT_RULES_VERSION",
)


@dataclass(frozen=True)
class IntentConsumption:
    """Where one discrete intent dimension enters the engine and where it changes the drawing.

    Two steps rather than one, because "received" and "applied" are different facts: a
    dimension that is carried into the plan but never applies anywhere is exactly the silent
    ignoring this table exists to make visible.
    """

    dimension: str
    received_at_step: str
    applied_at_step: str
    behaviour: str


#: Every declared intent dimension, with the step that applies it. Coverage is checked by the
#: validator in both directions, so an intent dimension with no consumption point and a
#: consumption point for a dimension that does not exist are both reported.
LAYOUT_INTENT_CONSUMPTION: tuple[IntentConsumption, ...] = (
    IntentConsumption(
        "orientation",
        "step_1",
        "step_4",
        "Landscape or portrait is enforced on the derived canvas, not requested in pixels.",
    ),
    IntentConsumption(
        "preferred_aspect_class",
        "step_1",
        "step_4",
        "The aspect class shapes the derived canvas so `extra_wide` is reachable.",
    ),
    IntentConsumption(
        "primary_flow_direction",
        "step_1",
        "step_2",
        "Ranking follows the process order, so the drawing reads the way the process runs.",
    ),
    IntentConsumption(
        "system_order",
        "step_1",
        "step_2",
        "The system partition follows the declared sequence, resolved against the spec.",
    ),
    IntentConsumption(
        "grouping",
        "step_1",
        "step_2",
        "Grouped or flat placement, so one system's equipment stays together.",
    ),
    IntentConsumption(
        "density",
        "step_1",
        "step_2",
        "Spacing class, used once as separation rather than nudged per element.",
    ),
)

#: Declared and unimplemented may not be the same thing as ignored.
UNIMPLEMENTED_INTENT_DIMENSION_MAY_BE_SILENTLY_IGNORED = False
INTENT_DIMENSIONS_ARE_RECEIVED_AT_STEP = "step_1"

#: The official chain. Canvas is the tail of it, and `canvas_width` / `canvas_height` remain
#: forbidden on the request: the engine may be given intent and a margin policy, never pixels.
CANVAS_DERIVATION_CHAIN: tuple[str, ...] = (
    "semantic_topology",
    "system_partition",
    "absolute_placement",
    "routing",
    "annotations",
    "content_bounds",
    "derive_canvas_bounds_from_content_and_margin_and_intent",
    "verify_no_clipping",
    "canonical_layout_projection",
    "canonical_layout_digest",
)
CANVAS_IS_ENGINE_OUTPUT_DERIVED_FROM_CONTENT = True
CANVAS_MUST_BE_VERIFIED_TO_CLIP_NOTHING = True
ENGINE_MAY_TAKE_INTENT_AND_MARGIN_POLICY = True
ENGINE_MAY_TAKE_CANVAS_DIMENSIONS_AS_INPUT = False

#: Phase 2B builds the ingress and the engine integration. It still adds no surface: nothing
#: routes, no tool, no endpoint, no button calls the new ingress yet.
PHASE_2B_WIRES_THE_INGRESS_TO_ANY_SURFACE = False
#: Step 3 adds one module: the frozen symbol geometry the engine places against. It is named
#: here rather than in a test because the whitelist *is* the rule -- a test that kept its own
#: copy of the list would enforce a rule the contract did not state.
PHASE_2B_MAY_IMPORT_THE_CONTRACT: tuple[str, ...] = (
    "auto_layout_semantic.py",
    "m7_symbol_geometry.py",
)

# ------------------------------------------------------------------------------------
# §8.3 Phase-2B step 2: deterministic partition, rank and absolute placement. The model
#      chooses a density *class*; the numbers it maps to are versioned engine rules.
# ------------------------------------------------------------------------------------

#: The temptation this forbids: letting intent carry numbers. `node_gap = 80` is a layout
#: fact, and a model that can state one has coordinate authority again through the intent.
MODEL_MAY_DECLARE_GAP_NUMBERS = False
DENSITY_SPACING_POLICY_IS_ENGINE_RULES = True
DENSITY_POLICY_CHANGE_REQUIRES_THE_LAYOUT_RULES_VERSION_BUMP = True
#: One rules version, not two: a spacing policy with its own lifecycle is a second source of
#: truth about the same drawing until the rules are big enough to need one.
SEPARATE_SPACING_POLICY_VERSION_ALLOWED_IN_V1 = False
UNKNOWN_DENSITY_IS_A_HARD_FAILURE = True
GROUPING_IS_INTENT_ONLY = True

#: Grouping *classes* mean something to the model; how they become ranks, gaps and in-group
#: order is engine rules.
#:
#: An intent the engine cannot honour is **refused**, not replaced. Substituting the nearest
#: class -- even one that reports itself -- is still substituting a layout the caller explicitly
#: asked not to have. Recognition and executability are different facts: a class may stay in the
#: vocabulary (the protocol knows the intent exists) while being unsupported for execution.
UNSUPPORTED_INTENT_IS_NEVER_SUBSTITUTED = True
UNSUPPORTED_INTENT_PRODUCES_ZERO_PLACEMENT = True
UNSUPPORTED_INTENT_CLASSES_MAY_STAY_IN_THE_VOCABULARY = True


@dataclass(frozen=True)
class UnsupportedLayoutIntent:
    """An intent the protocol recognises and the engine cannot honour, with a named code.

    The code is the point: a caller gets `zone_grouping_requires_zone_membership` rather than
    "unsupported", so the refusal says what is missing instead of only that something is.
    """

    dimension: str
    value: str
    code: str
    reason: str


UNSUPPORTED_LAYOUT_INTENT: tuple[UnsupportedLayoutIntent, ...] = (
    UnsupportedLayoutIntent(
        dimension="grouping",
        value="grouped_by_zone",
        code="zone_grouping_requires_zone_membership",
        reason=(
            "the specification models no zones and no zone membership, so a zone grouping "
            "cannot be honoured; it is refused rather than replaced with system bands"
        ),
    ),
)


@dataclass(frozen=True)
class SupportedLayoutIntentClass:
    """One intent class the engine can honour today, named so "supported" is a list, not a mood."""

    dimension: str
    value: str


SUPPORTED_LAYOUT_INTENT_CLASSES: tuple[SupportedLayoutIntentClass, ...] = (
    SupportedLayoutIntentClass(dimension="grouping", value="grouped_by_system"),
    SupportedLayoutIntentClass(dimension="grouping", value="flat"),
)

#: What a placement row may say it is. Routing and annotations are named now so that "the
#: entity was placed as equipment" stays distinguishable from "the entity was routed through"
#: once later steps exist.
PLACEMENT_KINDS: tuple[str, ...] = ("equipment", "instrument", "annotation", "routing")
STEP_2_PLACEMENT_KINDS: tuple[str, ...] = ("equipment", "instrument")

#: The placement projection is a prefix of the canonical layout projection: the same field
#: names, minus the routing output that step 3 produces and the envelope that step 4 derives.
#: A prefix cannot drift into a second, differently-named projection.
PLACEMENT_PROJECTION_FIELDS: tuple[str, ...] = (
    "placement_kind",
    "engineering_id",
    "x",
    "y",
    "width",
    "height",
)
PLACEMENT_PROJECTION_VERSION = "m7-placement-projection/1"
PLACEMENT_PROJECTION_IS_A_PREFIX_OF_THE_CANONICAL_PROJECTION = True
PLACEMENT_PROJECTION_IS_NOT_THE_CANONICAL_LAYOUT_DIGEST = True

#: Placement is a separate projection *beside* the topology, never a coordinate written back
#: into it. That is what lets the semantic digest be compared before and after placement.
PLACEMENT_NEVER_WRITES_INTO_THE_TOPOLOGY = True
SEMANTIC_DIGEST_IS_UNCHANGED_BY_PLACEMENT = True
NODE_SIZE_IS_DECLARED_BY_ENGINE_RULES_UNTIL_ROUTING = True

PLACEMENT_INVARIANTS: tuple[str, ...] = (
    "every node that needs a placement has exactly one",
    "no placement names an engineering id the topology does not declare",
    "no declared node is left unplaced",
    "placement modifies no node, tag, system membership or edge",
    "the same topology, intent and rules version produce the same placement",
    "no two placed nodes overlap",
    "every coordinate is finite and quantized to the declared coordinate quantum",
)

#: The deferral's trigger, written as a rule rather than as a milestone: what makes field-scoped
#: scanning mandatory is a schema change, not a phase boundary.
GEOMETRY_SCAN_SCOPE_TRIGGER = "the_diagram_specification_gains_a_prose_bearing_field"
FIELD_SCOPED_SCAN_IS_MANDATORY_BEFORE_SUCH_A_SCHEMA_SHIPS = True

PHASE_1_FORBIDDEN_SURFACES: tuple[str, ...] = (
    "new_http_route",
    "new_mcp_tool",
    "new_ui_surface",
    "diagram_spec_runtime",
    "deterministic_layout_runtime",
    "layout_service",
    "canvas_parameter_on_auto_layout_request",
)

#: The layout surface that already exists, declared so that "a new M7-2 surface" and "the
#: legacy layout path" cannot be confused for one another -- the same distinction, in the
#: surface layer, as the preserve_positions split. These two are the human/manual path:
#: phase 1 neither changes them nor adds a third.
PRE_EXISTING_LAYOUT_SURFACES: tuple[str, ...] = (
    "preview_auto_layout",
    "apply_auto_layout",
)

#: Read by the surface test the same way M7 phase 1 reads its own list. `auto_layout` is
#: deliberately absent: it names the pre-existing path, so using it here would report the
#: legacy feature as a violation and hide a genuine one among the false positives.
PHASE_1_FORBIDDEN_SURFACE_TOKENS: tuple[str, ...] = (
    "diagram_spec",
    "diagram-spec",
    "layout_intent",
    "layout-intent",
    "deterministic-layout",
    "deterministic_layout",
)

#: The milestone this contract takes over from the M7 line's deferral list.
SUPERSEDES_DEFERRAL = ("semantic_first_deterministic_layout",)
DEFERRED_TO_PHASE = "M7-2"

#: Phase 1 changes no drawing behaviour. A contract that shipped a behaviour change would not
#: be a contract, and the whole point of this phase is that the change is reviewable first.
PHASE_1_MAY_CHANGE_PRODUCTION_DRAWING_BEHAVIOUR = False

# --------------------------------------------------------------------------------------
# §12 Phase-2B step 3: frozen symbol geometry, routing and annotations. The layout engine is
#      still the only geometry authority; the catalogue supplies *facts* about a symbol and
#      never a decision about where an instance sits.
# --------------------------------------------------------------------------------------

#: The snapshot's own version, separate from the layout digest and from the layout rules. Two
#: drawings that differ because a symbol definition changed and two that differ because the
#: spacing rules changed are different events, and a single version number cannot tell them
#: apart.
SYMBOL_GEOMETRY_CATALOG_DIGEST_VERSION = "m7-symbol-geometry-catalog-digest/1"

#: Every fact a frozen symbol carries, in the order the row is written. The list is closed on
#: purpose: a new fact is a new snapshot version, not an extra key nobody reviews.
SYMBOL_GEOMETRY_FACT_FIELDS: tuple[str, ...] = (
    "symbol_key",
    "renderer_geometry_identity",
    "renderer_supported",
    "intrinsic_width",
    "intrinsic_height",
    "scale_constraint",
    "entity_kinds",
    "ports",
)

#: One port's geometry, as the catalogue states it. Anchors are *normalized* (fractions of the
#: intrinsic size) because the engine may instantiate the symbol larger or smaller: an absolute
#: anchor would only be correct at the catalogue's own size.
SYMBOL_GEOMETRY_PORT_FIELDS: tuple[str, ...] = (
    "port_id",
    "direction",
    "medium",
    "normalized_x",
    "normalized_y",
)

#: 「the symbol itself and where its ports are」 stays on this side of the line.
SYMBOL_CATALOG_OWNS: tuple[str, ...] = (
    "symbol_key",
    "renderer_geometry",
    "intrinsic_bounds",
    "scale_constraint",
    "entity_kind_compatibility",
    "port_identities",
    "port_anchors",
    "port_directions",
)

#: 「this instance, at this size, here, joined to that neighbour」 stays on the engine's side.
LAYOUT_ENGINE_OWNS_FOR_SYMBOLS: tuple[str, ...] = (
    "instance_width",
    "instance_height",
    "instance_x",
    "instance_y",
    "rank",
    "system_band",
    "route",
    "canvas",
)

#: Named so that "the snapshot must not contain x" is a list a validator can check instead of a
#: sentence a reader has to believe.
SYMBOL_GEOMETRY_FACT_EXCLUSIONS: tuple[str, ...] = (
    "x",
    "y",
    "rank",
    "system_band",
    "route",
    "waypoints",
    "canvas",
    "runtime_timings",
    "document_revision",
)
SNAPSHOT_CARRIES_INSTANCE_GEOMETRY = False
SNAPSHOT_CARRIES_LAYOUT_DECISIONS = False

#: The catalogue declares one nominal size per symbol and nothing else, so a "minimum bounds"
#: would have to be invented here. Naming the absence is cheaper than a fabricated minimum: the
#: fact is that the intrinsic size is the whole story until the engine scales it.
SYMBOL_GEOMETRY_HAS_A_SEPARATE_MINIMUM_BOUND = False

#: Scaling. Not a catalogue field today, so the default applies to every symbol -- and a
#: declared value this contract does not recognise is a hard failure rather than a silent
#: fallback, which is the same rule the density class follows.
SYMBOL_SCALE_CONSTRAINT_METADATA_KEY = "scale_constraint"
SYMBOL_SCALE_CONSTRAINT_DEFAULT = "scalable_preserving_aspect"
SYMBOL_SCALE_CONSTRAINTS: tuple[str, ...] = (
    "scalable_preserving_aspect",
    "intrinsic_size_fixed",
)
UNKNOWN_SYMBOL_SCALE_CONSTRAINT_IS_A_HARD_FAILURE = True

#: Where each declared fact comes from in the catalogue. The mapping is data so the validator
#: can check coverage instead of a reader checking prose, and so "which catalogue field is this
#: snapshot fact" is answerable without reading the snapshot code.
#: ``(fact field, catalogue source, the catalogue-ownership entry it discharges)``. The third
#: element is what lets the validator prove that every claimed ownership is actually read, in
#: both directions, without guessing from the field names.
SYMBOL_GEOMETRY_FACT_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("symbol_key", "key", "symbol_key"),
    ("renderer_geometry_identity", "shapes", "renderer_geometry"),
    ("renderer_supported", "shapes", "renderer_geometry"),
    ("intrinsic_width", "width", "intrinsic_bounds"),
    ("intrinsic_height", "height", "intrinsic_bounds"),
    ("scale_constraint", f"metadata.{SYMBOL_SCALE_CONSTRAINT_METADATA_KEY}", "scale_constraint"),
    ("entity_kinds", "category", "entity_kind_compatibility"),
    ("ports", "ports", "port_identities"),
)

SYMBOL_GEOMETRY_PORT_FACT_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("port_id", "ports[].id", "port_identities"),
    ("direction", "ports[].direction", "port_directions"),
    ("medium", "ports[].medium", "port_identities"),
    ("normalized_x", "ports[].x / width", "port_anchors"),
    ("normalized_y", "ports[].y / height", "port_anchors"),
)

#: Mirrors ``SymbolDefinition``: the snapshot may read these and nothing else. A test compares
#: this list against the model's own field names, so the two cannot drift apart quietly.
SYMBOL_CATALOG_SOURCE_FIELDS: tuple[str, ...] = (
    "key",
    "name",
    "category",
    "description",
    "width",
    "height",
    "ports",
    "shapes",
    "metadata",
)
SYMBOL_CATALOG_SOURCE_PORT_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "x",
    "y",
    "direction",
    "medium",
)

#: The closure rule. The digest covers the symbols *this layout depends on*, not the whole
#: catalogue: otherwise adding an unrelated symbol definition would change the identity of a
#: drawing that did not move, and identity churn is indistinguishable from a real change.
SYMBOL_GEOMETRY_DIGEST_INPUT_IS_THE_LAYOUT_CLOSURE = True
SYMBOL_GEOMETRY_CLOSURE_IS_COMPLETE = True
MISSING_SYMBOL_GEOMETRY_IS_A_HARD_FAILURE_BEFORE_ROUTING = True
MISSING_SYMBOL_GEOMETRY_FALLS_BACK_TO_RULE_SIZES = False
SAME_SYMBOL_GEOMETRY_FACTS_PRODUCE_THE_SAME_DIGEST = True
SYMBOL_GEOMETRY_FACT_ORDER_DOES_NOT_CHANGE_THE_DIGEST = True

#: How a node's symbol is chosen: an explicit field, not the engineering class wearing a second
#: hat. The two are different facts that happen to coincide today -- `equipment_class` says what
#: the equipment *is*, `symbol_key` says which catalogue symbol expresses it -- and a future where
#: one class has several legal graphics is exactly the future this separation is for.
SYMBOL_KEY_FIELD = "symbol_key"
SYMBOL_KEY_IS_EXPLICIT = True
ENTITY_SYMBOL_KEY_FALLS_BACK_TO_THE_PLACEMENT_KIND = False
ENTITY_SYMBOL_KEY_FALLS_BACK_TO_THE_ENGINEERING_CLASS = False
SYMBOL_KEY_MUST_EQUAL_THE_ENGINEERING_CLASS = False
SYMBOL_KEY_REQUIREMENTS: tuple[str, ...] = (
    "exists_in_the_frozen_catalogue",
    "renderer_supported",
    "entity_kind_compatible",
)
UNKNOWN_ENTITY_SYMBOL_KEY_IS_A_HARD_FAILURE = True
ENTITY_SYMBOL_KEY_IS_DECLARED_BY_THE_SPECIFICATION = True

#: What "entity kind compatible" means, as data. The catalogue states one category per symbol,
#: and exactly one category holds measuring instruments; an instrument node and an equipment
#: node may not cross that line, because a pressure indicator drawn as a vessel is a wrong
#: drawing even though both are renderable symbols.
SYMBOL_KIND_COMPATIBILITY_SOURCE = "SymbolDefinition.category"
INSTRUMENT_SYMBOL_CATEGORY = "仪表"
EQUIPMENT_SYMBOL_CATEGORIES_ARE_THE_REST = True
SYMBOL_KIND_INCOMPATIBILITY_IS_A_HARD_FAILURE = True

#: Renderer support is a declared property, not a guess: a symbol with no shapes has no geometry
#: to draw, so it can be a catalogue entry without being a placeable one.
SYMBOL_RENDERER_SUPPORT_REQUIRES_DECLARED_SHAPES = True

#: The two digests answer two different questions, and a renderer-only change is the case that
#: proves it. Re-binding a device to an equivalent graphic changes the layout input while the
#: engineering facts stand still, and only a pair of separate digests can say so.
ENGINEERING_SEMANTIC_DIGEST_IS_UNCHANGED_BY_SYMBOL_KEY = True
ADAPTER_TOPOLOGY_IDENTITY_INCLUDES_SYMBOL_KEY = True
SEMANTIC_LAYOUT_PLAN_IDENTITY_INCLUDES_SYMBOL_KEY = True

#: Step 3 materializes real geometry and may therefore have to place the drawing again. The
#: step-2 result is the initial solution, not a fact that the real sizes must fit into.
STEP_2_PLACEMENT_IS_THE_INITIAL_RANK_SOLUTION = True
MATERIALIZED_GEOMETRY_MAY_TRIGGER_DETERMINISTIC_REFLOW = True
REFLOW_IS_DETERMINISTIC = True
REFLOW_MAY_CHANGE_ENGINEERING_SEMANTICS = False
PLACEMENT_MUST_BE_REVALIDATED_AGAINST_MATERIALIZED_GEOMETRY = True

#: Routing. Every semantic connection is routed exactly once; routing adds waypoints and removes
#: nothing, because a drawing that quietly loses a connection is a drawing of something else.
ROUTING_RULES: tuple[str, ...] = (
    "every semantic connection produces exactly one routed connection projection",
    "routing may add waypoints and may not add or delete semantic connections",
    "route endpoints are derived from the frozen symbol port geometry",
    "every waypoint is finite and quantized to the declared coordinate quantum",
    "the same plan, snapshot and rules produce the same route",
    "a route never crosses the bounds of a node it does not serve",
)
ROUTING_MAY_ADD_WAYPOINTS = True
ROUTING_MAY_ADD_OR_DELETE_SEMANTIC_CONNECTIONS = False
ROUTE_ENDPOINTS_COME_FROM_FROZEN_PORT_GEOMETRY = True
ROUTING_IS_ORTHOGONAL_ONLY = True
PROTECTED_NODE_BOUNDS_ARE_OBSTACLES = True
UNROUTABLE_EDGE_IS_A_HARD_DIAGNOSTIC = True
UNROUTABLE_EDGE_FALLS_BACK_TO_A_STRAIGHT_LINE_THROUGH_EQUIPMENT = False
DEGRADED_ROUTING_POLICY_IS_DEFINED = False

#: Port binding. An explicit port is checked against the frozen geometry and its direction; an
#: omitted port may be inferred only when exactly one candidate exists. "Sort the candidates and
#: take the first" is rejected on purpose: that is a deterministic arbitrary choice, not an
#: engineering derivation, and on a three-way valve it would quietly pick one of three.
CONNECTION_PORT_IDS_MUST_EXIST_ON_THE_RESOLVED_SYMBOL = True
EXPLICIT_PORT_BINDING_MUST_BE_DIRECTION_COMPATIBLE = True
OMITTED_PORT_ID_IS_INFERRED_ONLY_WHEN_THE_CANDIDATE_IS_UNIQUE = True
OMITTED_PORT_ID_WITH_NO_CANDIDATE_IS_A_HARD_FAILURE = True
OMITTED_PORT_ID_WITH_SEVERAL_CANDIDATES_IS_A_HARD_FAILURE = True
SORTED_PORT_IDS_ARE_NOT_AN_INFERENCE_RULE = True
CATALOGUE_DEFAULT_PORT_IS_NOT_INVENTED_IN_V1 = True
PORT_ROLE_COMPATIBLE_DIRECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("source", ("out", "bidirectional")),
    ("target", ("in", "bidirectional")),
)
PORT_BINDING_RESOLUTIONS: tuple[str, ...] = ("explicit", "inferred_unique")
PORT_BINDING_CODES: tuple[str, ...] = (
    "port_not_found",
    "port_direction_mismatch",
    "no_compatible_port",
    "ambiguous_port_binding",
)
PORT_BINDING_IS_RESOLVED_BEFORE_ROUTING = True
ROUTING_CONSUMES_RESOLVED_BINDINGS_ONLY = True
ROUTING_MAY_RE_GUESS_A_PORT = False

#: Annotations. The text extent is a pure function of the text and its font size -- no browser
#: metrics, no installed fonts, no canvas measurement -- so reusing it keeps the canonical
#: layout digest free of the machine it ran on. The rule is *read* from the annotation layout
#: module rather than restated as a second formula here, and it belongs to the layout *rules*
#: axis: a change to the extent algorithm is a rules change, not a third identity axis.
ANNOTATION_TEXT_EXTENT_IS_DETERMINISTIC = True
ANNOTATION_TEXT_EXTENT_RULE_SOURCE = "agentcad.annotation_layout.text_bounds"
ANNOTATION_TEXT_EXTENT_CHARACTER_FACTOR = 0.6
ANNOTATION_TEXT_METRICS_POLICY = "deterministic_codepoint_extent_v1"
ANNOTATION_TEXT_METRICS_CHANGE_REQUIRES_LAYOUT_RULES_VERSION_BUMP = True
TEXT_METRICS_IS_NOT_A_SEPARATE_DIGEST_INPUT = True
ANNOTATION_PLACEMENT_READS_BROWSER_FONT_METRICS = False
ANNOTATION_PLACEMENT_READS_OS_FONTS = False
ANNOTATION_PLACEMENT_USES_CANVAS_MEASURE_TEXT = False
ANNOTATION_PLACEMENT_IS_PART_OF_THE_LAYOUT_IDENTITY = True

#: Fixed cases rather than a hash of the function: what must not change is the extent the
#: algorithm produces, not the bytes of the source that produced it.
ANNOTATION_TEXT_METRICS_GOLDEN_CASES: tuple[tuple[str, float, float], ...] = (
    ("V-101", 12.0, 36.0),
    ("PT-101", 12.0, 43.2),
    ("x", 14.0, 14.0),
)


# --------------------------------------------------------------------------------------
# The validator. Each temptation below is reported, and each has a mutation test in the
# companion test module.
# --------------------------------------------------------------------------------------


def iter_layout_intent_dimensions() -> tuple[LayoutIntentDimension, ...]:
    return LAYOUT_INTENT_DIMENSIONS


def layout_intent_dimension(name: str) -> LayoutIntentDimension:
    for dimension in LAYOUT_INTENT_DIMENSIONS:
        if dimension.name == name:
            return dimension
    raise KeyError(f"no layout intent dimension named {name!r}")


def iteration_superseded_deferrals() -> tuple[str, ...]:
    return SUPERSEDES_DEFERRAL


def validate_contract() -> list[str]:
    """Return the list of contract violations (empty means the contract is coherent)."""

    problems: list[str] = []

    # §1 the split, and the two prohibitions it exists to enforce.
    if MODEL_OUTPUT_MAY_CONTAIN_ABSOLUTE_GEOMETRY:
        problems.append("the model's output must not contain absolute geometry")
    if DIAGRAM_SPEC_CARRIES_ABSOLUTE_COORDINATES:
        problems.append("the diagram specification must not carry absolute coordinates")
    if MODEL_OUTPUT_MAY_CONTAIN_RELATIVE_ANCHORS:
        problems.append("the model's output must not contain relative anchors either")
    if not LAYOUT_ENGINE_IS_SINGLE_AUTHORITY:
        problems.append("there must be exactly one layout authority")
    for name in ("absolute_placement", "orthogonal_routing", "canvas_bounds"):
        if name not in CODE_OWNS:
            problems.append(f"code must own {name!r}")
    for name in ("meaning", "connections", "required_loops"):
        if name not in MODEL_OWNS:
            problems.append(f"the model must own {name!r}")
    if set(MODEL_OWNS) & set(CODE_OWNS):
        problems.append("model and code must not both own the same thing")
    if not FORBIDDEN_MODEL_GEOMETRY_FIELDS:
        problems.append("the geometry fields the model may not emit must be named")
    for guard in ("position", "waypoints", "canvas_width"):
        if guard not in FORBIDDEN_MODEL_GEOMETRY_FIELDS:
            problems.append(f"{guard!r} must be among the forbidden model geometry fields")

    # §2 preserve_positions is scoped by path, not deleted.
    if M7_SYNTHESIS_USES_PRESERVE_POSITIONS:
        problems.append("the M7 synthesis path must not preserve model-supplied positions")
    if not LEGACY_MANUAL_LAYOUT_MAY_PRESERVE_POSITIONS:
        problems.append(
            "preserve_positions must remain available to the human-edited and manual paths"
        )
    if not LEGACY_PRESERVE_POSITIONS_PATHS:
        problems.append("the paths that may preserve positions must be named")

    # §3 canvas is derived.
    if not CANVAS_IS_LAYOUT_OUTPUT:
        problems.append("the canvas must be a layout output rather than a request parameter")
    if AUTO_LAYOUT_REQUEST_MAY_TAKE_CANVAS_PIXELS:
        problems.append("the layout request must not accept canvas pixels")
    for parameter in ("canvas_width", "canvas_height"):
        if parameter not in AUTO_LAYOUT_REQUEST_FORBIDDEN_PARAMETERS:
            problems.append(f"the layout request must not accept {parameter!r}")
    if set(AUTO_LAYOUT_REQUEST_DISCRETE_CONSTRAINTS) & set(FORBIDDEN_MODEL_GEOMETRY_FIELDS):
        problems.append("a discrete layout constraint must not be a geometry field")
    for produced in ("content_bounds", "canvas_bounds", "canonical_layout_digest"):
        if produced not in AUTO_LAYOUT_OUTPUT_ADDS:
            problems.append(f"layout output must add {produced!r}")
    if not CANVAS_GROWS_TO_FIT_CONTENT:
        problems.append("the canvas must grow to fit the content")
    if not CANVAS_MARGIN_IS_DECLARED_NOT_ASSUMED:
        problems.append("the canvas margin must be declared rather than assumed")
    if not ASPECT_CLASS_SURVIVES_GROWTH:
        problems.append("the preferred aspect class must survive canvas growth")

    # §4 layout intent is discrete.
    names = [dimension.name for dimension in LAYOUT_INTENT_DIMENSIONS]
    expected = (
        "orientation",
        "preferred_aspect_class",
        "primary_flow_direction",
        "system_order",
        "grouping",
        "density",
    )
    if tuple(names) != expected:
        problems.append(f"the layout intent dimensions must be exactly {expected!r}")
    if len(names) != len(set(names)):
        problems.append("layout intent dimensions must have distinct names")
    for dimension in LAYOUT_INTENT_DIMENSIONS:
        if not dimension.values and not dimension.open_ended:
            problems.append(
                f"layout intent dimension {dimension.name!r} has no values and is not open"
            )
        if dimension.open_ended and dimension.values:
            problems.append(f"layout intent dimension {dimension.name!r} is open and fixed at once")
    if FREE_RELATIVE_ANCHORS_ALLOWED_IN_V1:
        problems.append("free relative anchors must not be allowed in v1")
    if not FORBIDDEN_RELATIVE_ANCHOR_PREDICATES:
        problems.append("the relative anchor predicates v1 forbids must be named")
    for predicate in ("left_of", "right_of", "above", "below", "near"):
        if predicate not in FORBIDDEN_RELATIVE_ANCHOR_PREDICATES:
            problems.append(f"{predicate!r} must be among the forbidden relative anchors")
    if not FUTURE_SEMANTIC_ADJACENCY_CONSTRAINTS:
        problems.append("the semantic adjacency vocabulary must be named for the future")
    if set(FUTURE_SEMANTIC_ADJACENCY_CONSTRAINTS) & set(FORBIDDEN_RELATIVE_ANCHOR_PREDICATES):
        problems.append("a semantic adjacency constraint must not be a positional predicate")
    for dimension in LAYOUT_INTENT_DIMENSIONS:
        for value in dimension.values:
            if value in FORBIDDEN_RELATIVE_ANCHOR_PREDICATES:
                problems.append(
                    f"layout intent dimension {dimension.name!r} accepts the forbidden "
                    f"predicate {value!r}"
                )

    # §5 layout must not change meaning.
    for flag, message in (
        (LAYOUT_MAY_CHANGE_TOPOLOGY, "layout must not change connectivity"),
        (
            LAYOUT_MAY_CREATE_OR_DELETE_ENGINEERING_EQUIPMENT,
            "layout must not create or delete engineering equipment",
        ),
        (LAYOUT_MAY_CHANGE_TAGS, "layout must not change tags"),
        (LAYOUT_MAY_CHANGE_SYSTEM_MEMBERSHIP, "layout must not change system membership"),
    ):
        if flag:
            problems.append(message)
    if not SEMANTIC_DIGEST_BEFORE_LAYOUT_MUST_EQUAL_SEMANTIC_DIGEST_AFTER:
        problems.append("the semantic digest must be unchanged by layout")
    if not LAYOUT_MAY_CHANGE_PRESENTATION_ONLY:
        problems.append("what layout may change must be named as presentation only")
    for noun in ENGINEERING_SEMANTIC_NOUNS:
        if noun in LAYOUT_MAY_CHANGE_PRESENTATION_ONLY:
            problems.append(f"layout must not list {noun!r} as presentation")

    # §6 determinism and the canonical projection.
    if not LAYOUT_IS_DETERMINISTIC:
        problems.append("layout must be deterministic")
    for digest_input in (
        "layout_digest_version",
        "layout_projection_version",
        "diagram_spec_semantic_digest",
        "layout_engine_version",
        "layout_rules_version",
        "canonical_projection_envelope",
        "canonical_placement_projection",
    ):
        if digest_input not in LAYOUT_DIGEST_INPUTS:
            problems.append(f"the layout digest must be computed from {digest_input!r}")
    for version in ("layout_digest_version", "layout_projection_version"):
        if version not in LAYOUT_DIGEST_INPUTS:
            problems.append(f"the digest envelope must carry {version!r}")
    if not LAYOUT_DIGEST_VERSION or not LAYOUT_PROJECTION_VERSION:
        problems.append(
            "the digest and the projection must have their own versions: the engine version "
            "describes the engine, not the shape of what was digested"
        )
    overlap = set(LAYOUT_DIGEST_INPUTS) & set(LAYOUT_DIGEST_EXCLUDES_VOLATILE_BOOKKEEPING)
    if overlap:
        problems.append(f"volatile bookkeeping must not enter the layout digest: {sorted(overlap)}")
    included = [field.name for field in CANONICAL_LAYOUT_PROJECTION_FIELDS if field.included]
    excluded = [field.name for field in CANONICAL_LAYOUT_PROJECTION_FIELDS if not field.included]
    if not included or not excluded:
        problems.append("the canonical projection must include and exclude something explicitly")
    for required in ("engineering_id", "placement_kind", "x", "y", "ordered_waypoints"):
        if required not in included:
            problems.append(f"the canonical projection must include {required!r}")
    for volatile in ("created_at", "duration_ms", "layout_run_id"):
        if volatile not in excluded:
            problems.append(f"the canonical projection must exclude the volatile {volatile!r}")
    if set(excluded) - set(LAYOUT_DIGEST_EXCLUDES_VOLATILE_BOOKKEEPING):
        problems.append("a field excluded from the projection must be named as volatile")

    # §6.1 bounds are global, so they live on the envelope rather than on every row.
    if not CANONICAL_PROJECTION_ENVELOPE_FIELDS:
        problems.append("the projection envelope must declare the whole-drawing fields")
    for envelope_field in CANONICAL_PROJECTION_ENVELOPE_FIELDS:
        if envelope_field in included:
            problems.append(
                f"{envelope_field!r} describes the whole drawing and must not be repeated "
                "on every row"
            )
        if envelope_field not in AUTO_LAYOUT_OUTPUT_ADDS:
            problems.append(f"the envelope field {envelope_field!r} must be a layout output")
    if not BOUNDS_ARE_ENVELOPE_FIELDS_NOT_ROWS:
        problems.append("bounds must be envelope fields rather than repeated rows")

    # §6.2 the total order has to be provable, not asserted.
    if not CANONICAL_PROJECTION_IS_SORTED:
        problems.append("the canonical projection must be sorted")
    if isinstance(CANONICAL_PROJECTION_SORT_KEY, str):
        # A bare string is iterable, so it would silently become a list of characters and the
        # order would look composite while proving nothing.
        problems.append(
            "the sort key must be a tuple of fields, not a single field name; a single "
            "identity field cannot prove a total order"
        )
    elif len(CANONICAL_PROJECTION_SORT_KEY) < 2:
        problems.append(
            "a single identity field cannot prove a total order; the sort key must be "
            "composite (placement_kind, engineering_id[, presentation_role])"
        )
    if not CANONICAL_PROJECTION_SORT_KEY_IS_COMPOSITE:
        problems.append("the sort key must be declared composite")
    for key_field in CANONICAL_PROJECTION_SORT_KEY:
        if key_field not in included:
            problems.append(f"the sort key field {key_field!r} must be part of the projection")
    if not CANONICAL_PROJECTION_SORT_KEY_IS_UNIQUE:
        problems.append("the sort key must be unique within the projection")
    if not DUPLICATE_SORT_KEY_IS_HARD_FAIL:
        problems.append("a duplicate canonical sort key must be a hard failure")
    if not CANONICAL_PROJECTION_IS_TOTAL_ORDERED:
        problems.append("the canonical projection must be totally ordered")
    if not CANONICAL_PROJECTION_TOTAL_ORDER_IS_PROVEN_BY_UNIQUENESS:
        problems.append(
            "the total order must be proven by sort-key uniqueness rather than declared"
        )
    if not CANONICAL_PROJECTION_EQUALITY_IS_FIELD_WISE:
        problems.append("canonical equality must be field-wise, not a summary")

    # §6.3 numerics: float equality is not an identity.
    if not LAYOUT_NUMERIC_CANONICALIZATION:
        problems.append("the numeric canonicalization must be named")
    for rule in (
        "reject_nan",
        "reject_positive_infinity",
        "reject_negative_infinity",
        "normalize_negative_zero_to_zero",
        "quantize_to_declared_coordinate_quantum",
        "format_without_python_repr",
        "format_without_locale",
        "equal_canonical_values_must_produce_equal_bytes",
    ):
        if rule not in LAYOUT_NUMERIC_CANONICALIZATION_RULES:
            problems.append(f"the numeric canonicalization must declare {rule!r}")
    if not NUMERIC_CANONICALIZATION_REJECTS_NON_FINITE:
        problems.append("NaN and the infinities must be rejected, not ordered")
    if not NEGATIVE_ZERO_IS_NORMALIZED_TO_ZERO:
        problems.append("negative zero must be normalized, or one placement has two digests")
    for flag, message in (
        (CANONICAL_SERIALIZATION_IS_REPR_INDEPENDENT, "must not depend on Python repr"),
        (CANONICAL_SERIALIZATION_IS_LOCALE_INDEPENDENT, "must not depend on the locale"),
        (CANONICAL_SERIALIZATION_IS_TIME_INDEPENDENT, "must not depend on the run's clock"),
        (
            EQUAL_CANONICAL_VALUES_PRODUCE_EQUAL_BYTES,
            "must give equal canonical values equal bytes",
        ),
    ):
        if not flag:
            problems.append(f"the canonical serialization {message}")
    if LAYOUT_COORDINATE_DECIMALS <= 0:
        problems.append("the coordinate decimals must be a positive constant")
    if LAYOUT_COORDINATE_QUANTUM != 10.0 ** (-LAYOUT_COORDINATE_DECIMALS):
        problems.append("the coordinate quantum must be exactly the declared decimals")
    if not COORDINATE_QUANTUM_IS_DECLARED_NOT_DERIVED:
        problems.append(
            "the coordinate quantum must be a contract constant, not derived from the grid, "
            "the canvas or the environment"
        )

    # §6.4 the version is bound to the rules it names: a change without a bump is a
    #      different digest wearing the same name.
    if not VERSION_BUMP_IS_EXPLICIT_NOT_IMPLIED:
        problems.append("a version bump must be explicit")
    for flag, message in (
        (PROJECTION_CHANGE_REQUIRES_VERSION_BUMP, "the projection fields"),
        (NUMERIC_CANONICALIZATION_CHANGE_REQUIRES_VERSION_BUMP, "the numeric canonicalization"),
        (COORDINATE_QUANTUM_CHANGE_REQUIRES_VERSION_BUMP, "the coordinate quantum"),
        (SORT_KEY_CHANGE_REQUIRES_VERSION_BUMP, "the sort key"),
    ):
        if not flag:
            problems.append(f"changing {message} must require a version bump")
    pinned = DIGEST_VERSION_CONTRACT
    if pinned.digest_version != LAYOUT_DIGEST_VERSION:
        problems.append(
            f"{pinned.digest_version!r} describes {LAYOUT_DIGEST_VERSION!r}; changing it needs "
            "a new digest version"
        )
    if pinned.projection_version != LAYOUT_PROJECTION_VERSION:
        problems.append(
            f"{pinned.projection_version!r} describes {LAYOUT_PROJECTION_VERSION!r}; changing "
            "it needs a new projection version"
        )
    if pinned.numeric_canonicalization != LAYOUT_NUMERIC_CANONICALIZATION:
        problems.append(
            f"{pinned.digest_version!r} pins numeric canonicalization "
            f"{pinned.numeric_canonicalization!r}, but the contract declares "
            f"{LAYOUT_NUMERIC_CANONICALIZATION!r}: that is a new digest version, not a "
            "silent redefinition"
        )
    if pinned.coordinate_decimals != LAYOUT_COORDINATE_DECIMALS:
        problems.append(
            f"{pinned.projection_version!r} pins {pinned.coordinate_decimals} coordinate "
            f"decimals, but the contract declares {LAYOUT_COORDINATE_DECIMALS}: changing the "
            "quantum needs a new projection and digest version"
        )
    if tuple(pinned.projection_field_names) != tuple(included):
        problems.append(
            f"{pinned.projection_version!r} pins the field set "
            f"{tuple(pinned.projection_field_names)}; the projection now declares "
            f"{tuple(included)}: adding or removing a field needs a new projection version"
        )
    if tuple(pinned.envelope_field_names) != tuple(CANONICAL_PROJECTION_ENVELOPE_FIELDS):
        problems.append(
            f"{pinned.projection_version!r} pins the envelope "
            f"{tuple(pinned.envelope_field_names)}; the contract declares "
            f"{tuple(CANONICAL_PROJECTION_ENVELOPE_FIELDS)}: that needs a new projection version"
        )
    if tuple(pinned.sort_key) != tuple(CANONICAL_PROJECTION_SORT_KEY):
        problems.append(
            f"{pinned.projection_version!r} pins the sort key {tuple(pinned.sort_key)}; the "
            f"contract declares {tuple(CANONICAL_PROJECTION_SORT_KEY)}: that needs a new "
            "projection version"
        )

    # §7 components: the adapter must not become a second placer.
    components = [entry.component for entry in LAYOUT_RESPONSIBILITIES]
    if tuple(components) != LAYOUT_COMPONENTS:
        problems.append(f"the layout components must be exactly {LAYOUT_COMPONENTS!r}")
    adapter = [
        entry for entry in LAYOUT_RESPONSIBILITIES if entry.component == "DiagramSpecAdapter"
    ]
    if len(adapter) == 1:
        for forbidden in ("x", "y", "absolute_placement", "orthogonal_routing", "canvas_bounds"):
            if forbidden not in adapter[0].must_not_own:
                problems.append(f"the adapter must not own {forbidden!r}")
    engine = [entry for entry in LAYOUT_RESPONSIBILITIES if entry.component == "AutoLayoutEngine"]
    if len(engine) == 1:
        if set(engine[0].owns) != set(CODE_OWNS):
            problems.append("the engine owns exactly what code owns")
        for not_engine in ("meaning", "topology", "tags"):
            if not_engine not in engine[0].must_not_own:
                problems.append(f"the engine must not own {not_engine!r}")
    for entry in LAYOUT_RESPONSIBILITIES:
        if set(entry.owns) & set(entry.must_not_own):
            problems.append(f"{entry.component} owns and must not own the same thing")

    # §8 fixtures: four, and a fixture that forbids nothing is not a fixture.
    keys = [fixture.key for fixture in LAYOUT_ACCEPTANCE_FIXTURES]
    if keys != ["A", "B", "C", "D"]:
        problems.append("there must be exactly four layout fixtures, A through D")
    for fixture in LAYOUT_ACCEPTANCE_FIXTURES:
        if not fixture.must_observe:
            problems.append(f"layout fixture {fixture.key} must declare what it must observe")
        if not fixture.must_not_observe:
            problems.append(f"layout fixture {fixture.key} must declare what it must not observe")
    by_key = {fixture.key: fixture for fixture in LAYOUT_ACCEPTANCE_FIXTURES}
    if "C" in by_key:
        geometry = " ".join(FORBIDDEN_MODEL_GEOMETRY_FIELDS[:3])
        joined = " ".join(by_key["C"].must_not_observe).lower()
        if not any(word in joined for word in ("coordinate", "geometry")):
            problems.append(
                "fixture C must forbid accepting geometry, not just describe a rejection "
                f"(geometry examples: {geometry})"
            )
    if "D" in by_key:
        joined = " ".join(by_key["D"].must_observe).lower()
        if "digest" not in joined:
            problems.append("fixture D must observe digest equality")

    # §9 phase boundary.
    for surface in (
        "diagram_spec_runtime",
        "deterministic_layout_runtime",
        "layout_service",
        "canvas_parameter_on_auto_layout_request",
    ):
        if surface not in PHASE_1_FORBIDDEN_SURFACES:
            problems.append(f"phase 1 must not build {surface!r}")
    for surface in ("new_http_route", "new_mcp_tool", "new_ui_surface"):
        if surface not in PHASE_1_FORBIDDEN_SURFACES:
            problems.append(f"phase 1 must not add a {surface!r}")
    if PHASE_1_MAY_CHANGE_PRODUCTION_DRAWING_BEHAVIOUR:
        problems.append("phase 1 is design and contract only; it changes no drawing behaviour")
    if tuple(name for name, _ in M7_2_PHASES) != (
        "M7-2 Phase-1",
        "M7-2 Phase-2A",
        "M7-2 Phase-2B",
    ):
        problems.append("M7-2 must declare its phases in order")
    if not VERSION_CHECK_DETECTS_DRIFT_BETWEEN_LIVE_AND_FROZEN_DEFINITION:
        problems.append("the version check must compare the live declaration to the frozen one")
    if not VERSION_CHECK_CANNOT_PREVENT_A_DELIBERATE_DOUBLE_EDIT:
        problems.append(
            "a version rule cannot prevent a deliberate double edit; claiming otherwise "
            "would invite machinery that proves nothing"
        )

    # §8.1 phase 2A: the adapter, and the geometry it must never emit.
    for built in (
        "diagram_spec_runtime",
        "diagram_spec_adapter",
        "semantic_topology_input",
        "adapter_topology_digest",
    ):
        if built not in PHASE_2A_MAY_BUILD:
            problems.append(f"phase 2A must build {built!r}")
    for forbidden in (
        "absolute_geometry_generation",
        "width_height_generation",
        "waypoint_generation",
        "routing",
        "annotation_placement",
        "canvas_calculation",
        "auto_layout_engine_placement_changes",
        "new_http_route",
        "new_mcp_tool",
        "new_ui_surface",
    ):
        if forbidden not in PHASE_2A_FORBIDDEN:
            problems.append(f"phase 2A must not do {forbidden!r}")
    if set(PHASE_2A_MAY_BUILD) & set(PHASE_2A_FORBIDDEN):
        problems.append("phase 2A may not both build and forbid the same thing")
    if ADAPTER_OUTPUT_CONTAINS_ABSOLUTE_GEOMETRY:
        problems.append("the adapter's output must not contain absolute geometry")
    if ADAPTER_OUTPUT_CONTAINS_WAYPOINTS:
        problems.append("the adapter must not produce waypoints; routing is phase 2B")
    if ADAPTER_OUTPUT_CONTAINS_CANVAS:
        problems.append("the adapter must not compute a canvas; bounds are layout output")
    if not ADAPTER_REJECTS_MODEL_GEOMETRY_BEFORE_VALIDATION:
        problems.append("geometry must be rejected before the specification is validated")
    if ADAPTER_MAY_SILENTLY_STRIP_GEOMETRY:
        problems.append(
            "geometry must be refused, not stripped: a stripped violation looks like success"
        )
    for preserved in (
        "engineering_ids",
        "tags",
        "system_membership",
        "topology",
        "required_loops",
    ):
        if preserved not in ADAPTER_MUST_PRESERVE:
            problems.append(f"the adapter must preserve {preserved!r} exactly")
    translated_from = [source for source, _ in ADAPTER_TRANSLATES]
    if translated_from != list(DIAGRAM_SPEC_DECLARES):
        problems.append(
            "every declared specification input must be translated exactly once, found "
            f"{translated_from!r}"
        )
    if not ADAPTER_IS_DETERMINISTIC:
        problems.append("the adapter must be deterministic")
    if not ADAPTER_SEMANTIC_DIGEST_BEFORE_EQUALS_AFTER:
        problems.append("the adapter must not change the semantic digest")
    if not ADAPTER_TOPOLOGY_DIGEST_IS_NOT_THE_LAYOUT_DIGEST:
        problems.append(
            "the adapter digest must stay distinct from the layout digest, or a later "
            "difference cannot be attributed to the adapter or the engine"
        )
    if not ADAPTER_TOPOLOGY_DIGEST_VERSION:
        problems.append("the adapter topology digest must be versioned")
    if ADAPTER_TOPOLOGY_DIGEST_VERSION in LAYOUT_DIGEST_INPUTS:
        problems.append("the adapter digest is not an input of the layout digest")
    if len(ADAPTER_TOPOLOGY_DIGEST_SORT_KEY) < 2:
        problems.append("the adapter projection needs a composite sort key to be canonical")
    for key_field in ADAPTER_TOPOLOGY_DIGEST_SORT_KEY:
        if key_field not in ADAPTER_TOPOLOGY_PROJECTION_FIELDS:
            problems.append(f"the adapter sort key field {key_field!r} must be in its projection")
    if tuple(ADAPTER_TOPOLOGY_DIGEST_VERSION_FIELD_SET) != tuple(
        ADAPTER_TOPOLOGY_PROJECTION_FIELDS
    ):
        problems.append(
            f"{ADAPTER_TOPOLOGY_DIGEST_VERSION!r} pins the adapter field set "
            f"{tuple(ADAPTER_TOPOLOGY_DIGEST_VERSION_FIELD_SET)}; the contract declares "
            f"{tuple(ADAPTER_TOPOLOGY_PROJECTION_FIELDS)}: changing it needs a new adapter "
            "topology digest version"
        )
    geometry_in_adapter_projection = set(ADAPTER_TOPOLOGY_PROJECTION_FIELDS) & set(
        FORBIDDEN_MODEL_GEOMETRY_FIELDS
    )
    if geometry_in_adapter_projection:
        problems.append(
            "the adapter projection must not contain geometry fields: "
            f"{sorted(geometry_in_adapter_projection)}"
        )
    for intent_dimension in (dimension.name for dimension in LAYOUT_INTENT_DIMENSIONS):
        if intent_dimension not in ADAPTER_TOPOLOGY_PROJECTION_FIELDS:
            problems.append(
                f"the adapter must carry the discrete intent dimension {intent_dimension!r}"
            )
    if not PHASE_2A_MAY_IMPORT_THE_CONTRACT:
        problems.append("the modules a phase may import the contract from must be named")
    if not SUPERSEDES_DEFERRAL:
        problems.append("the M7 deferral this milestone takes over must be named")
    if DEFERRED_TO_PHASE != "M7-2":
        problems.append("the deferral this contract supersedes belongs to M7-2")
    if not PRE_EXISTING_LAYOUT_SURFACES:
        problems.append("the pre-existing layout surface must be named so it is not mistaken")

    # §8.2 phase 2B: the seam, and the two shortcuts it exists to forbid.
    if ENGINE_INGRESS_MAY_FABRICATE_PLACEHOLDER_POSITIONS:
        problems.append(
            "the semantic ingress must not fabricate placeholder positions to reuse the "
            "document-shaped entry point"
        )
    if ENGINE_INGRESS_MAY_DISGUISE_TOPOLOGY_AS_A_POSITIONED_DOCUMENT:
        problems.append("the topology must reach the engine as topology")
    if SECOND_ADAPTER_WITH_SEMANTIC_AUTHORITY_IS_ALLOWED:
        problems.append("there must not be a second adapter with semantic authority")
    if not SEMANTIC_TOPOLOGY_IS_THE_ENGINE_FACING_INPUT_CONTRACT:
        problems.append("the topology is the engine-facing input contract")
    if LAYOUT_AUTHORITY_COUNT != 1:
        problems.append(f"there must be exactly one layout authority, found {LAYOUT_AUTHORITY_COUNT}")
    for counter_name in (
        "INTERNAL_NORMALIZATION_SEMANTIC_DECISIONS",
        "INTERNAL_NORMALIZATION_GEOMETRY_DECISIONS",
        "INTERNAL_NORMALIZATION_TOPOLOGY_EDITS",
    ):
        if globals()[counter_name] != 0:
            problems.append(
                f"internal graph normalization is representation conversion only, but "
                f"{counter_name} is {globals()[counter_name]}"
            )
    if M7_SYNTHESIS_INGRESS_PRESERVE_POSITIONS:
        problems.append("the semantic-first ingress must not preserve positions")
    if M7_INGRESS_PRESERVE_POSITIONS_IS_CALLER_OVERRIDABLE:
        problems.append(
            "the semantic-first ingress must have no preserve_positions parameter for a "
            "caller to override"
        )
    if not M7_INGRESS_HAS_NO_PRESERVE_POSITIONS_PARAMETER:
        problems.append("the semantic-first ingress must not accept preserve_positions")
    if ENGINE_MAY_TAKE_CANVAS_DIMENSIONS_AS_INPUT:
        problems.append("canvas dimensions are an engine output, not an input")
    if not CANVAS_IS_ENGINE_OUTPUT_DERIVED_FROM_CONTENT:
        problems.append("the canvas must be derived from the content")
    if not CANVAS_MUST_BE_VERIFIED_TO_CLIP_NOTHING:
        problems.append("a derived canvas that clips content is not an acceptable canvas")
    if PHASE_2B_WIRES_THE_INGRESS_TO_ANY_SURFACE:
        problems.append("phase 2B adds no surface: the ingress is not wired to a route or tool")
    if set(PHASE_2A_MAY_IMPORT_THE_CONTRACT) & set(PHASE_2B_MAY_IMPORT_THE_CONTRACT):
        problems.append(
            "a module may be declared the importer for one phase, not for two: "
            f"{sorted(set(PHASE_2A_MAY_IMPORT_THE_CONTRACT) & set(PHASE_2B_MAY_IMPORT_THE_CONTRACT))}"
        )
    step_keys = tuple(key for key, _ in PHASE_2B_STEPS)
    if not PHASE_2B_STEPS:
        problems.append("the engine integration must declare its steps")
    for consumption in LAYOUT_INTENT_CONSUMPTION:
        if consumption.received_at_step != INTENT_DIMENSIONS_ARE_RECEIVED_AT_STEP:
            problems.append(
                f"intent dimension {consumption.dimension!r} is not received at the ingress"
            )
        for step in (consumption.received_at_step, consumption.applied_at_step):
            if step not in step_keys:
                problems.append(
                    f"intent dimension {consumption.dimension!r} names an undeclared step {step!r}"
                )
        if not consumption.behaviour.strip():
            problems.append(
                f"intent dimension {consumption.dimension!r} must say what applying it does"
            )
    declared_dimensions = tuple(dimension.name for dimension in LAYOUT_INTENT_DIMENSIONS)
    consumed_dimensions = tuple(item.dimension for item in LAYOUT_INTENT_CONSUMPTION)
    if sorted(consumed_dimensions) != sorted(declared_dimensions):
        missing = sorted(set(declared_dimensions) - set(consumed_dimensions))
        extra = sorted(set(consumed_dimensions) - set(declared_dimensions))
        problems.append(
            "every declared layout intent dimension needs exactly one consumption point "
            f"(missing={missing}, unexpected={extra})"
        )
    if len(set(consumed_dimensions)) != len(consumed_dimensions):
        problems.append("a layout intent dimension may have only one consumption point")
    for dimension in declared_dimensions:
        if dimension == "preserve_positions":
            problems.append(
                "preserve_positions is execution-path policy, not a dimension the model may "
                "declare in layout intent"
            )
    if UNIMPLEMENTED_INTENT_DIMENSION_MAY_BE_SILENTLY_IGNORED:
        problems.append("a declared intent dimension may not be silently ignored")
    if not ENGINE_INGRESS_REPORTS_ENGINE_AND_RULES_VERSION:
        problems.append("the ingress must report which engine and rules version ran")
    if set(ENGINE_VERSION_CONSTANT_NAMES) == {"layout_engine_version", "layout_rules_version"}:
        problems.append(
            "the engine version constants must be the published constant names, not the "
            "digest field names that read them"
        )
    for constant_name in ENGINE_VERSION_CONSTANT_NAMES:
        if not constant_name.isupper():
            problems.append(
                f"the engine version constant {constant_name!r} must be a published constant"
            )
        if constant_name.lower() not in LAYOUT_DIGEST_INPUTS:
            problems.append(
                f"{constant_name!r} must be readable from the digest as "
                f"{constant_name.lower()!r}"
            )
    for field_name in ("layout_engine_version", "layout_rules_version"):
        if field_name not in LAYOUT_DIGEST_INPUTS:
            problems.append(f"the digest must include {field_name!r}")
    if CANVAS_DERIVATION_CHAIN[0] != "semantic_topology":
        problems.append("the canvas chain must start at the semantic topology")
    if CANVAS_DERIVATION_CHAIN[-1] != "canonical_layout_digest":
        problems.append("the canvas chain must end at the canonical layout digest")
    if "routing" not in CANVAS_DERIVATION_CHAIN[:-1]:
        problems.append("routing must precede the canvas derivation")
    canvas_bounds_step = "derive_canvas_bounds_from_content_and_margin_and_intent"
    for required_step in ("content_bounds", canvas_bounds_step):
        if required_step not in CANVAS_DERIVATION_CHAIN:
            problems.append(f"the canvas chain must include {required_step!r}")
    # Positional rules are only checked when both names are present: a validator that raises
    # on a broken declaration reports nothing at all.
    if (
        "content_bounds" in CANVAS_DERIVATION_CHAIN
        and canvas_bounds_step in CANVAS_DERIVATION_CHAIN
        and CANVAS_DERIVATION_CHAIN.index("content_bounds")
        > CANVAS_DERIVATION_CHAIN.index(canvas_bounds_step)
    ):
        problems.append("the canvas must be derived from the content bounds, not before them")
    if not GEOMETRY_SCAN_SCOPE_DEFERRAL.strip():
        problems.append("the anchor scan scope change must name its successor")
    if not PHASE_2B_MAY_IMPORT_THE_CONTRACT:
        problems.append("phase 2B must name the module that will read this contract")

    # §8.3 step 2: intent stays discrete, and placement stays a projection beside the topology.
    if MODEL_MAY_DECLARE_GAP_NUMBERS:
        problems.append("layout intent carries classes; a gap number is a layout fact")
    if not DENSITY_SPACING_POLICY_IS_ENGINE_RULES:
        problems.append("the density class is mapped to numbers by versioned engine rules")
    if not DENSITY_POLICY_CHANGE_REQUIRES_THE_LAYOUT_RULES_VERSION_BUMP:
        problems.append("changing the density policy must bump the layout rules version")
    if SEPARATE_SPACING_POLICY_VERSION_ALLOWED_IN_V1:
        problems.append("one rules version in v1: a second policy version is a second truth")
    if not UNKNOWN_DENSITY_IS_A_HARD_FAILURE:
        problems.append("an unknown density class must fail rather than default")
    if not GROUPING_IS_INTENT_ONLY:
        problems.append("grouping is intent; how it becomes ranks and gaps is engine rules")
    if not UNSUPPORTED_INTENT_IS_NEVER_SUBSTITUTED:
        problems.append("an intent the engine cannot honour must be refused, not replaced")
    if not UNSUPPORTED_INTENT_PRODUCES_ZERO_PLACEMENT:
        problems.append("a refused intent must produce no placement at all")
    for entry in UNSUPPORTED_LAYOUT_INTENT:
        if entry.dimension not in {d.name for d in LAYOUT_INTENT_DIMENSIONS}:
            problems.append(
                f"the unsupported intent {entry.value!r} names an undeclared dimension "
                f"{entry.dimension!r}"
            )
            continue
        vocabulary = set(layout_intent_dimension(entry.dimension).values)
        if entry.value not in vocabulary:
            problems.append(
                f"the unsupported intent {entry.value!r} is not in the {entry.dimension!r} "
                "vocabulary: an unrecognised value is a parse failure, not a capability gap"
            )
        if not entry.code.strip() or entry.code != entry.code.lower():
            problems.append(f"the unsupported intent {entry.value!r} needs a lowercase code")
        if not entry.reason.strip():
            problems.append(f"the unsupported intent {entry.value!r} must say what is missing")
    supported = {(item.dimension, item.value) for item in SUPPORTED_LAYOUT_INTENT_CLASSES}
    unsupported = {(item.dimension, item.value) for item in UNSUPPORTED_LAYOUT_INTENT}
    if supported & unsupported:
        problems.append(
            f"an intent class cannot be both supported and unsupported: "
            f"{sorted(supported & unsupported)}"
        )
    grouping_vocabulary = set(layout_intent_dimension("grouping").values)
    declared_grouping = {value for dimension, value in supported if dimension == "grouping"}
    grouped_grouping = grouping_vocabulary - {value for _, value in unsupported}
    if declared_grouping != grouped_grouping:
        problems.append(
            "every grouping class must be declared either supported or unsupported "
            f"(missing={sorted(grouped_grouping - declared_grouping)}, "
            f"unexpected={sorted(declared_grouping - grouped_grouping)})"
        )
    if not declared_grouping:
        problems.append("at least one grouping class must be executable")
    canonical_included = tuple(
        field.name for field in CANONICAL_LAYOUT_PROJECTION_FIELDS if field.included
    )
    for field_name in PLACEMENT_PROJECTION_FIELDS:
        if field_name not in canonical_included:
            problems.append(
                f"the placement projection field {field_name!r} is not a canonical layout "
                "projection field: the projection is a prefix, not a second vocabulary"
            )
    for required_field in CANONICAL_PROJECTION_SORT_KEY:
        if required_field not in PLACEMENT_PROJECTION_FIELDS:
            problems.append(
                f"the placement projection must carry the canonical sort key field "
                f"{required_field!r}"
            )
    if "ordered_waypoints" in PLACEMENT_PROJECTION_FIELDS:
        problems.append("routing output belongs to step 3, not to the placement projection")
    if not PLACEMENT_NEVER_WRITES_INTO_THE_TOPOLOGY:
        problems.append("placement must not write coordinates back into the topology")
    if not SEMANTIC_DIGEST_IS_UNCHANGED_BY_PLACEMENT:
        problems.append("placement changes presentation, never engineering semantics")
    if not PLACEMENT_PROJECTION_IS_A_PREFIX_OF_THE_CANONICAL_PROJECTION:
        problems.append("the placement projection must be a prefix of the canonical projection")
    if not PLACEMENT_INVARIANTS:
        problems.append("the placement invariants must be declared")
    for kind in STEP_2_PLACEMENT_KINDS:
        if kind not in PLACEMENT_KINDS:
            problems.append(f"the step-2 placement kind {kind!r} is not a declared placement kind")
    for kind in ("annotation", "routing"):
        if kind in STEP_2_PLACEMENT_KINDS:
            problems.append(
                f"{kind!r} rows are produced by a later step than the placement step"
            )
    if CANONICAL_PROJECTION_SORT_KEY[0] != "placement_kind":
        problems.append("the placement projection must be sorted on the placement kind first")
    if not NODE_SIZE_IS_DECLARED_BY_ENGINE_RULES_UNTIL_ROUTING:
        problems.append(
            "node sizes come from versioned engine rules until routing needs real bounds"
        )
    if not GEOMETRY_SCAN_SCOPE_TRIGGER:
        problems.append("the geometry scan deferral must name its trigger")
    if not FIELD_SCOPED_SCAN_IS_MANDATORY_BEFORE_SUCH_A_SCHEMA_SHIPS:
        problems.append(
            "a prose-bearing schema may not ship before the scan becomes field-scoped"
        )
    for token in PHASE_1_FORBIDDEN_SURFACE_TOKENS:
        clashing = [name for name in PRE_EXISTING_LAYOUT_SURFACES if token in name]
        if clashing:
            problems.append(
                f"the phase-1 violation token {token!r} also matches the pre-existing layout "
                f"surface {clashing}: it would report the legacy path instead of a new one"
            )

    # §12 step 3: the snapshot is facts, the engine stays the only geometry authority.
    if not SYMBOL_GEOMETRY_CATALOG_DIGEST_VERSION:
        problems.append("the symbol geometry snapshot must be versioned")
    if SYMBOL_GEOMETRY_CATALOG_DIGEST_VERSION in LAYOUT_DIGEST_INPUTS:
        problems.append("the snapshot's own version is not an input of the layout digest")
    if SYMBOL_GEOMETRY_CATALOG_DIGEST_VERSION == LAYOUT_DIGEST_VERSION:
        problems.append(
            "the snapshot digest and the layout digest must not share a version string: "
            "'a symbol changed' and 'the layout changed' are different events"
        )
    if "symbol_geometry_catalog_digest" not in LAYOUT_DIGEST_INPUTS:
        problems.append(
            "the layout digest must include the symbol geometry catalog digest: the engine no "
            "longer owns the only input to placement"
        )
    catalog_owned = set(SYMBOL_CATALOG_OWNS)
    if not SYMBOL_GEOMETRY_FACT_FIELDS:
        problems.append("the snapshot must declare its facts")
    sources = (*SYMBOL_GEOMETRY_FACT_SOURCES, *SYMBOL_GEOMETRY_PORT_FACT_SOURCES)
    sourced_facts = {fact for fact, _, _ in sources}
    for fact_field in (*SYMBOL_GEOMETRY_FACT_FIELDS, *SYMBOL_GEOMETRY_PORT_FIELDS):
        if fact_field not in sourced_facts:
            problems.append(
                f"the declared snapshot fact {fact_field!r} names no catalogue source: a fact "
                "nobody reads from anywhere is a fact nobody checks"
            )
    for fact, source, owner in sources:
        if fact not in SYMBOL_GEOMETRY_FACT_FIELDS + SYMBOL_GEOMETRY_PORT_FIELDS:
            problems.append(f"the snapshot fact source {fact!r} is not a declared fact field")
        root = source.split(".", 1)[0].split("[", 1)[0]
        if root not in SYMBOL_CATALOG_SOURCE_FIELDS:
            problems.append(
                f"the snapshot reads {source!r} from the catalogue, which does not declare the "
                f"field {root!r}"
            )
        if owner not in catalog_owned:
            problems.append(
                f"the snapshot fact {fact!r} discharges {owner!r}, which the catalogue does "
                "not claim to own"
            )
    discharged = {owner for _, _, owner in sources}
    for owned in sorted(catalog_owned - discharged):
        problems.append(
            f"the catalogue claims to own {owned!r}, which no declared snapshot fact reads: "
            "an ownership nobody exercises is a comment, not a boundary"
        )
    for port_source in (source for _, source, _ in SYMBOL_GEOMETRY_PORT_FACT_SOURCES):
        leaf = port_source.split("[]")[-1].lstrip(".").split()[0]
        if leaf not in SYMBOL_CATALOG_SOURCE_PORT_FIELDS:
            problems.append(
                f"the snapshot reads {port_source!r} from a symbol port, which does not declare "
                f"the field {leaf!r}"
            )
    if not SYMBOL_CATALOG_OWNS or not LAYOUT_ENGINE_OWNS_FOR_SYMBOLS:
        problems.append("the symbol/layout boundary must name both sides")
    if catalog_owned & set(LAYOUT_ENGINE_OWNS_FOR_SYMBOLS):
        problems.append(
            "a fact cannot belong to the catalogue and to the engine: "
            f"{sorted(catalog_owned & set(LAYOUT_ENGINE_OWNS_FOR_SYMBOLS))}"
        )
    declared_exclusions = set(SYMBOL_GEOMETRY_FACT_EXCLUSIONS)
    overlap = declared_exclusions & set(SYMBOL_GEOMETRY_FACT_FIELDS)
    if overlap:
        problems.append(
            f"a field cannot be both a declared fact and an excluded one: {sorted(overlap)}"
        )
    instance_geometry = declared_exclusions & set(LAYOUT_ENGINE_OWNS_FOR_SYMBOLS)
    if len(instance_geometry) < 4:
        problems.append(
            "the snapshot must exclude instance geometry (x, y, rank, canvas): a snapshot that "
            "carries a position is a second placement authority"
        )
    if SNAPSHOT_CARRIES_INSTANCE_GEOMETRY:
        problems.append("the snapshot carries facts about symbols, not about instances")
    if SNAPSHOT_CARRIES_LAYOUT_DECISIONS:
        problems.append("the snapshot carries facts; every layout decision stays with the engine")
    if SYMBOL_GEOMETRY_HAS_A_SEPARATE_MINIMUM_BOUND:
        problems.append(
            "the catalogue declares one nominal size: a separate minimum bound would be invented"
        )
    if SYMBOL_SCALE_CONSTRAINT_DEFAULT not in SYMBOL_SCALE_CONSTRAINTS:
        problems.append("the default scale constraint must be one of the declared constraints")
    if not UNKNOWN_SYMBOL_SCALE_CONSTRAINT_IS_A_HARD_FAILURE:
        problems.append("an unrecognised scale constraint must fail rather than default")
    if not SYMBOL_GEOMETRY_DIGEST_INPUT_IS_THE_LAYOUT_CLOSURE:
        problems.append(
            "the snapshot digest covers the layout's closure, not the whole catalogue: "
            "otherwise an unrelated symbol edit changes a drawing that did not move"
        )
    if not SYMBOL_GEOMETRY_CLOSURE_IS_COMPLETE:
        problems.append("the closure must be complete: every node that needs geometry is in it")
    if not MISSING_SYMBOL_GEOMETRY_IS_A_HARD_FAILURE_BEFORE_ROUTING:
        problems.append("missing symbol geometry must fail before routing")
    if MISSING_SYMBOL_GEOMETRY_FALLS_BACK_TO_RULE_SIZES:
        problems.append("missing symbol geometry must not fall back to the rule sizes")
    if not SYMBOL_KEY_IS_EXPLICIT:
        problems.append(
            "the symbol key is an explicit field: the engineering class says what the equipment "
            "is, the symbol key says how it is drawn, and they may diverge"
        )
    if ENTITY_SYMBOL_KEY_FALLS_BACK_TO_THE_PLACEMENT_KIND:
        problems.append("a node's symbol is not its placement kind")
    if ENTITY_SYMBOL_KEY_FALLS_BACK_TO_THE_ENGINEERING_CLASS:
        problems.append("a node's symbol may not be inferred from its engineering class")
    if SYMBOL_KEY_MUST_EQUAL_THE_ENGINEERING_CLASS:
        problems.append(
            "requiring symbol_key == equipment_class would forbid two graphics for one class"
        )
    if SYMBOL_KEY_FIELD not in ADAPTER_TOPOLOGY_PROJECTION_FIELDS:
        problems.append(
            f"the adapter identity must carry {SYMBOL_KEY_FIELD!r}: a renderer-only re-binding "
            "changes the layout input"
        )
    if not ENGINEERING_SEMANTIC_DIGEST_IS_UNCHANGED_BY_SYMBOL_KEY:
        problems.append("a renderer-only symbol change must not move the engineering semantics")
    for requirement in SYMBOL_KEY_REQUIREMENTS:
        if requirement != requirement.lower():
            problems.append(f"the symbol requirement {requirement!r} must be a lowercase code")
    if not SYMBOL_RENDERER_SUPPORT_REQUIRES_DECLARED_SHAPES:
        problems.append("a symbol without shapes has no renderer geometry to place")
    if SYMBOL_KIND_INCOMPATIBILITY_IS_A_HARD_FAILURE is False:
        problems.append("an instrument drawn as a vessel is a wrong drawing, not a warning")
    if not INSTRUMENT_SYMBOL_CATEGORY.strip():
        problems.append("the instrument category must name the catalogue category it means")
    if not STEP_2_PLACEMENT_IS_THE_INITIAL_RANK_SOLUTION:
        problems.append("the step-2 placement is the initial solution, not a fixed frame")
    if not PLACEMENT_MUST_BE_REVALIDATED_AGAINST_MATERIALIZED_GEOMETRY:
        problems.append("real geometry must be validated against the placement it lands in")
    if not MATERIALIZED_GEOMETRY_MAY_TRIGGER_DETERMINISTIC_REFLOW:
        problems.append("when real sizes break the placement, a deterministic reflow is needed")
    if not REFLOW_IS_DETERMINISTIC:
        problems.append("a reflow that is not deterministic is a new drawing every run")
    if REFLOW_MAY_CHANGE_ENGINEERING_SEMANTICS:
        problems.append("a reflow changes positions, never engineering semantics")
    if not ROUTING_RULES:
        problems.append("the routing rules must be declared")
    if not ROUTING_MAY_ADD_WAYPOINTS:
        problems.append("routing adds waypoints by definition")
    if ROUTING_MAY_ADD_OR_DELETE_SEMANTIC_CONNECTIONS:
        problems.append("routing may not add or delete a semantic connection")
    if not ROUTE_ENDPOINTS_COME_FROM_FROZEN_PORT_GEOMETRY:
        problems.append("route endpoints come from the frozen port geometry")
    if not ROUTING_IS_ORTHOGONAL_ONLY:
        problems.append("routing is orthogonal: a diagonal is a drawing nobody asked for")
    if not PROTECTED_NODE_BOUNDS_ARE_OBSTACLES:
        problems.append("a route may not cross the bounds of a node it does not serve")
    if not UNROUTABLE_EDGE_IS_A_HARD_DIAGNOSTIC:
        problems.append("an unroutable edge must be reported, not drawn as a straight line")
    if UNROUTABLE_EDGE_FALLS_BACK_TO_A_STRAIGHT_LINE_THROUGH_EQUIPMENT:
        problems.append("a straight line through equipment is not a fallback, it is a defect")
    if DEGRADED_ROUTING_POLICY_IS_DEFINED and not UNROUTABLE_EDGE_IS_A_HARD_DIAGNOSTIC:
        problems.append("a degraded routing policy may not replace the hard diagnostic")
    if not CONNECTION_PORT_IDS_MUST_EXIST_ON_THE_RESOLVED_SYMBOL:
        problems.append("a port the symbol does not define is a wrong reference")
    if not EXPLICIT_PORT_BINDING_MUST_BE_DIRECTION_COMPATIBLE:
        problems.append("an explicit port still has to point the way the connection runs")
    if not OMITTED_PORT_ID_IS_INFERRED_ONLY_WHEN_THE_CANDIDATE_IS_UNIQUE:
        problems.append(
            "an omitted port may be inferred only when exactly one candidate exists"
        )
    if not OMITTED_PORT_ID_WITH_NO_CANDIDATE_IS_A_HARD_FAILURE:
        problems.append("an omitted port with no candidate must fail")
    if not OMITTED_PORT_ID_WITH_SEVERAL_CANDIDATES_IS_A_HARD_FAILURE:
        problems.append(
            "an omitted port with several candidates must fail: choosing one would be an "
            "arbitrary decision dressed as a derivation"
        )
    if not SORTED_PORT_IDS_ARE_NOT_AN_INFERENCE_RULE:
        problems.append(
            "sorting the candidates and taking the first is not an engineering derivation"
        )
    if not CATALOGUE_DEFAULT_PORT_IS_NOT_INVENTED_IN_V1:
        problems.append("v1 does not invent a catalogue default port")
    if not PORT_BINDING_IS_RESOLVED_BEFORE_ROUTING:
        problems.append("endpoint bindings are resolved before routing, not during it")
    if not ROUTING_CONSUMES_RESOLVED_BINDINGS_ONLY:
        problems.append("routing consumes resolved bindings and never guesses a port again")
    if ROUTING_MAY_RE_GUESS_A_PORT:
        problems.append("routing may not re-guess a port the resolution step already resolved")
    declared_port_directions = {"in", "out", "bidirectional", "none"}
    role_names = [role for role, _ in PORT_ROLE_COMPATIBLE_DIRECTIONS]
    if sorted(role_names) != ["source", "target"]:
        problems.append(
            f"the port compatibility table must cover exactly both endpoints, found {role_names}"
        )
    for role, directions in PORT_ROLE_COMPATIBLE_DIRECTIONS:
        if not directions:
            problems.append(f"the {role} endpoint must accept at least one port direction")
        for direction in directions:
            if direction not in declared_port_directions:
                problems.append(
                    f"the {role} rule names {direction!r}, which is not a declared port direction"
                )
        if "bidirectional" not in directions:
            problems.append(
                f"the {role} rule must accept a bidirectional port: a symbol that can flow both "
                "ways is not thereby unusable"
            )
    if sorted(PORT_BINDING_RESOLUTIONS) != ["explicit", "inferred_unique"]:
        problems.append(
            f"port bindings are explicit or uniquely inferred, found {list(PORT_BINDING_RESOLUTIONS)}"
        )
    if not PORT_BINDING_CODES:
        problems.append("the port binding failures must be named")
    for code in PORT_BINDING_CODES:
        if code != code.lower():
            problems.append(f"the port binding code {code!r} must be lowercase")
    if not ANNOTATION_TEXT_EXTENT_IS_DETERMINISTIC:
        problems.append(
            "annotation text extent must be deterministic, or the layout digest depends on "
            "the machine"
        )
    if not ANNOTATION_TEXT_METRICS_POLICY.strip():
        problems.append("the text extent algorithm must be named as a policy")
    if not ANNOTATION_TEXT_METRICS_CHANGE_REQUIRES_LAYOUT_RULES_VERSION_BUMP:
        problems.append(
            "a change to the extent algorithm belongs to the layout rules version, or nobody "
            "can say which drawing changed and why"
        )
    if not TEXT_METRICS_IS_NOT_A_SEPARATE_DIGEST_INPUT:
        problems.append("text metrics ride the layout rules version, not a third identity axis")
    metric_inputs = [
        name
        for name in LAYOUT_DIGEST_INPUTS
        if "text" in name or "metric" in name or "font" in name
    ]
    if metric_inputs:
        problems.append(
            f"text metrics must not become a layout digest input on their own: {metric_inputs}"
        )
    if not ANNOTATION_TEXT_METRICS_GOLDEN_CASES:
        problems.append("the extent algorithm needs fixed cases, not a hash of its source")
    if not any(
        len(text) * font_size * ANNOTATION_TEXT_EXTENT_CHARACTER_FACTOR < font_size
        for text, font_size, _ in ANNOTATION_TEXT_METRICS_GOLDEN_CASES
    ):
        problems.append(
            "the golden cases must include a short label, where the font size floor decides the "
            "extent rather than the character count"
        )
    for flag, sentence in (
        (
            ANNOTATION_PLACEMENT_READS_BROWSER_FONT_METRICS,
            "annotation placement may not read browser font metrics",
        ),
        (ANNOTATION_PLACEMENT_READS_OS_FONTS, "annotation placement may not read installed fonts"),
        (
            ANNOTATION_PLACEMENT_USES_CANVAS_MEASURE_TEXT,
            "annotation placement may not measure text on a canvas",
        ),
    ):
        if flag:
            problems.append(sentence)
    if not ANNOTATION_TEXT_EXTENT_RULE_SOURCE.strip():
        problems.append("the text extent rule must name the function that implements it")
    if ANNOTATION_TEXT_EXTENT_CHARACTER_FACTOR <= 0:
        problems.append("the declared text extent factor must be positive")

    return problems


if __name__ == "__main__":  # pragma: no cover - manual review entry point
    violations = validate_contract()
    if violations:
        for violation in violations:
            print(f"CONTRACT VIOLATION: {violation}")
        raise SystemExit(1)
    print(f"{M7_LAYOUT_CONTRACT_VERSION}: contract coherent")
