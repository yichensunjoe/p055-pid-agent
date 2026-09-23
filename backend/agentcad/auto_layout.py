from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from heapq import heappop, heappush
from math import hypot
from typing import Literal

from .auto_layout_geometry import SemanticLayoutGeometry
from .auto_layout_semantic import SemanticTopologyIngress
from .diagram_quality import port_outward_normal
from .layout_models import (
    AutoLayoutMetrics,
    AutoLayoutPreview,
    AutoLayoutRequest,
    LayoutBounds,
    declared_lock_regions,
    element_is_locked,
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

EPSILON = 1e-6


@dataclass(frozen=True)
class Rect:
    x1: float
    y1: float
    x2: float
    y2: float
    element_id: str

    @property
    def width(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    def expanded(self, margin: float) -> Rect:
        return Rect(
            self.x1 - margin,
            self.y1 - margin,
            self.x2 + margin,
            self.y2 + margin,
            self.element_id,
        )


@dataclass(frozen=True)
class Segment:
    start: Point
    end: Point

    @property
    def horizontal(self) -> bool:
        return abs(self.start.y - self.end.y) <= EPSILON

    @property
    def vertical(self) -> bool:
        return abs(self.start.x - self.end.x) <= EPSILON

    @property
    def length(self) -> float:
        return abs(self.end.x - self.start.x) + abs(self.end.y - self.start.y)


@dataclass
class LayoutNode:
    element_id: str
    width: float
    height: float
    x: float
    y: float
    locked: bool
    rank: int = 0


class AutoLayoutEngine(SemanticLayoutGeometry, SemanticTopologyIngress):
    """The one layout authority.

    It gains a second way in for M7-2: :meth:`layout_semantic_topology` takes the
    engine-facing *semantic* input contract and receives the discrete layout intent. The
    document-shaped entry points are the manual path and keep ``preserve_positions``; the
    semantic ingress has no such parameter, by signature. Both paths end in the same
    algorithm, which is why this is a mixin on this class rather than a second engine beside
    it. Step 1 places nothing; placement, routing and the derived canvas arrive in the later
    phase-2B steps from the plan the ingress returns.

    :meth:`layout_semantic_geometry` continues that chain with step 3: real symbol geometry,
    endpoint bindings, orthogonal routing and annotations. It is a second mixin for the same
    reason as the first -- a router beside the engine would be a second geometry authority.
    """

    def __init__(self, service: DocumentService):
        self.service = service

    def preview(self, document_id: str, request: AutoLayoutRequest) -> AutoLayoutPreview:
        return self.preview_document(self.service.get_document(document_id), request)

    def preview_document(
        self,
        document: Document,
        request: AutoLayoutRequest,
    ) -> AutoLayoutPreview:
        """Preview layout for a supplied snapshot without persisting it.

        The semantic compiler uses this entry point for a newly generated drawing
        that has not yet been committed. The public preview API continues to load
        the persisted snapshot through :meth:`preview`.
        """
        if request.expected_revision is not None and request.expected_revision != document.revision:
            raise RevisionConflictError(
                f"expected revision {request.expected_revision}, current revision is {document.revision}"
            )

        visible_layer_ids = {
            layer.id for layer in document.layers if request.include_hidden or layer.visible
        }
        visible_system_ids = {
            system.id for system in document.systems if request.include_hidden or system.visible
        }
        locked_layer_ids = {layer.id for layer in document.layers if layer.locked}
        locked_ids = self._locked_element_ids(document, request)
        element_map = {element.id: element for element in document.elements}
        connectable_ids = {
            element.id
            for element in document.elements
            if element.type in {"symbol", "junction"}
            and element.layer_id in visible_layer_ids
            and element.system_id in visible_system_ids
        }
        scope_ids = self._resolve_scope(document, request.element_ids, connectable_ids)
        if not scope_ids:
            return AutoLayoutPreview(
                document_id=document.id,
                current_revision=document.revision,
                warnings=["没有可自动整理的设备或连接节点。"],
                metrics=self._metrics(document, document, request.obstacle_margin, request.lane_gap),
            )

        graph_connectors = [
            element
            for element in document.elements
            if element.type == "connector"
            and element.layer_id in visible_layer_ids
            and element.system_id in visible_system_ids
        ]
        nodes = self._make_nodes(document, scope_ids, locked_layer_ids, locked_ids)
        components = self._components(scope_ids, graph_connectors)
        positions = (
            {
                element_id: Point.model_validate(
                    element_map[element_id].position.model_dump(mode="python")
                )
                for element_id in nodes
            }
            if request.preserve_positions
            else self._layout_components(
                document,
                nodes,
                components,
                graph_connectors,
                request,
            )
        )

        working = Document.model_validate(document.model_dump(mode="python"))
        operations: list[Operation] = []
        moved_ids: list[str] = []
        skipped_locked = sorted(node.element_id for node in nodes.values() if node.locked)
        deltas: dict[str, tuple[float, float]] = {}

        for element_id, position in positions.items():
            node = nodes[element_id]
            if node.locked:
                continue
            current = element_map[element_id]
            if current.type not in {"symbol", "junction"}:
                continue
            current_x = current.position.x
            current_y = current.position.y
            if self._same(current_x, position.x) and self._same(current_y, position.y):
                continue
            operation = UpdateElementOperation(
                element_id=element_id,
                patch={"position": position.model_dump(mode="json")},
            )
            self.service._apply_operation(working, operation)
            operations.append(operation)
            moved_ids.append(element_id)
            deltas[element_id] = (position.x - current_x, position.y - current_y)

        moved_annotations = self._move_attached_annotations(
            working,
            operations,
            deltas,
            locked_layer_ids,
        )

        rerouted: list[str] = []
        warnings: list[str] = []
        if request.reroute_connectors:
            reroute_ids = self._connector_scope(
                working,
                request.element_ids,
                set(moved_ids),
                visible_layer_ids,
                visible_system_ids,
            )
            lane_segments: list[Segment] = []
            for connector_id in sorted(reroute_ids):
                connector = next(
                    (
                        element
                        for element in working.elements
                        if element.id == connector_id and element.type == "connector"
                    ),
                    None,
                )
                if connector is None:
                    continue
                if connector.layer_id in locked_layer_ids or connector.id in locked_ids:
                    if connector.id not in skipped_locked:
                        skipped_locked.append(connector.id)
                    continue
                if len(connector.points) < 2:
                    continue
                obstacles = self._routing_obstacles(
                    working,
                    connector,
                    request.obstacle_margin,
                    visible_layer_ids,
                    visible_system_ids,
                )
                route = self._route_bound_connector(
                    working,
                    connector,
                    obstacles,
                    lane_segments,
                    request.lane_gap,
                    document.canvas.grid_size,
                )
                if route is None:
                    warnings.append(
                        f"管线 {connector.id} 未找到完整避障路径，保留服务端基础正交路径。"
                    )
                    route = self.service._orthogonal_route(
                        connector.points[0], connector.points[-1]
                    )
                route = self._simplify(route)
                if self._same_points(route, connector.points) and connector.routing == "manual":
                    lane_segments.extend(self._segments(route))
                    continue
                operation = UpdateElementOperation(
                    element_id=connector.id,
                    patch={
                        "points": [point.model_dump(mode="json") for point in route],
                        "routing": "manual",
                    },
                )
                try:
                    self.service._apply_operation(working, operation)
                except InvalidOperationError:
                    fallback = self.service._orthogonal_route(
                        connector.points[0], connector.points[-1]
                    )
                    operation = UpdateElementOperation(
                        element_id=connector.id,
                        patch={
                            "points": [point.model_dump(mode="json") for point in fallback],
                            "routing": "manual",
                        },
                    )
                    self.service._apply_operation(working, operation)
                    warnings.append(
                        f"管线 {connector.id} 的避障路径未通过正交校验，已使用基础正交路径。"
                    )
                operations.append(operation)
                rerouted.append(connector.id)
                updated_connector = next(
                    element
                    for element in working.elements
                    if element.id == connector.id and element.type == "connector"
                )
                lane_segments.extend(self._segments(updated_connector.points))

        working = Document.model_validate(working.model_dump(mode="python"))
        metrics = self._metrics(document, working, request.obstacle_margin, request.lane_gap)
        transaction = None
        if operations:
            transaction = TransactionRequest(
                operations=operations,
                expected_revision=document.revision,
                label=(
                    f"Auto layout {len(moved_ids)} node(s), "
                    f"reroute {len(rerouted)} connector(s)"
                ),
                source="web",
            )
        else:
            warnings.append("当前布局已经满足本次整理条件，没有生成修改事务。")

        return AutoLayoutPreview(
            document_id=document.id,
            current_revision=document.revision,
            transaction=transaction,
            moved_element_ids=sorted(moved_ids),
            rerouted_connector_ids=sorted(rerouted),
            moved_annotation_ids=sorted(moved_annotations),
            skipped_locked_element_ids=sorted(set(skipped_locked)),
            locked_element_ids=sorted(locked_ids),
            warnings=warnings,
            metrics=metrics,
        )

    @staticmethod
    def _same(left: float, right: float) -> bool:
        return abs(left - right) <= EPSILON

    @classmethod
    def _same_points(cls, left: list[Point], right: list[Point]) -> bool:
        return len(left) == len(right) and all(
            cls._same(a.x, b.x) and cls._same(a.y, b.y)
            for a, b in zip(left, right, strict=False)
        )

    def _locked_element_ids(
        self,
        document: Document,
        request: AutoLayoutRequest,
    ) -> set[str]:
        """Resolve every source of manual lock into one set of frozen element ids.

        Three sources, deliberately kept separate but honoured together:

        1. ``request.locked_element_ids`` — a caller scoping one layout run.
        2. ``element.metadata['drafting_lock']`` — a human pin made in the editor,
           stored with the drawing so no caller (or model) has to remember it.
        3. elements fully inside a declared ``kind='lock'`` region — "this area was
           confirmed, arrange around it" (Charter §15 manual lock).

        Locked elements are never moved, never re-routed, and are used as fixed
        obstacles/anchors by the placement pass, so an automatic layout cannot quietly
        destroy human-reviewed work while still tidying anything around it.
        """

        locked: set[str] = {
            element_id for element_id in request.locked_element_ids if element_id
        }
        for element in document.elements:
            if element_is_locked(element):
                locked.add(element.id)
        regions = declared_lock_regions(document)
        if regions:
            for element in document.elements:
                rect = self._element_rect(element)
                if rect is None:
                    continue
                if any(
                    region.contains_box(rect.x1, rect.y1, rect.x2, rect.y2)
                    for region in regions
                ):
                    locked.add(element.id)
        return locked

    @staticmethod
    def _resolve_scope(
        document: Document,
        requested_ids: list[str],
        connectable_ids: set[str],
    ) -> set[str]:
        if not requested_ids:
            return set(connectable_ids)
        requested = set(requested_ids)
        scope = requested & connectable_ids
        for element in document.elements:
            if element.id not in requested or element.type != "connector":
                continue
            for endpoint in (element.source, element.target):
                if endpoint and endpoint.element_id in connectable_ids:
                    scope.add(endpoint.element_id)
        return scope

    @staticmethod
    def _make_nodes(
        document: Document,
        scope_ids: set[str],
        locked_layer_ids: set[str],
        locked_ids: set[str] | None = None,
    ) -> dict[str, LayoutNode]:
        result: dict[str, LayoutNode] = {}
        for element in document.elements:
            if element.id not in scope_ids or element.type not in {"symbol", "junction"}:
                continue
            if element.type == "symbol":
                width, height = element.width, element.height
                x, y = element.position.x, element.position.y
            else:
                width = height = element.radius * 2
                x, y = element.position.x - element.radius, element.position.y - element.radius
            result[element.id] = LayoutNode(
                element_id=element.id,
                width=width,
                height=height,
                x=x,
                y=y,
                locked=element.layer_id in locked_layer_ids
                or (locked_ids is not None and element.id in locked_ids),
            )
        return result

    @staticmethod
    def _components(
        scope_ids: set[str],
        connectors: list[ConnectorElement],
    ) -> list[list[str]]:
        adjacency: dict[str, set[str]] = {element_id: set() for element_id in scope_ids}
        for connector in connectors:
            source = connector.source.element_id if connector.source else None
            target = connector.target.element_id if connector.target else None
            if source in scope_ids and target in scope_ids and source != target:
                adjacency[source].add(target)
                adjacency[target].add(source)
        components: list[list[str]] = []
        remaining = set(scope_ids)
        while remaining:
            root = min(remaining)
            queue = deque([root])
            remaining.remove(root)
            component: list[str] = []
            while queue:
                current = queue.popleft()
                component.append(current)
                for neighbor in sorted(adjacency[current]):
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        queue.append(neighbor)
            components.append(component)
        return components

    def _layout_components(
        self,
        document: Document,
        nodes: dict[str, LayoutNode],
        components: list[list[str]],
        connectors: list[ConnectorElement],
        request: AutoLayoutRequest,
    ) -> dict[str, Point]:
        margin = max(document.canvas.grid_size * 2, request.obstacle_margin * 2)
        fixed_rects = [
            rect.expanded(request.obstacle_margin)
            for element in document.elements
            if element.type in {"symbol", "junction"}
            and (element.id not in nodes or nodes[element.id].locked)
            and (rect := self._element_rect(element)) is not None
        ]
        placed_rects: list[Rect] = []
        positions: dict[str, Point] = {}
        component_cursor = margin

        ordered_components = sorted(
            components,
            key=lambda ids: min(
                nodes[element_id].y if request.direction == "horizontal" else nodes[element_id].x
                for element_id in ids
            ),
        )
        for component in ordered_components:
            ranks = self._component_ranks(component, connectors, nodes, request.direction)
            for element_id, rank in ranks.items():
                nodes[element_id].rank = rank
            grouped: dict[int, list[LayoutNode]] = defaultdict(list)
            for element_id in component:
                grouped[nodes[element_id].rank].append(nodes[element_id])
            for values in grouped.values():
                values.sort(
                    key=lambda node: (
                        node.y if request.direction == "horizontal" else node.x,
                        node.element_id,
                    )
                )

            nominal: dict[str, tuple[float, float]] = {}
            rank_cursor = margin
            max_cross_extent = 0.0
            for rank in sorted(grouped):
                rank_nodes = grouped[rank]
                along_extent = max(
                    node.width if request.direction == "horizontal" else node.height
                    for node in rank_nodes
                )
                cross_cursor = component_cursor
                for node in rank_nodes:
                    x = rank_cursor if request.direction == "horizontal" else cross_cursor
                    y = cross_cursor if request.direction == "horizontal" else rank_cursor
                    nominal[node.element_id] = (x, y)
                    cross_extent = node.height if request.direction == "horizontal" else node.width
                    cross_cursor += cross_extent + request.node_gap
                max_cross_extent = max(max_cross_extent, cross_cursor - component_cursor)
                rank_cursor += along_extent + request.rank_gap

            locked_nodes = [nodes[element_id] for element_id in component if nodes[element_id].locked]
            shift_x = shift_y = 0.0
            if locked_nodes:
                anchor = min(locked_nodes, key=lambda item: item.element_id)
                nominal_x, nominal_y = nominal[anchor.element_id]
                shift_x = anchor.x - nominal_x
                shift_y = anchor.y - nominal_y

            for element_id in sorted(
                component,
                key=lambda item: (nodes[item].rank, nominal[item][1], item),
            ):
                node = nodes[element_id]
                if node.locked:
                    if element_id in nominal:
                        positions[element_id] = Point(x=node.x, y=node.y)
                    continue
                target_x, target_y = nominal[element_id]
                target_x = max(margin, target_x + shift_x)
                target_y = max(margin, target_y + shift_y)
                target_x, target_y = self._find_free_position(
                    node,
                    target_x,
                    target_y,
                    fixed_rects + placed_rects,
                    request,
                    document.canvas.grid_size,
                )
                positions[element_id] = Point(x=target_x, y=target_y)
                placed_rects.append(
                    Rect(
                        target_x - request.obstacle_margin,
                        target_y - request.obstacle_margin,
                        target_x + node.width + request.obstacle_margin,
                        target_y + node.height + request.obstacle_margin,
                        element_id,
                    )
                )
            component_cursor += max(max_cross_extent, request.component_gap) + request.component_gap
        return positions

    def _component_ranks(
        self,
        component: list[str],
        connectors: list[ConnectorElement],
        nodes: dict[str, LayoutNode],
        direction: Literal["horizontal", "vertical"],
    ) -> dict[str, int]:
        component_set = set(component)
        directed: dict[str, set[str]] = {element_id: set() for element_id in component}
        for connector in connectors:
            source = connector.source.element_id if connector.source else None
            target = connector.target.element_id if connector.target else None
            if source in component_set and target in component_set and source != target:
                directed[source].add(target)

        components = self._strongly_connected(component, directed)
        component_index = {
            element_id: index
            for index, values in enumerate(components)
            for element_id in values
        }
        dag: dict[int, set[int]] = {index: set() for index in range(len(components))}
        indegree = {index: 0 for index in range(len(components))}
        for source, targets in directed.items():
            source_index = component_index[source]
            for target in targets:
                target_index = component_index[target]
                if source_index == target_index or target_index in dag[source_index]:
                    continue
                dag[source_index].add(target_index)
                indegree[target_index] += 1
        queue = deque(sorted(index for index, degree in indegree.items() if degree == 0))
        ranks = {index: 0 for index in dag}
        while queue:
            current = queue.popleft()
            for target in sorted(dag[current]):
                ranks[target] = max(ranks[target], ranks[current] + 1)
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if not queue and components:
            order = sorted(
                range(len(components)),
                key=lambda index: min(
                    nodes[element_id].x if direction == "horizontal" else nodes[element_id].y
                    for element_id in components[index]
                ),
            )
            ranks.update({index: position for position, index in enumerate(order)})
        return {
            element_id: ranks[component_index[element_id]]
            for element_id in component
        }

    @staticmethod
    def _strongly_connected(
        nodes: list[str],
        adjacency: dict[str, set[str]],
    ) -> list[list[str]]:
        index = 0
        stack: list[str] = []
        on_stack: set[str] = set()
        indices: dict[str, int] = {}
        lowlinks: dict[str, int] = {}
        result: list[list[str]] = []

        def visit(node: str) -> None:
            nonlocal index
            indices[node] = index
            lowlinks[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)
            for neighbor in sorted(adjacency[node]):
                if neighbor not in indices:
                    visit(neighbor)
                    lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
                elif neighbor in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[neighbor])
            if lowlinks[node] != indices[node]:
                return
            component: list[str] = []
            while stack:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            result.append(sorted(component))

        for node in sorted(nodes):
            if node not in indices:
                visit(node)
        return result

    @staticmethod
    def _find_free_position(
        node: LayoutNode,
        target_x: float,
        target_y: float,
        obstacles: list[Rect],
        request: AutoLayoutRequest,
        grid_size: float,
    ) -> tuple[float, float]:
        step = max(4.0, min(grid_size, request.node_gap / 2))
        for along_step in range(0, 30):
            for cross_step in range(0, 240):
                if request.direction == "horizontal":
                    x = target_x + along_step * step
                    y = target_y + cross_step * step
                else:
                    x = target_x + cross_step * step
                    y = target_y + along_step * step
                candidate = Rect(
                    x - request.obstacle_margin,
                    y - request.obstacle_margin,
                    x + node.width + request.obstacle_margin,
                    y + node.height + request.obstacle_margin,
                    node.element_id,
                )
                if not any(AutoLayoutEngine._rects_overlap(candidate, item) for item in obstacles):
                    return x, y
        return target_x, target_y

    @staticmethod
    def _move_attached_annotations(
        working: Document,
        operations: list[Operation],
        deltas: dict[str, tuple[float, float]],
        locked_layer_ids: set[str],
    ) -> list[str]:
        moved: list[str] = []
        attachment_keys = ("parent_element_id", "attached_to", "element_id")
        for element in list(working.elements):
            if element.type != "text" or element.layer_id in locked_layer_ids:
                continue
            parent_id = next(
                (
                    element.metadata.get(key)
                    for key in attachment_keys
                    if isinstance(element.metadata.get(key), str)
                ),
                None,
            )
            if parent_id not in deltas:
                continue
            dx, dy = deltas[parent_id]
            operation = UpdateElementOperation(
                element_id=element.id,
                patch={
                    "position": {
                        "x": element.position.x + dx,
                        "y": element.position.y + dy,
                    }
                },
            )
            operations.append(operation)
            moved.append(element.id)
        return moved

    @staticmethod
    def _connector_scope(
        document: Document,
        requested_ids: list[str],
        moved_ids: set[str],
        visible_layer_ids: set[str],
        visible_system_ids: set[str],
    ) -> set[str]:
        requested = set(requested_ids)
        result: set[str] = set()
        whole_document = not requested_ids
        for element in document.elements:
            if (
                element.type != "connector"
                or element.layer_id not in visible_layer_ids
                or element.system_id not in visible_system_ids
            ):
                continue
            source_id = element.source.element_id if element.source else None
            target_id = element.target.element_id if element.target else None
            if (
                whole_document
                or element.id in requested
                or source_id in moved_ids
                or target_id in moved_ids
            ):
                result.add(element.id)
        return result

    def _routing_obstacles(
        self,
        document: Document,
        connector: ConnectorElement,
        margin: float,
        visible_layer_ids: set[str],
        visible_system_ids: set[str],
    ) -> list[Rect]:
        excluded = {
            connector.source.element_id if connector.source else None,
            connector.target.element_id if connector.target else None,
        }
        result: list[Rect] = []
        for element in document.elements:
            if (
                element.id in excluded
                or element.type not in {"symbol", "junction"}
                or element.layer_id not in visible_layer_ids
                or element.system_id not in visible_system_ids
            ):
                continue
            rect = self._element_rect(element)
            if rect is not None:
                result.append(rect.expanded(margin))
        return result

    def _route_connector(
        self,
        start: Point,
        end: Point,
        obstacles: list[Rect],
        lane_segments: list[Segment],
        lane_gap: float,
        grid_size: float,
        source_normal: tuple[int, int] | None = None,
        target_normal: tuple[int, int] | None = None,
    ) -> list[Point] | None:
        if self._same(start.x, end.x) or self._same(start.y, end.y):
            direct = Segment(start, end)
            if self._segment_clear(direct, obstacles):
                return [Point.model_validate(start.model_dump()), Point.model_validate(end.model_dump())]

        relevant = self._relevant_obstacles(start, end, obstacles, lane_gap)
        xs = {start.x, end.x}
        ys = {start.y, end.y}
        step = max(4.0, min(grid_size, lane_gap))
        shared_lane_offset = max(grid_size * 2, lane_gap * 2)
        xs.update(
            {
                min(start.x, end.x) - shared_lane_offset,
                max(start.x, end.x) + shared_lane_offset,
            }
        )
        ys.update(
            {
                min(start.y, end.y) - shared_lane_offset,
                max(start.y, end.y) + shared_lane_offset,
            }
        )
        for rect in relevant:
            xs.update({rect.x1, rect.x2, rect.x1 - lane_gap, rect.x2 + lane_gap})
            ys.update({rect.y1, rect.y2, rect.y1 - lane_gap, rect.y2 + lane_gap})
        xs.update({start.x - step, start.x + step, end.x - step, end.x + step})
        ys.update({start.y - step, start.y + step, end.y - step, end.y + step})
        x_values = sorted(xs)
        y_values = sorted(ys)

        points: list[Point] = []
        point_index: dict[tuple[float, float], int] = {}
        for x in x_values:
            for y in y_values:
                point = Point(x=x, y=y)
                if not self._same_point(point, start) and not self._same_point(point, end):
                    if any(self._point_inside(point, rect) for rect in relevant):
                        continue
                point_index[(x, y)] = len(points)
                points.append(point)
        for point in (start, end):
            key = (point.x, point.y)
            if key not in point_index:
                point_index[key] = len(points)
                points.append(Point.model_validate(point.model_dump()))

        neighbors: dict[int, list[tuple[int, Segment]]] = defaultdict(list)
        by_x: dict[float, list[int]] = defaultdict(list)
        by_y: dict[float, list[int]] = defaultdict(list)
        for index, point in enumerate(points):
            by_x[point.x].append(index)
            by_y[point.y].append(index)
        for indices in by_x.values():
            indices.sort(key=lambda value: points[value].y)
            self._connect_adjacent(
                indices,
                points,
                neighbors,
                relevant,
                minimum_leg=grid_size,
            )
        for indices in by_y.values():
            indices.sort(key=lambda value: points[value].x)
            self._connect_adjacent(
                indices,
                points,
                neighbors,
                relevant,
                minimum_leg=grid_size,
            )

        start_index = point_index[(start.x, start.y)]
        end_index = point_index[(end.x, end.y)]
        queue: list[tuple[float, int, int]] = [(0.0, start_index, 0)]
        distance: dict[tuple[int, int], float] = {(start_index, 0): 0.0}
        previous: dict[tuple[int, int], tuple[int, int]] = {}
        final_state: tuple[int, int] | None = None
        bend_penalty = max(lane_gap * 1.5, grid_size)

        while queue:
            cost, current, incoming_direction = heappop(queue)
            state = (current, incoming_direction)
            if cost > distance.get(state, float("inf")) + EPSILON:
                continue
            if current == end_index:
                final_state = state
                break
            for neighbor, segment in neighbors.get(current, []):
                dx = segment.end.x - segment.start.x
                dy = segment.end.y - segment.start.y
                if (
                    current == start_index
                    and source_normal is not None
                    and dx * source_normal[0] + dy * source_normal[1] < -EPSILON
                ):
                    continue
                if (
                    neighbor == end_index
                    and target_normal is not None
                    and dx * target_normal[0] + dy * target_normal[1] > EPSILON
                ):
                    continue
                direction = 1 if segment.horizontal else 2
                next_cost = cost + segment.length
                if segment.length < grid_size - EPSILON:
                    next_cost += (grid_size - segment.length + 1) * 10_000
                if incoming_direction and incoming_direction != direction:
                    next_cost += bend_penalty
                next_cost += self._lane_penalty(segment, lane_segments, lane_gap)
                next_state = (neighbor, direction)
                if next_cost + EPSILON >= distance.get(next_state, float("inf")):
                    continue
                distance[next_state] = next_cost
                previous[next_state] = state
                heappush(queue, (next_cost, neighbor, direction))

        if final_state is None:
            return None
        path_indices: list[int] = []
        state = final_state
        while True:
            path_indices.append(state[0])
            if state == (start_index, 0):
                break
            state = previous[state]
        path_indices.reverse()
        return [Point.model_validate(points[index].model_dump()) for index in path_indices]

    def _route_bound_connector(
        self,
        document: Document,
        connector: ConnectorElement,
        obstacles: list[Rect],
        lane_segments: list[Segment],
        lane_gap: float,
        grid_size: float,
    ) -> list[Point] | None:
        start = connector.points[0]
        end = connector.points[-1]
        element_map = {element.id: element for element in document.elements}

        def endpoint_normal(endpoint):
            if endpoint is None or not endpoint.element_id or not endpoint.port_id:
                return None
            element = element_map.get(endpoint.element_id)
            if element is None or element.type != "symbol":
                return None
            return port_outward_normal(element, endpoint.port_id, self.service.symbols)

        source_normal = endpoint_normal(connector.source)
        target_normal = endpoint_normal(connector.target)
        guarded_obstacles = list(obstacles)
        for endpoint in (connector.source, connector.target):
            if endpoint is None or not endpoint.element_id:
                continue
            element = element_map.get(endpoint.element_id)
            if element is None:
                continue
            rect = self._element_rect(element)
            if rect is not None:
                guarded_obstacles.append(rect.expanded(2.0))
        stub = max(20.0, grid_size)
        start_stub = (
            Point(
                x=start.x + source_normal[0] * stub,
                y=start.y + source_normal[1] * stub,
            )
            if source_normal is not None
            else start
        )
        end_stub = (
            Point(
                x=end.x + target_normal[0] * stub,
                y=end.y + target_normal[1] * stub,
            )
            if target_normal is not None
            else end
        )
        middle = self._route_connector(
            start_stub,
            end_stub,
            guarded_obstacles,
            lane_segments,
            lane_gap,
            grid_size,
            source_normal,
            target_normal,
        )
        if middle is None:
            return None
        return self._simplify([start, start_stub, *middle, end_stub, end])

    @staticmethod
    def _relevant_obstacles(
        start: Point,
        end: Point,
        obstacles: list[Rect],
        lane_gap: float,
    ) -> list[Rect]:
        min_x = min(start.x, end.x) - lane_gap * 8
        max_x = max(start.x, end.x) + lane_gap * 8
        min_y = min(start.y, end.y) - lane_gap * 8
        max_y = max(start.y, end.y) + lane_gap * 8
        nearby = [
            rect
            for rect in obstacles
            if rect.x2 >= min_x and rect.x1 <= max_x and rect.y2 >= min_y and rect.y1 <= max_y
        ]
        if len(nearby) <= 60:
            return nearby
        midpoint = ((start.x + end.x) / 2, (start.y + end.y) / 2)
        return sorted(
            nearby,
            key=lambda rect: hypot(rect.center[0] - midpoint[0], rect.center[1] - midpoint[1]),
        )[:60]

    @classmethod
    def _connect_adjacent(
        cls,
        indices: list[int],
        points: list[Point],
        neighbors: dict[int, list[tuple[int, Segment]]],
        obstacles: list[Rect],
        *,
        minimum_leg: float = 0.0,
    ) -> None:
        for position, left in enumerate(indices[:-1]):
            for right in indices[position + 1 :]:
                segment = Segment(points[left], points[right])
                if segment.length <= EPSILON:
                    continue
                if not cls._segment_clear(segment, obstacles):
                    break
                if segment.length < minimum_leg - EPSILON:
                    continue
                neighbors[left].append((right, segment))
                neighbors[right].append((left, Segment(segment.end, segment.start)))
                break

    @classmethod
    def _segment_clear(cls, segment: Segment, obstacles: list[Rect]) -> bool:
        return not any(cls._segment_intersects_rect(segment, rect) for rect in obstacles)

    @staticmethod
    def _lane_penalty(segment: Segment, used: list[Segment], lane_gap: float) -> float:
        penalty = 0.0
        for other in used:
            if segment.horizontal and other.horizontal:
                separation = abs(segment.start.y - other.start.y)
                if separation >= lane_gap:
                    continue
                overlap = AutoLayoutEngine._interval_overlap(
                    segment.start.x,
                    segment.end.x,
                    other.start.x,
                    other.end.x,
                )
            elif segment.vertical and other.vertical:
                separation = abs(segment.start.x - other.start.x)
                if separation >= lane_gap:
                    continue
                overlap = AutoLayoutEngine._interval_overlap(
                    segment.start.y,
                    segment.end.y,
                    other.start.y,
                    other.end.y,
                )
            else:
                continue
            if overlap > EPSILON:
                penalty += overlap * (1.0 + (lane_gap - separation) / max(lane_gap, 1.0))
        return penalty

    @staticmethod
    def _interval_overlap(a1: float, a2: float, b1: float, b2: float) -> float:
        left = max(min(a1, a2), min(b1, b2))
        right = min(max(a1, a2), max(b1, b2))
        return max(0.0, right - left)

    @staticmethod
    def _point_inside(point: Point, rect: Rect) -> bool:
        return (
            rect.x1 + EPSILON < point.x < rect.x2 - EPSILON
            and rect.y1 + EPSILON < point.y < rect.y2 - EPSILON
        )

    @staticmethod
    def _same_point(left: Point, right: Point) -> bool:
        return abs(left.x - right.x) <= EPSILON and abs(left.y - right.y) <= EPSILON

    @classmethod
    def _segment_intersects_rect(cls, segment: Segment, rect: Rect) -> bool:
        if segment.horizontal:
            y = segment.start.y
            if not (rect.y1 + EPSILON < y < rect.y2 - EPSILON):
                return False
            return cls._interval_overlap(segment.start.x, segment.end.x, rect.x1, rect.x2) > EPSILON
        if segment.vertical:
            x = segment.start.x
            if not (rect.x1 + EPSILON < x < rect.x2 - EPSILON):
                return False
            return cls._interval_overlap(segment.start.y, segment.end.y, rect.y1, rect.y2) > EPSILON
        return True

    @staticmethod
    def _rects_overlap(left: Rect, right: Rect) -> bool:
        return not (
            left.x2 <= right.x1 + EPSILON
            or right.x2 <= left.x1 + EPSILON
            or left.y2 <= right.y1 + EPSILON
            or right.y2 <= left.y1 + EPSILON
        )

    @staticmethod
    def _element_rect(element: Element) -> Rect | None:
        if element.type == "symbol":
            return Rect(
                element.position.x,
                element.position.y,
                element.position.x + element.width,
                element.position.y + element.height,
                element.id,
            )
        if element.type == "junction":
            return Rect(
                element.position.x - element.radius,
                element.position.y - element.radius,
                element.position.x + element.radius,
                element.position.y + element.radius,
                element.id,
            )
        return None

    @classmethod
    def _simplify(cls, points: list[Point]) -> list[Point]:
        deduped: list[Point] = []
        for point in points:
            if deduped and cls._same_point(deduped[-1], point):
                continue
            deduped.append(Point.model_validate(point.model_dump()))
        changed = True
        while changed and len(deduped) > 2:
            changed = False
            result = [deduped[0]]
            for index in range(1, len(deduped) - 1):
                previous = result[-1]
                current = deduped[index]
                following = deduped[index + 1]
                if (
                    cls._same(previous.x, current.x)
                    and cls._same(current.x, following.x)
                ) or (
                    cls._same(previous.y, current.y)
                    and cls._same(current.y, following.y)
                ):
                    changed = True
                    continue
                result.append(current)
            result.append(deduped[-1])
            deduped = result
        return deduped

    @staticmethod
    def _segments(points: list[Point]) -> list[Segment]:
        return [Segment(first, second) for first, second in zip(points, points[1:], strict=False)]

    def _metrics(
        self,
        before: Document,
        after: Document,
        obstacle_margin: float,
        lane_gap: float,
    ) -> AutoLayoutMetrics:
        before_nodes = [
            rect
            for element in before.elements
            if (rect := self._element_rect(element)) is not None
        ]
        after_nodes = [
            rect
            for element in after.elements
            if (rect := self._element_rect(element)) is not None
        ]
        before_connectors = [
            element for element in before.elements if element.type == "connector"
        ]
        after_connectors = [
            element for element in after.elements if element.type == "connector"
        ]
        return AutoLayoutMetrics(
            node_count=len(after_nodes),
            connector_count=len(after_connectors),
            overlaps_before=self._overlap_count(before_nodes),
            overlaps_after=self._overlap_count(after_nodes),
            pipe_obstacle_intersections_before=self._pipe_obstacle_intersections(
                before,
                before_connectors,
                obstacle_margin,
            ),
            pipe_obstacle_intersections_after=self._pipe_obstacle_intersections(
                after,
                after_connectors,
                obstacle_margin,
            ),
            shared_lane_segments_before=self._shared_lane_count(before_connectors, lane_gap),
            shared_lane_segments_after=self._shared_lane_count(after_connectors, lane_gap),
            total_route_length_before=self._route_length(before_connectors),
            total_route_length_after=self._route_length(after_connectors),
            bounds_before=self._bounds(before_nodes),
            bounds_after=self._bounds(after_nodes),
        )

    @classmethod
    def _overlap_count(cls, rects: list[Rect]) -> int:
        return sum(
            1
            for index, left in enumerate(rects)
            for right in rects[index + 1 :]
            if cls._rects_overlap(left, right)
        )

    def _pipe_obstacle_intersections(
        self,
        document: Document,
        connectors: list[ConnectorElement],
        margin: float,
    ) -> int:
        rects = {
            element.id: rect.expanded(margin)
            for element in document.elements
            if (rect := self._element_rect(element)) is not None
        }
        count = 0
        for connector in connectors:
            excluded = {
                connector.source.element_id if connector.source else None,
                connector.target.element_id if connector.target else None,
            }
            obstacles = [rect for element_id, rect in rects.items() if element_id not in excluded]
            count += sum(
                1
                for segment in self._segments(connector.points)
                if any(self._segment_intersects_rect(segment, rect) for rect in obstacles)
            )
        return count

    def _shared_lane_count(
        self,
        connectors: list[ConnectorElement],
        lane_gap: float,
    ) -> int:
        segments = [segment for connector in connectors for segment in self._segments(connector.points)]
        count = 0
        for index, left in enumerate(segments):
            for right in segments[index + 1 :]:
                if left.horizontal and right.horizontal:
                    if abs(left.start.y - right.start.y) >= lane_gap:
                        continue
                    overlap = self._interval_overlap(
                        left.start.x, left.end.x, right.start.x, right.end.x
                    )
                elif left.vertical and right.vertical:
                    if abs(left.start.x - right.start.x) >= lane_gap:
                        continue
                    overlap = self._interval_overlap(
                        left.start.y, left.end.y, right.start.y, right.end.y
                    )
                else:
                    continue
                if overlap > EPSILON:
                    count += 1
        return count

    @staticmethod
    def _route_length(connectors: Iterable[ConnectorElement]) -> float:
        return round(
            sum(
                abs(second.x - first.x) + abs(second.y - first.y)
                for connector in connectors
                for first, second in zip(connector.points, connector.points[1:], strict=False)
            ),
            3,
        )

    @staticmethod
    def _bounds(rects: list[Rect]) -> LayoutBounds:
        if not rects:
            return LayoutBounds()
        min_x = min(rect.x1 for rect in rects)
        min_y = min(rect.y1 for rect in rects)
        max_x = max(rect.x2 for rect in rects)
        max_y = max(rect.y2 for rect in rects)
        return LayoutBounds(
            min_x=min_x,
            min_y=min_y,
            max_x=max_x,
            max_y=max_y,
            width=max_x - min_x,
            height=max_y - min_y,
        )
