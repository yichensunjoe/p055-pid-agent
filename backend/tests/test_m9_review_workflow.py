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


# ---------------------------------------------------------------------------
# Round-2 Gate freeze: the unified approval reconcile, loopback token boundary,
# resolve state machine, and atomic invalidation audit. Each test here maps to
# one item of the PR #67 CHANGES REQUIRED ruling.
# ---------------------------------------------------------------------------


def _approved_state(reviews: ReviewService, document_id: str) -> ReviewState:
    """Drive one thread to closure and an approval to 'approved'."""

    state = reviews.create_thread(
        document_id=document_id, actor=OPERATOR, body="blocker", expected_governance_seq=0
    )
    thread_id = state.threads[0].thread_id
    state = reviews.resolve_thread(
        document_id=document_id,
        thread_id=thread_id,
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
    assert state.approvals[-1].status == "approved"
    return state


def _invalidation_audits(service: DocumentService) -> list:
    return [
        record
        for record in service.store.all_audit_records()
        if record.event_type == "approval.invalidated"
    ]


def test_new_open_thread_invalidates_approved_with_atomic_audit(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(tmp_path, service)
    state = _approved_state(reviews, document_id)

    state = reviews.create_thread(
        document_id=document_id,
        actor=AGENT,
        body="复核发现新问题",
        expected_governance_seq=state.governance_seq,
    )
    approval = state.approvals[-1]
    assert approval.status == "stale"
    assert approval.invalidation_reason == "review_digest_changed"

    audits = _invalidation_audits(service)
    assert len(audits) == 1
    assert audits[0].evidence["approval_id"] == approval.approval_id
    assert audits[0].evidence["reason"] == "review_digest_changed"
    assert audits[0].evidence["trigger_event"] == "review.thread.created"
    # Atomic with the triggering mutation: same governance_seq, committed together.
    created = [
        record
        for record in service.store.all_audit_records()
        if record.event_type == "review.thread.created"
    ]
    assert created[-1].evidence["governance_seq"] == audits[0].evidence["governance_seq"]


def test_comment_after_approval_invalidates_with_atomic_audit(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(tmp_path, service)
    state = _approved_state(reviews, document_id)
    thread_id = state.threads[0].thread_id
    approval_id = state.approvals[-1].approval_id

    state = reviews.add_comment(
        document_id=document_id,
        thread_id=thread_id,
        actor=OPERATOR,
        body="补充说明： Fit-101 已改 2 英寸",
        expected_governance_seq=state.governance_seq,
    )
    approval = next(a for a in state.approvals if a.approval_id == approval_id)
    assert approval.status == "stale"
    assert approval.invalidation_reason == "review_digest_changed"
    audits = _invalidation_audits(service)
    assert len(audits) == 1
    assert audits[0].evidence["approval_id"] == approval_id
    assert audits[0].evidence["trigger_event"] == "review.comment.posted"


def test_revision_drift_derives_stale_then_persists_on_next_mutation(
    tmp_path: Path,
) -> None:
    from agentcad.models import AddLayerOperation, Layer, TransactionRequest

    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(tmp_path, service)
    state = _approved_state(reviews, document_id)
    approval_id = state.approvals[-1].approval_id

    # External engineering edit moves the revision outside the governance plane.
    service.apply_transaction(
        document_id,
        TransactionRequest(
            expected_revision=0,
            label="drift the drawing",
            operations=[AddLayerOperation(layer=Layer(id="layer_x", name="X"))],
        ),
    )

    # The drifted approval reads as derived-stale at GET time even though no
    # governance mutation has persisted anything yet. The app must share the
    # service's database to see the same document.
    settings = Settings(
        database_path=tmp_path / "review.db",
        cors_origins=["http://localhost:5173"],
        frontend_dist=tmp_path / "dist",
        deployment_mode="local",  # type: ignore[arg-type]
        operator_identity="李工",
    )
    app = create_app(settings)
    client = TestClient(app)
    view = client.get(f"/api/v2/documents/{document_id}/review").json()
    approval_view = next(a for a in view["approvals"] if a["approval_id"] == approval_id)
    assert approval_view["derived_liveness"] == "stale"
    assert approval_view["status"] == "approved"  # not yet persisted

    # The next governance mutation persists the stale mark; revision drift wins
    # over the mutation's own digest reason.
    state = reviews.add_comment(
        document_id=document_id,
        thread_id=state.threads[0].thread_id,
        actor=OPERATOR,
        body="drift follow-up",
        expected_governance_seq=state.governance_seq,
    )
    approval = next(a for a in state.approvals if a.approval_id == approval_id)
    assert approval.status == "stale"
    assert approval.invalidation_reason == "revision_changed"
    audits = _invalidation_audits(service)
    assert len(audits) == 1
    assert audits[0].evidence["reason"] == "revision_changed"


def test_failed_mutation_leaves_no_invalidation_audit(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(tmp_path, service)
    state = _approved_state(reviews, document_id)

    # A CAS conflict aborts the whole transaction: no state change, no audit.
    with pytest.raises(ReviewStateConflict):
        reviews.create_thread(
            document_id=document_id,
            actor=AGENT,
            body="racing write",
            expected_governance_seq=state.governance_seq - 1,
        )
    assert _invalidation_audits(service) == []
    fresh = reviews.get_state(document_id)
    assert fresh.approvals[-1].status == "approved"
    assert fresh.governance_seq == state.governance_seq


def _local_app(tmp_path: Path, name: str):
    settings = Settings(
        database_path=tmp_path / name,
        cors_origins=["http://localhost:5173"],
        frontend_dist=tmp_path / "dist",
        deployment_mode="local",  # type: ignore[arg-type]
        operator_identity="李工",
    )
    return create_app(settings)


def test_valid_operator_token_from_non_loopback_peer_is_refused(tmp_path: Path) -> None:
    app = _local_app(tmp_path, "nl.db")
    loopback = TestClient(app)
    token = loopback.post("/api/v2/review/operator-session").json()["operator_token"]
    headers = {"X-Operator-Token": token}

    created = loopback.post(
        "/api/v2/documents",
        json={"name": "pilot drawing", "width": 1600, "height": 900},
    )
    document_id = created.json()["id"]
    view = loopback.post(
        f"/api/v2/documents/{document_id}/review/threads",
        json={"body": "泵出口缺止回阀", "expected_governance_seq": 0},
        headers=headers,
    ).json()
    thread_id = view["threads"][0]["thread_id"]

    remote = TestClient(app, client=("192.0.2.1", 44444))
    refused = remote.post(
        f"/api/v2/documents/{document_id}/review/threads/{thread_id}/resolve",
        json={"resolution_note": "远端冒充操作员", "expected_governance_seq": 1},
        headers=headers,
    )
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "actor_not_trusted"

    # The same call from the loopback peer with the same token succeeds — the
    # refusal is about the peer, not the token.
    allowed = loopback.post(
        f"/api/v2/documents/{document_id}/review/threads/{thread_id}/resolve",
        json={"resolution_note": "已加 CV-101", "expected_governance_seq": 1},
        headers=headers,
    )
    assert allowed.status_code == 200


def test_resolve_already_resolved_thread_fails_closed(tmp_path: Path) -> None:
    app = _local_app(tmp_path, "rs.db")
    client = TestClient(app)
    token = client.post("/api/v2/review/operator-session").json()["operator_token"]
    headers = {"X-Operator-Token": token}
    document_id = client.post(
        "/api/v2/documents",
        json={"name": "pilot drawing", "width": 1600, "height": 900},
    ).json()["id"]
    view = client.post(
        f"/api/v2/documents/{document_id}/review/threads",
        json={"body": "标注缺失", "expected_governance_seq": 0},
        headers=headers,
    ).json()
    thread_id = view["threads"][0]["thread_id"]

    first = client.post(
        f"/api/v2/documents/{document_id}/review/threads/{thread_id}/resolve",
        json={"resolution_note": "已补", "expected_governance_seq": 1},
        headers=headers,
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/v2/documents/{document_id}/review/threads/{thread_id}/resolve",
        json={"resolution_note": "重复闭环", "expected_governance_seq": 2},
        headers=headers,
    )
    assert second.status_code == 422
    assert second.json()["detail"]["code"] == "review_thread_not_open"
