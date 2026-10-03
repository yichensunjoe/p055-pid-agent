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
    member: bool = True,
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
    if member:
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
    source_endpoint: str = "from",
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
            ") VALUES (?, ?, 'cable_endpoint_equipment', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'tester', ?, '')",
            (
                link_id,
                DEFAULT_PROJECT,
                source_domain,
                source_id,
                source_ref,
                source_endpoint,
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


def test_dangling_source_positive(tmp_path: Path) -> None:
    """Frozen-matrix positive coverage: dangling_link_source."""
    plane = Plane(tmp_path, "dangle_src.db")
    _seed_cable_envelope(plane, "cab_unaffiliated", member=False)  # registered, NOT a member
    pid_id = _seed_pid(plane)
    _insert_link(plane, "lnk_ds", source_id="cab_unaffiliated", target_id=pid_id)
    result = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    assert "dangling_link_source" in _codes(result)
    assert result.state == "not_eligible"


def test_missing_source_object_positive(tmp_path: Path) -> None:
    """Frozen-matrix positive coverage: missing_source_object at the CURRENT
    cable revision (pins are current, the segment simply does not exist)."""
    plane = Plane(tmp_path, "missing_src.db")
    cable_id = _seed_cable_envelope(plane, "cab_ms")
    pid_id = _seed_pid(plane)
    _insert_link(plane, "lnk_ms", source_id=cable_id, source_ref="NOPE", target_id=pid_id)
    result = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    assert "missing_source_object" in _codes(result)
    assert "stale_pinned_revision_source" not in _codes(result)
    assert result.state == "not_eligible"


def test_stale_target_positive_reports_only_stale(tmp_path: Path) -> None:
    """Frozen-matrix positive coverage: stale_pinned_revision_target."""
    plane = Plane(tmp_path, "stale_tgt.db")
    cable_id = _seed_cable_envelope(plane, "cab_st")
    pid_id = _seed_pid(plane)
    _insert_link(plane, "lnk_st", source_id=cable_id, target_id=pid_id, pinned_target=1)
    # advance the pid document so the pinned target revision is behind
    document = plane.service.get_document(pid_id)
    plane.service.apply_transaction(
        pid_id,
        TransactionRequest(
            expected_revision=document.revision,
            label="touch revision",
            operations=[
                AddElementOperation(
                    element=SymbolElement(
                        id="PMP-102",
                        symbol_key="agitator",
                        position={"x": 300, "y": 100},
                        width=60,
                        height=40,
                        label="PMP-102",
                    )
                )
            ],
        ),
    )
    result = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    assert "stale_pinned_revision_target" in _codes(result)
    assert "missing_target_object" not in _codes(result)
    assert result.state == "not_eligible"


def test_wrong_domain_isolated_declaration_vs_registry(tmp_path: Path) -> None:
    """D80-1: wrong_domain_reference fires exactly when the link row's DECLARED
    domain drifts from the registry's actual domain — not merely when a
    semantically odd link exists."""
    plane = Plane(tmp_path, "wd_iso.db")
    cable_id = _seed_cable_envelope(plane, "cab_wi")
    pid_id = _seed_pid(plane)
    # honest link: declaration matches registry truth -> no finding
    _insert_link(plane, "lnk_ok", source_id=cable_id, target_id=pid_id)
    # drifted declaration: row claims 'cable' for a document the registry
    # records as 'pid' -> exactly one wrong_domain finding on this link
    _insert_link(
        plane,
        "lnk_drift",
        source_id=pid_id,
        source_ref="PMP-101",
        source_domain="cable",
        target_id=pid_id,
        target_ref="PMP-101",
        target_domain="pid",
    )
    result = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    findings = [i for i in result.issues if i.code == "wrong_domain_reference"]
    assert [i.link_id for i in findings] == ["lnk_drift"]


def test_nonexistent_project_fails_closed(tmp_path: Path) -> None:
    """D80-3: a nonexistent Project Graph must fail closed — never eligible."""
    plane = Plane(tmp_path, "no_project.db")
    from agentcad.project_readiness import ProjectReadinessError

    with pytest.raises(ProjectReadinessError) as exc_info:
        plane.readiness.assess(project_id="proj_nope", evaluation_as_of=AS_OF)
    assert exc_info.value.code == "project_not_found"


def test_profile_drift_propagates_into_project_hash(tmp_path: Path, monkeypatch) -> None:
    """F77 hard-lock: with evaluation_as_of FIXED, changing the effective P&ID
    profile (an added waiver) changes the P&ID member readiness_hash and the
    project result_hash together — member provenance really propagates."""
    from agentcad import project_readiness as readiness_module
    from agentcad.validation_models import Waiver
    from agentcad.validation_profile import _fingerprint, load_profile

    plane = Plane(tmp_path, "profile_drift.db")
    cable_id = _seed_cable_envelope(plane, "cab_pd")
    pid_id = _seed_pid(plane)
    _insert_link(plane, "lnk_pd", source_id=cable_id, target_id=pid_id)

    baseline = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)

    base_profile = load_profile()
    waived = base_profile.model_copy(
        update={
            "waivers": base_profile.waivers
            + [
                Waiver(
                    waiver_id="w-test-drift",
                    rule_id="*",
                    actor="gate-test",
                    reason="F77 drift hard-lock",
                    granted_at=AS_OF,
                )
            ]
        }
    )
    waived = waived.model_copy(update={"fingerprint": _fingerprint(waived.taints())})
    monkeypatch.setattr(readiness_module, "load_profile", lambda: waived)

    drifted = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    pid_before = next(m for m in baseline.members if m.document_id == pid_id)
    pid_after = next(m for m in drifted.members if m.document_id == pid_id)
    cable_after = next(m for m in drifted.members if m.document_id == cable_id)
    cable_before = next(m for m in baseline.members if m.document_id == cable_id)
    assert pid_after.readiness_hash != pid_before.readiness_hash
    assert cable_after.readiness_hash == cable_before.readiness_hash  # cable untouched
    assert drifted.result_hash != baseline.result_hash


def test_missing_source_object_covers_illegal_endpoint_corruption(tmp_path: Path) -> None:
    """D80-2 hard-lock: the frozen definition requires BOTH the segment in the
    current cable revision AND a legal from/to endpoint. A corrupted row (an
    endpoint the D3 write path would never store) must fail closed as
    missing_source_object."""
    plane = Plane(tmp_path, "corrupt_endpoint.db")
    cable_id = _seed_cable_envelope(plane, "cab_ce")  # SEG-1 exists at r1
    pid_id = _seed_pid(plane)
    _insert_link(
        plane,
        "lnk_ce",
        source_id=cable_id,
        source_ref="SEG-1",  # segment exists…
        source_endpoint="middle",  # …but the endpoint is not a legal from/to
        target_id=pid_id,
    )
    result = plane.readiness.assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    assert "missing_source_object" in _codes(result)
    assert "stale_pinned_revision_source" not in _codes(result)
    assert result.state == "not_eligible"
