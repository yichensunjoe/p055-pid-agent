"""M6 Phase-2B D2: the source-to-candidate adapter.

The chain this module completes is the first three layers of the M6 contract, against a
*real* imported document instead of a hand-built fixture:

    imported document (source, read-only evidence)
      -> source verification (existence + pinned revision + content hash)
      -> region derivation (block clusters + text anchors)
      -> deterministic extraction (symbol class / equipment tag / annotation role)
      -> candidate filing through the existing governed core

Every rule here is deterministic and declared, and the failure modes are the point:

* **The source is verified, never trusted.** ``pin_source`` reads the document and freezes
  ``(document id, revision, content hash)``; ``verify_source`` re-reads and refuses drift
  with a stable code and **zero writes**. The content hash is ``document_content_hash`` —
  the same engineering-content hash family the audit/index side already uses.
* **A block cluster is recovered, not assumed.** The importer keeps ``cad_block``/
  ``cad_handle`` provenance on every expanded element, but the handle belongs to the block
  *definition*, so two instances of one block share it. Instances are therefore recovered as
  touch-connected same-block geometry under a declared seam tolerance, plus a declared
  containment merge for parts that float inside an outline — never merged across distance,
  never split by traversal order.
* **Ambiguity is a recorded outcome, not a coin flip.** A tie in symbol matching, two tags
  on one cluster, or a tag equidistant from two clusters produces an ``unresolved``
  candidate that says why. Nothing here ever picks one reading to keep the queue tidy.
* **The only producer wired is ``deterministic_rule_engine``** (Gate Q3). TypeSafe/LLM
  producers keep their contract-declared seats; this slice does not connect them.

Nothing in this module writes the engineering model: the only store writes are the existing
insert-only M6 review tables, through the unchanged ``M6CandidateService.file_candidate``
(born ``proposed``, empty auto-accept whitelist). No HTTP route, no MCP tool.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .engineering_ir import document_content_hash
from .m6_candidate_core import (
    M6CandidateRepository,
    M6CandidateService,
    M6CoreError,
    canonical_digest,
)
from .m6_candidate_models import (
    CandidateEvidence,
    Confidence,
    FactKind,
    ProducerRef,
    ProposedSemantics,
    ProvenanceRef,
    SemanticCandidate,
    SourceArtifactRef,
    SourceRegion,
    TextSpan,
)
from .m6_ingestion_contract import REPLAY_VOLATILE_FIELDS_EXCLUDED
from .m6_region import bbox_geometry, build_source_region, element_bbox, union_bbox
from .models import Document, Element, StrictModel
from .store import StoredDocument
from .symbols import SymbolRegistry

M6_SOURCE_ADAPTER = "m6.source_adapter"
M6_SOURCE_ADAPTER_VERSION = "0.1.0"

#: The extraction rule bundle, versioned like the patch compiler's rules: replay compares
#: digests computed under one rules version, and a rule change is a visible event.
M6_SOURCE_RULES = "m6-source-rules/1"

#: A text element whose entire content is a tag of the form XX-### (letters, dash, digits,
#: optional letter suffix). A *substring* match would read tags into sentences; anchoring the
#: whole text is what keeps "工艺入口" from becoming a position number.
TAG_TEXT_PATTERN = re.compile(r"^\s*([A-Za-z]{1,5}-\d{2,5}[A-Za-z]?)\s*$")

#: How far a tag may sit from its cluster and still be bound to it, in document units:
#: a declared floor plus a term proportional to the tag's own size (a bigger drawing writes
#: bigger text and spaces its labels wider).
TAG_BINDING_MIN_REACH = 40.0
TAG_BINDING_REACH_FONT_MULTIPLE = 6.0

#: Seam tolerance for recovering one block instance as connected geometry (document units).
#: Import rounds coordinates to 6 decimals, so shared corners are bit-identical; the gap only
#: absorbs independently authored endpoints that were meant to meet.
CLUSTER_LINK_GAP = 0.25

#: A symbol's parts need not *touch*: a motor circle floats inside its enclosure outline.
#: After touch-connectivity, a same-block group fully contained in another group's bounding
#: box joins it. Bounding-box containment is an approximation (an L-shaped outline contains
#: points it does not enclose), declared as such; it can only merge geometry from one block
#: name, never across blocks.

#: Distances are quantized before comparison so a symmetric layout is an exact tie — a tie is
#: an ambiguity record, never a floating-point coin flip.
_DISTANCE_DECIMALS = 6

#: Declared ISA-style instrument tag shape: a measured-variable first letter followed by a
#: function letter (PT/TT/FT/LT/PI/…). A single letter (P-201) or an unlisted pair (TK-101)
#: is deliberately *not* an instrument reading — a conservative miss is reviewable, an
#: invented one is not.
INSTRUMENT_FIRST_LETTERS = frozenset("PFLT")
INSTRUMENT_SUCCEEDING_LETTERS = frozenset("TICSEVRAGY")


class SourceVerificationError(M6CoreError):
    """The pinned source evidence could not be re-verified. Always zero-write."""

    code = "source_verification_error"


class CandidateIdentityConflict(M6CoreError):
    """A candidate id already exists with *different* content — a derivation contradiction."""

    code = "candidate_identity_conflict"


class M6SourceStore(M6CandidateRepository, Protocol):
    """What the adapter needs from the store: the candidate repository plus document reads."""

    def get(self, document_id: str) -> StoredDocument | None: ...


#: Keys excluded from replay digests and re-ingest comparison. The set is the contract's
#: replay exclusion plus the timestamps the models stamp at birth — the same lesson the patch
#: compiler encodes: bookkeeping must never change an identity.
_VOLATILE_KEYS = frozenset(
    {*REPLAY_VOLATILE_FIELDS_EXCLUDED, "created_at", "decided_at", "confirmed_at", "imported_at"}
)


def _without_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_volatile(item) for key, item in value.items() if key not in _VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_without_volatile(item) for item in value]
    return value


def _is_instrument_tag(tag: str) -> bool:
    letters = tag.split("-", 1)[0].upper()
    return (
        len(letters) >= 2
        and letters[0] in INSTRUMENT_FIRST_LETTERS
        and letters[1] in INSTRUMENT_SUCCEEDING_LETTERS
    )


def _point_rect_distance(px: float, py: float, box: tuple[float, float, float, float]) -> float:
    dx = max(box[0] - px, 0.0, px - box[2])
    dy = max(box[1] - py, 0.0, py - box[3])
    return math.hypot(dx, dy)


def _boxes_touch(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    gap: float,
) -> bool:
    return (
        a[0] - gap <= b[2]
        and b[0] - gap <= a[2]
        and a[1] - gap <= b[3]
        and b[1] - gap <= a[3]
    )


def _box_contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
) -> bool:
    """``inner`` lies entirely inside ``outer`` (exact compare on import-rounded values)."""

    return (
        outer[0] <= inner[0]
        and inner[2] <= outer[2]
        and outer[1] <= inner[1]
        and inner[3] <= outer[3]
    )


def _find_root(parent: dict[str, str], element_id: str) -> str:
    while parent[element_id] != element_id:
        parent[element_id] = parent[parent[element_id]]
        element_id = parent[element_id]
    return element_id


@dataclass(frozen=True)
class _Cluster:
    """One recovered symbol instance: same block name, spatially connected geometry."""

    block: str
    element_ids: tuple[str, ...]
    bbox: tuple[float, float, float, float]
    layer: str


@dataclass(frozen=True)
class _TagText:
    element: Element
    tag: str


@dataclass(frozen=True)
class _Binding:
    """How one tag text relates to the clusters: uniquely bound, tied, or out of reach."""

    status: str  # "unique" | "ambiguous" | "unbound"
    clusters: tuple[_Cluster, ...]
    distance: float | None


@dataclass(frozen=True)
class _Derivation:
    candidates: list[SemanticCandidate]
    cluster_count: int
    tag_text_count: int
    unclassified_text_count: int
    blockless_geometry_count: int


class IngestionSummary(StrictModel):
    """What one intake run did, as data a reviewer can replay instead of a log line."""

    artifact: SourceArtifactRef
    target_document_id: str
    producer: ProducerRef
    rules: str
    catalogue_fingerprint: str
    cluster_count: int
    tag_text_count: int
    unclassified_text_count: int
    blockless_geometry_count: int
    candidate_ids: list[str]
    filed: list[str]
    already_present: list[str]
    counts_by_type: dict[str, int]
    extraction_digest: str


class M6SourceAdapter:
    """Reads an imported document, derives deterministic candidates, files them. No surface."""

    def __init__(self, store: M6SourceStore, registry: SymbolRegistry) -> None:
        self._store = store
        self._registry = registry
        self._candidates = M6CandidateService(store)

    # -- source evidence identity ------------------------------------------------------

    def pin_source(self, source_document_id: str) -> SourceArtifactRef:
        """Freeze the source triple at the observation moment. Read-only."""

        document = self._read_source(source_document_id)
        return self._artifact_for(document)

    def verify_source(self, artifact: SourceArtifactRef) -> Document:
        """Re-verify pinned evidence before anyone relies on it. Zero writes on any path.

        A missing document, a moved revision or a changed content hash each get their own
        stable refusal code, and nothing is ever silently re-pinned: evidence that drifted is
        evidence for a *new* ingestion, produced by a new import, under a new identity.
        """

        stored = self._store.get(artifact.source_document_id)
        if stored is None:
            raise SourceVerificationError(
                f"source document {artifact.source_document_id!r} no longer exists; the pinned "
                "snapshot cannot be re-verified",
                code="source_snapshot_unavailable",
            )
        document = stored.document
        if document.revision != artifact.source_revision:
            raise SourceVerificationError(
                f"source document {artifact.source_document_id!r} moved from pinned revision "
                f"{artifact.source_revision} to {document.revision}; the old evidence identity "
                "is sealed and cannot be re-pinned in place",
                code="source_revision_drift",
            )
        if not artifact.content_hash:
            raise SourceVerificationError(
                "the artifact carries no content hash, so there is nothing to verify against",
                code="source_snapshot_unavailable",
            )
        if document_content_hash(document) != artifact.content_hash:
            raise SourceVerificationError(
                f"source document {artifact.source_document_id!r} content hash drifted at the "
                f"pinned revision {artifact.source_revision}",
                code="source_content_drift",
            )
        return document

    def _read_source(self, source_document_id: str) -> Document:
        stored = self._store.get(source_document_id)
        if stored is None:
            raise SourceVerificationError(
                f"source document {source_document_id!r} does not exist",
                code="source_snapshot_unavailable",
            )
        return stored.document

    @staticmethod
    def _artifact_for(document: Document) -> SourceArtifactRef:
        """The artifact is the imported documents row itself; the id needs no inventing."""

        return SourceArtifactRef(
            artifact_id=document.id,
            source_document_id=document.id,
            source_revision=document.revision,
            content_hash=document_content_hash(document),
            note="imported drawing, pinned at intake; re-ingestion means a new import",
        )

    # -- derivation (pure) ---------------------------------------------------------------

    def derive_candidates(
        self,
        document: Document,
        artifact: SourceArtifactRef,
        *,
        target_document_id: str = "",
    ) -> list[SemanticCandidate]:
        """The deterministic extraction. Pure: same inputs, same candidates, no writes."""

        return self._derive(document, artifact, target_document_id=target_document_id).candidates

    def _derive(
        self,
        document: Document,
        artifact: SourceArtifactRef,
        *,
        target_document_id: str,
    ) -> _Derivation:
        if document.id != artifact.source_document_id:
            raise ValueError("the artifact does not describe this document")
        if document.revision != artifact.source_revision:
            raise ValueError(
                "the artifact pins a different revision than the document carries; "
                "a region is only re-locatable under its own revision"
            )

        clusters = self._clusters(document)
        free_texts = [
            element
            for element in document.elements
            if element.type == "text" and not element.metadata.get("cad_block")
        ]
        tags: list[_TagText] = []
        unclassified = 0
        for element in sorted(free_texts, key=lambda item: item.id):
            match = TAG_TEXT_PATTERN.match(element.text)
            if match is None:
                unclassified += 1
                continue
            tags.append(_TagText(element=element, tag=match.group(1)))
        bindings = {tag.element.id: self._bind(tag, clusters) for tag in tags}

        candidates: list[SemanticCandidate] = []
        for cluster in clusters:
            candidates.extend(
                self._cluster_candidates(
                    cluster, tags, bindings, artifact, target_document_id=target_document_id
                )
            )
        for tag in tags:
            if bindings[tag.element.id].status == "unique":
                continue  # the cluster pass already filed this tag's role candidate
            candidates.append(
                self._unbound_tag_candidate(
                    tag, bindings[tag.element.id], artifact, target_document_id=target_document_id
                )
            )
        return _Derivation(
            candidates=candidates,
            cluster_count=len(clusters),
            tag_text_count=len(tags),
            unclassified_text_count=unclassified,
            blockless_geometry_count=sum(
                1
                for element in document.elements
                if element.type != "text" and not element.metadata.get("cad_block")
            ),
        )

    def _clusters(self, document: Document) -> list[_Cluster]:
        """Recover block instances: touch-connectivity, then containment, within a block.

        Phase 1 is a swept union-find over same-block bounding boxes under the declared seam
        gap. Phase 2 merges a same-block group that floats entirely inside another group's
        bounding box (a motor circle inside its enclosure never touches the outline). Two
        *instances* of one block sit apart, so neither phase can merge them.
        """

        by_block: dict[str, list[Element]] = {}
        for element in document.elements:
            block = str(element.metadata.get("cad_block") or "")
            if block:
                by_block.setdefault(block, []).append(element)

        clusters: list[_Cluster] = []
        for block in sorted(by_block):
            members = sorted(by_block[block], key=lambda item: item.id)
            boxes = {element.id: element_bbox(element) for element in members}
            parent = {element.id: element.id for element in members}

            active: list[str] = []
            for element in sorted(members, key=lambda item: (boxes[item.id][0], item.id)):
                x0 = boxes[element.id][0]
                active = [
                    other for other in active if boxes[other][2] + CLUSTER_LINK_GAP >= x0
                ]
                for other in active:
                    if _boxes_touch(boxes[other], boxes[element.id], CLUSTER_LINK_GAP):
                        parent[_find_root(parent, other)] = _find_root(parent, element.id)
                active.append(element.id)

            groups: dict[str, list[str]] = {}
            for element in members:
                groups.setdefault(_find_root(parent, element.id), []).append(element.id)
            group_boxes = {
                head: union_bbox([boxes[element_id] for element_id in ids]) or (0, 0, 0, 0)
                for head, ids in groups.items()
            }
            # Phase 2: containment joins floaters to their outline. Fixpoint loop because
            # containment chains through the growing union box; group counts per block are
            # small, and the pass order is pinned by element id so the result is stable.
            while True:
                heads = sorted(groups)
                merged = False
                for inner in heads:
                    if inner not in groups:
                        continue
                    for outer in heads:
                        if outer == inner or outer not in groups:
                            continue
                        if _box_contains(group_boxes[outer], group_boxes[inner]):
                            groups[outer] = [*groups[outer], *groups.pop(inner)]
                            group_boxes[outer] = union_bbox(
                                [group_boxes[outer], group_boxes.pop(inner)]
                            ) or (0, 0, 0, 0)
                            merged = True
                            break
                    if merged:
                        break
                if not merged:
                    break

            for ids in groups.values():
                member_set = set(ids)
                layers = sorted(
                    {
                        str(element.metadata.get("cad_layer") or "")
                        for element in members
                        if element.id in member_set
                    }
                )
                clusters.append(
                    _Cluster(
                        block=block,
                        element_ids=tuple(sorted(ids)),
                        bbox=union_bbox([boxes[element_id] for element_id in ids]) or (
                            0.0,
                            0.0,
                            0.0,
                            0.0,
                        ),
                        layer=layers[0] if len(layers) == 1 else "",
                    )
                )
        clusters.sort(key=lambda cluster: (cluster.block, cluster.element_ids[0]))
        return clusters

    def _bind(self, tag: _TagText, clusters: list[_Cluster]) -> _Binding:
        position = tag.element.position
        reach = max(
            TAG_BINDING_MIN_REACH, tag.element.font_size * TAG_BINDING_REACH_FONT_MULTIPLE
        )
        best_distance: float | None = None
        best: list[_Cluster] = []
        for cluster in clusters:
            distance = round(
                _point_rect_distance(position.x, position.y, cluster.bbox), _DISTANCE_DECIMALS
            )
            if distance > reach:
                continue
            if best_distance is None or distance < best_distance:
                best_distance, best = distance, [cluster]
            elif distance == best_distance:
                best.append(cluster)
        if best_distance is None:
            return _Binding(status="unbound", clusters=(), distance=None)
        if len(best) > 1:
            return _Binding(status="ambiguous", clusters=tuple(best), distance=best_distance)
        return _Binding(status="unique", clusters=(best[0],), distance=best_distance)

    # -- candidate construction ------------------------------------------------------------

    def _cluster_candidates(
        self,
        cluster: _Cluster,
        tags: list[_TagText],
        bindings: dict[str, _Binding],
        artifact: SourceArtifactRef,
        *,
        target_document_id: str,
    ) -> list[SemanticCandidate]:
        region = self._region(
            artifact,
            element_refs=list(cluster.element_ids),
            boxes=[cluster.bbox],
            layer=cluster.layer,
        )
        produced = [
            self._symbol_candidate(
                cluster, region, artifact, target_document_id=target_document_id
            )
        ]

        bound = [
            tag
            for tag in tags
            if bindings[tag.element.id].status == "unique"
            and bindings[tag.element.id].clusters[0].element_ids == cluster.element_ids
        ]
        distinct_tags = sorted({tag.tag for tag in bound})
        if len(distinct_tags) == 1:
            tag_region = self._region(
                artifact,
                element_refs=[*cluster.element_ids, *(tag.element.id for tag in bound)],
                boxes=[cluster.bbox, *(element_bbox(tag.element) for tag in bound)],
                text_spans=[TextSpan(text=distinct_tags[0])],
                layer=region.layer,
            )
            produced.append(
                self._candidate(
                    artifact=artifact,
                    region=tag_region,
                    candidate_type="equipment_tag",
                    facts=ProposedSemantics(equipment_tag=distinct_tags[0]),
                    confidence_value=1.0,
                    evidence=[
                        CandidateEvidence(
                            kind="observed_text",
                            detail=f"a tag text reads {distinct_tags[0]!r}",
                            region_id=tag_region.region_id,
                            observed_value=distinct_tags[0],
                        ),
                        *(
                            CandidateEvidence(
                                kind="element_ref",
                                detail=(
                                    f"tag anchor element {tag.element.id} at distance "
                                    f"{bindings[tag.element.id].distance} from the cluster"
                                ),
                                region_id=tag_region.region_id,
                            )
                            for tag in bound
                        ),
                        CandidateEvidence(
                            kind="rule",
                            detail=(
                                f"{M6_SOURCE_RULES}: whole-text tag pattern, unique nearest "
                                "cluster within reach"
                            ),
                            region_id=tag_region.region_id,
                        ),
                    ],
                    target_document_id=target_document_id,
                )
            )
        elif len(distinct_tags) > 1:
            ambiguous_region = self._region(
                artifact,
                element_refs=[*cluster.element_ids, *(tag.element.id for tag in bound)],
                boxes=[cluster.bbox, *(element_bbox(tag.element) for tag in bound)],
                text_spans=[TextSpan(text=tag) for tag in distinct_tags],
                layer=region.layer,
            )
            produced.append(
                self._candidate(
                    artifact=artifact,
                    region=ambiguous_region,
                    candidate_type="unresolved",
                    facts=ProposedSemantics(unresolved_reason="ambiguous"),
                    confidence_value=0.0,
                    evidence=[
                        CandidateEvidence(
                            kind="observed_text",
                            detail=(
                                f"one block cluster is labelled by {len(distinct_tags)} distinct "
                                f"tags {distinct_tags}: which of them names the equipment is a "
                                "person's decision, not a rule's"
                            ),
                            region_id=ambiguous_region.region_id,
                        ),
                        CandidateEvidence(
                            kind="rule",
                            detail=f"{M6_SOURCE_RULES}: a tie is never resolved by picking one",
                            region_id=ambiguous_region.region_id,
                        ),
                    ],
                    target_document_id=target_document_id,
                )
            )
        for tag in bound:
            produced.append(
                self._role_candidate(
                    tag,
                    bindings[tag.element.id],
                    artifact,
                    target_document_id=target_document_id,
                )
            )
        return produced

    def _symbol_candidate(
        self,
        cluster: _Cluster,
        region: SourceRegion,
        artifact: SourceArtifactRef,
        *,
        target_document_id: str,
    ) -> SemanticCandidate:
        matches = self._match_symbol(cluster.block)
        element_evidence = CandidateEvidence(
            kind="element_ref",
            detail=(
                f"block cluster of {len(cluster.element_ids)} element(s) sharing cad_block "
                f"{cluster.block!r}"
            ),
            region_id=region.region_id,
            observed_value=cluster.block,
        )
        if len(matches) == 1:
            key = matches[0]
            return self._candidate(
                artifact=artifact,
                region=region,
                candidate_type="symbol_class",
                facts=ProposedSemantics(symbol_class=key),
                confidence_value=1.0,
                evidence=[
                    CandidateEvidence(
                        kind="catalogue",
                        detail=(
                            f"block name {cluster.block!r} matches exactly one catalogue entry: "
                            f"{key!r}"
                        ),
                        region_id=region.region_id,
                        observed_value=key,
                    ),
                    element_evidence,
                    CandidateEvidence(
                        kind="rule",
                        detail=(
                            f"{M6_SOURCE_RULES}: exact block-name match against catalogue key, "
                            "name or declared aliases"
                        ),
                        region_id=region.region_id,
                    ),
                ],
                target_document_id=target_document_id,
            )
        if not matches:
            return self._candidate(
                artifact=artifact,
                region=region,
                candidate_type="unresolved",
                facts=ProposedSemantics(unresolved_reason="out_of_catalog"),
                confidence_value=0.0,
                evidence=[
                    CandidateEvidence(
                        kind="catalogue",
                        detail=(
                            f"block name {cluster.block!r} matches no catalogue key, name or "
                            "declared alias; the gap is reported, never filled by a fuzzy read"
                        ),
                        region_id=region.region_id,
                        observed_value=cluster.block,
                    ),
                    element_evidence,
                ],
                target_document_id=target_document_id,
            )
        return self._candidate(
            artifact=artifact,
            region=region,
            candidate_type="unresolved",
            facts=ProposedSemantics(unresolved_reason="ambiguous"),
            confidence_value=0.0,
            evidence=[
                CandidateEvidence(
                    kind="catalogue",
                    detail=(
                        f"block name {cluster.block!r} matches {len(matches)} catalogue entries "
                        f"equally {matches}; choosing one would be a guess"
                    ),
                    region_id=region.region_id,
                    observed_value=cluster.block,
                ),
                element_evidence,
            ],
            target_document_id=target_document_id,
        )

    def _role_candidate(
        self,
        tag: _TagText,
        binding: _Binding,
        artifact: SourceArtifactRef,
        *,
        target_document_id: str,
    ) -> SemanticCandidate:
        role = "instrument_tag" if _is_instrument_tag(tag.tag) else "equipment_label"
        region = self._text_region(artifact, tag)
        evidence = [
            CandidateEvidence(
                kind="observed_text",
                detail=f"text {tag.element.id} reads {tag.tag!r}",
                region_id=region.region_id,
                observed_value=tag.tag,
            ),
            CandidateEvidence(
                kind="rule",
                detail=(
                    f"{M6_SOURCE_RULES}: instrument-shaped tag prefix"
                    if role == "instrument_tag"
                    else f"{M6_SOURCE_RULES}: a tag text bound to a block cluster labels it"
                ),
                region_id=region.region_id,
            ),
        ]
        if binding.status == "unique":
            cluster = binding.clusters[0]
            evidence.append(
                CandidateEvidence(
                    kind="neighbour",
                    detail=(
                        f"bound to block cluster {cluster.block!r} "
                        f"({len(cluster.element_ids)} elements) at distance {binding.distance}"
                    ),
                    region_id=region.region_id,
                )
            )
        return self._candidate(
            artifact=artifact,
            region=region,
            candidate_type="annotation_role",
            facts=ProposedSemantics(annotation_role=role),  # type: ignore[arg-type]
            confidence_value=1.0,
            evidence=evidence,
            target_document_id=target_document_id,
        )

    def _unbound_tag_candidate(
        self,
        tag: _TagText,
        binding: _Binding,
        artifact: SourceArtifactRef,
        *,
        target_document_id: str,
    ) -> SemanticCandidate:
        region = self._text_region(artifact, tag)
        if binding.status == "ambiguous":
            tied = sorted(cluster.block for cluster in binding.clusters)
            return self._candidate(
                artifact=artifact,
                region=region,
                candidate_type="unresolved",
                facts=ProposedSemantics(unresolved_reason="ambiguous"),
                confidence_value=0.0,
                evidence=[
                    CandidateEvidence(
                        kind="neighbour",
                        detail=(
                            f"tag {tag.tag!r} is equidistant ({binding.distance}) from "
                            f"{len(binding.clusters)} clusters {tied}; the binding is not "
                            "decidable by rule"
                        ),
                        region_id=region.region_id,
                        observed_value=tag.tag,
                    )
                ],
                target_document_id=target_document_id,
            )
        if _is_instrument_tag(tag.tag):
            return self._role_candidate(
                tag, binding, artifact, target_document_id=target_document_id
            )
        return self._candidate(
            artifact=artifact,
            region=region,
            candidate_type="unresolved",
            facts=ProposedSemantics(unresolved_reason="insufficient_evidence"),
            confidence_value=0.0,
            evidence=[
                CandidateEvidence(
                    kind="observed_text",
                    detail=(
                        f"tag-shaped text {tag.tag!r} has no block cluster within reach; the "
                        "reading is recorded, not anchored"
                    ),
                    region_id=region.region_id,
                    observed_value=tag.tag,
                )
            ],
            target_document_id=target_document_id,
        )

    # -- shared builders ------------------------------------------------------------

    def _match_symbol(self, block: str) -> list[str]:
        """Exact, case-folded block-name match against key, name and declared aliases.

        Substring or scored matching is deliberately absent: a near miss must surface as
        ``out_of_catalog``, not as the catalogue's nearest neighbour.
        """

        needle = block.strip().casefold()
        if not needle:
            return []
        matches: list[str] = []
        for definition in self._registry.list():
            names = [definition.key, definition.name]
            aliases = definition.metadata.get("aliases", [])
            names.extend(str(alias) for alias in aliases)
            if any(needle == str(name).strip().casefold() for name in names):
                matches.append(definition.key)
        return sorted(matches)

    def _region(
        self,
        artifact: SourceArtifactRef,
        *,
        element_refs: list[str],
        boxes: list[tuple[float, float, float, float]],
        text_spans: list[TextSpan] | None = None,
        layer: str = "",
    ) -> SourceRegion:
        box = union_bbox(boxes)
        return build_source_region(
            artifact=artifact,
            geometry_selector=bbox_geometry(box) if box is not None else None,
            element_refs=sorted(element_refs),
            text_spans=text_spans or [],
            layer=layer,
        )

    def _text_region(self, artifact: SourceArtifactRef, tag: _TagText) -> SourceRegion:
        return build_source_region(
            artifact=artifact,
            geometry_selector=None,
            element_refs=[tag.element.id],
            text_spans=[TextSpan(text=tag.tag)],
            layer=str(tag.element.metadata.get("cad_layer") or ""),
        )

    def _candidate(
        self,
        *,
        artifact: SourceArtifactRef,
        region: SourceRegion,
        candidate_type: FactKind,
        facts: ProposedSemantics,
        confidence_value: float,
        evidence: list[CandidateEvidence],
        target_document_id: str,
    ) -> SemanticCandidate:
        producer = ProducerRef(
            key="deterministic_rule_engine", version=M6_SOURCE_ADAPTER_VERSION
        )
        candidate_id = "cand_m6_" + canonical_digest(
            {
                "rules": M6_SOURCE_RULES,
                "artifact": artifact.artifact_id,
                "source_revision": artifact.source_revision,
                "region": region.region_id,
                "target_document_id": target_document_id,
                "candidate_type": candidate_type,
                "facts": facts.model_dump(mode="json"),
                "producer": producer.key,
                "producer_version": producer.version,
            }
        )
        return SemanticCandidate(
            candidate_id=candidate_id,
            artifact=artifact,
            region=region,
            target_document_id=target_document_id,
            candidate_type=candidate_type,
            proposed_semantics=facts,
            confidence=Confidence(
                value=confidence_value,
                calibration_class="not_measured",
                source="deterministic_rule",
            ),
            evidence=evidence,
            producer=producer,
            provenance=ProvenanceRef(
                provider="agentcad",
                model="",
                procedure_version=M6_SOURCE_RULES,
            ),
        )

    # -- filing --------------------------------------------------------------------------

    def extraction_digest(
        self,
        artifact: SourceArtifactRef,
        target_document_id: str,
        candidates: list[SemanticCandidate],
    ) -> str:
        """One digest over everything replay must reproduce: source, target, catalogue, rules."""

        return canonical_digest(
            {
                "digest": "m6-source-extraction/v1",
                "rules": M6_SOURCE_RULES,
                "catalogue": self._registry.fingerprint(),
                "artifact": {
                    "artifact_id": artifact.artifact_id,
                    "source_document_id": artifact.source_document_id,
                    "source_revision": artifact.source_revision,
                    "content_hash": artifact.content_hash,
                },
                "target_document_id": target_document_id,
                "candidates": [
                    _without_volatile(candidate.model_dump(mode="json", by_alias=True))
                    for candidate in candidates
                ],
            }
        )

    def ingest(self, source_document_id: str, *, target_document_id: str) -> IngestionSummary:
        """Pin the source, derive candidates, file them through the governed core.

        Re-ingesting the same source under the same rules is idempotent *by identity*: a
        candidate whose id is already stored with identical (non-volatile) content is left
        untouched, and the same id with different content is a contradiction that fails
        loudly rather than an overwrite.
        """

        document = self._read_source(source_document_id)
        artifact = self._artifact_for(document)
        derivation = self._derive(document, artifact, target_document_id=target_document_id)
        filed: list[str] = []
        already_present: list[str] = []
        for candidate in derivation.candidates:
            existing = self._store.get_semantic_candidate(candidate.candidate_id)
            if existing is None:
                self._candidates.file_candidate(candidate)
                filed.append(candidate.candidate_id)
                continue
            old = _without_volatile(existing.model_dump(mode="json", by_alias=True))
            new = _without_volatile(candidate.model_dump(mode="json", by_alias=True))
            if old == new:
                already_present.append(candidate.candidate_id)
                continue
            raise CandidateIdentityConflict(
                f"candidate {candidate.candidate_id} exists with different content; the "
                "identity is content-derived, so this is a derivation contradiction, not an "
                "update",
            )

        counts: dict[str, int] = {}
        for candidate in derivation.candidates:
            counts[candidate.candidate_type] = counts.get(candidate.candidate_type, 0) + 1
        return IngestionSummary(
            artifact=artifact,
            target_document_id=target_document_id,
            producer=ProducerRef(
                key="deterministic_rule_engine", version=M6_SOURCE_ADAPTER_VERSION
            ),
            rules=M6_SOURCE_RULES,
            catalogue_fingerprint=self._registry.fingerprint(),
            cluster_count=derivation.cluster_count,
            tag_text_count=derivation.tag_text_count,
            unclassified_text_count=derivation.unclassified_text_count,
            blockless_geometry_count=derivation.blockless_geometry_count,
            candidate_ids=[candidate.candidate_id for candidate in derivation.candidates],
            filed=filed,
            already_present=already_present,
            counts_by_type=counts,
            extraction_digest=self.extraction_digest(
                artifact, target_document_id, derivation.candidates
            ),
        )


__all__ = [
    "CLUSTER_LINK_GAP",
    "INSTRUMENT_FIRST_LETTERS",
    "INSTRUMENT_SUCCEEDING_LETTERS",
    "M6_SOURCE_ADAPTER",
    "M6_SOURCE_ADAPTER_VERSION",
    "M6_SOURCE_RULES",
    "TAG_BINDING_MIN_REACH",
    "TAG_BINDING_REACH_FONT_MULTIPLE",
    "TAG_TEXT_PATTERN",
    "CandidateIdentityConflict",
    "IngestionSummary",
    "M6SourceAdapter",
    "M6SourceStore",
    "SourceVerificationError",
]
