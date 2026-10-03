"""M12-D4 hard locks: project-level deterministic validation / readiness.

Gate-frozen scope (M12-D4 CODE GO): canonical member readiness aggregation
(P&ID via assess_document_release_readiness + server-side load_profile with
evaluation_as_of; Cable via assess_cable_document), seven stable issue codes,
stale-first semantics (once stale, only the stale finding per endpoint),
soft-deleted links excluded, eligible/not_eligible only, result_hash binding
per F77-1, pure read with zero audit growth.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentcad.audit import AuditRecorder
from agentcad.cable_service import CableService
from agentcad.models import (
    AddElementOperation,
    CreateDocumentRequest,
    DeleteElementOperation,
    SymbolElement,
    TransactionRequest,
)
from agentcad.project_readiness import (
    ISSUE_DANGLING_TARGET,
    ISSUE_MISSING_TARGET,
    ISSUE_STALE_SOURCE,
    ProjectReadinessService,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry

DEFAULT_PROJECT = "proj_m12default"
AS_OF = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


class Plane:
    def __init__(self, tmp_path: Path, name: str) -> None:
        self.store = SQLiteDocumentStore(tmp_path / name)
        self.service = DocumentService(self.store, SymbolRegistry())
        self.cable = CableService(self.store)
        self.readiness = ProjectReadinessService(self.store, self.service, self.cable)
        self.recorder = AuditRecorder(store=self.store, symbols=SymbolRegistry())


def _seed_cable_envelope(
    plane: Plane,
    cable_id: str,
    *,
    segment_id: str = "SEG-1",
    revision: int = 1,
    with_segment: bool = True,
) -> str:
    now = datetime.now(UTC).isoformat()
    segments = [{"id": segment_id, "from_node": "MCC-1", "to_node": "PMP-101"}] if with_segment else []
    payload = json.dumps(
        {"schema": "pid-agent.cable-document/1", "name": "cable", "segments": segments}
    )
    with plane.store._connect() as connection:  # noqa: SLF001 - test fixture
        connection.execute(
            "INSERT INTO documents_registry (document_id, domain, created_at) "
            "VALUES (?, 'cable', ?)",
            (cable_id, now),
        )
        connection.execute(
            "INSERT INTO cable_documents (document_id, revision, data_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (cable_id, revision, payload, now, now),
        )
        connection.commit()
    plane.store.add_document_to_project(DEFAULT_PROJECT, cable_id, added_by="tester")
    return cable_id


def _seed_pid(plane: Plane, element_id: str = "PMP-101", symbol_key: str = "agitator") -> str:
    document = plane.service.create_document(
        CreateDocumentRequest(name="pid", width=800, height=600)
    )
    operations = []
    if element_id:
        operations.append(
            AddElementOperation(
                element=SymbolElement(
                    id=element_id,
                    symbol_key=symbol_key,
                    position={"x": 100, "y": 100},
                    width=60,
                    height=40,
                    label=element_id,
                )
            )
        )
    if operations:
        plane.service.apply_transaction(
            document.id,
            TransactionRequest(
                expected_revision=0,
                label="seed",
                operations=operations,
            ),
        )
    plane.store.add_document_to_project(DEFAULT_PROJECT, document.id, added_by="tester")
    return document.id


def _insert_link(
    plane: Plane,
    link_id: str,
    *,
    source_id: str,
    source_ref: str = "SEG-1",
    source_domain: str = "cable",
    target_id: str,
    target_ref: str = "PMP-101",
    target_domain: str = "pid",
    pinned_source: int = 1,
    pinned_target: int = 1,
    deleted: bool = False,
) -> None:
    now = datetime.now(UTC).isoformat()
    with plane.store._connect() as connection:  # noqa: SLF001 - test fixture
        connection.execute(
            "INSERT INTO engineering_links ("
            " link_id, project_id, relation_type, source_domain, source_document_id,"
            " source_object_ref, source_endpoint, target_domain, target_document_id,"
            " target_object_ref, pinned_source_revision, pinned_target_revision,"
            " created_at, created_by, deleted_at, deleted_by"
            ") VALUES (?, ?, 'cable_endpoint_equipment', ?, ?, ?, 'from', ?, ?, ?, ?, ?, ?, 'tester', ?, '')",
            (
                link_id,
                DEFAULT_PROJECT,
                source_domain,
                source_id,
                source_ref,
                target_domain,
                target_id,
                target_ref,
                pinned_source,
                pinned_target,
                now,
                now if deleted else "",
            ),
        )
        connection.commit()


def _codes(readiness) -> list[str]:
    return [issue.code for issue in readiness.issues]


def test_happy_path_eligible_with_full_provenance(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "happy.db")
    cable_id = _seed_cable_envelope(plane, "cab_h")
    pid_id = _seed_pid(plane)
    _insert_link(plane, "lnk_h", source_id=cable_id, target_id=pid_id)
    audits_before = len(plane.recorder.store.all_audit_records())
    result = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    assert result.state == "eligible"
    assert result.issues == ()
    assert {m.domain for m in result.members} == {"cable", "pid"}
    assert all(len(m.readiness_hash) == 64 for m in result.members)
    assert len(result.result_hash) == 64
    assert result.profile_id == "project-built-in"
    assert result.profile_version == 1
    assert len(result.profile_fingerprint) == 64
    # pure read: zero audit growth
    assert len(plane.recorder.store.all_audit_records()) == audits_before
    assert plane.recorder.verify_chain().ok


def test_evaluation_as_of_must_be_timezone_aware(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "tz.db")
    with pytest.raises(ValueError, match="timezone-aware"):
        plane.readiness.assess(
            project_id=DEFAULT_PROJECT,
            evaluation_as_of=datetime(2026, 10, 3, 12, 0),
        )


def test_result_hash_binds_evaluation_as_of(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "hash.db")
    cable_id = _seed_cable_envelope(plane, "cab_hash")
    pid_id = _seed_pid(plane)
    _insert_link(plane, "lnk_hash", source_id=cable_id, target_id=pid_id)
    first = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    second = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    assert first.result_hash == second.result_hash  # deterministic
    later = plane.readiness.assess(
        project_id=DEFAULT_PROJECT,
        evaluation_as_of=datetime(2026, 10, 3, 13, 0, tzinfo=UTC),
    )
    assert later.result_hash != first.result_hash  # provenance drift visible


def test_stale_source_reports_only_stale(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "stale.db")
    cable_id = _seed_cable_envelope(plane, "cab_stale", revision=2)  # pin says 1
    pid_id = _seed_pid(plane)
    _insert_link(plane, "lnk_stale", source_id=cable_id, target_id=pid_id, pinned_source=1)
    result = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    assert ISSUE_STALE_SOURCE in _codes(result)
    # stale-first: the stale endpoint must not also claim object checks
    assert "missing_source_object" not in _codes(result)
    assert result.state == "not_eligible"  # stale is fail-on-warning


def test_dangling_target_and_wrong_domain_blockers(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "dangling.db")
    cable_id = _seed_cable_envelope(plane, "cab_d")
    pid_id = _seed_pid(plane)
    outsider = plane.service.create_document(CreateDocumentRequest(name="out", width=10, height=10))
    _insert_link(plane, "lnk_d", source_id=cable_id, target_id=outsider.id)
    result = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    assert ISSUE_DANGLING_TARGET in _codes(result)
    # also exercises the chain: dangling target has no registry membership but
    # is registered as a pid document, so wrong_domain does NOT fire here
    assert "wrong_domain_reference" not in _codes(result)
    assert result.state == "not_eligible"

    # wrong domain: source column claims cable but id is a pid document
    _insert_link(
        plane,
        "lnk_wd",
        source_id=pid_id,
        source_ref="PMP-101",
        source_domain="cable",
        target_id=pid_id,
        pinned_source=1,
    )
    result = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    assert "wrong_domain_reference" in _codes(result)


def test_missing_target_object_at_current_revision(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "missing.db")
    cable_id = _seed_cable_envelope(plane, "cab_m")
    pid_id = _seed_pid(plane)
    # element removed (pid revision advances), link re-pinned to the new
    # revision by fixture but still pointing at the deleted element
    document = plane.service.get_document(pid_id)
    plane.service.apply_transaction(
        pid_id,
        TransactionRequest(
            expected_revision=document.revision,
            label="remove element",
            operations=[DeleteElementOperation(element_id="PMP-101")],
        ),
    )
    _insert_link(plane, "lnk_m", source_id=cable_id, target_id=pid_id, pinned_target=2)
    result = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    assert ISSUE_MISSING_TARGET in _codes(result)
    assert result.state == "not_eligible"


def test_soft_deleted_link_does_not_participate(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "soft.db")
    cable_id = _seed_cable_envelope(plane, "cab_s", revision=5)  # would be stale
    pid_id = _seed_pid(plane)
    _insert_link(plane, "lnk_s", source_id=cable_id, target_id=pid_id, pinned_source=1, deleted=True)
    result = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    assert result.issues == ()
    assert result.state == "eligible"


def test_member_readiness_aggregation_cable_not_eligible(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "member.db")
    cable_id = _seed_cable_envelope(plane, "cab_empty", with_segment=False)
    pid_id = _seed_pid(plane)
    _insert_link(plane, "lnk_ok", source_id=cable_id, target_id=pid_id)
    # empty cable document fails its own readiness (document_non_empty)
    result = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    cable_member = next(m for m in result.members if m.document_id == cable_id)
    assert cable_member.state == "not_eligible"
    assert result.state == "not_eligible"
