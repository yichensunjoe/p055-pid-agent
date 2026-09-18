"""M3 deterministic drafting engine.

Charter reference: §15 (port-aware routing, collision detection, annotation placement,
region relayout, manual lock, crossing/junction semantics), §21.3, §48 ("layout,
routing, collision, annotation, regional repair and quality gate produce stable,
reproducible results").

What this engine is
-------------------
One preview-only pipeline over a drawing snapshot:

1. **lock resolution** — request locks + persistent element locks + declared lock
   regions become one frozen set.
2. **region relayout** — the existing topology-aware layout engine runs with that set
   passed in, so locked geometry acts as an anchor and everything else is arranged
   around it.
3. **port-aware reroute** — scoped connectors are re-routed with the deterministic
   candidate router that respects port outward normals, obstacles and crossings.
4. **annotation placement** — labels move only when the move strictly reduces that
   label's own collision cost.
5. **crossing bridges** — a legal crossing gets exactly one jump bridge, on the
   secondary line; a crossing that lands on a junction is reported, never smoothed over.
6. **junction check** — dangling junctions are reported; the engine never creates,
   deletes or re-binds topology (Charter P0-4: connectivity is semantics, not styling).
7. **collision relaxation** — a bounded, deterministic pass that separates overlapping
   nodes, one move at a time.

Two properties make the result trustworthy rather than merely plausible:

* **Monotone acceptance.** Every stage is guarded: it is committed only if no hard
  drafting metric (``DRAFTING_HARD_FIELDS``) gets worse, otherwise it is rolled back and
  reported as ``DRAFT_STAGE_ROLLED_BACK``. Drafting may tidy a drawing; it may not
  damage one.
* **Reproducibility.** All passes run over an id-canonical snapshot and emit canonically
  ordered operations, and the result carries content hashes plus a transaction digest.
  The same drawing content produces the same digest regardless of element storage order
  or how many times the engine runs.

What this engine is not
-----------------------
It never writes a document, never bypasses the transaction channel, never approves
anything and never changes engineering connectivity. It returns a
:class:`~agentcad.drafting_models.DraftingPreview` whose transaction a governed surface
may apply at an expected revision (Charter §7, P0-2, P0-6).
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field

from .annotation_layout import _candidate_score, _free_candidates, _parent_candidates
from .auto_layout import AutoLayoutEngine as BaseLayoutEngine
from .auto_layout import LayoutNode
from .auto_layout_engine import AutoLayoutEngine
from .diagram_quality import (
    _normal_mismatches,
    analyze_diagram_quality,
    port_outward_normal,
    route_connector_points,
)
from .drafting_geometry import (
    Rect,
    canonical_document,
    canonical_operations,
    classify_junctions,
    collision_findings,
    connector_segments,
    detect_crossings,
    drafting_content_hash,
    element_is_locked,
    element_rect,
    hard_regressions,
    improvements,
    lock_provenance,
    rects_overlap,
    resolve_ports,
    resolve_scope,
    segment_intersects_rect,
    simplify,
    snapshot,
    structural_findings,
    text_rect,
    transaction_digest,
)
from .drafting_models import (
    DRAFTING_BLOCKER_CODES,
    DRAFTING_CODES,
    DRAFTING_ENGINE_VERSION,
    DraftingCrossing,
    DraftingFinding,
    DraftingGate,
    DraftingJunction,
    DraftingLocks,
    DraftingMetrics,
    DraftingPolicy,
    DraftingPreview,
    DraftingReport,
    DraftingReproducibility,
    DraftingRequest,
    DraftingSnapshot,
)
from .layout_models import (
    AutoLayoutRequest,
    declared_lock_regions,
    declared_reserved_regions,
)
from .models import (
    ConnectorElement,
    Document,
    Element,
    Operation,
    Point,
    TransactionRequest,
    UpdateElementOperation,
)
from .service import DocumentService, InvalidOperationError, RevisionConflictError

#: Route quality compared as a tuple, most significant first: port-contract violations,
#: then obstacle hits, then drawing craft (orthogonality, micro segments, bends, length).
RouteScore = tuple[int, int, int, int, int, float]


@dataclass(frozen=True)
class ConnectionAnalysis:
    """Crossing, junction and collision facts about one drawing state."""

    collisions: list[DraftingFinding] = field(default_factory=list)
    crossings: list[DraftingCrossing] = field(default_factory=list)
    junctions: list[DraftingJunction] = field(default_factory=list)
    findings: list[DraftingFinding] = field(default_factory=list)


class DraftingEngine:
    """Deterministic, preview-only drafting over one drawing snapshot."""

    def __init__(self, service: DocumentService):
        self.service = service

    # ----------------------------------------------------------------- #
    # Read-only analysis
    # ----------------------------------------------------------------- #

    def report(self, document_id: str, request: DraftingRequest | None = None) -> DraftingReport:
        return self.report_document(
            self.service.get_document(document_id),
            request or DraftingRequest(),
        )

    def report_document(
        self,
        document: Document,
        request: DraftingRequest | None = None,
    ) -> DraftingReport:
        """Analyse one drawing state without changing anything."""

        request = request or DraftingRequest()
        policy = request.policy
        canonical = canonical_document(document)
        scope_ids, scope_kind = resolve_scope(canonical, request)
        locked = self._locked_ids(canonical, request)
        analysis = self._analyze(canonical, policy)
        state = snapshot(canonical, self.service.symbols, policy)
        content_hash = drafting_content_hash(canonical)
        return DraftingReport(
            document_id=canonical.id,
            document_name=canonical.name,
            revision=canonical.revision,
            content_hash=content_hash,
            input_content_hash=content_hash,
            scope_kind=scope_kind,
            scope_element_ids=scope_ids,
            locks=DraftingLocks(**lock_provenance(canonical, request, locked)),
            ports=resolve_ports(canonical, self.service.symbols),
            collisions=analysis.collisions,
            crossings=analysis.crossings,
            junctions=analysis.junctions,
            findings=self._mark_waived(analysis.findings, policy),
            gate=self._gate(canonical, analysis.findings, state, policy),
            score=state.score,
        )

    # ----------------------------------------------------------------- #
    # Preview pipeline
    # ----------------------------------------------------------------- #

    def preview(self, document_id: str, request: DraftingRequest) -> DraftingPreview:
        return self.preview_document(self.service.get_document(document_id), request)

    def preview_document(self, document: Document, request: DraftingRequest) -> DraftingPreview:
        if request.expected_revision is not None and request.expected_revision != document.revision:
            raise RevisionConflictError(
                f"expected revision {request.expected_revision}, current revision is "
                f"{document.revision}"
            )
        policy = request.policy
        registry = self.service.symbols
        input_hash = drafting_content_hash(document)
        working = canonical_document(document)
        scope_ids, scope_kind = resolve_scope(working, request)
        scope_set = set(scope_ids)
        locked = self._locked_ids(working, request)
        before = snapshot(working, registry, policy)

        operations: list[Operation] = []
        moved: set[str] = set()
        rerouted: set[str] = set()
        moved_annotations: set[str] = set()
        bridged: set[str] = set()
        skipped_locked: set[str] = set()
        warnings: list[str] = []
        stage_findings: list[DraftingFinding] = []
        #: Connectors whose endpoint moved in this run and therefore must be re-routed.
        forced: set[str] = set()

        def commit(stage: str, candidate: Document) -> bool:
            """Accept a stage only if it worsens no hard drafting metric."""

            regressions = hard_regressions(before, snapshot(candidate, registry, policy))
            if not regressions:
                return True
            stage_findings.append(
                DraftingFinding(
                    severity="warning",
                    code="DRAFT_STAGE_ROLLED_BACK",
                    message=f"阶段 {stage} 会让图面变差，已回滚：{'；'.join(regressions)}",
                    details={"stage": stage, "regressions": regressions},
                )
            )
            warnings.append(f"阶段 {stage} 已回滚（会使硬性图面指标变差）。")
            return False

        # The pipeline runs to a fixed point, at most ``pipeline_rounds`` times, because
        # the passes interfere by design: a collision move relocates a node and therefore
        # invalidates the route that was optimal before it. Repeating the pipeline is what
        # makes ``settled`` a property of the result instead of a hope, and the round bound
        # keeps the run finite and reproducible. Re-layout runs only in the first round:
        # it is a global arrangement, not a local repair.
        for round_index in range(policy.pipeline_rounds):
            changed = False

            # 1) Region-aware relayout with the frozen set passed in as anchors.
            if round_index == 0 and scope_set and request.relayout:
                changed = self._stage_relayout(
                    working,
                    request,
                    policy,
                    scope_ids,
                    locked,
                    commit,
                    operations,
                    moved,
                    rerouted,
                    moved_annotations,
                    skipped_locked,
                    warnings,
                ) or changed

            # 2) Port-aware routing. Two different reasons to touch a pipe:
            #    * **forced** — this run moved one of its symbols, so the pipe must follow
            #      its port; a drawing whose pipe still ends where the symbol *was* is not
            #      tidy, it is broken.
            #    * **opportunistic** — `reroute_connectors` is on and this pipe's own route
            #      quality strictly improves.
            #    Forced re-routing is deliberately independent of `reroute_connectors`: that
            #    switch means "do not tidy routes", never "leave the pipes you moved behind".
            if scope_set and (request.reroute_connectors or forced):
                candidate = deepcopy(working)
                staged_reroute: list[Operation] = []
                stage_rerouted: set[str] = set()
                for connector in self._scoped_connectors(candidate, scope_set, locked):
                    must_follow = connector.id in forced
                    if not (request.reroute_connectors or must_follow):
                        continue
                    points = self._refine_route(
                        candidate, connector, policy, forced=must_follow
                    )
                    if points is None:
                        continue
                    staged_reroute.append(
                        self._apply_patch(
                            candidate,
                            connector.id,
                            {
                                "points": [point.model_dump(mode="json") for point in points],
                                "routing": "manual",
                            },
                        )
                    )
                    stage_rerouted.add(connector.id)
                if staged_reroute and commit(f"reroute-{round_index + 1}", candidate):
                    working = candidate
                    operations.extend(staged_reroute)
                    rerouted.update(stage_rerouted)
                    changed = True

            # 3) Annotation placement.
            if request.place_annotations and scope_set:
                candidate = deepcopy(working)
                staged = self._place_annotations(candidate, scope_set, locked, scope_kind)
                if staged and commit(f"annotations-{round_index + 1}", candidate):
                    working = candidate
                    operations.extend(staged)
                    moved_annotations.update(operation.element_id for operation in staged)
                    changed = True

            # 4) Crossing bridges.
            if request.bridge_crossings and scope_set:
                candidate = deepcopy(working)
                staged, bridged_now = self._bridge_crossings(
                    candidate,
                    scope_set,
                    locked,
                    stage_findings,
                )
                if staged and commit(f"crossings-{round_index + 1}", candidate):
                    working = candidate
                    operations.extend(staged)
                    bridged.update(bridged_now)
                    changed = True

            # 5) Collision relaxation.
            if request.resolve_collisions and scope_set:
                for pass_index in range(policy.collision_passes):
                    candidate = deepcopy(working)
                    staged = self._relax_collisions(candidate, scope_set, locked, policy, request)
                    if not staged or not commit(
                        f"collision-{round_index + 1}-{pass_index + 1}", candidate
                    ):
                        break
                    working = candidate
                    operations.extend(staged)
                    relocated = {operation.element_id for operation in staged}
                    moved.update(relocated)
                    # The pipes attached to what just moved now have to follow it, in this
                    # run, or the result would disagree with its own geometry.
                    forced.update(
                        connector.id
                        for connector in self._connectors_touching(candidate, relocated)
                    )
                    changed = True

            # 6) Reserved-space eviction. A symbol parked on the legend or the title block
            #    is not an overlap, so stage 5 cannot see it: the obstacle is a declared
            #    zone, not another node. Unlocked intruders are moved out here; a locked
            #    one is left exactly where the engineer froze it and stays visible in the
            #    gate as an honest blocker.
            if request.resolve_collisions and scope_set:
                candidate = deepcopy(working)
                staged = self._evict_reserved_intruders(
                    candidate, scope_set, locked, policy, request
                )
                if staged and commit(f"reserved-{round_index + 1}", candidate):
                    working = candidate
                    operations.extend(staged)
                    relocated = {operation.element_id for operation in staged}
                    moved.update(relocated)
                    forced.update(
                        connector.id
                        for connector in self._connectors_touching(candidate, relocated)
                    )
                    changed = True

            if not changed:
                break

        result = canonical_document(working)
        after = snapshot(result, registry, policy)
        regressions = hard_regressions(before, after)
        analysis = self._analyze(result, policy)
        scoped_locks = sorted(
            element_id
            for element_id in locked
            if element_id in {element.id for element in document.elements}
        )
        findings = [
            *analysis.findings,
            *self._integrity_findings(result, operations, scope_set, scope_kind, locked),
            *stage_findings,
        ]
        findings = self._mark_waived(findings, policy)
        if regressions:
            findings.append(
                DraftingFinding(
                    severity="blocker",
                    code="DRAFT_RESULT_REGRESSION",
                    message=f"整理结果使硬性图面指标变差：{'；'.join(regressions)}",
                    details={"regressions": regressions},
                )
            )
        gate = self._gate(result, findings, after, policy)
        ordered = canonical_operations(
            operations,
            element_kinds={element.id: element.type for element in result.elements},
        )
        transaction = None
        if ordered:
            transaction = TransactionRequest(
                operations=ordered,
                expected_revision=document.revision,
                label=(
                    f"Deterministic drafting: {len(moved)} node(s), {len(rerouted)} route(s), "
                    f"{len(moved_annotations)} label(s)"
                ),
                source=None,
            )
        else:
            warnings.append("图纸已满足本次整理条件，未生成修改事务。")

        output_hash = drafting_content_hash(result)
        return DraftingPreview(
            document_id=document.id,
            document_name=document.name,
            current_revision=document.revision,
            transaction=transaction,
            settled=transaction is None,
            moved_element_ids=sorted(moved),
            rerouted_connector_ids=sorted(rerouted),
            moved_annotation_ids=sorted(moved_annotations),
            bridged_connector_ids=sorted(bridged),
            locked_element_ids=sorted(locked),
            locks=DraftingLocks(
                **lock_provenance(document, request, set(scoped_locks))
            ),
            skipped_locked_element_ids=sorted(skipped_locked),
            in_scope_element_ids=scope_ids,
            warnings=warnings,
            collisions=analysis.collisions,
            crossings=analysis.crossings,
            junctions=analysis.junctions,
            metrics=DraftingMetrics(
                before=before,
                after=after,
                improvements=improvements(before, after),
                regressions=regressions,
            ),
            gate=gate,
            findings=findings,
            reproducibility=DraftingReproducibility(
                engine_version=DRAFTING_ENGINE_VERSION,
                input_content_hash=input_hash,
                output_content_hash=output_hash,
                transaction_digest=transaction_digest(
                    engine_version=DRAFTING_ENGINE_VERSION,
                    request=request,
                    input_hash=input_hash,
                    output_hash=output_hash,
                    operations=ordered,
                    element_kinds={
                        element.id: element.type for element in result.elements
                    },
                ),
                operation_count=len(ordered),
                settled=transaction is None,
            ),
        )

    # ----------------------------------------------------------------- #
    # Lock, scope and analysis helpers
    # ----------------------------------------------------------------- #

    def _locked_ids(self, document: Document, request: DraftingRequest) -> set[str]:
        """Request locks + persistent element locks + declared lock regions."""

        locked = {element_id for element_id in request.locked_element_ids if element_id}
        for element in document.elements:
            if element_is_locked(element):
                locked.add(element.id)
        regions = declared_lock_regions(document)
        if regions:
            for element in document.elements:
                rect = element_rect(element)
                if rect is None:
                    continue
                if any(
                    region.contains_box(rect.x1, rect.y1, rect.x2, rect.y2) for region in regions
                ):
                    locked.add(element.id)
        return locked

    def _rebind_connector(
        self,
        document: Document,
        connector: ConnectorElement,
    ) -> ConnectorElement:
        """A copy of one connector whose endpoints point at their ports *right now*.

        The pipeline moves symbols, so a connector's cached endpoint coordinates can lag
        one pass behind. Routing from those stale coordinates would produce a diagonal
        polyline once the write path re-binds the endpoint, so every route is planned from
        the current port position. A broken binding is left as-is and reported by the gate
        as ``DRAFT_PORT_UNRESOLVED`` instead of being inventing a coordinate.
        """

        element_map = {element.id: element for element in document.elements}
        fresh = connector.model_copy(deep=True)
        for name in ("source", "target"):
            endpoint = getattr(fresh, name)
            if endpoint is None or endpoint.element_id is None or not endpoint.port_id:
                continue
            bound = element_map.get(endpoint.element_id)
            if bound is None:
                continue
            try:
                if bound.type == "junction":
                    if endpoint.port_id != "node":
                        continue
                    point = Point.model_validate(bound.position.model_dump())
                elif bound.type == "symbol":
                    point = self.service._symbol_port_point(bound, endpoint.port_id)
                else:
                    continue
            except (InvalidOperationError, KeyError):
                continue
            setattr(fresh, name, endpoint.model_copy(update={"point": point}))
        return fresh

    def _stage_relayout(
        self,
        working: Document,
        request: DraftingRequest,
        policy: DraftingPolicy,
        scope_ids: list[str],
        locked: set[str],
        commit,
        operations: list[Operation],
        moved: set[str],
        rerouted: set[str],
        moved_annotations: set[str],
        skipped_locked: set[str],
        warnings: list[str],
    ) -> bool:
        """Round-one region relayout, replaying the layout engine's transaction locally.

        The layout engine previews on its own copy, so its result has to be replayed here
        for the later evidence-based passes to see the moved geometry. Locked geometry is
        passed in as anchors, so an engineer's frozen work arranges the rest of the
        drawing instead of being arranged by it.
        """

        candidate = deepcopy(working)
        layout = AutoLayoutEngine(self.service).preview_document(
            candidate,
            AutoLayoutRequest(
                expected_revision=None,
                element_ids=scope_ids,
                locked_element_ids=sorted(locked),
                direction=request.direction,
                rank_gap=request.rank_gap,
                node_gap=request.node_gap,
                component_gap=request.component_gap,
                obstacle_margin=policy.obstacle_margin,
                lane_gap=policy.lane_gap,
                reroute_connectors=request.reroute_connectors,
                include_hidden=request.include_hidden,
            ),
        )
        skipped_locked.update(layout.skipped_locked_element_ids)
        staged = self._apply_operations(candidate, layout.transaction)
        if staged and commit("relayout", candidate):
            working.elements = candidate.elements
            working.metadata = candidate.metadata
            operations.extend(staged)
            moved.update(layout.moved_element_ids)
            rerouted.update(layout.rerouted_connector_ids)
            moved_annotations.update(layout.moved_annotation_ids)
            warnings.extend(layout.warnings)
            return True
        warnings.extend(layout.warnings)
        return False

    def _reserved_rects(self, document: Document) -> list[Rect]:
        """Declared reserved drawing space (legend / title block / notes / keep-clear).

        Charter §15 asks for legend and reserved-area avoidance. Reserved space is real,
        declared geometry, so the drafting passes treat it as an obstacle — routes are
        scored against it, collision moves avoid it, and labels prefer not to sit in it.
        Without this the check would only ever complain after the fact.
        """

        return [
            Rect(region.x1, region.y1, region.x2, region.y2)
            for region in declared_reserved_regions(document)
        ]

    @staticmethod
    def _mark_waived(
        findings: list[DraftingFinding],
        policy: DraftingPolicy,
    ) -> list[DraftingFinding]:
        """Mark waived findings instead of dropping them, so a waiver stays visible."""

        waived = set(policy.waived_codes)
        return [
            finding.model_copy(update={"waived": True})
            if finding.code in waived and not finding.waived
            else finding
            for finding in findings
        ]

    def _analyze(self, document: Document, policy: DraftingPolicy) -> ConnectionAnalysis:
        registry = self.service.symbols
        collisions = collision_findings(document, registry, policy)
        return ConnectionAnalysis(
            collisions=collisions,
            crossings=detect_crossings(document),
            junctions=classify_junctions(document),
            findings=[
                *structural_findings(document, registry, policy),
                *collisions,
            ],
        )

    def _scoped_connectors(
        self,
        document: Document,
        scope_ids: set[str],
        locked: set[str],
    ) -> list[ConnectorElement]:
        return [
            element
            for element in document.elements
            if element.type == "connector" and element.id in scope_ids and element.id not in locked
        ]

    def _connectors_touching(
        self,
        document: Document,
        element_ids: set[str],
    ) -> list[ConnectorElement]:
        result: list[ConnectorElement] = []
        for element in document.elements:
            if element.type != "connector":
                continue
            for endpoint in (element.source, element.target):
                if endpoint is not None and endpoint.element_id in element_ids:
                    result.append(element)
                    break
        return result

    def _apply_operations(
        self,
        document: Document,
        transaction: TransactionRequest | None,
    ) -> list[Operation]:
        """Apply another engine's transaction to *our* working copy, in its own order.

        The layout engine previews on its own copy, so its result has to be replayed
        here for the later evidence-based passes to see the moved geometry.
        """

        if transaction is None:
            return []
        operations = list(transaction.operations)
        for operation in operations:
            self.service._apply_operation(document, operation)
        return operations

    def _apply_patch(
        self,
        document: Document,
        element_id: str,
        patch: dict,
    ) -> Operation:
        operation = UpdateElementOperation(element_id=element_id, patch=patch)
        self.service._apply_operation(document, operation)
        return operation

    # ----------------------------------------------------------------- #
    # Stage implementations
    # ----------------------------------------------------------------- #

    def _route_score(
        self,
        document: Document,
        connector: ConnectorElement,
        points: Sequence[Point],
        policy: DraftingPolicy,
    ) -> RouteScore:
        element_map = {element.id: element for element in document.elements}
        normals: list[tuple[int, int] | None] = []
        for endpoint in (connector.source, connector.target):
            normal = None
            if endpoint is not None and endpoint.element_id and endpoint.port_id:
                bound = element_map.get(endpoint.element_id)
                if bound is not None and bound.type == "symbol":
                    normal = port_outward_normal(bound, endpoint.port_id, self.service.symbols)
            normals.append(normal)
        segment_list = connector_segments(
            connector.model_copy(update={"points": list(points)}, deep=True)
        )
        mismatches = _normal_mismatches(list(points), normals[0], normals[1])
        excluded = {
            endpoint.element_id
            for endpoint in (connector.source, connector.target)
            if endpoint is not None
        }
        obstacles = [
            rect.expanded(policy.route_clearance)
            for element in document.elements
            if element.id not in excluded
            if (rect := element_rect(element)) is not None
        ] + [
            rect.expanded(policy.route_clearance) for rect in self._reserved_rects(document)
        ]
        hits = sum(
            1
            for segment in segment_list
            if any(segment_intersects_rect(segment, rect) for rect in obstacles)
        )
        grid = max(5.0, document.canvas.grid_size)
        non_orthogonal = sum(
            not (segment.horizontal or segment.vertical) for segment in segment_list
        )
        micro = sum(0 < segment.length < grid - 1e-6 for segment in segment_list)
        bends = max(0, len(simplify(points)) - 2)
        length = round(sum(segment.length for segment in segment_list), 6)
        return (mismatches, hits, non_orthogonal, micro, bends, length)

    def _refine_route(
        self,
        document: Document,
        connector: ConnectorElement,
        policy: DraftingPolicy,
        *,
        forced: bool = False,
    ) -> list[Point] | None:
        """Re-route one connector, from its ports' current positions.

        A *forced* connector (its symbol moved in this run) is re-routed whenever the fresh
        route genuinely differs, because the stored route no longer describes the drawing.
        Otherwise the connector is only re-routed when its own route quality strictly
        improves — "tidy" never means "different for no reason".
        """

        if connector.source is None or connector.target is None:
            return None
        fresh = self._rebind_connector(document, connector)
        current = self._route_score(document, connector, connector.points, policy)
        proposal = simplify(
            route_connector_points(
                document,
                fresh,
                self.service.symbols,
                avoid_rects=[
                    (rect.x1, rect.y1, rect.x2, rect.y2)
                    for rect in self._reserved_rects(document)
                ],
            )
        )
        if len(proposal) < 2:
            return None
        if not forced:
            if self._route_score(document, fresh, proposal, policy) >= current:
                return None
            return proposal
        if proposal == simplify(connector.points) and (
            fresh.source is not None
            and connector.source is not None
            and fresh.source.point == connector.source.point
            and fresh.target is not None
            and connector.target is not None
            and fresh.target.point == connector.target.point
        ):
            # Forced only in principle: the route already ends where the ports are.
            return None
        return proposal

    def _place_annotations(
        self,
        document: Document,
        scope_ids: set[str],
        locked: set[str],
        scope_kind: str,
    ) -> list[Operation]:
        """Move a label only when the chosen candidate strictly reduces its cost."""

        element_map = {element.id: element for element in document.elements}
        # Reserved drawing space counts as an obstacle for label placement too: a label
        # inside the legend or title block is a real drawing defect (Charter §15).
        symbol_rects: list[Rect] = [
            rect.expanded(4)
            for element in document.elements
            if (rect := element_rect(element)) is not None
        ] + self._reserved_rects(document)
        connectors = [element for element in document.elements if element.type == "connector"]
        placed_rects: list[Rect] = []
        operations: list[Operation] = []
        texts = sorted(
            (
                element
                for element in document.elements
                if element.type == "text" and element.text.strip()
            ),
            key=lambda element: (
                0 if element.metadata.get("parent_element_id") else 1,
                element.id,
            ),
        )
        for text in texts:
            parent_id = text.metadata.get("parent_element_id")
            parent = element_map.get(parent_id) if isinstance(parent_id, str) else None
            movable = text.id not in locked and (
                scope_kind == "document"
                or text.id in scope_ids
                or (parent is not None and parent.id in scope_ids)
            )
            candidates = (
                _parent_candidates(text, parent) if parent is not None else _free_candidates(text)
            ) if movable else []
            if not candidates:
                placed_rects.append(text_rect(text))
                continue
            current_cost = _candidate_score(text, document, symbol_rects, placed_rects, connectors)
            scored = [
                (
                    _candidate_score(candidate, document, symbol_rects, placed_rects, connectors),
                    index,
                    candidate,
                )
                for index, (candidate, _remote) in enumerate(candidates)
            ]
            best_cost, _, best = min(scored, key=lambda item: (item[0], item[1]))
            if best_cost >= current_cost or (
                best.position == text.position and best.anchor == text.anchor
            ):
                placed_rects.append(text_rect(text))
                continue
            operations.append(
                self._apply_patch(
                    document,
                    text.id,
                    {
                        "position": best.position.model_dump(mode="json"),
                        "anchor": best.anchor,
                    },
                )
            )
            placed_rects.append(text_rect(best))
        return operations

    def _bridge_crossings(
        self,
        document: Document,
        scope_ids: set[str],
        locked: set[str],
        stage_findings: list[DraftingFinding],
    ) -> tuple[list[Operation], set[str]]:
        """Give every legal crossing exactly one bridge, on the secondary line.

        The primary line (declared flow first, then fewest bends) stays straight. A
        crossing involving a locked or out-of-scope connector is reported instead of
        edited: the engine does not touch geometry it was not allowed to touch.
        """

        operations: list[Operation] = []
        bridged: set[str] = set()
        connectors = {
            element.id: element for element in document.elements if element.type == "connector"
        }
        for crossing in detect_crossings(document):
            if crossing.at_junction_id:
                continue
            first = connectors.get(crossing.first_connector_id)
            second = connectors.get(crossing.second_connector_id)
            if first is None or second is None:
                continue
            candidates = [
                connector
                for connector in (first, second)
                if connector.id in scope_ids and connector.id not in locked
            ]
            primary = min((first, second), key=self._connector_priority)
            secondary = second if primary.id == first.id else first
            editable = {connector.id for connector in candidates}
            if crossing.bridged_by == "both":
                # Exactly one bridge, and it belongs on the secondary line: removing it
                # from the primary is only safe when the primary is editable.
                if primary.id in editable:
                    operations.append(
                        self._apply_patch(document, primary.id, {"crossing_style": "none"})
                    )
                continue
            if crossing.bridged_by:
                continue
            if secondary.id not in editable:
                stage_findings.append(
                    DraftingFinding(
                        severity="warning",
                        code="DRAFT_CROSSING_UNBRIDGED_LOCKED",
                        message=(
                            f"管线 {first.id} 与 {second.id} 交叉，次管线 {secondary.id} 被锁定或不在本次"
                            "范围内，未自动添加跨线桥（不把跨线桥画到主管线上）。"
                        ),
                        element_ids=[first.id, second.id],
                    )
                )
                continue
            operations.append(self._apply_patch(document, secondary.id, {"crossing_style": "jump"}))
            bridged.add(secondary.id)
        return operations, bridged

    @staticmethod
    def _connector_priority(connector: ConnectorElement) -> tuple[int, int, float, str]:
        """Lower is more primary: declared flow first, then fewer bends, then shorter."""

        declared_flow = 0 if connector.flow_direction != "none" else 1
        bends = max(0, len(connector.points) - 2)
        length = round(
            sum(segment.length for segment in connector_segments(connector)),
            6,
        )
        return (declared_flow, bends, length, connector.id)

    def _relax_collisions(
        self,
        document: Document,
        scope_ids: set[str],
        locked: set[str],
        policy: DraftingPolicy,
        request: DraftingRequest,
    ) -> list[Operation]:
        """Separate overlapping nodes with deterministic, one-at-a-time moves."""

        nodes: dict[str, Element] = {
            element.id: element
            for element in document.elements
            if element.type in {"symbol", "junction"} and element.id in scope_ids
        }
        rects = {
            element_id: rect
            for element_id, element in nodes.items()
            if (rect := element_rect(element)) is not None
        }
        ids = sorted(rects)
        overlap_pairs = [
            (first_id, second_id)
            for index, first_id in enumerate(ids)
            for second_id in ids[index + 1 :]
            if rects_overlap(rects[first_id], rects[second_id])
        ]
        if not overlap_pairs:
            return []
        fixed = [
            rect.expanded(policy.obstacle_margin)
            for element in document.elements
            if element.id not in scope_ids
            if (rect := element_rect(element)) is not None
        ] + [rect.expanded(policy.obstacle_margin) for rect in self._reserved_rects(document)]
        current_rects = dict(rects)
        operations: list[Operation] = []
        relocated: set[str] = set()
        for first_id, second_id in overlap_pairs:
            victim_id = second_id if second_id not in locked else first_id
            if victim_id in locked or victim_id in relocated:
                continue
            victim = nodes[victim_id]
            is_junction = victim.type == "junction"
            node = LayoutNode(
                element_id=victim_id,
                width=victim.radius * 2 if is_junction else victim.width,
                height=victim.radius * 2 if is_junction else victim.height,
                x=victim.position.x - victim.radius if is_junction else victim.position.x,
                y=victim.position.y - victim.radius if is_junction else victim.position.y,
                locked=False,
            )
            obstacles = [
                rect.expanded(policy.obstacle_margin)
                for element_id, rect in current_rects.items()
                if element_id != victim_id
            ] + fixed
            target_x, target_y = BaseLayoutEngine._find_free_position(
                node,
                node.x,
                node.y,
                obstacles,
                AutoLayoutRequest(
                    direction=request.direction,
                    rank_gap=request.rank_gap,
                    node_gap=request.node_gap,
                    component_gap=request.component_gap,
                    obstacle_margin=policy.obstacle_margin,
                    lane_gap=policy.lane_gap,
                ),
                document.canvas.grid_size,
            )
            if is_junction:
                target_x += victim.radius
                target_y += victim.radius
            if abs(target_x - victim.position.x) < 1e-6 and abs(target_y - victim.position.y) < 1e-6:
                continue
            operations.append(
                self._apply_patch(
                    document,
                    victim_id,
                    {"position": {"x": target_x, "y": target_y}},
                )
            )
            relocated.add(victim_id)
            updated = next(element for element in document.elements if element.id == victim_id)
            updated_rect = element_rect(updated)
            if updated_rect is not None:
                current_rects[victim_id] = updated_rect
        return operations

    def _evict_reserved_intruders(
        self,
        document: Document,
        scope_ids: set[str],
        locked: set[str],
        policy: DraftingPolicy,
        request: DraftingRequest,
    ) -> list[Operation]:
        """Move unlocked geometry out of declared reserved space.

        Charter §15: legend, title block and keep-clear zones are declared drawing data
        (``metadata.layout_regions``), so a device drawn on top of them is a real defect.
        The repair is the same kind of deterministic move as separating two overlapping
        symbols; the difference is that the obstacle is a zone rather than a node, so
        locked geometry is never pushed out -- it is reported instead.
        """

        reserved = self._reserved_rects(document)
        if not reserved:
            return []
        zones = [rect.expanded(policy.obstacle_margin) for rect in reserved]
        intruders = sorted(
            (
                element
                for element in document.elements
                if element.type in {"symbol", "junction"}
                and element.id in scope_ids
                and element.id not in locked
            ),
            key=lambda element: element.id,
        )
        if not intruders:
            return []
        fixed = [
            rect.expanded(policy.obstacle_margin)
            for element in document.elements
            if element.id not in scope_ids
            if (rect := element_rect(element)) is not None
        ]
        current_rects = {
            element.id: rect
            for element in document.elements
            if (rect := element_rect(element)) is not None
        }
        operations: list[Operation] = []
        for element in intruders:
            rect = element_rect(element)
            if rect is None or not any(rects_overlap(rect, zone) for zone in zones):
                continue
            is_junction = element.type == "junction"
            node = LayoutNode(
                element_id=element.id,
                width=element.radius * 2 if is_junction else element.width,
                height=element.radius * 2 if is_junction else element.height,
                x=element.position.x - element.radius if is_junction else element.position.x,
                y=element.position.y - element.radius if is_junction else element.position.y,
                locked=False,
            )
            obstacles = [
                rect.expanded(policy.obstacle_margin)
                for element_id, rect in current_rects.items()
                if element_id != element.id
            ] + fixed + zones
            target_x, target_y = BaseLayoutEngine._find_free_position(
                node,
                node.x,
                node.y,
                obstacles,
                AutoLayoutRequest(
                    direction=request.direction,
                    rank_gap=request.rank_gap,
                    node_gap=request.node_gap,
                    component_gap=request.component_gap,
                    obstacle_margin=policy.obstacle_margin,
                    lane_gap=policy.lane_gap,
                ),
                document.canvas.grid_size,
            )
            if is_junction:
                target_x += element.radius
                target_y += element.radius
            if (
                abs(target_x - element.position.x) < 1e-6
                and abs(target_y - element.position.y) < 1e-6
            ):
                continue
            operations.append(
                self._apply_patch(
                    document,
                    element.id,
                    {"position": {"x": target_x, "y": target_y}},
                )
            )
            moved_element = next(
                candidate for candidate in document.elements if candidate.id == element.id
            )
            moved_rect = element_rect(moved_element)
            if moved_rect is not None:
                current_rects[element.id] = moved_rect
        return operations

    # ----------------------------------------------------------------- #
    # Integrity and gate
    # ----------------------------------------------------------------- #

    def _integrity_findings(
        self,
        document: Document,
        operations: list[Operation],
        scope_ids: set[str],
        scope_kind: str,
        locked: set[str],
    ) -> list[DraftingFinding]:
        """Verify the invariants this engine promises, on the actual result.

        Structural checks, not opinions: an operation outside the requested scope, a
        modified locked element, a non-geometric operation or any change to connectivity
        is a blocker even when the drawing looks tidier afterwards.
        """

        findings: list[DraftingFinding] = []
        for operation in operations:
            if not isinstance(operation, UpdateElementOperation):
                findings.append(
                    DraftingFinding(
                        severity="blocker",
                        code="DRAFT_TOPOLOGY_CHANGED",
                        message=f"整理事务包含非几何操作 {getattr(operation, 'op', '?')}。",
                        element_ids=[getattr(operation, "element_id", "")],
                    )
                )
                continue
            element_id = operation.element_id
            if element_id in locked:
                findings.append(
                    DraftingFinding(
                        severity="blocker",
                        code="DRAFT_LOCKED_ELEMENT_MOVED",
                        message=f"锁定元素 {element_id} 被整理操作修改。",
                        element_ids=[element_id],
                    )
                )
            if scope_kind != "document" and element_id not in scope_ids:
                findings.append(
                    DraftingFinding(
                        severity="blocker",
                        code="DRAFT_OUT_OF_SCOPE_CHANGE",
                        message=f"整理操作修改了范围外元素 {element_id}。",
                        element_ids=[element_id],
                    )
                )
            if "source" in operation.patch or "target" in operation.patch:
                findings.append(
                    DraftingFinding(
                        severity="blocker",
                        code="DRAFT_TOPOLOGY_CHANGED",
                        message=f"整理操作改写了 {element_id} 的连接端点，属于拓扑变更。",
                        element_ids=[element_id],
                    )
                )
        return findings

    def _gate(
        self,
        document: Document,
        findings: list[DraftingFinding],
        state: DraftingSnapshot,
        policy: DraftingPolicy,
    ) -> DraftingGate:
        waived = set(policy.waived_codes)
        findings = self._mark_waived(findings, policy)
        blockers = sorted(
            (
                finding
                for finding in findings
                if not finding.waived
                and (
                    finding.code in DRAFTING_BLOCKER_CODES
                    or (
                        finding.severity in {"error", "blocker"}
                        and finding.code.startswith("DRAFT_")
                    )
                )
            ),
            key=lambda finding: (finding.code, finding.element_ids),
        )
        drawing_report = analyze_diagram_quality(document, self.service.symbols)
        drawing_issues = sorted(
            (
                DraftingFinding(
                    severity="error" if issue.severity == "error" else "warning",
                    code=issue.code,
                    message=issue.message,
                    element_ids=list(issue.element_ids),
                    details=dict(issue.details),
                    rule_source="diagram_quality",
                    waived=issue.code in waived,
                )
                for issue in drawing_report.issues
            ),
            key=lambda finding: (finding.severity != "error", finding.code, finding.element_ids),
        )
        blocking_drawing = [
            issue for issue in drawing_issues if issue.severity == "error" and not issue.waived
        ]
        return DraftingGate(
            passed=not blockers and not blocking_drawing and state.score >= policy.target_score,
            score=state.score,
            target_score=policy.target_score,
            blockers=blockers,
            drawing_issues=drawing_issues,
            waived_codes=sorted(waived),
            checked_codes=sorted({*DRAFTING_CODES}),
        )


__all__ = ["ConnectionAnalysis", "DraftingEngine"]
