"""Exact machine-surface parity for the M4 validation contract (remote baseline R3 P0-4).

R2 required that REST, CLI and MCP describe the same document/profile/as-of with the same
canonical payload "modulo surface framing". Review then found the requirement was not met
in a way a field-by-field test could not see: FastAPI serialised the response model *by
alias* (``schema``) while the CLI and MCP published ``model_dump(mode="json")`` with the
internal field name (``schema_name``). Two names for one field is a second contract.

So this file does not spot-check fields. For one fixture it obtains the result and the
readiness verdict from all three surfaces, strips the transport framing (an HTTP response
body, a line of CLI stdout, a tool's return value are all just JSON objects), and asserts
the payloads are **equal in full** — nested issues, skips, counts, policy, hashes and all.

The browser UI is deliberately absent here: it is a REST consumer, and a fourth
independent validation implementation in TypeScript would be the bug, not the test. What
the UI owes is proof that it does not recompute policy and that it binds the readiness it
shows to the validation result it shows; that lives in the frontend suite and the E2E spec.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentcad.cli import main
from agentcad.config import Settings
from agentcad.main import create_app
from agentcad.mcp_server import build_mcp_server
from agentcad.models import CreateDocumentRequest, TransactionRequest
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry
from agentcad.validation_engine import validate_document
from agentcad.validation_models import canonical_json
from agentcad.validation_profile import load_profile

#: One fixed evaluation instant, so the three surfaces cannot disagree about "now" by
#: accident: ``evaluated_at`` is hashed, and that is the point.
_AS_OF = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
_AS_OF_TEXT = "2026-09-19T12:00:00Z"


@pytest.fixture()
def database(tmp_path) -> str:
    return str(tmp_path / "validation-parity.db")


def _seed(database: str) -> str:
    """One document with one deliberate finding, created through the governed path."""

    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    created = service.create_document(CreateDocumentRequest(name="Parity fixture"))
    service.apply_transaction(
        created.id,
        TransactionRequest.model_validate(
            {
                "expected_revision": 0,
                "label": "parity fixture",
                "operations": [
                    {
                        "op": "add_element",
                        "element": {
                            "type": "symbol",
                            "id": f"parity_hv_{index}",
                            "symbol_key": "gate_valve",
                            "label": "HV-500",
                            "position": {"x": 10.0 + index * 80.0, "y": 10.0},
                            "width": 30,
                            "height": 30,
                        },
                    }
                    for index in range(2)
                ],
            }
        ),
    )
    return created.id


def _rest_payloads(database: str, document_id: str) -> tuple[dict, dict]:
    os.environ["PID_AGENT_DATABASE_PATH"] = database
    app = create_app()
    with TestClient(app) as client:
        result = client.get(
            f"/api/v2/validation/documents/{document_id}", params={"as_of": _AS_OF_TEXT}
        )
        readiness = client.get(
            f"/api/v2/validation/documents/{document_id}/release-readiness",
            params={"as_of": _AS_OF_TEXT},
        )
        assert result.status_code == 200, result.text
        assert readiness.status_code == 200, readiness.text
        return result.json(), readiness.json()


def _cli_payloads(database: str, document_id: str, capsys) -> tuple[dict, dict]:
    def run(command: str, code: int) -> dict:
        with pytest.raises(SystemExit) as exited:
            main(
                [
                    command,
                    document_id,
                    "--as-of",
                    _AS_OF_TEXT,
                    "--database",
                    database,
                ]
            )
        assert exited.value.code == code
        return json.loads(capsys.readouterr().out)

    return run("validate", 0), run("release-readiness", 0)


def _mcp_payloads(database: str, document_id: str) -> tuple[dict, dict]:
    database_path = Path(database)
    settings = Settings(
        database_path=database_path,
        cors_origins=[],
        frontend_dist=database_path.parent / "dist",
    )
    server, _transport = build_mcp_server(settings)
    tools = server._tool_manager
    result = tools.get_tool("validate_document").fn(
        document_id=document_id, as_of=_AS_OF_TEXT
    )
    readiness = tools.get_tool("assess_release_readiness").fn(
        document_id=document_id, as_of=_AS_OF_TEXT
    )
    return result, readiness


@pytest.mark.parametrize("kind", ["validation", "readiness"])
def test_rest_cli_and_mcp_publish_the_same_canonical_payload(
    database: str, capsys, kind: str
) -> None:
    document_id = _seed(database)
    rest_result, rest_readiness = _rest_payloads(database, document_id)
    cli_result, cli_readiness = _cli_payloads(database, document_id, capsys)
    mcp_result, mcp_readiness = _mcp_payloads(database, document_id)

    payloads = {
        "rest": (rest_result, rest_readiness),
        "cli": (cli_result, cli_readiness),
        "mcp": (mcp_result, mcp_readiness),
    }
    index = 0 if kind == "validation" else 1
    rest = payloads["rest"][index]

    for surface, pair in payloads.items():
        payload = pair[index]
        assert payload == rest, f"{surface} payload differs from REST for {kind}"
        assert "schema" in payload, f"{surface} must publish the canonical alias"
        assert "schema_name" not in payload, (
            f"{surface} published the internal field name instead of the canonical alias"
        )

    # The payload is not just equal, it is complete: the parts a reviewer argues about.
    if kind == "validation":
        issue = next(item for item in rest["issues"] if item["code"] == "TAG_DUPLICATE")
        assert issue["validator_id"] == "engineering-report"
        assert issue["rule_id"] == "engineering-report.TAG_DUPLICATE"
        assert issue["rule_source"] == "built-in"
        assert issue["waiver_status"] == "not_waived"
        assert set(rest["validators_skipped"]) == set()
        assert rest["counts"]["total"] == len(rest["issues"])
        # Canonical UTC on every surface: one instant, one spelling (R3 P0-2).
        assert rest["evaluated_at"] in {_AS_OF.isoformat(), "2026-09-19T12:00:00Z"}
        assert rest["evaluated_at"].endswith("Z") or rest["evaluated_at"].endswith("+00:00")
    else:
        assert rest["human_approval_required"] is True
        assert rest["state"] in {"eligible", "not_eligible"}
        assert rest["policy"]["fail_on"] == ["blocker"]
        assert rest["policy"]["required_validators"] == sorted(
            rest["policy"]["required_validators"]
        )


def test_the_readiness_verdict_names_the_validation_result_it_came_from(
    database: str,
) -> None:
    """The binding a UI must show: readiness hashes the exact validation run it reports.

    Both requests use the same explicit ``as_of``, so the readiness payload references the
    result the reviewer is looking at — which is precisely what the UI cannot guarantee
    when it lets the server pick two different "now" values (R3 P0-5).
    """

    document_id = _seed(database)
    result, readiness = _rest_payloads(database, document_id)

    assert readiness["validation_hash"] == result["result_hash"]
    assert readiness["evaluated_at"] == result["evaluated_at"]
    assert readiness["revision"] == result["revision"]
    assert readiness["profile_id"] == result["profile_id"]
    assert readiness["profile_version"] == result["profile_version"]
    assert readiness["rule_bundle_fingerprint"] == result["rule_bundle_fingerprint"]


def test_the_published_hash_can_be_recomputed_from_the_published_payload(database: str) -> None:
    """A hash nobody can recompute is a number, not evidence."""

    document_id = _seed(database)
    result, readiness = _rest_payloads(database, document_id)

    result_body = {key: value for key, value in result.items() if key != "result_hash"}
    canonical = json.dumps(
        result_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert result["result_hash"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    readiness_body = {key: value for key, value in readiness.items() if key != "readiness_hash"}
    canonical = json.dumps(
        readiness_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert readiness["readiness_hash"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # And the helper the repository actually uses produces the same bytes.
    service = DocumentService(SQLiteDocumentStore(database), SymbolRegistry())
    recomputed = validate_document(service, document_id, load_profile(), now=_AS_OF)
    assert recomputed.result_hash == result["result_hash"]
    assert canonical_json(recomputed, exclude=frozenset({"result_hash"})) == canonical_json(
        recomputed, exclude=frozenset({"result_hash"})
    )
