"""M13-D4 hard locks: exact intent approval + atomic multi-domain executor.

Gate-frozen hard-locks covered: approval binds project_id + declared pins +
evaluation_as_of + ordered mutations + D3 canonical impacted/re-pins +
effective profile fingerprint; apply re-computes and hash-compares (drift ->
change_set_conflict, zero writes); C4 (approved row alone never authorizes);
one success transaction (pid+cable CAS + re-pins + applied transition +
evidence + closeout + exactly one applied audit); failure = rollback +
separate closeout (refused + failed tool call + failed session + exactly one
rejected audit, no domain residue); snapshot readiness hash == post-commit
D4 canonical hash; approval replay refused.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentcad.audit import AuditRecorder
from agentcad.cable_models import CableSegment
from agentcad.cable_service import CableService
from agentcad.harness_models import (
    ToolApprovalCreateRequest,
    ToolApprovalResolveRequest,
)
from agentcad.models import (
    AddElementOperation,
    CreateDocumentRequest,
    SymbolElement,
    TransactionRequest,
)
from agentcad.project_change_set import (
    ChangeSetError,
    ChangeSetImpactAnalyzer,
    ChangeSetIntent,
    ChangeSetMutation,
    ChangeSetPreviewer,
    canonical_change_set_intent,
    change_set_intent_hash,
    new_change_set_id,
)
from agentcad.project_change_set_runtime import (
    TOOL_APPLY_CHANGE_SET,
    ChangeSetExecutor,
    ProjectChangeAuditAdapter,
    ProjectChangeDomainAdapter,
    ProjectChangeToolRegistry,
)
from agentcad.project_readiness import ProjectReadinessService
from agentcad.runtime.harness import AgentHarnessRuntime
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry

DEFAULT_PROJECT = "proj_m12default"
AS_OF = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


class Plane:
    def __init__(self, tmp_path: Path, name: str) -> None:
        self.store = SQLiteDocumentStore(tmp_path / name)
        self.service = DocumentService(self.store, SymbolRegistry())
        self.cable = CableService(self.store)
        self.recorder = AuditRecorder(store=self.store, symbols=SymbolRegistry())
        self.analyzer = ChangeSetImpactAnalyzer(self.store, self.service, self.cable)
        self.previewer = ChangeSetPreviewer(self.store, self.service, self.cable)
        self.executor = ChangeSetExecutor(
            self.store, self.service, self.cable, self.analyzer, self.previewer
        )
        self.runtime = AgentHarnessRuntime(
            store=self.store,
            registry=ProjectChangeToolRegistry(),
            adapter=ProjectChangeDomainAdapter(self.executor, self.store),
            audit=ProjectChangeAuditAdapter(self.recorder),
        )
        pid = self.service.create_document(CreateDocumentRequest(name="pid", width=800, height=600))
        self.service.apply_transaction(
            pid.id,
            TransactionRequest(
                expected_revision=0,
                label="seed",
                operations=[
                    AddElementOperation(
                        element=SymbolElement(
                            id="PMP-101",
                            symbol_key="agitator",
                            position={"x": 100, "y": 100},
                            width=60,
                            height=40,
                            label="PMP-101",
                        )
                    )
                ],
            ),
        )
        self.pid_id = pid.id
        view = self.cable.create_document("cable")
        updated = view.document.with_segment(
            CableSegment(id="SEG-1", from_node="MCC-1", to_node="PMP-101", gauge="4mm2")
        )
        connection = self.store._connect()  # noqa: SLF001
        try:
            from agentcad.cable_models import serialize_cable_payload

            connection.execute(
                "UPDATE cable_documents SET revision = ?, data_json = ?, updated_at = ? WHERE document_id = ?",
                (updated.revision, serialize_cable_payload(updated), datetime.now(UTC).isoformat(), view.document_id),
            )
            connection.execute(
                "INSERT INTO engineering_links ("
                " link_id, project_id, relation_type, source_domain, source_document_id,"
                " source_object_ref, source_endpoint, target_domain, target_document_id,"
                " target_object_ref, pinned_source_revision, pinned_target_revision,"
                " created_at, created_by"
                ") VALUES ('lnk_seed', ?, 'cable_endpoint_equipment', 'cable', ?, 'SEG-1',"
                " 'from', 'pid', ?, 'PMP-101', ?, ?, ?, 'tester')",
                (
                    DEFAULT_PROJECT,
                    view.document_id,
                    self.pid_id,
                    updated.revision,
                    self.service.get_document(self.pid_id).revision,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        self.cable_id = view.document_id
        self.store.add_document_to_project(DEFAULT_PROJECT, self.pid_id, added_by="tester")
        self.store.add_document_to_project(DEFAULT_PROJECT, self.cable_id, added_by="tester")

    def pins(self) -> dict[str, int]:
        result = {}
        for document_id, domain, _added in self.store.list_project_documents(DEFAULT_PROJECT):
            if domain == "cable":
                result[document_id] = int(self.store.get_cable_envelope(document_id)[0])
            else:
                result[document_id] = self.store.get(document_id).document.revision
        return result

    def intent(self) -> ChangeSetIntent:
        view = self.cable.load(self.cable_id)
        segments = tuple(
            CableSegment(id="SEG-1", from_node="MCC-1", to_node="PMP-101", gauge="6mm2")
            if segment.id == "SEG-1"
            else segment
            for segment in view.document.segments
        )
        cable_doc = view.document.model_copy(update={"segments": segments})
        cable_payload = cable_doc.model_dump(mode="json")
        cable_payload.pop("revision", None)
        return ChangeSetIntent(
            project_id=DEFAULT_PROJECT,
            base_member_pins=self.pins(),
            evaluation_as_of=AS_OF,
            mutations=[
                ChangeSetMutation(
                    domain="pid",
                    document_id=self.pid_id,
                    kind="pid_transaction",
                    payload={
                        "operations": [
                            {
                                "op": "update_element",
                                "element_id": "PMP-101",
                                "patch": {"label": "PMP-101A"},
                            }
                        ]
                    },
                ),
                ChangeSetMutation(
                    domain="cable",
                    document_id=self.cable_id,
                    kind="cable_update",
                    payload=cable_payload,
                ),
            ],
        )


def _stage_approve_authorize(plane: Plane, intent: ChangeSetIntent):
    impact = plane.analyzer.analyze(intent)
    preview = plane.previewer.preview(intent, impact)
    change_set_id = new_change_set_id()
    plane.store.insert_change_set(
        change_set_id=change_set_id,
        project_id=DEFAULT_PROJECT,
        base_pins=json.dumps(intent.base_member_pins, sort_keys=True),
        intent=canonical_change_set_intent(intent),
        intent_hash=change_set_intent_hash(intent),
        impacted=json.dumps(impact.canonical(), sort_keys=True),
        preview=json.dumps(preview.canonical(), sort_keys=True),
        created_by="engineer",
    )
    assert plane.store.update_change_set_status(
        change_set_id=change_set_id, expected_status="staged", new_status="approved"
    )
    payload = {
        "change_set_id": change_set_id,
        "intent": json.loads(canonical_change_set_intent(intent)),
    }
    binding_doc = sorted(m.document_id for m in intent.mutations)[0]
    session = plane.runtime.ensure_session(binding_doc, actor="engineer")
    approval = plane.runtime.request_approval(
        session.id,
        ToolApprovalCreateRequest(
            tool_name=TOOL_APPLY_CHANGE_SET,
            document_id=binding_doc,
            intent=payload,
            requested_by="engineer",
        ),
    )
    plane.runtime.resolve_approval(
        approval.id, ToolApprovalResolveRequest(approved=True, actor="operator")
    )
    authorized = plane.runtime.authorize(
        session_id=session.id,
        tool_name=TOOL_APPLY_CHANGE_SET,
        document_id=binding_doc,
        intent=payload,
        approval_id=approval.id,
        base_revision=None,
    )
    return authorized, change_set_id, payload


def test_atomic_apply_happy_path_and_evidence(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "happy.db")
    intent = plane.intent()
    pins = plane.pins()
    authorized, change_set_id, payload = _stage_approve_authorize(plane, intent)

    outcome = plane.runtime.apply_authorized(authorized, sorted(payload["intent"]["mutations"], key=lambda m: m["document_id"])[0]["document_id"], payload)

    assert outcome.payload["change_set_id"] == change_set_id
    assert plane.pins() == {
        document_id: pin + 1 for document_id, pin in pins.items()
    }
    link = plane.store.get_engineering_link("lnk_seed")
    assert link["pinned_source_revision"] == pins[plane.cable_id] + 1
    assert link["pinned_target_revision"] == pins[plane.pid_id] + 1

    row = plane.store.get_change_set(change_set_id)
    assert row["status"] == "applied"
    evidence = json.loads(row["evidence"])
    assert evidence["before_pins"] == pins
    assert evidence["after_pins"] == outcome.payload["result_pins"]
    assert evidence["approval_id"] == authorized.approval.id

    # exactly one applied governance audit; closeout consumed/completed
    events = plane.recorder.store.all_audit_records()
    applied = [e for e in events if e.event_type == "project_change_set.applied"]
    assert len(applied) == 1
    assert applied[0].session_id and applied[0].approval_id and applied[0].tool_call_id
    assert applied[0].project_id == DEFAULT_PROJECT
    assert plane.recorder.verify_chain().ok

    # frozen hard-lock: in-transaction snapshot hash == post-commit D4 hash
    post = ProjectReadinessService(
        plane.store, plane.service, plane.cable
    ).assess(project_id=DEFAULT_PROJECT, evaluation_as_of=AS_OF)
    assert evidence["readiness_result_hash"] == post.result_hash


def test_c4_approved_row_alone_never_authorizes(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "c4.db")
    intent = plane.intent()
    impact = plane.analyzer.analyze(intent)
    change_set_id = new_change_set_id()
    plane.store.insert_change_set(
        change_set_id=change_set_id,
        project_id=DEFAULT_PROJECT,
        base_pins=json.dumps(intent.base_member_pins, sort_keys=True),
        intent=canonical_change_set_intent(intent),
        intent_hash=change_set_intent_hash(intent),
        impacted=json.dumps(impact.canonical(), sort_keys=True),
        created_by="engineer",
    )
    # row is only staged — an approved-looking request must fail C4
    assert plane.store.update_change_set_status(
        change_set_id=change_set_id, expected_status="staged", new_status="approved"
    )
    tampered = ChangeSetIntent(
        project_id=DEFAULT_PROJECT,
        base_member_pins=intent.base_member_pins,
        evaluation_as_of=AS_OF,
        mutations=list(reversed(intent.mutations)),
    )
    payload = {
        "change_set_id": change_set_id,
        "intent": json.loads(canonical_change_set_intent(tampered)),
    }
    binding_doc = sorted(m.document_id for m in intent.mutations)[0]
    session = plane.runtime.ensure_session(binding_doc, actor="engineer")
    approval = plane.runtime.request_approval(
        session.id,
        ToolApprovalCreateRequest(
            tool_name=TOOL_APPLY_CHANGE_SET,
            document_id=binding_doc,
            intent=payload,
            requested_by="engineer",
        ),
    )
    plane.runtime.resolve_approval(
        approval.id, ToolApprovalResolveRequest(approved=True, actor="operator")
    )
    authorized = plane.runtime.authorize(
        session_id=session.id,
        tool_name=TOOL_APPLY_CHANGE_SET,
        document_id=binding_doc,
        intent=payload,
        approval_id=approval.id,
        base_revision=None,
    )
    with pytest.raises(ChangeSetError) as exc_info:
        plane.runtime.apply_authorized(authorized, binding_doc, payload)
    assert exc_info.value.code == "change_set_intent_mismatch"
    assert plane.pins() == intent.base_member_pins  # zero engineering writes
    row = plane.store.get_change_set(change_set_id)
    assert row["status"] == "refused"  # closeout transitioned approved -> refused


def test_conflict_after_approval_refuses_with_zero_writes(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "conflict.db")
    intent = plane.intent()
    pins = plane.pins()
    authorized, change_set_id, payload = _stage_approve_authorize(plane, intent)

    # state drifts after approval: cable revision advances
    connection = plane.store._connect()  # noqa: SLF001
    try:
        connection.execute(
            "UPDATE cable_documents SET revision = ? WHERE document_id = ?",
            (pins[plane.cable_id] + 1, plane.cable_id),
        )
        connection.commit()
    finally:
        connection.close()

    binding_doc = sorted(payload["intent"]["mutations"], key=lambda m: m["document_id"])[0]["document_id"]
    with pytest.raises(ChangeSetError) as exc_info:
        plane.runtime.apply_authorized(authorized, binding_doc, payload)
    assert exc_info.value.code == "change_set_base_not_current"

    # zero writes: pid untouched, link pins untouched
    assert plane.service.get_document(plane.pid_id).revision == pins[plane.pid_id]
    link = plane.store.get_engineering_link("lnk_seed")
    assert link["pinned_source_revision"] == pins[plane.cable_id]
    assert plane.store.get_change_set(change_set_id)["status"] == "refused"

    events = plane.recorder.store.all_audit_records()
    rejected = [e for e in events if e.event_type == "project_change_set.rejected"]
    assert len(rejected) == 1
    assert plane.recorder.verify_chain().ok


def test_partial_commit_failure_rolls_back_everything(tmp_path: Path, monkeypatch) -> None:
    plane = Plane(tmp_path, "partial.db")
    intent = plane.intent()
    pins = plane.pins()
    authorized, change_set_id, payload = _stage_approve_authorize(plane, intent)

    original = plane.store._write_tool_call  # noqa: SLF001
    calls = {"n": 0}

    def flaky(connection, record):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected mid-commit failure")
        return original(connection, record)

    monkeypatch.setattr(plane.store, "_write_tool_call", flaky)
    binding_doc = sorted(payload["intent"]["mutations"], key=lambda m: m["document_id"])[0]["document_id"]
    with pytest.raises(RuntimeError, match="injected"):
        plane.runtime.apply_authorized(authorized, binding_doc, payload)

    # no half-project state anywhere
    assert plane.pins() == pins
    link = plane.store.get_engineering_link("lnk_seed")
    assert link["pinned_source_revision"] == pins[plane.cable_id]
    assert plane.store.get_change_set(change_set_id)["status"] == "refused"
    events = plane.recorder.store.all_audit_records()
    rejected = [e for e in events if e.event_type == "project_change_set.rejected"]
    assert len(rejected) == 1
    assert plane.recorder.verify_chain().ok


def test_approval_replay_refused(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "replay.db")
    intent = plane.intent()
    authorized, change_set_id, payload = _stage_approve_authorize(plane, intent)
    binding_doc = sorted(payload["intent"]["mutations"], key=lambda m: m["document_id"])[0]["document_id"]
    plane.runtime.apply_authorized(authorized, binding_doc, payload)

    events_before = len(plane.recorder.store.all_audit_records())
    with pytest.raises(ChangeSetError) as exc_info:
        plane.runtime.apply_authorized(authorized, binding_doc, payload)
    assert exc_info.value.code == "change_set_not_authorized"

    # D85-5: replay must NOT overwrite the terminal success records.
    tool_call = plane.store.get_tool_call(authorized.record.id)
    assert tool_call.status == "completed"
    session = plane.store.get_agent_session(authorized.session.id)
    assert session.status == "completed"
    approval = plane.store.get_tool_approval(authorized.approval.id)
    assert approval.status == "consumed"
    events_after = plane.recorder.store.all_audit_records()
    assert len(events_after) == events_before  # no new refusal audit either
    applied = [e for e in events_after if e.event_type == "project_change_set.applied"]
    assert len(applied) == 1


def test_undo_exactly_reverts_change_set_pid_revision(tmp_path: Path) -> None:
    """D85-2 hard-lock: the change-set P&ID write maintains undo/redo +
    history, so a normal undo() reverts exactly the change-set revision."""
    plane = Plane(tmp_path, "undo.db")
    intent = plane.intent()
    base_revision = plane.service.get_document(plane.pid_id).revision
    authorized, change_set_id, payload = _stage_approve_authorize(plane, intent)
    binding_doc = sorted(payload["intent"]["mutations"], key=lambda m: m["document_id"])[0]["document_id"]
    plane.runtime.apply_authorized(authorized, binding_doc, payload)

    assert plane.service.get_document(plane.pid_id).revision == base_revision + 1
    undone = plane.service.undo(plane.pid_id)
    # undo() itself lands a new revision whose CONTENT is exactly the base
    # revision content — the change-set revision is reverted, not skipped.
    assert undone.revision == base_revision + 2
    element = next(e for e in undone.elements if e.id == "PMP-101")
    assert element.label == "PMP-101"  # change-set label change reverted
    history = plane.service.get_history(plane.pid_id, limit=10)
    revisions = [entry.revision for entry in history]
    assert base_revision + 1 in revisions  # no history hole


def test_in_transaction_repin_race_rolls_back(tmp_path: Path, monkeypatch) -> None:
    """D85-1 hard-lock: if the in-transaction endpoint facts disagree with
    the approval-bound pins, the whole change set rolls back."""
    plane = Plane(tmp_path, "repin_race.db")
    intent = plane.intent()
    pins = plane.pins()
    authorized, change_set_id, payload = _stage_approve_authorize(plane, intent)

    original = plane.store._link_endpoint_facts  # noqa: SLF001
    calls = {"n": 0}

    def flaky(connection, **kwargs):
        calls["n"] += 1
        result = original(connection, **kwargs)
        if "expected" in kwargs and calls["n"] >= 1:
            # in-transaction re-validation sees different current revisions
            return (result[0] + 9, result[1] + 9)
        return result

    monkeypatch.setattr(plane.store, "_link_endpoint_facts", flaky)
    binding_doc = sorted(payload["intent"]["mutations"], key=lambda m: m["document_id"])[0]["document_id"]
    from agentcad.store import StoreRevisionConflictError

    with pytest.raises(StoreRevisionConflictError):
        plane.runtime.apply_authorized(authorized, binding_doc, payload)

    assert plane.pins() == pins
    assert plane.store.get_change_set(change_set_id)["status"] == "refused"
    assert plane.recorder.verify_chain().ok


def test_result_pins_cover_unmutated_members(tmp_path: Path) -> None:
    """D85-4 hard-lock: after_pins / result_pins are full project pins —
    unmutated members keep their declared base pin."""
    plane = Plane(tmp_path, "fullpins.db")
    other = plane.service.create_document(CreateDocumentRequest(name="pid2", width=800, height=600))
    plane.store.add_document_to_project(DEFAULT_PROJECT, other.id, added_by="tester")
    intent = plane.intent()  # mutates pid + cable only
    declared = plane.pins()
    authorized, change_set_id, payload = _stage_approve_authorize(plane, intent)
    binding_doc = sorted(payload["intent"]["mutations"], key=lambda m: m["document_id"])[0]["document_id"]
    outcome = plane.runtime.apply_authorized(authorized, binding_doc, payload)

    assert outcome.payload["result_pins"][other.id] == declared[other.id]
    assert outcome.payload["result_pins"][plane.pid_id] == declared[plane.pid_id] + 1
    row = plane.store.get_change_set(change_set_id)
    stored_pins = json.loads(row["result_pins"])
    assert stored_pins == outcome.payload["result_pins"]
    # stored pins equal the current project pins -> M12 package-ready
    assert stored_pins == plane.pins()
