"""REST/CLI surface tests for the M4 validation system.

What matters here is not "does the route return 200" but the boundaries:

* the profile is resolved server-side - no route accepts a rule, severity, threshold or
  waiver from the caller;
* validating is a read: revision, elements and history are unchanged afterwards, and the
  optional audit event is a *read* event rather than ``revision.created``;
* readiness is evidence: it can say ``eligible``/``not_eligible`` and cannot say
  ``approved``;
* ``as_of`` is explicit rather than ambient, because waivers expire.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agentcad.cli import main
from agentcad.main import create_app
from agentcad.models import (
    AddElementOperation,
    CreateDocumentRequest,
    Point,
    SymbolElement,
    TransactionRequest,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry


@pytest.fixture()
def client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PID_AGENT_DATABASE_PATH", str(tmp_path / "validation-api.db"))
    monkeypatch.delenv("PID_AGENT_VALIDATION_PROFILE", raising=False)
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def _audit_records(client: TestClient) -> list[dict]:
    payload = client.get("/api/v2/audit/records", params={"limit": 20}).json()
    return payload["records"] if isinstance(payload, dict) else payload


def _run_cli(argv: list[str], *, code: int = 0) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == code


def _duplicate_tag_document(client: TestClient) -> str:
    created = client.post("/api/v2/documents", json={"name": "Validation fixture"}).json()
    operations = [
        {
            "op": "add_element",
            "element": {
                "type": "symbol",
                "id": f"hv_{index}",
                "symbol_key": "gate_valve",
                "label": "HV-101",
                "position": {"x": 10.0 + index * 80.0, "y": 10.0},
                "width": 30,
                "height": 30,
            },
        }
        for index in range(2)
    ]
    response = client.post(
        f"/api/v2/documents/{created['id']}/transactions",
        json={"expected_revision": 0, "operations": operations, "label": "fixture"},
    )
    assert response.status_code == 200, response.text
    return created["id"]


def test_the_profile_route_reports_the_resolved_rule_bundle(client: TestClient) -> None:
    payload = client.get("/api/v2/validation/profile").json()

    assert payload["profile_id"] == "built-in"
    assert len(payload["fingerprint"]) == 64
    assert payload["engine_version"]
    assert all(rule["rule_source"] == "built-in" for rule in payload["rules"])
    assert "diagram-quality.QUALITY_SCORE_BELOW_TARGET" in {rule["rule_id"] for rule in payload["rules"]}
    # The one configurable number is visible, with its default, before anyone argues
    # about a finding.
    threshold_rule = next(
        rule
        for rule in payload["rules"]
        if rule["rule_id"] == "diagram-quality.QUALITY_SCORE_BELOW_TARGET"
    )
    assert threshold_rule["threshold"] == 95.0


def test_validation_is_a_read_and_the_result_is_bound(client: TestClient) -> None:
    document_id = _duplicate_tag_document(client)
    before = client.get(f"/api/v2/documents/{document_id}").json()
    history_before = client.get(f"/api/v2/documents/{document_id}/history").json()

    response = client.get(f"/api/v2/validation/documents/{document_id}")
    assert response.status_code == 200, response.text
    result = response.json()

    assert result["document_id"] == document_id
    assert result["revision"] == before["revision"]
    assert len(result["content_hash"]) == 64
    assert result["rule_bundle_fingerprint"]
    assert result["engine_version"]
    assert len(result["symbol_registry_fingerprint"]) == 64
    assert result["evaluated_at"]
    assert sorted(result["validators_run"]) == [
        "diagram-quality",
        "engineering-graph",
        "engineering-report",
    ]
    issue = next(item for item in result["issues"] if item["code"] == "TAG_DUPLICATE")
    assert issue["severity"] == "error"
    assert issue["waiver_status"] == "not_waived"
    assert "threshold" in issue and issue["threshold"] is None
    assert issue["rule_source"] == "built-in"

    after = client.get(f"/api/v2/documents/{document_id}").json()
    history_after = client.get(f"/api/v2/documents/{document_id}/history").json()
    assert after == before
    assert history_after == history_before


def test_validation_audit_is_a_read_event_not_a_revision(client: TestClient) -> None:
    document_id = _duplicate_tag_document(client)
    before = client.get(f"/api/v2/documents/{document_id}/history").json()

    response = client.get(
        f"/api/v2/validation/documents/{document_id}", params={"audit": True}
    )
    assert response.status_code == 200, response.text
    result = response.json()

    records = _audit_records(client)
    validation_records = [
        record for record in records if record["event_type"] == "validation.completed"
    ]
    assert validation_records, "audit=true must record a read/tool evidence event"
    record = validation_records[0]
    assert record["document_id"] == document_id
    assert record["evidence"]["validation_hash"] == result["result_hash"]
    assert record["evidence"]["rule_bundle_fingerprint"] == result["rule_bundle_fingerprint"]
    assert "result_revision" not in record or record.get("result_revision") in (None, "")

    after = client.get(f"/api/v2/documents/{document_id}/history").json()
    assert after == before, "validation must never append document history"


def test_a_naive_as_of_is_refused(client: TestClient) -> None:
    document_id = _duplicate_tag_document(client)

    response = client.get(
        f"/api/v2/validation/documents/{document_id}", params={"as_of": "2026-09-19T12:00:00"}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "as_of_not_timezone_aware"


def test_an_unknown_document_is_a_404(client: TestClient) -> None:
    assert client.get("/api/v2/validation/documents/doc_missing").status_code == 404
    assert (
        client.get("/api/v2/validation/documents/doc_missing/release-readiness").status_code
        == 404
    )


def test_release_readiness_reports_evidence_not_an_approval(client: TestClient) -> None:
    document_id = _duplicate_tag_document(client)

    response = client.get(f"/api/v2/validation/documents/{document_id}/release-readiness")
    assert response.status_code == 200, response.text
    readiness = response.json()

    assert readiness["state"] in {"eligible", "not_eligible"}
    assert readiness["human_approval_required"] is True
    assert readiness["release_validator_version"]
    assert readiness["readiness_hash"]
    assert readiness["validation_hash"]
    assert readiness["evaluated_at"]
    assert set(readiness) & {"approved", "approval", "ifc", "afc", "signature"} == set()


def test_release_readiness_audit_records_the_readiness_hash(client: TestClient) -> None:
    document_id = _duplicate_tag_document(client)

    readiness = client.get(
        f"/api/v2/validation/documents/{document_id}/release-readiness",
        params={"audit": True},
    ).json()

    records = _audit_records(client)
    event = next(
        record
        for record in records
        if record["event_type"] == "release.readiness.assessed"
    )
    assert event["evidence"]["readiness_hash"] == readiness["readiness_hash"]
    assert event["evidence"]["human_approval_required"] is True


def test_validation_surfaces_are_get_only(client: TestClient) -> None:
    """No validation route may be a write: the whole subtree is read-only by shape."""

    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    validation_paths = {
        path: sorted(method.upper() for method in operations)
        for path, operations in paths.items()
        if path.startswith("/api/v2/validation")
    }

    assert validation_paths, "the validation surface must be registered"
    for path, methods in validation_paths.items():
        assert methods == ["GET"], f"{path} exposes {methods}; validation is read-only"


def _code_only(source: str) -> str:
    """The module's code, with comments and string literals removed.

    Prose is not a write path: these modules deliberately *name* the states they cannot
    produce, and a check that flagged the explanation would push the explanation out of
    the code. Only executable tokens are inspected.
    """

    import io
    import tokenize

    parts: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in {tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE}:
            continue
        parts.append(token.string)
    return "".join(parts)


def test_no_validation_module_has_a_write_path() -> None:
    """An Agent must not be able to approve a release; prove it structurally.

    A behavioural test can only show that the paths it exercises approve nothing. This
    shows the modules that decide validation and readiness contain no write call and do
    not emit ``revision.created`` at all, so "validation is a read" cannot regress by
    someone adding a convenient store call.
    """

    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "agentcad"
    write_calls = (
        ".save(",
        ".delete(",
        "apply_transaction(",
        "create_document(",
        "_stage_mutation(",
        "record_event(",
        "undo(",
        "redo(",
    )
    for name in (
        "validation_models.py",
        "validation_rules.py",
        "validation_profile.py",
        "validation_engine.py",
        "release_validator.py",
    ):
        source = (package / name).read_text(encoding="utf-8")
        code = _code_only(source)
        for call in write_calls:
            assert call not in code, f"{name} must not call {call!r}"
        assert "revision.created" not in source, f"{name} must not emit a revision event"
        assert "Approved" not in code and "IFC" not in code and "AFC" not in code, (
            f"{name} must not define an approval state"
        )


def test_the_cli_validates_and_gates_with_exit_codes(capsys, tmp_path) -> None:
    database = str(tmp_path / "validation-cli.db")
    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    created = service.create_document(CreateDocumentRequest(name="CLI fixture"))
    service.apply_transaction(
        created.id,
        TransactionRequest(
            expected_revision=0,
            operations=[
                AddElementOperation(
                    element=SymbolElement(
                        id="cli_hv",
                        symbol_key="gate_valve",
                        label="HV-1",
                        position=Point(x=10, y=10),
                        width=30,
                        height=30,
                    )
                )
            ],
            label="fixture",
        ),
    )

    _run_cli(["validate", created.id, "--summary", "--database", database])
    payload = json.loads(capsys.readouterr().out)
    assert payload["document_id"] == created.id
    assert payload["counts"]["blocker"] == 0
    assert payload["rule_bundle_fingerprint"]
    assert payload["evaluated_at"]
    assert {issue["waiver_status"] for issue in payload["issues"]} == {"not_waived"}

    # A blocker makes the exit code 2, which is what an unattended job gates on.
    _run_cli(
        [
            "release-readiness",
            created.id,
            "--database",
            database,
            "--summary",
        ],
        code=0,
    )
    readiness = json.loads(capsys.readouterr().out)
    assert readiness["state"] in {"eligible", "not_eligible"}
    assert readiness["human_approval_required"] is True


def test_the_cli_refuses_a_naive_as_of(capsys, tmp_path) -> None:
    database = str(tmp_path / "validation-cli.db")
    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    created = service.create_document(CreateDocumentRequest(name="CLI fixture"))

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "validate",
                created.id,
                "--as-of",
                "2026-09-19T12:00:00",
                "--database",
                database,
            ]
        )

    assert excinfo.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "as_of_not_timezone_aware"
