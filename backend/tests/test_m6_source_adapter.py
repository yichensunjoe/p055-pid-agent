"""M6 Phase-2B D2: the source-to-candidate adapter, proved by what it refuses.

The adapter is judged by the same standard as the rest of M6: not by how much it reads into
a drawing, but by what it will *not* do — it will not read a drifted source, will not guess
between tied readings, will not fuzz a missing catalogue entry into a near one, and will not
touch the engineering model while filing a single candidate.

The positive fixtures are built two ways on purpose:

* directly as documents (fast, precise control of block/layer provenance), and
* through the real governed import path (``CadImporter``), because the adapter's input in
  production is an imported document — a derivation that only works on hand-built documents
  would prove nothing about the chain the gate cares about.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from cad_fixtures import DxfBuilder

from agentcad.cad_import import CadImporter
from agentcad.cad_models import CadImportOptions
from agentcad.engineering_ir import document_content_hash
from agentcad.m6_candidate_core import M6CandidateService, filing_decision
from agentcad.m6_candidate_models import (
    RegionGeometry,
    SemanticCandidate,
    SourceArtifactRef,
    TextSpan,
)
from agentcad.m6_ingestion_contract import IDENTITY_FULL_DIGEST_HEX, identity_prefix
from agentcad.m6_region import build_source_region, region_identity
from agentcad.m6_source_adapter import (
    M6_SOURCE_RULES,
    CandidateIdentityConflict,
    IngestionSummary,
    M6SourceAdapter,
    SourceVerificationError,
    TargetDocumentError,
)
from agentcad.models import (
    CircleElement,
    Document,
    Layer,
    LineElement,
    Point,
    TextElement,
)
from agentcad.service import DocumentService
from agentcad.store import M6FilingError, SQLiteDocumentStore, StoredDocument
from agentcad.symbols import SymbolRegistry

SOURCE_ID = "doc_m6d2_source"
TARGET_ID = "doc_m6d2_target"
REGION_PREFIX = identity_prefix("region_id")


@pytest.fixture(scope="module")
def registry() -> SymbolRegistry:
    return SymbolRegistry()


@pytest.fixture()
def store(tmp_path) -> SQLiteDocumentStore:
    return SQLiteDocumentStore(tmp_path / "m6d2.sqlite3")


@pytest.fixture()
def adapter(store: SQLiteDocumentStore, registry: SymbolRegistry) -> M6SourceAdapter:
    return M6SourceAdapter(store, registry)


def _block_lines(prefix: str, x: float, y: float, block: str, layer: str = "EQUIP") -> list:
    """One fake symbol instance: a box plus a circle, carrying CAD block provenance."""

    return [
        LineElement(
            id=f"{prefix}_a",
            start=Point(x=x, y=y),
            end=Point(x=x + 20, y=y),
            metadata={"cad_block": block, "cad_handle": "1A", "cad_layer": layer},
        ),
        LineElement(
            id=f"{prefix}_b",
            start=Point(x=x + 20, y=y),
            end=Point(x=x + 20, y=y + 20),
            metadata={"cad_block": block, "cad_handle": "1B", "cad_layer": layer},
        ),
        LineElement(
            id=f"{prefix}_c",
            start=Point(x=x + 20, y=y + 20),
            end=Point(x=x, y=y + 20),
            metadata={"cad_block": block, "cad_handle": "1C", "cad_layer": layer},
        ),
        LineElement(
            id=f"{prefix}_d",
            start=Point(x=x, y=y + 20),
            end=Point(x=x, y=y),
            metadata={"cad_block": block, "cad_handle": "1D", "cad_layer": layer},
        ),
        CircleElement(
            id=f"{prefix}_e",
            center=Point(x=x + 10, y=y + 10),
            radius=8,
            metadata={"cad_block": block, "cad_handle": "1E", "cad_layer": layer},
        ),
    ]


def _tag(element_id: str, x: float, y: float, text: str, layer: str = "EQUIP") -> TextElement:
    return TextElement(
        id=element_id,
        position=Point(x=x, y=y),
        text=text,
        font_size=12,
        metadata={"cad_layer": layer},
    )


def _document(elements: list, *, revision: int = 3, document_id: str = SOURCE_ID) -> Document:
    return Document(
        id=document_id,
        name="M6 D2 fixture",
        revision=revision,
        layers=[Layer(id="layer_default", name="Default"), Layer(id="layer_cad", name="EQUIP")],
        elements=elements,
    )


def _save(store: SQLiteDocumentStore, document: Document) -> None:
    store.save(StoredDocument(document=document, undo_stack=[], redo_stack=[]))


def _two_pump_document() -> Document:
    return _document(
        [
            *_block_lines("p1", 100, 100, "centrifugal_pump"),
            *_block_lines("p2", 300, 100, "centrifugal_pump"),
            _tag("t1", 110, 130, "P-201"),
            _tag("t2", 310, 130, "P-202"),
        ]
    )


def _by_type(candidates: list[SemanticCandidate], kind: str) -> list[SemanticCandidate]:
    return [candidate for candidate in candidates if candidate.candidate_type == kind]


def _ingest(
    adapter: M6SourceAdapter, source_id: str = SOURCE_ID, target_id: str = TARGET_ID
) -> IngestionSummary:
    """Pin-then-ingest: the only way in after D88-1 — the adapter never re-pins."""

    return adapter.ingest(adapter.pin_source(source_id), target_document_id=target_id)


# --------------------------------------------------------------------------------------
# Region identity: derived, deterministic, revision-sensitive
# --------------------------------------------------------------------------------------


def test_region_identity_format_matches_the_contract_declaration() -> None:
    region_id = region_identity(
        artifact_id="artifact_x",
        source_revision=7,
        geometry_selector=RegionGeometry(x=1, y=2, width=3, height=4),
        element_refs=["el_1"],
        text_spans=[],
    )
    assert region_id.startswith(REGION_PREFIX)
    suffix = region_id[len(REGION_PREFIX):]
    assert len(suffix) == IDENTITY_FULL_DIGEST_HEX
    assert all(char in "0123456789abcdef" for char in suffix)


def test_region_identity_is_deterministic_and_order_independent() -> None:
    first = region_identity(
        artifact_id="artifact_x",
        source_revision=7,
        geometry_selector=None,
        element_refs=["el_2", "el_1"],
        text_spans=[TextSpan(text="B"), TextSpan(text="A")],
    )
    shuffled = region_identity(
        artifact_id="artifact_x",
        source_revision=7,
        geometry_selector=None,
        element_refs=["el_1", "el_2"],
        text_spans=[TextSpan(text="A"), TextSpan(text="B")],
    )
    assert first == shuffled


def test_a_changed_source_revision_produces_a_new_region_identity() -> None:
    """The contract's region rule, pinned as behaviour rather than prose."""

    base = dict(
        artifact_id="artifact_x",
        geometry_selector=None,
        element_refs=["el_1"],
        text_spans=[],
    )
    assert region_identity(source_revision=7, **base) != region_identity(source_revision=8, **base)


def test_a_built_region_carries_the_identity_its_content_derives() -> None:
    artifact = SourceArtifactRef(
        artifact_id="artifact_x",
        source_document_id=SOURCE_ID,
        source_revision=3,
        content_hash="abc",
    )
    region = build_source_region(
        artifact=artifact,
        geometry_selector=RegionGeometry(x=0, y=0, width=10, height=10),
        element_refs=["el_1"],
    )
    assert region.region_id == region_identity(
        artifact_id=artifact.artifact_id,
        source_revision=artifact.source_revision,
        geometry_selector=region.geometry_selector,
        element_refs=region.element_refs,
        text_spans=region.text_spans,
    )
    assert region.coordinate_frame == "document"


# --------------------------------------------------------------------------------------
# Positive: block clustering, tag binding, roles, filing — on a hand-built document
# --------------------------------------------------------------------------------------


def test_block_instances_of_the_same_block_split_spatially(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    """Two INSERTs of one block share the block definition's handles; instances are
    recovered as connected geometry, so both pumps must become *separate* regions."""

    _save(store, _two_pump_document())
    summary = _ingest(adapter)

    assert summary.cluster_count == 2
    symbol = _by_type(store.list_semantic_candidates(), "symbol_class")
    assert len(symbol) == 2
    assert {candidate.proposed_semantics.symbol_class for candidate in symbol} == {
        "centrifugal_pump"
    }
    assert len({candidate.region.region_id for candidate in symbol}) == 2


def test_candidates_carry_both_document_identities(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    _save(store, _two_pump_document())
    _ingest(adapter)

    for candidate in store.list_semantic_candidates():
        assert candidate.artifact.source_document_id == SOURCE_ID
        assert candidate.artifact.source_revision == 3
        assert candidate.target_document_id == TARGET_ID
        assert candidate.region.source_document_id == SOURCE_ID


def test_filed_candidates_are_born_proposed_then_needs_review(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    """Filing goes through the governed core: proposed at birth, needs_review after the
    producer's filing event, and nothing reaches confirmed without a recorded person."""

    _save(store, _two_pump_document())
    summary = _ingest(adapter)

    service = M6CandidateService(store)
    assert summary.filed == summary.candidate_ids
    for candidate_id in summary.candidate_ids:
        stored = store.get_semantic_candidate(candidate_id)
        assert stored is not None
        assert stored.review_status == "proposed"
        assert service.current_status(candidate_id) == "needs_review"
        assert stored.producer.key == "deterministic_rule_engine"
        assert stored.provenance.procedure_version == M6_SOURCE_RULES


def test_equipment_tag_and_role_candidates_anchor_to_their_cluster(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    _save(store, _two_pump_document())
    _ingest(adapter)

    tags = _by_type(store.list_semantic_candidates(), "equipment_tag")
    assert {candidate.proposed_semantics.equipment_tag for candidate in tags} == {
        "P-201",
        "P-202",
    }
    for candidate in tags:
        assert "t1" in candidate.region.element_refs or "t2" in candidate.region.element_refs
        assert candidate.region.text_spans[0].text == candidate.proposed_semantics.equipment_tag

    roles = _by_type(store.list_semantic_candidates(), "annotation_role")
    assert len(roles) == 2
    assert {candidate.proposed_semantics.annotation_role for candidate in roles} == {
        "equipment_label"
    }


def test_an_instrument_shaped_tag_gets_the_instrument_role(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    _save(store, _document([_tag("t_pt", 800, 700, "PT-101")]))
    _ingest(adapter)

    roles = _by_type(store.list_semantic_candidates(), "annotation_role")
    assert len(roles) == 1
    assert roles[0].proposed_semantics.annotation_role == "instrument_tag"


# --------------------------------------------------------------------------------------
# Refusals: the source is verified, never trusted
# --------------------------------------------------------------------------------------


def test_a_missing_source_is_refused_without_writes(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    gone = SourceArtifactRef(
        artifact_id="artifact_gone",
        source_document_id="doc_never_existed",
        source_revision=1,
        content_hash="x" * 64,
    )
    with pytest.raises(SourceVerificationError) as caught:
        adapter.ingest(gone, target_document_id=TARGET_ID)
    assert caught.value.code == "source_snapshot_unavailable"
    assert store.list_semantic_candidates() == []


def test_ingest_refuses_a_stale_artifact_without_writes(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    """D88-1: the pin is the evidence identity; ingest must never re-pin to fit it."""

    _save(store, _two_pump_document())
    artifact = adapter.pin_source(SOURCE_ID)
    _save(store, _two_pump_document().model_copy(update={"revision": 4}))

    with pytest.raises(SourceVerificationError) as caught:
        adapter.ingest(artifact, target_document_id=TARGET_ID)
    assert caught.value.code == "source_revision_drift"
    assert store.list_semantic_candidates() == []
    assert all(
        store.list_review_decisions(cid) == []
        for cid in [c.candidate_id for c in store.list_semantic_candidates()]
    )


def test_ingest_refuses_a_forged_hash_without_writes(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    _save(store, _two_pump_document())
    artifact = adapter.pin_source(SOURCE_ID).model_copy(
        update={"content_hash": "0" * 64}
    )
    with pytest.raises(SourceVerificationError) as caught:
        adapter.ingest(artifact, target_document_id=TARGET_ID)
    assert caught.value.code == "source_content_drift"
    assert store.list_semantic_candidates() == []


def test_derive_refuses_a_document_that_does_not_match_the_pin(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    """D88-1, second half: derivation itself verifies the content hash it was handed."""

    _save(store, _two_pump_document())
    artifact = adapter.pin_source(SOURCE_ID)
    drifted = _document(
        [*_two_pump_document().elements, _tag("t_extra", 700, 700, "E-301")],
        revision=3,
    )
    with pytest.raises(SourceVerificationError) as caught:
        adapter.derive_candidates(drifted, artifact, target_document_id=TARGET_ID)
    assert caught.value.code == "source_content_drift"


def test_ingest_requires_a_declared_target_document(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    """D88-3: an empty target is refused with a stable code and zero writes."""

    _save(store, _two_pump_document())
    artifact = adapter.pin_source(SOURCE_ID)
    with pytest.raises(TargetDocumentError) as caught:
        adapter.ingest(artifact, target_document_id="")
    assert caught.value.code == "target_document_id_required"
    assert store.list_semantic_candidates() == []


def test_ingest_refuses_a_target_that_is_the_source(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    """D88-3: the evidence document can never be its own engineering target."""

    _save(store, _two_pump_document())
    artifact = adapter.pin_source(SOURCE_ID)
    with pytest.raises(TargetDocumentError) as caught:
        adapter.ingest(artifact, target_document_id=SOURCE_ID)
    assert caught.value.code == "target_document_equals_source"
    assert store.list_semantic_candidates() == []


def test_a_moved_source_revision_is_refused(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    _save(store, _two_pump_document())
    artifact = adapter.pin_source(SOURCE_ID)

    moved = _two_pump_document().model_copy(update={"revision": 4})
    _save(store, moved)
    with pytest.raises(SourceVerificationError) as caught:
        adapter.verify_source(artifact)
    assert caught.value.code == "source_revision_drift"
    assert store.list_semantic_candidates() == []


def test_a_content_drift_at_the_pinned_revision_is_refused(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    _save(store, _two_pump_document())
    artifact = adapter.pin_source(SOURCE_ID)

    tampered = _document(
        [*_two_pump_document().elements, _tag("t_extra", 700, 700, "E-301")],
        revision=3,
    )
    _save(store, tampered)  # same revision, different content
    assert tampered.revision == artifact.source_revision
    with pytest.raises(SourceVerificationError) as caught:
        adapter.verify_source(artifact)
    assert caught.value.code == "source_content_drift"


def test_a_deleted_source_cannot_be_reverified(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    _save(store, _two_pump_document())
    artifact = adapter.pin_source(SOURCE_ID)

    assert store.delete(SOURCE_ID, expected_revision=artifact.source_revision)
    with pytest.raises(SourceVerificationError) as caught:
        adapter.verify_source(artifact)
    assert caught.value.code == "source_snapshot_unavailable"


def test_an_unpinned_hash_is_not_verifiable(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    _save(store, _two_pump_document())
    artifact = adapter.pin_source(SOURCE_ID).model_copy(update={"content_hash": ""})
    with pytest.raises(SourceVerificationError) as caught:
        adapter.verify_source(artifact)
    assert caught.value.code == "source_snapshot_unavailable"


# --------------------------------------------------------------------------------------
# Refusals: ambiguity and gaps are recorded, never guessed away
# --------------------------------------------------------------------------------------


def test_an_unknown_block_is_out_of_catalogue_not_guessed(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    _save(store, _document(_block_lines("z1", 500, 500, "ZZZ-MYSTERY")))
    _ingest(adapter)

    candidates = store.list_semantic_candidates()
    assert _by_type(candidates, "symbol_class") == []
    unresolved = _by_type(candidates, "unresolved")
    assert len(unresolved) == 1
    assert unresolved[0].proposed_semantics.unresolved_reason == "out_of_catalog"


def test_a_tied_symbol_match_is_ambiguous_not_picked(
    store: SQLiteDocumentStore, tmp_path
) -> None:
    catalogue = tmp_path / "extra_symbols.json"
    catalogue.write_text(
        json.dumps(
            {
                "symbols": [
                    {
                        "key": "multiway_valve_a",
                        "name": "多路阀A",
                        "category": "阀门",
                        "width": 40,
                        "height": 40,
                        "metadata": {"aliases": ["multiway"]},
                    },
                    {
                        "key": "multiway_valve_b",
                        "name": "多路阀B",
                        "category": "阀门",
                        "width": 40,
                        "height": 40,
                        "metadata": {"aliases": ["multiway"]},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    adapter = M6SourceAdapter(store, SymbolRegistry(search_paths=[tmp_path]))
    _save(store, _document(_block_lines("m1", 500, 500, "MULTIWAY")))
    _ingest(adapter)

    candidates = store.list_semantic_candidates()
    assert _by_type(candidates, "symbol_class") == []
    unresolved = _by_type(candidates, "unresolved")
    assert len(unresolved) == 1
    assert unresolved[0].proposed_semantics.unresolved_reason == "ambiguous"
    assert "multiway_valve_a" in unresolved[0].evidence[0].detail
    assert "multiway_valve_b" in unresolved[0].evidence[0].detail


def test_two_tags_on_one_cluster_are_an_ambiguity_record(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    _save(
        store,
        _document(
            [
                *_block_lines("p1", 100, 100, "centrifugal_pump"),
                _tag("t1", 110, 130, "P-201"),
                _tag("t2", 112, 132, "P-202"),
            ]
        ),
    )
    _ingest(adapter)

    candidates = store.list_semantic_candidates()
    assert _by_type(candidates, "equipment_tag") == []
    unresolved = _by_type(candidates, "unresolved")
    assert len(unresolved) == 1
    assert unresolved[0].proposed_semantics.unresolved_reason == "ambiguous"
    # The symbol classification is a separate question and is still answered.
    assert len(_by_type(candidates, "symbol_class")) == 1


def test_an_equidistant_tag_binding_is_ambiguous_not_picked(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    _save(
        store,
        _document(
            [
                *_block_lines("p1", 100, 100, "centrifugal_pump"),
                *_block_lines("p2", 160, 100, "gate_valve"),
                _tag("t_mid", 140, 150, "Q-500"),  # 30 from each cluster
            ]
        ),
    )
    _ingest(adapter)

    candidates = store.list_semantic_candidates()
    assert _by_type(candidates, "equipment_tag") == []
    unresolved = _by_type(candidates, "unresolved")
    assert len(unresolved) == 1
    assert unresolved[0].proposed_semantics.unresolved_reason == "ambiguous"
    assert unresolved[0].evidence[0].kind == "neighbour"


def test_an_unanchorable_tag_is_insufficient_evidence(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    _save(
        store,
        _document(
            [
                *_block_lines("p1", 100, 100, "centrifugal_pump"),
                _tag("t_far", 900, 900, "P-999"),
            ]
        ),
    )
    _ingest(adapter)

    candidates = store.list_semantic_candidates()
    assert _by_type(candidates, "equipment_tag") == []
    unresolved = _by_type(candidates, "unresolved")
    assert len(unresolved) == 1
    assert unresolved[0].proposed_semantics.unresolved_reason == "insufficient_evidence"


# --------------------------------------------------------------------------------------
# Determinism: replay and idempotent re-ingest
# --------------------------------------------------------------------------------------


def test_the_same_input_derives_the_same_candidates_twice(
    store: SQLiteDocumentStore, tmp_path, registry: SymbolRegistry
) -> None:
    document = _two_pump_document()
    second_store = SQLiteDocumentStore(tmp_path / "second.sqlite3")
    _save(store, document)
    _save(second_store, document)

    first = M6SourceAdapter(store, registry)
    second = M6SourceAdapter(second_store, registry)
    artifact = first.pin_source(SOURCE_ID)
    digest_first = first.extraction_digest(
        artifact, TARGET_ID, first.derive_candidates(document, artifact, target_document_id=TARGET_ID)
    )
    artifact_second = second.pin_source(SOURCE_ID)
    digest_second = second.extraction_digest(
        artifact_second,
        TARGET_ID,
        second.derive_candidates(document, artifact_second, target_document_id=TARGET_ID),
    )
    # imported_at is provenance, never identity: the pinned triple is what must agree.
    assert artifact.model_dump(exclude={"imported_at"}) == artifact_second.model_dump(
        exclude={"imported_at"}
    )
    assert digest_first == digest_second


def test_re_ingest_is_idempotent_by_identity(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    _save(store, _two_pump_document())
    first = _ingest(adapter)
    second = _ingest(adapter)

    assert second.filed == []
    assert sorted(second.already_present) == sorted(first.candidate_ids)
    assert second.extraction_digest == first.extraction_digest
    assert len(store.list_semantic_candidates()) == len(first.candidate_ids)
    # No decision rows are duplicated either: one filing event per candidate, still.
    for candidate_id in first.candidate_ids:
        assert len(store.list_review_decisions(candidate_id)) == 1


def test_a_candidate_id_collision_with_different_content_fails_loudly(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    """A stored row whose content disagrees with its content-derived id aborts the batch."""

    _save(store, _two_pump_document())
    artifact = adapter.pin_source(SOURCE_ID)
    candidates = adapter.derive_candidates(
        store.get(SOURCE_ID).document, artifact, target_document_id=TARGET_ID
    )
    victim = candidates[len(candidates) // 2]
    tampered = victim.model_copy(update={"review_status": "needs_review"})
    store.insert_semantic_candidate(tampered)  # same id, different content

    with pytest.raises(CandidateIdentityConflict):
        adapter.ingest(artifact, target_document_id=TARGET_ID)

    # The batch rolled back: the tampered row is all that exists, and no filing decisions.
    assert [c.candidate_id for c in store.list_semantic_candidates()] == [victim.candidate_id]
    assert store.list_review_decisions(victim.candidate_id) == []


# --------------------------------------------------------------------------------------
# Atomic filing: one transaction, all or nothing (D88-2)
# --------------------------------------------------------------------------------------


def test_a_mid_batch_failure_rolls_back_everything(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    """Fault injection: the middle filing decision fails -> zero new rows anywhere.

    A trigger on the decisions table makes a mid-batch write fail for real, inside the
    transaction the store opened — nothing is mocked, so this exercises the rollback rather
    than an imitation of it.
    """

    _save(store, _two_pump_document())
    artifact = adapter.pin_source(SOURCE_ID)
    candidates = adapter.derive_candidates(
        store.get(SOURCE_ID).document, artifact, target_document_id=TARGET_ID
    )
    victim = candidates[len(candidates) // 2]
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            f"""
            CREATE TRIGGER m6_inject_filing_failure
            BEFORE INSERT ON review_decisions
            WHEN NEW.candidate_id = '{victim.candidate_id}'
            BEGIN SELECT RAISE(FAIL, 'injected mid-batch failure'); END
            """
        )
        connection.commit()

    with pytest.raises(sqlite3.Error):
        adapter.ingest(artifact, target_document_id=TARGET_ID)

    assert store.list_semantic_candidates() == []
    for candidate in candidates:
        assert store.list_review_decisions(candidate.candidate_id) == []
    # The source document is exactly as the save left it.
    assert store.get(SOURCE_ID).document.revision == 3

    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DROP TRIGGER m6_inject_filing_failure")
        connection.commit()

    # And once the fault is gone, the same ingest goes through cleanly.
    summary = adapter.ingest(artifact, target_document_id=TARGET_ID)
    assert len(summary.filed) == len(candidates)


def test_the_batch_skips_known_identities_and_files_new_ones_atomically(
    store: SQLiteDocumentStore,
) -> None:
    """A mixed batch: known ids are left untouched, new ids file, in one transaction."""

    _save(store, _two_pump_document())
    adapter = M6SourceAdapter(store, SymbolRegistry())
    artifact = adapter.pin_source(SOURCE_ID)
    document = store.get(SOURCE_ID).document
    candidates = adapter.derive_candidates(document, artifact, target_document_id=TARGET_ID)
    known, new = candidates[:2], candidates[2:]
    store.file_semantic_candidates(
        [(candidate, filing_decision(candidate)) for candidate in known],
        source_document_id=SOURCE_ID,
        expected_source_revision=document.revision,
        expected_source_content_hash=document_content_hash(document),
    )

    filed, already = store.file_semantic_candidates(
        [(candidate, filing_decision(candidate)) for candidate in [*known, *new]],
        source_document_id=SOURCE_ID,
        expected_source_revision=document.revision,
        expected_source_content_hash=document_content_hash(document),
    )

    assert sorted(already) == sorted(c.candidate_id for c in known)
    assert sorted(filed) == sorted(c.candidate_id for c in new)
    assert len(store.list_semantic_candidates()) == len(candidates)
    for candidate in candidates:
        assert len(store.list_review_decisions(candidate.candidate_id)) == 1


def test_the_batch_reverifies_the_source_inside_its_transaction(
    store: SQLiteDocumentStore,
) -> None:
    """The final in-transaction check, exercised directly: wrong pins fail closed."""

    _save(store, _two_pump_document())
    document = store.get(SOURCE_ID).document
    with pytest.raises(M6FilingError) as caught:
        store.file_semantic_candidates(
            [],
            source_document_id=SOURCE_ID,
            expected_source_revision=99,
            expected_source_content_hash=document_content_hash(document),
        )
    assert caught.value.code == "source_revision_drift"
    with pytest.raises(M6FilingError) as caught:
        store.file_semantic_candidates(
            [],
            source_document_id=SOURCE_ID,
            expected_source_revision=document.revision,
            expected_source_content_hash="0" * 64,
        )
    assert caught.value.code == "source_content_drift"
    with pytest.raises(M6FilingError) as caught:
        store.file_semantic_candidates(
            [],
            source_document_id="doc_gone",
            expected_source_revision=1,
            expected_source_content_hash="0" * 64,
        )
    assert caught.value.code == "source_snapshot_unavailable"


# --------------------------------------------------------------------------------------
# v7 payload compatibility: an old row without target_document_id still reads
# --------------------------------------------------------------------------------------


def test_a_v7_payload_without_target_document_id_still_validates(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    _save(store, _two_pump_document())
    _ingest(adapter)
    candidate = store.list_semantic_candidates()[0]

    payload = candidate.model_dump(mode="json", by_alias=True)
    payload.pop("target_document_id")
    old = SemanticCandidate.model_validate_json(json.dumps(payload))
    assert old.target_document_id == ""
    assert old.candidate_id == candidate.candidate_id


def test_a_stored_candidate_round_trips_with_its_target(
    adapter: M6SourceAdapter, store: SQLiteDocumentStore
) -> None:
    _save(store, _two_pump_document())
    _ingest(adapter)
    written = store.list_semantic_candidates()[0]
    read_back = store.get_semantic_candidate(written.candidate_id)
    assert read_back is not None
    assert read_back.target_document_id == TARGET_ID
    assert read_back.model_dump(mode="json", by_alias=True) == written.model_dump(
        mode="json", by_alias=True
    )


# --------------------------------------------------------------------------------------
# The governed boundary: filing never touches the engineering plane
# --------------------------------------------------------------------------------------


def test_ingestion_writes_zero_engineering_state(tmp_path, registry: SymbolRegistry) -> None:
    """Revision, history and the audit chain are exactly what the import left behind."""

    store = SQLiteDocumentStore(tmp_path / "zero_write.sqlite3")
    service = DocumentService(store, registry)
    document = _two_pump_document()
    _save(store, document)
    service.get_document(SOURCE_ID)  # prove the read path is the same one the service uses

    before_revision = store.get(SOURCE_ID).document.revision
    before_history = len(store.list_history(SOURCE_ID))
    before_audit = len(store.all_audit_records())
    before_content = document_content_hash(store.get(SOURCE_ID).document)

    summary = _ingest(M6SourceAdapter(store, registry))
    assert summary.filed, "the fixture must actually file candidates"

    after = store.get(SOURCE_ID)
    assert after.document.revision == before_revision
    assert len(store.list_history(SOURCE_ID)) == before_history
    assert len(store.all_audit_records()) == before_audit
    assert document_content_hash(after.document) == before_content
    # The writes that did happen went to the governance tables only.
    assert len(store.list_semantic_candidates()) == len(summary.candidate_ids)


# --------------------------------------------------------------------------------------
# End-to-end: the real import path feeds the adapter
# --------------------------------------------------------------------------------------


def _synthetic_dxf() -> bytes:
    """A small but real DXF: two pump instances, one valve, one unknown block, tags."""

    builder = DxfBuilder()
    builder.layer("EQUIP", color=5)
    # A pump-ish block: a box with a circle inside.
    builder.block(
        "centrifugal_pump",
        [
            [(0, "LINE"), (8, "EQUIP"), (10, -10.0), (20, -10.0), (11, 10.0), (21, -10.0)],
            [(0, "LINE"), (8, "EQUIP"), (10, 10.0), (20, -10.0), (11, 10.0), (21, 10.0)],
            [(0, "LINE"), (8, "EQUIP"), (10, 10.0), (20, 10.0), (11, -10.0), (21, 10.0)],
            [(0, "LINE"), (8, "EQUIP"), (10, -10.0), (20, 10.0), (11, -10.0), (21, -10.0)],
            [(0, "CIRCLE"), (8, "EQUIP"), (10, 0.0), (20, 0.0), (40, 6.0)],
        ],
    )
    builder.block(
        "闸阀",
        [
            [(0, "LINE"), (8, "EQUIP"), (10, -8.0), (20, -6.0), (11, 8.0), (21, 6.0)],
            [(0, "LINE"), (8, "EQUIP"), (10, -8.0), (20, -6.0), (11, 8.0), (21, -6.0)],
            [(0, "LINE"), (8, "EQUIP"), (10, 8.0), (20, -6.0), (11, -8.0), (21, 6.0)],
            [(0, "LINE"), (8, "EQUIP"), (10, -8.0), (20, 6.0), (11, 8.0), (21, -6.0)],
        ],
    )
    builder.block(
        "ZZZ-MYSTERY",
        [
            [(0, "LINE"), (8, "EQUIP"), (10, -8.0), (20, -8.0), (11, 8.0), (21, 8.0)],
            [(0, "LINE"), (8, "EQUIP"), (10, -8.0), (20, 8.0), (11, 8.0), (21, -8.0)],
        ],
    )
    builder.insert("centrifugal_pump", (125.0, 125.0))
    builder.insert("centrifugal_pump", (215.0, 125.0))
    builder.insert("闸阀", (125.0, 425.0))
    builder.insert("ZZZ-MYSTERY", (600.0, 600.0))
    builder.text("P-201", (125.0, 150.0), 2.5, layer="EQUIP")
    builder.text("P-202", (215.0, 150.0), 2.5, layer="EQUIP")
    builder.text("V-101", (125.0, 400.0), 2.5, layer="EQUIP")
    builder.text("PT-101", (700.0, 750.0), 2.5, layer="EQUIP")
    builder.text("工艺主流程", (300.0, 500.0), 3.0, layer="EQUIP")
    builder.line((135.0, 125.0), (205.0, 125.0), layer="EQUIP")
    builder.line((125.0, 135.0), (125.0, 419.0), layer="EQUIP")
    builder.extents((0.0, 0.0, 800.0, 800.0))
    return builder.build()


def test_the_governed_import_feeds_the_adapter(tmp_path, registry: SymbolRegistry) -> None:
    """P1: DXF -> governed import -> block clustering -> candidates, all on real paths."""

    store = SQLiteDocumentStore(tmp_path / "e2e.sqlite3")
    service = DocumentService(store, registry)
    result = CadImporter(service).import_bytes(
        _synthetic_dxf(), filename="synthetic_unit.dxf", options=CadImportOptions()
    )
    imported = service.get_document(result.document_id)
    assert all(not hasattr(element, "symbol_key") for element in imported.elements)

    adapter = M6SourceAdapter(store, registry)
    summary = adapter.ingest(
        adapter.pin_source(result.document_id), target_document_id=TARGET_ID
    )

    assert summary.cluster_count == 4  # two pump instances, one valve, one mystery block
    candidates = store.list_semantic_candidates()
    assert {c.proposed_semantics.symbol_class for c in _by_type(candidates, "symbol_class")} == {
        "centrifugal_pump",
        "gate_valve",
    }
    assert {c.proposed_semantics.equipment_tag for c in _by_type(candidates, "equipment_tag")} == {
        "P-201",
        "P-202",
        "V-101",
    }
    roles = _by_type(candidates, "annotation_role")
    assert sum(1 for c in roles if c.proposed_semantics.annotation_role == "equipment_label") == 3
    assert sum(1 for c in roles if c.proposed_semantics.annotation_role == "instrument_tag") == 1
    unresolved = _by_type(candidates, "unresolved")
    assert {c.proposed_semantics.unresolved_reason for c in unresolved} == {"out_of_catalog"}
    # The import is the only engineering write in sight.
    assert service.get_document(result.document_id).revision == result.revision == 1
    assert len(store.list_history(result.document_id)) == 1


def test_the_evidence_walkthrough_still_runs() -> None:
    """The D2 walkthrough is evidence, so it is executed rather than quoted."""

    script = Path(__file__).resolve().parents[2] / "scripts" / "m6_2b_d2_walkthrough.py"
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"identical": true' in result.stdout
    assert '"zero_engineering_write": true' in result.stdout
    assert "source_revision_drift" in result.stdout
    assert '"already_present": true' in result.stdout
    assert "target_document_id_required" in result.stdout
    assert "target_document_equals_source" in result.stdout
