"""M13-D3 hard locks: change-set contract, C1/C2, deterministic impact,
zero-write shadow preview, readiness pure core.

Gate-frozen hard-locks covered: declared pins never silently replaced (C1);
payload carries no revision truth (C2); impact fail-closed with stable codes;
existing-active-link re-pin derivation only; linked-object invalidation
fail-closed; preview leaves engineering state byte-identical (not just audit
counts); snapshot readiness determinism / evaluation_as_of parity; controlled
impact/preview snapshot persistence.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentcad.audit import AuditRecorder
from agentcad.cable_models import CableSegment
from agentcad.cable_service import CableService
from agentcad.models import (
    AddElementOperation,
    CreateDocumentRequest,
    DeleteElementOperation,
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
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry

DEFAULT_PROJECT = "proj_m12default"
AS_OF = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
AS_OF_LATER = datetime(2026, 10, 5, 13, 0, tzinfo=UTC)


class Plane:
    def __init__(self, tmp_path: Path, name: str) -> None:
        self.store = SQLiteDocumentStore(tmp_path / name)
        self.service = DocumentService(self.store, SymbolRegistry())
        self.cable = CableService(self.store)
        self.recorder = AuditRecorder(store=self.store, symbols=SymbolRegistry())
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
                (
                    updated.revision,
                    serialize_cable_payload(updated),
                    datetime.now(UTC).isoformat(),
                    view.document_id,
                ),
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
        pins: dict[str, int] = {}
        for document_id, domain, _added in self.store.list_project_documents(
            DEFAULT_PROJECT
        ):
            if domain == "cable":
                pins[document_id] = int(self.store.get_cable_envelope(document_id)[0])
            else:
                pins[document_id] = self.store.get(document_id).document.revision
        return pins

    def cable_payload(self, gauge: str = "6mm2") -> dict:
        view = self.cable.load(self.cable_id)
        segments = tuple(
            CableSegment(id="SEG-1", from_node="MCC-1", to_node="PMP-101", gauge=gauge)
            if segment.id == "SEG-1"
            else segment
            for segment in view.document.segments
        )
        document = view.document.model_copy(update={"segments": segments})
        payload = document.model_dump(mode="json")
        payload.pop("revision", None)
        return payload


def _mutate_both_intent(plane: Plane, *, as_of: datetime = AS_OF) -> ChangeSetIntent:
    return ChangeSetIntent(
        project_id=DEFAULT_PROJECT,
        base_member_pins=plane.pins(),
        evaluation_as_of=as_of,
        mutations=[
            ChangeSetMutation(
                domain="pid",
                document_id=plane.pid_id,
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
                document_id=plane.cable_id,
                kind="cable_update",
                payload=plane.cable_payload(),
            ),
        ],
    )


def test_contract_hash_determinism_and_as_of_requirement(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "contract.db")
    first = _mutate_both_intent(plane)
    second = _mutate_both_intent(plane)
    assert canonical_change_set_intent(first) == canonical_change_set_intent(second)
    assert change_set_intent_hash(first) == change_set_intent_hash(second)
    assert change_set_intent_hash(first) != change_set_intent_hash(
        _mutate_both_intent(plane, as_of=AS_OF_LATER)
    )

    with pytest.raises(ValueError):
        ChangeSetIntent(
            project_id=DEFAULT_PROJECT,
            base_member_pins=plane.pins(),
            evaluation_as_of=datetime(2026, 10, 5, 12, 0),
            mutations=second.mutations,
        )
    with pytest.raises(ValueError):
        ChangeSetIntent(
            project_id=DEFAULT_PROJECT,
            base_member_pins=plane.pins(),
            evaluation_as_of=AS_OF,
            mutations=[second.mutations[0], second.mutations[0]],
        )
    with pytest.raises(ValueError):
        ChangeSetMutation(domain="pid", document_id="x", kind="nonsense", payload={})


def test_c1_declared_pins_never_silently_replaced(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "c1.db")
    analyzer = ChangeSetImpactAnalyzer(plane.store, plane.service, plane.cable)

    pins = plane.pins()
    pins[plane.pid_id] = pins[plane.pid_id] - 1  # stale declared pin
    with pytest.raises(ChangeSetError) as exc_info:
        analyzer.analyze(_mutate_both_intent(plane, as_of=AS_OF).__class__(
            project_id=DEFAULT_PROJECT,
            base_member_pins=pins,
            evaluation_as_of=AS_OF,
            mutations=_mutate_both_intent(plane).mutations,
        ))
    assert exc_info.value.code == "change_set_base_not_current"

    partial = plane.pins()
    del partial[plane.cable_id]
    with pytest.raises(ChangeSetError) as exc_info:
        analyzer.analyze(ChangeSetIntent(
            project_id=DEFAULT_PROJECT,
            base_member_pins=partial,
            evaluation_as_of=AS_OF,
            mutations=_mutate_both_intent(plane).mutations,
        ))
    assert exc_info.value.code == "change_set_base_not_current"


def test_c2_single_cas_source(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "c2.db")
    analyzer = ChangeSetImpactAnalyzer(plane.store, plane.service, plane.cable)
    intent = _mutate_both_intent(plane)
    leaked = ChangeSetIntent(
        project_id=DEFAULT_PROJECT,
        base_member_pins=plane.pins(),
        evaluation_as_of=AS_OF,
        mutations=[
            ChangeSetMutation(
                domain="pid",
                document_id=plane.pid_id,
                kind="pid_transaction",
                payload={
                    "operations": [
                        {
                            "op": "update_element",
                            "element_id": "PMP-101",
                            "patch": {"label": "x"},
                            "expected_revision": 0,
                        }
                    ]
                },
            ),
            intent.mutations[1],
        ],
    )
    with pytest.raises(ChangeSetError) as exc_info:
        analyzer.analyze(leaked)
    assert exc_info.value.code == "change_set_revision_leak"

    # cable payload revision must equal the declared base pin when present
    bad_cable = plane.cable_payload()
    bad_cable["revision"] = 99
    with pytest.raises(ChangeSetError) as exc_info:
        analyzer.analyze(ChangeSetIntent(
            project_id=DEFAULT_PROJECT,
            base_member_pins=plane.pins(),
            evaluation_as_of=AS_OF,
            mutations=[
                intent.mutations[0],
                ChangeSetMutation(
                    domain="cable",
                    document_id=plane.cable_id,
                    kind="cable_update",
                    payload=bad_cable,
                ),
            ],
        ))
    assert exc_info.value.code == "change_set_revision_leak"


def test_impact_repin_derivation_and_fail_closed_codes(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "impact.db")
    analyzer = ChangeSetImpactAnalyzer(plane.store, plane.service, plane.cable)

    impact = analyzer.analyze(_mutate_both_intent(plane))
    assert impact.affected_documents == tuple(sorted([plane.pid_id, plane.cable_id]))
    assert [action.link_id for action in impact.derived_repin_actions] == ["lnk_seed"]
    action = impact.derived_repin_actions[0]
    pins = plane.pins()
    assert action.pinned_source_revision == pins[plane.cable_id] + 1
    assert action.pinned_target_revision == pins[plane.pid_id] + 1

    # unknown object: mutation targets a non-member
    with pytest.raises(ChangeSetError) as exc_info:
        analyzer.analyze(ChangeSetIntent(
            project_id=DEFAULT_PROJECT,
            base_member_pins=plane.pins(),
            evaluation_as_of=AS_OF,
            mutations=[
                ChangeSetMutation(
                    domain="pid", document_id="doc_nope", kind="pid_transaction", payload={}
                )
            ],
        ))
    assert exc_info.value.code == "change_set_base_not_current" or exc_info.value.code == "impact_unknown_object"

    # linked-object invalidation: deleting the linked pid element fails closed
    with pytest.raises(ChangeSetError) as exc_info:
        analyzer.analyze(ChangeSetIntent(
            project_id=DEFAULT_PROJECT,
            base_member_pins=plane.pins(),
            evaluation_as_of=AS_OF,
            mutations=[
                ChangeSetMutation(
                    domain="pid",
                    document_id=plane.pid_id,
                    kind="pid_transaction",
                    payload={
                        "operations": [
                            DeleteElementOperation(element_id="PMP-101").model_dump()
                        ]
                    },
                )
            ],
        ))
    assert exc_info.value.code == "change_set_invalidates_link"

    # equipment-predicate break on an updated element fails closed too
    with pytest.raises(ChangeSetError) as exc_info:
        analyzer.analyze(ChangeSetIntent(
            project_id=DEFAULT_PROJECT,
            base_member_pins=plane.pins(),
            evaluation_as_of=AS_OF,
            mutations=[
                ChangeSetMutation(
                    domain="pid",
                    document_id=plane.pid_id,
                    kind="pid_transaction",
                    payload={
                        "operations": [
                            {
                                "op": "update_element",
                                "element_id": "PMP-101",
                                "patch": {"symbol_key": "analyzer_indicator"},
                            }
                        ]
                    },
                )
            ],
        ))
    assert exc_info.value.code == "change_set_invalidates_link"


def test_repin_algorithm_non_diffusion_and_unmutated_pins(tmp_path: Path) -> None:
    """R84-3 hard-locks: only mutated documents advance to base+1; unmutated
    members keep their pin; re-pins touch only links with a mutated endpoint,
    and only the mutated side moves; impact never diffuses transitively."""
    plane = Plane(tmp_path, "repin.db")
    analyzer = ChangeSetImpactAnalyzer(plane.store, plane.service, plane.cable)

    # third member + a second link from the same cable to another pid doc
    other = plane.service.create_document(CreateDocumentRequest(name="pid2", width=800, height=600))
    plane.service.apply_transaction(
        other.id,
        TransactionRequest(
            expected_revision=0,
            label="seed2",
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
    plane.store.add_document_to_project(DEFAULT_PROJECT, other.id, added_by="tester")
    connection = plane.store._connect()  # noqa: SLF001
    try:
        connection.execute(
            "INSERT INTO engineering_links ("
            " link_id, project_id, relation_type, source_domain, source_document_id,"
            " source_object_ref, source_endpoint, target_domain, target_document_id,"
            " target_object_ref, pinned_source_revision, pinned_target_revision,"
            " created_at, created_by"
            ") VALUES ('lnk_bc', ?, 'cable_endpoint_equipment', 'cable', ?, 'SEG-1',"
            " 'to', 'pid', ?, 'PMP-102', ?, ?, ?, 'tester')",
            (
                DEFAULT_PROJECT,
                plane.cable_id,
                other.id,
                plane.pins()[plane.cable_id],
                plane.service.get_document(other.id).revision,
                datetime.now(UTC).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    pins = plane.pins()
    pid_only = ChangeSetIntent(
        project_id=DEFAULT_PROJECT,
        base_member_pins=pins,
        evaluation_as_of=AS_OF,
        mutations=[
            ChangeSetMutation(
                domain="pid",
                document_id=plane.pid_id,
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
            )
        ],
    )
    impact = analyzer.analyze(pid_only)
    # mutated doc PLUS its direct link counterpart; no transitive diffusion
    assert impact.affected_documents == tuple(sorted([plane.pid_id, plane.cable_id]))
    assert impact.affected_links == tuple(
        link for link in impact.affected_links if link["link_id"] == "lnk_seed"
    )
    assert [link["link_id"] for link in impact.affected_links] == ["lnk_seed"]
    action = impact.derived_repin_actions[0]
    assert action.pinned_target_revision == pins[plane.pid_id] + 1  # mutated side
    assert action.pinned_source_revision == pins[plane.cable_id]  # unmutated side keeps pin

    previewer = ChangeSetPreviewer(plane.store, plane.service, plane.cable)
    result = previewer.preview(pid_only, impact)
    assert result.revision_projection[plane.pid_id] == pins[plane.pid_id] + 1
    assert result.revision_projection[plane.cable_id] == pins[plane.cable_id]
    assert result.revision_projection[other.id] == pins[other.id]

    # cable-only: the pid side of the link keeps its pin
    cable_only = ChangeSetIntent(
        project_id=DEFAULT_PROJECT,
        base_member_pins=pins,
        evaluation_as_of=AS_OF,
        mutations=[
            ChangeSetMutation(
                domain="cable",
                document_id=plane.cable_id,
                kind="cable_update",
                payload=plane.cable_payload(),
            )
        ],
    )
    cable_impact = analyzer.analyze(cable_only)
    assert sorted(link["link_id"] for link in cable_impact.affected_links) == [
        "lnk_bc",
        "lnk_seed",
    ]
    for action in cable_impact.derived_repin_actions:
        assert action.pinned_source_revision == pins[plane.cable_id] + 1
        target_doc = next(
            link["target_document_id"]
            for link in cable_impact.affected_links
            if link["link_id"] == action.link_id
        )
        assert action.pinned_target_revision == pins[target_doc]

    # both-mutated: the cable side and the mutated pid side advance; the
    # unmutated pid2 (lnk_bc target) keeps its pin even here
    both = analyzer.analyze(_mutate_both_intent(plane))
    for action in both.derived_repin_actions:
        assert action.pinned_source_revision == pins[plane.cable_id] + 1
        target_doc = next(
            link["target_document_id"]
            for link in both.affected_links
            if link["link_id"] == action.link_id
        )
        expected_target = (
            pins[target_doc] + 1 if target_doc == plane.pid_id else pins[target_doc]
        )
        assert action.pinned_target_revision == expected_target


def test_unlinked_element_may_become_instrument(tmp_path: Path) -> None:
    """R84-5: the equipment predicate only guards elements referenced by active
    engineering links; an unlinked element may change category."""
    plane = Plane(tmp_path, "unlinked.db")
    plane.service.apply_transaction(
        plane.pid_id,
        TransactionRequest(
            expected_revision=plane.service.get_document(plane.pid_id).revision,
            label="add unlinked",
            operations=[
                AddElementOperation(
                    element=SymbolElement(
                        id="AI-9",
                        symbol_key="agitator",
                        position={"x": 500, "y": 100},
                        width=60,
                        height=40,
                        label="AI-9",
                    )
                )
            ],
        ),
    )
    analyzer = ChangeSetImpactAnalyzer(plane.store, plane.service, plane.cable)
    intent = ChangeSetIntent(
        project_id=DEFAULT_PROJECT,
        base_member_pins=plane.pins(),
        evaluation_as_of=AS_OF,
        mutations=[
            ChangeSetMutation(
                domain="pid",
                document_id=plane.pid_id,
                kind="pid_transaction",
                payload={
                    "operations": [
                        {
                            "op": "update_element",
                            "element_id": "AI-9",
                            "patch": {"symbol_key": "analyzer_indicator"},
                        }
                    ]
                },
            )
        ],
    )
    impact = analyzer.analyze(intent)  # must NOT raise
    assert impact.affected_documents == tuple(sorted([plane.pid_id, plane.cable_id]))


def test_clear_document_invalidates_linked_objects(tmp_path: Path) -> None:
    """clear_document removes every element; a linked element therefore fails
    the change set closed via the staged post-state."""
    plane = Plane(tmp_path, "clear.db")
    analyzer = ChangeSetImpactAnalyzer(plane.store, plane.service, plane.cable)
    intent = ChangeSetIntent(
        project_id=DEFAULT_PROJECT,
        base_member_pins=plane.pins(),
        evaluation_as_of=AS_OF,
        mutations=[
            ChangeSetMutation(
                domain="pid",
                document_id=plane.pid_id,
                kind="pid_transaction",
                payload={"operations": [{"op": "clear_document"}]},
            )
        ],
    )
    with pytest.raises(ChangeSetError) as exc_info:
        analyzer.analyze(intent)
    assert exc_info.value.code == "change_set_invalidates_link"


def test_pid_payload_rejects_revision_keys_at_any_depth(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "depth.db")
    analyzer = ChangeSetImpactAnalyzer(plane.store, plane.service, plane.cable)
    intent = ChangeSetIntent(
        project_id=DEFAULT_PROJECT,
        base_member_pins=plane.pins(),
        evaluation_as_of=AS_OF,
        mutations=[
            ChangeSetMutation(
                domain="pid",
                document_id=plane.pid_id,
                kind="pid_transaction",
                payload={
                    "operations": [
                        {
                            "op": "update_element",
                            "element_id": "PMP-101",
                            "patch": {"metadata": {"revision": 3}},
                        }
                    ]
                },
            )
        ],
    )
    with pytest.raises(ChangeSetError) as exc_info:
        analyzer.analyze(intent)
    assert exc_info.value.code == "change_set_revision_leak"


def test_preview_zero_state_change_and_determinism(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "preview.db")
    analyzer = ChangeSetImpactAnalyzer(plane.store, plane.service, plane.cable)
    previewer = ChangeSetPreviewer(plane.store, plane.service, plane.cable)
    intent = _mutate_both_intent(plane)
    impact = analyzer.analyze(intent)

    pins_before = plane.pins()
    audit_before = len(plane.recorder.store.all_audit_records())
    cable_json_before = plane.store.get_cable_envelope(plane.cable_id)[1]
    pid_before = plane.service.get_document(plane.pid_id).model_dump_json()

    result = previewer.preview(intent, impact)

    assert plane.pins() == pins_before
    assert len(plane.recorder.store.all_audit_records()) == audit_before
    assert plane.store.get_cable_envelope(plane.cable_id)[1] == cable_json_before
    assert plane.service.get_document(plane.pid_id).model_dump_json() == pid_before

    assert result.revision_projection == {
        document_id: pin + 1 for document_id, pin in intent.base_member_pins.items()
    }
    assert result.pid_diffs[plane.pid_id]["updated"] == ["PMP-101"]
    assert result.cable_diffs[plane.cable_id]["removed"] == []
    assert result.derived_repin_actions[0]["pinned_source_revision"] == pins_before[plane.cable_id] + 1

    second = previewer.preview(intent, impact)
    assert result.canonical() == second.canonical()
    assert change_set_intent_hash(intent) == result.intent_hash


def test_preview_readiness_projection_and_persistence(tmp_path: Path) -> None:
    plane = Plane(tmp_path, "preview2.db")
    analyzer = ChangeSetImpactAnalyzer(plane.store, plane.service, plane.cable)
    previewer = ChangeSetPreviewer(plane.store, plane.service, plane.cable)
    intent = _mutate_both_intent(plane)
    impact = analyzer.analyze(intent)
    result = previewer.preview(intent, impact)

    # before: everything linked and eligible; after projection stays eligible
    assert result.readiness_before.state == "eligible"
    assert result.readiness_after.state == "eligible"
    assert result.readiness_after.result_hash != result.readiness_before.result_hash

    # snapshot readiness determinism / evaluation_as_of parity
    later = previewer.preview(_mutate_both_intent(plane, as_of=AS_OF_LATER), impact)
    assert later.readiness_after.result_hash != result.readiness_after.result_hash

    # controlled persistence of the analysis snapshots
    from agentcad.project_change_set import new_change_set_id

    change_set_id = new_change_set_id()
    plane.store.insert_change_set(
        change_set_id=change_set_id,
        project_id=DEFAULT_PROJECT,
        base_pins=json.dumps(intent.base_member_pins, sort_keys=True),
        intent=canonical_change_set_intent(intent),
        intent_hash=change_set_intent_hash(intent),
        created_by="tester",
    )
    assert plane.store.save_change_set_analysis(
        change_set_id=change_set_id,
        impacted=json.dumps(impact.canonical(), sort_keys=True),
        preview=json.dumps(result.canonical(), sort_keys=True),
    ) is True
    row = plane.store.get_change_set(change_set_id)
    persisted_impact = json.loads(row["impacted"])
    # the FULL impacted snapshot is persisted for D4 exact-approval binding
    assert persisted_impact == impact.canonical()
    # D1 frozen six fields are all present in the persisted snapshot
    for key in (
        "affected_documents",
        "affected_objects",
        "affected_links",
        "derived_repin_actions",
        "validation_scope",
        "issues",
    ):
        assert key in persisted_impact
    assert persisted_impact["issues"] == []
    assert persisted_impact["derived_repin_actions"] == [
        {
            "link_id": action.link_id,
            "pinned_source_revision": action.pinned_source_revision,
            "pinned_target_revision": action.pinned_target_revision,
        }
        for action in impact.derived_repin_actions
    ]
    assert json.loads(row["preview"])["intent_hash"] == result.intent_hash
    # CAS: only a staged row accepts analysis snapshots
    assert plane.store.save_change_set_analysis(
        change_set_id=change_set_id, impacted="{}"
    ) is True  # still staged
    plane.store.update_change_set_status(
        change_set_id=change_set_id, expected_status="staged", new_status="approved"
    )
    assert plane.store.save_change_set_analysis(
        change_set_id=change_set_id, impacted="{}"
    ) is False
