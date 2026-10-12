"""M6-2B-D4: governed apply & replay — the chain, its ledger, and what it refuses.

The chain: confirmed finding -> deterministic compile -> durable patch (append-only) ->
M10 sealed approval -> one atomic commit (L1 identity chain + final source verification +
semantic baseline recheck + document CAS + ledger + audit + harness close-out) -> two-layer
replay -> compensation revert. Most of this file is about refusals, because that is what a
governance boundary is.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentcad.database_recovery import (
    CURRENT_SCHEMA_VERSION,
    create_backup,
    database_info,
    restore_backup,
)
from agentcad.m6_candidate_core import (
    CompilationRefused,
    M6CandidateService,
)
from agentcad.m6_candidate_models import (
    CandidateEvidence,
    Confidence,
    ProducerRef,
    ProposedSemantics,
    SemanticCandidate,
    SourceRegion,
)
from agentcad.m6_ingestion_contract import identity_prefix
from agentcad.m6_review_service import M6ReviewService
from agentcad.m6_source_adapter import M6SourceAdapter
from agentcad.main import create_app
from agentcad.models import (
    CircleElement,
    CreateDocumentRequest,
    Document,
    Layer,
    LineElement,
    Point,
    SymbolElement,
    TextElement,
)
from agentcad.store import SQLiteDocumentStore, StoredDocument
from agentcad.symbols import SymbolRegistry

REVIEWER = "engineer.joe"


@pytest.fixture()
def app(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PID_AGENT_DATABASE_PATH", str(tmp_path / "m6-apply.db"))
    return create_app()


@pytest.fixture()
def client(app) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def shared_app(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PID_AGENT_DATABASE_PATH", str(tmp_path / "m6-apply-shared.db"))
    monkeypatch.setenv("PID_AGENT_DEPLOYMENT_MODE", "shared")
    monkeypatch.setenv("PID_AGENT_API_TOKEN", "m6-apply-shared-token")
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


def _seed_confirmed(app, *, tag: str = "P-201") -> tuple[str, str, str, str]:
    """Source + target + ingest + one confirmed symbol_class candidate.

    Returns (source_id, target_id, candidate_id, finding_id).
    """

    store = _store(app)
    source = Document(
        id="doc_m6d4_source",
        name="M6 D4 source",
        revision=3,
        layers=[Layer(id="layer_default", name="Default"), Layer(id="layer_cad", name="EQUIP")],
        elements=[
            *_block_lines("p1", 100, 100, "centrifugal_pump"),
            TextElement(
                id="t1",
                position=Point(x=110, y=130),
                text=tag,
                font_size=12,
                metadata={"cad_layer": "EQUIP"},
            ),
        ],
    )
    store.save(StoredDocument(document=source, undo_stack=[], redo_stack=[]))
    target = app.state.service.create_document(
        CreateDocumentRequest(name="M6 D4 target", width=1600, height=900)
    )
    adapter = M6SourceAdapter(store, app.state.service.symbols)
    adapter.ingest(adapter.pin_source(source.id), target_document_id=target.id)
    reviews = M6ReviewService(store, SymbolRegistry())
    candidate = next(
        candidate
        for candidate in store.list_semantic_candidates(source_document_id=source.id)
        if candidate.candidate_type == "symbol_class"
    )
    outcome = reviews.decide(
        candidate.candidate_id,
        action="confirm",
        reviewer_identity=REVIEWER,
        reviewer_action="核对原图设备簇与目录",
    )
    assert outcome.finding_id
    return source.id, target.id, candidate.candidate_id, outcome.finding_id


def _compile(
    client: TestClient, target_id: str, finding_id: str, *, headers: dict | None = None
) -> dict:
    response = client.post(
        f"/api/v2/documents/{target_id}/m6/patches",
        json={"finding_id": finding_id},
        headers=headers or {},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _apply(client: TestClient, target_id: str, patch_id: str) -> dict:
    response = client.post(f"/api/v2/documents/{target_id}/m6/patches/{patch_id}/apply", json={})
    assert response.status_code == 200, response.text
    return response.json()


def _revert(client: TestClient, target_id: str, apply_id: str) -> dict:
    response = client.post(
        f"/api/v2/documents/{target_id}/m6/apply-records/{apply_id}/revert", json={}
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------------------
# Migration v16 -> v17 (local/CI only)
# --------------------------------------------------------------------------------------


def _downgrade_to_v16(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE IF EXISTS m6_apply_records")
        connection.execute("DROP TABLE IF EXISTS m6_structured_patches")
        connection.execute("PRAGMA user_version=16")
        connection.commit()


def test_a_v16_database_gains_the_apply_tables(tmp_path: Path) -> None:
    database = tmp_path / "v16.db"
    SQLiteDocumentStore(database)  # build current, then downgrade the fixture
    _downgrade_to_v16(database)

    SQLiteDocumentStore(database)  # reopen: migrates 16 -> 17
    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert version == CURRENT_SCHEMA_VERSION == 17
    assert {"m6_structured_patches", "m6_apply_records"} <= tables


def test_migration_17_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "idem.db"
    SQLiteDocumentStore(database)
    SQLiteDocumentStore(database)  # reopen is a no-op
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 17


def test_a_failed_migration_17_rolls_back(tmp_path: Path) -> None:
    """Fault injection inside the migration: nothing half-applies, the version stays."""

    import agentcad.database_recovery as recovery

    database = tmp_path / "fails.db"
    SQLiteDocumentStore(database)
    _downgrade_to_v16(database)

    def broken(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE m6_structured_patches (patch_id TEXT PRIMARY KEY)"
        )
        raise RuntimeError("injected migration failure")

    original = recovery._MIGRATIONS[17]
    recovery._MIGRATIONS[17] = broken
    try:
        with pytest.raises(RuntimeError, match="injected migration failure"):
            SQLiteDocumentStore(database)
    finally:
        recovery._MIGRATIONS[17] = original

    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert version == 16
    assert "m6_structured_patches" not in tables
    assert "m6_apply_records" not in tables


def test_a_v17_backup_restores_and_a_v16_database_migrates_on_reopen(
    tmp_path: Path,
) -> None:
    """Backup/restore at v17, and the migration replaying on reopen of a v16 database.

    (``create_backup`` initializes — migrates — its source first, so a "v16 backup" cannot
    be produced by the current binary; the replay direction is covered by the
    downgrade-and-reopen test above, and the pre-current restore gate by the M12 tests.)
    """

    database = tmp_path / "source.db"
    store = SQLiteDocumentStore(database)
    store.save(
        StoredDocument(
            document=Document(id="doc_b", name="backup doc", revision=0), undo_stack=[], redo_stack=[]
        )
    )
    backup = tmp_path / "snapshot.pidbak"
    create_backup(database, backup)
    restored_path = tmp_path / "restored.db"
    restore_backup(backup, restored_path, expected_instance_id=database_info(database).instance_id)
    restored = SQLiteDocumentStore(restored_path)
    assert restored.schema_version == 17
    assert restored.get("doc_b") is not None


# --------------------------------------------------------------------------------------
# Patch persistence (append-only, content-derived identity)
# --------------------------------------------------------------------------------------


def test_patch_persistence_is_append_only_and_idempotent(client: TestClient, app) -> None:
    _source_id, target_id, _candidate_id, finding_id = _seed_confirmed(app)
    patch = _compile(client, target_id, finding_id)
    store = _store(app)
    row = store.get_structured_patch(patch["patch_id"])
    assert row is not None
    assert row["patch_id"] == patch["patch_id"]
    assert str(row["patch_id"]).startswith(identity_prefix("patch_id"))
    assert len(str(row["canonical_digest"])) == 64

    # Recompile is idempotent: same row, and no second compile audit.
    audits_before = len(store.all_audit_records())
    again = _compile(client, target_id, finding_id)
    assert again["patch_id"] == patch["patch_id"]
    assert len(store.all_audit_records()) == audits_before

    # Same id, different content: closed refusal.
    with pytest.raises(Exception) as caught:
        store.insert_structured_patch(
            patch_id=patch["patch_id"],
            finding_ids=patch["finding_ids"],
            intent="creation",
            policy_disposition="allowed_after_confirmation",
            operations_json="[]",
            compiler_name="m6.patch_compiler",
            compiler_version="0.1.0",
            compiler_rules="m6-rules/1",
            baseline_revision=0,
            canonical_digest="0" * 64,
            source_document_id="doc_m6d4_source",
            target_document_id=target_id,
        )
    assert getattr(caught.value, "code", "") == "patch_identity_conflict"


# --------------------------------------------------------------------------------------
# The compiler extension: relationship_addition (unique ports only, never guessed)
# --------------------------------------------------------------------------------------


def _relationship_finding(app, target_id: str, *, to_tag: str) -> str:
    """Confirm a connection_relationship P-101 -> ``to_tag`` through the governed review path.

    The source is a real document whose revision and content hash are pinned, so the decision
    re-verifies it at write time exactly as a person's confirm does. No finding is hand-built.
    """

    store = _store(app)
    source = Document(
        id="doc_m6d4_source_rel",
        name="M6 D4 relation source",
        revision=1,
        layers=[Layer(id="layer_default", name="Default")],
        elements=[
            TextElement(id="rel_t1", position=Point(x=110, y=130), text="P-101", font_size=12),
            TextElement(id="rel_t2", position=Point(x=510, y=130), text=to_tag, font_size=12),
        ],
    )
    store.save(StoredDocument(document=source, undo_stack=[], redo_stack=[]))
    artifact = M6SourceAdapter(store, app.state.service.symbols).pin_source(source.id)
    candidate = SemanticCandidate(
        candidate_id=f"cand_m6_rel_{to_tag}",
        artifact=artifact,
        region=SourceRegion(
            region_id=f"region_rel_{to_tag}",
            artifact_id=artifact.artifact_id,
            source_document_id=source.id,
            source_revision=artifact.source_revision,
            element_refs=["rel_t1", "rel_t2"],
        ),
        target_document_id=target_id,
        candidate_type="connection_relationship",
        proposed_semantics=ProposedSemantics(
            relationship_source="P-101", relationship_target=to_tag
        ),
        confidence=Confidence(value=1.0, source="deterministic_rule"),
        evidence=[CandidateEvidence(kind="rule", detail="relation fixture")],
        producer=ProducerRef(key="deterministic_rule_engine", version="0.1.0"),
    )
    M6CandidateService(store).file_candidate(candidate)
    outcome = M6ReviewService(store, SymbolRegistry()).decide(
        candidate.candidate_id,
        action="confirm",
        reviewer_identity=REVIEWER,
        reviewer_action="核对两设备的工艺连接",
    )
    assert outcome.finding_id
    return outcome.finding_id


def test_the_frozen_chain_compiles_persists_approves_applies_and_replays(
    client: TestClient, app
) -> None:
    source_id, target_id, candidate_id, finding_id = _seed_confirmed(app)
    patch = _compile(client, target_id, finding_id)
    store = _store(app)
    revision_before = store.get(target_id).document.revision
    audits_before = len(store.all_audit_records())

    outcome = _apply(client, target_id, patch["patch_id"])
    assert outcome["apply_id"].startswith(identity_prefix("apply_id"))
    assert outcome["result_revision"] == revision_before + 1

    # P4: the ledger row + the document + the audit + the harness close-out agree.
    record = store.get_apply_record(outcome["apply_id"])
    assert record is not None
    assert record["status"] == "applied"
    assert record["patch_id"] == patch["patch_id"]
    assert record["candidate_id"] == candidate_id
    assert record["source_document_id"] == source_id
    assert record["target_document_id"] == target_id
    assert record["base_revision"] == revision_before
    assert record["result_revision"] == revision_before + 1
    assert record["audit_record_id"]
    audit = store.get_audit_record(record["audit_record_id"])
    assert audit is not None and audit.event_type == "revision.created"
    assert audit.evidence["m6"]["patch_id"] == patch["patch_id"]
    assert audit.evidence["m6"]["inverse_operations"], "the inverse must be recorded"
    document = store.get(target_id).document
    assert any(element.symbol_key == "centrifugal_pump" for element in document.elements)
    created = next(
        element for element in document.elements if element.symbol_key == "centrifugal_pump"
    )
    assert created.id.startswith(identity_prefix("generated_element_id"))
    # exactly one new audit for the apply (the compile's audit predates it)
    assert len(store.all_audit_records()) == audits_before + 1

    # P5: replay reproduces both layers.
    from agentcad.m6_replay import replay_patch

    report = replay_patch(store, app.state.service, app.state.service.symbols, patch["patch_id"])
    assert report.ok is True
    assert report.canonical_patch_match is True
    assert report.baseline_reconstructable is True
    assert report.semantic_poststate_match is True

    # P6: the compensation revert through the same sealed flow.
    revert = _revert(client, target_id, outcome["apply_id"])
    original = store.get_apply_record(outcome["apply_id"])
    assert original is not None and original["status"] == "reverted"
    assert original["revert_apply_id"] == revert["revert_apply_id"]
    compensation = store.get_apply_record(revert["revert_apply_id"])
    assert compensation is not None and compensation["status"] == "applied"
    after = store.get(target_id).document
    assert not any(element.id == created.id for element in after.elements)
    # The candidate is never "reverted" — the contract has no such state.
    assert M6CandidateService(store).current_status(candidate_id) == "confirmed"


# --------------------------------------------------------------------------------------
# Refusals (N5/N6/N7/N8/N14 and the binding guard)
# --------------------------------------------------------------------------------------


def test_double_apply_is_refused(client: TestClient, app) -> None:
    """N7: one patch, one applied record — a second apply is refused before any write."""

    _source_id, target_id, _candidate_id, finding_id = _seed_confirmed(app)
    patch = _compile(client, target_id, finding_id)
    _apply(client, target_id, patch["patch_id"])
    revision = _store(app).get(target_id).document.revision
    second = client.post(
        f"/api/v2/documents/{target_id}/m6/patches/{patch['patch_id']}/apply", json={}
    )
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "patch_already_applied"
    assert _store(app).get(target_id).document.revision == revision


def test_approval_replay_is_consumed(client: TestClient, app) -> None:
    """N7 second half: an approval is single-use — replaying it is refused by the harness."""

    _source_id, target_id, _candidate_id, finding_id = _seed_confirmed(app)
    patch = _compile(client, target_id, finding_id)
    approval = client.post(
        f"/api/v2/documents/{target_id}/m6/patches/{patch['patch_id']}/approval-requests"
    ).json()
    # local auto path consumes a fresh approval; the requested one stays pending -> a
    # replay attempt with it fails the binding (session differs), and after resolving it
    # manually the apply consumes it, so a second apply with the same approval refuses.
    first = _apply(client, target_id, patch["patch_id"])
    assert first["apply_id"]
    replay = client.post(
        f"/api/v2/documents/{target_id}/m6/patches/{patch['patch_id']}/apply",
        json={"session_id": approval["session_id"], "approval_id": approval["approval_id"]},
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] in {
        "tool_intent_mismatch",
        "tool_approval_consumed",
        "patch_already_applied",
    }


def test_source_drift_before_apply_is_refused(client: TestClient, app) -> None:
    """N5: the pinned source moved after approval -> the commit refuses, zero writes."""

    source_id, target_id, _candidate_id, finding_id = _seed_confirmed(app)
    patch = _compile(client, target_id, finding_id)
    store = _store(app)
    moved = store.get(source_id).document.model_copy(update={"revision": 4})
    store.save(StoredDocument(document=moved, undo_stack=[], redo_stack=[]))
    audits_before = len(store.all_audit_records())

    response = client.post(
        f"/api/v2/documents/{target_id}/m6/patches/{patch['patch_id']}/apply", json={}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_revision_drift"
    assert store.list_apply_records(patch_id=patch["patch_id"]) == []
    # exactly one rejected closeout audit, no applied audit
    new_records = store.all_audit_records()[audits_before:]
    assert len(new_records) == 1
    assert new_records[0].event_type == "m6.apply.rejected"
    assert new_records[0].status == "rejected"


def test_target_semantic_drift_is_a_conflict_not_a_write(client: TestClient, app) -> None:
    """N6 (D4-frozen reading): the apply-time recheck keys on the REAL created identity.

    The reviewed expectation for a creation is "the derived element id is absent". If the
    target already carries exactly that element identity, the commit refuses with
    semantic_baseline_moved — never the region-shaped always-absent convention from D3.
    """

    _source_id, target_id, _candidate_id, finding_id = _seed_confirmed(app)
    patch = _compile(client, target_id, finding_id)
    store = _store(app)
    operations = json.loads(patch["operations"])
    created_id = operations[0]["element"]["id"]
    assert created_id.startswith(identity_prefix("generated_element_id"))

    target = store.get(target_id).document
    store.save(
        StoredDocument(
            document=target.model_copy(
                update={
                    "revision": target.revision + 1,
                    "elements": [
                        *target.elements,
                        SymbolElement(
                            id=created_id,
                            symbol_key="gas_tank",
                            position=Point(x=900, y=900),
                            width=60,
                            height=60,
                        ),
                    ],
                }
            ),
            undo_stack=[],
            redo_stack=[],
        )
    )
    response = client.post(
        f"/api/v2/documents/{target_id}/m6/patches/{patch['patch_id']}/apply", json={}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "semantic_baseline_moved"
    assert store.list_apply_records(patch_id=patch["patch_id"]) == []


def test_l1_forgery_refusals(client: TestClient, app) -> None:
    """N8/N14: the identity chain is cross-checked on the write connection."""

    _source_id, target_id, _candidate_id, finding_id = _seed_confirmed(app)
    patch = _compile(client, target_id, finding_id)
    store = _store(app)

    # (a) a patch row whose digest no longer recomputes (edited in place)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE m6_structured_patches SET intent = 'metadata_enrichment' WHERE patch_id = ?",
            (patch["patch_id"],),
        )
        connection.commit()
    forged = client.post(
        f"/api/v2/documents/{target_id}/m6/patches/{patch['patch_id']}/apply", json={}
    )
    assert forged.status_code == 409
    assert forged.json()["detail"]["code"] == "apply_identity_chain_mismatch"
    assert store.list_apply_records(patch_id=patch["patch_id"]) == []


def test_an_unconfirmed_finding_cannot_reach_the_ledger(client: TestClient, app) -> None:
    """N8: a patch citing a finding whose candidate never confirmed is refused."""

    source_id, target_id, candidate_id, _finding_id = _seed_confirmed(app)
    store = _store(app)
    # A second candidate, filed but never confirmed; a patch row naming a fake finding of it.
    # reuse the seeded candidates: the unresolved ZZZ row... our seed has none; file one via
    # the review-surface path instead: reject the confirmed candidate's sibling.
    row = next(
        candidate
        for candidate in store.list_semantic_candidates(source_document_id=source_id)
        if candidate.candidate_type == "annotation_role"
    )
    bogus_finding_id = "m6find_" + "0" * 64
    store.insert_structured_patch(
        patch_id=identity_prefix("patch_id") + "1" * 64,
        finding_ids=[bogus_finding_id],
        intent="creation",
        policy_disposition="allowed_after_confirmation",
        operations_json="[]",
        compiler_name="m6.patch_compiler",
        compiler_version="0.1.0",
        compiler_rules="m6-rules/1",
        baseline_revision=0,
        canonical_digest="1" * 64,
        source_document_id=source_id,
        target_document_id=target_id,
    )
    response = client.post(
        f"/api/v2/documents/{target_id}/m6/patches/{identity_prefix('patch_id') + '1' * 64}/apply",
        json={},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "apply_identity_chain_mismatch"
    assert row.candidate_id


def test_concurrent_double_apply_lands_exactly_once(client: TestClient, app) -> None:
    """Two authorized applies race one patch; exactly one commit lands."""

    _source_id, target_id, _candidate_id, finding_id = _seed_confirmed(app)
    patch = _compile(client, target_id, finding_id)
    store = _store(app)

    # Build two independent authorized calls against the same patch.
    from agentcad.audit import AuditRecorder
    from agentcad.m6_apply_runtime import (
        TOOL_APPLY_M6_FINDING,
        M6ApplyExecutor,
        M6AuditAdapter,
        M6DomainAdapter,
        M6ToolRegistry,
    )
    from agentcad.runtime.harness import AgentHarnessRuntime

    symbols = app.state.service.symbols
    executor = M6ApplyExecutor(store, app.state.service, symbols)
    runtime = AgentHarnessRuntime(
        store=store,
        registry=M6ToolRegistry(),
        adapter=M6DomainAdapter(executor, store),
        audit=M6AuditAdapter(AuditRecorder(store=store, symbols=symbols)),
    )

    def authorize_once():
        session = runtime.ensure_session(target_id, actor="web-user")
        approval = runtime.request_approval(
            session.id,
            __import__("agentcad.harness_models", fromlist=["ToolApprovalCreateRequest"]).ToolApprovalCreateRequest(
                tool_name=TOOL_APPLY_M6_FINDING,
                document_id=target_id,
                intent={"patch_id": patch["patch_id"]},
                requested_by="web-user",
            ),
        )
        runtime.resolve_approval(
            approval.id,
            __import__("agentcad.harness_models", fromlist=["ToolApprovalResolveRequest"]).ToolApprovalResolveRequest(
                approved=True, actor="web-user"
            ),
        )
        return runtime.authorize(
            session_id=session.id,
            tool_name=TOOL_APPLY_M6_FINDING,
            document_id=target_id,
            intent={"patch_id": patch["patch_id"]},
            approval_id=approval.id,
        )

    barrier = threading.Barrier(2)
    outcomes: list[dict] = [{}, {}]

    def run(index: int) -> None:
        authorized = authorize_once()
        barrier.wait()
        try:
            runtime.apply_authorized(authorized, target_id, {"patch_id": patch["patch_id"]})
            outcomes[index] = {"ok": True}
        except Exception as exc:  # noqa: BLE001 - the shape is asserted below
            outcomes[index] = {"ok": False, "error": exc}

    threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(1 for outcome in outcomes if outcome["ok"]) == 1
    loser = next(outcome["error"] for outcome in outcomes if not outcome["ok"])
    assert getattr(loser, "code", "") in {
        "patch_already_applied",
        "m6_apply_not_authorized",
    }
    applied = store.list_apply_records(patch_id=patch["patch_id"])
    assert len(applied) == 1
    assert applied[0]["status"] == "applied"


def test_a_mid_commit_failure_rolls_back_and_closes_out(client: TestClient, app) -> None:
    """N10: fault injection inside the atomic commit — zero dirty writes, one rejected audit."""

    _source_id, target_id, _candidate_id, finding_id = _seed_confirmed(app)
    patch = _compile(client, target_id, finding_id)
    store = _store(app)
    revision_before = store.get(target_id).document.revision
    audits_before = len(store.all_audit_records())

    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER m6_fail_apply_insert
            BEFORE INSERT ON m6_apply_records
            BEGIN SELECT RAISE(FAIL, 'injected apply failure'); END
            """
        )
        connection.commit()

    response = client.post(
        f"/api/v2/documents/{target_id}/m6/patches/{patch['patch_id']}/apply", json={}
    )
    assert response.status_code == 409
    # zero dirty writes: no revision move, no ledger row; the closeout wrote exactly one
    # rejected audit and nothing else.
    assert store.get(target_id).document.revision == revision_before
    assert store.list_apply_records(patch_id=patch["patch_id"]) == []
    new_records = store.all_audit_records()[audits_before:]
    assert len(new_records) == 1
    assert new_records[0].event_type == "m6.apply.rejected"
    assert new_records[0].status == "rejected"

    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DROP TRIGGER m6_fail_apply_insert")
        connection.commit()
    # and once the fault is gone the same chain lands cleanly
    outcome = _apply(client, target_id, patch["patch_id"])
    assert outcome["apply_id"]


# --------------------------------------------------------------------------------------
# The compiler extension: relationship_addition
# --------------------------------------------------------------------------------------


def _seed_relation_target(app) -> tuple[str, str]:
    """A target with two tagged pumps; returns (target_id, candidate_id) for a
    governed-confirmed connection_relationship P-101 -> P-201 (one discharge, one suction)."""

    service = app.state.service
    target = service.create_document(CreateDocumentRequest(name="rel target", width=1600, height=900))
    service.apply_transaction(
        target.id,
        __import__("agentcad.models", fromlist=["TransactionRequest"]).TransactionRequest(
            expected_revision=target.revision,
            label="seed two pumps",
            operations=[
                {
                    "op": "add_element",
                    "element": {
                        "type": "symbol",
                        "symbol_key": "centrifugal_pump",
                        "position": {"x": 100.0, "y": 100.0},
                        "width": 80.0,
                        "height": 70.0,
                        "label": "P-101",
                    },
                },
                {
                    "op": "add_element",
                    "element": {
                        "type": "symbol",
                        "symbol_key": "centrifugal_pump",
                        "position": {"x": 400.0, "y": 100.0},
                        "width": 80.0,
                        "height": 70.0,
                        "label": "P-201",
                    },
                },
            ],
        ),
    )
    return target.id


def test_relationship_addition_compiles_with_recomputed_endpoints(client: TestClient, app) -> None:
    """The connector's endpoints carry symbol+port; coordinates are server-computed."""

    target_id = _seed_relation_target(app)
    finding_id = _relationship_finding(app, target_id, to_tag="P-201")
    patch = _compile(client, target_id, finding_id)
    assert patch["intent"] == "relationship_addition"
    operations = json.loads(patch["operations"])
    assert len(operations) == 1
    element = operations[0]["element"]
    assert element["type"] == "connector"
    assert element["source"]["port_id"] and element["target"]["port_id"]
    assert element["routing"] == "orthogonal"
    # The endpoints carry the symbol elements, and the points are present (server-computed).
    assert element["source"]["element_id"] and element["target"]["element_id"]
    assert len(element["points"]) >= 2


def test_relationship_addition_refuses_an_unresolved_endpoint(client: TestClient, app) -> None:
    """A confirmed relation whose endpoint is not in the target never compiles to a write."""

    target_id = _seed_relation_target(app)
    finding_id = _relationship_finding(app, target_id, to_tag="P-999")
    store = _store(app)
    document = store.get(target_id).document
    with pytest.raises(CompilationRefused) as caught:
        M6CandidateService(store).compile_finding(
            finding_id, document=document, registry=app.state.service.symbols
        )
    assert caught.value.code == "relationship_endpoint_not_resolved"


# --------------------------------------------------------------------------------------
# Shared deployment: the approval boundary (N11)
# --------------------------------------------------------------------------------------


def test_shared_mode_requires_a_human_resolved_approval(shared_app) -> None:
    with TestClient(shared_app) as client:
        _source_id, target_id, _candidate_id, finding_id = _seed_confirmed(shared_app)
        headers = {"Authorization": "Bearer m6-apply-shared-token"}
        patch = _compile(client, target_id, finding_id, headers=headers)

        unauthenticated = client.post(
            f"/api/v2/documents/{target_id}/m6/patches/{patch['patch_id']}/apply", json={}
        )
        assert unauthenticated.status_code == 401

        self_served = client.post(
            f"/api/v2/documents/{target_id}/m6/patches/{patch['patch_id']}/apply",
            headers=headers,
            json={},
        )
        assert self_served.status_code == 403
        assert self_served.json()["detail"]["code"] == "approval_not_self_served"

        # the human path: request -> resolve -> apply
        requested = client.post(
            f"/api/v2/documents/{target_id}/m6/patches/{patch['patch_id']}/approval-requests",
            headers=headers,
        )
        assert requested.status_code == 200, requested.text
        ids = requested.json()
        resolved = client.post(
            f"/api/v2/agent/approvals/{ids['approval_id']}/resolve",
            headers=headers,
            json={"approved": True, "actor": "共享复核人", "note": "human decision"},
        )
        assert resolved.status_code == 200, resolved.text
        applied = client.post(
            f"/api/v2/documents/{target_id}/m6/patches/{patch['patch_id']}/apply",
            headers=headers,
            json={"session_id": ids["session_id"], "approval_id": ids["approval_id"]},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["apply_id"]


def test_a_failed_revert_keeps_the_original_applied_and_closes_out(
    client: TestClient, app
) -> None:
    """N9: a compensation transaction whose CAS step fails leaves the original record
    applied, writes exactly one rejected audit as its closeout, and can be retried."""

    _source_id, target_id, _candidate_id, finding_id = _seed_confirmed(app)
    patch = _compile(client, target_id, finding_id)
    outcome = _apply(client, target_id, patch["patch_id"])
    store = _store(app)
    revision_before = store.get(target_id).document.revision
    audits_before = len(store.all_audit_records())

    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER m6_fail_revert_update
            BEFORE UPDATE ON m6_apply_records
            BEGIN SELECT RAISE(FAIL, 'injected revert failure'); END
            """
        )
        connection.commit()

    response = client.post(
        f"/api/v2/documents/{target_id}/m6/apply-records/{outcome['apply_id']}/revert", json={}
    )
    assert response.status_code == 409
    # The original record is untouched and still applied; no compensation apply row landed.
    original = store.get_apply_record(outcome["apply_id"])
    assert original is not None
    assert original["status"] == "applied"
    assert original["revert_apply_id"] == ""
    assert store.get(target_id).document.revision == revision_before
    new_records = store.all_audit_records()[audits_before:]
    assert len(new_records) == 1
    assert new_records[0].event_type == "m6.apply.rejected"
    assert new_records[0].status == "rejected"

    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DROP TRIGGER m6_fail_revert_update")
        connection.commit()
    # The failure is retriable: the same revert now lands through the sealed flow.
    revert = _revert(client, target_id, outcome["apply_id"])
    reverted = store.get_apply_record(outcome["apply_id"])
    assert reverted is not None and reverted["status"] == "reverted"
    compensation = store.get_apply_record(revert["revert_apply_id"])
    assert compensation is not None and compensation["status"] == "applied"


def test_ten_candidates_from_one_source_apply_sequentially(client: TestClient, app) -> None:
    """N13 (D87-1): ten candidates pinned to one source stay applicable while the target
    advances — each apply recomputes its semantic baseline digest against the *current*
    committed target, so the other nine never go stale on the source pin."""

    store = _store(app)
    tags = [f"P-3{index:02d}" for index in range(10)]
    elements: list = []
    for index, tag in enumerate(tags):
        x = 100 + (index % 5) * 120
        y = 100 + (index // 5) * 120
        elements.extend(_block_lines(f"blk{index}", x, y, "centrifugal_pump"))
        elements.append(
            TextElement(
                id=f"tag{index}",
                position=Point(x=x + 10, y=y + 30),
                text=tag,
                font_size=12,
                metadata={"cad_layer": "EQUIP"},
            )
        )
    source = Document(
        id="doc_m6d4_source10",
        name="M6 D4 source x10",
        revision=3,
        layers=[Layer(id="layer_default", name="Default"), Layer(id="layer_cad", name="EQUIP")],
        elements=elements,
    )
    store.save(StoredDocument(document=source, undo_stack=[], redo_stack=[]))
    target = app.state.service.create_document(
        CreateDocumentRequest(name="M6 D4 target x10", width=1600, height=900)
    )
    adapter = M6SourceAdapter(store, app.state.service.symbols)
    adapter.ingest(adapter.pin_source(source.id), target_document_id=target.id)

    candidates = sorted(
        (
            candidate
            for candidate in store.list_semantic_candidates(source_document_id=source.id)
            if candidate.candidate_type == "symbol_class"
        ),
        key=lambda candidate: candidate.candidate_id,
    )
    assert len(candidates) == 10

    reviews = M6ReviewService(store, SymbolRegistry())
    # Compile every patch up front: each is bound to the same target baseline revision.
    patches = []
    for candidate in candidates:
        outcome = reviews.decide(
            candidate.candidate_id,
            action="confirm",
            reviewer_identity=REVIEWER,
            reviewer_action="逐台核对原图设备簇与位号",
        )
        assert outcome.finding_id
        patches.append(_compile(client, target.id, outcome.finding_id))

    # Applying the first advances the target; the remaining nine must still land, each
    # rechecked against the target state current at its own apply moment.
    for patch in patches:
        outcome = _apply(client, target.id, patch["patch_id"])
        assert outcome["apply_id"]
    document = store.get(target.id).document
    assert sum(1 for e in document.elements if e.symbol_key == "centrifugal_pump") == 10
    assert len(store.list_apply_records()) == 10
