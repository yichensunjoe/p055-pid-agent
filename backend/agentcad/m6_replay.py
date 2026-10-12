"""M6-2B-D4: the replay harness.

Replay answers one question against frozen inputs: does re-running the chain reproduce what
was committed? Per the contract's replay ruling, two layers must match and raw transaction
bytes are explicitly NOT the contract (volatile provenance — transaction ids, timestamps,
audit times — never enters the comparison):

1. **canonical patch**: the stored patch's findings are recompiled against the reconstructed
   baseline and must reproduce the stored ``canonical_digest`` (and therefore the patch id);
2. **semantic post-state**: the stored patch's operations are re-staged onto the
   reconstructed baseline, and the resulting semantic value at the comparison key must match
   the post-apply digest recorded in the apply audit evidence.

The baseline is reconstructed from the target document's undo chain (each entry is the
document payload before that write). A baseline too old to reconstruct is reported as such —
never silently compared against the wrong state.
"""

from __future__ import annotations

import json
from typing import Any

from .engineering_ir import build_engineering_graph
from .m6_apply_runtime import parse_operations
from .m6_candidate_core import M6CandidateService, semantic_value, value_digest
from .models import Document, StrictModel
from .service import DocumentService
from .store import SQLiteDocumentStore
from .symbols import SymbolRegistry


class ReplayReport(StrictModel):
    """The two-layer replay verdict, as data."""

    patch_id: str
    apply_id: str = ""
    canonical_patch_match: bool = False
    canonical_patch_stored: str = ""
    canonical_patch_recomputed: str = ""
    baseline_reconstructable: bool = False
    semantic_poststate_match: bool | None = None
    semantic_poststate_recorded: str = ""
    semantic_poststate_recomputed: str = ""
    ok: bool = False


def _reconstruct_baseline(
    document: Document, undo_stack: list[dict[str, Any]], baseline_revision: int
) -> Document | None:
    """The target document at ``baseline_revision``, from the persisted undo chain."""

    if document.revision == baseline_revision:
        return document
    for entry in reversed(undo_stack):
        candidate = Document.model_validate(entry)
        if candidate.revision == baseline_revision:
            return candidate
    return None


def replay_patch(
    store: SQLiteDocumentStore,
    service: DocumentService,
    registry: SymbolRegistry,
    patch_id: str,
    *,
    apply_id: str = "",
) -> ReplayReport:
    """Replay one patch through the frozen chain and compare both layers."""

    patch_row = store.get_structured_patch(patch_id)
    if patch_row is None:
        return ReplayReport(patch_id=patch_id)
    report = ReplayReport(patch_id=patch_id, apply_id=apply_id)

    apply_row = (
        store.get_apply_record(apply_id)
        if apply_id
        else next(
            (
                row
                for row in store.list_apply_records(patch_id=patch_id)
                if row["status"] == "applied"
            ),
            None,
        )
    )
    target_id = str(patch_row["target_document_id"])
    stored = store.get(target_id)
    if stored is None:
        return report
    baseline = _reconstruct_baseline(
        stored.document, stored.undo_stack, int(patch_row["baseline_revision"])
    )
    report.baseline_reconstructable = baseline is not None

    # Layer 1: recompile the findings against the reconstructed baseline; the digest must
    # reproduce the stored one (volatile provenance never enters it).
    candidates = M6CandidateService(store)
    if baseline is not None:
        findings = [
            store.get_confirmed_finding(finding_id)
            for finding_id in json.loads(str(patch_row["finding_ids_json"]))
        ]
        if findings and all(finding is not None for finding in findings):
            digests = [
                candidates.compile_finding(
                    finding.finding_id, document=baseline, registry=registry
                ).canonical_digest
                for finding in findings
                if finding is not None
            ]
            if len(digests) == 1:
                report.canonical_patch_recomputed = digests[0]
    report.canonical_patch_stored = str(patch_row["canonical_digest"])
    report.canonical_patch_match = bool(report.canonical_patch_recomputed) and (
        report.canonical_patch_recomputed == report.canonical_patch_stored
    )

    # Layer 2: re-stage the stored operations onto the baseline; the semantic value at the
    # comparison key must match the digest the apply recorded.
    if apply_row is not None and baseline is not None:
        apply_audit = store.get_audit_record(str(apply_row["audit_record_id"]))
        evidence = apply_audit.evidence.get("m6", {}) if apply_audit is not None else {}
        recorded = str(evidence.get("post_value_digest", ""))
        identity = str(evidence.get("semantic_identity", ""))
        path = str(evidence.get("semantic_path", "existence"))
        report.semantic_poststate_recorded = recorded
        if recorded and identity:
            operations = parse_operations(json.loads(str(patch_row["operations_json"])))
            staged = service._stage_mutation(baseline, operations)  # noqa: SLF001 - replay re-stages in memory, never writes
            recomputed_value = value_digest(
                path,  # type: ignore[arg-type]
                semantic_value(
                    staged,
                    build_engineering_graph(staged, registry),
                    identity,
                    path,  # type: ignore[arg-type]
                ),
            )
            report.semantic_poststate_recomputed = recomputed_value
            report.semantic_poststate_match = recomputed_value == recorded
    report.ok = report.canonical_patch_match and (
        report.semantic_poststate_match in (True, None)
    )
    return report


__all__ = ["ReplayReport", "replay_patch"]
