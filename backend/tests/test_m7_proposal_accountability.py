"""M7 phase 2A: a partially compiled proposal must be visible, durable and unable to succeed.

The fixture that matters here is the observed incident, reconstructed through the real
compiler rather than mocked: 197 proposed semantic operations, 120 retained, 77 dropped one
by one, and every surface reporting success. These tests assert the four things that were
false then and must be true now:

* the plan is reported ``valid`` *and* ``partial`` -- two answers, not one;
* every dropped operation leaves a receipt carrying the diagnostic that used to be discarded;
* what was submitted is durable, so the difference between proposed and applied is auditable;
* a partial proposal cannot be recorded as a completed session, and that refusal lives in the
  backend rather than in a dialog.

The last group covers the read-only catalogue audit, because a receipt is only actionable if
it distinguishes a symbol that exists but is suppressed from one that does not exist at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcad import m7_catalogue_audit as audit
from agentcad.agent_semantic_models import SemanticTransaction
from agentcad.harness import AgentHarnessService, HarnessError
from agentcad.harness_models import AgentSessionCreateRequest
from agentcad.m7_synthesis_models import (
    PROPOSAL_EVIDENCE_TABLE,
    build_not_evaluated_evidence,
    build_proposal_evidence,
    derive_completeness,
)
from agentcad.models import CreateDocumentRequest, TransactionRequest
from agentcad.permissive_semantic_compiler import PermissiveSemanticTransactionCompiler
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry

#: The shape of the real incident: proposed, retained, dropped.
INCIDENT_PROPOSED = 197
INCIDENT_ACCEPTED = 120
INCIDENT_REJECTED = 77


def _service(tmp_path: Path) -> tuple[DocumentService, SQLiteDocumentStore]:
    store = SQLiteDocumentStore(tmp_path / "m7.db")
    return DocumentService(store=store, symbols=SymbolRegistry()), store


def _seeded(service: DocumentService) -> str:
    document = service.create_document(CreateDocumentRequest(name="Accountability"))
    service.apply_transaction(
        document.id,
        TransactionRequest.model_validate(
            {
                "expected_revision": 0,
                "operations": [
                    {
                        "op": "add_element",
                        "element": {
                            "id": "seed",
                            "type": "text",
                            "position": {"x": 20, "y": 20},
                            "text": "seed",
                        },
                    }
                ],
            }
        ),
    )
    return document.id


def _accepted_text(index: int) -> dict:
    return {
        "op": "add_element",
        "element": {
            "id": f"kept_{index}",
            "type": "text",
            "position": {"x": 20 + (index % 20) * 70, "y": 60 + (index // 20) * 40},
            "text": f"kept {index}",
        },
    }


def _rejected_update(index: int) -> dict:
    """An operation the strict compiler must refuse: it names an element that does not exist."""

    return {
        "op": "update_element",
        "element_id": f"absent_{index}",
        "patch": {"name": "ignored"},
    }


def _incident_transaction(revision: int) -> SemanticTransaction:
    operations = [_accepted_text(index) for index in range(INCIDENT_ACCEPTED)]
    operations.extend(_rejected_update(index) for index in range(INCIDENT_REJECTED))
    return SemanticTransaction.model_validate(
        {
            "expected_revision": revision,
            "label": "熔盐堆厂区级气路系统总图",
            "operations": operations,
        }
    )


# --------------------------------------------------------------------------------------
# The incident, reproduced through the real compiler
# --------------------------------------------------------------------------------------


def test_the_observed_incident_is_reported_as_partial_not_as_success(tmp_path: Path) -> None:
    """197 proposed, 120 retained, 77 dropped: valid *and* partial, and both are visible."""

    service, _ = _service(tmp_path)
    document_id = _seeded(service)
    current = service.get_document(document_id)
    compiler = PermissiveSemanticTransactionCompiler(service)

    compiled = compiler.compile(document_id, _incident_transaction(current.revision))
    assessment = compiled.assessment

    assert len(compiled.transaction.operations) == INCIDENT_ACCEPTED
    assert (
        f"applied {INCIDENT_ACCEPTED}/{INCIDENT_PROPOSED} operations" in compiled.transaction.label
    )

    # The two axes, and the counts that make the loss arithmetic rather than prose.
    assert assessment.valid is True
    assert assessment.completeness == "partial"
    assert assessment.proposed_operation_count == INCIDENT_PROPOSED
    assert assessment.accepted_operation_count == INCIDENT_ACCEPTED
    assert assessment.rejected_operation_count == INCIDENT_REJECTED
    assert assessment.proposed_operation_count == (
        assessment.accepted_operation_count + assessment.rejected_operation_count
    )

    # The three places that used to say "passed": one field cannot say it any more.
    assert assessment.may_proceed_to_authorisation() is False
    assert assessment.is_complete is False


def test_every_dropped_operation_leaves_an_actionable_receipt(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    document_id = _seeded(service)
    current = service.get_document(document_id)
    compiler = PermissiveSemanticTransactionCompiler(service)

    assessment = compiler.compile(document_id, _incident_transaction(current.revision)).assessment

    assert len(assessment.rejected_operations) == INCIDENT_REJECTED
    for receipt in assessment.rejected_operations:
        assert receipt.reason_code, receipt.operation_id
        assert receipt.operation_kind == "update_element"
        assert receipt.operation_id.startswith("op")
        # The strict compiler computed all of this before; the receipt is what keeps it.
        assert receipt.field_path is not None
        assert isinstance(receipt.available_values, dict)
        assert isinstance(receipt.suggestions, list)

    indices = sorted(receipt.original_index for receipt in assessment.rejected_operations)
    assert indices == sorted(range(INCIDENT_ACCEPTED, INCIDENT_ACCEPTED + INCIDENT_REJECTED))


def test_a_whole_plan_reports_complete_with_no_receipts(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    document_id = _seeded(service)
    current = service.get_document(document_id)
    compiler = PermissiveSemanticTransactionCompiler(service)

    transaction = SemanticTransaction.model_validate(
        {
            "expected_revision": current.revision,
            "operations": [_accepted_text(index) for index in range(120)],
        }
    )
    assessment = compiler.compile(document_id, transaction).assessment

    assert assessment.valid is True
    assert assessment.completeness == "complete"
    assert assessment.rejected_operation_count == 0
    assert assessment.rejected_operations == []
    assert assessment.may_proceed_to_authorisation() is True


def test_a_plan_whose_every_operation_is_rejected_says_so_rather_than_reporting_zero(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path)
    document_id = _seeded(service)
    current = service.get_document(document_id)
    compiler = PermissiveSemanticTransactionCompiler(service)

    transaction = SemanticTransaction.model_validate(
        {
            "expected_revision": current.revision,
            "operations": [_rejected_update(index) for index in range(3)],
        }
    )
    assessment = compiler.compile(document_id, transaction).assessment

    # Every operation *was* examined and every one failed. The honest report is
    # 0 accepted of 3 rejected -- not 0/0, which would be the same fabrication this
    # milestone removes, only pointing the other way.
    assert assessment.valid is False
    assert assessment.operation_accounting == "evaluated"
    assert assessment.accepted_operation_count == 0
    assert assessment.rejected_operation_count == 3
    assert len(assessment.rejected_operations) == 3
    # ``empty`` is decisive on the accepted side: nothing survived. It is not ``partial``
    # (which requires retained work) and it is emphatically not ``complete``. The rejection
    # count says *why* it is empty, which is what the old 0/0 report destroyed.
    assert assessment.completeness == "empty"
    assert assessment.may_proceed_to_authorisation() is False


def test_an_assessment_may_not_report_counts_it_never_computed() -> None:
    """``not_evaluated`` is the default, and it forbids the numbers rather than zeroing them."""

    from agentcad.agent_semantic_models import AgentTransactionAssessment

    unevaluated = AgentTransactionAssessment(
        valid=False,
        stage="compile",
        document_id="doc_1",
        current_revision=1,
        next_revision=2,
        semantic_operation_count=77,
        global_failure_reason="revision_conflict: expected revision 9",
    )
    assert unevaluated.operation_accounting == "not_evaluated"
    assert unevaluated.accepted_operation_count is None
    assert unevaluated.rejected_operation_count is None
    assert unevaluated.completeness is None
    assert unevaluated.rejected_operations is None
    assert unevaluated.accounting_problems() == []
    # None is not complete, so an unchecked proposal cannot be offered for authorisation.
    assert unevaluated.is_complete is False
    assert unevaluated.may_proceed_to_authorisation() is False

    # Inventing a number for a plan that was never examined is refused, zero included.
    fabricated = unevaluated.model_copy(update={"accepted_operation_count": 0})
    problems = fabricated.accounting_problems()
    assert any("accepted_operation_count" in problem for problem in problems)

    # "We did not evaluate it" without saying why explains nothing, and the client cannot tell
    # it apart from a response that simply forgot to carry the contract.
    unexplained = unevaluated.model_copy(update={"global_failure_reason": ""})
    assert any("must record why" in problem for problem in unexplained.accounting_problems())

    # And an evaluated assessment must carry both counts and a verdict.
    missing_verdict = unevaluated.model_copy(
        update={
            "operation_accounting": "evaluated",
            "accepted_operation_count": 77,
            "rejected_operation_count": 0,
            "rejected_operations": [],
        }
    )
    assert any("completeness verdict" in problem for problem in missing_verdict.accounting_problems())


def test_every_assessment_the_plan_path_returns_is_coherent(tmp_path: Path) -> None:
    """The runtime invariant, on the compiler the agent actually goes through.

    Three outcomes, and each must state its own accounting: a plan that is whole, a plan whose
    every operation was refused, and a plan the compiler never reached. The third is the one
    that used to be indistinguishable from a broken envelope.
    """

    service, _ = _service(tmp_path)
    document_id = _seeded(service)
    compiler = PermissiveSemanticTransactionCompiler(service)
    current = service.get_document(document_id)

    partial = compiler.compile(document_id, _incident_transaction(current.revision)).assessment
    assert partial.operation_accounting == "evaluated"
    assert partial.completeness == "partial"
    assert partial.accounting_problems() == []

    empty = compiler.compile(
        document_id,
        SemanticTransaction.model_validate(
            {
                "expected_revision": current.revision,
                "operations": [_rejected_update(index) for index in range(5)],
            }
        ),
    ).assessment
    assert empty.operation_accounting == "evaluated"
    assert empty.accepted_operation_count == 0
    assert empty.rejected_operation_count == 5
    assert empty.completeness == "empty"
    assert empty.accounting_problems() == []

    stale = compiler.compile(
        document_id,
        SemanticTransaction.model_validate(
            {
                "expected_revision": current.revision + 42,
                "operations": [_accepted_text(0)],
            }
        ),
    ).assessment
    assert stale.operation_accounting == "not_evaluated"
    assert stale.accepted_operation_count is None
    # The reason travels with the answer, which is what separates it from a malformed one.
    assert stale.global_failure_reason.startswith("revision_conflict")
    assert stale.accounting_problems() == []


def test_completeness_cannot_be_supplied_and_must_follow_from_the_counts() -> None:
    assert derive_completeness(proposed=197, accepted=120, rejected=77) == "partial"
    assert derive_completeness(proposed=120, accepted=120, rejected=0) == "complete"
    assert derive_completeness(proposed=0, accepted=0, rejected=0) == "empty"
    with pytest.raises(ValueError, match="must equal"):
        derive_completeness(proposed=197, accepted=120, rejected=70)


# --------------------------------------------------------------------------------------
# The raw proposal is durable evidence
# --------------------------------------------------------------------------------------


def _evidence(service: DocumentService, store, document_id: str, session_id: str, attempt: int = 0):
    current = service.get_document(document_id)
    compiler = PermissiveSemanticTransactionCompiler(service)
    transaction = _incident_transaction(current.revision)
    assessment = compiler.compile(document_id, transaction).assessment
    return build_proposal_evidence(
        session_id=session_id,
        document_id=document_id,
        proposal_attempt_index=attempt,
        raw_proposed_operations=[
            operation.model_dump(mode="json") for operation in transaction.operations
        ],
        accepted_operation_count=assessment.accepted_operation_count,
        compiled_operation_count=assessment.compiled_operation_count,
        rejected_operations=list(assessment.rejected_operations),
        validity="valid",
        compiler_version="permissive-semantic-compiler/1",
        assessment=assessment.model_dump(mode="json"),
    )


def test_evidence_keeps_the_submitted_operations_not_just_the_retained_ones(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    document_id = _seeded(service)
    evidence = _evidence(service, store, document_id, "session_test")

    assert evidence.problems() == []
    assert evidence.proposed_operation_count == INCIDENT_PROPOSED
    assert len(evidence.raw_proposed_operations) == INCIDENT_PROPOSED
    assert evidence.completeness == "partial"
    assert evidence.is_success_candidate() is False
    # Readable without the provider: the record itself carries who proposed it.
    assert evidence.evidence_schema
    assert evidence.proposal_payload_digest


def test_evidence_is_append_only_and_a_replan_adds_a_row(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    document_id = _seeded(service)

    first = _evidence(service, store, document_id, "session_replan", attempt=0)
    store.append_synthesis_proposal_evidence(first)
    second = _evidence(service, store, document_id, "session_replan", attempt=1)
    store.append_synthesis_proposal_evidence(second)

    rows = store.list_synthesis_proposal_evidence(session_id="session_replan")
    assert [row.proposal_attempt_index for row in rows] == [0, 1]
    assert store.latest_synthesis_proposal_evidence("session_replan").proposal_attempt_index == 1

    # Re-appending the same record is refused rather than silently overwriting it.
    with pytest.raises(ValueError, match="append-only"):
        store.append_synthesis_proposal_evidence(first)


def test_the_attempt_index_is_unique_within_a_session(tmp_path: Path) -> None:
    """``proposal_attempt_index`` carries the audit order, so two "attempt 2"s are a defect.

    Refused twice over: by the store with a readable reason, and by a unique index for the
    cross-process case the store's own lock cannot see.
    """

    service, store = _service(tmp_path)
    document_id = _seeded(service)
    store.append_synthesis_proposal_evidence(
        _evidence(service, store, document_id, "session_unique", attempt=0)
    )
    second = _evidence(service, store, document_id, "session_unique", attempt=1)
    store.append_synthesis_proposal_evidence(second)

    # A different row id claiming to be "attempt 1" of the same session. The store computes the
    # next index from the previous row, so this can only be reached by a caller that sets it.
    clash = second.model_copy(update={"proposal_evidence_id": "m7ev_other"})
    with pytest.raises(ValueError, match="must be unique within a session"):
        store.append_synthesis_proposal_evidence(clash)

    connection = store._connect()
    try:
        indexes = {
            row[1]: row[2]
            for row in connection.execute(
                f"PRAGMA index_list({PROPOSAL_EVIDENCE_TABLE})"
            ).fetchall()
        }
    finally:
        connection.close()
    assert indexes.get("uq_synthesis_proposal_evidence_attempt") == 1, (
        "the audit order must be enforced by a unique index, not only by the store's check"
    )

    # The database refuses it even if a caller bypasses the store's pre-check.
    import sqlite3

    connection = store._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO {PROPOSAL_EVIDENCE_TABLE} "
                "(proposal_evidence_id, session_id, document_id, proposal_attempt_index, "
                "proposed_operation_count, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "m7ev_raw",
                    "session_unique",
                    document_id,
                    1,
                    0,
                    "{}",
                    "2026-09-23T00:00:00+00:00",
                ),
            )
    finally:
        connection.close()


def test_an_incoherent_record_is_refused_before_it_reaches_the_table(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    document_id = _seeded(service)
    evidence = _evidence(service, store, document_id, "session_bad")
    broken = evidence.model_copy(
        update={"completeness": "complete"}  # 77 rejections cannot be described as complete
    )

    problems = broken.problems()
    assert any("does not follow from the counts" in problem for problem in problems)
    with pytest.raises(ValueError, match="incoherent"):
        store.append_synthesis_proposal_evidence(broken)


def test_an_unevaluated_proposal_is_still_durable_evidence(tmp_path: Path) -> None:
    """A revision conflict stops before per-operation checking, and still leaves a row.

    This is the case the previous rule silently dropped: nothing was accounted for, so the
    proposal was not recorded at all -- which is precisely why a plan that lost 39% of its own
    operations could not be inspected afterwards. The row exists, and it is honest about what
    it does not know.
    """

    service, store = _service(tmp_path)
    document_id = _seeded(service)
    reason = "revision_conflict: expected revision 9, current revision is 1"
    evidence = build_not_evaluated_evidence(
        session_id="session_unaccounted",
        document_id=document_id,
        proposal_attempt_index=0,
        raw_proposed_operations=[_rejected_update(index) for index in range(77)],
        global_failure_reason=reason,
        compiler_version="permissive-semantic-compiler/1",
    )

    assert evidence.problems() == []
    assert evidence.operation_accounting == "not_evaluated"
    assert evidence.accepted_operation_count is None
    assert evidence.rejected_operation_count is None
    assert evidence.completeness is None
    assert evidence.is_success_candidate() is False

    store.append_synthesis_proposal_evidence(evidence)
    latest = store.latest_synthesis_proposal_evidence("session_unaccounted")
    assert latest is not None
    # The raw submission survives, which is the whole point of recording it.
    assert latest.proposed_operation_count == 77
    assert len(latest.raw_proposed_operations) == 77
    # And "not evaluated" is not stored as "evaluated to zero".
    assert latest.operation_accounting == "not_evaluated"
    assert latest.accepted_operation_count is None
    assert latest.rejected_operation_count is None
    assert latest.validity is None
    assert latest.completeness is None
    assert latest.global_failure_reason == reason


def test_zero_may_not_stand_in_for_a_count_that_was_never_computed(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    document_id = _seeded(service)
    evidence = build_not_evaluated_evidence(
        session_id="session_fabricated",
        document_id=document_id,
        proposal_attempt_index=0,
        raw_proposed_operations=[_rejected_update(0)],
        global_failure_reason="revision_conflict",
    )

    problems = evidence.model_copy(update={"accepted_operation_count": 0}).problems()
    assert any("rather than reporting zero" in problem for problem in problems)
    with pytest.raises(ValueError, match="incoherent"):
        store.append_synthesis_proposal_evidence(
            evidence.model_copy(update={"accepted_operation_count": 0})
        )

    # The same rule on the assessment side: an unchecked proposal may not carry a verdict.
    fabricated = evidence.model_copy(update={"operation_accounting": "evaluated"})
    assert fabricated.problems()


def test_the_replan_prompt_carries_the_rejected_operations(tmp_path: Path) -> None:
    """The receipt is not decorative: it is what the next proposal is asked to repair.

    Read on the real planner, with the provider call intercepted, so this asserts the string
    the model would receive rather than that a helper exists.
    """

    from agentcad.agent_semantic_models import SemanticAgentPlan, SemanticAgentReplanRequest
    from agentcad.models import ProviderConfig
    from agentcad.semantic_planner import SemanticAgentPlanner

    service, _ = _service(tmp_path)
    document_id = _seeded(service)
    current = service.get_document(document_id)
    compiler = PermissiveSemanticTransactionCompiler(service)
    transaction = _incident_transaction(current.revision)
    compiled = compiler.compile(document_id, transaction)
    assessment = compiled.assessment
    assert assessment.completeness == "partial"

    planner = SemanticAgentPlanner(service=service, symbols=SymbolRegistry())
    captured: dict[str, str] = {}

    def fake_request_model_json(provider, **kwargs):
        captured.update(kwargs)
        return {
            "explanation": "repaired plan",
            "transaction": {
                "expected_revision": current.revision,
                "operations": [_accepted_text(0)],
            },
        }

    planner._request_model_json = fake_request_model_json  # type: ignore[method-assign]
    planner.replan(
        document_id,
        SemanticAgentReplanRequest(
            prompt="画一张熔盐堆气路系统总图",
            context="",
            expected_revision=current.revision,
            failed_plan=SemanticAgentPlan(explanation="the incident plan", transaction=transaction),
            attempt=1,
            provider=ProviderConfig(base_url="http://provider.test/v1", model="test-model"),
        ),
        assessment,
    )

    prompt = captured["user_prompt"]
    # The arithmetic, so the model is told what is missing rather than that it failed.
    assert "partial" in prompt
    assert f"{INCIDENT_ACCEPTED} accepted, {INCIDENT_REJECTED} rejected of {INCIDENT_PROPOSED}" in prompt
    assert "PARTIAL plan, not an invalid one" in prompt
    # And each rejected operation arrives with the diagnostic the strict compiler computed.
    first = assessment.rejected_operations[0]
    assert first.reason_code in prompt
    assert f"operation #{first.original_index}" in prompt


def test_an_unevaluated_failure_tells_the_model_the_counts_are_unknown(tmp_path: Path) -> None:
    """Replanning on an unchecked proposal must not be phrased as "you proposed nothing"."""

    from agentcad.agent_semantic_models import AgentTransactionAssessment
    from agentcad.semantic_planner import SemanticAgentPlanner

    service, _ = _service(tmp_path)
    document_id = _seeded(service)
    current = service.get_document(document_id)
    planner = SemanticAgentPlanner(service=service, symbols=SymbolRegistry())
    block = planner._completeness_block(
        AgentTransactionAssessment(
            valid=False,
            stage="compile",
            document_id=document_id,
            current_revision=current.revision,
            next_revision=current.revision + 1,
            semantic_operation_count=77,
        )
    )

    assert "not_evaluated" in block
    assert "unknown rather than zero" in block
    assert "0 accepted" not in block


def test_a_plan_that_lost_everything_is_told_so_rather_than_told_to_keep_its_work(
    tmp_path: Path,
) -> None:
    """``empty`` with rejections must not reuse the partial wording ("keep what was accepted")."""

    from agentcad.semantic_planner import SemanticAgentPlanner

    service, _ = _service(tmp_path)
    document_id = _seeded(service)
    current = service.get_document(document_id)
    compiler = PermissiveSemanticTransactionCompiler(service)
    transaction = SemanticTransaction.model_validate(
        {
            "expected_revision": current.revision,
            "operations": [_rejected_update(index) for index in range(3)],
        }
    )
    assessment = compiler.compile(document_id, transaction).assessment
    assert assessment.completeness == "empty"

    planner = SemanticAgentPlanner(service=service, symbols=SymbolRegistry())
    block = planner._completeness_block(assessment)

    assert "0 accepted, 3 rejected of 3 proposed" in block
    assert "Nothing survived" in block
    assert "keeps the accepted work" not in block
    # The receipts still arrive, which is what makes the next attempt actionable.
    assert assessment.rejected_operations[0].reason_code in block


def test_a_database_that_already_ran_version_nine_gains_the_unique_index(tmp_path: Path) -> None:
    """The constraint arrives in its own migration, so an already-migrated database is fixed.

    This is the reason version 10 exists at all: the real database on this machine had already
    executed version 9, so folding the index into version 9's body would have left it with the
    old shape while every freshly created test database had the new one.
    """

    import sqlite3

    from agentcad import database_recovery

    path = tmp_path / "m7-v9.db"
    SQLiteDocumentStore(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP INDEX uq_synthesis_proposal_evidence_attempt")
        connection.execute("PRAGMA user_version = 9")
        connection.commit()
        database_recovery._migrate(connection)
        connection.commit()
        assert database_recovery._schema_version(connection) == (
            database_recovery.CURRENT_SCHEMA_VERSION
        )
        indexes = {
            row[1]: row[2]
            for row in connection.execute(
                f"PRAGMA index_list({PROPOSAL_EVIDENCE_TABLE})"
            ).fetchall()
        }
    finally:
        connection.close()
    assert indexes.get("uq_synthesis_proposal_evidence_attempt") == 1

    # And the repaired database is usable, not just structurally current.
    store = SQLiteDocumentStore(path)
    service = DocumentService(store=store, symbols=SymbolRegistry())
    document_id = _seeded(service)
    store.append_synthesis_proposal_evidence(
        _evidence(service, store, document_id, "session_repaired", attempt=0)
    )
    assert store.latest_synthesis_proposal_evidence("session_repaired") is not None


def test_the_evidence_carrier_exists_at_the_current_schema_version(tmp_path: Path) -> None:
    from agentcad.database_recovery import CURRENT_SCHEMA_VERSION

    _, store = _service(tmp_path)
    connection = store._connect()
    try:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        connection.close()

    assert PROPOSAL_EVIDENCE_TABLE in names
    assert store.schema_version == CURRENT_SCHEMA_VERSION


# --------------------------------------------------------------------------------------
# What the client actually receives: the accounting contract on the wire
# --------------------------------------------------------------------------------------


def _api_client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from agentcad.config import Settings
    from agentcad.main import create_app

    return TestClient(
        create_app(
            Settings(
                database_path=tmp_path / "m7-contract.db",
                cors_origins=["http://localhost:5173"],
                frontend_dist=tmp_path / "missing-dist",
                diagnostics_path=tmp_path / "m7-contract.diagnostics.jsonl",
            )
        )
    )


def test_a_stale_revision_reaches_the_client_as_not_evaluated_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one legitimate route to ``not_evaluated``, read off the response envelope.

    A caller has to be able to tell this apart from a malformed response, which is why the
    reason travels with it and why no count is fabricated: the frontend recovers from this
    answer and refuses to recover from a missing contract.
    """

    from agentcad.agent_semantic_models import SemanticAgentPlan
    from agentcad.semantic_planner import SemanticAgentPlanner

    def fake_plan(self, document_id, request):
        # A revision the document never had, so the strict compiler refuses before any
        # operation is examined.
        return SemanticAgentPlan.model_validate(
            {
                "explanation": "stale revision",
                "transaction": {
                    "expected_revision": (request.expected_revision or 0) + 99,
                    "label": "stale",
                    "operations": [_accepted_text(0)],
                },
            }
        )

    monkeypatch.setattr(SemanticAgentPlanner, "plan", fake_plan)
    client = _api_client(tmp_path)
    service = client.app.state.service
    document = service.create_document(CreateDocumentRequest(name="Envelope"))

    response = client.post(
        f"/api/v2/documents/{document.id}/agent/plan-v2",
        json={
            "prompt": "draw one text element",
            "dry_run": True,
            "expected_revision": document.revision,
        },
    )
    assert response.status_code == 200
    assessment = response.json()["assessment"]

    assert assessment["operation_accounting"] == "not_evaluated"
    assert assessment["accepted_operation_count"] is None
    assert assessment["rejected_operation_count"] is None
    assert assessment["completeness"] is None
    assert assessment["rejected_operations"] is None
    # The invariant the frontend reads: unevaluated says why, and does not claim zero.
    assert assessment["global_failure_reason"]
    assert assessment["issues"]

    store = SQLiteDocumentStore(tmp_path / "m7-contract.db")
    session_id = response.json()["session_id"]
    evidence = store.latest_synthesis_proposal_evidence(session_id)
    assert evidence is not None, "an unevaluated proposal still leaves a durable row"
    assert evidence.operation_accounting == "not_evaluated"
    assert evidence.global_failure_reason
    assert evidence.accepted_operation_count is None


def test_a_plan_whose_every_operation_is_refused_reaches_the_client_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other end of the same decision: 0 accepted is a claim, not an absence."""

    from agentcad.agent_semantic_models import SemanticAgentPlan
    from agentcad.semantic_planner import SemanticAgentPlanner

    def fake_plan(self, document_id, request):
        return SemanticAgentPlan.model_validate(
            {
                "explanation": "nothing survives",
                "transaction": {
                    "expected_revision": request.expected_revision,
                    "label": "all refused",
                    "operations": [_rejected_update(index) for index in range(7)],
                },
            }
        )

    monkeypatch.setattr(SemanticAgentPlanner, "plan", fake_plan)
    client = _api_client(tmp_path)
    service = client.app.state.service
    document = service.create_document(CreateDocumentRequest(name="Envelope"))

    response = client.post(
        f"/api/v2/documents/{document.id}/agent/plan-v2",
        json={
            "prompt": "update elements that do not exist",
            "dry_run": True,
            "expected_revision": document.revision,
        },
    )
    assert response.status_code == 200
    assessment = response.json()["assessment"]

    assert assessment["operation_accounting"] == "evaluated"
    assert assessment["accepted_operation_count"] == 0
    assert assessment["rejected_operation_count"] == 7
    assert assessment["completeness"] == "empty"
    assert len(assessment["rejected_operations"]) == 7
    # An evaluated-envelope invariant, checked where the client will check it.
    assert assessment["semantic_operation_count"] == 7
    assert (
        assessment["accepted_operation_count"] + assessment["rejected_operation_count"]
        == assessment["semantic_operation_count"]
    )


# --------------------------------------------------------------------------------------
# A partial proposal cannot become a completed session
# --------------------------------------------------------------------------------------


def _harness(service: DocumentService, store: SQLiteDocumentStore, document_id: str):
    harness = AgentHarnessService(service, store)
    session = harness.create_session(
        AgentSessionCreateRequest(document_id=document_id, actor="web-user", provider="llm")
    )
    return harness, session


def test_a_partial_proposal_may_not_be_recorded_as_a_completed_session(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    document_id = _seeded(service)
    harness, session = _harness(service, store, document_id)
    harness.record_synthesis_proposal_evidence(_evidence(service, store, document_id, session.id))

    with pytest.raises(HarnessError) as failure:
        harness.complete_session(session.id, end_revision=None)

    assert failure.value.code == "synthesis_proposal_incomplete"
    # The refusal names the arithmetic, so it is diagnosable rather than merely negative.
    assert "120 accepted" in str(failure.value)
    assert "77 rejected" in str(failure.value)

    # The session is genuinely still open, not silently completed-then-corrected.
    assert harness.get_session(session.id).status == "active"

    # A non-success outcome is still allowed: the refusal is about claiming completion.
    closed = harness.complete_session(session.id, end_revision=None, status="failed")
    assert closed.status == "failed"


def test_a_complete_proposal_may_complete(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    document_id = _seeded(service)
    harness, session = _harness(service, store, document_id)
    current = service.get_document(document_id)
    compiler = PermissiveSemanticTransactionCompiler(service)
    transaction = SemanticTransaction.model_validate(
        {
            "expected_revision": current.revision,
            "operations": [_accepted_text(index) for index in range(5)],
        }
    )
    assessment = compiler.compile(document_id, transaction).assessment
    harness.record_synthesis_proposal_evidence(
        build_proposal_evidence(
            session_id=session.id,
            document_id=document_id,
            proposal_attempt_index=0,
            raw_proposed_operations=[
                operation.model_dump(mode="json") for operation in transaction.operations
            ],
            accepted_operation_count=assessment.accepted_operation_count,
            compiled_operation_count=assessment.compiled_operation_count,
            rejected_operations=[],
            validity="valid",
        )
    )

    assert harness.complete_session(session.id, end_revision=None).status == "completed"


def test_a_session_with_no_proposal_is_unaffected(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    document_id = _seeded(service)
    harness, session = _harness(service, store, document_id)

    assert harness.complete_session(session.id, end_revision=None).status == "completed"


# --------------------------------------------------------------------------------------
# The read-only catalogue audit: hidden is not missing
# --------------------------------------------------------------------------------------


def test_the_audit_separates_a_suppressed_symbol_from_one_that_does_not_exist() -> None:
    registry = SymbolRegistry()

    hidden = audit.audit_symbol_key(registry, "flow_transmitter", requested_type="FT")
    missing = audit.audit_symbol_key(registry, "molecular_sieve_bed", requested_type="分子筛吸附床")

    assert registry.exists("flow_transmitter") is True
    assert hidden.verdict == "hidden"
    assert "withheld" in hidden.detail
    assert hidden.is_representable() is False
    # A one-line fix lives behind this verdict, which is why the two must not be merged.

    assert registry.exists("molecular_sieve_bed") is False
    assert missing.verdict == "missing"
    assert missing.symbol_key is None
    assert missing.is_representable() is False


def test_a_missing_requirement_is_reported_with_alternatives_rather_than_substituted() -> None:
    registry = SymbolRegistry()
    entry = audit.audit_symbol_key(
        registry,
        "buffer_tank_zzz",
        requested_type="缓冲罐",
        requested_tag="V-101",
        source_requirement="供气侧缓冲罐",
    )

    assert entry.verdict == "missing"
    assert entry.requested_tag == "V-101"
    assert entry.source_requirement == "供气侧缓冲罐"
    # Offered for a human to choose; the contract forbids applying one automatically.
    assert "buffer_tank" in entry.available_alternatives


def test_a_visible_symbol_audits_clean() -> None:
    registry = SymbolRegistry()
    entry = audit.audit_symbol_key(registry, "centrifugal_pump", requested_type="离心泵")

    assert entry.verdict == "visible"
    assert entry.is_representable() is True
    assert entry.available_alternatives == ()


def test_the_audit_order_asks_existence_before_visibility() -> None:
    assert audit.AUDIT_ORDER == ("existence", "visibility", "compiler_support", "renderer_support")
