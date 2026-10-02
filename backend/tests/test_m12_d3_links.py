"""M12-D3 hard locks: cross-domain engineering links + governed mutation.

Gate-frozen scope (M12-D3 CODE GO):
- create / re-pin / soft-delete with fail-closed relation invariants
  (R77-Q2: orientation, endpoint, project membership, pins==current,
  one-active-link-per-endpoint via partial unique index);
- equipment predicate fail-closed at write time: target element exists in the
  current P&ID revision and its symbol category is not the instrument
  category (INSTRUMENT_SYMBOL_CATEGORY);
- governance audit on the existing global audit hash chain (new event types,
  no hash-formation / ordinal change); soft-deleted links are not active.

Cable envelopes are SQL-seeded at a pinned revision (same fixture pattern as
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
from agentcad.models import (
    AddElementOperation,
    CreateDocumentRequest,
    SymbolElement,
    TransactionRequest,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry

DEFAULT_PROJECT = "proj_m12default"


def _services(tmp_path: Path, name: str):
    service = DocumentService(SQLiteDocumentStore(tmp_path / name), SymbolRegistry())
    cable = CableService(service.store)
    links = EngineeringLinkService(service.store, service, cable)
    return service, cable, links


def _seed_cable_envelope(
    service: DocumentService, cable_id: str, segment_id: str = "SEG-1", revision: int = 1
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
    with service.store._connect() as connection:  # noqa: SLF001 - test fixture
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
    service.store.add_document_to_project(DEFAULT_PROJECT, cable_id, added_by="tester")
    return cable_id


def _bump_cable_revision(service: DocumentService, cable_id: str, revision: int) -> None:
    with service.store._connect() as connection:  # noqa: SLF001 - test fixture
        connection.execute(
            "UPDATE cable_documents SET revision = ?, updated_at = ? WHERE document_id = ?",
            (revision, datetime.now(UTC).isoformat(), cable_id),
        )
        connection.commit()


def _seed_pid(service: DocumentService, element_id: str, symbol_key: str) -> str:
    document = service.create_document(CreateDocumentRequest(name="pid", width=800, height=600))
    element = SymbolElement(
        id=element_id,
        symbol_key=symbol_key,
        position={"x": 100, "y": 100},
        width=60,
        height=40,
        label=element_id,
    )
    service.apply_transaction(
        document.id,
        TransactionRequest(
            expected_revision=0,
            label="seed element",
            operations=[AddElementOperation(element=element)],
        ),
    )
    service.store.add_document_to_project(DEFAULT_PROJECT, document.id, added_by="tester")
    return document.id


def _make_linked(tmp_path: Path, name: str = "ok.db"):
    service, cable, links = _services(tmp_path, name)
    cable_id = _seed_cable_envelope(service, "cab_test1")
    pid_id = _seed_pid(service, "PMP-101", "agitator")
    view = links.create_link(
        project_id=DEFAULT_PROJECT,
        source_document_id=cable_id,
        source_object_ref="SEG-1",
        source_endpoint="from",
        target_document_id=pid_id,
        target_object_ref="PMP-101",
        actor="engineer",
    )
    return service, cable, links, cable_id, pid_id, view


def test_create_link_happy_path_pins_current_revisions(tmp_path: Path) -> None:
    _service, cable, links, cable_id, _pid_id, view = _make_linked(tmp_path)
    assert view.pinned_source_revision == 1  # seeded cable envelope revision
    assert view.pinned_target_revision == 1  # one transaction applied
    assert view.relation_type == "cable_endpoint_equipment"
    assert [link.link_id for link in links.list_active_links(DEFAULT_PROJECT)] == [view.link_id]
    assert view.pinned_source_revision == cable.load(cable_id).document.revision


def test_relation_orientation_fail_closed(tmp_path: Path) -> None:
    service, cable, links = _services(tmp_path, "orient.db")
    cable_id = _seed_cable_envelope(service, "cab_orient")
    pid_id = _seed_pid(service, "PMP-101", "agitator")
    with pytest.raises(EngineeringLinkError, match="relation_orientation"):
        links.create_link(
            project_id=DEFAULT_PROJECT,
            source_document_id=pid_id,  # pid as source: forbidden
            source_object_ref="PMP-101",
            source_endpoint="from",
            target_document_id=cable_id,
            target_object_ref="SEG-1",
            actor="engineer",
        )


def test_invalid_endpoint_fail_closed(tmp_path: Path) -> None:
    service, cable, links = _services(tmp_path, "endpoint.db")
    cable_id = _seed_cable_envelope(service, "cab_endpoint")
    pid_id = _seed_pid(service, "PMP-101", "agitator")
    with pytest.raises(EngineeringLinkError, match="invalid_endpoint"):
        links.create_link(
            project_id=DEFAULT_PROJECT,
            source_document_id=cable_id,
            source_object_ref="SEG-1",
            source_endpoint="middle",
            target_document_id=pid_id,
            target_object_ref="PMP-101",
            actor="engineer",
        )


def test_cross_project_membership_fail_closed(tmp_path: Path) -> None:
    service, cable, links = _services(tmp_path, "members.db")
    cable_id = _seed_cable_envelope(service, "cab_members")
    other_pid = service.create_document(CreateDocumentRequest(name="other", width=10, height=10))
    with pytest.raises(EngineeringLinkError, match="not a member of project"):
        links.create_link(
            project_id=DEFAULT_PROJECT,
            source_document_id=cable_id,
            source_object_ref="SEG-1",
            source_endpoint="from",
            target_document_id=other_pid.id,
            target_object_ref="EL-1",
            actor="engineer",
        )


def test_missing_segment_and_element_fail_closed(tmp_path: Path) -> None:
    service, cable, links = _services(tmp_path, "missing.db")
    cable_id = _seed_cable_envelope(service, "cab_missing")
    pid_id = _seed_pid(service, "PMP-101", "agitator")
    with pytest.raises(EngineeringLinkError, match="missing_source_object"):
        links.create_link(
            project_id=DEFAULT_PROJECT,
            source_document_id=cable_id,
            source_object_ref="NOPE",
            source_endpoint="from",
            target_document_id=pid_id,
            target_object_ref="PMP-101",
            actor="engineer",
        )
    with pytest.raises(EngineeringLinkError, match="missing_target_object"):
        links.create_link(
            project_id=DEFAULT_PROJECT,
            source_document_id=cable_id,
            source_object_ref="SEG-1",
            source_endpoint="from",
            target_document_id=pid_id,
            target_object_ref="NOPE",
            actor="engineer",
        )


def test_equipment_predicate_rejects_instrument_fail_closed(tmp_path: Path) -> None:
    service, cable, links = _services(tmp_path, "instr.db")
    cable_id = _seed_cable_envelope(service, "cab_instr")
    pid_id = _seed_pid(service, "AI-1001", "analyzer_indicator")
    with pytest.raises(EngineeringLinkError, match="not equipment"):
        links.create_link(
            project_id=DEFAULT_PROJECT,
            source_document_id=cable_id,
            source_object_ref="SEG-1",
            source_endpoint="from",
            target_document_id=pid_id,
            target_object_ref="AI-1001",
            actor="engineer",
        )
    assert links.list_active_links(DEFAULT_PROJECT) == []


def test_active_endpoint_uniqueness_fail_closed(tmp_path: Path) -> None:
    service, _cable, links, cable_id, _pid_id, view = _make_linked(tmp_path, "unique.db")
    pid_two = _seed_pid(service, "PMP-102", "agitator")
    with pytest.raises(EngineeringLinkError, match="endpoint_already_connected"):
        links.create_link(
            project_id=DEFAULT_PROJECT,
            source_document_id=cable_id,
            source_object_ref="SEG-1",
            source_endpoint="from",  # same endpoint, even against another device
            target_document_id=pid_two,
            target_object_ref="PMP-102",
            actor="engineer",
        )
    # the other endpoint of the same segment is free
    other = links.create_link(
        project_id=DEFAULT_PROJECT,
        source_document_id=cable_id,
        source_object_ref="SEG-1",
        source_endpoint="to",
        target_document_id=pid_two,
        target_object_ref="PMP-102",
        actor="engineer",
    )
    assert other.link_id != view.link_id


def test_repin_advances_pins_to_current(tmp_path: Path) -> None:
    service, _cable, links, cable_id, _pid_id, view = _make_linked(tmp_path, "repin.db")
    _bump_cable_revision(service, cable_id, 2)
    refreshed = links.repin_link(link_id=view.link_id, actor="engineer")
    assert refreshed.pinned_source_revision == 2
    assert refreshed.pinned_target_revision == 1


def test_soft_delete_and_double_delete_fail_closed(tmp_path: Path) -> None:
    _service, _cable, links, _cable_id, _pid_id, view = _make_linked(tmp_path, "delete.db")
    links.soft_delete_link(link_id=view.link_id, actor="engineer")
    assert links.get_link(view.link_id).deleted is True  # type: ignore[union-attr]
    assert links.list_active_links(DEFAULT_PROJECT) == []
    with pytest.raises(EngineeringLinkError, match="already deleted"):
        links.soft_delete_link(link_id=view.link_id, actor="engineer")
    with pytest.raises(EngineeringLinkError, match="already deleted"):
        links.repin_link(link_id=view.link_id, actor="engineer")


def test_link_mutations_grow_audit_chain_and_verify(tmp_path: Path) -> None:
    service, _cable, links, _cable_id, _pid_id, view = _make_linked(tmp_path, "audit.db")
    links.repin_link(link_id=view.link_id, actor="engineer")
    links.soft_delete_link(link_id=view.link_id, actor="engineer")
    recorder = AuditRecorder(store=service.store, symbols=service.symbols)
    assert recorder.verify_chain().ok
    events = [record.event_type for record in recorder.store.all_audit_records()]
    assert events.count("engineering_link.created") == 1
    assert events.count("engineering_link.repinned") == 1
    assert events.count("engineering_link.deleted") == 1


def test_equipment_predicate_constant_pinned_to_layout_contract(tmp_path: Path) -> None:
    """The local instrument-category binding must never drift from the
    canonical M7 contract constant (engineering_links may not import the
    contract itself — phase import discipline)."""
    from agentcad.engineering_links import EQUIPMENT_PREDICATE_INSTRUMENT_CATEGORY
    from agentcad.m7_layout_contract import INSTRUMENT_SYMBOL_CATEGORY

    assert EQUIPMENT_PREDICATE_INSTRUMENT_CATEGORY == INSTRUMENT_SYMBOL_CATEGORY
