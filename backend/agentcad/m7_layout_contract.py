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
LAYOUT_DIGEST_INPUTS: tuple[str, ...] = (
    "diagram_spec_semantic_digest",
    "layout_engine_version",
    "layout_rules_version",
    "canonical_placement_projection",
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
    CanonicalProjectionField("content_bounds", True, "Extent of the content."),
    CanonicalProjectionField("canvas_bounds", True, "Extent of the drawing surface."),
    CanonicalProjectionField(
        "duration_ms",
        False,
        "Volatile: two identical layouts differ here, so it would make determinism "
        "unprovable.",
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

CANONICAL_PROJECTION_IS_SORTED = True
CANONICAL_PROJECTION_SORT_KEY = "engineering_id"
CANONICAL_PROJECTION_IS_TOTAL_ORDERED = True
#: Two layouts are identical when their projections are identical, field for field and in
#: order. Anything weaker would let a real difference hide behind a stable digest.
CANONICAL_PROJECTION_EQUALITY_IS_FIELD_WISE = True

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
            "Many systems and devices, including a loop that must close, at reference "
            "plant scale."
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
    ("M7-2 Phase-2", "Runtime Integration"),
)

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
            problems.append(
                f"layout intent dimension {dimension.name!r} is open and fixed at once"
            )
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
        "diagram_spec_semantic_digest",
        "layout_engine_version",
        "layout_rules_version",
        "canonical_placement_projection",
    ):
        if digest_input not in LAYOUT_DIGEST_INPUTS:
            problems.append(f"the layout digest must be computed from {digest_input!r}")
    overlap = set(LAYOUT_DIGEST_INPUTS) & set(LAYOUT_DIGEST_EXCLUDES_VOLATILE_BOOKKEEPING)
    if overlap:
        problems.append(
            f"volatile bookkeeping must not enter the layout digest: {sorted(overlap)}"
        )
    included = [field.name for field in CANONICAL_LAYOUT_PROJECTION_FIELDS if field.included]
    excluded = [field.name for field in CANONICAL_LAYOUT_PROJECTION_FIELDS if not field.included]
    if not included or not excluded:
        problems.append("the canonical projection must include and exclude something explicitly")
    for required in ("engineering_id", "x", "y", "ordered_waypoints", "canvas_bounds"):
        if required not in included:
            problems.append(f"the canonical projection must include {required!r}")
    for volatile in ("created_at", "duration_ms", "layout_run_id"):
        if volatile not in excluded:
            problems.append(f"the canonical projection must exclude the volatile {volatile!r}")
    if set(excluded) - set(LAYOUT_DIGEST_EXCLUDES_VOLATILE_BOOKKEEPING):
        problems.append("a field excluded from the projection must be named as volatile")
    if not CANONICAL_PROJECTION_IS_SORTED:
        problems.append("the canonical projection must be sorted")
    if CANONICAL_PROJECTION_SORT_KEY not in included:
        problems.append("the canonical projection must sort on an included field")
    if not CANONICAL_PROJECTION_IS_TOTAL_ORDERED:
        problems.append("the canonical projection must be totally ordered")
    if not CANONICAL_PROJECTION_EQUALITY_IS_FIELD_WISE:
        problems.append("canonical equality must be field-wise, not a summary")

    # §7 components: the adapter must not become a second placer.
    components = [entry.component for entry in LAYOUT_RESPONSIBILITIES]
    if tuple(components) != LAYOUT_COMPONENTS:
        problems.append(f"the layout components must be exactly {LAYOUT_COMPONENTS!r}")
    adapter = [entry for entry in LAYOUT_RESPONSIBILITIES if entry.component == "DiagramSpecAdapter"]
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
    if tuple(name for name, _ in M7_2_PHASES) != ("M7-2 Phase-1", "M7-2 Phase-2"):
        problems.append("M7-2 must declare its two phases in order")
    if not SUPERSEDES_DEFERRAL:
        problems.append("the M7 deferral this milestone takes over must be named")
    if DEFERRED_TO_PHASE != "M7-2":
        problems.append("the deferral this contract supersedes belongs to M7-2")
    if not PRE_EXISTING_LAYOUT_SURFACES:
        problems.append("the pre-existing layout surface must be named so it is not mistaken")
    for token in PHASE_1_FORBIDDEN_SURFACE_TOKENS:
        clashing = [name for name in PRE_EXISTING_LAYOUT_SURFACES if token in name]
        if clashing:
            problems.append(
                f"the phase-1 violation token {token!r} also matches the pre-existing layout "
                f"surface {clashing}: it would report the legacy path instead of a new one"
            )

    return problems


if __name__ == "__main__":  # pragma: no cover - manual review entry point
    violations = validate_contract()
    if violations:
        for violation in violations:
            print(f"CONTRACT VIOLATION: {violation}")
        raise SystemExit(1)
    print(f"{M7_LAYOUT_CONTRACT_VERSION}: contract coherent")
