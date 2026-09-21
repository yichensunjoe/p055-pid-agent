"""The coverage ledger: where every strategy-present code ended up.

``repair_planner`` could already repair seven canonical codes the frozen 72-case corpus could not
produce, so a claim like "F1/F2/F4/F5 cover them" was never true — it was assumed. The
coverage-extension track answered that with evidence rather than assertion, and split the seven in
two:

* **Four codes are representable.** ``DUPLICATE_LABEL``, ``PORT_DIRECTION_MISMATCH``,
  ``UNBRIDGED_CROSSING`` and ``ANNOTATION_OVERLAP`` each got a deterministic producer, a
  manifestation proof that the canonical validator really raises that exact code, and a governed
  repair through the *same* runner, oracle and governance the frozen corpus uses.
* **Three codes are unreachable through every supported surface.** ``SYMBOL_DEFINITION_MISSING``,
  ``CONNECTOR_ENDPOINT_PORT_MISSING`` and ``CONNECTOR_ENDPOINT_POINT_MISMATCH`` cannot be brought
  into a document by either entry point this product has.

The remote ruling (§③, "coverage-promotion") then named what to do with each half: the four
reachable ones are **promoted** into the frozen catalogue as real cases, the three unreachable ones
become a **disposition ledger** that keeps the mechanical proof. This module is the record of both
halves, and it is a *check* rather than a note:

* for every promoted code it finds the case the frozen generator derives for that code's operator,
  runs it through the ordinary runner, and requires the canonical validator to raise the exact code
  and the oracle to resolve it — so a promotion that quietly stopped producing its defect goes red
  here, not silently missing from a matrix;
* for every unreachable code it re-runs both probes and requires the defect to still be absent.

The staging area this module used to be — its own corpus, its own four cases — is gone on purpose.
Keeping it would have meant two corpora claiming the same four codes, and the promotion's whole
point is that those codes now belong to the frozen gate. What is *not* gone is the published
identity of that staging area: it was quoted to the remote as ``coverage_corpus_digest =
3dc8ca1a…`` and ``report_hash = f56c2c50…``, and
:data:`SUPERSEDED_EXTENSION_IDENTITIES` keeps both as recorded history, because a number that was
published cannot be recomputed after the code that produced it has changed, and pretending
otherwise is how a project loses its audit trail.

What this module deliberately is **not**:

* **Not a second validator truth.** Nothing here decides what counts as repaired: every record it
  produces is an ordinary :class:`RepairCaseRecord`, judged by the frozen oracle clauses.
* **Not a claim that the repair succeeds.** A promoted code whose deterministic plan cannot resolve
  the defect is reported as ``not_repaired`` with its failure code, because a coverage matrix that
  lists only successes is a marketing document.
* **Not a gate on the frozen corpus.** ``repair-benchmark`` is the gate; this track reports the
  disposition of codes the corpus does *not* own, and proves the promoted codes are still owned.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import Any

from .agent_semantic_models import SafeDeleteElementOperation
from .models import UpdateElementOperation
from .repair_benchmark import (
    ACCEPTANCE_CASES_PER_FAMILY,
    BENCHMARK_SPEC_VERSION,
    CORE_CORPUS_ID,
    CORE_CORPUS_VERSION,
    MUTATIONS,
    BaseDrawing,
    BenchmarkCase,
    MutationOperator,
    MutationResult,
    _patch,
    _three_valves_base,
    build_base_drawing,
    generate_cases,
    generator_fingerprint,
    spec_fingerprint,
)
from .repair_benchmark_runner import BenchmarkContext, _target_issue, run_case
from .repair_evidence import RepairCaseRecord, classify_case
from .repair_planner import DeterministicRepairPlanner, RepairPlanDraft
from .service import DocumentService, InvalidOperationError, ProjectIOError
from .validation_engine import run_validation

#: The ledger's own identity. Independent of ``BENCHMARK_SPEC_VERSION`` and of
#: ``CORE_CORPUS_VERSION`` on purpose: this file records *dispositions*, and a disposition table
#: that moved every time the corpus grew would not be a record of anything.
LEDGER_ID = "m5-coverage-ledger"
LEDGER_VERSION = "1"

#: The candidate every ledger case is derived for when the caller does not name one. The promoted
#: codes are carried by the frozen generator, so their case ids and seeds move with the candidate;
#: the ledger only needs *a* frozen case that owns each code, and it says which one it used.
DEFAULT_LEDGER_CANDIDATE = "coverage-ledger"


@dataclass(frozen=True)
class PromotedEntry:
    """One strategy-present code that the coverage promotion handed to the frozen corpus."""

    code: str
    family: str
    validator_id: str
    #: The operator the code is now carried by, read from the frozen catalogue rather than trusted:
    #: ``test_repair_coverage_ledger`` asserts the catalogue really defines it under this family
    #: and really declares this code.
    operator_id: str
    #: How the target finding can be located, which decides what the case has to declare:
    #: ``element`` (the finding names the element), ``drawing`` (the finding is drawing-wide, so
    #: the declared element is the only binding there is).
    locator_kind: str
    #: The staging operator this code arrived under, so the promotion is traceable back to the
    #: evidence that justified it. Not an operator any more: it exists only in the published
    #: ``reports/m5/repair-coverage-extension.json``.
    staged_by: str
    note: str = ""


@dataclass(frozen=True)
class UnreachableEntry:
    """One strategy-present code that no supported entry surface can bring into a document."""

    code: str
    family: str
    validator_id: str
    #: The formal disposition. These are the remote's three named outcomes (§③), not free text:
    #: a reader can grep for them, and the guard test asserts the set.
    disposition: str
    #: Which write the probe attempts, described so a reader can reproduce it by hand.
    attempted_write: str
    note: str = ""


PROMOTED_ENTRIES: tuple[PromotedEntry, ...] = (
    PromotedEntry(
        code="DUPLICATE_LABEL",
        family="F1",
        validator_id="diagram-quality",
        operator_id="f1_duplicate_label",
        locator_kind="drawing",
        staged_by="ext_duplicate_label",
        note=(
            "the free note repeats the visible label of HV-101 within the duplicate radius; the "
            "duplicate is a *visible* label, so no engineering-report code is involved"
        ),
    ),
    PromotedEntry(
        code="PORT_DIRECTION_MISMATCH",
        family="F2",
        validator_id="diagram-quality",
        operator_id="f2_port_direction_mismatch",
        locator_kind="element",
        staged_by="ext_port_direction_mismatch",
        note="the trunk's declared flow contradicts the port directions it is wired to",
    ),
    PromotedEntry(
        code="UNBRIDGED_CROSSING",
        family="F4",
        validator_id="diagram-quality",
        operator_id="f4_unbridged_crossing",
        locator_kind="element",
        staged_by="ext_unbridged_crossing",
        note=(
            "the branch line is routed across the trunk with no jump bridge on either side; two "
            "lines that share a device are never reported as crossing, so the base carries a "
            "fourth device for the branch to run between"
        ),
    ),
    PromotedEntry(
        code="ANNOTATION_OVERLAP",
        family="F5",
        validator_id="diagram-quality",
        operator_id="f5_annotation_overlap",
        locator_kind="drawing",
        staged_by="ext_annotation_overlap",
        note="the isolated device's label annotation sits on top of the device it labels",
    ),
)

#: The remote's names for the two ways a supported surface prevents a state from existing. They are
#: deliberately different from each other: ``unreachable`` means the surface refuses the write,
#: ``normalized_or_rejected`` means the write *succeeds* and the value is re-derived so the defect
#: never lands — or the import surface refuses the file that carries it. Collapsing the two would
#: hide the finding that motivated the distinction.
UNREACHABLE_AT_SUPPORTED_INGRESS = "unreachable_at_supported_ingress"
NORMALIZED_OR_REJECTED_AT_SUPPORTED_INGRESS = "normalized_or_rejected_at_supported_ingress"

UNREACHABLE_ENTRIES: tuple[UnreachableEntry, ...] = (
    UnreachableEntry(
        code="SYMBOL_DEFINITION_MISSING",
        family="F1",
        validator_id="engineering-report",
        disposition=UNREACHABLE_AT_SUPPORTED_INGRESS,
        attempted_write="update a symbol's symbol_key to a key the catalog does not define",
        note="a catalogue that moved on is the scenario the rule guards",
    ),
    UnreachableEntry(
        code="CONNECTOR_ENDPOINT_PORT_MISSING",
        family="F2",
        validator_id="engineering-report",
        disposition=UNREACHABLE_AT_SUPPORTED_INGRESS,
        attempted_write="bind a connector endpoint to a port id the device does not define",
        note="a device library that changed under an existing drawing",
    ),
    UnreachableEntry(
        code="CONNECTOR_ENDPOINT_POINT_MISMATCH",
        family="F2",
        validator_id="engineering-report",
        disposition=NORMALIZED_OR_REJECTED_AT_SUPPORTED_INGRESS,
        attempted_write="leave a connector endpoint bound to a stale coordinate",
        note=(
            "equipment moving without its binding following; the governed write accepts it and "
            "re-derives the point, so the defect never lands, and the import path refuses the file"
        ),
    ),
)

BY_CODE: dict[str, PromotedEntry] = {entry.code: entry for entry in PROMOTED_ENTRIES}

#: Identities published by the staging area that the promotion retired. They are recorded and not
#: recomputed: ``coverage_fingerprint`` hashed CPython bytecode, and both hashes covered a corpus
#: and a producer set that no longer exist. This is the same treatment the remote gave
#: ``generator_fingerprint`` — a fact about one runtime and one revision, kept as a fact.
SUPERSEDED_EXTENSION_IDENTITIES: dict[str, Any] = {
    "corpus_id": "m5-coverage-extension",
    "corpus_version": "1",
    "case_count": 4,
    #: The cross-interpreter corpus identity the remote named as the extension's formal one.
    "coverage_corpus_digest": "3dc8ca1ade6c5adbe5f8c3a5fab8709eff3ffdeb4f904a4da4fd8f7ae6b19969",
    #: The published report hash, recomputable only from the published payload.
    "coverage_report_hash": "f56c2c5098119a0b753660ba6a33a0fb7bcc03c24a545f0fd1849f6e44fe0931",
    #: The full fingerprint, which is interpreter-bound, recorded per interpreter as published.
    "coverage_fingerprint_by_interpreter": {
        "3.11": "073252f38b4a52c125c1b8bd3fbc2faaa60981c8937f298858a9af4cc18ab8c0",
        "3.12": "c198eb77c73157b25eba537fd3fef4264110897f23325d30b37c2bad9d6095e4",
    },
    "superseded_by": {
        "corpus_id": CORE_CORPUS_ID,
        "corpus_version": CORE_CORPUS_VERSION,
        "spec_version": BENCHMARK_SPEC_VERSION,
        "ledger_id": LEDGER_ID,
        "ledger_version": LEDGER_VERSION,
    },
}


# -- the ledger manifest ----------------------------------------------------- #


def ledger_manifest() -> dict[str, Any]:
    """What this ledger claims, in one reviewable object."""

    return {
        "ledger_id": LEDGER_ID,
        "ledger_version": LEDGER_VERSION,
        "spec_version": BENCHMARK_SPEC_VERSION,
        "spec_fingerprint": spec_fingerprint(),
        "generator_fingerprint": generator_fingerprint(),
        "corpus_id": CORE_CORPUS_ID,
        "corpus_version": CORE_CORPUS_VERSION,
        "promoted_count": len(PROMOTED_ENTRIES),
        "unreachable_count": len(UNREACHABLE_ENTRIES),
        "promoted": [
            {
                "code": entry.code,
                "family": entry.family,
                "validator_id": entry.validator_id,
                "operator_id": entry.operator_id,
                "locator_kind": entry.locator_kind,
                "staged_by": entry.staged_by,
            }
            for entry in PROMOTED_ENTRIES
        ],
        "dispositions": [
            {
                "code": entry.code,
                "family": entry.family,
                "validator_id": entry.validator_id,
                "disposition": entry.disposition,
                "attempted_write": entry.attempted_write,
            }
            for entry in UNREACHABLE_ENTRIES
        ],
    }


#: Manifest keys that describe the *environment* rather than the ledger. Same rule the corpus
#: identity already uses: ``generator_fingerprint`` digests CPython bytecode (``co_code``), so it
#: moves with the interpreter and cannot be part of an identity two machines are supposed to agree
#: on. Both stay published; only the digest is the identity.
ENVIRONMENT_DERIVED_KEYS: tuple[str, ...] = ("spec_fingerprint", "generator_fingerprint")


def ledger_fingerprint() -> str:
    """The published fingerprint: the whole manifest, environment-derived digests included."""

    return payload_hash(ledger_manifest())


def ledger_digest_manifest() -> dict[str, Any]:
    """The manifest without the fields that only describe the interpreter running it."""

    return {
        key: value
        for key, value in ledger_manifest().items()
        if key not in ENVIRONMENT_DERIVED_KEYS
    }


def ledger_digest() -> str:
    """The ledger identity: identical on every interpreter, so a change here is a ledger change."""

    return payload_hash(ledger_digest_manifest())


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


# -- the promotion proof ----------------------------------------------------- #


@dataclass
class PromotionProof:
    """Did the frozen corpus really take over the code the ledger says it took over?"""

    code: str
    operator_id: str
    case_id: str
    case_index: int
    seed: int
    manifested: bool
    added_codes: list[str]
    target_finding: dict[str, Any] | None
    case: RepairCaseRecord
    status: str
    notes: list[str] = field(default_factory=list)

    def as_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "operator_id": self.operator_id,
            "case_id": self.case_id,
            "case_index": self.case_index,
            "seed": self.seed,
            "manifested": self.manifested,
            "added_codes": list(self.added_codes),
            "target_finding": self.target_finding,
            "status": self.status,
            "case": self.case.model_dump(mode="json", by_alias=True),
            "notes": list(self.notes),
        }


def promotion_cases(candidate_sha: str = DEFAULT_LEDGER_CANDIDATE) -> dict[str, BenchmarkCase]:
    """The frozen acceptance case that owns each promoted code, derived the way CI derives them.

    Nothing is hand-picked: the case comes out of :func:`~agentcad.repair_benchmark.generate_cases`
    for the same family and the same candidate SHA the acceptance suite uses. If the promotion were
    reverted, or the operator dropped out of its family's rotation, this lookup fails instead of
    quietly reporting coverage the corpus does not have.
    """

    cases: dict[str, BenchmarkCase] = {}
    for entry in PROMOTED_ENTRIES:
        owned = [
            case
            for case in generate_cases(
                candidate_sha=candidate_sha,
                family=entry.family,
                count=ACCEPTANCE_CASES_PER_FAMILY,
                suite="acceptance",
            )
            if case.operator_id == entry.operator_id
        ]
        if not owned:
            raise AssertionError(
                f"{entry.code} is recorded as promoted but no acceptance case in {entry.family} "
                f"is derived for {entry.operator_id}"
            )
        cases[entry.code] = owned[0]
    return cases


def _target_finding_for(
    context: BenchmarkContext, mutation: MutationResult, document_id: str
) -> dict[str, Any] | None:
    result = run_validation(
        context.service.get_document(document_id),
        context.registry,
        context.profile,
        service=context.service,
    )
    issue = _target_issue(
        mutation.target_code,
        mutation.target_validator_id,
        mutation.target_element_ids,
        result.issues,
        mutation.target_details,
    )
    return issue.model_dump(mode="json") if issue is not None else None


def prove_promotion(
    context: BenchmarkContext, *, candidate_sha: str = DEFAULT_LEDGER_CANDIDATE
) -> list[PromotionProof]:
    """Run each promoted code's frozen case and require the exact code to appear and be resolved.

    This is the same two-part proof the staging area used, applied to the corpus that now owns the
    code: the canonical validator has to raise the declared code on a drawing the producer really
    wrote (not on a fixture), and the ordinary runner has to resolve it under the ordinary oracle.
    ``status`` is the honest three-way answer — a code whose producer stopped manifesting is
    ``not_manifested``, however green its repair run looks.
    """

    proofs: list[PromotionProof] = []
    for entry in PROMOTED_ENTRIES:
        case = promotion_cases(candidate_sha)[entry.code]
        operator = MUTATIONS[case.operator_id]
        builder = operator.document_builder or build_base_drawing
        base = builder(context.service, context.registry)
        before = run_validation(
            context.service.get_document(base.document_id),
            context.registry,
            context.profile,
            service=context.service,
        )
        mutation = operator.apply(context.service, base, random.Random(case.seed))
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
        record = run_case(
            context,
            case,
            candidate_sha=candidate_sha,
            operators=MUTATIONS,
        )
        manifesting = target is not None
        status = promotion_status(manifesting, record)
        notes = [entry.note]
        if record.failure_code:
            notes.append(f"failure: {record.failure_code}")
        if record.reasons:
            notes.append(f"reason: {record.reasons[0]}")
        proofs.append(
            PromotionProof(
                code=entry.code,
                operator_id=entry.operator_id,
                case_id=case.case_id,
                case_index=case.index,
                seed=case.seed,
                manifested=manifesting,
                added_codes=_multiset_difference(produced_codes, base_codes),
                target_finding=target.model_dump(mode="json") if target is not None else None,
                case=record,
                status=status,
                notes=notes,
            )
        )
    return proofs


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
    disposition: str
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
            "disposition": self.disposition,
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
        connector = _connector_element(service, base.document_id, base["p1"])
        target = connector.target.model_dump(mode="json")
        target["port_id"] = "ext_no_such_port"
        _patch(service, base.document_id, base["p1"], {"target": target}, "probe-port-missing")
        return
    if code == "CONNECTOR_ENDPOINT_POINT_MISMATCH":
        connector = _connector_element(service, base.document_id, base["p1"])
        target = connector.target.model_dump(mode="json")
        target["point"] = {"x": target["point"]["x"] + 30.0, "y": target["point"]["y"] + 40.0}
        _patch(service, base.document_id, base["p1"], {"target": target}, "probe-point-mismatch")
        return
    raise AssertionError(f"no write probe for {code}")


def _connector_element(service: DocumentService, document_id: str, connector_id: str):
    from .models import ConnectorElement

    document = service.get_document(document_id)
    connector = next(item for item in document.elements if item.id == connector_id)
    if not isinstance(connector, ConnectorElement):
        raise AssertionError(f"{connector_id} is not a connector")
    return connector


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
        base = _three_valves_base(context.service, context.registry)
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

        source = _three_valves_base(context.service, context.registry)
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
                disposition=entry.disposition,
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


# -- negative capability ----------------------------------------------------- #
#
# The review asked for the gate to go red on a *broken* producer or a *cheating* plan, not only
# green on a good one. Each control below breaks one link in the chain on purpose and records
# which link reported it. They run against the frozen catalogue now, because that is where the
# producers live; a control that could not go red against the frozen cases would be a control that
# no longer guards anything.


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


def _control_operator(declared_code: str, declared_validator: str, apply) -> MutationOperator:
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
        document_builder=_three_valves_base,
        touched_budget_class="simple_metadata",
        base_variant="three_valves",
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


def _manifestation_of(
    context: BenchmarkContext,
    operator: MutationOperator,
    case: BenchmarkCase,
) -> tuple[bool, list[str]]:
    """Run one producer and ask the canonical validator - not the operator's name - what it made."""

    builder = operator.document_builder or build_base_drawing
    base = builder(context.service, context.registry)
    before = run_validation(
        context.service.get_document(base.document_id),
        context.registry,
        context.profile,
        service=context.service,
    )
    mutation = operator.apply(context.service, base, random.Random(case.seed))
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
    return target is not None, _multiset_difference(produced_codes, base_codes)


def _control_case(case_id: str, case: BenchmarkCase) -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id,
        family=case.family,
        operator_id="negative_control",
        index=case.index,
        seed=case.seed,
        suite="coverage-ledger-negative",
    )


def promotion_status(manifested: bool, record: RepairCaseRecord) -> str:
    """The three honest outcomes of a promoted code, derived from evidence rather than declared.

    ``not_manifested`` is what keeps the ledger from lying: if the producer could not make the
    canonical validator raise the code, the code is *not* carried by the corpus, however green the
    rest of the case looks. ``covered`` requires both halves - the defect was real and the governed
    repair resolved it - and neither half is inferred from the operator's name.
    """

    if not manifested:
        return "not_manifested"
    return "covered" if classify_case(record) == "success" else "not_repaired"


def run_negative_controls(
    context: BenchmarkContext, *, candidate_sha: str = DEFAULT_LEDGER_CANDIDATE
) -> list[NegativeControl]:
    """Prove the coverage gate can go red: broken producer, wrong code, cheating plan."""

    controls: list[NegativeControl] = []
    case = promotion_cases(candidate_sha)["DUPLICATE_LABEL"]

    # 1. The producer writes but stages nothing: the manifestation proof must fail, and the row
    #    must not be counted as coverage.
    silent = _control_operator("DUPLICATE_LABEL", "diagram-quality", _benign_write)
    silent_case = _control_case("negative:producer-stages-nothing", case)
    silent_catalogue = {**MUTATIONS, "negative_control": silent}
    silent_manifested, _ = _manifestation_of(context, silent, silent_case)
    silent_record = run_case(
        context, silent_case, candidate_sha="negative-control", operators=silent_catalogue
    )
    controls.append(
        NegativeControl(
            control="producer_stages_nothing",
            expected_signal="not_manifested",
            observed_signal=promotion_status(silent_manifested, silent_record),
            red=(
                not silent_manifested
                and promotion_status(silent_manifested, silent_record) == "not_manifested"
            ),
            detail=f"target_present={silent_manifested}",
        )
    )

    # 2. The producer stages a real defect but declares a different code: the exact-code check is
    #    what fails, not the write. The defect is real (the same write as the covered case), only
    #    the claim moved, so a name-based check would happily pass it.
    def mislabelled(service, base, rng) -> MutationResult:
        result = MUTATIONS["f1_duplicate_label"].apply(service, base, rng)
        result.target_code = "NODE_OVERLAP"
        result.target_validator_id = "diagram-quality"
        return result

    wrong = _control_operator("NODE_OVERLAP", "diagram-quality", mislabelled)
    wrong_case = _control_case("negative:producer-declares-wrong-code", case)
    wrong_catalogue = {**MUTATIONS, "negative_control": wrong}
    wrong_manifested, wrong_added = _manifestation_of(context, wrong, wrong_case)
    controls.append(
        NegativeControl(
            control="producer_declares_wrong_code",
            expected_signal="not_manifested",
            observed_signal=promotion_status(
                wrong_manifested,
                run_case(
                    context, wrong_case, candidate_sha="negative-control", operators=wrong_catalogue
                ),
            ),
            red=not wrong_manifested,
            detail=f"declared=NODE_OVERLAP added={wrong_added}",
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
                    # A neutral edit - a line tag that changes nothing else - so the clause that
                    # reports is locality and not a collateral finding the edit introduced.
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
        operators=MUTATIONS,
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
        operators=MUTATIONS,
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


# -- the ledger -------------------------------------------------------------- #


def ledger_payload(
    context: BenchmarkContext, *, candidate_sha: str = DEFAULT_LEDGER_CANDIDATE
) -> dict[str, Any]:
    """The whole track in one reviewable payload: promotions, dispositions, negative controls."""

    promotions = prove_promotion(context, candidate_sha=candidate_sha)
    probes = probe_representability(context)
    controls = run_negative_controls(context, candidate_sha=candidate_sha)
    payload: dict[str, Any] = {
        "ledger": ledger_manifest(),
        "ledger_fingerprint": ledger_fingerprint(),
        "ledger_digest": ledger_digest(),
        "superseded_extension": SUPERSEDED_EXTENSION_IDENTITIES,
        "candidate_sha": candidate_sha,
        "promotions": [proof.as_payload() for proof in promotions],
        "promoted": sorted(proof.code for proof in promotions if proof.status == "covered"),
        "promoted_but_not_covered": sorted(
            proof.code for proof in promotions if proof.status != "covered"
        ),
        "representability": [probe.as_payload() for probe in probes],
        "unreachable": sorted(probe.code for probe in probes if not probe.reachable),
        "became_reachable": sorted(probe.code for probe in probes if probe.reachable),
        "negative_controls": [control.as_payload() for control in controls],
        "negative_controls_all_red": all(control.red for control in controls),
    }
    # The published report carries exactly one hash and that hash covers every byte the reader
    # gets, ``ledger_fingerprint`` included: a field outside the hash is a field an edit can move
    # without the report noticing.
    return {**payload, "report_hash": payload_hash(payload)}


class CoverageLedgerError(RuntimeError):
    """The ledger contradicts itself: a promotion that is not carried, or a code that became stageable."""


def assert_ledger_is_sound(payload: dict[str, Any]) -> None:
    """The three conditions that make the ledger worth publishing, checked on its own payload.

    They are the reason a reader does not have to re-derive anything: the promoted codes are
    carried by the frozen corpus with the exact code manifesting, the unreachable codes are still
    unreachable at both supported surfaces, and every negative control went red. A future change
    that makes one of the three unreachable codes stageable turns this into a failure, which is
    what "promote it then" means mechanically.
    """

    if payload["promoted_but_not_covered"]:
        raise CoverageLedgerError(
            "promoted codes are not covered by the frozen corpus: "
            f"{payload['promoted_but_not_covered']}"
        )
    if payload["became_reachable"]:
        raise CoverageLedgerError(
            "codes recorded as unreachable can now be staged: "
            f"{payload['became_reachable']} - promote them to real cases"
        )
    if not payload["negative_controls_all_red"]:
        raise CoverageLedgerError("a negative control stopped reporting")


__all__ = [
    "BY_CODE",
    "DEFAULT_LEDGER_CANDIDATE",
    "ENVIRONMENT_DERIVED_KEYS",
    "LEDGER_ID",
    "LEDGER_VERSION",
    "NORMALIZED_OR_REJECTED_AT_SUPPORTED_INGRESS",
    "PROMOTED_ENTRIES",
    "SUPERSEDED_EXTENSION_IDENTITIES",
    "UNREACHABLE_AT_SUPPORTED_INGRESS",
    "UNREACHABLE_ENTRIES",
    "CoverageLedgerError",
    "NegativeControl",
    "PromotedEntry",
    "PromotionProof",
    "RepresentationProbe",
    "SurfaceAttempt",
    "UnreachableEntry",
    "assert_ledger_is_sound",
    "canonical_payload_json",
    "ledger_digest",
    "ledger_digest_manifest",
    "ledger_fingerprint",
    "ledger_manifest",
    "ledger_payload",
    "payload_hash",
    "probe_representability",
    "promotion_cases",
    "promotion_status",
    "prove_promotion",
    "run_negative_controls",
]
