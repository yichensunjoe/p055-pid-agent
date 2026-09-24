"""The natural-language surface: one sentence in, one governed revision out.

The companion tests for the ``text-plan`` route, declared in ``surface_contract.py`` with the
tool ``draw_text_plan``. What is proved here is the surface behaviour, not the planner's (that
is ``test_m7_text_planner.py``) or the materializer's (that is
``test_m7_layout_materialization.py``): the route drives them in order, refuses a non-empty
target *before* anything is written, and stamps the audit with the identity chain plus the
contract version the surface ran against.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentcad import m7_layout_contract as contract
from agentcad.config import Settings
from agentcad.main import create_app
from agentcad.surface_contract import http_binding
from agentcad.typesafe import TypesafeClient

SENTENCE = "添加一个缓冲罐 V-101，添加一台燃料盐泵 P-101，把 V-101 接到 P-101"


class _JudgeStub:
    """Answers every question with the alphabetically-first candidate, confidently.

    The surface test does not judge judgment quality -- that is the planner's own suite -- it
    proves the route drives a real planner and a real chain end to end without a network.
    """

    def __call__(self, state: dict, questions: dict) -> dict:
        return {
            "model": "judge-stub",
            "latency_ms": 1.0,
            "answers": {
                question: {
                    "choice": sorted(definition["criteria"])[0],
                    "confidence": 0.9,
                }
                for question, definition in questions.items()
            },
        }


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(TypesafeClient, "judge", _JudgeStub())
    app = create_app(
        Settings(
            database_path=tmp_path / "nl-surface.db",
            cors_origins=["http://localhost:5173"],
            frontend_dist=tmp_path / "missing-dist",
        )
    )
    with TestClient(app) as test_client:
        yield test_client


def _new_document(client: TestClient, name: str = "NL 出图") -> str:
    response = client.post("/api/v2/documents", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _draw(client: TestClient, document_id: str, sentence: str, **overrides):
    body = {"sentence": sentence, "api_key": "test-key"}
    body.update(overrides)
    return client.post(f"/api/v2/documents/{document_id}/agent/text-plan", json=body)


def _audit_records(client: TestClient, document_id: str):
    service = client.app.state.service
    return service.audit.audit_trail(document_id=document_id, limit=50)


def test_a_sentence_becomes_a_committed_drawing_with_identity(client: TestClient) -> None:
    document_id = _new_document(client)
    response = _draw(client, document_id, SENTENCE)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["committed"] is True
    assert result["revision"] == 1
    assert result["canonical_layout_digest"]
    assert result["materialization_digest"]
    assert {entity["tag"] for entity in result["spec"]["entities"]} == {"V-101", "P-101"}
    assert len(result["spec"]["connections"]) == 1

    service = client.app.state.service
    document = service.get_document(document_id)
    assert document.revision == 1
    assert document.elements, "the drawing exists"
    # The words on the drawing are the tags the sentence wrote.
    drawn_words = {getattr(element, "text", "") for element in document.elements}
    assert {"V-101", "P-101"} <= drawn_words

    # The audit carries the M7 identity chain under the reserved namespace, plus the contract
    # version the surface read -- the provenance is asserted against the *declared* key set,
    # not against what the same code path would return for itself. The namespace is written
    # *with* the revision, so it cannot contain a resulting_revision: that half of the record
    # is closed from what the writer committed, after the write.
    record = service.audit.revision_evidence(document_id, 1).audit_record
    recorded = record.evidence["metadata"][contract.M7_PROVENANCE_METADATA_KEY]
    required = set(contract.M7_PROVENANCE_REQUIRED_IDENTITIES) | set(
        contract.MATERIALIZATION_PROVENANCE_VERSION_FIELDS
    )
    assert required <= set(recorded), sorted(required - set(recorded))
    assert "resulting_revision" not in recorded
    assert record.evidence["metadata"]["layout_contract_version"] == (
        contract.M7_LAYOUT_CONTRACT_VERSION
    )


def test_dry_run_plans_and_finalizes_without_touching_the_document(client: TestClient) -> None:
    document_id = _new_document(client)
    response = _draw(client, document_id, SENTENCE, dry_run=True)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["committed"] is False
    assert result["revision"] is None
    assert result["canonical_layout_digest"]
    assert result["materialization_digest"]

    service = client.app.state.service
    assert service.get_document(document_id).revision == 0
    assert not service.get_document(document_id).elements
    assert not [
        record
        for record in _audit_records(client, document_id)
        if record.event_type == "revision.created"
    ]


def test_a_nonempty_target_is_refused_without_side_effects(client: TestClient) -> None:
    """The companion negative test for the surface declaration: a refused draw is a no-op."""

    document_id = _new_document(client)
    first = _draw(client, document_id, SENTENCE)
    assert first.status_code == 200, first.text
    service = client.app.state.service
    elements_before = len(service.get_document(document_id).elements)

    second = _draw(client, document_id, "添加一个缓冲罐 V-201，添加一台燃料盐泵 P-201，把 P-201 接到 V-201")
    assert second.status_code == 409, second.text
    assert "empty" in second.json()["detail"].lower()

    after = service.get_document(document_id)
    assert after.revision == 1, "a refused draw must not advance the revision"
    assert len(after.elements) == elements_before, "a refused draw must not add elements"
    assert len(
        [
            record
            for record in _audit_records(client, document_id)
            if record.event_type == "revision.created"
        ]
    ) == 1, "exactly the one committed revision may exist"


def test_an_unknown_document_is_a_404_not_a_session(client: TestClient) -> None:
    response = _draw(client, "doc-ghost", SENTENCE)
    assert response.status_code == 404


def test_a_sentence_without_devices_is_422_and_names_the_sentence(client: TestClient) -> None:
    document_id = _new_document(client)
    response = _draw(client, document_id, "把所有的东西都连起来")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "typesafe_spec_no_device"
    assert "把所有的东西都连起来" in detail["message"]
    assert client.app.state.service.get_document(document_id).revision == 0


def test_every_device_skipped_is_422_not_an_empty_revision(client: TestClient) -> None:
    """All clauses uncertain -> nothing to draw; committing an empty drawing would be a
    revision claiming it drew something."""

    document_id = _new_document(client)

    class _UncertainStub:
        def __call__(self, state: dict, questions: dict) -> dict:
            return {
                "model": "judge-stub",
                "answers": {
                    question: {"choice": sorted(d["criteria"])[0], "confidence": 0.05}
                    for question, d in questions.items()
                },
            }

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(TypesafeClient, "judge", _UncertainStub())
    try:
        response = _draw(client, document_id, SENTENCE)
    finally:
        monkeypatch.undo()
    assert response.status_code == 422
    assert client.app.state.service.get_document(document_id).revision == 0


def test_the_contract_declaration_and_the_live_route_agree(client: TestClient) -> None:
    """The phase-4 declaration in the layout contract names this exact live route, and the
    surface module reads the contract it is declared against -- both directions, so a rename
    on one side turns a test red instead of leaving a dead declaration."""

    binding = http_binding("POST", contract.PHASE_4_NL_SURFACE_ROUTE)
    assert binding is not None, "surface_contract must declare the phase-4 route"
    assert binding.tool == "draw_text_plan"
    assert binding.audited is True

    import pathlib

    import agentcad.api_semantic_agent as surface_module

    source = pathlib.Path(surface_module.__file__).read_text(encoding="utf-8")
    assert "m7_layout_contract" in source, "the declared surface module must read the contract"
    assert contract.PHASE_4_NL_SURFACE_MODULE == "api_semantic_agent.py"
