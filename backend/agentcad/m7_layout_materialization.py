"""M7-2 phase 3: materializing a finalized canonical layout into the production writer's input.

The deterministic engine ends at step 5 with a named canonical layout. This module makes that
layout *real*: it compiles it into the operations the one governed write path already accepts, so
a drawing exists as a document revision rather than as a plan object.

Four rules shape the code, and each one is a decision someone would otherwise make differently:

* **The layout is the only geometry input.** Nothing here reads a model, a prompt or a document
  for a coordinate: every number comes from the finalized plan. ``LLM_GEOMETRY_IS_FORBIDDEN`` on
  this path is not a policy statement, it is the absence of an argument for one.
* **The materialization is deterministic.** Element ids are derived from engineering identities,
  operations are emitted in a declared order, the drawing is translated by the declared canvas
  origin, and the digest covers the projection -- so two runs of the same layout produce the same
  drawing, and the identity of that drawing does not contain a clock, a session or a run id.
* **The existing writer is the only writer.** The output is a plain ``TransactionRequest`` for
  ``DocumentService.apply_transaction``; there is no second transaction system here, and the
  endpoints are bound to symbols and ports the writer resolves itself, so "the pipe meets the
  nozzle" is the writer's check rather than this module's promise.
* **Nothing may change the semantics.** The operations are adds only -- no deletes, no updates,
  no re-tagging -- and the committed document is read back and compared against the layout the
  materializer was given, so "every placed, routed and annotated row appears exactly once" is a
  check on the committed drawing rather than on the intent.

Two things that are *not* inputs, because taking them would break the function relationship above:

* **The label text.** The layout places a label *box*, measured against one specific string; that
  string is the engineering identity's tag. Accepting labels from the caller would let one
  finalized layout, under one canonical digest, produce two different drawings -- and the box
  would then be text nobody measured. The materializer derives the text and proves the box fits it.
* **The target's baseline.** ``add``-only operations plus a post-write reconciliation would commit
  an unrelated element that was already in the target and only *then* report it, so the target is
  preflighted before the write and the commit is bound to the revision the preflight read.

Two consequences worth naming, because both look like details and are not:

* Connectors are emitted as ``routing="manual"`` carrying the layout's own waypoints. The writer
  recomputes an ``orthogonal`` route from its two endpoints, which would silently replace the
  obstacle-aware route with a two-bend elbow; ``manual`` is the mode that keeps the points the
  engine decided and still binds them to the ports.
* The canvas is *reported*, never invented. The derived canvas belongs to the layout, so a target
  document smaller than it is a hard failure naming the size to create instead of a drawing that
  is quietly clipped.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .annotation_layout import text_bounds
from .audit_models import AuditContext
from .auto_layout_geometry import ANNOTATION_FONT_SIZE
from .auto_layout_identity import (
    LayoutIdentityError,
    canonical_projection_rows,
    plan_engineering_digest,
)
from .auto_layout_semantic import STEP_5, SemanticLayoutPlan
from .m7_diagram_spec import SPEC_SCHEMA
from .m7_layout_contract import (
    ADAPTER_TOPOLOGY_DIGEST_VERSION,
    LAYOUT_DIGEST_VERSION,
    LAYOUT_PROJECTION_VERSION,
    M7_PROVENANCE_METADATA_KEY,
    M7_PROVENANCE_REQUIRED_IDENTITIES,
    MATERIALIZATION_DIGEST_VERSION,
    MATERIALIZER_VERSION,
    SYMBOL_GEOMETRY_CATALOG_DIGEST_VERSION,
)
from .models import (
    AddElementOperation,
    AddSystemOperation,
    ConnectorElement,
    ConnectorEndpoint,
    Document,
    Operation,
    Point,
    SymbolElement,
    SystemGroup,
    TextElement,
    TransactionRequest,
)
from .service import DocumentService

#: The annotation role an editable symbol label carries. Read from the tag resolver's vocabulary
#: rather than retyped: a second spelling of this string would produce a label no report sees.
SYMBOL_LABEL_ANNOTATION_ROLE = "symbol_label"

#: The metadata field a text element uses to name the symbol it labels.
ANNOTATION_SUBJECT_FIELD = "parent_element_id"

#: The layer every materialized element is placed on. The default layer exists in every document,
#: so nothing has to be created for it and no drawing depends on a layer id a caller invented.
MATERIALIZATION_LAYER_ID = "layer_default"

#: The system group every created document carries. It belongs to an *empty* document rather than
#: to any drawing, so the baseline permits it while refusing every other group. Named here and
#: asserted against the model in the test module, so it cannot drift away from the default.
DEFAULT_SYSTEM_GROUP_ID = "system_default"

#: The reserved metadata namespace and the identities the write requires: declared in the contract,
#: because "which revision can be traced back to what" is a governance claim rather than an
#: implementation detail. Re-exported so call sites read one name.
#
#: The version each digest is defined by. Three of them pin the *upstream* definitions the digest
#: was computed with (spec schema, adapter digest, symbol geometry catalog) and four pin the layout
#: and materialization definitions, so the record says which definitions each digest is a digest
#: under -- one version for the whole record would not.
PROVENANCE_VERSION_VALUES: tuple[tuple[str, str], ...] = (
    ("diagram_spec_schema_version", SPEC_SCHEMA),
    ("adapter_topology_digest_version", ADAPTER_TOPOLOGY_DIGEST_VERSION),
    ("symbol_geometry_catalog_digest_version", SYMBOL_GEOMETRY_CATALOG_DIGEST_VERSION),
    ("layout_digest_version", LAYOUT_DIGEST_VERSION),
    ("layout_projection_version", LAYOUT_PROJECTION_VERSION),
    ("materializer_version", MATERIALIZER_VERSION),
    ("materialization_digest_version", MATERIALIZATION_DIGEST_VERSION),
)


def provenance_version_values() -> dict[str, str]:
    """The declared version of every field the binding table asks for, in its order.

    Read from the declarations rather than from a live run: these pin *which definition* a digest is
    a digest under, and the whole point of recording them is that a later reader can interpret a
    stored digest without re-running anything.
    """

    return dict(PROVENANCE_VERSION_VALUES)



class MaterializationError(ValueError):
    """The canonical layout could not be turned into a drawing."""


class LayoutIsNotFinalizedError(MaterializationError):
    """The plan has no canonical identity yet, so it names no drawing to materialize."""


class MaterializationLabelError(MaterializationError):
    """The label box the layout placed does not fit the text its entity implies."""


class MaterializationCanvasError(MaterializationError):
    """The target document's canvas cannot hold the derived canvas."""


class MaterializationTargetNotEmptyError(MaterializationError):
    """The target holds content the layout did not decide, so appending would be a merge."""


class MaterializationProvenanceError(MaterializationError):
    """The caller tried to speak for the materializer's own provenance.

    Attribution is the caller's to give -- who asked, from which surface, under which session. The
    engineering identity chain is not: it is a property of the compilation, and a caller able to
    omit or overwrite it could commit a drawing whose revision cannot be traced back to the
    specification it came from.
    """


class MaterializationDocumentError(MaterializationError):
    """The committed document does not cover the layout it was materialized from."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _quantize(value: float) -> float:
    number = round(float(value), 6)
    return 0.0 if number == 0 else number


# ---------------------------------------------------------------------------------------------
# Deterministic identities: the drawing's element ids are functions of the engineering ids.
# ---------------------------------------------------------------------------------------------


def materialized_element_id(engineering_id: str, role: str, *, disambiguator: int = 0) -> str:
    """The element id one engineering row gets in the document.

    Derived rather than generated: an id from ``new_id`` would differ between two runs of the same
    layout, which would make "the same drawing" unprovable at the level the drawing is read at.
    The role is part of the id because a node and its label are two elements, and
    ``disambiguator`` settles the case where two engineering ids normalize to one string --
    a collision is resolved deterministically rather than left to the document validator.
    """

    cleaned = "".join(character if character.isalnum() else "_" for character in engineering_id)
    cleaned = cleaned.strip("_") or "row"
    suffix = "" if not disambiguator else f"_{disambiguator}"
    return f"el_{role}_{cleaned}{suffix}" if role else f"el_{cleaned}{suffix}"


def _assign_element_ids(rows: list[dict[str, Any]]) -> None:
    """Give every row an id, resolving normalization collisions in a declared order.

    Two distinct engineering ids can normalize to the same element id (``a-1`` and ``a 1``). The
    order is the canonical row order, so the resolution is the same on every run and the clash is
    a nameable event rather than whatever the document validator happens to say.
    """

    seen: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        role = str(row["element_id_role"])
        cleaned = materialized_element_id(str(row["engineering_id"]), role)
        seen.setdefault((role, cleaned), []).append(row)
    for (role, base), group in seen.items():
        for index, row in enumerate(sorted(group, key=lambda item: str(item["engineering_id"]))):
            row["element_id"] = base if index == 0 else f"{base}_{index}"
            if role == LABEL_ELEMENT_ROLE:
                row["subject_element_id"] = materialized_element_id(
                    str(row["engineering_id"]), SYMBOL_ELEMENT_ROLE
                )


SYMBOL_ELEMENT_ROLE = "symbol"
CONNECTOR_ELEMENT_ROLE = "connector"
LABEL_ELEMENT_ROLE = "label"

#: The order rows are emitted in, and it is not alphabetical: a connector names the symbols it
#: binds to, so symbols are written first or the writer looks up an element that does not exist
#: yet. Systems come before everything for the same reason -- an element names its system.
MATERIALIZATION_ROW_ORDER: tuple[str, ...] = ("symbol", "connector", "annotation")

#: The reconciliation's field groups, split by *kind of comparison* rather than by element kind.
#: The drawn words are their own group because they are what a geometry-only comparison silently
#: misses: a same-length substitution leaves the box identical.
RECONCILED_IDENTITY_FIELDS: tuple[str, ...] = (
    "kind",
    "engineering_id",
    "tag",
    "symbol_key",
    "system_id",
)
RECONCILED_TEXT_FIELDS: tuple[str, ...] = (
    "text",
    "label",
)
RECONCILED_GEOMETRY_FIELDS: tuple[str, ...] = ("x", "y", "width", "height")

#: The non-geometric row fields the digest sees. The drawn words are in here (``text`` for an
#: annotation, ``label`` for a symbol): matching geometry with the wrong words on the drawing is
#: not the same drawing.
DIGEST_ROW_FIELDS: frozenset[str] = frozenset(
    {
        "kind",
        "engineering_id",
        "element_id",
        "system_id",
        "tag",
        "symbol_key",
        "label",
        "text",
        "medium",
        "flow_direction",
        "subject_element_id",
        "source",
        "target",
    }
)


# ---------------------------------------------------------------------------------------------
# The materialized drawing, as data.
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterializedLayout:
    """One finalized canonical layout, compiled into the writer's input.

    ``operations`` is what the caller submits; ``rows`` is the same drawing described canonically
    for the digest and for verification, so a reader can compare a materialization with a
    committed document without re-deriving anything from geometry.
    """

    document_id: str
    operations: tuple[Operation, ...]
    rows: tuple[dict[str, Any], ...]
    canvas_width: float
    canvas_height: float
    origin_x: float
    origin_y: float
    element_ids: tuple[tuple[str, str, str], ...]
    materialization_digest: str
    materializer_version: str = MATERIALIZER_VERSION
    digest_version: str = MATERIALIZATION_DIGEST_VERSION
    #: The identity chain this drawing was compiled from, as pairs rather than a mapping so a frozen
    #: dataclass stays hashable. It travels *with* the layout because it is a fact about the
    #: compilation: computing it at write time would let two call sites disagree about it, and
    #: letting the caller pass it would let a caller omit it.
    provenance_identities: tuple[tuple[str, str], ...] = ()

    @property
    def digest(self) -> str:
        return self.materialization_digest

    def element_id_for(self, engineering_id: str, role: str) -> str:
        for row_id, row_role, element_id in self.element_ids:
            if row_id == engineering_id and row_role == role:
                return element_id
        raise KeyError(f"no materialized element for {engineering_id!r} as {role!r}")

    def provenance(self) -> dict[str, str]:
        """The chain a committed revision has to be traceable through, as plain strings.

        Every digest travels with the version that defines it: a digest without its version is not
        traceable, because two versions of the same digest describe different things.
        """

        return dict(self.provenance_identities)


def materialization_payload(
    *,
    document_canvas: tuple[float, float],
    origin: tuple[float, float],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The digest envelope: the materializer's version, the layout identity and the drawing.

    The document id is deliberately absent -- it says *where* the drawing was written, not *what*
    was drawn -- and so is every clock, since the same layout written twice must digest the same.
    """

    return {
        "materialization_digest_version": MATERIALIZATION_DIGEST_VERSION,
        "materializer_version": MATERIALIZER_VERSION,
        "canvas": {"width": document_canvas[0], "height": document_canvas[1]},
        "origin": {"x": origin[0], "y": origin[1]},
        "rows": [dict(row) for row in rows],
    }


def materialization_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(payload)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# Reading the layout back out of the committed document.
# ---------------------------------------------------------------------------------------------


def document_rows(document: Document) -> tuple[dict[str, Any], ...]:
    """The committed drawing as canonical rows, in a declared order.

    Sorted by element id rather than read in document order: element order is an artefact of the
    operation order, and comparing two drawings should not depend on how they were written.
    """

    rows: list[dict[str, Any]] = []
    for element in document.elements:
        if element.type == "symbol":
            rows.append(
                {
                    "element_id": element.id,
                    "kind": "symbol",
                    "engineering_id": str((element.properties or {}).get("engineering_id", "")),
                    "tag": str((element.properties or {}).get("tag", "")),
                    "symbol_key": element.symbol_key,
                    "system_id": element.system_id,
                    # The symbol's own label field, read rather than assumed empty: it is the
                    # second surface a post-write edit could have written text into.
                    "label": element.label,
                    "text": "",
                    "x": _quantize(element.position.x),
                    "y": _quantize(element.position.y),
                    "width": _quantize(element.width),
                    "height": _quantize(element.height),
                    "waypoints": [],
                }
            )
        elif element.type == "connector":
            rows.append(
                {
                    "element_id": element.id,
                    "kind": "connector",
                    "engineering_id": str((element.metadata or {}).get("engineering_id", "")),
                    "tag": element.process_tag,
                    "symbol_key": "",
                    "label": "",
                    "text": "",
                    "system_id": element.system_id,
                    "x": _quantize(element.points[0].x),
                    "y": _quantize(element.points[0].y),
                    "width": 0.0,
                    "height": 0.0,
                    "waypoints": [
                        [_quantize(point.x), _quantize(point.y)] for point in element.points[1:]
                    ],
                }
            )
        elif element.type == "text":
            # The box is measured with the same pure text rule the layout placed against, so
            # "the label is where the layout put it" compares two boxes rather than a box with a
            # baseline.
            box = text_bounds(element)
            rows.append(
                {
                    "element_id": element.id,
                    "kind": "annotation",
                    "engineering_id": str((element.metadata or {}).get("engineering_id", "")),
                    "tag": "",
                    "symbol_key": "",
                    "label": "",
                    # The words on the drawing. Compared, because a drawing whose geometry,
                    # tags and bindings all match while its text says something else is a
                    # different drawing wearing the same layout.
                    "text": element.text,
                    "system_id": element.system_id,
                    "x": _quantize(box.x1),
                    "y": _quantize(box.y1),
                    "width": _quantize(box.x2 - box.x1),
                    "height": _quantize(box.y2 - box.y1),
                    "waypoints": [],
                }
            )
    rows.sort(key=lambda row: (str(row["kind"]), str(row["engineering_id"]), str(row["element_id"])))
    return tuple(rows)


# ---------------------------------------------------------------------------------------------
# The materializer itself.
# ---------------------------------------------------------------------------------------------


def _symbol_element_id(rows: Sequence[Mapping[str, Any]], node_id: str) -> str:
    """The element id a connector binds to, read from the placed symbols themselves."""

    for row in rows:
        if row["kind"] == "symbol" and str(row["engineering_id"]) == node_id:
            return str(row["element_id"])
    raise MaterializationError(
        f"a route binds to {node_id!r}, which the layout did not place: a pipe into nothing"
    )


def label_text_for(plan: SemanticLayoutPlan, node_id: str) -> str:
    """The one string the layout's label box for ``node_id`` was measured against.

    Derived, never supplied: the annotation row carries a box, and the box's own width already
    encodes the text length (``text_bounds`` measures ``len(text) * font_size * 0.6``). A caller
    could therefore hand over any text of the right *length* while the drawing said something
    else, which is exactly the failure a geometry-only reconciliation cannot see.
    """

    for fact in plan.engineering_entities:
        if fact.engineering_id == node_id:
            text = str(fact.tag).strip()
            if not text:
                raise MaterializationLabelError(
                    f"the layout placed a label box for {node_id!r} and its engineering row "
                    "carries no tag: the box was measured against a string the plan does not hold"
                )
            return text
    raise MaterializationLabelError(
        f"the layout annotated {node_id!r}, which the plan does not declare as an entity"
    )


def _label_box_matches_text(box: Mapping[str, Any], text: str) -> bool:
    """Whether the placed box is the box ``text`` measures to, exactly.

    Compared through the same pure rule the layout measured with, rather than by re-deriving a
    width here: two implementations of the same rule is how a box and its text drift apart.
    """

    element = TextElement(
        id="measure",
        layer_id=MATERIALIZATION_LAYER_ID,
        position=Point(x=float(box["x"]), y=float(box["y"]) + ANNOTATION_FONT_SIZE),
        text=text,
        font_size=ANNOTATION_FONT_SIZE,
        anchor="start",
    )
    measured = text_bounds(element)
    return (
        _quantize(measured.x1) == _quantize(float(box["x"]))
        and _quantize(measured.x2 - measured.x1) == _quantize(float(box["width"]))
        and _quantize(measured.y2 - measured.y1) == _quantize(float(box["height"]))
    )


def _plan_rows(plan: SemanticLayoutPlan) -> tuple[dict[str, Any], ...]:
    """The layout as materialization rows, before any element is constructed.

    Verification and construction read the same rows, so "what was checked" and "what was built"
    cannot drift apart.
    """

    kinds = {fact.engineering_id: fact.kind for fact in plan.engineering_entities}
    if not kinds:
        raise MaterializationError(
            "the plan carries no engineering entities: materializing a drawing whose plant is "
            "unrecorded would put elements in a document nobody could check"
        )
    systems = {fact.system_id for fact in plan.engineering_systems}
    symbols = dict(plan.node_symbols)
    facts = {fact.engineering_id: fact for fact in plan.engineering_entities}
    bindings = {(binding.connection_id, binding.role): binding for binding in plan.endpoint_bindings}
    flow_pairs = set(plan.flow_edges)

    rows: list[dict[str, Any]] = []
    for row in plan.placement:
        node_id = str(row["engineering_id"])
        if node_id not in kinds:
            raise MaterializationError(
                f"the layout placed {node_id!r}, which the plan does not declare as an entity"
            )
        fact = facts[node_id]
        rows.append(
            {
                "kind": "symbol",
                "engineering_id": node_id,
                "element_id_role": SYMBOL_ELEMENT_ROLE,
                "system_id": fact.system_id,
                "tag": fact.tag,
                "symbol_key": symbols.get(node_id, ""),
                # Written empty on purpose: the repository's polish already established that a
                # symbol has one editable text (the ``symbol_label`` annotation) and not a second,
                # uneditable copy of the same string in ``label``.
                "label": "",
                "x": _quantize(row["x"]),
                "y": _quantize(row["y"]),
                "width": _quantize(row["width"]),
                "height": _quantize(row["height"]),
                "waypoints": [],
            }
        )

    for row in plan.routing:
        connection_id = str(row["engineering_id"])
        connection = next(
            (item for item in plan.connections if item.connection_id == connection_id), None
        )
        if connection is None:
            raise MaterializationError(
                f"the layout routed {connection_id!r}, which the plan does not declare"
            )
        source = bindings.get((connection_id, "source"))
        target = bindings.get((connection_id, "target"))
        if source is None or target is None:
            raise MaterializationError(
                f"connection {connection_id!r} has no resolved endpoint binding: a route with an "
                "unresolved end is a pipe into nothing"
            )
        points = [
            [_quantize(row["x"]), _quantize(row["y"])],
            *[[_quantize(point[0]), _quantize(point[1])] for point in row["ordered_waypoints"]],
        ]
        rows.append(
            {
                "kind": "connector",
                "engineering_id": connection_id,
                "element_id_role": CONNECTOR_ELEMENT_ROLE,
                "system_id": facts[source.node_id].system_id,
                "tag": connection.tag,
                "symbol_key": "",
                "label": "",
                "medium": connection.medium,
                "source": {
                    "node_id": source.node_id,
                    "port_id": source.port_id,
                },
                "target": {
                    "node_id": target.node_id,
                    "port_id": target.port_id,
                },
                "flow_direction": "forward"
                if (connection.source_engineering_id, connection.target_engineering_id) in flow_pairs
                else "none",
                "x": points[0][0],
                "y": points[0][1],
                "width": 0.0,
                "height": 0.0,
                "waypoints": points[1:],
            }
        )

    annotated = {str(row["engineering_id"]) for row in plan.annotations}
    for node_id in sorted(annotated):
        if node_id not in kinds:
            raise MaterializationError(
                f"the layout annotated {node_id!r}, which the plan does not declare"
            )
        # Derived, and then *proved* to fit the box: the layout measured that box against one
        # specific string, so a box that does not fit the derived text means the drawing would
        # carry words nothing was measured for.
        text = label_text_for(plan, node_id)
        box = next(row for row in plan.annotations if str(row["engineering_id"]) == node_id)
        if not _label_box_matches_text(box, text):
            raise MaterializationLabelError(
                f"the label box the layout placed for {node_id!r} is "
                f"{box['width']}x{box['height']} at ({box['x']}, {box['y']}) and does not measure "
                f"to the {text!r} its engineering row carries: the box and the text came from two "
                "different runs"
            )
        rows.append(
            {
                "kind": "annotation",
                "engineering_id": node_id,
                "element_id_role": LABEL_ELEMENT_ROLE,
                "system_id": facts[node_id].system_id,
                "tag": "",
                "symbol_key": "",
                "label": "",
                "text": text,
                "x": _quantize(box["x"]),
                "y": _quantize(box["y"]),
                "width": _quantize(box["width"]),
                "height": _quantize(box["height"]),
                "waypoints": [],
            }
        )

    _assign_element_ids(rows)
    for row in rows:
        if row["kind"] == "connector":
            row["source"]["element_id"] = _symbol_element_id(rows, str(row["source"]["node_id"]))
            row["target"]["element_id"] = _symbol_element_id(rows, str(row["target"]["node_id"]))
        elif row["kind"] == "annotation":
            row["subject_element_id"] = _symbol_element_id(rows, str(row["engineering_id"]))

    for row in rows:
        if row["system_id"] not in systems:
            raise MaterializationError(
                f"{row['kind']} {row['engineering_id']!r} belongs to system {row['system_id']!r}, "
                "which the plan does not declare"
            )
        if row["kind"] == "symbol" and not row["symbol_key"]:
            raise MaterializationError(
                f"symbol {row['engineering_id']!r} has no catalogue binding: the layout placed it "
                "against geometry it cannot name"
            )
    ids = [row["element_id"] for row in rows]
    if len(ids) != len(set(ids)):
        duplicate = next(item for item in ids if ids.count(item) > 1)
        raise MaterializationError(
            f"two rows materialize to the same element id {duplicate!r}: element ids must be "
            "unique in a document, so the engineering identities are not distinct enough"
        )
    rows.sort(key=lambda row: (MATERIALIZATION_ROW_ORDER.index(str(row["kind"])), str(row["engineering_id"])))
    return tuple(rows)


def _operations(
    rows: Sequence[Mapping[str, Any]],
    *,
    systems: Sequence[tuple[str, str]],
    origin_x: float,
    origin_y: float,
) -> tuple[Operation, ...]:
    """The writer-ready operations, in the order the document will hold them."""

    operations: list[Operation] = [
        AddSystemOperation(system=SystemGroup(id=system_id, name=name))
        for system_id, name in systems
    ]
    for row in rows:
        element_id = str(row["element_id"])
        system_id = str(row["system_id"])
        if row["kind"] == "symbol":
            operations.append(
                AddElementOperation(
                    element=SymbolElement(
                        id=element_id,
                        layer_id=MATERIALIZATION_LAYER_ID,
                        system_id=system_id,
                        symbol_key=str(row["symbol_key"]),
                        # Left empty on purpose, and asserted empty on the way back: the
                        # repository's polish established that a symbol has one editable text,
                        # so writing the tag here too would draw it twice.
                        label="",
                        position=Point(
                            x=_quantize(float(row["x"]) - origin_x),
                            y=_quantize(float(row["y"]) - origin_y),
                        ),
                        width=float(row["width"]),
                        height=float(row["height"]),
                        metadata={"engineering_id": str(row["engineering_id"])},
                        properties={
                            "engineering_id": str(row["engineering_id"]),
                            "tag": str(row["tag"]),
                        },
                    )
                )
            )
        elif row["kind"] == "connector":
            # The row carries the first point separately (that is where the route starts), so the
            # element's point list is the first point followed by the waypoints -- not the
            # waypoints alone, which would silently drop the departure stub the router drew.
            points = [
                Point(
                    x=_quantize(float(row["x"]) - origin_x),
                    y=_quantize(float(row["y"]) - origin_y),
                ),
                *[
                    Point(x=_quantize(p[0] - origin_x), y=_quantize(p[1] - origin_y))
                    for p in row["waypoints"]
                ],
            ]
            operations.append(
                AddElementOperation(
                    element=ConnectorElement(
                        id=element_id,
                        layer_id=MATERIALIZATION_LAYER_ID,
                        system_id=system_id,
                        points=points,
                        source=ConnectorEndpoint(
                            element_id=str(row["source"]["element_id"]),
                            port_id=str(row["source"]["port_id"]),
                            point=points[0],
                        ),
                        target=ConnectorEndpoint(
                            element_id=str(row["target"]["element_id"]),
                            port_id=str(row["target"]["port_id"]),
                            point=points[-1],
                        ),
                        # The layout's own waypoints, kept as manual: an orthogonal connector is
                        # re-routed from its endpoints by the writer, which would discard the
                        # engine's obstacle-aware route.
                        routing="manual",
                        process_tag=str(row["tag"]),
                        medium=str(row["medium"]),
                        flow_direction=str(row["flow_direction"]),
                        metadata={"engineering_id": str(row["engineering_id"])},
                    )
                )
            )
        else:
            operations.append(
                AddElementOperation(
                    element=TextElement(
                        id=element_id,
                        layer_id=MATERIALIZATION_LAYER_ID,
                        system_id=system_id,
                        position=Point(
                            x=_quantize(float(row["x"]) - origin_x),
                            # The layout places a text *box*; a text element is anchored on its
                            # baseline, so the ascent puts the box top where the layout put it.
                            y=_quantize(float(row["y"]) + ANNOTATION_FONT_SIZE - origin_y),
                        ),
                        text=str(row["text"]),
                        font_size=ANNOTATION_FONT_SIZE,
                        anchor="start",
                        metadata={
                            "engineering_id": str(row["engineering_id"]),
                            ANNOTATION_SUBJECT_FIELD: str(row["subject_element_id"]),
                            "annotation_role": SYMBOL_LABEL_ANNOTATION_ROLE,
                        },
                    )
                )
            )
    return tuple(operations)


def materialize_canonical_layout(
    plan: SemanticLayoutPlan,
    *,
    document_id: str,
) -> MaterializedLayout:
    """Phase 3: compile a finalized canonical layout into the production writer's input.

    The plan must be at step 5 with its identity computed: materializing an unfinished layout
    would write geometry that no identity names, which is the state this whole milestone exists to
    remove.

    There is no ``labels`` argument, and that is the point: the label text is derived here from the
    engineering identity the layout already binds, and the box the layout placed is proven to be
    the box that text measures to. A caller-supplied label would make ``finalized layout ->
    drawing`` a relation instead of a function, under one canonical digest.
    """

    if plan.produced_at_step != STEP_5 or not plan.canonical_layout_digest:
        raise LayoutIsNotFinalizedError(
            f"materialization needs a finalized canonical layout, not one produced at "
            f"{plan.produced_at_step!r} with digest {plan.canonical_layout_digest!r}"
        )
    if plan.canonical_projection_envelope is None:
        raise LayoutIsNotFinalizedError("the layout carries no canonical projection envelope")
    try:
        canonical_projection_rows(plan)
    except LayoutIdentityError as exc:  # pragma: no cover - defensive: the gate already ran
        raise MaterializationError(str(exc)) from exc

    rows = _plan_rows(plan)
    canvas = plan.canonical_projection_envelope["canvas_bounds"]
    origin_x = _quantize(float(canvas["x"]))
    origin_y = _quantize(float(canvas["y"]))
    width = _quantize(float(canvas["width"]))
    height = _quantize(float(canvas["height"]))
    systems = tuple(
        (fact.system_id, fact.name)
        for fact in sorted(plan.engineering_systems, key=lambda item: item.system_id)
    )
    operations = _operations(
        rows, systems=systems, origin_x=origin_x, origin_y=origin_y
    )
    digest_rows = [_digest_row(row, origin_x, origin_y) for row in rows]
    digest = materialization_digest(
        materialization_payload(
            document_canvas=(width, height),
            origin=(origin_x, origin_y),
            rows=digest_rows,
        )
    )
    identities = {
        "diagram_spec_semantic_digest": plan_engineering_digest(plan),
        "adapter_topology_digest": plan.topology_digest,
        "symbol_geometry_catalog_digest": plan.symbol_geometry_catalog_digest,
        "canonical_layout_digest": plan.canonical_layout_digest,
        "materialization_digest": digest,
    }
    provenance = {**identities, **provenance_version_values()}
    return MaterializedLayout(
        document_id=document_id,
        operations=operations,
        rows=tuple(digest_rows),
        canvas_width=width,
        canvas_height=height,
        origin_x=origin_x,
        origin_y=origin_y,
        # Keyed by the row's *role*, not its kind: a node and its label are two elements of one
        # engineering row, so "which element is this row in that role" needs the role to be asked
        # for. (For a symbol and a connector the two strings coincide; for a label they do not.)
        element_ids=tuple(
            (str(row["engineering_id"]), str(row["element_id_role"]), str(row["element_id"]))
            for row in rows
        ),
        materialization_digest=digest,
        provenance_identities=tuple(sorted(provenance.items())),
    )


def _digest_row(row: Mapping[str, Any], origin_x: float, origin_y: float) -> dict[str, Any]:
    """One row as the digest sees it: coordinates already translated into document space.

    The translation is inside the digested values because two drawings that differ only by the
    origin are not the same drawing: one of them has a margin on a different side.
    """

    digest = {
        key: value
        for key, value in row.items()
        if key in DIGEST_ROW_FIELDS
    }
    digest["x"] = _quantize(float(row["x"]) - origin_x)
    digest["y"] = _quantize(float(row["y"]) - origin_y)
    digest["width"] = _quantize(float(row["width"]))
    digest["height"] = _quantize(float(row["height"]))
    digest["waypoints"] = [
        [_quantize(point[0] - origin_x), _quantize(point[1] - origin_y)]
        for point in row["waypoints"]
    ]
    return digest


# ---------------------------------------------------------------------------------------------
# The write, through the existing governed writer.
# ---------------------------------------------------------------------------------------------


def materialization_matches_document(
    layout: MaterializedLayout, document: Document
) -> list[str]:
    """The committed drawing against the layout it was materialized from.

    Row-for-row and in both directions: a drawing missing a row is the failure a digest cannot see,
    and a drawing with an extra row means something was written that the layout never decided.
    """

    problems: list[str] = []
    committed = document_rows(document)
    expected = {str(row["element_id"]): row for row in layout.rows}
    actual = {str(row["element_id"]): row for row in committed}

    for element_id in sorted(set(expected) - set(actual)):
        row = expected[element_id]
        problems.append(
            f"the document is missing {row['kind']} {row['engineering_id']!r} "
            f"({element_id}): the layout decided it and it was never written"
        )

    for element_id in sorted(set(actual) - set(expected)):
        row = actual[element_id]
        problems.append(
            f"the document holds {row['kind']} {row['engineering_id']!r} ({element_id}), which "
            "the layout never decided"
        )
    for element_id in sorted(set(expected) & set(actual)):
        left, right = expected[element_id], actual[element_id]
        for field in RECONCILED_IDENTITY_FIELDS:
            if str(left.get(field, "")) != str(right.get(field, "")):
                problems.append(
                    f"{element_id} has {field} {right.get(field)!r} in the document and "
                    f"{left.get(field)!r} in the layout"
                )
        for field in RECONCILED_TEXT_FIELDS:
            # The drawn words. A drawing whose geometry, tags and bindings all match while its
            # text says something else is a different drawing wearing the same layout -- and a
            # geometry-only comparison cannot see it, because the box is the same size.
            if str(left.get(field, "")) != str(right.get(field, "")):
                problems.append(
                    f"{element_id} says {right.get(field)!r} in {field} and the layout decided "
                    f"{left.get(field)!r}"
                )
        for field in RECONCILED_GEOMETRY_FIELDS:
            if _quantize(float(left[field])) != _quantize(float(right[field])):
                problems.append(
                    f"{element_id} has {field} {right[field]!r} in the document and "
                    f"{left[field]!r} in the layout"
                )
        if [
            [_quantize(p[0]), _quantize(p[1])] for p in left.get("waypoints", [])
        ] != [[_quantize(p[0]), _quantize(p[1])] for p in right.get("waypoints", [])]:
            problems.append(
                f"{element_id} was routed differently in the document than the layout decided"
            )
    return problems


def require_document_canvas(document: Document, layout: MaterializedLayout) -> None:
    """A document smaller than the derived canvas would crop the drawing, so it is refused."""

    if (
        document.canvas.width + 1e-9 < layout.canvas_width
        or document.canvas.height + 1e-9 < layout.canvas_height
    ):
        raise MaterializationCanvasError(
            f"the derived canvas is {layout.canvas_width}x{layout.canvas_height} and document "
            f"{document.id!r} is {document.canvas.width}x{document.canvas.height}: the canvas "
            "belongs to the layout and may not be spent to keep a smaller document"
        )


def with_materialization_provenance(layout: MaterializedLayout, audit: Any = None) -> AuditContext:
    """The audit context the write actually uses: the caller's attribution plus this chain.

    Attribution is the caller's to give (actor, surface, tool, session, validation status). The
    engineering identity chain is not: it is a property of the compilation, so it is attached here
    and a caller that supplies it is refused rather than merged. Refusing is the point -- silently
    overriding would leave a caller believing its own chain was recorded.
    """

    provenance = layout.provenance()
    missing = [name for name in M7_PROVENANCE_REQUIRED_IDENTITIES if not provenance.get(name)]
    if missing:
        raise MaterializationProvenanceError(
            f"this layout carries no {missing}: a drawing whose revision cannot be traced back to "
            "the specification it came from may not be committed"
        )
    if audit is None:
        context = AuditContext(
            actor="m7-materializer",
            surface="internal",
            tool_name="m7_materialize_layout",
            validation_status="valid",
        )
    else:
        context = (
            audit if isinstance(audit, AuditContext) else AuditContext.model_validate(audit)
        )
    metadata = dict(context.metadata)
    if M7_PROVENANCE_METADATA_KEY in metadata:
        raise MaterializationProvenanceError(
            f"the audit metadata already carries {M7_PROVENANCE_METADATA_KEY!r}: the engineering "
            "identity chain is the materializer's to record, not the caller's to supply or to "
            "overwrite -- attribution is yours, provenance is the compilation's"
        )
    metadata[M7_PROVENANCE_METADATA_KEY] = provenance
    return context.model_copy(update={"metadata": metadata})


def require_empty_target(document: Document) -> None:
    """The pre-write baseline: v1 appends a drawing to a document that holds nothing else.

    ``add``-only operations plus a post-write reconciliation are not enough on their own. An
    unrelated element already in the target would be committed -- revision advanced, audit written
    -- and only *then* reported as an extra row, so the failure would live in the history of a
    drawing that is wrong. Reconciliation stays as the second check; this is the boundary in front
    of the write.

    The rule is "holds nothing engineered", not "is empty": a created document already has a
    default layer and a default system group, and those belong to the empty document rather than
    to any drawing. What is refused is content the layout did not decide -- elements, and any
    system group other than the default one.
    """

    if document.elements:
        offenders = ", ".join(sorted(f"{element.type} {element.id}" for element in document.elements))
        raise MaterializationTargetNotEmptyError(
            f"document {document.id!r} already holds {len(document.elements)} element(s) "
            f"({offenders}) and this layout did not decide them: an add-only materialization "
            "appends a whole drawing, so a non-empty target would be a merge that only shows up "
            "in the committed revision"
        )
    stray = sorted(
        system.id for system in document.systems if system.id != DEFAULT_SYSTEM_GROUP_ID
    )
    if stray:
        raise MaterializationTargetNotEmptyError(
            f"document {document.id!r} already holds system group(s) {stray}, which this layout "
            "did not decide: systems are part of the drawing's reconciliation universe"
        )


def materialized_transaction(
    layout: MaterializedLayout, *, expected_revision: int, label: str = ""
) -> TransactionRequest:
    """The request the governed writer takes. No new writer, no new surface."""

    return TransactionRequest(
        operations=list(layout.operations),
        expected_revision=expected_revision,
        label=label or f"M7 layout materialization {layout.materialization_digest[:12]}",
        source="system",
    )


def apply_materialized_layout(
    service: DocumentService,
    layout: MaterializedLayout,
    *,
    expected_revision: int,
    audit: Any = None,
    label: str = "",
) -> Any:
    """Submit the materialization through ``DocumentService.apply_transaction``.

    The provenance chain travels as audit metadata, because that is where this codebase records
    *why* a revision exists, and the document metadata is not writable through the operation
    vocabulary the writer accepts -- inventing an operation for it would be a new write surface.

    Read, verify, then commit against the revision that was read: the preflight is what keeps a
    wrong drawing out of the history, and ``expected_revision`` is what keeps another writer's
    revision from being landed on top of. A conflict is terminal -- this never retries at N+1,
    because the drawing it verified belongs to N.
    """

    current = service.get_document(layout.document_id)
    if current.revision != expected_revision:
        raise MaterializationError(
            f"expected revision {expected_revision}, document {layout.document_id!r} is at "
            f"{current.revision}"
        )
    require_empty_target(current)
    require_document_canvas(current, layout)
    # The identity chain is attached here rather than accepted from the caller: a caller-supplied
    # chain could be omitted, and an omitted chain is a committed revision nobody can trace back to
    # the specification it came from. Refused before the write, so nothing is committed.
    context = with_materialization_provenance(layout, audit)
    request = materialized_transaction(layout, expected_revision=expected_revision, label=label)
    result = service.apply_transaction(
        layout.document_id,
        request,
        source="system",
        audit=context,
    )
    problems = materialization_matches_document(layout, result.document)
    if problems:
        raise MaterializationDocumentError(
            "the committed document does not cover the layout: " + "; ".join(problems)
        )
    return result


def materialization_provenance(layout: MaterializedLayout) -> dict[str, str]:
    """The identities that go *with* the write: what this drawing is compiled from.

    Read from the layout rather than recomputed here, so "which chain did this drawing come from"
    has one answer rather than one answer per call site.

    Deliberately without a revision. A revision supplied here would be a prediction, and a
    prediction of the next revision number is not a fact about this drawing -- two writers could
    both predict N+1. The revision this record belongs to is whatever the writer actually
    committed, recorded by the writer, and read back through :func:`materialization_record`.
    """

    return layout.provenance()


def materialization_record(layout: MaterializedLayout, result: Any) -> dict[str, str]:
    """The complete audit relation, closed with the revision the writer *did* commit.

    Reads ``result.document.revision`` rather than accepting a number: the chain means "these
    identities produced this successfully committed revision", so the revision half has to come
    from the commit. Nothing here can be assembled before the write.
    """

    committed = getattr(result, "document", None)
    revision = getattr(committed, "revision", None)
    if revision is None:
        raise MaterializationError(
            "the provenance record is closed with the revision the writer committed, so it "
            "cannot be built from a result that carries no committed document"
        )
    record = materialization_provenance(layout)
    record["resulting_revision"] = str(revision)
    return record


__all__ = [
    "ANNOTATION_SUBJECT_FIELD",
    "CONNECTOR_ELEMENT_ROLE",
    "LABEL_ELEMENT_ROLE",
    "MATERIALIZATION_LAYER_ID",
    "SYMBOL_ELEMENT_ROLE",
    "SYMBOL_LABEL_ANNOTATION_ROLE",
    "LayoutIsNotFinalizedError",
    "MaterializationCanvasError",
    "MaterializationDocumentError",
    "MaterializationError",
    "M7_PROVENANCE_METADATA_KEY",
    "M7_PROVENANCE_REQUIRED_IDENTITIES",
    "MaterializationLabelError",
    "MaterializationProvenanceError",
    "MaterializationTargetNotEmptyError",
    "MaterializedLayout",
    "apply_materialized_layout",
    "document_rows",
    "materialization_digest",
    "materialization_matches_document",
    "materialization_payload",
    "materialization_provenance",
    "materialization_record",
    "materialize_canonical_layout",
    "label_text_for",
    "materialized_element_id",
    "materialized_transaction",
    "require_document_canvas",
    "require_empty_target",
    "with_materialization_provenance",
]
