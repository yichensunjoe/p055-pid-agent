"""M6 Phase-2B D3: the human review surface, proved by what it records and refuses.

The decision route is a governance write: every recorded decision carries its audit fact in
the same transaction, the pinned source is re-verified before any of it, and a drifted
source refuses with zero writes. The reads are pure. And the identity discipline is the
Gate's D87-2 ruling as observable behaviour: the reviewer name is declared, the token only
authenticates the request, and the audit evidence says exactly that.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agentcad.m6_source_adapter import M6SourceAdapter
from agentcad.main import create_app
from agentcad.models import (
    CircleElement,
    CreateDocumentRequest,
    Document,
    Layer,
    LineElement,
    Point,
    TextElement,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore, StoredDocument

REVIEWER = "engineer.joe"
TARGET_ID = "doc_m6d3_target"


@pytest.fixture()
def app(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PID_AGENT_DATABASE_PATH", str(tmp_path / "m6-review.db"))
    return create_app()


@pytest.fixture()
def client(app) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def shared_app(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PID_AGENT_DATABASE_PATH", str(tmp_path / "m6-review-shared.db"))
    monkeypatch.setenv("PID_AGENT_DEPLOYMENT_MODE", "shared")
    monkeypatch.setenv("PID_AGENT_API_TOKEN", "m6-shared-test-token")
    monkeypatch.setenv("PID_AGENT_CORS_ORIGINS", "http://localhost:4174")
    return create_app()


def _store(app) -> SQLiteDocumentStore:
    return app.state.service.store


def _block_lines(prefix: str, x: float, y: float, block: str) -> list:
    return [
        LineElement(
            id=f"{prefix}_a",
            start=Point(x=x, y=y),
            end=Point(x=x + 20, y=y),
            metadata={"cad_block": block, "cad_layer": "EQUIP"},
        ),
        LineElement(
            id=f"{prefix}_b",
            start=Point(x=x + 20, y=y),
            end=Point(x=x + 20, y=y + 20),
            metadata={"cad_block": block, "cad_layer": "EQUIP"},
        ),
        LineElement(
            id=f"{prefix}_c",
            start=Point(x=x + 20, y=y + 20),
            end=Point(x=x, y=y + 20),
            metadata={"cad_block": block, "cad_layer": "EQUIP"},
        ),
        LineElement(
            id=f"{prefix}_d",
            start=Point(x=x, y=y + 20),
            end=Point(x=x, y=y),
            metadata={"cad_block": block, "cad_layer": "EQUIP"},
        ),
        CircleElement(
            id=f"{prefix}_e",
            center=Point(x=x + 10, y=y + 10),
            radius=8,
            metadata={"cad_block": block, "cad_layer": "EQUIP"},
        ),
    ]


def _source_document(document_id: str = "doc_m6d3_source") -> Document:
    return Document(
        id=document_id,
        name="M6 D3 source",
        revision=3,
        layers=[Layer(id="layer_default", name="Default"), Layer(id="layer_cad", name="EQUIP")],
        elements=[
            *_block_lines("p1", 100, 100, "centrifugal_pump"),
            *_block_lines("p2", 300, 100, "centrifugal_pump"),
            TextElement(
                id="t1",
                position=Point(x=110, y=130),
                text="P-201",
                font_size=12,
                metadata={"cad_layer": "EQUIP"},
            ),
            TextElement(
                id="t2",
                position=Point(x=310, y=130),
                text="P-202",
                font_size=12,
                metadata={"cad_layer": "EQUIP"},
            ),
        ],
    )


def _seed(app) -> str:
    """One real source document, one empty target, one real ingest. Returns source id."""

    store = _store(app)
    source = _source_document()
    store.save(StoredDocument(document=source, undo_stack=[], redo_stack=[]))
    service: DocumentService = app.state.service
    target = service.create_document(
        CreateDocumentRequest(name="M6 D3 target", width=1600, height=900)
    )
    assert target.id != source.id
    adapter = M6SourceAdapter(store, app.state.service.symbols)
    summary = adapter.ingest(
        adapter.pin_source(source.id), target_document_id=target.id
    )
    assert summary.filed, "the seed must actually file candidates"
    return source.id


def _queue(client: TestClient, source_id: str) -> list[dict]:
    response = client.get(f"/api/v2/documents/{source_id}/m6/candidates")
    assert response.status_code == 200, response.text
    return response.json()["candidates"]


def _tag_candidate(client: TestClient, source_id: str, tag: str) -> dict:
    for row in _queue(client, source_id):
        if row["proposed_semantics"].get("equipment_tag") == tag:
            return row
    raise AssertionError(f"no equipment_tag candidate for {tag}")


def _decide(client: TestClient, source_id: str, candidate_id: str, payload: dict):
    return client.post(
        f"/api/v2/documents/{source_id}/m6/candidates/{candidate_id}/decisions",
        json=payload,
    )


def _audit_records(client: TestClient, headers: dict | None = None) -> list[dict]:
    payload = client.get(
        "/api/v2/audit/records", params={"limit": 200}, headers=headers or {}
    ).json()
    records = payload["records"] if isinstance(payload, dict) else payload
    return [record for record in records if record["event_type"] == "m6.review.decision"]


# --------------------------------------------------------------------------------------
# The read surface
# --------------------------------------------------------------------------------------


def test_the_queue_lists_candidates_with_derived_status(client: TestClient, app) -> None:
    source_id = _seed(app)
    payload = client.get(f"/api/v2/documents/{source_id}/m6/candidates").json()
    assert payload["document_id"] == source_id
    assert payload["source"]["available"] is True
    rows = payload["candidates"]
    assert rows, "the seed queue must not be empty"
    assert {row["review_status"] for row in rows} == {"needs_review"}
    assert {row["candidate_type"] for row in rows} >= {
        "symbol_class",
        "equipment_tag",
        "annotation_role",
    }
    for row in rows:
        assert row["region_id"].startswith("m6reg_")
        assert row["producer"]["key"] == "deterministic_rule_engine"
        assert row["target_document_id"]


def test_candidate_detail_carries_evidence_elements_and_history(client: TestClient, app) -> None:
    source_id = _seed(app)
    row = _tag_candidate(client, source_id, "P-201")
    detail = client.get(
        f"/api/v2/documents/{source_id}/m6/candidates/{row['candidate_id']}"
    ).json()
    assert detail["review_status"] == "needs_review"
    assert detail["candidate"]["region"]["element_refs"]
    assert detail["evidence_elements"], "the evidence view must resolve the region's elements"
    assert any(element.get("text") == "P-201" for element in detail["evidence_elements"])
    # The filing event is on the record.
    assert [decision["kind"] for decision in detail["decisions"]] == ["filed"]
    assert detail["target"]["exists"] is True


def test_reads_write_nothing(client: TestClient, app) -> None:
    source_id = _seed(app)
    store = _store(app)
    audits_before = len(store.all_audit_records())
    revision_before = store.get(source_id).document.revision

    client.get(f"/api/v2/documents/{source_id}/m6/candidates")
    row = _tag_candidate(client, source_id, "P-201")
    client.get(f"/api/v2/documents/{source_id}/m6/candidates/{row['candidate_id']}")

    assert len(store.all_audit_records()) == audits_before
    assert store.get(source_id).document.revision == revision_before


def test_an_unknown_source_queue_is_a_404(client: TestClient) -> None:
    response = client.get("/api/v2/documents/doc_nothing/m6/candidates")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "source_unknown"


def test_pagination_is_bounded(client: TestClient, app) -> None:
    source_id = _seed(app)
    page = client.get(
        f"/api/v2/documents/{source_id}/m6/candidates", params={"limit": 2, "offset": 1}
    ).json()
    assert page["limit"] == 2
    assert len(page["candidates"]) == 2
    assert page["total"] > 2
    capped = client.get(
        f"/api/v2/documents/{source_id}/m6/candidates", params={"limit": 100000}
    ).json()
    assert capped["limit"] == 200


def test_an_unknown_status_filter_is_a_422(client: TestClient, app) -> None:
    source_id = _seed(app)
    response = client.get(
        f"/api/v2/documents/{source_id}/m6/candidates", params={"status": "applied-ish"}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_review_status"


def test_a_v7_row_without_target_document_id_still_reads(client: TestClient, app) -> None:
    source_id = _seed(app)
    row = _tag_candidate(client, source_id, "P-201")
    store = _store(app)
    candidate = store.get_semantic_candidate(row["candidate_id"])
    assert candidate is not None
    payload = candidate.model_dump(mode="json", by_alias=True)
    payload.pop("target_document_id")
    old_id = "cand_m6_v7legacy"
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """
            INSERT INTO semantic_candidates (
                candidate_id, source_document_id, source_revision, region_id,
                candidate_type, review_status_at_creation, producer_key,
                producer_version, contract_version, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                old_id,
                candidate.artifact.source_document_id,
                candidate.artifact.source_revision,
                candidate.region.region_id,
                candidate.candidate_type,
                "proposed",
                candidate.producer.key,
                candidate.producer.version,
                candidate.contract,
                candidate.created_at.isoformat(),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        connection.commit()

    detail = client.get(f"/api/v2/documents/{source_id}/m6/candidates/{old_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["candidate"]["target_document_id"] == ""


# --------------------------------------------------------------------------------------
# Decisions: recorded, audited, atomic
# --------------------------------------------------------------------------------------


def test_confirm_writes_decision_finding_and_audit_atomically(client: TestClient, app) -> None:
    source_id = _seed(app)
    row = _tag_candidate(client, source_id, "P-201")
    audits_before = len(_store(app).all_audit_records())

    response = _decide(
        client,
        source_id,
        row["candidate_id"],
        {
            "action": "confirm",
            "reviewer_identity": REVIEWER,
            "reviewer_action": "核对了原图位号与泵簇",
            "note": "与源图一致",
        },
    )
    assert response.status_code == 200, response.text
    outcome = response.json()
    assert outcome["status"] == "confirmed"
    assert outcome["changed"] is True
    assert outcome["finding_id"].startswith("m6find_")
    assert outcome["review_decision_id"].startswith("m6dec_")

    store = _store(app)
    assert store.get_confirmed_finding(outcome["finding_id"]) is not None
    decisions = store.list_review_decisions(row["candidate_id"])
    assert [decision.kind for decision in decisions] == ["filed", "human_confirm"]
    # The audit fact: exactly one new record, carrying the declared-identity four-tuple.
    audits = _audit_records(client)
    assert len(store.all_audit_records()) == audits_before + 1
    assert len(audits) == 1
    record = audits[0]
    assert record["actor"] == REVIEWER
    assert record["evidence"]["reviewer_attribution"] == REVIEWER
    assert record["evidence"]["identity_assurance"] == "declared"
    assert record["evidence"]["authentication_evidence"] == "local-process"
    assert record["evidence"]["action"] == "confirm"
    assert record["evidence"]["candidate_id"] == row["candidate_id"]


def test_reject_records_the_human_decision(client: TestClient, app) -> None:
    source_id = _seed(app)
    row = _tag_candidate(client, source_id, "P-202")
    response = _decide(
        client,
        source_id,
        row["candidate_id"],
        {
            "action": "reject",
            "reviewer_identity": REVIEWER,
            "reviewer_action": "位号不属于该簇",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    store = _store(app)
    assert store.get_confirmed_finding(response.json()["finding_id"] or "none") is None
    decisions = store.list_review_decisions(row["candidate_id"])
    assert [decision.kind for decision in decisions] == ["filed", "human_reject"]
    assert decisions[-1].reviewer_identity == REVIEWER
    assert len(_audit_records(client)) == 1


def test_confirm_requires_a_declared_identity(client: TestClient, app) -> None:
    source_id = _seed(app)
    row = _tag_candidate(client, source_id, "P-201")
    response = _decide(
        client,
        source_id,
        row["candidate_id"],
        {"action": "confirm", "reviewer_identity": "  ", "reviewer_action": "核对"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "reviewer_identity_required"


def test_confirm_requires_a_reviewer_action(client: TestClient, app) -> None:
    source_id = _seed(app)
    row = _tag_candidate(client, source_id, "P-201")
    response = _decide(
        client,
        source_id,
        row["candidate_id"],
        {"action": "confirm", "reviewer_identity": REVIEWER, "reviewer_action": ""},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "reviewer_action_required"


def test_confirming_an_unresolved_candidate_is_refused(client: TestClient, app) -> None:
    source_id = _seed(app)
    unresolved = next(
        row
        for row in _queue(client, source_id)
        if row["candidate_type"] == "unresolved"
    ) if any(row["candidate_type"] == "unresolved" for row in _queue(client, source_id)) else None
    # The two-cluster fixture has no unresolved row; seed one explicitly.
    if unresolved is None:
        store = _store(app)
        from agentcad.m6_candidate_models import (
            CandidateEvidence,
            Confidence,
            ProducerRef,
            ProposedSemantics,
            SemanticCandidate,
        )
        from agentcad.m6_region import build_source_region

        artifact = M6SourceAdapter(store, app.state.service.symbols).pin_source(source_id)
        region = build_source_region(
            artifact=artifact,
            geometry_selector=None,
            element_refs=["t1"],
            text_spans=[],
        )
        candidate = SemanticCandidate(
            candidate_id="cand_m6_unresolved_fixture",
            artifact=artifact,
            region=region,
            target_document_id=TARGET_ID,
            candidate_type="unresolved",
            proposed_semantics=ProposedSemantics(unresolved_reason="ambiguous"),
            confidence=Confidence(value=0.0, source="deterministic_rule"),
            evidence=[CandidateEvidence(kind="observed_text", detail="two readings tie")],
            producer=ProducerRef(key="deterministic_rule_engine", version="0.1.0"),
        )
        store.file_semantic_candidates(
            [(candidate, _filed(candidate))],
            source_document_id=artifact.source_document_id,
            expected_source_revision=artifact.source_revision,
            expected_source_content_hash=artifact.content_hash,
        )
        unresolved_row = candidate.candidate_id
    else:
        unresolved_row = unresolved["candidate_id"]

    response = _decide(
        client,
        source_id,
        unresolved_row,
        {
            "action": "confirm",
            "reviewer_identity": REVIEWER,
            "reviewer_action": "试图确认一条无事实候选",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "unresolved_has_no_finding"


def _filed(candidate):
    from agentcad.m6_candidate_core import filing_decision

    return filing_decision(candidate)


# --------------------------------------------------------------------------------------
# Source re-verification before decisions (D88-1 carried into review)
# --------------------------------------------------------------------------------------


def test_a_decision_on_a_drifted_source_is_refused_without_writes(client: TestClient, app) -> None:
    source_id = _seed(app)
    row = _tag_candidate(client, source_id, "P-201")
    store = _store(app)
    audits_before = len(store.all_audit_records())
    decisions_before = len(store.list_review_decisions(row["candidate_id"]))

    moved = _source_document(source_id).model_copy(update={"revision": 4})
    store.save(StoredDocument(document=moved, undo_stack=[], redo_stack=[]))

    response = _decide(
        client,
        source_id,
        row["candidate_id"],
        {
            "action": "confirm",
            "reviewer_identity": REVIEWER,
            "reviewer_action": "核对",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_revision_drift"
    assert len(store.list_review_decisions(row["candidate_id"])) == decisions_before
    assert len(store.all_audit_records()) == audits_before


def test_a_decision_on_a_deleted_source_is_refused(client: TestClient, app) -> None:
    source_id = _seed(app)
    row = _tag_candidate(client, source_id, "P-201")
    store = _store(app)
    assert store.delete(source_id, expected_revision=3)

    # The queue stays readable (evidence outlives the drawing) and reports the loss.
    queue = client.get(f"/api/v2/documents/{source_id}/m6/candidates")
    assert queue.status_code == 200
    assert queue.json()["source"] == {
        "available": False,
        "code": "source_snapshot_unavailable",
        "revision": None,
    }
    response = _decide(
        client,
        source_id,
        row["candidate_id"],
        {"action": "reject", "reviewer_identity": REVIEWER, "reviewer_action": "拒绝"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_snapshot_unavailable"


# --------------------------------------------------------------------------------------
# Baseline recheck and conflict resolution (N6 chain)
# --------------------------------------------------------------------------------------


def _confirm_p201(client: TestClient, source_id: str) -> dict:
    row = _tag_candidate(client, source_id, "P-201")
    response = _decide(
        client,
        source_id,
        row["candidate_id"],
        {
            "action": "confirm",
            "reviewer_identity": REVIEWER,
            "reviewer_action": "核对了原图位号与泵簇",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_recheck_unchanged_reports_no_change_and_writes_nothing(client: TestClient, app) -> None:
    source_id = _seed(app)
    confirmed = _confirm_p201(client, source_id)
    decisions_before = len(_store(app).list_review_decisions(confirmed["candidate_id"]))

    response = _decide(
        client,
        source_id,
        confirmed["candidate_id"],
        {"action": "recheck", "reviewer_identity": REVIEWER},
    )
    assert response.status_code == 200, response.text
    assert response.json()["changed"] is False
    assert response.json()["status"] == "confirmed"
    assert len(_store(app).list_review_decisions(confirmed["candidate_id"])) == decisions_before


def test_a_moved_target_baseline_falls_into_conflict_and_resolves(client: TestClient, app) -> None:
    source_id = _seed(app)
    confirmed = _confirm_p201(client, source_id)
    candidate_id = confirmed["candidate_id"]
    target_id = _queue(client, source_id)[0]["target_document_id"]

    # Somebody else creates an object tagged P-201 in the target before any apply.
    target = _store(app).get(target_id).document
    from agentcad.models import SymbolElement

    _store(app).save(
        StoredDocument(
            document=target.model_copy(
                update={
                    "revision": target.revision + 1,
                    "elements": [
                        *target.elements,
                        SymbolElement(
                            id="sym_p201",
                            symbol_key="centrifugal_pump",
                            position=Point(x=50, y=50),
                            width=60,
                            height=60,
                            label="P-201",
                        ),
                    ],
                }
            ),
            undo_stack=[],
            redo_stack=[],
        )
    )

    response = _decide(
        client, source_id, candidate_id, {"action": "recheck", "reviewer_identity": REVIEWER}
    )
    assert response.status_code == 200, response.text
    assert response.json()["changed"] is True
    assert response.json()["status"] == "conflicted"
    kinds = [
        decision.kind for decision in _store(app).list_review_decisions(candidate_id)
    ]
    assert kinds == ["filed", "human_confirm", "conflict_detected"]

    # Resolving costs a new human decision with an explicit resolution.
    resolved = _decide(
        client,
        source_id,
        candidate_id,
        {
            "action": "resolve_conflict",
            "reviewer_identity": "engineer.jane",
            "reviewer_action": "复核目标文档已有 P-201，保留既有对象",
            "conflict_resolution": "目标侧已存在 P-201；本候选按 keep_existing 解除",
            "resolution_choice": "keep_existing",
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "needs_review"
    assert resolved.json()["review_decision_id"] != confirmed["review_decision_id"]
    audits = _audit_records(client)
    # audit reads are newest-first; the three decisions are confirm, recheck, resolve.
    assert sorted(record["actor"] for record in audits) == sorted(
        [REVIEWER, REVIEWER, "engineer.jane"]
    )
    assert audits[0]["evidence"]["action"] == "resolve_conflict"


def test_resolve_conflict_requires_the_resolution(client: TestClient, app) -> None:
    source_id = _seed(app)
    confirmed = _confirm_p201(client, source_id)
    response = _decide(
        client,
        source_id,
        confirmed["candidate_id"],
        {
            "action": "resolve_conflict",
            "reviewer_identity": REVIEWER,
            "reviewer_action": "复核",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "conflict_resolution_required"


# --------------------------------------------------------------------------------------
# Reassign: corrected binding, append-only (N4)
# --------------------------------------------------------------------------------------


def test_reassign_files_a_corrected_candidate_and_supersedes_the_old(
    client: TestClient, app
) -> None:
    source_id = _seed(app)
    store = _store(app)
    row = _tag_candidate(client, source_id, "P-201")
    detail = client.get(
        f"/api/v2/documents/{source_id}/m6/candidates/{row['candidate_id']}"
    ).json()
    corrected_refs = [f"p2_{suffix}" for suffix in ("a", "b", "c", "d", "e")] + ["t1"]

    response = _decide(
        client,
        source_id,
        row["candidate_id"],
        {
            "action": "reassign",
            "reviewer_identity": REVIEWER,
            "reviewer_action": "P-201 实际标注的是第二台泵",
            "element_refs": corrected_refs,
            "note": "位号绑错簇",
        },
    )
    assert response.status_code == 200, response.text
    outcome = response.json()
    assert outcome["status"] == "superseded"
    successor_id = outcome["successor_candidate_id"]
    assert successor_id != row["candidate_id"]

    service_status_old = detail_after(client, source_id, row["candidate_id"])
    assert service_status_old == "superseded"
    successor = store.get_semantic_candidate(successor_id)
    assert successor is not None
    assert successor.derived_from == [row["candidate_id"]]
    assert successor.proposed_semantics.equipment_tag == "P-201"
    assert successor.region.element_refs == sorted(corrected_refs)
    # The corrected region has its own derived identity.
    assert successor.region.region_id != detail["candidate"]["region"]["region_id"]
    # One audit fact records the reassignment with the declared identity.
    audits = _audit_records(client)
    assert len(audits) == 1
    assert audits[0]["evidence"]["action"] == "reassign"
    assert audits[0]["evidence"]["successor_candidate_id"] == successor_id
    # A retry is refused: the old candidate has no supersede edge left.
    retry = _decide(
        client,
        source_id,
        row["candidate_id"],
        {
            "action": "reassign",
            "reviewer_identity": REVIEWER,
            "reviewer_action": "再次改派",
            "element_refs": corrected_refs,
        },
    )
    assert retry.status_code == 422
    assert retry.json()["detail"]["code"] == "supersede_not_applicable"


def detail_after(client: TestClient, source_id: str, candidate_id: str) -> str:
    return client.get(
        f"/api/v2/documents/{source_id}/m6/candidates/{candidate_id}"
    ).json()["review_status"]


def test_reassign_validates_its_inputs(client: TestClient, app) -> None:
    source_id = _seed(app)
    row = _tag_candidate(client, source_id, "P-201")
    empty = _decide(
        client,
        source_id,
        row["candidate_id"],
        {
            "action": "reassign",
            "reviewer_identity": REVIEWER,
            "reviewer_action": "改派",
            "element_refs": [],
        },
    )
    assert empty.status_code == 422
    assert empty.json()["detail"]["code"] == "reassign_requires_element_refs"

    unknown = _decide(
        client,
        source_id,
        row["candidate_id"],
        {
            "action": "reassign",
            "reviewer_identity": REVIEWER,
            "reviewer_action": "改派",
            "element_refs": ["el_nope"],
        },
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["code"] == "reassign_unknown_element_refs"


def test_unknown_candidate_and_wrong_document_are_404(client: TestClient, app) -> None:
    source_id = _seed(app)
    missing = _decide(
        client,
        source_id,
        "cand_m6_nope",
        {"action": "reject", "reviewer_identity": REVIEWER, "reviewer_action": "拒绝"},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "candidate_not_found"

    row = _tag_candidate(client, source_id, "P-201")
    wrong_doc = _decide(
        client,
        TARGET_ID,
        row["candidate_id"],
        {"action": "reject", "reviewer_identity": REVIEWER, "reviewer_action": "拒绝"},
    )
    assert wrong_doc.status_code == 404


# --------------------------------------------------------------------------------------
# Shared deployment: the token authenticates the request, never the person (N11 / D87-2)
# --------------------------------------------------------------------------------------


def test_shared_mode_requires_a_token_and_records_service_token_evidence(
    shared_app, tmp_path
) -> None:
    with TestClient(shared_app) as client:
        source_id = _seed(shared_app)
        store = _store(shared_app)
        row = next(
            candidate
            for candidate in store.list_semantic_candidates(source_document_id=source_id)
            if candidate.proposed_semantics.equipment_tag == "P-201"
        )
        url = f"/api/v2/documents/{source_id}/m6/candidates"

        assert client.get(url).status_code == 401
        assert client.get(url).json()["detail"]["error"] == "authentication_required"
        assert (
            client.post(
                f"{url}/{row.candidate_id}/decisions",
                json={
                    "action": "reject",
                    "reviewer_identity": REVIEWER,
                    "reviewer_action": "拒绝",
                },
            ).status_code
            == 401
        )
        wrong = client.get(url, headers={"Authorization": "Bearer wrong"})
        assert wrong.status_code == 403

        headers = {"Authorization": "Bearer m6-shared-test-token"}
        assert client.get(url, headers=headers).status_code == 200
        decided = client.post(
            f"{url}/{row.candidate_id}/decisions",
            headers=headers,
            json={
                "action": "reject",
                "reviewer_identity": REVIEWER,
                "reviewer_action": "共享部署下拒绝",
            },
        )
        assert decided.status_code == 200, decided.text
        audits = _audit_records(client, headers=headers)
        assert len(audits) == 1
        evidence = audits[0]["evidence"]
        assert evidence["authentication_evidence"] == "service-token"
        assert evidence["reviewer_attribution"] == REVIEWER
        assert evidence["identity_assurance"] == "declared"
