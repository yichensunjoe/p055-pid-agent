"""REST / CLI / MCP parity for the self-repair surfaces (baseline M5 §M5-5, §C).

Three surfaces, one server-side run. What this file defends is that they are *the same run*: for
one staged finding it obtains the repair record from REST, from the CLI and from MCP, strips the
transport framing (an HTTP body, a line of stdout and a tool's return value are all JSON objects),
and asserts the payloads are equal **in full** — the request binding, every attempt with its
failure code, the scope, the selected plan hash, the oracle's verdict and the apply proof.

It also pins the things that make the surfaces trustworthy rather than merely consistent: a
refused candidate writes nothing on any surface, and the audit chain binds the repair to the
validation it was planned against and to the validation it produced, without a prompt or a secret.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentcad.agent_semantic_models import ClearDocumentOperation
from agentcad.cli import main
from agentcad.config import Settings
from agentcad.main import create_app
from agentcad.mcp_server import build_mcp_server
from agentcad.models import CreateDocumentRequest, TransactionRequest
from agentcad.repair_models import RepairBudgets, repair_payload
from agentcad.repair_orchestrator import (
    RepairOrchestrator,
    RepairStaleEvidence,
    build_repair_request,
    repair_document_finding,
)
from agentcad.repair_planner import RepairPlanDraft
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry
from agentcad.validation_engine import run_validation
from agentcad.validation_profile import built_in_profile, resolve_profile

_FINDING_CODE = "LINE_MEDIUM_MISSING"
_FINDING_VALIDATOR = "engineering-report"
_AS_OF = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
_AS_OF_TEXT = "2026-09-20T12:00:00Z"


def _seed(database: str) -> str:
    """One document with exactly one repairable finding: a line with no medium.

    Built from raw low-level operations rather than the semantic compiler, because this fixture
    is about the *surfaces*, not about the compiler: what the three surfaces must agree on is one
    finding that the same orchestrator repairs.
    """

    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    document = service.create_document(CreateDocumentRequest(name="Repair parity fixture"))
    service.apply_transaction(
        document.id,
        TransactionRequest.model_validate(
            {
                "expected_revision": 0,
                "label": "repair parity fixture",
                "operations": [
                    {
                        "op": "add_element",
                        "element": {
                            "type": "symbol",
                            "id": "v1",
                            "symbol_key": "ball_valve",
                            "label": "HV-101",
                            "position": {"x": 200.0, "y": 300.0},
                            "width": 60,
                            "height": 60,
                            "properties": {"tag": "HV-101"},
                        },
                    },
                    {
                        "op": "add_element",
                        "element": {
                            "type": "symbol",
                            "id": "v2",
                            "symbol_key": "ball_valve",
                            "label": "HV-102",
                            "position": {"x": 700.0, "y": 300.0},
                            "width": 60,
                            "height": 60,
                            "properties": {"tag": "HV-102"},
                        },
                    },
                    {
                        "op": "add_element",
                        "element": {
                            "type": "connector",
                            "id": "p1",
                            "points": [
                                {"x": 260.0, "y": 330.0},
                                {"x": 700.0, "y": 330.0},
                            ],
                            "routing": "orthogonal",
                            "process_tag": "L-001",
                            "medium": "",
                            "nominal_diameter": "DN50",
                            "flow_direction": "forward",
                        },
                    },
                ],
            }
        ),
    )
    return document.id


@pytest.fixture()
def database(tmp_path) -> str:
    path = tmp_path / "repair-surfaces.db"
    _seed(str(path))
    return str(path)


@pytest.fixture()
def document_id(database: str) -> str:
    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    documents = service.list_documents()
    assert documents, "fixture produced no document"
    return documents[0].id


def _service(database: str) -> DocumentService:
    return DocumentService(SQLiteDocumentStore(database), SymbolRegistry())


def _settings(database: str) -> Settings:
    path = Path(database)
    return Settings(database_path=path, cors_origins=[], frontend_dist=path.parent / "dist")


def _client(database: str) -> TestClient:
    """REST and MCP both build from settings; both are pointed at the fixture database."""

    return TestClient(create_app(_settings(database)))


def _strip(payload: dict) -> dict:
    """Drop transport framing so three surfaces can be compared as payloads."""

    return {key: value for key, value in payload.items() if key != "release_state"}


def _fixture_has_the_finding(database: str) -> None:
    service = _service(database)
    profile = resolve_profile(built_in_profile())
    for document in service.list_documents():
        result = run_validation(
            service.get_document(document.id), service.symbols, profile, service=service
        )
        if any(issue.code == _FINDING_CODE for issue in result.issues):
            return
    raise AssertionError("fixture produced no repairable finding")


def test_all_three_surfaces_publish_the_identical_repair_record(
    database: str, document_id: str, capsys
):
    _fixture_has_the_finding(database)

    client = _client(database)
    rest = client.post(
        f"/api/v2/documents/{document_id}/repair/preview",
        json={
            "code": _FINDING_CODE,
            "validator_id": _FINDING_VALIDATOR,
            "hop": 1,
            "permits_creation": False,
        },
        params={"as_of": _AS_OF_TEXT},
    )
    assert rest.status_code == 200, rest.text
    rest_payload = rest.json()

    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "repair",
                document_id,
                "--code",
                _FINDING_CODE,
                "--validator",
                _FINDING_VALIDATOR,
                "--preview",
                "--as-of",
                _AS_OF_TEXT,
                "--database",
                database,
            ]
        )
    assert exit_info.value.code == 0
    cli_payload = json.loads(capsys.readouterr().out)
    cli_payload.pop("preview", None)

    server, _transport = build_mcp_server(_settings(database))
    mcp_payload = server._tool_manager.get_tool("preview_repair_finding").fn(
        document_id=document_id,
        code=_FINDING_CODE,
        validator_id=_FINDING_VALIDATOR,
        as_of=_AS_OF_TEXT,
    )

    assert _strip(rest_payload) == _strip(cli_payload) == _strip(mcp_payload)
    assert rest_payload["schema"] == "pid-agent.repair-run-result"
    assert rest_payload["target"]["code"] == _FINDING_CODE
    assert rest_payload["applied"]["applied"] is False


def test_the_apply_route_writes_once_and_reads_the_release_state(
    database: str, document_id: str
):
    client = _client(database)
    before = _service(database).get_document(document_id)
    response = client.post(
        f"/api/v2/documents/{document_id}/repair",
        json={"code": _FINDING_CODE},
        params={"as_of": _AS_OF_TEXT},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["applied"]["applied"] is True
    assert payload["selected_attempt"] == 1
    # The release verdict comes from the release validator, never from the repair's own result.
    assert payload["release_state"] in {"eligible", "not_eligible"}
    after = _service(database).get_document(document_id)
    assert after.revision == payload["applied"]["result_revision"]
    assert after.revision > before.revision


def test_an_unknown_finding_is_reported_as_stale_not_as_a_repair(database: str, document_id: str):
    client = _client(database)
    response = client.post(
        f"/api/v2/documents/{document_id}/repair/preview",
        json={"code": "NOT_A_REAL_FINDING"},
    )
    assert response.status_code == 409
    assert "no_repairable_finding" in response.json()["detail"]


def test_a_refused_candidate_writes_nothing_on_the_apply_route(database: str, document_id: str):
    """The apply route is safe on a drawing the caller cannot undo."""

    service = _service(database)
    profile = resolve_profile(built_in_profile())
    document = service.get_document(document_id)
    result = run_validation(document, service.symbols, profile, service=service)
    issue = next(issue for issue in result.issues if issue.code == _FINDING_CODE)

    class WipeoutPlanner:
        planner_id = "test-double"

        def plan(self, context):
            return RepairPlanDraft(
                operations=[ClearDocumentOperation()], rationale="deliberately out of scope"
            )

    request = build_repair_request(
        document,
        result,
        issue,
        registry=service.symbols,
        profile=profile,
        budgets=RepairBudgets(),
    )
    run = RepairOrchestrator(
        service, profile, planner=WipeoutPlanner(), apply_enabled=True
    ).run(request)

    assert run.status != "repaired"
    assert not run.applied.applied
    assert service.get_document(document_id).revision == document.revision


def test_the_audit_chain_binds_the_repair_to_both_validations(database: str, document_id: str):
    """§M5-5: target hash, attempt count, selected plan, final hash and scope, in the chain."""

    service = _service(database)
    profile = resolve_profile(built_in_profile())
    run = repair_document_finding(
        service, profile, document_id, target_code=_FINDING_CODE, now=_AS_OF
    )
    assert run.repaired

    records = service.audit.audit_trail(document_id=document_id, limit=50)
    writes = [
        record
        for record in records
        if record.event_type == "revision.created"
        and record.tool_name == "repair_drawing"
    ]
    completions = [record for record in records if record.event_type == "repair.completed"]
    assert len(writes) == 1 and len(completions) == 1

    write = writes[0]
    assert write.validation_status == "valid"
    evidence = write.evidence["validation"]
    assert evidence["validation_hash"] == run.validation_hash
    assert evidence["scope_fingerprint"] == run.protected_pre.scope_fingerprint
    assert evidence["attempt_count"] == run.attempt_count
    assert write.evidence["metadata"]["plan_hash"] == run.selected_plan_hash

    completion = completions[0].evidence
    assert completion["target_validation_hash"] == run.validation_hash
    assert completion["attempt_count"] == run.attempt_count
    assert completion["selected_plan_hash"] == run.selected_plan_hash
    assert completion["final_validation_hash"] == run.applied.post_validation_hash
    assert completion["scope_fingerprint"] == run.protected_pre.scope_fingerprint

    # No secret, no prompt and no repaired payload may appear anywhere in the chain.
    chain = json.dumps(
        [record.model_dump(mode="json") for record in records], ensure_ascii=False
    )
    for forbidden in ("api_key", "Authorization", "Bearer ", "reasoning"):
        assert forbidden not in chain


def test_an_unbindable_request_records_no_completion(database: str, document_id: str):
    service = _service(database)
    profile = resolve_profile(built_in_profile())

    with pytest.raises(RepairStaleEvidence):
        repair_document_finding(
            service, profile, document_id, target_code="NOT_A_REAL_FINDING", now=_AS_OF
        )

    events = service.audit.audit_trail(document_id=document_id)
    assert not [record for record in events if record.event_type == "repair.completed"]


def test_the_canonical_payload_hides_machine_scoped_fields(database: str, document_id: str):
    """Three surfaces can only agree if they publish the same object, not three summaries."""

    service = _service(database)
    profile = resolve_profile(built_in_profile())
    run = repair_document_finding(
        service,
        profile,
        document_id,
        target_code=_FINDING_CODE,
        apply_enabled=False,
        now=_AS_OF,
    )
    payload = repair_payload(run)

    for volatile in ("document_id", "audit_record_id", "transaction_hash", "planner", "timings"):
        assert volatile not in payload
    assert payload["request_hash"] == run.request_hash
    assert payload["scope"]["allowed_element_ids"]
