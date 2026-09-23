"""M7-2 phase 2A: the diagram specification the model is allowed to produce.

The specification is the model's whole output in the semantic-first design: what exists, what
connects to what, which loops must close, and how the drawing should read. It has no
coordinates, and the reason that is enforceable rather than aspirational is
:func:`reject_geometry`, which walks the *raw payload* before validation and refuses any
geometry-shaped key by name.

Two details are deliberate:

* **Rejection rather than stripping.** Dropping the coordinates and carrying on would turn a
  response that violates the contract into a success, which is the same failure this project
  keeps hitting: the artefact looks fine and the violation is gone from view. A specification
  that carries geometry is refused, and the refusal names the field.
* **The scan happens on the wire form, not the parsed model.** A model field list can only
  reject keys it knows about; scanning the raw mapping also catches geometry nested inside an
  entity, inside a loop, or inside metadata added by a future field.

The models themselves stay plain: ``StrictModel`` already refuses unknown fields and
non-finite numbers, so the scan and the model are two nets around the same rule rather than
two implementations of it.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import ConfigDict, Field

from .m7_layout_contract import FORBIDDEN_MODEL_GEOMETRY_FIELDS
from .models import StrictModel

#:
#: v2: every entity names the catalogue symbol that expresses it. The engineering class says
#: *what* the device is; the symbol key says *how it is drawn*, and those are two facts that
#: happen to coincide today. Keeping them separate is what lets one class have several legal
#: graphics later, and what keeps a renderer-only change out of the engineering digest.
SPEC_SCHEMA = "m7-diagram-spec/2"

EntityKind = Literal["equipment", "instrument"]
Orientation = Literal["landscape", "portrait"]
AspectClass = Literal["standard", "wide", "extra_wide"]
FlowDirection = Literal["left_to_right", "top_to_bottom"]
Grouping = Literal["grouped_by_system", "grouped_by_zone", "flat"]
Density = Literal["compact", "comfortable"]

#: Field names that are geometry wherever they appear. Imported from the contract rather than
#: re-listed here, so the two cannot drift.
GEOMETRY_FIELD_NAMES: tuple[str, ...] = FORBIDDEN_MODEL_GEOMETRY_FIELDS

#: Relative anchors are geometry expressed as a relationship, so they are refused with the
#: same force as ``x`` and ``y``. The predicate names are checked as *values* too, so
#: ``{"relation": "left_of"}`` is caught even though ``relation`` is not a geometry field.
RELATIVE_ANCHOR_MARKERS: tuple[str, ...] = (
    "left_of",
    "right_of",
    "above",
    "below",
    "near",
    "closer_than",
    "distance_ratio",
    "relative_to",
    "anchor",
    "offset",
)


class DiagramSpecGeometryError(ValueError):
    """A specification carried geometry. Names the field path, never repairs it silently."""

    def __init__(self, field_path: str, value: Any = None) -> None:
        self.field_path = field_path
        self.value = value
        rendered = f"{field_path} = {value!r}" if value is not None else field_path
        super().__init__(
            f"diagram specification carries geometry at {rendered}: the model owns meaning "
            "and code owns placement, so geometry is refused rather than ignored"
        )


#: A positional predicate written into free text is still an instruction to place something,
#: so string values are checked too -- as whole tokens, so that ordinary prose (and the
#: Chinese text this application is used in) is not refused by accident.
_ANCHOR_IN_TEXT = re.compile(
    r"\b(" + "|".join(re.escape(marker) for marker in RELATIVE_ANCHOR_MARKERS) + r")\b",
    re.IGNORECASE,
)


def _is_geometry_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in GEOMETRY_FIELD_NAMES:
        return True
    if lowered in RELATIVE_ANCHOR_MARKERS:
        return True
    # ``port_x`` and ``canvas_width`` are already named; this catches the same shape under a
    # new prefix without accepting a legitimate field such as ``x`` inside a word.
    return any(
        lowered.startswith(f"{prefix}_") and lowered.split("_", 1)[1] in GEOMETRY_FIELD_NAMES
        for prefix in ("port", "canvas", "node", "element", "symbol", "connector")
    )


def reject_geometry(payload: Any, *, path: str = "$") -> None:
    """Walk a raw specification and refuse geometry-shaped keys or anchor predicates.

    Raises :class:`DiagramSpecGeometryError` on the first offending field, which is the
    behaviour fixture C asks for: reject, name the field, produce nothing.
    """

    if isinstance(payload, list):
        for index, item in enumerate(payload):
            reject_geometry(item, path=f"{path}[{index}]")
        return
    if not isinstance(payload, dict):
        return
    for key, value in payload.items():
        key_text = str(key)
        field_path = f"{path}.{key_text}"
        if _is_geometry_key(key_text):
            raise DiagramSpecGeometryError(field_path, value)
        if isinstance(value, str) and _ANCHOR_IN_TEXT.search(value):
            raise DiagramSpecGeometryError(field_path, value)
        reject_geometry(value, path=field_path)


class DiagramSystem(StrictModel):
    """One declared system, with the order the drawing should read them in."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)

    system_id: str
    name: str = ""
    order: int = Field(default=0, ge=0)


class DiagramEntity(StrictModel):
    """Equipment or instrument: a node of the semantic topology, without a position."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)

    engineering_id: str
    kind: EntityKind
    system_id: str
    tag: str = ""
    name: str = ""
    equipment_class: str = ""
    instrument_type: str = ""
    measurement: str = ""
    #: Required, and required to be *something*: an empty value would let a node reach the
    #: engine with no renderer binding at all, which step 3 would then have to refuse anyway.
    symbol_key: str = Field(min_length=1)


class DiagramConnection(StrictModel):
    """A connection between two entities. Endpoints are identities and port names."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)

    engineering_id: str
    source_engineering_id: str
    target_engineering_id: str
    source_port_id: str = ""
    target_port_id: str = ""
    medium: str = ""
    tag: str = ""


class DiagramRequiredLoop(StrictModel):
    """A loop the drawing must close, stated as the ordered identities along it."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)

    loop_id: str
    engineering_ids: list[str] = Field(default_factory=list)
    note: str = ""


class DiagramLayoutIntent(StrictModel):
    """Discrete layout intent. Every field is a class or an ordering, never a number."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)

    orientation: Orientation | None = None
    preferred_aspect_class: AspectClass | None = None
    primary_flow_direction: FlowDirection | None = None
    system_order: list[str] = Field(default_factory=list)
    grouping: Grouping | None = None
    density: Density | None = None


class DiagramSpec(StrictModel):
    """The complete model-side output: meaning and intent, and nothing placed."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)

    schema_version: str = SPEC_SCHEMA
    label: str = ""
    systems: list[DiagramSystem] = Field(default_factory=list)
    entities: list[DiagramEntity] = Field(default_factory=list)
    connections: list[DiagramConnection] = Field(default_factory=list)
    required_loops: list[DiagramRequiredLoop] = Field(default_factory=list)
    layout_intent: DiagramLayoutIntent = Field(default_factory=DiagramLayoutIntent)

    def problems(self) -> list[str]:
        """Structural coherence: a specification may not describe what it does not declare."""

        problems: list[str] = []
        system_ids = [system.system_id for system in self.systems]
        if len(system_ids) != len(set(system_ids)):
            problems.append("system ids must be unique")
        entity_ids = [entity.engineering_id for entity in self.entities]
        if len(entity_ids) != len(set(entity_ids)):
            problems.append("engineering ids must be unique within a specification")
        known = set(system_ids)
        for entity in self.entities:
            if entity.system_id not in known:
                problems.append(
                    f"entity {entity.engineering_id!r} names an undeclared system "
                    f"{entity.system_id!r}"
                )
        declared = set(entity_ids)
        connection_ids = [connection.engineering_id for connection in self.connections]
        if len(connection_ids) != len(set(connection_ids)):
            problems.append("connection ids must be unique")
        for connection in self.connections:
            for endpoint in (
                connection.source_engineering_id,
                connection.target_engineering_id,
            ):
                if endpoint not in declared:
                    problems.append(
                        f"connection {connection.engineering_id!r} names an undeclared "
                        f"entity {endpoint!r}"
                    )
        for loop in self.required_loops:
            missing = [item for item in loop.engineering_ids if item not in declared]
            if missing:
                problems.append(
                    f"required loop {loop.loop_id!r} names undeclared entities {missing}"
                )
        for ordered in self.layout_intent.system_order:
            if ordered not in known:
                problems.append(f"layout intent orders an undeclared system {ordered!r}")
        return problems


def load_diagram_spec(payload: Any) -> DiagramSpec:
    """Validate a raw specification, refusing geometry before anything else happens.

    Order matters: geometry is refused first, so a payload that carries coordinates fails as
    *geometry* rather than as some incidental validation error about an unexpected key.
    """

    reject_geometry(payload)
    spec = DiagramSpec.model_validate(payload)
    problems = spec.problems()
    if problems:
        raise ValueError("incoherent diagram specification: " + "; ".join(problems))
    return spec
