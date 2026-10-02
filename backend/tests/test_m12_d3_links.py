"""M12-D3 hard locks: cross-domain engineering links through the M10 runtime.

Gate-frozen scope (M12-D3 CODE GO + D79-1/D79-2/D79-3):
- the only link write paths are AgentHarnessRuntime-driven: neutral tool
  definitions (permission=ask, risk=engineering_change), session / approval /
  authorization bound, mutation + governance audit + tool-call closure +
  approval consumption + session closure in ONE transaction;
- every frozen invariant is re-checked inside the BEGIN IMMEDIATE write
  transaction (TOCTOU fail-closed), including membership;
- equipment predicate: target element exists in the current P&ID revision and
  its symbol category is not the instrument category (canonical constant
  imported from the M7 contract under a declared allow-list entry).

Cable envelopes are SQL-seeded at a pinned revision (fixture pattern as in
the M11-D4 e2e); the governed cable write path itself is M11-D2-tested.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentcad.audit import AuditRecorder
from agentcad.cable_service import CableService
from agentcad.engineering_links import (
    EngineeringLinkError,
    EngineeringLinkService,
)
from agentcad.harness_models import (
    ToolApprovalCreateRequest,
    ToolApprovalResolveRequest,
)
from agentcad.m7_layout_contract import INSTRUMENT_SYMBOL_CATEGORY
from agentcad.models import (
    AddElementOperation,
    CreateDocumentRequest,
    SymbolElement,
    TransactionRequest,
)
from agentcad.project_link_runtime import (
    TOOL_CREATE_LINK,
    TOOL_DELETE_LINK,
    TOOL_REPIN_LINK,
    ProjectLinkAuditAdapter,
    ProjectLinkDomainAdapter,
    ProjectLinkToolRegistry,
)
from agentcad.runtime.harness import AgentHarnessRuntime
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry

DEFAULT_PROJECT = "proj_m12default"


class Plane:
    def __init__(self, tmp_path: Path, name: str) -> None:
        self.store = SQLiteDocumentStore(tmp_path / name)
        self.service = DocumentService(self.store, SymbolRegistry())
        self.cable = CableService(self.store)
        self.links = EngineeringLinkService(self.store, self.service, self.cable)
        self.recorder = AuditRecorder(store=self.store, symbols=SymbolRegistry())
        self.runtime = AgentHarnessRuntime(
            store=self.store,
            registry=ProjectLinkToolRegistry(),
            adapter=ProjectLinkDomainAdapter(self.links),
            audit=ProjectLinkAuditAdapter(self.recorder),
        )


def _seed_cable_envelope(
    plane: Plane, cable_id: str, segment_id: str = "SEG-1", revision: int = 1
) -> str:
    now = datetime.now(UTC).isoformat()
    payload = json.dumps(
        {
            "schema": "pid-agent.cable-document/1",
            "name": "cable",
            "segments": [
                {"id": segment_id, "from_node": "MCC-1", "to_node": "PMP-101"}
            ],
        }
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


def _bump_cable_revision(plane: Plane, cable_id: str, revision: int) -> None:
    with plane.store._connect() as connection:  # noqa: SLF001 - test fixture
        connection.execute(
            "UPDATE cable_documents SET revision = ?, updated_at = ? WHERE document_id = ?",
            (revision, datetime.now(UTC).isoformat(), cable_id),
        )
        connection.commit()


def _seed_pid(plane: Plane, element_id: str, symbol_key: str) -> str:
    document = plane.service.create_document(
        CreateDocumentRequest(name="pid", width=800, height=600)
    )
    element = SymbolElement(
        id=element_id,
        symbol_key=symbol_key,
        position={"x": 100, "y": 100},
        width=60,
        height=40,
        label=element_id,
    )
    plane.service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=0,
            label="seed element",
            operations=[AddElementOperation(element=element)],
        ),
    )
    plane.store.add_document_to_project(DEFAULT_PROJECT, document.id, added_by="tester")
    return document.id


def _governed(plane: Plane, tool_name: str, binding_document_id: str, intent: dict, base_revision: int):
    """Full M10 runtime flow: session -> approval -> authorization -> execution."""
    session = plane.runtime.ensure_session(binding_document_id, actor="engineer")
    approval = plane.runtime.request_approval(
        session.id,
        ToolApprovalCreateRequest(
            tool_name=tool_name,
            document_id=binding_document_id,
            intent=intent,
            requested_by="engineer",
        ),
    )
    plane.runtime.resolve_approval(
        approval.id, ToolApprovalResolveRequest(approved=True, actor="operator")
    )
    authorized = plane.runtime.authorize(
        session_id=session.id,
        tool_name=tool_name,
        document_id=binding_document_id,
        intent=intent,
        approval_id=approval.id,
        base_revision=base_revision,
    )
    return plane.runtime.apply_authorized(authorized, binding_document_id, intent)


def _create_intent(cable_id: str, pid_id: str, segment: str = "SEG-1", endpoint: str = "from", target: str = "PMP-101") -> dict:
    return {
        "project_id": DEFAULT_PROJECT,
        "source_document_id": cable_id,
        "source_object_ref": segment,
        "source_endpoint": endpoint,
        "target_document_id": pid_id,
        "target_object_ref": target,
    }


def _make_linked(tmp_path: Path, name: str = "ok.db"):
    plane = Plane(tmp_path, name)
    cable_id = _seed_cable_envelope(plane, "cab_test1")
    pid_id = _seed_pid(plane, "PMP-101", "agitator")
    outcome = _governed(plane, TOOL_CREATE_LINK, cable_id, _create_intent(cable_id, pid_id), 1)
    return plane, cable_id, pid_id, outcome.payload["link_id"]


def _rejected_events(plane: Plane) -> list[str]:
    return [
        record.error_code
        for record in plane.recorder.store.all_audit_records()
        if record.event_type == "engineering_link.rejected"
    ]


def test_create_link_via_runtime_pins_current_and_closes_harness(tmp_path: Path) -> None:
    plane, cable_id, pid_id, link_id = _make_linked(tmp_path)
    view = plane.links.get_link(link_id)
    assert view is not None
    assert (view.pinned_source_revision, view.pinned_target_revision) == (1, 1)
    assert [link.link_id for link in plane.links.list_active_links(DEFAULT_PROJECT)] == [link_id]
    events = plane.recorder.store.all_audit_records()
    created = [e for e in events if e.event_type == "engineering_link.created"]
    assert len(created) == 1
    assert created[0].session_id and created[0].approval_id and created[0].tool_call_id
    assert created[0].project_id == DEFAULT_PROJECT
    assert plane.recorder.verify_chain().ok


@pytest.mark.parametrize(
    ("intent_patch", "code"),
    [
        ({"source_endpoint": "middle"}, "invalid_endpoint"),
        ({"source_object_ref": "NOPE"}, "missing_source_object"),
        ({"target_object_ref": "NOPE"}, "missing_target_object"),
    ],
)
def test_create_invariants_fail_closed_via_runtime(tmp_path: Path, intent_patch, code) -> None:
    plane = Plane(tmp_path, f"inv_{code}.db")
    cable_id = _seed_cable_envelope(plane, "cab_inv")
    pid_id = _seed_pid(plane, "PMP-101", "agitator")
    intent = _create_intent(cable_id, pid_id)
    intent.update(intent_patch)
    with pytest.raises(EngineeringLinkError) as exc_info:
        _governed(plane, TOOL_CREATE_LINK, cable_id, intent, 1)
    assert exc_info.value.code == code
    assert plane.links.list_active_links(DEFAULT_PROJECT) == []
    assert code in _rejected_events(plane)
    assert plane.recorder.verify_chain().ok


def test_relation_orientation_fail_closed(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "orient.db")
    cable_id = _seed_cable_envelope(plane, "cab_orient")
    pid_id = _seed_pid(plane, "PMP-101", "agitator")
    intent = {
        "project_id": DEFAULT_PROJECT,
        "source_document_id": pid_id,  # pid as source: forbidden
        "source_object_ref": "PMP-101",
        "source_endpoint": "from",
        "target_document_id": cable_id,
        "target_object_ref": "SEG-1",
    }
    with pytest.raises(EngineeringLinkError) as exc_info:
        _governed(plane, TOOL_CREATE_LINK, cable_id, intent, 1)
    assert exc_info.value.code == "relation_orientation"
    assert "relation_orientation" in _rejected_events(plane)


def test_cross_project_membership_fail_closed(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "members.db")
    cable_id = _seed_cable_envelope(plane, "cab_members")
    other_pid = plane.service.create_document(CreateDocumentRequest(name="other", width=10, height=10))
    intent = _create_intent(cable_id, other_pid.id, target="EL-1")
    with pytest.raises(EngineeringLinkError) as exc_info:
        _governed(plane, TOOL_CREATE_LINK, cable_id, intent, 1)
    assert exc_info.value.code == "target_not_in_project"
    assert "target_not_in_project" in _rejected_events(plane)


def test_membership_removal_between_authorize_and_apply_fails_closed(tmp_path: Path) -> None:
    """D79-2: the write transaction re-checks membership; a removal that lands
    after authorization cannot slip a link in."""
    plane = Plane(tmp_path, "race.db")
    cable_id = _seed_cable_envelope(plane, "cab_race")
    pid_id = _seed_pid(plane, "PMP-101", "agitator")
    intent = _create_intent(cable_id, pid_id)
    session = plane.runtime.ensure_session(cable_id, actor="engineer")
    approval = plane.runtime.request_approval(
        session.id,
        ToolApprovalCreateRequest(
            tool_name=TOOL_CREATE_LINK,
            document_id=cable_id,
            intent=intent,
            requested_by="engineer",
        ),
    )
    plane.runtime.resolve_approval(
        approval.id, ToolApprovalResolveRequest(approved=True, actor="operator")
    )
    authorized = plane.runtime.authorize(
        session_id=session.id,
        tool_name=TOOL_CREATE_LINK,
        document_id=cable_id,
        intent=intent,
        approval_id=approval.id,
        base_revision=1,
    )
    plane.store.remove_document_from_project(DEFAULT_PROJECT, pid_id)
    with pytest.raises(EngineeringLinkError) as exc_info:
        plane.runtime.apply_authorized(authorized, cable_id, intent)
    assert exc_info.value.code == "target_not_in_project"
    assert plane.links.list_active_links(DEFAULT_PROJECT) == []
    assert "target_not_in_project" in _rejected_events(plane)
    assert plane.recorder.verify_chain().ok


def test_equipment_predicate_rejects_instrument_fail_closed(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "instr.db")
    cable_id = _seed_cable_envelope(plane, "cab_instr")
    pid_id = _seed_pid(plane, "AI-1001", "analyzer_indicator")
    with pytest.raises(EngineeringLinkError) as exc_info:
        _governed(plane, TOOL_CREATE_LINK, cable_id, _create_intent(cable_id, pid_id, target="AI-1001"), 1)
    assert exc_info.value.code == "target_not_equipment"
    assert plane.links.list_active_links(DEFAULT_PROJECT) == []
    assert "target_not_equipment" in _rejected_events(plane)


def test_active_endpoint_uniqueness_fail_closed(tmp_path: Path) -> None:
    plane, cable_id, _pid_id, first = _make_linked(tmp_path, "unique.db")
    pid_two = _seed_pid(plane, "PMP-102", "agitator")
    with pytest.raises(EngineeringLinkError) as exc_info:
        _governed(
            plane,
            TOOL_CREATE_LINK,
            cable_id,
            _create_intent(cable_id, pid_two, target="PMP-102"),
            1,
        )
    assert exc_info.value.code == "endpoint_already_connected"
    assert "endpoint_already_connected" in _rejected_events(plane)
    # the other endpoint of the same segment remains free
    outcome = _governed(
        plane,
        TOOL_CREATE_LINK,
        cable_id,
        _create_intent(cable_id, pid_two, endpoint="to", target="PMP-102"),
        1,
    )
    assert outcome.payload["link_id"] != first


def test_repin_via_runtime_advances_pins_and_closes_harness(tmp_path: Path) -> None:
    plane, cable_id, _pid_id, link_id = _make_linked(tmp_path, "repin.db")
    _bump_cable_revision(plane, cable_id, 2)
    outcome = _governed(plane, TOOL_REPIN_LINK, cable_id, {"link_id": link_id}, 2)
    assert (outcome.payload["pinned_source_revision"], outcome.payload["pinned_target_revision"]) == (2, 1)
    events = plane.recorder.store.all_audit_records()
    assert len([e for e in events if e.event_type == "engineering_link.repinned"]) == 1
    assert plane.recorder.verify_chain().ok


def test_soft_delete_via_runtime_and_double_delete_fail_closed(tmp_path: Path) -> None:
    plane, cable_id, _pid_id, link_id = _make_linked(tmp_path, "delete.db")
    outcome = _governed(plane, TOOL_DELETE_LINK, cable_id, {"link_id": link_id}, 1)
    assert outcome.payload["deleted"] is True
    view = plane.links.get_link(link_id)
    assert view is not None and view.deleted is True
    assert plane.links.list_active_links(DEFAULT_PROJECT) == []
    with pytest.raises(EngineeringLinkError) as exc_info:
        _governed(plane, TOOL_DELETE_LINK, cable_id, {"link_id": link_id}, 1)
    assert exc_info.value.code == "link_already_deleted"
    events = plane.recorder.store.all_audit_records()
    assert len([e for e in events if e.event_type == "engineering_link.deleted"]) == 1
    assert plane.recorder.verify_chain().ok


def test_link_mutations_grow_audit_chain_with_bound_provenance(tmp_path: Path) -> None:
    plane, cable_id, _pid_id, link_id = _make_linked(tmp_path, "audit.db")
    _bump_cable_revision(plane, cable_id, 2)
    _governed(plane, TOOL_REPIN_LINK, cable_id, {"link_id": link_id}, 2)
    _governed(plane, TOOL_DELETE_LINK, cable_id, {"link_id": link_id}, 2)
    assert plane.recorder.verify_chain().ok
    events = plane.recorder.store.all_audit_records()
    for event_type, count in (
        ("engineering_link.created", 1),
        ("engineering_link.repinned", 1),
        ("engineering_link.deleted", 1),
    ):
        matched = [e for e in events if e.event_type == event_type]
        assert len(matched) == count
        assert all(e.session_id and e.approval_id and e.tool_call_id for e in matched)


def test_instrument_keyset_matches_contract_category(tmp_path: Path) -> None:
    """The service's instrument predicate keyset is exactly the catalogue keys
    whose category is the canonical instrument category (D79-3)."""
    plane = Plane(tmp_path, "keyset.db")
    symbols = plane.service.symbols._symbols  # noqa: SLF001 - test assertion
    expected = frozenset(
        key for key, symbol in symbols.items() if symbol.category == INSTRUMENT_SYMBOL_CATEGORY
    )
    assert plane.links._instrument_symbol_keys == expected  # noqa: SLF001
    assert "analyzer_indicator" in expected
