"""M6 Phase-2A walkthrough: the governed chain, printed instead of described.

Run it and you get the two records the gate asks for without having to trust a summary:

* the **positive** record — one fact from artifact to a compiled patch, with the review
  decision, the finding and the digest it produces;
* the **negative** record — the same fact after somebody else moves the authoritative value,
  which must become a conflict and must not compile;
* the **determinism** record — a second compile of the same finding, which must produce the
  same patch identity;
* the **cascade** record — the drawing deleted, with the review history still there.

Deliberately reads nothing but the two public modules, so it is also a check that the evidence
is reachable from outside the test suite.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agentcad.engineering_ir import build_engineering_graph  # noqa: E402
from agentcad.m6_candidate_core import (  # noqa: E402
    CompilationRefused,
    GovernedWriteNotAuthorized,
    M6CandidateService,
    baseline_record,
)
from agentcad.m6_candidate_models import (  # noqa: E402
    CandidateEvidence,
    Confidence,
    ProducerRef,
    ProposedSemantics,
    ProvenanceRef,
    RegionGeometry,
    SemanticCandidate,
    SourceArtifactRef,
    SourceRegion,
    TextSpan,
)
from agentcad.models import (  # noqa: E402
    ConnectorElement,
    ConnectorEndpoint,
    Document,
    Layer,
    Point,
    SymbolElement,
)
from agentcad.store import SQLiteDocumentStore, StoredDocument  # noqa: E402
from agentcad.symbols import SymbolRegistry  # noqa: E402

DOCUMENT_ID = "doc_m6_walkthrough"
REVIEWER = "engineer.joe"


def _drawing(untagged_label: str = "") -> Document:
    untagged = SymbolElement(
        id="pump_untagged",
        symbol_key="centrifugal_pump",
        position=Point(x=100, y=100),
        width=60,
        height=60,
        label=untagged_label,
    )
    tagged = SymbolElement(
        id="pump_tagged",
        symbol_key="centrifugal_pump",
        position=Point(x=300, y=100),
        width=60,
        height=60,
        label="P-101",
    )
    line = ConnectorElement(
        id="line_a",
        source=ConnectorEndpoint(
            element_id="pump_untagged", port_id="discharge", point=Point(x=160, y=100)
        ),
        target=ConnectorEndpoint(
            element_id="pump_tagged", port_id="suction", point=Point(x=300, y=100)
        ),
        points=[Point(x=160, y=100), Point(x=300, y=100)],
        routing="manual",
    )
    return Document(
        id=DOCUMENT_ID,
        name="M6 walkthrough",
        revision=7,
        layers=[Layer(id="layer_default", name="Process")],
        elements=[untagged, tagged, line],
    )


def _candidate() -> SemanticCandidate:
    return SemanticCandidate(
        candidate_id="cand_tag_walkthrough",
        artifact=SourceArtifactRef(
            artifact_id="artifact_1",
            source_document_id=DOCUMENT_ID,
            source_revision=7,
            content_hash="deadbeef",
        ),
        region=SourceRegion(
            region_id="region_1",
            artifact_id="artifact_1",
            source_document_id=DOCUMENT_ID,
            source_revision=7,
            geometry_selector=RegionGeometry(x=100, y=100, width=60, height=60),
            element_refs=["pump_untagged"],
            text_spans=[TextSpan(text="P-201")],
        ),
        candidate_type="equipment_tag",
        proposed_semantics=ProposedSemantics(
            target_identity="element:pump_untagged", equipment_tag="P-201"
        ),
        confidence=Confidence(value=0.93, source="model"),
        evidence=[
            CandidateEvidence(
                kind="observed_text", detail="label reads P-201", observed_value="P-201"
            )
        ],
        producer=ProducerRef(key="typesafe", version="jev-1.13.0"),
        provenance=ProvenanceRef(provider="typesafe", model="jev", procedure_version="draw-v1"),
    )


def _print(title: str, payload: object) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=str))


def main() -> int:
    registry = SymbolRegistry()
    with tempfile.TemporaryDirectory() as tmp:
        store = SQLiteDocumentStore(Path(tmp) / "walkthrough.sqlite3")
        service = M6CandidateService(store)
        document = _drawing()
        store.save(StoredDocument(document=document, undo_stack=[], redo_stack=[]))

        candidate = _candidate()
        service.file_candidate(candidate)
        graph = build_engineering_graph(document, registry)
        baseline = baseline_record(
            document, graph, identity="element:pump_untagged", path="equipment_tag"
        )
        confirmation = service.confirm(
            candidate.candidate_id,
            reviewer_identity=REVIEWER,
            reviewer_action="saw the label P-201 on the pump",
            baseline=baseline,
        )
        decision = confirmation.decision
        finding = confirmation.finding
        patch = service.compile_finding(finding.finding_id, document=document, registry=registry)
        recompiled = service.compile_finding(
            finding.finding_id, document=document, registry=registry
        )

        _print(
            "positive record: artifact -> region -> candidate -> decision -> finding -> patch",
            {
                "candidate_id": candidate.candidate_id,
                "status_after_filing": service.decisions(candidate.candidate_id)[0].to_status,
                "review_decision_id": decision.review_decision_id,
                "reviewer": decision.reviewer_identity,
                "reviewer_action": decision.reviewer_action,
                "baseline": {
                    "revision": decision.baseline.baseline_revision if decision.baseline else None,
                    "path": decision.baseline.comparison_path if decision.baseline else None,
                    "value_present": decision.baseline.value_present if decision.baseline else None,
                    "digest_version": decision.baseline.digest_version if decision.baseline else None,
                    "value_digest": decision.baseline.value_digest if decision.baseline else None,
                },
                "finding_id": finding.finding_id,
                "provenance_chain": finding.provenance_chain,
                "patch_id": patch.patch_id,
                "canonical_digest": patch.canonical_digest,
                "intent": patch.intent,
                "policy_disposition": patch.policy_disposition,
                "operations": [op.model_dump(mode="json") for op in patch.operations],
                "write_authority": patch.write_authority,
            },
        )

        _print(
            "determinism record: the same finding compiled twice",
            {
                "patch_id_first": patch.patch_id,
                "patch_id_second": recompiled.patch_id,
                "digest_first": patch.canonical_digest,
                "digest_second": recompiled.canonical_digest,
                "identical": patch.patch_id == recompiled.patch_id
                and patch.canonical_digest == recompiled.canonical_digest,
            },
        )

        _print(
            "negative record: apply is refused in Phase-2A",
            _refusal(lambda: service.request_apply(patch.patch_id)),
        )

        # Somebody else tags the same pump before the write happens.
        moved = _drawing(untagged_label="P-777").model_copy(update={"revision": 8})
        moved_graph = build_engineering_graph(moved, registry)
        current = baseline_record(
            moved, moved_graph, identity="element:pump_untagged", path="equipment_tag"
        )
        conflict = service.recheck_baseline(candidate.candidate_id, current=current)
        _print(
            "negative record: a moved baseline forces a conflict",
            {
                "reviewed_value_digest": conflict.baseline.value_digest
                if conflict and conflict.baseline
                else None,
                "current_value_digest": current.value_digest,
                "status_after_reecheck": service.current_status(candidate.candidate_id),
                "conflict_recorded": conflict is not None,
                "compile_after_conflict": _refusal(
                    lambda: service.compile_finding(
                        finding.finding_id, document=moved, registry=registry
                    )
                ),
            },
        )

        deleted = store.delete(DOCUMENT_ID, expected_revision=document.revision)
        _print(
            "cascade record: the drawing is gone, the review history is not",
            {
                "document_deleted": deleted,
                "candidate_still_readable": store.get_semantic_candidate(candidate.candidate_id)
                is not None,
                "decisions_still_readable": len(
                    store.list_review_decisions(candidate.candidate_id)
                ),
                "finding_still_readable": store.get_confirmed_finding(finding.finding_id)
                is not None,
            },
        )
    return 0


def _refusal(action) -> dict[str, str]:
    try:
        action()
    except (CompilationRefused, GovernedWriteNotAuthorized) as refusal:
        return {"refused": type(refusal).__name__, "code": refusal.code, "reason": str(refusal)}
    raise AssertionError("this call was supposed to be refused")


if __name__ == "__main__":
    raise SystemExit(main())
