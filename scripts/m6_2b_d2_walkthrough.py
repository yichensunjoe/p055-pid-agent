"""M6-2B-D2 walkthrough: a real governed import feeds the source-to-candidate adapter.

Run it and the four records the D2 gate asks for are printed, not summarised:

* the **intake** record — a synthetic DXF enters through the unchanged governed import (one
  revision, one history entry, one audit record), then the adapter pins the source triple,
  recovers block-instance regions, and files deterministic candidates with ``m6reg_``
  identities;
* the **determinism** record — the same source + target + catalogue + rules are derived
  twice and re-ingested once: digests are identical and the second filing is a pure no-op;
* the **zero-write** record — the engineering document's revision, history and audit chain
  are exactly what the import left behind;
* the **refusal** record — a moved source revision, a deleted source and an unknown block
  are each answered with a stable code and no writes.

The DXF is authored inline (the format is documented and line-oriented) so the evidence
depends on nothing outside the repository. The drawing is synthetic; per the Gate's Q4
ruling that is all this slice may use.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agentcad.audit_models import AuditContext  # noqa: E402
from agentcad.cad_import import CadImporter  # noqa: E402
from agentcad.engineering_ir import document_content_hash  # noqa: E402
from agentcad.m6_source_adapter import (  # noqa: E402
    M6SourceAdapter,
    SourceVerificationError,
    TargetDocumentError,
)
from agentcad.service import DocumentService  # noqa: E402
from agentcad.store import SQLiteDocumentStore  # noqa: E402
from agentcad.symbols import SymbolRegistry  # noqa: E402

TARGET_DOCUMENT_ID = "doc_rebuild_target"


def _record(kind: str, items: list[tuple[int, object]]) -> list[tuple[int, object]]:
    return [(0, kind), *items]


def _synthetic_dxf() -> bytes:
    """A regeneration skidsheet: two pumps, one gate valve, one unknown block, tags."""

    lines: list[str] = []

    def emit(*records: tuple[int, object]) -> None:
        for code, value in records:
            lines.extend([str(code), str(value)])

    emit((0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1032"))
    emit((9, "$DWGCODEPAGE"), (3, "UTF-8"))
    emit((9, "$EXTMIN"), (10, 0.0), (20, 0.0), (30, 0.0))
    emit((9, "$EXTMAX"), (10, 800.0), (20, 800.0), (30, 0.0))
    emit((0, "ENDSEC"))
    emit((0, "SECTION"), (2, "TABLES"), (0, "TABLE"), (2, "LAYER"))
    emit((0, "LAYER"), (2, "0"), (70, 0), (62, 7), (6, "CONTINUOUS"))
    emit((0, "LAYER"), (2, "EQUIP"), (70, 0), (62, 5), (6, "CONTINUOUS"))
    emit((0, "ENDTAB"), (0, "ENDSEC"))

    blocks = {
        # A pump-ish outline: a box with a circle inside.
        "centrifugal_pump": [
            _record("LINE", [(8, "EQUIP"), (10, -10.0), (20, -10.0), (11, 10.0), (21, -10.0)]),
            _record("LINE", [(8, "EQUIP"), (10, 10.0), (20, -10.0), (11, 10.0), (21, 10.0)]),
            _record("LINE", [(8, "EQUIP"), (10, 10.0), (20, 10.0), (11, -10.0), (21, 10.0)]),
            _record("LINE", [(8, "EQUIP"), (10, -10.0), (20, 10.0), (11, -10.0), (21, -10.0)]),
            _record("CIRCLE", [(8, "EQUIP"), (10, 0.0), (20, 0.0), (40, 6.0)]),
        ],
        # The gate valve matched through its Chinese catalogue name.
        "闸阀": [
            _record("LINE", [(8, "EQUIP"), (10, -8.0), (20, -6.0), (11, 8.0), (21, 6.0)]),
            _record("LINE", [(8, "EQUIP"), (10, -8.0), (20, -6.0), (11, 8.0), (21, -6.0)]),
            _record("LINE", [(8, "EQUIP"), (10, 8.0), (20, -6.0), (11, -8.0), (21, 6.0)]),
            _record("LINE", [(8, "EQUIP"), (10, -8.0), (20, 6.0), (11, 8.0), (21, -6.0)]),
        ],
        # Not in the catalogue: must come back as out_of_catalog, never a guess.
        "ZZZ-MYSTERY": [
            _record("LINE", [(8, "EQUIP"), (10, -8.0), (20, -8.0), (11, 8.0), (21, 8.0)]),
            _record("LINE", [(8, "EQUIP"), (10, -8.0), (20, 8.0), (11, 8.0), (21, -8.0)]),
        ],
    }
    emit((0, "SECTION"), (2, "BLOCKS"))
    for name, entities in blocks.items():
        emit((0, "BLOCK"), (8, "0"), (2, name), (70, 0), (10, 0.0), (20, 0.0), (30, 0.0))
        for entity in entities:
            emit(*entity)
        emit((0, "ENDBLK"), (8, "0"))
    emit((0, "ENDSEC"))

    emit((0, "SECTION"), (2, "ENTITIES"))
    for name, at in (
        ("centrifugal_pump", (125.0, 125.0)),
        ("centrifugal_pump", (215.0, 125.0)),
        ("闸阀", (125.0, 425.0)),
        ("ZZZ-MYSTERY", (600.0, 600.0)),
    ):
        emit(*_record("INSERT", [(8, "EQUIP"), (2, name), (10, at[0]), (20, at[1]), (30, 0.0)]))
    for text, at, height in (
        ("P-201", (125.0, 150.0), 2.5),
        ("P-202", (215.0, 150.0), 2.5),
        ("V-101", (125.0, 400.0), 2.5),
        ("PT-101", (700.0, 750.0), 2.5),  # instrument-shaped, far from any cluster
        ("Q-500", (170.0, 150.0), 2.5),  # exactly between the two pumps: a tie
        ("工艺主流程", (300.0, 500.0), 3.0),  # a note, not a tag
    ):
        emit(
            *_record(
                "TEXT",
                [(8, "EQUIP"), (10, at[0]), (20, at[1]), (30, 0.0), (40, height), (1, text)],
            )
        )
    for start, end in (((135.0, 125.0), (205.0, 125.0)), ((125.0, 135.0), (125.0, 419.0))):
        emit(
            *_record(
                "LINE",
                [(8, "EQUIP"), (10, start[0]), (20, start[1]), (11, end[0]), (21, end[1])],
            )
        )
    emit((0, "ENDSEC"), (0, "EOF"))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _print(title: str, payload: object) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=str))


def _refusal(action) -> dict[str, str]:
    try:
        action()
    except (SourceVerificationError, TargetDocumentError) as refusal:
        return {"refused": type(refusal).__name__, "code": refusal.code, "reason": str(refusal)}
    raise AssertionError("this call was supposed to be refused")


def main() -> int:
    registry = SymbolRegistry()
    with tempfile.TemporaryDirectory() as tmp:
        store = SQLiteDocumentStore(Path(tmp) / "walkthrough.sqlite3")
        service = DocumentService(store, registry)

        # 1. The unchanged governed import: one revision, one history entry, one audit.
        imported = CadImporter(service).import_bytes(
            _synthetic_dxf(),
            filename="regeneration_skid.dxf",
            audit=AuditContext(
                actor="m6-2b-d2-walkthrough", surface="internal", tool_name="import_cad_drawing"
            ),
        )
        source_id = imported.document_id
        document = service.get_document(source_id)
        baseline = {
            "revision": document.revision,
            "history_entries": len(store.list_history(source_id)),
            "audit_records": len(store.all_audit_records()),
            "content_hash": document_content_hash(document),
        }

        # 2. Intake: pin the source, verify the pin, derive, file — one atomic batch.
        adapter = M6SourceAdapter(store, registry)
        artifact = adapter.pin_source(source_id)
        adapter.verify_source(artifact)
        summary = adapter.ingest(artifact, target_document_id=TARGET_DOCUMENT_ID)
        candidates = store.list_semantic_candidates(source_document_id=source_id)
        _print(
            "intake record: import -> pinned source -> regions -> candidates",
            {
                "source_document_id": source_id,
                "source_revision": artifact.source_revision,
                "source_content_hash": artifact.content_hash,
                "target_document_id": summary.target_document_id,
                "elements": len(document.elements),
                "clusters": summary.cluster_count,
                "tag_texts": summary.tag_text_count,
                "unclassified_texts": summary.unclassified_text_count,
                "blockless_geometry": summary.blockless_geometry_count,
                "rules": summary.rules,
                "catalogue_fingerprint": summary.catalogue_fingerprint,
                "candidates": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "candidate_type": candidate.candidate_type,
                        "facts": {
                            key: value
                            for key, value in candidate.proposed_semantics.model_dump(
                                mode="json"
                            ).items()
                            if value not in ("", None)
                        },
                        "region_id": candidate.region.region_id,
                        "producer": candidate.producer.key,
                        "confidence": candidate.confidence.value,
                        "target_document_id": candidate.target_document_id,
                    }
                    for candidate in candidates
                ],
                "counts_by_type": summary.counts_by_type,
                "extraction_digest": summary.extraction_digest,
            },
        )

        # 3. Determinism: derive twice, re-ingest once — identical digests, no new rows.
        second_pass = adapter.derive_candidates(
            document, artifact, target_document_id=TARGET_DOCUMENT_ID
        )
        second_digest = adapter.extraction_digest(
            artifact, TARGET_DOCUMENT_ID, second_pass
        )
        refiled = adapter.ingest(artifact, target_document_id=TARGET_DOCUMENT_ID)
        _print(
            "determinism record: same source + target + catalogue + rules",
            {
                "digest_first": summary.extraction_digest,
                "digest_second": second_digest,
                "identical": summary.extraction_digest == second_digest,
                "refile_filed": len(refiled.filed),
                "already_present": len(refiled.already_present) == len(summary.candidate_ids),
            },
        )

        # 4. Zero engineering write: the document, its history and the audit chain
        #    are exactly what the governed import left behind.
        after = service.get_document(source_id)
        _print(
            "zero-write record: ingestion never touches the engineering plane",
            {
                "zero_engineering_write": after.revision == baseline["revision"]
                and len(store.list_history(source_id)) == baseline["history_entries"]
                and len(store.all_audit_records()) == baseline["audit_records"]
                and document_content_hash(after) == baseline["content_hash"],
                "revision": after.revision,
                "history_entries": len(store.list_history(source_id)),
                "audit_records": len(store.all_audit_records()),
                "candidates_filed": len(summary.filed),
            },
        )

        # 5. Refusals: a moved revision, a deleted source, an unknown block — and the two
        #    target-identity guards a new ingestion must pass (D88-3).
        drifted = artifact.model_copy(update={"source_revision": artifact.source_revision + 1})
        _print(
            "refusal record: drift and catalogue gaps are stable codes, zero writes",
            {
                "moved_revision": _refusal(lambda: adapter.verify_source(drifted)),
                "deleted_source": _refusal(
                    lambda: adapter.verify_source(
                        artifact.model_copy(update={"source_document_id": "doc_deleted"})
                    )
                ),
                "stale_pin_at_ingest": _refusal(
                    lambda: adapter.ingest(drifted, target_document_id=TARGET_DOCUMENT_ID)
                ),
                "empty_target": _refusal(
                    lambda: adapter.ingest(artifact, target_document_id="")
                ),
                "target_is_source": _refusal(
                    lambda: adapter.ingest(artifact, target_document_id=source_id)
                ),
                "unknown_block_candidate": next(
                    candidate.proposed_semantics.unresolved_reason
                    for candidate in candidates
                    if candidate.candidate_type == "unresolved"
                    and candidate.proposed_semantics.unresolved_reason == "out_of_catalog"
                ),
                "ambiguous_tie_candidate": next(
                    candidate.proposed_semantics.unresolved_reason
                    for candidate in candidates
                    if candidate.candidate_type == "unresolved"
                    and candidate.proposed_semantics.unresolved_reason == "ambiguous"
                ),
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
