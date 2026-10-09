"""M6 Phase-2B D2: the source evidence region, and how its identity is derived.

A region is *where in an imported artifact a judgement was made*. The contract
(:mod:`agentcad.m6_ingestion_contract`) fixes the identity rule: under the same
``source_document_id`` + ``source_revision`` the same evidence must be re-locatable, and a
changed source revision must produce a **new** region identity — a crop from a superseded
drawing can never stand as evidence about the new one.

So a region id is not assigned, it is derived:

    identity_prefix("region_id") + sha256 over the canonical payload of
    (artifact_id, source_revision, the three selector kinds' canonical content)

Three properties are enforced by construction rather than by convention:

* **Full width.** The digest is never truncated (the contract's ``IDENTITY_FULL_DIGEST_HEX``),
  and the prefix is read from the contract's declaration — this module never spells it.
* **Order-independence.** ``element_refs`` and ``text_spans`` describe a *set* of evidence, so
  both are canonicalized before hashing; the same region derived through a different traversal
  order still gets the same identity.
* **Volatility exclusion.** No timestamp, no document name, no style enters the payload — the
  A5/replay lesson is that an identity is exactly the list of fields you remembered to leave
  out. The selector content is the whole identity.

This module also owns the geometry reading a region needs (element bounding boxes and their
union), because "re-crop the same evidence" is only testable if the crop is computed by one
canonical function rather than re-derived per caller.
"""

from __future__ import annotations

from typing import Any

from .m6_candidate_core import canonical_digest
from .m6_candidate_models import (
    RegionGeometry,
    SourceArtifactRef,
    SourceRegion,
    TextSpan,
)
from .m6_ingestion_contract import identity_prefix
from .models import Element

#: Versioned for the same reason the review-decision id carries one: a later scheme change
#: must be identifiable by re-derivation, never silently compared against the old scheme.
REGION_IDENTITY_VERSION = "source-region-v1"

#: A degenerate selector (a vertical line has zero width) still has to be a legal
#: ``RegionGeometry``; the padding is a declared constant so it is part of the scheme, not an
#: improvisation at the call site.
MIN_REGION_EXTENT = 1e-6


def element_bbox(element: Element) -> tuple[float, float, float, float]:
    """The document-frame bounding box of one element, as ``(x0, y0, x1, y1)``.

    Text contributes its anchor point only: the editor stores no text extents, and inventing
    a width model here would make the region claim to know something the import did not keep.
    """

    if element.type == "line":
        xs = (element.start.x, element.end.x)
        ys = (element.start.y, element.end.y)
        return min(xs), min(ys), max(xs), max(ys)
    if element.type == "polyline":
        xs = [point.x for point in element.points]
        ys = [point.y for point in element.points]
        return min(xs), min(ys), max(xs), max(ys)
    if element.type == "rectangle":
        return element.x, element.y, element.x + element.width, element.y + element.height
    if element.type == "circle":
        return (
            element.center.x - element.radius,
            element.center.y - element.radius,
            element.center.x + element.radius,
            element.center.y + element.radius,
        )
    if element.type == "symbol":
        return (
            element.position.x,
            element.position.y,
            element.position.x + element.width,
            element.position.y + element.height,
        )
    if element.type == "junction":
        return (
            element.position.x - element.radius,
            element.position.y - element.radius,
            element.position.x + element.radius,
            element.position.y + element.radius,
        )
    if element.type == "connector":
        xs = [point.x for point in element.points]
        ys = [point.y for point in element.points]
        return min(xs), min(ys), max(xs), max(ys)
    # text: the anchor point is all that survived the import.
    return element.position.x, element.position.y, element.position.x, element.position.y


def union_bbox(
    boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    """The union of boxes, or ``None`` for an empty input (a region with no geometry)."""

    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def bbox_geometry(box: tuple[float, float, float, float]) -> RegionGeometry:
    """A bounding box as a ``RegionGeometry`` — degenerate extents padded by the declared floor."""

    x0, y0, x1, y1 = box
    return RegionGeometry(
        x=x0,
        y=y0,
        width=max(x1 - x0, MIN_REGION_EXTENT),
        height=max(y1 - y0, MIN_REGION_EXTENT),
    )


def _geometry_payload(geometry: RegionGeometry | None) -> dict[str, float] | None:
    if geometry is None:
        return None
    return {"x": geometry.x, "y": geometry.y, "width": geometry.width, "height": geometry.height}


def _span_payload(span: TextSpan) -> dict[str, Any]:
    return {"text": span.text, "bounds": _geometry_payload(span.bounds)}


def region_identity_payload(
    *,
    artifact_id: str,
    source_revision: int,
    geometry_selector: RegionGeometry | None,
    element_refs: list[str] | tuple[str, ...],
    text_spans: list[TextSpan] | tuple[TextSpan, ...],
) -> dict[str, Any]:
    """The canonical content a region identity is derived from.

    Frame fields (``page``/``layer``/``coordinate_frame``) are deliberately *not* identity:
    they re-locate the crop, and two crops of the same evidence with different frame metadata
    are the same region. The artifact + pinned revision + selector content is the identity —
    the same rule the contract states for why a revision change must produce a new one.
    """

    return {
        "identity": REGION_IDENTITY_VERSION,
        "artifact_id": artifact_id,
        "source_revision": source_revision,
        "geometry_selector": _geometry_payload(geometry_selector),
        "element_refs": sorted(element_refs),
        "text_spans": sorted(
            (_span_payload(span) for span in text_spans),
            key=canonical_digest,
        ),
    }


def region_identity(
    *,
    artifact_id: str,
    source_revision: int,
    geometry_selector: RegionGeometry | None,
    element_refs: list[str] | tuple[str, ...],
    text_spans: list[TextSpan] | tuple[TextSpan, ...],
) -> str:
    """The persistent identity of one evidence region: declared prefix + full digest."""

    return identity_prefix("region_id") + canonical_digest(
        region_identity_payload(
            artifact_id=artifact_id,
            source_revision=source_revision,
            geometry_selector=geometry_selector,
            element_refs=element_refs,
            text_spans=text_spans,
        )
    )


def build_source_region(
    *,
    artifact: SourceArtifactRef,
    geometry_selector: RegionGeometry | None,
    element_refs: list[str],
    text_spans: list[TextSpan] | None = None,
    layer: str = "",
) -> SourceRegion:
    """A region whose identity is derived, never assigned.

    The identity is computed from the same values the region carries, so a reader can always
    re-derive it — and a region whose stored id disagrees with its selectors is detectably
    corrupt rather than silently trusted.
    """

    spans = list(text_spans or [])
    return SourceRegion(
        region_id=region_identity(
            artifact_id=artifact.artifact_id,
            source_revision=artifact.source_revision,
            geometry_selector=geometry_selector,
            element_refs=element_refs,
            text_spans=spans,
        ),
        artifact_id=artifact.artifact_id,
        source_document_id=artifact.source_document_id,
        source_revision=artifact.source_revision,
        geometry_selector=geometry_selector,
        element_refs=element_refs,
        text_spans=spans,
        layer=layer,
        coordinate_frame="document",
    )


def region_id_is_current(region: SourceRegion) -> bool:
    """Whether the stored id is exactly what the region's own content re-derives to."""

    return region.region_id == region_identity(
        artifact_id=region.artifact_id,
        source_revision=region.source_revision,
        geometry_selector=region.geometry_selector,
        element_refs=region.element_refs,
        text_spans=region.text_spans,
    )


__all__ = [
    "MIN_REGION_EXTENT",
    "REGION_IDENTITY_VERSION",
    "bbox_geometry",
    "build_source_region",
    "element_bbox",
    "region_id_is_current",
    "region_identity",
    "region_identity_payload",
    "union_bbox",
]
