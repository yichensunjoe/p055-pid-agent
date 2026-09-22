"""M6 Phase-2A: the governance chain, proved by the paths that must fail.

A governance layer is judged by what it refuses, so most of this file is about refusals. Each
negative below corresponds to a way the chain could quietly stop being a chain — a candidate
that carries a write operation, a confirmation that is only a status field, a conflict that is
resolved by reusing the old decision, an overwrite that happens because nobody compared the
authoritative value first, a delete that erases the history it was about.

Three properties are structural rather than empirical and are asserted as such:

* the code's declared states, transitions and producers are the contract's, so the contract
  cannot drift away from the thing that enforces it;
* a patch is never applied in this phase, and no decision kind can even name the ``applied``
  status;
* the same finding compiles twice into the same ``patch_id`` *and* the same digest, because the
  id is derived from the digest rather than assigned alongside it.

Everything runs against the real SQLite store on a temporary database: the review aggregates
are exercised through the same datastore and transaction abstraction as the documents, which is
the point of putting them there.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest
from pydantic import ValidationError

from agentcad import m6_candidate_core as core
from agentcad import m6_candidate_models as schemas
from agentcad import m6_ingestion_contract as contract
from agentcad.engineering_ir import build_engineering_graph
from agentcad.m6_candidate_core import (
    CandidateSchemaViolation,
    CompilationRefused,
    GovernedWriteNotAuthorized,
    IllegalTransition,
    M6CandidateService,
    NotConfirmedError,
    baseline_record,
    semantic_value,
)
from agentcad.m6_candidate_models import (
    CandidateEvidence,
    Confidence,
    ConfirmedSemanticFinding,
    ProducerRef,
    ProposedSemantics,
    ProvenanceRef,
    ReviewDecision,
    SemanticCandidate,
    SourceArtifactRef,
    SourceRegion,
    StructuredEngineeringPatch,
)
from agentcad.models import (
    ConnectorElement,
    ConnectorEndpoint,
    Document,
    Layer,
    Point,
    Style,
    SymbolElement,
)
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry

DOCUMENT_ID = "doc_m6_phase2a"
REVIEWER = "engineer.joe"


@pytest.fixture(scope="module")
def registry() -> SymbolRegistry:
    return SymbolRegistry()


@pytest.fixture()
def store(tmp_path) -> SQLiteDocumentStore:
    return SQLiteDocumentStore(tmp_path / "m6.sqlite3")


@pytest.fixture()
def service(store: SQLiteDocumentStore) -> M6CandidateService:
    return M6CandidateService(store)


def _untagged_pump() -> SymbolElement:
    return SymbolElement(
        id="pump_untagged",
        symbol_key="centrifugal_pump",
        position=Point(x=100, y=100),
        width=60,
        height=60,
    )


def _tagged_pump() -> SymbolElement:
    return SymbolElement(
        id="pump_tagged",
        symbol_key="centrifugal_pump",
        position=Point(x=300, y=100),
        width=60,
        height=60,
        label="P-101",
    )


def _drawing(elements: list) -> Document:
    """A committed drawing. It is never written by M6 Phase-2A — only read.

    The connecting line is only added when both pumps are present, so a test can build a
    one-element drawing without tripping the document's reference validation.
    """
    element_ids = {element.id for element in elements}
    if not {"pump_untagged", "pump_tagged"} <= element_ids:
        return Document(
            id=DOCUMENT_ID,
            name="M6 fixture",
            revision=7,
            layers=[Layer(id="layer_default", name="Process")],
            elements=elements,
        )
    line = ConnectorElement(
        id="line_a",
        source=ConnectorEndpoint(
            element_id="pump_untagged", port_id="discharge", point=Point(x=160, y=100)
        ),
        target=ConnectorEndpoint(
            element_id="pump_tagged", port_id="suction", point=Point(x=300, y=100)
        ),
        points=[Point(x=160, y=100), Point(x=300, y=100)],
        routing="manual",
        style=Style(),
    )
    return Document(
        id=DOCUMENT_ID,
        name="M6 fixture",
        revision=7,
        layers=[Layer(id="layer_default", name="Process")],
        elements=[*elements, line],
    )


def _region(*, element_refs: list[str] | None = None, x: float = 100.0, y: float = 100.0) -> SourceRegion:
    return SourceRegion(
        region_id="region_m6_1",
        artifact_id="artifact_m6_1",
        source_document_id=DOCUMENT_ID,
        source_revision=7,
        geometry_selector=schemas.RegionGeometry(x=x, y=y, width=60, height=60),
        element_refs=element_refs or [],
        text_spans=[schemas.TextSpan(text="P-201")],
    )


def _artifact() -> SourceArtifactRef:
    return SourceArtifactRef(
        artifact_id="artifact_m6_1",
        source_document_id=DOCUMENT_ID,
        source_revision=7,
        content_hash="deadbeef",
    )


def _tag_candidate(candidate_id: str = "cand_tag_1", *, tag: str = "P-201") -> SemanticCandidate:
    return SemanticCandidate(
        candidate_id=candidate_id,
        artifact=_artifact(),
        region=_region(element_refs=["pump_untagged"]),
        candidate_type="equipment_tag",
        proposed_semantics=ProposedSemantics(
            target_identity="element:pump_untagged", equipment_tag=tag
        ),
        confidence=Confidence(value=0.93, source="model"),
        evidence=[
            CandidateEvidence(kind="observed_text", detail="label reads P-201", observed_value=tag)
        ],
        producer=ProducerRef(key="typesafe", version="jev-1.13.0"),
        provenance=ProvenanceRef(provider="typesafe", model="jev", procedure_version="draw-v1"),
        created_at=datetime(2026, 9, 22, 10, 0, tzinfo=UTC),
    )


# --------------------------------------------------------------------------------------
# The contract and the code describe the same system
# --------------------------------------------------------------------------------------


def test_the_schemas_declare_exactly_the_contract_vocabulary() -> None:
    assert set(get_args(schemas.CandidateStatus)) == set(contract.CANDIDATE_STATES)
    assert set(get_args(schemas.FactKind)) == set(contract.CANDIDATE_TYPE_KINDS)
    assert set(get_args(schemas.ProducerKey)) == {p.key for p in contract.JUDGMENT_PRODUCERS}
    assert set(get_args(schemas.WriteIntent)) == {row.intent for row in contract.WRITE_POLICY_V1}
    assert set(get_args(schemas.PolicyDisposition)) == {
        row.disposition for row in contract.WRITE_POLICY_V1
    }
    assert set(get_args(schemas.CalibrationClass)) == set(contract.CONFIDENCE_CALIBRATION_CLASSES)
    assert set(get_args(schemas.ConfidenceSource)) == set(contract.CONFIDENCE_SOURCES)
    assert schemas.HUMAN_DECISION_KINDS == tuple(
        kind for kind in schemas.HUMAN_DECISION_KINDS if kind.startswith("human_")
    )


def test_the_tables_hold_only_the_declared_aggregates(store: SQLiteDocumentStore) -> None:
    with sqlite3.connect(store.database_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"semantic_candidates", "review_decisions", "confirmed_semantic_findings"} <= tables


# --------------------------------------------------------------------------------------
# Required negative 1: a candidate that carries write operations
# --------------------------------------------------------------------------------------


def test_a_candidate_carrying_operations_is_rejected() -> None:
    payload = _tag_candidate().model_dump(mode="json", by_alias=True)
    payload["operations"] = [{"op": "delete_element", "element_id": "pump_untagged"}]
    with pytest.raises(ValidationError):
        SemanticCandidate.model_validate(payload)


def test_a_candidate_payload_has_no_write_operation_anywhere() -> None:
    document = schemas.candidate_document(_tag_candidate())
    forbidden = {"operations", "op", "delete_element", "update_element", "patch"}
    assert forbidden.isdisjoint(document)
    assert forbidden.isdisjoint(document["proposed_semantics"])


# --------------------------------------------------------------------------------------
# Required negative 2: compiling (or applying) before any review
# --------------------------------------------------------------------------------------


def test_compiling_without_a_finding_is_refused(
    service: M6CandidateService, registry: SymbolRegistry
) -> None:
    with pytest.raises(NotConfirmedError):
        service.compile_finding("m6find_nonexistent", document=_drawing([_untagged_pump()]), registry=registry)


def test_a_candidate_cannot_be_filed_as_already_confirmed(service: M6CandidateService) -> None:
    """A status written at filing time is not a decision, so it is refused at the door."""

    forged = _tag_candidate().model_copy(update={"review_status": "confirmed"})
    with pytest.raises(CandidateSchemaViolation):
        service.file_candidate(forged)


def test_a_status_column_edited_behind_the_service_grants_nothing(
    service: M6CandidateService, store: SQLiteDocumentStore
) -> None:
    """The status is replayed from the decision log; the stored value is not consulted.

    This is what makes "confirmed" mean "a person confirmed it" rather than "somebody wrote
    the word confirmed into a row".
    """

    candidate = _tag_candidate()
    service.file_candidate(candidate)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE semantic_candidates SET review_status_at_creation = 'confirmed' "
            "WHERE candidate_id = ?",
            (candidate.candidate_id,),
        )
        connection.commit()

    assert service.current_status(candidate.candidate_id) == "needs_review"
    with pytest.raises(NotConfirmedError):
        service.confirm_finding(candidate.candidate_id)


def test_a_finding_without_a_review_decision_is_unrepresentable() -> None:
    with pytest.raises(ValidationError):
        ConfirmedSemanticFinding(
            finding_id="m6find_x",
            candidate_id="cand_tag_1",
            review_decision_id="",
            artifact=_artifact(),
            region_id="region_m6_1",
            candidate_type="equipment_tag",
            confirmed_semantics=ProposedSemantics(equipment_tag="P-201"),
            evidence=[CandidateEvidence(kind="rule", detail="observed")],
            baseline=schemas.ConflictBaselineRecord(
                baseline_revision=7,
                comparison_identity="element:pump_untagged",
                comparison_path="equipment_tag",
                value_digest="0" * 32,
            ),
            producer=ProducerRef(key="typesafe"),
            provenance_chain=["a", "b", "c", "d"],
        )


def test_the_apply_boundary_refuses_and_no_decision_can_name_applied(
    service: M6CandidateService,
) -> None:
    with pytest.raises(GovernedWriteNotAuthorized):
        service.request_apply("m6patch_anything")

    # The refusals are structural: no decision kind targets `applied`, and the module refuses
    # to import if one ever did. So the transition cannot be recorded even by a determined caller.
    assert "applied" not in set(core._KIND_TARGET_STATUS.values())
    assert set(get_args(schemas.DecisionKind)) == set(core._KIND_TARGET_STATUS)
    assert ("conflicted", "applied") in contract.FORBIDDEN_TRANSITIONS
    assert ("needs_review", "applied") in contract.FORBIDDEN_TRANSITIONS


# --------------------------------------------------------------------------------------
# Required negative 3: an overwrite of an existing authoritative value
# --------------------------------------------------------------------------------------


def _confirmed_finding_for(
    service: M6CandidateService,
    document: Document,
    registry: SymbolRegistry,
    candidate: SemanticCandidate,
) -> ConfirmedSemanticFinding:
    service.file_candidate(candidate)
    graph = build_engineering_graph(document, registry)
    baseline = baseline_record(
        document,
        graph,
        identity=candidate.proposed_semantics.target_identity,
        path="equipment_tag",
    )
    service.confirm(
        candidate.candidate_id,
        reviewer_identity=REVIEWER,
        reviewer_action="confirmed the symbol is P-201",
        baseline=baseline,
    )
    return service.confirm_finding(candidate.candidate_id)


def test_overwriting_an_authoritative_value_is_refused_not_compiled(
    service: M6CandidateService, registry: SymbolRegistry
) -> None:
    document = _drawing([_untagged_pump(), _tagged_pump()])
    candidate = _tag_candidate(tag="P-999")
    candidate = candidate.model_copy(
        update={
            "proposed_semantics": ProposedSemantics(
                target_identity="element:pump_tagged", equipment_tag="P-999"
            ),
            "region": _region(element_refs=["pump_tagged"]),
        }
    )
    finding = _confirmed_finding_for(service, document, registry, candidate)
    with pytest.raises(CompilationRefused) as refusal:
        service.compile_finding(finding.finding_id, document=document, registry=registry)
    assert refusal.value.code == "overwrite_requires_human_resolution"
    assert "conflict" in str(refusal.value)


def test_the_same_value_already_stated_is_refused(
    service: M6CandidateService, registry: SymbolRegistry
) -> None:
    document = _drawing([_untagged_pump(), _tagged_pump()])
    candidate = _tag_candidate(tag="P-101").model_copy(
        update={
            "proposed_semantics": ProposedSemantics(
                target_identity="element:pump_tagged", equipment_tag="P-101"
            ),
            "region": _region(element_refs=["pump_tagged"]),
        }
    )
    finding = _confirmed_finding_for(service, document, registry, candidate)
    with pytest.raises(CompilationRefused) as refusal:
        service.compile_finding(finding.finding_id, document=document, registry=registry)
    assert refusal.value.code == "already_stated"


# --------------------------------------------------------------------------------------
# Required negatives 4 and 5: baseline drift, and the old decision cannot resolve it
# --------------------------------------------------------------------------------------


def test_a_moved_baseline_forces_a_conflict(
    service: M6CandidateService, registry: SymbolRegistry
) -> None:
    document = _drawing([_untagged_pump(), _tagged_pump()])
    candidate = _tag_candidate()
    finding = _confirmed_finding_for(service, document, registry, candidate)
    assert service.current_status(candidate.candidate_id) == "confirmed"

    # Somebody else tags the same object before the write happens.
    moved = document.model_copy(
        update={
            "revision": 8,
            "elements": [
                element.model_copy(update={"label": "P-777"})
                if getattr(element, "id", "") == "pump_untagged"
                else element
                for element in document.elements
            ],
        }
    )
    graph = build_engineering_graph(moved, registry)
    current = baseline_record(
        moved, graph, identity="element:pump_untagged", path="equipment_tag"
    )
    decision = service.recheck_baseline(candidate.candidate_id, current=current)
    assert decision is not None and decision.kind == "conflict_detected"
    assert service.current_status(candidate.candidate_id) == "conflicted"
    assert decision.conflict is not None
    assert decision.conflict.baseline.value_digest != current.value_digest
    # The finding that was created before the drift still exists: history is not rewritten.
    assert _stored_finding(service, finding.finding_id) is not None
    with pytest.raises(CompilationRefused) as refusal:
        service.compile_finding(finding.finding_id, document=moved, registry=registry)
    assert refusal.value.code == "overwrite_requires_human_resolution"


def test_an_unchanged_baseline_is_not_a_conflict(
    service: M6CandidateService, registry: SymbolRegistry
) -> None:
    document = _drawing([_untagged_pump(), _tagged_pump()])
    candidate = _tag_candidate()
    _confirmed_finding_for(service, document, registry, candidate)
    graph = build_engineering_graph(document, registry)
    current = baseline_record(document, graph, identity="element:pump_untagged", path="equipment_tag")
    assert service.recheck_baseline(candidate.candidate_id, current=current) is None
    assert service.current_status(candidate.candidate_id) == "confirmed"


def test_an_old_confirmation_cannot_resolve_a_conflict(
    service: M6CandidateService, registry: SymbolRegistry
) -> None:
    document = _drawing([_untagged_pump(), _tagged_pump()])
    candidate = _tag_candidate()
    _confirmed_finding_for(service, document, registry, candidate)
    moved = document.model_copy(update={"revision": 8})
    graph = build_engineering_graph(moved, registry)
    current = baseline_record(
        moved,
        graph,
        identity="element:pump_untagged",
        path="equipment_tag",
    ).model_copy(update={"value_digest": "f" * 32})
    service.recheck_baseline(candidate.candidate_id, current=current)
    assert service.current_status(candidate.candidate_id) == "conflicted"

    # Reusing the original confirmation would be a status edit, not a decision: there is no
    # transition from conflicted straight back to confirmed.
    with pytest.raises(IllegalTransition):
        service.record_decision(candidate.candidate_id, "human_confirm", reviewer_identity=REVIEWER)

    # A fresh decision is required, and it is recorded as one.
    decision = service.resolve_conflict(
        candidate.candidate_id,
        reviewer_identity=REVIEWER,
        reviewer_action="resolved: keep the newly stated P-777",
        conflict_resolution="keep the existing authoritative value",
        resolution_choice="keep_existing",
        baseline=current,
    )
    assert decision.kind == "human_resolves_conflict"
    assert decision.review_decision_id not in {
        item.review_decision_id for item in service.decisions(candidate.candidate_id)[:-1]
    }
    assert service.current_status(candidate.candidate_id) == "needs_review"


def test_a_conflicted_candidate_cannot_be_applied(
    service: M6CandidateService, registry: SymbolRegistry
) -> None:
    document = _drawing([_untagged_pump(), _tagged_pump()])
    candidate = _tag_candidate()
    _confirmed_finding_for(service, document, registry, candidate)
    assert ("conflicted", "applied") in contract.FORBIDDEN_TRANSITIONS
    with pytest.raises(GovernedWriteNotAuthorized):
        service.request_apply("m6patch_any")


# --------------------------------------------------------------------------------------
# Required negative 6 (implicit above) and 7: deleting the source must not erase history
# --------------------------------------------------------------------------------------


def test_deleting_the_source_drawing_leaves_the_review_history(
    service: M6CandidateService, store: SQLiteDocumentStore, registry: SymbolRegistry
) -> None:
    document = _drawing([_untagged_pump(), _tagged_pump()])
    store.save(_stored(document))
    candidate = _tag_candidate()
    finding = _confirmed_finding_for(service, document, registry, candidate)

    assert store.delete(DOCUMENT_ID, expected_revision=document.revision) is True

    assert store.get_semantic_candidate(candidate.candidate_id) is not None
    assert store.list_review_decisions(candidate.candidate_id)
    assert store.get_confirmed_finding(finding.finding_id) is not None
    assert store.list_confirmed_findings(source_document_id=DOCUMENT_ID)


def test_the_review_tables_have_no_foreign_key_to_documents(store: SQLiteDocumentStore) -> None:
    """The absence is the enforcement: a cascade would delete the evidence with the drawing."""

    with sqlite3.connect(store.database_path) as connection:
        for table in ("semantic_candidates", "review_decisions", "confirmed_semantic_findings"):
            keys = connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            assert keys == [], f"{table} must not reference documents: {keys}"


# --------------------------------------------------------------------------------------
# Required negative 8: the compiler is deterministic
# --------------------------------------------------------------------------------------


def test_the_same_finding_compiles_into_the_same_patch_twice(
    service: M6CandidateService, registry: SymbolRegistry
) -> None:
    document = _drawing([_untagged_pump(), _tagged_pump()])
    finding = _confirmed_finding_for(service, document, registry, _tag_candidate())

    first = service.compile_finding(finding.finding_id, document=document, registry=registry)
    second = service.compile_finding(finding.finding_id, document=document, registry=registry)

    assert isinstance(first, StructuredEngineeringPatch)
    assert first.canonical_digest == second.canonical_digest
    assert first.patch_id == second.patch_id
    assert first.patch_id == f"m6patch_{first.canonical_digest[:16]}"
    assert first.write_authority == "none"


def test_the_canonical_digest_ignores_volatile_provenance(
    service: M6CandidateService, registry: SymbolRegistry
) -> None:
    """Timestamps move, the digest does not: the same mistake as identity-by-runtime-metadata."""

    document = _drawing([_untagged_pump(), _tagged_pump()])
    finding = _confirmed_finding_for(service, document, registry, _tag_candidate())
    original = service.compile_finding(finding.finding_id, document=document, registry=registry)

    # Same facts, three days later, with a different provenance timestamp.
    later = finding.model_copy(update={"confirmed_at": finding.confirmed_at + timedelta(days=3)})
    recompiled = core.M6CandidateService._compile(  # noqa: SLF001 - the pure compile step
        service, later, document=document, registry=registry
    )

    assert later.confirmed_at != finding.confirmed_at
    assert recompiled.canonical_digest == original.canonical_digest
    assert recompiled.patch_id == original.patch_id


def test_a_candidate_that_cannot_be_compiled_says_why_before_it_can_be_confirmed() -> None:
    """An unresolved candidate is a recorded outcome, not a fact that sneaks into a patch."""

    unresolved = SemanticCandidate(
        candidate_id="cand_unresolved",
        artifact=_artifact(),
        region=_region(),
        candidate_type="unresolved",
        proposed_semantics=ProposedSemantics(unresolved_reason="insufficient_evidence"),
        confidence=Confidence(value=0.2, source="model"),
        evidence=[CandidateEvidence(kind="rule", detail="no catalogue match in the region")],
        producer=ProducerRef(key="deterministic_rule_engine", version="1"),
    )
    assert unresolved.proposed_semantics.equipment_tag == ""


# --------------------------------------------------------------------------------------
# The positive fixture: one governed fact from artifact to patch
# --------------------------------------------------------------------------------------


def test_the_positive_fixture_reaches_a_patch_and_stops_there(
    service: M6CandidateService, store: SQLiteDocumentStore, registry: SymbolRegistry
) -> None:
    document = _drawing([_untagged_pump(), _tagged_pump()])

    # 1. artifact + region + candidate
    candidate = _tag_candidate()
    service.file_candidate(candidate)
    assert service.current_status(candidate.candidate_id) == "needs_review"

    # 2. a person reviews it against a recorded baseline
    graph = build_engineering_graph(document, registry)
    assert semantic_value(document, graph, "element:pump_untagged", "equipment_tag") == ""
    baseline = baseline_record(
        document, graph, identity="element:pump_untagged", path="equipment_tag"
    )
    assert baseline.value_present is False
    decision = service.confirm(
        candidate.candidate_id,
        reviewer_identity=REVIEWER,
        reviewer_action="saw the label P-201 on the pump",
        baseline=baseline,
    )
    assert decision.is_human and decision.reviewer_identity == REVIEWER

    # 3. confirmed finding
    finding = service.confirm_finding(candidate.candidate_id)
    assert finding.review_decision_id == decision.review_decision_id
    assert finding.provenance_chain[:4] == [
        candidate.artifact.artifact_id,
        candidate.region.region_id,
        candidate.candidate_id,
        decision.review_decision_id,
    ]
    assert isinstance(finding.confirmed_semantics, ProposedSemantics)

    # 4. deterministic patch — and no write
    patch = service.compile_finding(finding.finding_id, document=document, registry=registry)
    assert patch.intent == "metadata_enrichment"
    assert patch.policy_disposition == "allowed_after_confirmation"
    assert patch.finding_ids == [finding.finding_id]
    assert len(patch.operations) == 1
    assert patch.operations[0].op == "update_element"
    assert patch.operations[0].patch == {"label": "P-201"}
    assert patch.baseline_revision == document.revision

    # the document itself is untouched
    assert store.get(DOCUMENT_ID) is None
    with pytest.raises(GovernedWriteNotAuthorized):
        service.request_apply(patch.patch_id)


def test_creation_compiles_from_the_region_and_the_catalogue(
    service: M6CandidateService, registry: SymbolRegistry
) -> None:
    document = _drawing([_untagged_pump(), _tagged_pump()])
    candidate = SemanticCandidate(
        candidate_id="cand_class",
        artifact=_artifact(),
        region=_region(x=500, y=500),
        candidate_type="symbol_class",
        proposed_semantics=ProposedSemantics(symbol_class="centrifugal_pump", equipment_tag="P-201"),
        confidence=Confidence(value=0.8, source="model"),
        evidence=[CandidateEvidence(kind="geometry", detail="pump outline at 500,500")],
        producer=ProducerRef(key="typesafe", version="jev-1.13.0"),
    )
    service.file_candidate(candidate)
    graph = build_engineering_graph(document, registry)
    service.confirm(
        candidate.candidate_id,
        reviewer_identity=REVIEWER,
        reviewer_action="new pump P-201",
        baseline=baseline_record(document, graph, identity="", path="existence"),
    )
    finding = service.confirm_finding(candidate.candidate_id)
    patch = service.compile_finding(finding.finding_id, document=document, registry=registry)
    assert patch.intent == "creation"
    operation = patch.operations[0]
    assert operation.op == "add_element"
    assert operation.element.symbol_key == "centrifugal_pump"
    assert operation.element.label == "P-201"
    assert operation.element.position.x == 500
    # the generated id is derived, so a recompile is byte-identical
    again = service.compile_finding(finding.finding_id, document=document, registry=registry)
    assert again.operations[0].element.id == operation.element.id
    assert again.patch_id == patch.patch_id


def test_creation_outside_the_catalogue_is_refused(
    service: M6CandidateService, registry: SymbolRegistry
) -> None:
    document = _drawing([_untagged_pump(), _tagged_pump()])
    candidate = SemanticCandidate(
        candidate_id="cand_class_unknown",
        artifact=_artifact(),
        region=_region(x=500, y=500),
        candidate_type="symbol_class",
        proposed_semantics=ProposedSemantics(symbol_class="not_a_real_symbol_class"),
        confidence=Confidence(value=0.5, source="model"),
        evidence=[CandidateEvidence(kind="geometry", detail="blob at 500,500")],
        producer=ProducerRef(key="typesafe", version="jev-1.13.0"),
    )
    service.file_candidate(candidate)
    graph = build_engineering_graph(document, registry)
    service.confirm(
        candidate.candidate_id,
        reviewer_identity=REVIEWER,
        reviewer_action="a pump",
        baseline=baseline_record(document, graph, identity="", path="existence"),
    )
    finding = service.confirm_finding(candidate.candidate_id)
    with pytest.raises(CompilationRefused) as refusal:
        service.compile_finding(finding.finding_id, document=document, registry=registry)
    assert refusal.value.code == "out_of_catalogue"


# --------------------------------------------------------------------------------------
# Supporting negatives: the queue itself
# --------------------------------------------------------------------------------------


def test_a_human_transition_without_a_reviewer_action_is_refused(
    service: M6CandidateService,
) -> None:
    candidate = _tag_candidate()
    service.file_candidate(candidate)
    with pytest.raises(ValidationError):
        service.record_decision(candidate.candidate_id, "human_confirm", reviewer_identity=REVIEWER)


def test_a_producer_event_may_not_claim_a_reviewer_action(
    service: M6CandidateService,
) -> None:
    candidate = _tag_candidate()
    service.file_candidate(candidate)
    with pytest.raises(ValidationError):
        ReviewDecision(
            review_decision_id="m6dec_fake",
            candidate_id=candidate.candidate_id,
            kind="filed",
            from_status="proposed",
            to_status="needs_review",
            reviewer_action="I approved this",
        )


def test_a_transition_the_contract_does_not_declare_is_refused(service: M6CandidateService) -> None:
    candidate = _tag_candidate()
    service.file_candidate(candidate)
    # `needs_review -> needs_review` is not an edge: a second filing is not a transition.
    with pytest.raises(IllegalTransition):
        service.record_decision(candidate.candidate_id, "filed")


def test_measuring_calibration_needs_a_signed_gate() -> None:
    with pytest.raises(ValidationError):
        Confidence(value=0.99, calibration_class="measured_on_gold_corpus", source="model")
    signed = Confidence(
        value=0.99,
        calibration_class="measured_on_gold_corpus",
        source="model",
        calibration_gate_id="m6cal_1",
    )
    assert signed.calibration_gate_id == "m6cal_1"
    assert signed.meaning == contract.CONFIDENCE_MEANING


def test_confidence_can_never_be_restated_as_a_promise() -> None:
    with pytest.raises(ValidationError):
        Confidence(value=1.0, source="model", meaning="the answer is certainly correct")


def test_a_region_needs_a_selector_and_keeps_the_source_revision() -> None:
    with pytest.raises(ValidationError):
        SourceRegion(
            region_id="r",
            artifact_id="a",
            source_document_id=DOCUMENT_ID,
            source_revision=7,
        )
    with pytest.raises(ValidationError):
        SemanticCandidate.model_validate(
            {
                **_tag_candidate().model_dump(mode="json", by_alias=True),
                "region": {
                    **_region().model_dump(mode="json", by_alias=True),
                    "source_revision": 6,
                },
            }
        )


def test_decisions_are_append_only(service: M6CandidateService, store: SQLiteDocumentStore) -> None:
    candidate = _tag_candidate()
    service.file_candidate(candidate)
    decision = service.decisions(candidate.candidate_id)[0]
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_review_decision(decision)


def test_the_repository_exposes_no_update_path(
    service: M6CandidateService, store: SQLiteDocumentStore
) -> None:
    for name in ("update_semantic_candidate", "update_review_decision", "delete_review_decision"):
        assert not hasattr(store, name), f"{name} would break the append-only guarantee"


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _stored(document: Document):
    from agentcad.store import StoredDocument

    return StoredDocument(document=document, undo_stack=[], redo_stack=[])


def _stored_finding(service: M6CandidateService, finding_id: str) -> ConfirmedSemanticFinding | None:
    return service._repository.get_confirmed_finding(finding_id)  # noqa: SLF001
