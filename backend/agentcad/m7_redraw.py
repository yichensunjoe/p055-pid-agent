"""The redraw boundary: replace one governed drawing with the next, atomically.

The design gate froze this shape, and every rule here is one of its clauses:

* The target proof is **rebuilt from the stored source**, not recalled from an audit digest:
  spec S_N re-runs the frozen chain to the expected prior materialization M_N, and the live
  document must match M_N exactly -- identity, text, geometry, waypoints, and system
  membership. Element-ID equality is not enough: a human may keep an id and move the thing.
* A stored spec whose chain versions no longer match this code is not recomputed under new
  rules: it is a migration, refused as ``semantic_source_version_requires_migration``.
* The replace is **one** existing-writer transaction: ``clear_document``, then the
  deterministic removal of the prior M7-owned non-default systems (``clear_document``
  deliberately does not touch system groups), then the complete new materialization's own
  operations. ``delete_system`` here removes a whole prior replace unit; it is not the
  element-level diff-patching the design gate forbade.
* The new revision's audit carries the full fresh identity chain plus three lineage edges
  (``edited_from_revision``, ``edited_from_spec_digest``,
  ``replaced_from_materialization_digest``) -- lineage, not a new identity axis -- and the
  new spec row commits in the same SQLite transaction as the revision itself.
"""

from __future__ import annotations

from typing import Any

from .audit_models import AuditContext
from .auto_layout_canvas import derive_semantic_canvas
from .auto_layout_geometry import (
    annotate_semantic_layout,
    materialize_semantic_layout,
    route_semantic_layout,
)
from .auto_layout_identity import finalize_semantic_layout
from .auto_layout_semantic import place_semantic_layout, plan_semantic_layout
from .m7_diagram_adapter import adapt
from .m7_diagram_spec import DiagramSpec, load_diagram_spec
from .m7_layout_materialization import (
    AddSystemOperation,
    MaterializationDocumentError,
    MaterializationError,
    MaterializedLayout,
    materialization_matches_document,
    materialize_canonical_layout,
    provenance_version_values,
    with_materialization_provenance,
)
from .m7_semantic_specs import (
    SemanticSourceVersionError,
    SemanticSpecRecord,
    build_spec_record,
)
from .m7_symbol_geometry import freeze_symbol_geometry
from .models import (
    ClearDocumentOperation,
    DeleteSystemOperation,
    Document,
    TransactionRequest,
)
from .service import DocumentService


class RedrawError(MaterializationError):
    """The redraw boundary refused: nothing was written."""


class RedrawTargetDriftError(RedrawError):
    """The live document is not exactly the prior materialization: foreign or human-edited
    content exists, and an automatic redraw must not destroy it."""

    code = "redraw_target_drift"


def rebuild_expected_materialization(record: SemanticSpecRecord) -> MaterializedLayout:
    """M_N rebuilt from the stored source S_N under the rules S_N records.

    The chain is the frozen one; the versions are checked first, because a drawing rebuilt
    under newer rules than its source recorded is a different drawing, and "looks the same"
    is not a reconciliation.
    """

    current_versions = provenance_version_values()
    mismatched = {
        name: (record.chain_versions.get(name), current_versions[name])
        for name in current_versions
        if record.chain_versions.get(name) != current_versions[name]
    }
    if mismatched:
        raise SemanticSourceVersionError(
            "stored spec at revision "
            f"{record.revision} records chain versions this code no longer runs: "
            + "; ".join(f"{k} stored {v[0]!r} vs current {v[1]!r}" for k, v in mismatched.items())
        )
    return materialize_finalized(_finalize_spec_layout(record.spec), document_id=record.document_id)


def _finalize_spec_layout(spec: DiagramSpec) -> Any:
    topology = adapt(load_diagram_spec(spec.model_dump(mode="json")))
    snapshot = freeze_symbol_geometry(entity.symbol_key for entity in spec.entities)
    staged = plan_semantic_layout(topology)
    placed = place_semantic_layout(staged)
    materialized = materialize_semantic_layout(placed, snapshot)
    routed = route_semantic_layout(materialized, snapshot)
    annotated = annotate_semantic_layout(
        routed, {entity.engineering_id: entity.tag for entity in spec.entities}
    )
    canvassed = derive_semantic_canvas(annotated)
    return finalize_semantic_layout(canvassed, topology)


def materialize_finalized(finalized: Any, *, document_id: str) -> MaterializedLayout:
    return materialize_canonical_layout(finalized, document_id=document_id)


def require_replaceable_target(
    document: Document,
    prior: MaterializedLayout,
) -> None:
    """The pre-write gate, stricter than element-id equality.

    Phase-3's own reconciliation -- identity, text, geometry, waypoints, both directions --
    plus the system membership: the document's non-default system groups must be exactly the
    prior materialization's engineering systems, no more, no less, no drift. Revision
    binding is not this check's job: the caller's stale guard and the writer's
    ``expected_revision`` own it, and a raced revision must fail atomically there.
    """

    problems = materialization_matches_document(prior, document)
    # Raw-element coverage on top: the row reconciliation only reads the element kinds a
    # materialization writes, so a foreign rectangle or note would be invisible to it. The
    # element-id sets must match exactly in both directions -- same id is not same content,
    # but different id sets are different drawings, full stop.
    expected_ids = {str(row["element_id"]) for row in prior.rows}
    actual_ids = {element.id for element in document.elements}
    for element_id in sorted(actual_ids - expected_ids):
        problems.append(
            f"the document holds element {element_id!r}, which the prior materialization "
            "never decided"
        )
    for element_id in sorted(expected_ids - actual_ids):
        problems.append(
            f"the document is missing element {element_id!r} the prior materialization decided"
        )
    expected_systems = _materialized_systems(prior)
    actual_systems = {
        (system.id, system.name)
        for system in document.systems
        if system.id != "system_default"
    }
    for system_id, _name in sorted(expected_systems - actual_systems):
        problems.append(f"the document is missing the materialized system {system_id!r}")
    for system_id, _name in sorted(actual_systems - expected_systems):
        problems.append(
            f"the document holds system {system_id!r} the prior materialization never decided"
        )
    if problems:
        raise RedrawTargetDriftError(
            "the current document is not exactly the prior materialization; an automatic "
            "redraw would destroy content it did not decide: " + "; ".join(problems[:5])
        )


def replace_transaction_operations(
    prior: MaterializedLayout, new: MaterializedLayout
) -> list:
    """One transaction's operations: clear, drop prior M7 systems, add the whole new drawing.

    ``clear_document`` deliberately leaves system groups alone, and ``add_system`` hard-fails
    on an existing id, so the prior drawing's own non-default systems must be removed first.
    This is the removal of one whole replace unit, not element-level patching.
    """

    operations: list = [ClearDocumentOperation()]
    for system_id, _name in sorted(_materialized_systems(prior)):
        operations.append(DeleteSystemOperation(system_id=system_id))
    operations.extend(new.operations)
    return operations


def _materialized_systems(layout: MaterializedLayout) -> set[tuple[str, str]]:
    """The M7-owned system groups a materialization writes, as (id, name) pairs.

    Read from the layout's own operations rather than a caller's memory: the operations are
    what the prior revision actually wrote, so the removal set can never drift from them.
    """

    return {
        (op.system.id, op.system.name)
        for op in layout.operations
        if isinstance(op, AddSystemOperation)
    }


def apply_redraw(
    service: DocumentService,
    *,
    document_id: str,
    expected_revision: int,
    prior: MaterializedLayout,
    new: MaterializedLayout,
    new_spec: DiagramSpec,
    base_record: SemanticSpecRecord,
    audit: AuditContext | None = None,
    label: str = "",
) -> Any:
    """Verify, replace atomically, and commit the new source beside the new revision."""

    current = service.get_document(document_id)
    if current.revision != expected_revision:
        raise MaterializationError(
            f"expected revision {expected_revision}, document {document_id!r} is at "
            f"{current.revision}"
        )
    require_replaceable_target(current, prior)

    lineage = {
        "edited_from_revision": str(base_record.revision),
        "edited_from_spec_digest": base_record.spec_digest,
        "replaced_from_materialization_digest": prior.materialization_digest,
    }
    context = audit or AuditContext(actor="system", surface="internal", tool_name="m7_redraw")
    context.metadata.update(lineage)
    # The fresh identity chain travels the same way the first materialization carries it:
    # attached here, from the compilation, never accepted from the caller.
    context = with_materialization_provenance(new, context)

    request = TransactionRequest(
        operations=replace_transaction_operations(prior, new),
        expected_revision=expected_revision,
        label=label or f"M7 redraw {new.materialization_digest[:12]}",
        source="system",
    )
    record = build_spec_record(
        new_spec,
        document_id=document_id,
        revision=expected_revision,
        chain_versions=provenance_version_values(),
    )
    result = service.apply_transaction(
        document_id,
        request,
        source="system",
        audit=context,
        semantic_spec=record,
    )
    problems = materialization_matches_document(new, result.document)
    if problems:
        raise MaterializationDocumentError(
            "the redrawn document does not cover the new layout: " + "; ".join(problems)
        )
    return result


__all__ = [
    "RedrawError",
    "RedrawTargetDriftError",
    "apply_redraw",
    "rebuild_expected_materialization",
    "replace_transaction_operations",
    "require_replaceable_target",
]
