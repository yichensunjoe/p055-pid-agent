"""M9-WS2: the release gate + evidence package contract tests.

Locks the Gate-frozen behaviour: guard order (actor → threads → approval live →
one-live-release-per-binding → fresh readiness → package build), the two-phase
discipline (Phase A in memory on one pinned snapshot, Phase B the single atomic
``commit_release``), the hash-formation order (R49-1, no self-reference), the
audit-only denial facts (F1/F2/F3 exactly one ``release.denied``, no governance
seq; F4/F6/F7 none), the double-binding supersede (revision OR review digest),
and the export-side integrity re-check.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agentcad.review_service as review_service_module
from agentcad.api_release import EvidenceBuildError
from agentcad.config import Settings
from agentcad.main import create_app
from agentcad.models import AddLayerOperation, Layer, TransactionRequest
from agentcad.review_models import review_snapshot_digest
from agentcad.review_service import (
    Actor,
    ReleaseAlreadyExistsError,
    ReleaseApprovalNotLiveError,
    ReleaseConflictError,
    ReleaseOpenThreadsError,
    ReleaseReadinessNotEligibleError,
    ReviewService,
    ReviewWorkflowError,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry


def make_service(tmp_path: Path) -> DocumentService:
    return DocumentService(SQLiteDocumentStore(tmp_path / "release.db"), SymbolRegistry())


def seed_document(service: DocumentService) -> str:
    from agentcad.models import CreateDocumentRequest

    document = service.create_document(
        CreateDocumentRequest(name="release-target", width=1600, height=900)
    )
    return document.id


def make_reviews(service: DocumentService) -> ReviewService:
    from agentcad.validation_profile import load_profile

    return ReviewService(service.store, service, readiness_profile=load_profile())


OPERATOR = Actor(identity="发布工程师", kind="operator")
AGENT = Actor(identity="agent", kind="agent")


def _approved(reviews: ReviewService, document_id: str):
    """Close one thread and drive an approval to 'approved'."""

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
    return state


def _release(reviews: ReviewService, document_id: str, state) -> ReviewService:
    return reviews.release_document(
        document_id=document_id,
        actor=OPERATOR,
        expected_governance_seq=state.governance_seq,
    )


def _denied_audits(service: DocumentService) -> list:
    return [
        record
        for record in service.store.all_audit_records()
        if record.event_type == "release.denied"
    ]


def _released_audits(service: DocumentService) -> list:
    return [
        record
        for record in service.store.all_audit_records()
        if record.event_type == "release.released"
    ]


def _superseded_audits(service: DocumentService) -> list:
    return [
        record
        for record in service.store.all_audit_records()
        if record.event_type == "release.superseded"
    ]


# ① F1: no / stale / superseded approval cannot release; exactly one denial fact.
def test_release_without_live_approval_refused_with_single_denial(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(service)

    with pytest.raises(ReleaseApprovalNotLiveError) as excinfo:
        reviews.release_document(
            document_id=document_id, actor=OPERATOR, expected_governance_seq=0
        )
    assert excinfo.value.code == "release_approval_not_live"
    denied = _denied_audits(service)
    assert len(denied) == 1
    assert denied[0].status == "rejected"
    assert denied[0].evidence["error_code"] == "release_approval_not_live"
    # Audit-only: no governance movement, no release record.
    assert reviews.get_state(document_id).governance_seq == 0
    assert reviews.get_state(document_id).releases == ()


# ② F2 via the Gate-mandated seam: readiness goes not_eligible while revision
# and review digest are unchanged — proving the release re-runs readiness
# instead of reusing the decision-time hash.
def test_release_fresh_readiness_gate_is_really_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(service)
    state = _approved(reviews, document_id)
    approval = state.approvals[-1]
    calls: list = []
    real = review_service_module.assess_release_readiness

    def seam(document, symbols, profile, **kwargs):
        readiness = real(document, symbols, profile, **kwargs)
        calls.append(readiness)
        return readiness.model_copy(
            update={"state": "not_eligible", "reasons": ["seam: fresh gate"]}
        )

    monkeypatch.setattr(review_service_module, "assess_release_readiness", seam)
    with pytest.raises(ReleaseReadinessNotEligibleError):
        _release(reviews, document_id, state)

    assert len(calls) == 1, "release must re-run readiness exactly once on the pinned snapshot"
    assert calls[0].readiness_hash != approval.decision_readiness_hash
    denied = _denied_audits(service)
    assert len(denied) == 1
    assert denied[0].evidence["error_code"] == "release_readiness_not_eligible"
    assert reviews.get_state(document_id).releases == ()


# ③ F3 reachable and ordered before the approval check (frozen guard order).
def test_open_threads_block_release_before_approval_check(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(service)
    state = reviews.create_thread(
        document_id=document_id, actor=OPERATOR, body="open finding", expected_governance_seq=0
    )
    with pytest.raises(ReleaseOpenThreadsError) as excinfo:
        reviews.release_document(
            document_id=document_id, actor=OPERATOR, expected_governance_seq=state.governance_seq
        )
    assert excinfo.value.code == "release_open_threads"
    denied = _denied_audits(service)
    assert len(denied) == 1
    assert denied[0].evidence["error_code"] == "release_open_threads"


# ④ F4: an untrusted actor is refused before any audit can be injected.
def test_agent_release_attempt_gets_403_and_no_audit(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "f4.db",
        cors_origins=["http://localhost:5173"],
        frontend_dist=tmp_path / "dist",
        deployment_mode="local",  # type: ignore[arg-type]
        operator_identity="李工",
    )
    app = create_app(settings)
    client = TestClient(app)
    document_id = client.post(
        "/api/v2/documents",
        json={"name": "pilot", "width": 1600, "height": 900},
    ).json()["id"]
    before_records = SQLiteDocumentStore(settings.database_path).all_audit_records()
    before_tip = before_records[-1].record_hash if before_records else ""

    response = client.post(
        f"/api/v2/documents/{document_id}/releases",
        json={"expected_governance_seq": 0},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "actor_not_trusted"
    # R68-4, machine-locked: the attempt must not grow the global audit chain
    # at all — not just "no release.denied", zero new records and same tip.
    records_after = SQLiteDocumentStore(settings.database_path).all_audit_records()
    assert [r for r in records_after if r.event_type == "release.denied"] == []
    assert len(records_after) == len(before_records)
    assert (records_after[-1].record_hash if records_after else "") == before_tip


# ⑤ F5: a package-build failure never reaches Phase B — zero everything.
def test_package_build_failure_leaves_no_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(service)
    state = _approved(reviews, document_id)

    def boom(**kwargs):
        raise EvidenceBuildError("injected export failure")

    monkeypatch.setattr(review_service_module, "build_release_evidence_package", boom)
    with pytest.raises(ReviewWorkflowError) as excinfo:
        _release(reviews, document_id, state)
    assert excinfo.value.details["code"] == "release_evidence_build_failed"
    fresh = reviews.get_state(document_id)
    assert fresh.governance_seq == state.governance_seq
    assert fresh.releases == ()
    assert _released_audits(service) == []
    assert _denied_audits(service) == []


# ⑥ one live release per binding: a repeat release is 409 release_already_exists
# with zero new anything; a CAS race is 409 release_conflict — neither records
# a denial.
def test_repeat_release_conflict_and_already_exists(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(service)
    state = _approved(reviews, document_id)

    released = _release(reviews, document_id, state)
    release_id = released.releases[-1].release_id
    assert released.releases[-1].seq == released.governance_seq

    with pytest.raises(ReleaseAlreadyExistsError) as excinfo:
        _release(reviews, document_id, released)
    assert excinfo.value.code == "release_already_exists"

    with pytest.raises(ReleaseConflictError) as excinfo:
        reviews.release_document(
            document_id=document_id, actor=OPERATOR, expected_governance_seq=state.governance_seq
        )
    assert excinfo.value.code == "release_conflict"

    assert _denied_audits(service) == []
    assert len(_released_audits(service)) == 1
    assert reviews.get_state(document_id).governance_seq == released.governance_seq
    package = service.store.get_release_package(release_id)
    assert package is not None


# ⑦ F7: an engineering edit racing Phase A fails the Phase B revision recheck.
def test_phase_b_revision_recheck_fails_racing_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(service)
    state = _approved(reviews, document_id)

    real_build = review_service_module.build_release_evidence_package

    def racing_build(**kwargs):
        # The drawing moves while the evidence package is being built.
        service.apply_transaction(
            document_id,
            TransactionRequest(
                expected_revision=0,
                label="racing engineering edit",
                operations=[AddLayerOperation(layer=Layer(id="layer_race", name="RACE"))],
            ),
        )
        return real_build(**kwargs)

    monkeypatch.setattr(review_service_module, "build_release_evidence_package", racing_build)
    with pytest.raises(ReleaseConflictError) as excinfo:
        _release(reviews, document_id, state)
    assert excinfo.value.code == "release_conflict"
    fresh = reviews.get_state(document_id)
    assert fresh.releases == ()
    assert _released_audits(service) == []
    assert _denied_audits(service) == []


# ⑧ the release audit carries the frozen evidence five-tuple, and publishing
# never moves the review digest (releases stay outside the projection).
def test_release_audit_five_tuple_and_digest_unchanged(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(service)
    state = _approved(reviews, document_id)
    digest_before = review_snapshot_digest(state)

    released = _release(reviews, document_id, state)
    release = released.releases[-1]
    assert review_snapshot_digest(released) == digest_before

    audits = _released_audits(service)
    assert len(audits) == 1
    evidence = audits[0].evidence
    for key in (
        "release_id",
        "evidence_manifest_hash",
        "package_sha256",
        "verified_through_ordinal",
        "verified_global_tip_hash",
    ):
        assert key in evidence, f"release audit missing {key}"
    assert evidence["release_id"] == release.release_id
    assert evidence["evidence_manifest_hash"] == release.evidence_manifest_hash
    assert evidence["package_sha256"] == release.package_sha256


# ⑨ double-binding supersede: an engineering edit supersedes by revision; a
# review-surface move supersedes by digest — each exactly once, and a re-release
# requires a fresh approval on the new revision.
def test_supersede_double_binding_and_rerelease_needs_new_approval(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(service)
    state = _approved(reviews, document_id)
    released = _release(reviews, document_id, state)
    release_id = released.releases[-1].release_id

    # Engineering edit: revision binding breaks. The next governance mutation
    # persists the supersede with exactly one audit.
    service.apply_transaction(
        document_id,
        TransactionRequest(
            expected_revision=0,
            label="edit after release",
            operations=[AddLayerOperation(layer=Layer(id="layer_2", name="L2"))],
        ),
    )
    follow_up = reviews.create_thread(
        document_id=document_id,
        actor=OPERATOR,
        body="post-release finding",
        expected_governance_seq=released.governance_seq,
    )
    persisted = next(r for r in follow_up.releases if r.release_id == release_id)
    assert persisted.state == "superseded"
    assert persisted.superseded_reason == "revision_changed"
    superseded = _superseded_audits(service)
    assert len(superseded) == 1
    assert superseded[0].evidence["release_id"] == release_id
    assert superseded[0].evidence["reason"] == "revision_changed"

    # The old approval is revision-stale now; with the thread closed, releasing
    # fails F1 (approval liveness), not F3 — the frozen guard order is visible.
    thread_id = follow_up.threads[-1].thread_id
    resolved = reviews.resolve_thread(
        document_id=document_id,
        thread_id=thread_id,
        actor=OPERATOR,
        resolution_note="已处理",
        expected_governance_seq=follow_up.governance_seq,
    )
    with pytest.raises(ReleaseApprovalNotLiveError):
        reviews.release_document(
            document_id=document_id,
            actor=OPERATOR,
            expected_governance_seq=resolved.governance_seq,
        )

    # Approve anew on the new revision and release again.
    state = reviews.request_approval(
        document_id=document_id, actor=AGENT, expected_governance_seq=resolved.governance_seq
    )
    approval = state.approvals[-1]
    state = reviews.decide_approval(
        document_id=document_id,
        approval_id=approval.approval_id,
        actor=OPERATOR,
        decision="approved",
        expected_governance_seq=state.governance_seq,
    )
    second = _release(reviews, document_id, state)
    assert second.releases[-1].release_id != release_id
    assert len(_released_audits(service)) == 2

    # A review-surface move (new comment) breaks the digest binding: the next
    # governance mutation supersedes the second release by review_snapshot_changed.
    commented = reviews.add_comment(
        document_id=document_id,
        thread_id=thread_id,
        actor=OPERATOR,
        body="发布后补充说明",
        expected_governance_seq=second.governance_seq,
    )
    second_persisted = next(r for r in commented.releases if r.release_id == second.releases[-1].release_id)
    assert second_persisted.state == "superseded"
    assert second_persisted.superseded_reason == "review_snapshot_changed"
    reasons = [a.evidence["reason"] for a in _superseded_audits(service)]
    assert reasons == ["revision_changed", "review_snapshot_changed"]


# ⑩ v12 -> v13 additive migration: pre-WS2 databases open cleanly with zero
# evidence packages and stay readable.
def test_v12_database_migrates_additively_to_v13(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(service)
    state = _approved(reviews, document_id)
    _release(reviews, document_id, state)
    release_id = reviews.get_state(document_id).releases[-1].release_id

    database = tmp_path / "release.db"
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE release_evidence_packages")
    connection.execute("PRAGMA user_version=12")
    connection.commit()
    connection.close()

    reopened_store = SQLiteDocumentStore(database)
    rows = reopened_store._connect().execute(  # noqa: SLF001 - migration assertion
        "SELECT COUNT(*) AS n FROM release_evidence_packages"
    ).fetchone()
    assert rows["n"] == 0, "migrated v12 database must start with zero evidence packages"
    state_after = reopened_store.get_review_state(document_id)
    assert state_after is not None
    assert [release.release_id for release in state_after.releases] == [release_id]
    connection = reopened_store._connect()  # noqa: SLF001
    from agentcad.database_recovery import CURRENT_SCHEMA_VERSION

    version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == CURRENT_SCHEMA_VERSION


# ⑪ export integrity: repeated GETs are byte-identical; a tampered blob is
# refused with release_evidence_corrupt.
def test_evidence_export_byte_stable_and_corruption_refused(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "exp.db",
        cors_origins=["http://localhost:5173"],
        frontend_dist=tmp_path / "dist",
        deployment_mode="local",  # type: ignore[arg-type]
        operator_identity="李工",
    )
    app = create_app(settings)
    client = TestClient(app)
    token = client.post("/api/v2/review/operator-session").json()["operator_token"]
    headers = {"X-Operator-Token": token}
    document_id = client.post(
        "/api/v2/documents",
        json={"name": "pilot", "width": 1600, "height": 900},
    ).json()["id"]

    # Approve through the API surface.
    view = client.post(
        f"/api/v2/documents/{document_id}/review/threads",
        json={"body": "标注缺失", "expected_governance_seq": 0},
        headers=headers,
    ).json()
    thread_id = view["threads"][0]["thread_id"]
    seq = view["governance_seq"]
    view = client.post(
        f"/api/v2/documents/{document_id}/review/threads/{thread_id}/resolve",
        json={"resolution_note": "已补", "expected_governance_seq": seq},
        headers=headers,
    ).json()
    seq = view["governance_seq"]
    view = client.post(
        f"/api/v2/documents/{document_id}/approval/request",
        json={"expected_governance_seq": seq},
    ).json()
    seq = view["governance_seq"]
    approval_id = view["approvals"][-1]["approval_id"]
    view = client.post(
        f"/api/v2/documents/{document_id}/approval/{approval_id}/decide",
        json={"decision": "approved", "expected_governance_seq": seq},
        headers=headers,
    ).json()
    seq = view["governance_seq"]
    assert view["releases"] == []

    view = client.post(
        f"/api/v2/documents/{document_id}/releases",
        json={"expected_governance_seq": seq},
        headers=headers,
    ).json()
    release = view["releases"][-1]
    release_id = release["release_id"]
    assert release["derived_superseded"] is False
    assert release["evidence_manifest_hash"]
    assert release["package_sha256"]

    url = f"/api/v2/documents/{document_id}/releases/{release_id}/evidence.zip"
    first = client.get(url)
    second = client.get(url)
    assert first.status_code == 200
    assert first.content == second.content
    assert hashlib.sha256(first.content).hexdigest() == release["package_sha256"]

    # Tamper with the stored blob: the export re-check must refuse to serve it.
    connection = sqlite3.connect(settings.database_path)
    connection.execute(
        "UPDATE release_evidence_packages SET package_blob = ? WHERE release_id = ?",
        (sqlite3.Binary(b"not a zip"), release_id),
    )
    connection.commit()
    connection.close()
    refused = client.get(url)
    assert refused.status_code == 500
    assert refused.json()["detail"]["code"] == "release_evidence_corrupt"


# ⑫ frozen hash formation: release.json inside the package carries no hash
# fields; the MANIFEST binds every member; the record write-back matches.
def test_package_hash_formation_order_and_member_contract(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(service)
    state = _approved(reviews, document_id)
    released = _release(reviews, document_id, state)
    release = released.releases[-1]

    package = service.store.get_release_package(release.release_id)
    assert package is not None
    assert package.manifest_sha256 == release.evidence_manifest_hash
    assert package.package_sha256 == release.package_sha256
    assert hashlib.sha256(package.package_blob).hexdigest() == release.package_sha256

    with zipfile.ZipFile(BytesIO(package.package_blob)) as archive:
        names = sorted(archive.namelist())
        assert names == sorted(
            [
                "MANIFEST.sha256",
                "approval.json",
                "audit-chain.json",
                "drawing.dxf",
                "drawing.pdf",
                "release-readiness.json",
                "release.json",
                "validation-report.json",
            ]
        )
        manifest = archive.read("MANIFEST.sha256").decode("utf-8")
        entries: dict[str, str] = {}
        for line in manifest.splitlines():
            digest, _, name = line.partition("  ")
            entries[name] = digest
        for name, digest in entries.items():
            assert digest == hashlib.sha256(archive.read(name)).hexdigest()
        assert hashlib.sha256(manifest.encode("utf-8")).hexdigest() == release.evidence_manifest_hash

        release_member = json.loads(archive.read("release.json").decode("utf-8"))
        for forbidden in ("evidence_manifest_hash", "package_sha256", "package_blob"):
            assert forbidden not in release_member
        assert release_member["release_id"] == release.release_id
        assert release_member["seq"] == release.seq

        audit_member = json.loads(archive.read("audit-chain.json").decode("utf-8"))
        assert audit_member["scope"] == "document-subset"
        assert audit_member["global_chain_verification"] == "ok"
        assert audit_member["database_instance_id"]
        assert audit_member["verified_through_ordinal"] >= 1

        readiness_member = json.loads(archive.read("release-readiness.json").decode("utf-8"))
        validation_member = json.loads(archive.read("validation-report.json").decode("utf-8"))
        assert readiness_member["validation_hash"] == validation_member["result_hash"]


# R68-5, machine-locked: two releases racing from the SAME initial governance
# seq cross Phase A together (barrier seam) — exactly one lands.
def test_concurrent_double_release_one_wins_one_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(service)
    state = _approved(reviews, document_id)

    barrier = threading.Barrier(2)
    real_build = review_service_module.build_release_evidence_package

    def synchronised_build(**kwargs):
        barrier.wait(timeout=15)
        return real_build(**kwargs)

    monkeypatch.setattr(review_service_module, "build_release_evidence_package", synchronised_build)

    results: list = []
    errors: list = []

    def attempt():
        try:
            results.append(
                reviews.release_document(
                    document_id=document_id,
                    actor=OPERATOR,
                    expected_governance_seq=state.governance_seq,
                )
            )
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 1, "exactly one concurrent release may land"
    assert len(errors) == 1
    assert isinstance(errors[0], ReleaseConflictError)
    assert errors[0].code == "release_conflict"

    final = reviews.get_state(document_id)
    assert len(final.releases) == 1
    assert len(_released_audits(service)) == 1
    assert _denied_audits(service) == []
    package = service.store.get_release_package(final.releases[0].release_id)
    assert package is not None


# R68-2, machine-locked: a BLOB row made internally self-consistent with
# ANOTHER release's package still fails against this release's record.
def test_cross_plane_binding_rejects_foreign_but_valid_package(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "xplane.db",
        cors_origins=["http://localhost:5173"],
        frontend_dist=tmp_path / "dist",
        deployment_mode="local",  # type: ignore[arg-type]
        operator_identity="李工",
    )
    app = create_app(settings)
    client = TestClient(app)
    token = client.post("/api/v2/review/operator-session").json()["operator_token"]
    headers = {"X-Operator-Token": token}

    release_ids: list[str] = []
    for name in ("doc-a", "doc-b"):
        created = client.post(
            "/api/v2/documents", json={"name": name, "width": 1600, "height": 900}
        ).json()
        document_id = created["id"]
        view = client.post(
            f"/api/v2/documents/{document_id}/review/threads",
            json={"body": "复核", "expected_governance_seq": 0},
            headers=headers,
        ).json()
        thread_id = view["threads"][0]["thread_id"]
        seq = view["governance_seq"]
        view = client.post(
            f"/api/v2/documents/{document_id}/review/threads/{thread_id}/resolve",
            json={"resolution_note": "已补", "expected_governance_seq": seq},
            headers=headers,
        ).json()
        seq = view["governance_seq"]
        view = client.post(
            f"/api/v2/documents/{document_id}/approval/request",
            json={"expected_governance_seq": seq},
        ).json()
        seq = view["governance_seq"]
        approval_id = view["approvals"][-1]["approval_id"]
        view = client.post(
            f"/api/v2/documents/{document_id}/approval/{approval_id}/decide",
            json={"decision": "approved", "expected_governance_seq": seq},
            headers=headers,
        ).json()
        seq = view["governance_seq"]
        view = client.post(
            f"/api/v2/documents/{document_id}/releases",
            json={"expected_governance_seq": seq},
            headers=headers,
        ).json()
        release_ids.append(view["releases"][-1]["release_id"])

    store = SQLiteDocumentStore(settings.database_path)
    row_a = store.get_release_package(release_ids[0])
    row_b = store.get_release_package(release_ids[1])
    assert row_a is not None and row_b is not None
    # Rewrite A's row with B's fully self-consistent package bytes + hashes.
    connection = sqlite3.connect(settings.database_path)
    connection.execute(
        "UPDATE release_evidence_packages SET package_blob = ?, manifest_sha256 = ?, "
        "package_sha256 = ? WHERE release_id = ?",
        (sqlite3.Binary(row_b.package_blob), row_b.manifest_sha256, row_b.package_sha256, row_a.release_id),
    )
    connection.commit()
    connection.close()

    # A's document id comes straight from the (tampered) row.
    connection = sqlite3.connect(settings.database_path)
    row = connection.execute(
        "SELECT document_id FROM release_evidence_packages WHERE release_id = ?",
        (release_ids[0],),
    ).fetchone()
    connection.close()
    document_id_a = row[0]
    response = client.get(
        f"/api/v2/documents/{document_id_a}/releases/{release_ids[0]}/evidence.zip"
    )
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "release_evidence_corrupt"


# R68-3, unit-locked: every structural/encoding/JSON failure maps to
# release_evidence_corrupt — never an unhandled exception.
def test_verify_maps_encoding_and_json_failures_to_corrupt(tmp_path: Path) -> None:
    import zipfile as zf

    from agentcad.api_release import (
        ReleaseEvidenceCorruptError,
        verify_evidence_package,
    )
    from agentcad.store import ReleaseEvidencePackage

    service = make_service(tmp_path)
    document_id = seed_document(service)
    reviews = make_reviews(service)
    state = _approved(reviews, document_id)
    released = _release(reviews, document_id, state)
    release = released.releases[-1]
    row = service.store.get_release_package(release.release_id)
    assert row is not None

    def tampered(replace: dict[str, bytes]) -> ReleaseEvidencePackage:
        with zf.ZipFile(BytesIO(row.package_blob)) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members.update(replace)
        buffer = BytesIO()
        with zf.ZipFile(buffer, "w", zf.ZIP_DEFLATED) as archive:
            for name in sorted(members):
                archive.writestr(name, members[name])
        blob = buffer.getvalue()
        manifest = members["MANIFEST.sha256"]
        return ReleaseEvidencePackage(
            release_id=row.release_id,
            document_id=row.document_id,
            manifest_sha256=hashlib.sha256(manifest).hexdigest(),
            package_sha256=hashlib.sha256(blob).hexdigest(),
            package_blob=blob,
            created_at=row.created_at,
        )

    def expect_corrupt(package: ReleaseEvidencePackage, label: str) -> None:
        with pytest.raises(ReleaseEvidenceCorruptError) as excinfo:
            verify_evidence_package(
                package,
                expected_manifest_sha256=package.manifest_sha256,
                expected_package_sha256=package.package_sha256,
            )
        assert excinfo.value.code == "release_evidence_corrupt", label

    expect_corrupt(tampered({"MANIFEST.sha256": b"\xff\xff not utf-8"}), "manifest encoding")
    expect_corrupt(tampered({"release.json": b"{not json"}), "release.json json")
    expect_corrupt(tampered({"MANIFEST.sha256": b"short\n"}), "manifest line format")

    # Final order lock (Gate R68-3): the same seven valid lines in a WRONG order,
    # with the tampered manifest/package hashes passed as the expected values, must
    # still be refused — the rejection comes from the canonical order, not the hash.
    with zf.ZipFile(BytesIO(row.package_blob)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    reversed_lines = list(reversed(members["MANIFEST.sha256"].decode("utf-8").splitlines()))
    members["MANIFEST.sha256"] = ("\n".join(reversed_lines) + "\n").encode("utf-8")
    buffer = BytesIO()
    with zf.ZipFile(buffer, "w", zf.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            archive.writestr(name, members[name])
    reordered_blob = buffer.getvalue()
    reordered_manifest = members["MANIFEST.sha256"]
    reordered = ReleaseEvidencePackage(
        release_id=row.release_id,
        document_id=row.document_id,
        manifest_sha256=hashlib.sha256(reordered_manifest).hexdigest(),
        package_sha256=hashlib.sha256(reordered_blob).hexdigest(),
        package_blob=reordered_blob,
        created_at=row.created_at,
    )
    expect_corrupt(reordered, "manifest line order")
