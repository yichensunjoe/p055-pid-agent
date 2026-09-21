"""The coverage-extension track: the codes that had a repair strategy but no producer.

``repair_planner`` could already repair seven canonical codes that the frozen 72-case corpus
could not produce, so a claim like "F1/F2/F4/F5 cover them" was never true — it was assumed.
This module answers the question the way it has to be answered: **which of those codes can a
document in this repository actually contain?**

Two answers came back, and the second one is the interesting one:

* **Four codes are representable**, so they get a deterministic producer, a manifestation proof
  that the canonical validator really raises that exact code, and then the *same* runner, oracle
  and governance the frozen corpus uses.
* **Three codes are unreachable through every supported surface.** ``SYMBOL_DEFINITION_MISSING``,
  ``CONNECTOR_ENDPOINT_PORT_MISSING`` and ``CONNECTOR_ENDPOINT_POINT_MISMATCH`` cannot be brought
  into a document by either entry point this product has. ``probe_representability`` attempts
  both and records what each surface did — *refused* is not the same answer as *accepted and
  re-derived*, and the probe keeps them apart:

  * an unknown symbol key is refused by both,
  * an unknown port id is refused by both,
  * a stale binding is **silently re-derived** by the governed write path (the write succeeds, the
    binding is recomputed, the defect never exists) and refused by the import path
    (``stale_endpoint_binding``).

  So "this defect cannot be staged" is a machine-checked claim about the product's own surfaces,
  with the exact refusal text attached, rather than a fixture someone hand-wrote into the store.

That distinction is the point of the track. For those three codes the honest status is not
"untested coverage" but "rule present, strategy present, state unreachable through the product's
own entry surfaces" — the rules are defence in depth for data that arrived some other way, and
the planner keeps a strategy for it. If a future change ever makes one of them stageable, the
probe flips to ``reachable`` and the guard test in ``tests/test_repair_coverage_extension.py``
goes red until the code is promoted to a real case.

What this track deliberately is **not**:

* **Not part of the frozen corpus.** ``BENCHMARK_SPEC_VERSION`` stays at 2 and
  ``spec_fingerprint()`` is untouched, so the published 72-case evidence keeps its meaning. The
  extension has its own corpus id, its own case count and its own supported-code manifest.
* **Not a second validator truth.** Nothing here decides what counts as repaired: the record it
  produces is the ordinary :class:`RepairCaseRecord`, judged by the frozen oracle clauses.
* **Not a claim that the repair succeeds.** A code whose deterministic plan cannot resolve the
  defect is reported as ``not_repaired`` with its failure code, because a coverage matrix that
  lists only successes is a marketing document.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import Any

from .agent_semantic_models import ConnectPortsOperation, SafeDeleteElementOperation
from .models import (
    AddElementOperation,
    ConnectorElement,
    Point,
    SymbolElement,
    UpdateElementOperation,
)
from .repair_benchmark import (
    BENCHMARK_SPEC_VERSION,
    BaseDrawing,
    BenchmarkCase,
    MutationOperator,
    MutationResult,
    _apply_semantic,
    _patch,
    _symbol_label_annotations,
    build_base_drawing,
    derive_seed,
    generator_fingerprint,
    spec_fingerprint,
)
from .repair_benchmark import (
    _symbol as _benchmark_symbol,
)
from .repair_benchmark_runner import BenchmarkContext, _target_issue, run_case
from .repair_evidence import RepairCaseRecord, classify_case
from .repair_planner import DeterministicRepairPlanner, RepairPlanDraft
from .repair_scope import element_map
from .service import DocumentService, InvalidOperationError, ProjectIOError
from .validation_engine import run_validation

#: Bumped when a producer, a target binding or the acceptance reading of this track changes.
#: Independent of ``BENCHMARK_SPEC_VERSION`` on purpose: adding coverage does not change what
#: counts as passing, and the frozen corpus must not move when coverage grows.
EXTENSION_CORPUS_ID = "m5-coverage-extension"
EXTENSION_CORPUS_VERSION = "1"


@dataclass(frozen=True)
class CoverageEntry:
    """One strategy-present, representable code and the case that proves it."""

    code: str
    family: str
    validator_id: str
    operator_id: str
    #: How the target finding can be located, which decides what the case has to declare:
    #: ``element`` (the finding names the element), ``drawing`` (the finding is drawing-wide, so
    #: the declared element is the only binding there is).
    locator_kind: str
    note: str = ""


@dataclass(frozen=True)
class UnreachableEntry:
    """One strategy-present code that no supported entry surface can bring into a document."""

    code: str
    family: str
    validator_id: str
    #: Which write the probe attempts, described so a reader can reproduce it by hand.
    attempted_write: str
    note: str = ""


COVERAGE_ENTRIES: tuple[CoverageEntry, ...] = (
    CoverageEntry(
        code="DUPLICATE_LABEL",
        family="F1",
        validator_id="diagram-quality",
        operator_id="ext_duplicate_label",
        locator_kind="drawing",
        note=(
            "the free note repeats the visible label of HV-101 within the duplicate radius; the "
            "duplicate is a *visible* label, so no engineering-report code is involved"
        ),
    ),
    CoverageEntry(
        code="PORT_DIRECTION_MISMATCH",
        family="F2",
        validator_id="diagram-quality",
        operator_id="ext_port_direction_mismatch",
        locator_kind="element",
        note="the trunk's declared flow contradicts the port directions it is wired to",
    ),
    CoverageEntry(
        code="UNBRIDGED_CROSSING",
        family="F4",
        validator_id="diagram-quality",
        operator_id="ext_unbridged_crossing",
        locator_kind="element",
        note=(
            "the branch line is routed across the trunk with no jump bridge on either side; two "
            "lines that share a device are never reported as crossing, so the base carries a "
            "fourth device for the branch to run between"
        ),
    ),
    CoverageEntry(
        code="ANNOTATION_OVERLAP",
        family="F5",
        validator_id="diagram-quality",
        operator_id="ext_annotation_overlap",
        locator_kind="drawing",
        note="the isolated device's label annotation sits on top of the device it labels",
    ),
)

UNREACHABLE_ENTRIES: tuple[UnreachableEntry, ...] = (
    UnreachableEntry(
        code="SYMBOL_DEFINITION_MISSING",
        family="F1",
        validator_id="engineering-report",
        attempted_write="update a symbol's symbol_key to a key the catalog does not define",
        note="a catalogue that moved on is the scenario the rule guards",
    ),
    UnreachableEntry(
        code="CONNECTOR_ENDPOINT_PORT_MISSING",
        family="F2",
        validator_id="engineering-report",
        attempted_write="bind a connector endpoint to a port id the device does not define",
        note="a device library that changed under an existing drawing",
    ),
    UnreachableEntry(
        code="CONNECTOR_ENDPOINT_POINT_MISMATCH",
        family="F2",
        validator_id="engineering-report",
        attempted_write="leave a connector endpoint bound to a stale coordinate",
        note="equipment moving without its binding following",
    ),
)

BY_CODE: dict[str, CoverageEntry] = {entry.code: entry for entry in COVERAGE_ENTRIES}


# -- producers --------------------------------------------------------------- #
#
# Each producer stages exactly one defect through the governed write path and declares the one
# canonical finding it is buying. The manifestation proof separately validation-compares the
# drawing before and after, so a reviewer sees which findings the producer *added* rather than
# having to trust the operator's name.


def _base(service: DocumentService, registry) -> BaseDrawing:
    return build_base_drawing(
        service,
        registry,
        name="M5 coverage-extension base (three valves)",
        variant="three_valves",
    )


def _crossing_base(service: DocumentService, registry) -> BaseDrawing:
    """The three-valve base plus a fourth device and a branch line to it.

    A crossing between two lines that share a device is not a crossing in this model — the two
    ends meet at a junction — so the branch has to run between devices the trunk does not touch.
    Both the device and its line are added through the semantic compiler as part of the *base*
    drawing, exactly the way the three-valve base is built, which keeps the mutation itself a
    single re-route.
    """

    base = build_base_drawing(
        service,
        registry,
        name="M5 coverage-extension base (crossing)",
        variant="three_valves",
    )
    _apply_semantic(
        service,
        base.document_id,
        [
            AddElementOperation(
                element=_benchmark_symbol("v4", registry, "ball_valve", 400, 700, label="HV-104")
            ),
            ConnectPortsOperation(
                connector_id="q1",
                source_element_id="v4",
                source_port_id="out",
                target_element_id="v3",
                target_port_id="in",
                routing="orthogonal",
                process_tag="L-M5-004",
                medium="process",
                nominal_diameter="DN50",
                flow_direction="forward",
            ),
        ],
        "extension.crossing-base",
    )
    base.elements.update({"v4": "v4", "q1": "q1"})
    return base


def _element(service: DocumentService, document_id: str, element_id: str):
    element = element_map(service.get_document(document_id)).get(element_id)
    if element is None:
        raise AssertionError(f"{element_id} is not in the document")
    return element


def _connector(service: DocumentService, document_id: str, connector_id: str) -> ConnectorElement:
    element = _element(service, document_id, connector_id)
    if not isinstance(element, ConnectorElement):
        raise AssertionError(f"{connector_id} is not a connector")
    return element


def _symbol(service: DocumentService, document_id: str, element_id: str) -> SymbolElement:
    element = _element(service, document_id, element_id)
    if not isinstance(element, SymbolElement):
        raise AssertionError(f"{element_id} is not a symbol")
    return element


def _ext_duplicate_label(service, base, rng) -> MutationResult:
    """Make the free note repeat a device's visible label.

    ``DUPLICATE_LABEL`` counts *visible* labels that are also close together, so the note has to
    be both textually and spatially a duplicate. It is deliberately not a ``TAG_DUPLICATE``: the
    device's own tag is untouched, which is why this producer buys exactly one code.
    """

    from .annotation_layout import measure_annotation_quality

    note_id = base["note1"]
    label = str(_symbol(service, base.document_id, base["v1"]).properties.get("tag") or "")
    revision = _patch(service, base.document_id, note_id, {"text": label}, "ext-duplicate-label")
    quality = measure_annotation_quality(service.get_document(base.document_id), service.symbols)
    if quality.duplicate_label_count <= 0:
        raise AssertionError(
            f"{note_id} was made identical to a visible label but no duplicate was measured"
        )
    return MutationResult(
        operator_id="ext_duplicate_label",
        family="F1",
        target_code="DUPLICATE_LABEL",
        target_validator_id="diagram-quality",
        target_element_ids=[note_id],
        declared_scope_ids=[note_id],
        touched_budget_class="simple_metadata",
        mutation_revision=revision,
        notes=[
            f"the note now reads {label!r}, which is also the visible label of {base['v1']}",
            f"duplicate_label_count={quality.duplicate_label_count}",
        ],
    )


def _ext_port_direction_mismatch(service, base, rng) -> MutationResult:
    """Declare the trunk's flow opposite to the ports it is wired between."""

    connector_id = base["p1"]
    revision = _patch(
        service,
        base.document_id,
        connector_id,
        {"flow_direction": "reverse"},
        "ext-port-direction-mismatch",
    )
    return MutationResult(
        operator_id="ext_port_direction_mismatch",
        family="F2",
        target_code="PORT_DIRECTION_MISMATCH",
        target_validator_id="diagram-quality",
        target_element_ids=[connector_id],
        # The finding fires per connector and carries what the ports imply; pinning that makes the
        # binding the case's own fact.
        target_details={"inferred_flow_direction": "forward"},
        declared_scope_ids=[connector_id],
        touched_budget_class="simple_endpoint",
        mutation_revision=revision,
        notes=["the ports still determine flow 'forward'; only the declaration moved"],
    )


def _ext_unbridged_crossing(service, base, rng) -> MutationResult:
    """Re-route the branch line straight across the trunk, with no jump bridge on either side.

    The line is re-routed rather than added, so the case needs no create permission and the defect
    stays a geometry defect. Departure and approach stay port-outward and the declared flow keeps
    agreeing with the ports — this case is about a crossing, not about a broken exit. The detour
    costs more than three bends by construction (both ports are horizontal, so any path that
    crosses a horizontal trunk and returns to the ports has at least four), which the manifestation
    proof reports in ``added_codes`` rather than hides.
    """

    connector_id = base["q1"]
    connector = _connector(service, base.document_id, connector_id)
    start = connector.points[0]
    end = connector.points[-1]
    grid = max(5.0, service.get_document(base.document_id).canvas.grid_size)
    # The crossing column is derived from the trunk the branch has to cross, not hard-coded: the
    # vertical has to pass *through* the trunk, so its x must be strictly inside the trunk's span.
    # The route then climbs over the trunk and comes back down onto the target port from the left,
    # which is the only approach its outward normal allows.
    trunk = _connector(service, base.document_id, base["p1"])
    trunk_xs = [point.x for point in trunk.points]
    corridor = min(point.y for point in trunk.points) - 5.0 * grid
    column = min(max(start.x + 7.0 * grid, min(trunk_xs) + grid), max(trunk_xs) - grid)
    descent = end.x - 3.5 * grid
    route = [
        Point(x=start.x, y=start.y),
        Point(x=column, y=start.y),
        Point(x=column, y=corridor),
        Point(x=descent, y=corridor),
        Point(x=descent, y=end.y),
        Point(x=end.x, y=end.y),
    ]
    revision = _patch(
        service,
        base.document_id,
        connector_id,
        {"routing": "manual", "points": [point.model_dump(mode="json") for point in route]},
        "ext-unbridged-crossing-route",
    )
    return MutationResult(
        operator_id="ext_unbridged_crossing",
        family="F4",
        target_code="UNBRIDGED_CROSSING",
        target_validator_id="diagram-quality",
        target_element_ids=[connector_id],
        postconditions={"element_ids_preserved": [connector_id, base["p1"]]},
        declared_scope_ids=[connector_id],
        touched_budget_class="local_geometry",
        mutation_revision=revision,
        notes=[f"{connector_id} crosses {base['p1']} and neither side asks for a jump bridge"],
    )


def _ext_annotation_overlap(service, base, rng) -> MutationResult:
    """Put the isolated device's label annotation on top of the device itself."""

    element_id = base["v3"]
    annotations = _symbol_label_annotations(service, base.document_id, element_id)
    if not annotations:
        raise AssertionError(f"{element_id} has no generated label annotation to overlap")
    annotation_id = annotations[0]
    symbol = _symbol(service, base.document_id, element_id)
    revision = _patch(
        service,
        base.document_id,
        annotation_id,
        {"position": {"x": symbol.position.x, "y": symbol.position.y}},
        "ext-annotation-overlap",
    )
    return MutationResult(
        operator_id="ext_annotation_overlap",
        family="F5",
        target_code="ANNOTATION_OVERLAP",
        target_validator_id="diagram-quality",
        # The finding is drawing-wide: the case declares which annotation it broke, so the binding
        # is a fact about the case rather than about sort order.
        target_element_ids=[annotation_id],
        declared_scope_ids=[annotation_id, element_id],
        touched_budget_class="local_geometry",
        mutation_revision=revision,
        notes=[f"{annotation_id} now sits at the centre of {element_id}"],
    )


EXTENSION_MUTATIONS: dict[str, MutationOperator] = {
    "ext_duplicate_label": MutationOperator(
        "ext_duplicate_label",
        "F1",
        "DUPLICATE_LABEL",
        "diagram-quality",
        _ext_duplicate_label,
        document_builder=_base,
        touched_budget_class="simple_metadata",
    ),
    "ext_port_direction_mismatch": MutationOperator(
        "ext_port_direction_mismatch",
        "F2",
        "PORT_DIRECTION_MISMATCH",
        "diagram-quality",
        _ext_port_direction_mismatch,
        document_builder=_base,
        touched_budget_class="simple_endpoint",
    ),
    "ext_unbridged_crossing": MutationOperator(
        "ext_unbridged_crossing",
        "F4",
        "UNBRIDGED_CROSSING",
        "diagram-quality",
        _ext_unbridged_crossing,
        document_builder=_crossing_base,
        touched_budget_class="local_geometry",
    ),
    "ext_annotation_overlap": MutationOperator(
        "ext_annotation_overlap",
        "F5",
        "ANNOTATION_OVERLAP",
        "diagram-quality",
        _ext_annotation_overlap,
        document_builder=_base,
        touched_budget_class="local_geometry",
    ),
}


# -- cases and manifest ------------------------------------------------------ #


def extension_cases() -> list[BenchmarkCase]:
    """The extension corpus: one case per covered code, derived, never hand-picked."""

    cases: list[BenchmarkCase] = []
    for index, entry in enumerate(COVERAGE_ENTRIES):
        cases.append(
            BenchmarkCase(
                case_id=f"{EXTENSION_CORPUS_ID}:{entry.family}:{index:02d}:{entry.operator_id}",
                family=entry.family,
                operator_id=entry.operator_id,
                index=index,
                seed=derive_seed(
                    spec=EXTENSION_CORPUS_VERSION,
                    candidate_sha=EXTENSION_CORPUS_ID,
                    family=entry.family,
                    index=index,
                ),
                suite="coverage-extension",
            )
        )
    return cases


def coverage_manifest() -> dict[str, Any]:
    """The extension corpus's own manifest: what it adds, and what it must not move."""

    return {
        "corpus_id": EXTENSION_CORPUS_ID,
        "corpus_version": EXTENSION_CORPUS_VERSION,
        "spec_version": BENCHMARK_SPEC_VERSION,
        "spec_fingerprint": spec_fingerprint(),
        "generator_fingerprint": generator_fingerprint(),
        "case_count": len(COVERAGE_ENTRIES),
        "supported_codes": [
            {
                "code": entry.code,
                "family": entry.family,
                "validator_id": entry.validator_id,
                "operator_id": entry.operator_id,
                "locator_kind": entry.locator_kind,
                "representable": True,
            }
            for entry in COVERAGE_ENTRIES
        ],
        "unreachable_codes": [
            {
                "code": entry.code,
                "family": entry.family,
                "validator_id": entry.validator_id,
                "representable": False,
                "attempted_write": entry.attempted_write,
            }
            for entry in UNREACHABLE_ENTRIES
        ],
    }


#: Manifest keys that describe the *environment* rather than the corpus. ``generator_fingerprint``
#: digests CPython bytecode (``co_code`` of the base builder and every operator), so the identical
#: corpus hashed by 3.11 and by 3.12 publishes two different numbers: the first CI run of the
#: pushed commit computed ``073252f3…`` while the workstation computed ``c198eb77…``. A frozen
#: fingerprint that moves with the interpreter would make "frozen" mean "frozen on this machine",
#: so these two stay published but are not part of the corpus identity — the same rule the evidence
#: layer already applies to its volatile fields.
ENVIRONMENT_DERIVED_KEYS: tuple[str, ...] = ("spec_fingerprint", "generator_fingerprint")


def coverage_fingerprint() -> str:
    """The published fingerprint: the whole manifest, environment-derived digests included."""

    return payload_hash(coverage_manifest())


def coverage_corpus_manifest() -> dict[str, Any]:
    """The manifest without the fields that only describe the interpreter running it."""

    return {
        key: value
        for key, value in coverage_manifest().items()
        if key not in ENVIRONMENT_DERIVED_KEYS
    }


def coverage_corpus_digest() -> str:
    """The corpus identity: identical on every interpreter, so a change here is a coverage change."""

    return payload_hash(coverage_corpus_manifest())


def payload_hash(payload: dict[str, Any]) -> str:
    """Canonical hash of a JSON payload, so a published report can be checked by recomputation.

    The repair evidence hashes its pydantic models with ``repair_digest``; this track publishes
    plain dicts, so it applies the *same* rule itself rather than pretending a dict is a model:
    the fields that only describe the machine on the day (``REPAIR_VOLATILE_FIELDS``: wall clock,
    timings, token counts, document and audit ids, verification hashes) are published but not
    hashed, so the hash answers "is this the same run's result" instead of "was this run as fast".
    """

    return hashlib.sha256(canonical_payload_json(payload).encode("utf-8")).hexdigest()


def canonical_payload_json(payload: Any) -> str:
    """Payload without its volatile fields, serialised canonically."""

    from .repair_models import REPAIR_VOLATILE_FIELDS

    def _strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: _strip(item)
                for key, item in sorted(value.items())
                if key not in REPAIR_VOLATILE_FIELDS
            }
        if isinstance(value, list):
            return [_strip(item) for item in value]
        return value

    return json.dumps(_strip(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# -- representability -------------------------------------------------------- #


@dataclass
class SurfaceAttempt:
    """One entry surface's answer, which is not the same question as "did it raise?".

    A surface can refuse the defect outright, or it can accept the write and quietly re-derive the
    value so the defect never exists. Those are different behaviours and they are recorded
    separately: only ``defect_present`` decides reachability, and the canonical validator decides
    ``defect_present``.
    """

    surface: str
    accepted: bool
    refusal: str
    defect_present: bool
    finding: dict[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "accepted": self.accepted,
            "refusal": self.refusal,
            "defect_present": self.defect_present,
            "finding": self.finding,
        }


@dataclass
class RepresentationProbe:
    """One code's answer to "can a document in this repository even contain it?"."""

    code: str
    family: str
    validator_id: str
    attempted_write: str
    attempts: list[SurfaceAttempt]
    note: str = ""

    @property
    def reachable(self) -> bool:
        """Reachable the moment *either* surface leaves a document carrying the defect."""

        return any(attempt.defect_present for attempt in self.attempts)

    def as_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "family": self.family,
            "validator_id": self.validator_id,
            "attempted_write": self.attempted_write,
            "reachable": self.reachable,
            "attempts": [attempt.as_payload() for attempt in self.attempts],
            "note": self.note,
        }


def _document_payload(service: DocumentService, document_id: str) -> dict[str, Any]:
    envelope = service.export_document_envelope(document_id)
    payload = envelope.model_dump(mode="json") if hasattr(envelope, "model_dump") else envelope
    return json.loads(json.dumps(payload))


def _probe_payload_mutation(code: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Edit the exported payload the way a foreign or older file would have arrived."""

    document = payload.get("document", payload)
    elements = document["elements"]
    if code == "SYMBOL_DEFINITION_MISSING":
        for element in elements:
            if element["id"] == "v3":
                element["symbol_key"] = "ext_symbol_not_in_catalog"
                return payload
        raise AssertionError("v3 not found in the exported payload")
    if code == "CONNECTOR_ENDPOINT_PORT_MISSING":
        for element in elements:
            if element["id"] == "p1":
                element["target"]["port_id"] = "ext_no_such_port"
                return payload
        raise AssertionError("p1 not found in the exported payload")
    if code == "CONNECTOR_ENDPOINT_POINT_MISMATCH":
        for element in elements:
            if element["id"] == "p1":
                target = element["target"]
                stale = {"x": target["point"]["x"] + 30.0, "y": target["point"]["y"] + 40.0}
                target["point"] = dict(stale)
                # A file whose *route* moved with its binding: the binding is stale against the
                # port, not against the route, which is the defect this code names.
                element["points"][-1] = dict(stale)
                return payload
        raise AssertionError("p1 not found in the exported payload")
    raise AssertionError(f"no probe payload mutation for {code}")


def _probe_writes(code: str, service: DocumentService, base: BaseDrawing) -> None:
    if code == "SYMBOL_DEFINITION_MISSING":
        _patch(
            service,
            base.document_id,
            base["v3"],
            {"symbol_key": "ext_symbol_not_in_catalog"},
            "probe-symbol-definition",
        )
        return
    if code == "CONNECTOR_ENDPOINT_PORT_MISSING":
        connector = _connector(service, base.document_id, base["p1"])
        target = connector.target.model_dump(mode="json")
        target["port_id"] = "ext_no_such_port"
        _patch(service, base.document_id, base["p1"], {"target": target}, "probe-port-missing")
        return
    if code == "CONNECTOR_ENDPOINT_POINT_MISMATCH":
        connector = _connector(service, base.document_id, base["p1"])
        target = connector.target.model_dump(mode="json")
        target["point"] = {"x": target["point"]["x"] + 30.0, "y": target["point"]["y"] + 40.0}
        _patch(service, base.document_id, base["p1"], {"target": target}, "probe-point-mismatch")
        return
    raise AssertionError(f"no write probe for {code}")


def _look_for_code(
    context: BenchmarkContext, document_id: str, code: str, validator_id: str
) -> dict[str, Any] | None:
    """Ask the canonical validator, on a document that now exists, for the code in question."""

    result = run_validation(
        context.service.get_document(document_id),
        context.registry,
        context.profile,
        service=context.service,
    )
    for finding in result.issues:
        if finding.code == code and finding.validator_id == validator_id and not finding.is_waived:
            return finding.model_dump(mode="json")
    return None


def probe_representability(context: BenchmarkContext) -> list[RepresentationProbe]:
    """Try to *make* each unreachable defect, through the write surface and the import surface.

    Both are supported entry points for a document in this product, so a defect that neither of
    them can leave behind cannot exist in a document being edited. The interesting distinction is
    kept: the write path may *refuse* the defect, or it may *accept and re-derive* it away, and the
    two say different things about how defensible the claim is. Either way the canonical validator
    is what decides whether the defect is there.
    """

    probes: list[RepresentationProbe] = []
    for entry in UNREACHABLE_ENTRIES:
        base = _base(context.service, context.registry)
        write_accepted = True
        write_refusal = ""
        try:
            _probe_writes(entry.code, context.service, base)
        except (InvalidOperationError, AssertionError) as exc:
            write_accepted = False
            write_refusal = f"{type(exc).__name__}: {exc}"
        write_finding = (
            _look_for_code(context, base.document_id, entry.code, entry.validator_id)
            if write_accepted
            else None
        )

        source = _base(context.service, context.registry)
        payload = _probe_payload_mutation(
            entry.code, _document_payload(context.service, source.document_id)
        )
        import_accepted = True
        import_refusal = ""
        import_finding: dict[str, Any] | None = None
        try:
            imported = context.service.import_document_payload(payload, conflict_policy="regenerate")
            for document in imported.documents:
                found = _look_for_code(context, document.id, entry.code, entry.validator_id)
                if found is not None:
                    import_finding = found
                    break
        except (ProjectIOError, AssertionError) as exc:
            import_accepted = False
            import_refusal = f"{type(exc).__name__}: {exc}"

        probes.append(
            RepresentationProbe(
                code=entry.code,
                family=entry.family,
                validator_id=entry.validator_id,
                attempted_write=entry.attempted_write,
                attempts=[
                    SurfaceAttempt(
                        surface="governed_write",
                        accepted=write_accepted,
                        refusal=write_refusal,
                        defect_present=write_finding is not None,
                        finding=write_finding,
                    ),
                    SurfaceAttempt(
                        surface="import",
                        accepted=import_accepted,
                        refusal=import_refusal,
                        defect_present=import_finding is not None,
                        finding=import_finding,
                    ),
                ],
                note=entry.note,
            )
        )
    return probes


# -- manifestation proof ----------------------------------------------------- #


@dataclass
class Manifestation:
    """Did the producer really make the canonical validator raise the declared code?"""

    code: str
    validator_id: str
    base_codes: list[str]
    produced_codes: list[str]
    added_codes: list[str]
    target_present: bool
    target_finding: dict[str, Any] | None
    base_revision: int
    revision: int
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.target_present

    def as_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "validator_id": self.validator_id,
            "produced": self.ok,
            "base_codes": self.base_codes,
            "produced_codes": self.produced_codes,
            "added_codes": self.added_codes,
            "target_finding": self.target_finding,
            "base_revision": self.base_revision,
            "revision": self.revision,
            "notes": list(self.notes),
        }


def prove_manifestation(
    context: BenchmarkContext,
    case: BenchmarkCase,
    *,
    operators: dict[str, MutationOperator] | None = None,
) -> Manifestation:
    """Run one producer and look for its code with the canonical validator, not with its name."""

    catalogue = EXTENSION_MUTATIONS if operators is None else operators
    operator = catalogue[case.operator_id]
    builder = operator.document_builder or build_base_drawing
    base = builder(context.service, context.registry)
    pristine = context.service.get_document(base.document_id)
    before = run_validation(pristine, context.registry, context.profile, service=context.service)
    mutation: MutationResult = operator.apply(context.service, base, random.Random(case.seed))
    after = run_validation(
        context.service.get_document(base.document_id),
        context.registry,
        context.profile,
        service=context.service,
    )
    target = _target_issue(
        mutation.target_code,
        mutation.target_validator_id,
        mutation.target_element_ids,
        after.issues,
        mutation.target_details,
    )
    base_codes = sorted(finding.code for finding in before.issues if not finding.is_waived)
    produced_codes = sorted(finding.code for finding in after.issues if not finding.is_waived)
    return Manifestation(
        code=mutation.target_code,
        validator_id=mutation.target_validator_id,
        base_codes=base_codes,
        produced_codes=produced_codes,
        added_codes=_multiset_difference(produced_codes, base_codes),
        target_present=target is not None,
        target_finding=target.model_dump(mode="json") if target is not None else None,
        base_revision=pristine.revision,
        revision=context.service.get_document(base.document_id).revision,
        notes=list(mutation.notes),
    )


def _multiset_difference(left: list[str], right: list[str]) -> list[str]:
    remaining: dict[str, int] = {}
    for code in right:
        remaining[code] = remaining.get(code, 0) + 1
    added: list[str] = []
    for code in left:
        if remaining.get(code, 0) > 0:
            remaining[code] -= 1
            continue
        added.append(code)
    return sorted(added)


# -- the track --------------------------------------------------------------- #


@dataclass
class CoverageRow:
    code: str
    family: str
    validator_id: str
    operator_id: str
    locator_kind: str
    manifestation: Manifestation
    case: RepairCaseRecord
    status: str
    notes: list[str] = field(default_factory=list)

    def as_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "family": self.family,
            "validator_id": self.validator_id,
            "operator_id": self.operator_id,
            "locator_kind": self.locator_kind,
            "status": self.status,
            "manifestation": self.manifestation.as_payload(),
            "case": self.case.model_dump(mode="json", by_alias=True),
            "notes": list(self.notes),
        }


@dataclass
class CoverageReport:
    manifest: dict[str, Any]
    fingerprint: str
    rows: list[CoverageRow]
    probes: list[RepresentationProbe]

    @property
    def covered(self) -> list[str]:
        return sorted(row.code for row in self.rows if row.status == "covered")

    @property
    def uncovered(self) -> list[str]:
        return sorted(row.code for row in self.rows if row.status != "covered")

    @property
    def unreachable(self) -> list[str]:
        return sorted(probe.code for probe in self.probes if not probe.reachable)

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "corpus": self.manifest,
            "corpus_fingerprint": self.fingerprint,
            "rows": [row.as_payload() for row in self.rows],
            "covered": self.covered,
            "uncovered": self.uncovered,
            "representability": [probe.as_payload() for probe in self.probes],
            "unreachable": self.unreachable,
        }
        return {**payload, "report_hash": payload_hash(payload)}


def classify_row(manifestation: Manifestation, case: RepairCaseRecord) -> str:
    """The three honest outcomes of a coverage case.

    ``not_manifested`` is what keeps the matrix from lying: if the producer could not make the
    canonical validator raise the code, the code is *not* covered, however green the rest of the
    case looks. ``covered`` requires both halves — the defect was real and the governed repair
    resolved it — and neither half is inferred from the producer's name.
    """

    if not manifestation.ok:
        return "not_manifested"
    if classify_case(case) == "success":
        return "covered"
    return "not_repaired"


def run_coverage_extension(
    context: BenchmarkContext,
    *,
    planner=None,
    candidate_sha: str = EXTENSION_CORPUS_ID,
) -> CoverageReport:
    """Run every extension case through the *same* runner the frozen corpus uses."""

    rows: list[CoverageRow] = []
    for case in extension_cases():
        operator = EXTENSION_MUTATIONS[case.operator_id]
        entry = BY_CODE[operator.target_code]
        manifestation = prove_manifestation(context, case)
        record = run_case(
            context,
            case,
            planner=planner,
            candidate_sha=candidate_sha,
            operators=EXTENSION_MUTATIONS,
        )
        notes = [entry.note]
        if record.failure_code:
            notes.append(f"failure: {record.failure_code}")
        if record.reasons:
            notes.append(f"reason: {record.reasons[0]}")
        rows.append(
            CoverageRow(
                code=entry.code,
                family=entry.family,
                validator_id=entry.validator_id,
                operator_id=entry.operator_id,
                locator_kind=entry.locator_kind,
                manifestation=manifestation,
                case=record,
                status=classify_row(manifestation, record),
                notes=notes,
            )
        )
    return CoverageReport(
        manifest=coverage_manifest(),
        fingerprint=coverage_fingerprint(),
        rows=rows,
        probes=probe_representability(context),
    )


# -- negative capability ----------------------------------------------------- #
#
# The review asked for the gate to go red on a *broken* producer or a *cheating* plan, not only
# green on a good one. Each control below breaks one link in the chain on purpose and records
# which link reported it.


@dataclass
class NegativeControl:
    control: str
    expected_signal: str
    observed_signal: str
    red: bool
    detail: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "control": self.control,
            "expected_signal": self.expected_signal,
            "observed_signal": self.observed_signal,
            "red": self.red,
            "detail": self.detail,
        }


def _benign_operator(declared_code: str, declared_validator: str, apply) -> MutationOperator:
    """A producer that writes through the governed path but does not stage what its case declares.

    ``apply`` is the real producer for the control that stages the *wrong* code, and a harmless
    write for the control that stages nothing: either way the declared code is the claim under
    test, and the manifestation proof is what has to refuse it.
    """

    return MutationOperator(
        "negative_control",
        "F1",
        declared_code,
        declared_validator,
        apply,
        document_builder=_base,
        touched_budget_class="simple_metadata",
    )


def _benign_write(service, base, rng) -> MutationResult:
    """A real governed write that leaves the drawing free of the declared defect."""

    revision = _patch(
        service,
        base.document_id,
        base["note1"],
        {"text": "negative control: nothing is broken here"},
        "negative-control-benign-write",
    )
    return MutationResult(
        operator_id="negative_control",
        family="F1",
        target_code="DUPLICATE_LABEL",
        target_validator_id="diagram-quality",
        target_element_ids=[base["note1"]],
        mutation_revision=revision,
    )


def _negative_case(case_id: str, case: BenchmarkCase) -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id,
        family=case.family,
        operator_id="negative_control",
        index=case.index,
        seed=case.seed,
        suite="coverage-extension-negative",
    )


def run_negative_controls(context: BenchmarkContext) -> list[NegativeControl]:
    """Prove the coverage gate can go red: broken producer, wrong code, cheating plan."""

    controls: list[NegativeControl] = []
    case = next(item for item in extension_cases() if item.operator_id == "ext_duplicate_label")

    # 1. The producer writes but stages nothing: the manifestation proof must fail, and the row
    #    must not be counted as coverage.
    silent = _benign_operator("DUPLICATE_LABEL", "diagram-quality", _benign_write)
    silent_case = _negative_case("negative:producer-stages-nothing", case)
    silent_catalogue = {**EXTENSION_MUTATIONS, "negative_control": silent}
    silent_manifestation = prove_manifestation(context, silent_case, operators=silent_catalogue)
    silent_record = run_case(
        context, silent_case, candidate_sha="negative-control", operators=silent_catalogue
    )
    controls.append(
        NegativeControl(
            control="producer_stages_nothing",
            expected_signal="not_manifested",
            observed_signal=classify_row(silent_manifestation, silent_record),
            red=(
                not silent_manifestation.ok
                and classify_row(silent_manifestation, silent_record) == "not_manifested"
            ),
            detail=f"target_present={silent_manifestation.target_present}",
        )
    )

    # 2. The producer stages a real defect but declares a different code: the exact-code check is
    #    what fails, not the write. The defect is real (the same write as the covered case), only
    #    the claim moved, so a name-based check would happily pass it.
    def mislabelled(service, base, rng) -> MutationResult:
        result = EXTENSION_MUTATIONS["ext_duplicate_label"].apply(service, base, rng)
        result.target_code = "NODE_OVERLAP"
        result.target_validator_id = "diagram-quality"
        return result

    wrong = _benign_operator("NODE_OVERLAP", "diagram-quality", mislabelled)
    wrong_case = _negative_case("negative:producer-declares-wrong-code", case)
    wrong_catalogue = {**EXTENSION_MUTATIONS, "negative_control": wrong}
    wrong_manifestation = prove_manifestation(context, wrong_case, operators=wrong_catalogue)
    controls.append(
        NegativeControl(
            control="producer_declares_wrong_code",
            expected_signal="not_manifested",
            observed_signal=classify_row(
                wrong_manifestation,
                run_case(
                    context, wrong_case, candidate_sha="negative-control", operators=wrong_catalogue
                ),
            ),
            red=not wrong_manifestation.target_present,
            detail=f"declared=NODE_OVERLAP added={wrong_manifestation.added_codes}",
        )
    )

    # 3. A plan that resolves the target by touching an element outside the frozen scope must be
    #    refused for locality, not accepted because the finding went away.
    class OutOfScopePlanner(DeterministicRepairPlanner):
        planner_id = "negative-control-out-of-scope"

        def plan(self, repair_context) -> RepairPlanDraft:
            draft = super().plan(repair_context)
            return RepairPlanDraft(
                operations=[
                    *draft.operations,
                    # A neutral edit — a line number that changes nothing else — so the clause
                    # that reports is locality and not a collateral finding the edit introduced.
                    UpdateElementOperation(
                        element_id="p2", patch={"process_tag": "L-CONTROL-999"}
                    ),
                ],
                rationale="negative control: resolve the target, then edit outside the scope",
                source="test-double",
            )

    out_of_scope = run_case(
        context,
        case,
        planner=OutOfScopePlanner(),
        candidate_sha="negative-control",
        operators=EXTENSION_MUTATIONS,
    )
    # The run-level failure is "the attempt budget ran out", because the control planner is
    # deterministic and will produce the same refused plan every time. The refusal itself is the
    # per-attempt code, which is where the locality clause reports.
    out_of_scope_codes = set(out_of_scope.attempt_failure_codes)
    controls.append(
        NegativeControl(
            control="planner_edits_outside_scope",
            expected_signal="locality_violation",
            observed_signal=",".join(sorted(out_of_scope_codes)) or out_of_scope.failure_code,
            red=(
                "locality_violation" in out_of_scope_codes
                and classify_case(out_of_scope) != "success"
            ),
            detail="; ".join(out_of_scope.reasons[:1]),
        )
    )

    # 4. A plan that makes the finding disappear by deleting the element it names is not a repair.
    class DeletingPlanner(DeterministicRepairPlanner):
        planner_id = "negative-control-delete-target"

        def plan(self, repair_context) -> RepairPlanDraft:
            return RepairPlanDraft(
                operations=[
                    SafeDeleteElementOperation(
                        element_id=repair_context.request.target.element_ids[0]
                    )
                ],
                rationale="negative control: delete the finding instead of repairing it",
                source="test-double",
            )

    deleting = run_case(
        context,
        case,
        planner=DeletingPlanner(),
        candidate_sha="negative-control",
        operators=EXTENSION_MUTATIONS,
    )
    controls.append(
        NegativeControl(
            control="planner_deletes_the_target",
            expected_signal="deletion_not_permitted",
            observed_signal=deleting.failure_code or classify_case(deleting),
            red=deleting.failure_code == "deletion_not_permitted",
            detail="; ".join(deleting.reasons[:1]),
        )
    )
    return controls


def extension_payload(context: BenchmarkContext, *, planner=None) -> dict[str, Any]:
    """The whole track in one reviewable payload: matrix, cases and negative controls."""

    report = run_coverage_extension(context, planner=planner)
    controls = run_negative_controls(context)
    # The matrix's own hash is dropped before the whole report is hashed, so the published report
    # carries exactly one hash and that hash covers every byte the reader gets.
    matrix = report.as_payload()
    matrix.pop("report_hash", None)
    payload: dict[str, Any] = {
        **matrix,
        "negative_controls": [control.as_payload() for control in controls],
        "negative_controls_all_red": all(control.red for control in controls),
    }
    return {**payload, "report_hash": payload_hash(payload)}


__all__ = [
    "BY_CODE",
    "COVERAGE_ENTRIES",
    "EXTENSION_CORPUS_ID",
    "EXTENSION_CORPUS_VERSION",
    "EXTENSION_MUTATIONS",
    "UNREACHABLE_ENTRIES",
    "CoverageEntry",
    "CoverageReport",
    "CoverageRow",
    "Manifestation",
    "NegativeControl",
    "RepresentationProbe",
    "SurfaceAttempt",
    "UnreachableEntry",
    "classify_row",
    "canonical_payload_json",
    "ENVIRONMENT_DERIVED_KEYS",
    "coverage_corpus_digest",
    "coverage_corpus_manifest",
    "coverage_fingerprint",
    "coverage_manifest",
    "extension_cases",
    "extension_payload",
    "payload_hash",
    "probe_representability",
    "prove_manifestation",
    "run_coverage_extension",
    "run_negative_controls",
]
