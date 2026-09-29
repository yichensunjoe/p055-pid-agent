"""M9-WS1: the review workflow contract tests.

Locks the Gate-frozen behaviour: governance never moves the engineering revision,
CAS rejects racing mutations, the review digest covers review facts (with actor
identity) but never approvals, agents can never decide, approvals bind decision-time
fresh readiness, and staleness/reopen invalidation is machine-checked.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentcad.config import Settings
from agentcad.main import create_app
from agentcad.review_models import ReviewState, review_snapshot_digest
from agentcad.review_service import (
    Actor,
    ActorNotTrustedError,
    ApprovalBlockedOpenThreadsError,
    ReviewService,
    ReviewStateConflict,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry


def make_service(tmp_path: Path) -> DocumentService:
    return DocumentService(SQLiteDocumentStore(tmp_path / "review.db"), SymbolRegistry())


def seed_document(service: DocumentService) -> str:
    from agentcad.models import CreateDocumentRequest

    document = service.create_document(
        CreateDocumentRequest(name="review-target", width=1600, height=900)
    )
    return document.id


def make_reviews(tmp_path: Path, service: DocumentService) -> ReviewService:
    from agentcad.validation_profile import load_profile

    return ReviewService(
        service.store, service, readiness_profile=load_profile()
    )


OPERATOR = Actor(identity="测试工程师", kind="operator")
AGENT = Actor(identity="agent", kind="agent")


def test_governance_never_moves_engineering_revision(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(tmp_path, service)
    revision_before = service.get_document(document_id).revision

    state = reviews.create_thread(
        document_id=document_id,
        actor=OPERATOR,
        body="入口管径标注缺失",
        expected_governance_seq=0,
    )
    reviews.add_comment(
        document_id=document_id,
        thread_id=state.threads[0].thread_id,
        actor=AGENT,
        body="收到，补 FIT 标注",
        expected_governance_seq=state.governance_seq,
    )
    assert service.get_document(document_id).revision == revision_before


def test_cas_rejects_racing_mutations(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(tmp_path, service)
    state = reviews.create_thread(
        document_id=document_id,
        actor=OPERATOR,
        body="first",
        expected_governance_seq=0,
    )
    with pytest.raises(ReviewStateConflict):
        reviews.add_comment(
            document_id=document_id,
            thread_id=state.threads[0].thread_id,
            actor=OPERATOR,
            body="stale seq write",
            expected_governance_seq=0,  # moved to 1 already
        )


def test_digest_covers_review_facts_and_actors_not_approvals() -> None:
    base = ReviewState(document_id="doc_x")
    assert review_snapshot_digest(base) == review_snapshot_digest(
        base.model_copy(update={"governance_seq": 99})
    )
    # Same actions by different operators -> different digest (actor is a fact).
    import tempfile

    dir_a = Path(tempfile.mkdtemp())
    service = make_service(dir_a)
    document_id = seed_document(service)
    reviews = make_reviews(dir_a, service)
    s1 = reviews.create_thread(
        document_id=document_id, actor=OPERATOR, body="x", expected_governance_seq=0
    )
    dir_b = Path(tempfile.mkdtemp())
    service_b = make_service(dir_b)
    document_b = seed_document(service_b)
    reviews_b = make_reviews(dir_b, service_b)
    s2 = reviews_b.create_thread(
        document_id=document_b, actor=AGENT, body="x", expected_governance_seq=0
    )
    d_operator = review_snapshot_digest(s1)
    d_agent = review_snapshot_digest(s2)
    assert d_operator != d_agent

    # Approvals never enter the digest: requesting one leaves the digest unchanged.
    before = review_snapshot_digest(s1)
    reviews.request_approval(
        document_id=document_id, actor=AGENT, expected_governance_seq=s1.governance_seq
    )
    after = reviews.snapshot_digest(document_id)
    assert before == after


def test_agent_cannot_resolve_or_approve(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(tmp_path, service)
    state = reviews.create_thread(
        document_id=document_id, actor=AGENT, body="agent finding", expected_governance_seq=0
    )
    with pytest.raises(ActorNotTrustedError):
        reviews.resolve_thread(
            document_id=document_id,
            thread_id=state.threads[0].thread_id,
            actor=AGENT,
            resolution_note="agent self-approval",
            expected_governance_seq=state.governance_seq,
        )


def test_open_threads_block_approval_and_reopen_invalidates(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(tmp_path, service)
    state = reviews.create_thread(
        document_id=document_id, actor=OPERATOR, body="blocker", expected_governance_seq=0
    )
    # Requesting is allowed while threads are open; only the decision is blocked.
    state = reviews.request_approval(
        document_id=document_id, actor=AGENT, expected_governance_seq=state.governance_seq
    )
    approval = state.approvals[0]
    with pytest.raises(ApprovalBlockedOpenThreadsError):
        reviews.decide_approval(
            document_id=document_id,
            approval_id=approval.approval_id,
            actor=OPERATOR,
            decision="approved",
            expected_governance_seq=state.governance_seq,
        )
    # Close the loop (the review surface moved, so the stale binding is
    # re-requested), then approve, then reopen -> approval goes stale.
    state = reviews.resolve_thread(
        document_id=document_id,
        thread_id=state.threads[0].thread_id,
        actor=OPERATOR,
        resolution_note="已补标注",
        expected_governance_seq=state.governance_seq,
    )
    state = reviews.request_approval(
        document_id=document_id, actor=AGENT, expected_governance_seq=state.governance_seq
    )
    approval = state.approvals[-1]
    state = reviews.decide_approval(
        document_id=document_id,
        approval_id=approval.approval_id,
        actor=OPERATOR,
        decision="approved",
        expected_governance_seq=state.governance_seq,
    )
    decided = next(a for a in state.approvals if a.approval_id == approval.approval_id)
    assert decided.status == "approved"
    assert decided.decision_readiness_hash
    state = reviews.reopen_thread(
        document_id=document_id,
        thread_id=state.threads[0].thread_id,
        actor=OPERATOR,
        expected_governance_seq=state.governance_seq,
    )
    reopened = next(a for a in state.approvals if a.approval_id == approval.approval_id)
    assert reopened.status == "stale"
    assert reopened.invalidation_reason == "review_reopened"


def test_shared_deployment_has_no_operator(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "s.db",
        cors_origins=["http://localhost:5173"],
        frontend_dist=tmp_path / "dist",
        deployment_mode="shared",  # type: ignore[arg-type]
        api_token="deployment-token",
        operator_identity="张三",
    )
    app = create_app(settings)
    client = TestClient(app)
    authenticated = client.post(
        "/api/v2/review/operator-session",
        headers={"Authorization": "Bearer deployment-token"},
    )
    assert authenticated.status_code == 403


def test_operator_session_bootstrap_and_review_roundtrip(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "o.db",
        cors_origins=["http://localhost:5173"],
        frontend_dist=tmp_path / "dist",
        deployment_mode="local",  # type: ignore[arg-type]
        operator_identity="李工",
    )
    app = create_app(settings)
    client = TestClient(app)
    bootstrap = client.post("/api/v2/review/operator-session")
    assert bootstrap.status_code == 200
    token = bootstrap.json()["operator_token"]
    headers = {"X-Operator-Token": token}

    created = client.post(
        "/api/v2/documents",
        json={"name": "pilot drawing", "width": 1600, "height": 900},
    )
    document_id = created.json()["id"]

    view = client.get(f"/api/v2/documents/{document_id}/review").json()
    assert view["governance_seq"] == 0
    view = client.post(
        f"/api/v2/documents/{document_id}/review/threads",
        json={"body": "泵出口缺止回阀", "expected_governance_seq": 0},
        headers=headers,
    ).json()
    thread_id = view["threads"][0]["thread_id"]
    view = client.post(
        f"/api/v2/documents/{document_id}/review/threads/{thread_id}/resolve",
        json={"resolution_note": "已加 CV-101", "expected_governance_seq": 1},
        headers=headers,
    ).json()
    assert view["threads"][0]["status"] == "resolved"
    assert view["threads"][0]["addressed_at_revision"] == 0

    # Engineering revision never moved.
    document = client.get(f"/api/v2/documents/{document_id}").json()
    assert document["revision"] == 0
