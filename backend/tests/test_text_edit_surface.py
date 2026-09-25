"""The text-edit surface: the second sentence redraws the drawing through one transaction.

Companion to ``test_text_plan_surface.py``: the create path. What is proved here is the
edit path -- stale guards refuse, drift refuses before the write, a redraw is one
revision with the fresh identity chain and the lineage edges, and the new spec row
commits beside it. Every refusal is asserted to leave the document byte-identical.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentcad import m7_layout_contract as contract
from agentcad.config import Settings
from agentcad.m7_semantic_specs import spec_digest
from agentcad.main import create_app
from agentcad.surface_contract import http_binding
from agentcad.typesafe import TypesafeClient

SENTENCE = "添加一个缓冲罐 V-101，添加一台燃料盐泵 P-101，把 V-101 接到 P-101"
EDIT = "再添加一个缓冲罐 V-102，把 V-102 接到 V-101"


class _JudgeStub:
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
            database_path=tmp_path / "nl-edit.db",
            cors_origins=["http://localhost:5173"],
            frontend_dist=tmp_path / "missing-dist",
        )
    )
    with TestClient(app) as test_client:
        yield test_client


def _new_document(client: TestClient, name: str = "NL 改图") -> str:
    response = client.post("/api/v2/documents", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _draw(client: TestClient, document_id: str, sentence: str, **overrides):
    body = {"sentence": sentence, "api_key": "test-key"}
    body.update(overrides)
    return client.post(f"/api/v2/documents/{document_id}/agent/text-plan", json=body)


def _edit(client: TestClient, document_id: str, sentence: str, revision: int, digest: str, **overrides):
    body = {
        "sentence": sentence,
        "expected_revision": revision,
        "base_spec_digest": digest,
        "api_key": "test-key",
    }
    body.update(overrides)
    return client.post(f"/api/v2/documents/{document_id}/agent/text-edit", json=body)


def _source(client: TestClient, document_id: str):
    return client.app.state.service.store.latest_semantic_spec(document_id)


def test_an_edit_commits_one_redraw_with_the_new_source_and_lineage(client: TestClient) -> None:
    document_id = _new_document(client)
    drawn = _draw(client, document_id, SENTENCE)
    assert drawn.status_code == 200, drawn.text

    base = _source(client, document_id)
    assert base is not None and base.revision == 1
    edited = _edit(client, document_id, EDIT, revision=1, digest=base.spec_digest)
    assert edited.status_code == 200, edited.text
    result = edited.json()
    assert result["committed"] is True
    assert result["revision"] == 2
    assert result["completeness"] == "complete"
    assert {entity["tag"] for entity in result["spec"]["entities"]} == {
        "V-101",
        "P-101",
        "V-102",
    }
    assert len(result["spec"]["connections"]) == 2
    assert result["edited_from_revision"] == 1
    assert result["edited_from_spec_digest"] == base.spec_digest
    assert result["replaced_from_materialization_digest"]

    service = client.app.state.service
    document = service.get_document(document_id)
    assert document.revision == 2
    assert len(document.elements), "the redrawn drawing exists"
    drawn_words = {getattr(element, "text", "") for element in document.elements}
    assert {"V-101", "P-101", "V-102"} <= drawn_words

    # The new source row is beside the new revision, and the audit carries the fresh
    # identity chain plus the lineage edges.
    new_source = _source(client, document_id)
    assert new_source is not None and new_source.revision == 2
    assert new_source.spec_digest == spec_digest(new_source.spec)
    record = service.audit.revision_evidence(document_id, 2).audit_record
    metadata = record.evidence["metadata"]
    recorded = metadata[contract.M7_PROVENANCE_METADATA_KEY]
    required = set(contract.M7_PROVENANCE_REQUIRED_IDENTITIES) | set(
        contract.MATERIALIZATION_PROVENANCE_VERSION_FIELDS
    )
    assert required <= set(recorded), sorted(required - set(recorded))
    assert metadata["edited_from_revision"] == "1"
    assert metadata["edited_from_spec_digest"] == base.spec_digest
    assert metadata["replaced_from_materialization_digest"] == (
        result["replaced_from_materialization_digest"]
    )
    # The old revision is evidence: it is not rewritten.
    assert service.store.semantic_spec(document_id, 1).spec_digest == base.spec_digest


def test_dry_run_edits_nothing_and_reports_the_projected_spec(client: TestClient) -> None:
    document_id = _new_document(client)
    assert _draw(client, document_id, SENTENCE).status_code == 200
    base = _source(client, document_id)

    edited = _edit(
        client, document_id, EDIT, revision=1, digest=base.spec_digest, dry_run=True
    )
    assert edited.status_code == 200, edited.text
    result = edited.json()
    assert result["committed"] is False
    assert result["revision"] is None
    assert {entity["tag"] for entity in result["spec"]["entities"]} == {
        "V-101",
        "P-101",
        "V-102",
    }
    service = client.app.state.service
    assert service.get_document(document_id).revision == 1
    assert _source(client, document_id).spec_digest == base.spec_digest


def test_a_stale_expected_revision_is_refused_without_side_effects(client: TestClient) -> None:
    document_id = _new_document(client)
    assert _draw(client, document_id, SENTENCE).status_code == 200
    base = _source(client, document_id)

    edited = _edit(client, document_id, EDIT, revision=0, digest=base.spec_digest)
    assert edited.status_code == 409
    assert edited.json()["detail"]["code"] == "semantic_source_stale"
    service = client.app.state.service
    assert service.get_document(document_id).revision == 1
    assert _source(client, document_id).spec_digest == base.spec_digest


def test_a_stale_base_digest_is_refused_without_side_effects(client: TestClient) -> None:
    document_id = _new_document(client)
    assert _draw(client, document_id, SENTENCE).status_code == 200
    base = _source(client, document_id)

    edited = _edit(client, document_id, EDIT, revision=1, digest="f" * 64)
    assert edited.status_code == 409
    assert edited.json()["detail"]["code"] == "semantic_source_stale"
    service = client.app.state.service
    assert service.get_document(document_id).revision == 1
    assert _source(client, document_id).spec_digest == base.spec_digest


def test_a_drawing_without_a_stored_source_is_not_reverse_engineered(client: TestClient) -> None:
    document_id = _new_document(client)
    base = _source(client, document_id)
    assert base is None

    edited = _edit(client, document_id, EDIT, revision=0, digest="f" * 64)
    assert edited.status_code == 422
    assert edited.json()["detail"]["code"] == "semantic_source_unavailable"
    assert client.app.state.service.get_document(document_id).revision == 0


def test_foreign_or_human_drift_refuses_the_redraw_before_any_write(client: TestClient) -> None:
    document_id = _new_document(client)
    assert _draw(client, document_id, SENTENCE).status_code == 200
    base = _source(client, document_id)

    service = client.app.state.service
    current = service.get_document(document_id)
    from agentcad.models import TransactionRequest

    service.apply_transaction(
        document_id,
        TransactionRequest(
            operations=[
                {
                    "op": "add_element",
                    "element": {
                        "id": "foreign_note",
                        "type": "rectangle",
                        "x": 40.0,
                        "y": 40.0,
                        "width": 120.0,
                        "height": 40.0,
                        "layer_id": "layer_default",
                        "system_id": "system_default",
                        "style": {
                            "stroke": "#111827",
                            "fill": "none",
                            "stroke_width": 1.5,
                            "opacity": 1,
                            "dash": [],
                        },
                    },
                }
            ],
            expected_revision=current.revision,
            label="human addition",
        ),
    )

    edited = _edit(client, document_id, EDIT, revision=1, digest=base.spec_digest)
    assert edited.status_code == 409
    assert "not exactly the prior materialization" in edited.json()["detail"]
    after = service.get_document(document_id)
    assert after.revision == 2, "the refused redraw must not advance the revision"
    assert any(element.id == "foreign_note" for element in after.elements)


def test_a_partial_edit_cannot_be_committed(client: TestClient) -> None:
    document_id = _new_document(client)
    assert _draw(client, document_id, SENTENCE).status_code == 200
    base = _source(client, document_id)

    edited = _edit(
        client,
        document_id,
        EDIT + "，顺便说明一下布局",
        revision=1,
        digest=base.spec_digest,
    )
    assert edited.status_code == 422
    detail = edited.json()["detail"]
    assert detail["code"] == "typesafe_spec_partial"
    assert detail["undelivered"]
    service = client.app.state.service
    assert service.get_document(document_id).revision == 1


def test_the_contract_declaration_and_the_live_route_agree(client: TestClient) -> None:
    binding = http_binding("POST", "/api/v2/documents/{document_id}/agent/text-edit")
    assert binding is not None
    assert binding.tool == "edit_text_plan"
    assert binding.audited is True


def test_a_document_that_moved_after_the_callers_read_is_stale_not_a_new_baseline(
    client: TestClient,
) -> None:
    """TOCTOU: the caller read r1/S1, then another governed write advanced the document.
    The edit must refuse -- the caller's expected_revision binds the edit end to end and
    is never swapped for a freshly-read 'current'."""

    document_id = _new_document(client)
    assert _draw(client, document_id, SENTENCE).status_code == 200
    base = _source(client, document_id)

    renamed = client.put(
        f"/api/v2/documents/{document_id}/name",
        json={"name": "renamed", "expected_revision": 1},
    )
    assert renamed.status_code == 200, renamed.text

    edited = _edit(client, document_id, EDIT, revision=1, digest=base.spec_digest)
    assert edited.status_code == 409
    detail = edited.json()["detail"]
    assert (
        (isinstance(detail, dict) and detail.get("code") == "semantic_source_stale")
        or "revision 2" in str(detail)
    ), "a document that moved after the caller's read is refused, however it drifted"

    service = client.app.state.service
    assert service.get_document(document_id).revision == 2
    assert _source(client, document_id).revision == 1, "no spec row for a refused edit"
    record = service.audit.revision_evidence(document_id, 2).audit_record
    assert "edited_from_revision" not in record.evidence["metadata"]


def test_a_same_id_human_style_edit_refuses_the_redraw_and_keeps_the_change(
    client: TestClient,
) -> None:
    """The destructive guard is wider than canonical identity: a human restyle of an M7
    element -- same id, same position, same tag, invisible to row reconciliation -- must
    refuse the redraw and survive it."""

    document_id = _new_document(client)
    assert _draw(client, document_id, SENTENCE).status_code == 200
    base = _source(client, document_id)

    service = client.app.state.service
    current = service.get_document(document_id)
    symbol_element = next(e for e in current.elements if e.type == "symbol")
    from agentcad.models import TransactionRequest

    service.apply_transaction(
        document_id,
        TransactionRequest(
            operations=[
                {
                    "op": "update_element",
                    "element_id": symbol_element.id,
                    "patch": {"style": {**symbol_element.style.model_dump(), "stroke": "#ff0000"}},
                }
            ],
            expected_revision=current.revision,
            label="human restyle",
        ),
    )

    edited = _edit(client, document_id, EDIT, revision=1, digest=base.spec_digest)
    assert edited.status_code == 409
    assert "modified since the prior materialization" in edited.json()["detail"]

    after = service.get_document(document_id)
    assert after.revision == 2, "the refused redraw must not advance the revision"
    restyled = next(e for e in after.elements if e.id == symbol_element.id)
    assert restyled.style.stroke == "#ff0000", "the human change survives the refusal"
    assert _source(client, document_id).revision == 1


def test_a_same_id_human_metadata_edit_also_refuses_the_redraw(client: TestClient) -> None:
    document_id = _new_document(client)
    assert _draw(client, document_id, SENTENCE).status_code == 200
    base = _source(client, document_id)

    service = client.app.state.service
    current = service.get_document(document_id)
    symbol_element = next(e for e in current.elements if e.type == "symbol")
    from agentcad.models import TransactionRequest

    service.apply_transaction(
        document_id,
        TransactionRequest(
            operations=[
                {
                    "op": "update_element",
                    "element_id": symbol_element.id,
                    "patch": {"metadata": {**symbol_element.metadata, "human_note": "touched"}},
                }
            ],
            expected_revision=current.revision,
            label="human metadata note",
        ),
    )

    edited = _edit(client, document_id, EDIT, revision=1, digest=base.spec_digest)
    assert edited.status_code == 409
    assert "modified since the prior materialization" in edited.json()["detail"]
    assert service.get_document(document_id).revision == 2


def test_a_redraw_whose_derived_canvas_no_longer_fits_is_refused(client: TestClient) -> None:
    document_id = _new_document(client)
    assert _draw(client, document_id, SENTENCE).status_code == 200
    base = _source(client, document_id)

    additions = "，".join(f"再添加一个缓冲罐 V-{index}" for index in range(102, 114))
    edited = _edit(client, document_id, additions, revision=1, digest=base.spec_digest)
    assert edited.status_code == 422, edited.text
    detail = edited.json()["detail"]
    assert "canvas" in str(detail).lower()

    service = client.app.state.service
    assert service.get_document(document_id).revision == 1
    assert _source(client, document_id).spec_digest == base.spec_digest
    created = [
        record
        for record in service.audit.audit_trail(document_id=document_id, limit=10)
        if record.event_type == "revision.created"
    ]
    assert len(created) == 1, "exactly the first drawing's revision may exist"
